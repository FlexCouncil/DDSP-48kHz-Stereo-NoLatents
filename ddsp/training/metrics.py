# Copyright 2020 The DDSP Authors.
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

# This file has been modified from the original

# Lint as: python3
"""Library of performance metrics relevant to DDSP training."""

from absl import logging
import spectral_ops
import core
import librosa
import mir_eval
import numpy as np
import tensorflow.compat.v2 as tf

# Global values for evaluation.
MIN_F0_CONFIDENCE = 0.85
OUTLIER_MIDI_THRESH = 12



# ---------------------- Helper Functions --------------------------------------
def squeeze(input_vector):
  """Ensure vector only has one axis of dimensionality."""
  if input_vector.ndim > 1:
    return np.squeeze(input_vector)
  else:
    return input_vector


def l1_distance(prediction, ground_truth):
  """Average L1 distance difference between two 1-D vectors."""
  prediction, ground_truth = np.squeeze(prediction), np.squeeze(ground_truth)
  min_length = min(prediction.size, ground_truth.size)
  diff = prediction[:min_length] - ground_truth[:min_length]
  return np.abs(diff)


def is_outlier(ground_truth_f0_conf):
  """Determine if ground truth f0 for audio sample is an outlier."""
  ground_truth_f0_conf = squeeze(ground_truth_f0_conf)
  return np.max(ground_truth_f0_conf) < MIN_F0_CONFIDENCE


def compute_audio_features(audio,
                           n_fft=6144,
                           sample_rate=48000,
                           frame_rate=250):
  """Compute features from audio."""
  # adapted for stereo
  audioM = np.squeeze(np.mean(audio, axis=1))
  audioL = np.squeeze(audio[:,0:1])
  audioR = np.squeeze(audio[:,1:2])
  audioM = np.expand_dims(audioM, axis=0)
  audioL = np.expand_dims(audioL, axis=0)
  audioR = np.expand_dims(audioR, axis=0)
  audio_feats = {'audioM': audioM, 'audioL': audioL, 'audioR': audioR}
  audioM32 = audioM.astype(np.float32)
  audioL32 = audioL.astype(np.float32)
  audioR32 = audioR.astype(np.float32)
      
  audio_feats['loudness_dbM'] = spectral_ops.compute_loudness(
      audioM32, sample_rate, frame_rate, n_fft)
      
  audio_feats['loudness_dbL'] = spectral_ops.compute_loudness(
      audioL32, sample_rate, frame_rate, n_fft)
      
  audio_feats['loudness_dbR'] = spectral_ops.compute_loudness(
      audioR32, sample_rate, frame_rate, n_fft)

  audio_feats['f0_hzM'], audio_feats['f0_confidenceM'] = (
      spectral_ops.compute_f0(np.squeeze(audioM), sample_rate, frame_rate))
      
  audio_feats['f0_hzL'], audio_feats['f0_confidenceL'] = (
      spectral_ops.compute_f0(np.squeeze(audioL), sample_rate, frame_rate))
      
  audio_feats['f0_hzR'], audio_feats['f0_confidenceR'] = (
      spectral_ops.compute_f0(np.squeeze(audioR), sample_rate, frame_rate))

  return audio_feats


def f0_dist_conf_thresh(f0_hz,
                        f0_hz_gen,
                        f0_confidence,
                        f0_confidence_thresh=MIN_F0_CONFIDENCE):
  """Compute L1 between gen audio and ground truth audio.

  Calculating F0 distance is more complicated than calculating loudness
  distance because of inherent inaccuracies in pitch tracking.

  We take the following steps:
  - Define a `keep_mask` that only select f0 values above when f0_confidence in
  the original audio exceeds a minimum threshold.
  Experimentation by jessengel@ and hanoih@ found this to be optimal way to
  filter out bad f0 pitch tracking.
  - Compute `delta_f0` between generated audio and ground truth audio.
  - Only select values in `delta_f0` based on this `keep_mask`
  - Compute mean on this selection
  - At the start of training, audio samples will sound bad and thus have no
  pitch content. If the `f0_confidence` is all below the threshold, we keep a
  count of it. A better performing model will have a smaller count of
  "untrackable pitch" samples.

  Args:
    f0_hz: Ground truth audio f0 in hertz [MB,:].
    f0_hz_gen: Generated audio f0 in hertz [MB,:].
    f0_confidence: Ground truth audio f0 confidence [MB,:]
    f0_confidence_thresh: Confidence threshold above which f0 metrics will be
      computed

  Returns:
    delta_f0_mean: Float or None if entire generated sample had
      f0_confidence below threshold. In units of MIDI (logarithmic frequency).
  """
  if len(f0_hz.shape) > 2:
    f0_hz = f0_hz[:, :, 0]
  if len(f0_hz_gen.shape) > 2:
    f0_hz_gen = f0_hz_gen[:, :, 0]
  if len(f0_confidence.shape) > 2:
    f0_confidence = f0_confidence[:, :, 0]

  if np.max(f0_confidence) < f0_confidence_thresh:
    # Generated audio is not good enough for reliable pitch tracking.
    return None
  else:
    keep_mask = f0_confidence >= f0_confidence_thresh

    # Report mean error in midi space for easier interpretation.
    f0_midi = librosa.core.hz_to_midi(f0_hz)
    f0_midi_gen = librosa.core.hz_to_midi(f0_hz_gen)
    # Set -infs introduced by hz_to_midi to 0.
    f0_midi[f0_midi == -np.inf] = 0
    f0_midi_gen[f0_midi_gen == -np.inf] = 0

    delta_f0_midi = l1_distance(f0_midi, f0_midi_gen)
    delta_f0_midi_filt = delta_f0_midi[keep_mask]
    return np.mean(delta_f0_midi_filt)


# ---------------------- Metrics -----------------------------------------------
class BaseMetrics(object):
  """Base object for computing metrics on generated audio samples."""

  def __init__(self, sample_rate, frame_rate, name):
    """Constructor.

    Args:
      sample_rate: Audio sample rate.
      frame_rate: Feature frame rate.
      name: Name to be printed when logging and scope for summaries.
    """
    self._sample_rate = sample_rate
    self._frame_rate = frame_rate
    self._name = name

  @property
  def metrics(self):
    """Initialize metrics dictionary with keys and keras metric objects."""
    raise NotImplementedError()

  def update_state(self):
    """Update running state of metric."""
    raise NotImplementedError()

  def flush(self, step):
    """Add summaries for each metric and reset the state."""
    # Log metrics
    logging.info('Computing %s metrics complete. Flushing all metrics',
                 self._name)
    metrics_str = ' | '.join(
        '{}: {:0.3f}'.format(k, v.result()) for k, v in self.metrics.items())
    logging.info(metrics_str)

    # Write tf summaries.
    for metric_name, metric in self.metrics.items():
      tf.summary.scalar('metrics/{}/{}'.format(self._name, metric_name),
                        metric.result(), step)
      metric.reset_states()


class LoudnessMetrics(BaseMetrics):
  """Helper object for computing loudness metrics."""

  def __init__(self, sample_rate, frame_rate, name='loudness'):
    super().__init__(sample_rate=sample_rate, frame_rate=frame_rate, name=name)
    self._metrics = {
        'loudness_db': tf.keras.metrics.Mean('loudness_db'),
    }

  @property
  def metrics(self):
    return self._metrics

  def update_state(self, batch, audio_gen):
    """Update metrics based on a batch of audio.

    Args:
      batch: Dictionary of input features.
      audio_gen: Batch of generated audio.
    """
    loudness_original = batch['loudness_db']
    # Compute loudness across entire batch
    loudness_gen = spectral_ops.compute_loudness(
        audio_gen, sample_rate=self._sample_rate, frame_rate=self._frame_rate)

    batch_size = int(audio_gen.shape[0])
    for i in range(batch_size):
      ld_dist = np.mean(l1_distance(loudness_original[i], loudness_gen[i]))
      self.metrics['loudness_db'].update_state(ld_dist)
      log_str = f'{self._name} | sample {i} | ld_dist(db): {ld_dist:.3f}'
      logging.info(log_str)


class F0Metrics(BaseMetrics):
  """Helper object for computing f0 encoder metrics."""

  def __init__(
      self, sample_rate, frame_rate, rpa_tolerance=50, name='f0'):
    super().__init__(sample_rate=sample_rate, frame_rate=frame_rate, name=name)
    self._metrics = {
        'f0_dist': tf.keras.metrics.Mean('f0_dist'),
        'raw_pitch_accuracy': tf.keras.metrics.Mean('raw_chroma_accuracy'),
        'raw_chroma_accuracy': tf.keras.metrics.Mean('raw_pitch_accuracy')
    }
    self._rpa_tolerance = rpa_tolerance

  @property
  def metrics(self):
    return self._metrics

  def update_state(self, batch, f0_hz_predict):
    """Update metrics based on a batch of audio.

    Args:
      batch: Dictionary of input features.
      f0_hz_predict: Batch of encoded f0, same as input f0 if no f0 encoder.
    """
    batch_size = int(f0_hz_predict.shape[0])
    # Match number of timesteps.
    if f0_hz_predict.shape[1] != batch['f0_hz'].shape[1]:
      # f0_hz_predict = core.resample(f0_hz_predict,
      #                                    batch['f0_hz'].shape[1]).numpy()
      batch['f0_hz'] = core.resample(batch['f0_hz'],
                                          f0_hz_predict.shape[1]).numpy()
      batch['f0_confidence'] = core.resample(
          batch['f0_confidence'], f0_hz_predict.shape[1]).numpy()

    # Compute metrics per sample. No batch operations possible.
    for i in range(batch_size):
      f0_hz_gt = batch['f0_hz'][i]
      f0_conf_gt = batch['f0_confidence'][i]

      if not is_outlier(f0_conf_gt):
        # Gound truth f0 was reliable, proceed with metrics
        # Compute distance between original f0_hz labels and f0 encoder values.
        # Resample if f0 encoder has different number of time steps.
        # TODO(hanoih): compare f0_hz against frame_rate * len_sec
        f0_hz = f0_hz_predict[i]
        f0_dist = f0_dist_conf_thresh(f0_hz_gt, f0_hz, f0_conf_gt)
        self.metrics['f0_dist'].update_state(f0_dist)

        f0_hz_gt = np.squeeze(f0_hz_gt)
        f0_hz = np.squeeze(f0_hz)
        voiced_gt = mir_eval.melody.freq_to_voicing(f0_hz_gt)[1]
        cents_gt = mir_eval.melody.hz2cents(f0_hz_gt)
        cents_est = mir_eval.melody.hz2cents(f0_hz)
        rca = mir_eval.melody.raw_chroma_accuracy(
            voiced_gt,
            cents_gt,
            voiced_gt,
            cents_est,
            cent_tolerance=self._rpa_tolerance)
        rpa = mir_eval.melody.raw_pitch_accuracy(
            voiced_gt,
            cents_gt,
            voiced_gt,
            cents_est,
            cent_tolerance=self._rpa_tolerance)
        self.metrics['raw_chroma_accuracy'].update_state(rca)
        self.metrics['raw_pitch_accuracy'].update_state(rpa)
        log_str = (f'{self._name} | sample {i} | f0_dist(midi): {f0_dist:.3f} '
                   f'raw_chroma_accuracy: {rca:.3f} '
                   f'raw_pitch_accuracy: {rpa:.3f}')
        logging.info(log_str)

