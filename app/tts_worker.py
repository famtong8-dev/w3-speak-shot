import logging
import os
import queue
import threading
import time
from collections import OrderedDict

import numpy as np
from vieneu import Vieneu

try:
    from .audio_utils import convert_audio_to_array, infer_audio, speed_up_audio
    from . import audio_player
except ImportError:
    from audio_utils import convert_audio_to_array, infer_audio, speed_up_audio
    import audio_player

logger = logging.getLogger(__name__)


class TTSWorker(threading.Thread):
    def __init__(self, base_rate_wpm=260, debug_output_file=None):
        super().__init__(daemon=True)
        self.text_queue = queue.Queue()
        self.audio_cache = OrderedDict()  # Cache: text -> audio_data (with LRU eviction)
        self.audio_cache_max_size = 50
        self.cache_lock = threading.Lock()
        self.stop_token = object()
        self.base_rate_wpm = max(120, int(base_rate_wpm))
        self.config_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.infer_lock = threading.Lock()
        self.current_proc = None
        self.stop_requested = threading.Event()
        # rate_multiplier is the actual playback speed (e.g. 1.5 = 1.5x).
        # Seeded from W3_TTS_SPEED_FACTOR so the menu reflects the real default.
        self.rate_multiplier = max(0.1, self._read_env_float("W3_TTS_SPEED_FACTOR", 1.5))
        self.base_rate_multiplier = self.rate_multiplier
        self.auto_speed = False
        self.pre_render = True  # Pre-render audio in background to reduce gap between sentences

        # Debug mode: save audio to file instead of playing
        self.debug_output_file = debug_output_file
        self.sample_rate = 24000
        self.debug_audio_buffer = []  # Buffer for appending audio
        self.debug_buffer_lock = threading.Lock()

        if self.debug_output_file:
            logger.info(f"[DEBUG MODE] Audio will be buffered to: {self.debug_output_file}")

        # Periodic cleanup of old temp files (every 100 spoken items)
        self.speak_count = 0
        self.cleanup_interval = 100
        self.playback_lock = threading.Lock()

        try:
            self.tts_engine = Vieneu()
            logger.info("TTS backend: VieNeu-TTS")
            # Language detection falls back to rules-only (acronyms, camelCase).
        except Exception as err:
            logger.error(f"TTS initialization error: {err}")
            self.tts_engine = None

    @staticmethod
    def _read_env_float(key, default):
        raw = os.getenv(key, "").strip()
        if not raw:
            return float(default)
        try:
            return float(raw)
        except Exception:
            logger.warning(f"Invalid value for {key}={raw!r}, using default {default}")
            return float(default)

    def _get_cached_audio(self, text):
        """Get audio from cache or generate if needed."""
        with self.cache_lock:
            if text in self.audio_cache:
                # Move to end (most recently used) for LRU tracking
                self.audio_cache.move_to_end(text)
                return self.audio_cache[text]

        # Not in cache, generate
        audio_data = self._infer_and_convert(text)
        if audio_data is not None:
            with self.cache_lock:
                # Evict least recently used if cache is full
                while len(self.audio_cache) >= self.audio_cache_max_size:
                    self.audio_cache.popitem(last=False)
                self.audio_cache[text] = audio_data

        return audio_data

    def _speed_up_audio(self, audio_data, speed_factor=1.2):
        return speed_up_audio(audio_data, speed_factor=speed_factor, sample_rate=self.sample_rate)

    def _infer_and_convert(self, text):
        """Infer audio and convert to numpy array (serialised via infer_lock)."""
        if not self.tts_engine:
            return None
        # Acquire lock before entering shared infer_audio helper so inference
        # calls are serialised even when multiple threads call this method.
        with self.infer_lock:
            return infer_audio(self.tts_engine, text, sample_rate=self.sample_rate)

    def _play_segment_with_vieneu(self, text, audio_data):
        if not self.tts_engine:
            return
        try:
            if audio_data is None:
                raw = self._get_cached_audio(text)
                if raw is None or len(raw) < 100:
                    return
                audio_data = self._speed_up_audio(raw, speed_factor=self.get_rate_multiplier())
            if len(audio_data) < 100:
                return

            # Validate audio - must be finite and properly shaped
            if not np.all(np.isfinite(audio_data)):
                return

            with self.runtime_lock:
                self.current_proc = True

            try:
                # Debug mode: buffer audio to file
                if self.debug_output_file:
                    with self.debug_buffer_lock:
                        self.debug_audio_buffer.append(audio_data)
                        total_samples = sum(len(a) for a in self.debug_audio_buffer)
                    logger.info(f"[DEBUG  ] Audio buffered ({len(audio_data)} samples, total: {total_samples} samples)")
                else:
                    # Normal mode: play audio via temp file (BLOCKING)
                    logger.debug("Playing audio via temp file (blocking)")
                    self._play_audio_file_based(audio_data)
                    logger.debug("Audio playback complete")

                    # Periodic cleanup of old temp files (async to avoid blocking)
                    self.speak_count += 1
                    if self.speak_count % self.cleanup_interval == 0:
                        cleanup_thread = threading.Thread(target=self._cleanup_old_temp_files, daemon=True)
                        cleanup_thread.start()
            finally:
                with self.runtime_lock:
                    self.current_proc = None
        except Exception as err:
            logger.error(f"TTS error (playback): {err}")
            with self.runtime_lock:
                self.current_proc = None

    def _play_audio_file_based(self, audio_data):
        with self.playback_lock:
            audio_player.play(audio_data, self.sample_rate)

    def _cleanup_old_temp_files(self):
        audio_player.cleanup_old_temp_files()

    def _convert_audio_to_array(self, audio):
        return convert_audio_to_array(audio)

    def _play_text_with_vieneu(self, text, audio_data):
        try:
            if self.stop_requested.is_set():
                return
            self._play_segment_with_vieneu(text, audio_data)
        except Exception as err:
            logger.error(f"TTS error (VieNeu playback): {err}")
            with self.runtime_lock:
                self.current_proc = None

    def run(self):
        while True:
            try:
                # 1 second timeout allows faster shutdown response
                item = self.text_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is self.stop_token:
                break

            if isinstance(item, tuple):
                text, audio_data = item
            else:
                text, audio_data = item, None

            # Auto-adjust speed based on queue size
            if self.auto_speed:
                queue_size = self.text_queue.qsize()
                if queue_size >= 2:
                    new_rate = self.base_rate_multiplier + 0.25
                    self.set_rate_multiplier(new_rate)
                    logger.info(f"[SPEED  ] auto {new_rate:.2f}x (queue={queue_size})")
                else:
                    self.set_rate_multiplier(self.base_rate_multiplier)

            self.stop_requested.clear()
            self._play_text_with_vieneu(text, audio_data)

    def speak(self, text):
        prepared = self.prepare_text(text)
        if not prepared:
            return

        # Infer + resample in background, queue pre-rendered audio to eliminate
        # resample latency between sentences.
        def infer_and_queue():
            raw = self._get_cached_audio(prepared)
            if raw is None:
                return
            if self.pre_render:
                sped = self._speed_up_audio(raw, speed_factor=self.get_rate_multiplier())
                self.text_queue.put((prepared, sped))
            else:
                self.text_queue.put(prepared)

        threading.Thread(target=infer_and_queue, daemon=True).start()

    # Maximum characters sent to TTS in one call (prevents model hangs on huge blobs)
    MAX_TTS_LENGTH = 500

    def prepare_text(self, text):
        normalized = self._normalize_tts_text(text)
        if not normalized:
            return ""
        if len(normalized) > self.MAX_TTS_LENGTH:
            logger.warning(
                f"[SKIP   ] text too long ({len(normalized)} chars > {self.MAX_TTS_LENGTH}): "
                f"{normalized[:80]}…"
            )
            return ""
        return normalized

    def _normalize_tts_text(self, text):
        """Keep text as-is - only trim whitespace."""
        normalized = text.strip() if text else ""
        return normalized

    def set_rate_multiplier(self, multiplier):
        with self.config_lock:
            self.rate_multiplier = max(0.1, float(multiplier))

    def set_base_rate_multiplier(self, multiplier):
        with self.config_lock:
            self.base_rate_multiplier = max(0.1, float(multiplier))
            self.rate_multiplier = self.base_rate_multiplier

    def get_rate_multiplier(self):
        with self.config_lock:
            return self.rate_multiplier

    def get_rate_wpm(self):
        with self.config_lock:
            return max(120, int(self.base_rate_wpm * self.rate_multiplier))

    def skip_to_latest(self, text):
        """Drain the queue and speak the latest text after current audio finishes."""
        while True:
            try:
                self.text_queue.get_nowait()
            except queue.Empty:
                break
        self.speak(text)

    def stop_speaking(self):
        self.stop_requested.set()
        while True:
            try:
                item = self.text_queue.get_nowait()
                if item is self.stop_token:
                    self.text_queue.put(self.stop_token)
                    break
            except queue.Empty:
                break

        # Audio playback is via subprocess (temp files), no explicit stop needed

        with self.runtime_lock:
            self.current_proc = None

    def save_debug_audio(self):
        """Save buffered audio to WAV file. Call this at the end of session."""
        logger.info(f"save_debug_audio called: output_file={self.debug_output_file}, buffer_size={len(self.debug_audio_buffer)}")

        if not self.debug_output_file:
            logger.info("No debug output file set, skipping")
            return

        if not self.debug_audio_buffer:
            logger.warning(f"Debug audio buffer is empty!")
            return

        try:
            with self.debug_buffer_lock:
                if not self.debug_audio_buffer:
                    logger.warning("Buffer became empty in lock")
                    return
                # Concatenate all audio chunks
                combined_audio = np.concatenate(self.debug_audio_buffer, axis=0)
                self.debug_audio_buffer.clear()

            logger.info(f"Writing {len(combined_audio)} samples to {self.debug_output_file}")
            sf.write(self.debug_output_file, combined_audio, self.sample_rate, subtype='FLOAT')
            logger.info(f"✓ Debug audio saved to {self.debug_output_file} ({len(combined_audio)} samples)")
        except Exception as err:
            logger.error(f"Failed to save debug audio: {err}", exc_info=True)

    def stop(self):
        # Save debug audio before stopping
        if self.debug_output_file:
            self.save_debug_audio()
        self.text_queue.put(self.stop_token)
