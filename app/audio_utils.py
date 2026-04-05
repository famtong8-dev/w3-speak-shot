"""Shared audio utilities used by both TTSWorker and test_dataset."""

import logging

import numpy as np
from scipy import signal

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000


def convert_audio_to_array(audio):
    """Convert various audio formats to numpy float32 array."""
    try:
        if isinstance(audio, np.ndarray):
            arr = audio
        elif hasattr(audio, 'cpu') and hasattr(audio, 'numpy'):  # PyTorch tensor
            arr = audio.cpu().detach().numpy()
        elif hasattr(audio, 'get_array_of_samples'):  # pygame.mixer.Sound
            arr = audio.get_array_of_samples()
        else:
            arr = np.array(audio, dtype=np.float32)

        if arr.ndim > 1:
            arr = arr.flatten()

        return arr.astype(np.float32)
    except Exception as err:
        logger.error(f"convert_audio_to_array error: {err}")
        return None


def speed_up_audio(audio_data, speed_factor=1.2, sample_rate=SAMPLE_RATE):
    """Speed up audio by resampling with fade-out to prevent clicking."""
    if speed_factor <= 1.0 or len(audio_data) == 0:
        return audio_data
    try:
        new_length = int(len(audio_data) / speed_factor)
        if new_length < 100:
            return audio_data

        resampled = signal.resample(audio_data, new_length)
        resampled = resampled.astype(np.float32)
        resampled = np.clip(resampled, -1.0, 1.0)

        # Add fade-out in last 50ms to prevent clicking
        fade_samples = min(int(sample_rate * 0.05), len(resampled) // 4)
        if len(resampled) > fade_samples and fade_samples > 0:
            fade_start = len(resampled) - fade_samples
            resampled[fade_start:] *= np.linspace(1.0, 0.0, fade_samples)

        return resampled
    except Exception as err:
        logger.error(f"speed_up_audio error: {err}")
        return audio_data


def infer_audio(engine, text, sample_rate=SAMPLE_RATE):
    """Run TTS inference and return normalised float32 audio array, or None on failure.

    This is a pure helper with no locking — callers must serialise access if needed.
    """
    if engine is None:
        return None
    if "œ" in text:
        logger.warning(f"[SKIP   ] contains 'œ': {text[:60]}")
        return None
    try:
        audio = engine.infer(text=text)
        if audio is None:
            return None

        audio_data = convert_audio_to_array(audio)
        if audio_data is None or len(audio_data) == 0:
            return None

        audio_data = audio_data.astype(np.float32)
        max_val = np.max(np.abs(audio_data))
        if max_val > 0:
            audio_data = audio_data / (max_val + 1e-8)

        # Reject suspiciously long audio (model stuck in loop)
        max_duration = max(5.0, len(text) * 0.15)
        actual_duration = len(audio_data) / sample_rate
        if actual_duration > max_duration:
            logger.warning(
                f"[SKIP   ] {actual_duration:.1f}s > {max_duration:.1f}s limit"
                f" — likely stuck: {text[:60]}"
            )
            return None

        return audio_data
    except Exception as err:
        logger.error(f"infer_audio error: {err}")
        return None
