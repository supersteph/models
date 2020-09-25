# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""BERT models that are compatible with TF 2.0."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import gin
import tensorflow as tf
import tensorflow_hub as hub

from official.nlp.configs import electra
from official.nlp.bert import bert_models
from official.nlp.modeling import losses
from official.nlp.modeling import models


class ElectraPretrainLossAndMetricLayer(tf.keras.layers.Layer):
  """Returns layer that computes custom loss and metrics for pretraining."""

  def __init__(self, vocab_size, discrim_rate, **kwargs):
    super(ElectraPretrainLossAndMetricLayer, self).__init__(**kwargs)
    self._vocab_size = vocab_size
    self._discrim_rate = discrim_rate
    self.config = {
        'vocab_size': vocab_size,
        'discrim_rate': discrim_rate
    }

  def _add_metrics(self, lm_output, lm_labels, lm_label_weights,
                   lm_example_loss, discrim_output, discrim_label,
                   discrim_loss, tot_loss):
    """Adds metrics."""
    masked_lm_accuracy = tf.keras.metrics.sparse_categorical_accuracy(
        lm_labels, lm_output)
    numerator = tf.reduce_sum(
        masked_lm_accuracy * tf.cast(lm_label_weights, tf.float32))
    denominator = tf.reduce_sum(tf.cast(lm_label_weights, tf.float32)) + 1e-5
    masked_lm_accuracy = numerator / denominator

    discrim_accuracy = tf.keras.metrics.binary_accuracy(
        tf.cast(discrim_label, tf.float32), discrim_output)
    self.add_metric(discrim_accuracy, name='discriminator_accuracy',
                    aggregation='mean')
    self.add_metric(
        masked_lm_accuracy, name='masked_lm_accuracy', aggregation='mean')

    self.add_metric(lm_example_loss, name='lm_example_loss', aggregation='mean')

    self.add_metric(tot_loss, name='total_loss', aggregation='mean')
    self.add_metric(discrim_loss, name='discriminator_loss', aggregation='mean')


  def call(self, lm_output, lm_label_ids, lm_label_weights,
           discrim_output, discrim_labels):
    """Implements call() for the layer."""
    weights = tf.cast(lm_label_weights, tf.float32)
    lm_output = tf.cast(lm_output, tf.float32)
    discrim_output = tf.cast(discrim_output, tf.float32)
    mask_label_loss = losses.weighted_sparse_categorical_crossentropy_loss(
        labels=lm_label_ids, predictions=lm_output, weights=weights)
    discrim_ind_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        logits=discrim_output,
        labels=tf.cast(discrim_labels, tf.float32))
    discrim_loss = tf.reduce_sum(discrim_ind_loss)
    loss = mask_label_loss + self.config["discrim_rate"] * discrim_loss


    self._add_metrics(lm_output, lm_label_ids, lm_label_weights,
                      mask_label_loss, discrim_output, discrim_labels,
                      discrim_loss, loss)
    return loss



def pretrain_model(electra_config,
                   seq_length,
                   max_predictions_per_seq,
                   initializer=None):
  """Returns model to be used for pre-training.
  Args:
      bert_config: Configuration that defines the core BERT model.
      seq_length: Maximum sequence length of the training data.
      max_predictions_per_seq: Maximum number of tokens in sequence to mask out
        and use for pretraining.
      initializer: Initializer for weights in BertPretrainer.
  Returns:
      Pretraining model as well as core BERT submodel from which to save
      weights after pretraining.
  """
  input_word_ids = tf.keras.layers.Input(
      shape=(seq_length,), name='input_word_ids', dtype=tf.int32)
  input_mask = tf.keras.layers.Input(
      shape=(seq_length,), name='input_mask', dtype=tf.int32)
  input_type_ids = tf.keras.layers.Input(
      shape=(seq_length,), name='input_type_ids', dtype=tf.int32)
  masked_lm_positions = tf.keras.layers.Input(
      shape=(max_predictions_per_seq,),
      name='masked_lm_positions',
      dtype=tf.int32)
  masked_lm_ids = tf.keras.layers.Input(
      shape=(max_predictions_per_seq,), name='masked_lm_ids', dtype=tf.int32)
  masked_lm_weights = tf.keras.layers.Input(
      shape=(max_predictions_per_seq,),
      name='masked_lm_weights',
      dtype=tf.int32)
  gen_encoder = bert_models.get_transformer_encoder(
      electra_config.generator_encoder,
      seq_length)
  discrim_encoder = bert_models.get_transformer_encoder(
      electra_config.discriminator_encoder,
      seq_length)
  if initializer is None:
    initializer = tf.keras.initializers.TruncatedNormal(
        stddev=electra_config.generator_encoder.initializer_range)
  pretrainer_model = models.ElectraPretrainer(
      generator_network=gen_encoder,
      discriminator_network=discrim_encoder,
      vocab_size=electra_config.generator_encoder.vocab_size,
      num_classes=2,
      sequence_length=seq_length,
      last_hidden_dim=electra_config.generator_encoder.hidden_size,
      num_token_predictions=max_predictions_per_seq,
      initializer=initializer,
      output='predictions')

  lm_output, sentence_output,discrim_logits, discrim_labels = pretrainer_model(
      {
          'input_word_ids': input_word_ids,
          'input_mask': input_mask,
          'input_type_ids': input_type_ids,
          'masked_lm_positions': masked_lm_positions,
          'masked_lm_ids': masked_lm_ids,
          'masked_lm_weights': masked_lm_weights,
      })

  pretrain_loss_layer = ElectraPretrainLossAndMetricLayer(
      vocab_size=electra_config.generator_encoder.vocab_size,
      discrim_rate=electra_config.discriminator_loss_weight)
  output_loss = pretrain_loss_layer(lm_output, masked_lm_ids, masked_lm_weights,
                                    discrim_logits, discrim_labels)
  keras_model = tf.keras.Model(
      inputs={
          'input_word_ids': input_word_ids,
          'input_mask': input_mask,
          'input_type_ids': input_type_ids,
          'masked_lm_positions': masked_lm_positions,
          'masked_lm_ids': masked_lm_ids,
          'masked_lm_weights': masked_lm_weights,
      },
      outputs=output_loss)
  return keras_model, discrim_encoder


def squad_model(bert_config,
                max_seq_length,
                initializer=None,
                hub_module_url=None,
                hub_module_trainable=True):
  """Returns BERT Squad model along with core BERT model to import weights.
  Args:
    bert_config: BertConfig, the config defines the core Bert model.
    max_seq_length: integer, the maximum input sequence length.
    initializer: Initializer for the final dense layer in the span labeler.
      Defaulted to TruncatedNormal initializer.
    hub_module_url: TF-Hub path/url to Bert module.
    hub_module_trainable: True to finetune layers in the hub module.
  Returns:
    A tuple of (1) keras model that outputs start logits and end logits and
    (2) the core BERT transformer encoder.
  """
  if initializer is None:
    initializer = tf.keras.initializers.TruncatedNormal(
        stddev=bert_config.initializer_range)
  if not hub_module_url:
    bert_encoder = bert_models.get_transformer_encoder(bert_config,
                                                       max_seq_length)
    return models.BertSpanLabeler(
        network=bert_encoder, initializer=initializer), bert_encoder

  input_word_ids = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_word_ids')
  input_mask = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_mask')
  input_type_ids = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_type_ids')
  core_model = hub.KerasLayer(hub_module_url, trainable=hub_module_trainable)
  pooled_output, sequence_output = core_model(
      [input_word_ids, input_mask, input_type_ids])
  bert_encoder = tf.keras.Model(
      inputs={
          'input_word_ids': input_word_ids,
          'input_mask': input_mask,
          'input_type_ids': input_type_ids,
      },
      outputs=[sequence_output, pooled_output],
      name='core_model')
  return models.BertSpanLabeler(
      network=bert_encoder, initializer=initializer), bert_encoder


def classifier_model(bert_config,
                     num_labels,
                     max_seq_length,
                     final_layer_initializer=None,
                     hub_module_url=None,
                     hub_module_trainable=True):
  """BERT classifier model in functional API style.
  Construct a Keras model for predicting `num_labels` outputs from an input with
  maximum sequence length `max_seq_length`.
  Args:
    bert_config: BertConfig or AlbertConfig, the config defines the core BERT or
      ALBERT model.
    num_labels: integer, the number of classes.
    max_seq_length: integer, the maximum input sequence length.
    final_layer_initializer: Initializer for final dense layer. Defaulted
      TruncatedNormal initializer.
    hub_module_url: TF-Hub path/url to Bert module.
    hub_module_trainable: True to finetune layers in the hub module.
  Returns:
    Combined prediction model (words, mask, type) -> (one-hot labels)
    BERT sub-model (words, mask, type) -> (bert_outputs)
  """
  if final_layer_initializer is not None:
    initializer = final_layer_initializer
  else:
    initializer = tf.keras.initializers.TruncatedNormal(
        stddev=bert_config.initializer_range)

  if not hub_module_url:
    bert_encoder = bert_models.get_transformer_encoder(bert_config,
                                                       max_seq_length)
    return models.BertClassifier(
        bert_encoder,
        num_classes=num_labels,
        dropout_rate=bert_config.hidden_dropout_prob,
        initializer=initializer), bert_encoder

  input_word_ids = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_word_ids')
  input_mask = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_mask')
  input_type_ids = tf.keras.layers.Input(
      shape=(max_seq_length,), dtype=tf.int32, name='input_type_ids')
  bert_model = hub.KerasLayer(
      hub_module_url, trainable=hub_module_trainable)
  pooled_output, _ = bert_model([input_word_ids, input_mask, input_type_ids])
  output = tf.keras.layers.Dropout(rate=bert_config.hidden_dropout_prob)(
      pooled_output)

  output = tf.keras.layers.Dense(
      num_labels, kernel_initializer=initializer, name='output')(
          output)
  return tf.keras.Model(
      inputs={
          'input_word_ids': input_word_ids,
          'input_mask': input_mask,
          'input_type_ids': input_type_ids
      },
      outputs=output), bert_model
