# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Code for constructing the model."""
from typing import Any, Mapping, Optional, Union

from absl import logging
from alphafold.common import confidence
from alphafold.model import features
from alphafold.model import modules
from alphafold.model import modules_multimer
from alphafold.common import residue_constants
import haiku as hk
import jax
import ml_collections
import numpy as np
import tensorflow.compat.v1 as tf
import tree


def get_confidence_metrics(
    prediction_result: Mapping[str, Any],
    multimer_mode: bool, 
    num_res: int,
    recompile_padding: float = 1.0) -> Mapping[str, Any]:
  """Post processes prediction_result to get confidence metrics."""
  confidence_metrics = {}
  confidence_metrics['plddt'] = confidence.compute_plddt(
      prediction_result['predicted_lddt']['logits'],
      num_res=num_res,
      recompile_padding=recompile_padding)
  if 'predicted_aligned_error' in prediction_result:
    confidence_metrics.update(confidence.compute_predicted_aligned_error(
        logits=prediction_result['predicted_aligned_error']['logits'],
        breaks=prediction_result['predicted_aligned_error']['breaks']))
    confidence_metrics['ptm'] = confidence.predicted_tm_score(
        logits=prediction_result['predicted_aligned_error']['logits'],
        breaks=prediction_result['predicted_aligned_error']['breaks'],
        num_res=num_res,
        asym_id=None,
        recompile_padding=recompile_padding)
    if multimer_mode:
      # Compute the ipTM only for the multimer model.
      confidence_metrics['iptm'] = confidence.predicted_tm_score(
          logits=prediction_result['predicted_aligned_error']['logits'],
          breaks=prediction_result['predicted_aligned_error']['breaks'],
          num_res=num_res,
          asym_id=prediction_result['predicted_aligned_error']['asym_id'],
          interface=True,
          recompile_padding=recompile_padding)
      confidence_metrics['ranking_confidence'] = (
          0.8 * confidence_metrics['iptm'] + 0.2 * confidence_metrics['ptm'])

  if not multimer_mode:
    # Monomer models use mean pLDDT for model ranking.
    confidence_metrics['ranking_confidence'] = np.mean(
        confidence_metrics['plddt'])

  return confidence_metrics


class RunModel:
  """Container for JAX model."""

  def __init__(self,
               config: ml_collections.ConfigDict,
               params: Optional[Mapping[str, Mapping[str, np.ndarray]]] = None,
               is_training = False):
    
    self.config = config
    self.params = params
    self.multimer_mode = config.model.global_config.multimer_mode


    if self.multimer_mode:
      def _forward_fn(batch):
        model = modules_multimer.AlphaFold(self.config.model)
        return model(batch, is_training=is_training)
    else:
      def _forward_fn(batch):
        model = modules.AlphaFold(self.config.model)
        return model(
            batch,
            is_training=is_training,
            compute_loss=False,
            ensemble_representations=True)

    self.apply = jax.jit(hk.transform(_forward_fn).apply)
    self.init = jax.jit(hk.transform(_forward_fn).init)

  def init_params(self, feat: features.FeatureDict, random_seed: int = 0):
    """Initializes the model parameters.

    If none were provided when this class was instantiated then the parameters
    are randomly initialized.

    Args:
      feat: A dictionary of NumPy feature arrays as output by
        RunModel.process_features.
      random_seed: A random seed to use to initialize the parameters if none
        were set when this class was initialized.
    """
    if not self.params:
      # Init params randomly.
      rng = jax.random.PRNGKey(random_seed)
      self.params = hk.data_structures.to_mutable_dict(
          self.init(rng, feat))
      logging.warning('Initialized parameters randomly')

  def process_features(
      self,
      raw_features: Union[tf.train.Example, features.FeatureDict],
      random_seed: int) -> features.FeatureDict:
    """Processes features to prepare for feeding them into the model.

    Args:
      raw_features: The output of the data pipeline either as a dict of NumPy
        arrays or as a tf.train.Example.
      random_seed: The random seed to use when processing the features.

    Returns:
      A dict of NumPy feature arrays suitable for feeding into the model.
    """

    if self.multimer_mode:
      return raw_features

    # Single-chain mode.
    if isinstance(raw_features, dict):
      return features.np_example_to_features(
          np_example=raw_features,
          config=self.config,
          random_seed=random_seed)
    else:
      return features.tf_example_to_features(
          tf_example=raw_features,
          config=self.config,
          random_seed=random_seed)

  def eval_shape(self, feat: features.FeatureDict) -> jax.ShapeDtypeStruct:
    self.init_params(feat)
    logging.debug('Running eval_shape with shape(feat) = %s',
                 tree.map_structure(lambda x: x.shape, feat))
    shape = jax.eval_shape(self.apply, self.params, jax.random.PRNGKey(0), feat)
    logging.info('Output shape was %s', shape)
    return shape

  def predict(self,
              feat: features.FeatureDict,
              random_seed: int = 0,
              recompile_padding: float = 1.0,
              seq_len: int = 0
              ) -> Mapping[str, Any]:
    """Makes a prediction by inferencing the model on the provided features.

    Args:
      feat: A dictionary of NumPy feature arrays as output by
        RunModel.process_features.
      random_seed: The random seed to use when running the model. In the
        multimer model this controls the MSA sampling.

    Returns:
      A dictionary of model outputs.
    """
    self.init_params(feat)
    logging.info('Running predict with shape(feat) = %s',
                 tree.map_structure(lambda x: x.shape, feat))
    
    aatype = feat["aatype"]
    if self.multimer_mode:
      num_iters = self.config.model.num_recycle + 1
      L = aatype.shape[0]
    else:
      num_iters = self.config.model.num_recycle + 1
      num_ensemble = self.config.data.eval.num_ensemble
      L = aatype.shape[1]
    
    result = {"prev":{'prev_msa_first_row': np.zeros([L,256]),
                      'prev_pair': np.zeros([L,L,128]),
                      'prev_pos': np.zeros([L,37,3])}}
        
    r = 0
    key = jax.random.PRNGKey(random_seed)
    while r < num_iters:
        if self.multimer_mode:
            sub_feat = feat
            sub_feat["iter"] = np.array(r)
            num_res = feat['seq_length']
        else:
            s = r * num_ensemble
            e = (r+1) * num_ensemble
            sub_feat = jax.tree_map(lambda x:x[s:e], feat)
            num_res = feat['seq_length'][0]

        sub_feat["prev"] = result["prev"]
        result, _ = self.apply(self.params, key, sub_feat)
        confidences = get_confidence_metrics(result, multimer_mode=self.multimer_mode, num_res=num_res, recompile_padding=recompile_padding)

        if self.config.model.stop_at_score_ranker == "plddt":
          mean_score = (confidences["plddt"] * feat["seq_mask"][:,:num_res]).sum() / feat["seq_mask"].sum()
        else:
          mean_score = confidences["ptm"].mean()
        
        result.update(confidences)
        r += 1

        if mean_score > self.config.model.stop_at_score:
            break

        if self.config.model.recycle_early_stop_tolerance > 0:
          ca_idx = residue_constants.atom_order['CA']
          if r > 1:
            # Early stopping criteria
            pos = result["prev"]["prev_pos"][:,ca_idx]
            dist = lambda x: np.sqrt(np.square(x[:,None]-x[None,:]).sum(-1))
            sq_diff = np.square(dist(pos) - dist(prev_pos))
            seq_mask = feat["seq_mask"] if self.multimer_mode else feat["seq_mask"][0]
            mask = seq_mask[:,None] * seq_mask[None,:]
            diff = np.sqrt((sq_diff * mask).sum()/mask.sum())
            if diff < self.config.model.recycle_early_stop_tolerance: break
          prev_pos = result["prev"]["prev_pos"][:,ca_idx]

    logging.info('Output shape was %s', tree.map_structure(lambda x: x.shape, result))
    return result, (r-1)
