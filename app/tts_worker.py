import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict

import numpy as np
import soundfile as sf
from scipy import signal
from vieneu import Vieneu

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
        # Track subprocess for cleanup
        self.current_playback_process = None
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
        """Speed up audio by resampling with fade-out."""
        if speed_factor <= 1.0 or len(audio_data) == 0:
            return audio_data
        try:
            # Reduce number of samples = speed up playback
            new_length = int(len(audio_data) / speed_factor)
            if new_length < 100:
                return audio_data

            resampled = signal.resample(audio_data, new_length)
            resampled = resampled.astype(np.float32)

            # Clamp to [-1, 1] range to prevent overflow
            resampled = np.clip(resampled, -1.0, 1.0)

            # Add fade-out in last 50ms to prevent clicking
            fade_samples = min(int(24000 * 0.05), len(resampled) // 4)  # 50ms or 25% of audio
            if len(resampled) > fade_samples and fade_samples > 0:
                fade_start = len(resampled) - fade_samples
                fade_out = np.linspace(1.0, 0.0, fade_samples)
                resampled[fade_start:] *= fade_out

            return resampled

        except Exception as err:
            return audio_data

    def _infer_and_convert(self, text):
        """Infer audio and convert to numpy array."""
        if not self.tts_engine:
            return None
        try:
            # Serialize infer calls to avoid race conditions
            with self.infer_lock:
                audio = self.tts_engine.infer(text=text)
            if audio is None:
                return None

            audio_data = self._convert_audio_to_array(audio)
            if audio_data is None or len(audio_data) == 0:
                return None

            # Ensure audio is float32 and normalized
            audio_data = audio_data.astype(np.float32)
            max_val = np.max(np.abs(audio_data))
            if max_val > 0:
                audio_data = audio_data / (max_val + 1e-8)

            # Reject audio that is suspiciously long (model stuck in loop)
            max_duration = max(5.0, len(text) * 0.15)
            actual_duration = len(audio_data) / self.sample_rate
            if actual_duration > max_duration:
                logger.warning(f"[SKIP   ] {actual_duration:.1f}s > {max_duration:.1f}s limit — likely stuck: {text[:60]}")
                return None

            return audio_data
        except Exception as err:
            print(f"TTS error (inference): {err}")
            return None

    def _play_segment_with_vieneu(self, text, voice):
        if not self.tts_engine:
            return
        try:
            # Use cached audio or generate
            audio_data = self._get_cached_audio(text)
            if audio_data is None or len(audio_data) < 100:
                return

            # Apply speed dynamically so Set Speed takes effect without re-inference
            audio_data = self._speed_up_audio(audio_data, speed_factor=self.get_rate_multiplier())

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
        """Play audio via temp file + subprocess (avoids sounddevice issues)."""
        # LOCK entire playback operation to prevent parallel playback
        with self.playback_lock:
            try:
                # Write to temp file
                keep_temp = os.getenv("W3_KEEP_TEMP_AUDIO", "").lower() == "true"
                temp_dir = os.getenv("W3_TEMP_AUDIO_DIR", tempfile.gettempdir())

                with tempfile.NamedTemporaryFile(suffix='.wav', dir=temp_dir, delete=False) as tmp:
                    temp_path = tmp.name

                sf.write(temp_path, audio_data, self.sample_rate, subtype='FLOAT')

                # Log file info
                file_size_kb = os.path.getsize(temp_path) / 1024
                duration_sec = len(audio_data) / self.sample_rate
                logger.info(f"[AUDIO  ] {duration_sec:.1f}s | {file_size_kb:.1f}KB")

                # Detect OS and use appropriate player
                import platform
                system = platform.system()

                if system == 'Darwin':  # macOS
                    cmd = ['afplay', temp_path]
                elif system == 'Linux':
                    # Try multiple players: paplay, aplay, ffplay
                    cmd = ['paplay', '--latency=100ms', temp_path]  # PulseAudio with low latency
                else:
                    logger.warning(f"Unsupported OS for file-based playback: {system}")
                    return

                # Play and WAIT (BLOCKING)
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # BLOCK until current process finishes
                    logger.debug("Playing audio (blocking)...")
                    proc.wait()  # Block until audio finishes
                    logger.debug(f"Audio playback finished (return code: {proc.returncode})")

                    # Clean up temp file
                    if not keep_temp:
                        try:
                            os.unlink(temp_path)
                        except Exception:
                            pass

                except FileNotFoundError:
                    logger.error(f"Audio player not found. Install: afplay (macOS), paplay/aplay (Linux)")
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass

            except Exception as err:
                logger.error(f"Audio file playback error: {err}")

    def _cleanup_old_temp_files(self):
        """Remove old temp audio files to prevent disk space buildup."""
        try:
            temp_dir = os.getenv("W3_TEMP_AUDIO_DIR", tempfile.gettempdir())
            max_age_hours = 2  # Delete files older than 2 hours

            now = time.time()
            for fname in os.listdir(temp_dir):
                if not fname.startswith('tmp') or not fname.endswith('.wav'):
                    continue

                fpath = os.path.join(temp_dir, fname)
                try:
                    age_hours = (now - os.path.getmtime(fpath)) / 3600
                    if age_hours > max_age_hours:
                        os.unlink(fpath)
                        logger.debug(f"Cleaned up old temp file: {fname}")
                except Exception:
                    pass

        except Exception as err:
            logger.debug(f"Cleanup error: {err}")

    def _convert_audio_to_array(self, audio):
        """Convert various audio formats to numpy array."""
        try:
            # Already a numpy array
            if isinstance(audio, np.ndarray):
                arr = audio
            # Check for torch tensor
            elif hasattr(audio, 'cpu') and hasattr(audio, 'numpy'):  # PyTorch tensor
                arr = audio.cpu().detach().numpy()
            # Check for audio object with specific methods
            elif hasattr(audio, 'get_array_of_samples'):  # pygame.mixer.Sound
                arr = audio.get_array_of_samples()
            else:
                # Try to convert directly
                arr = np.array(audio, dtype=np.float32)

            # Ensure it's 1D float array
            if arr.ndim > 1:
                arr = arr.flatten()

            return arr.astype(np.float32)
        except Exception as err:
            return None

    def _play_text_with_vieneu(self, text):
        try:
            if self.stop_requested.is_set():
                return
            # Play entire text at once (VieNeu handles EN/VI internally)
            self._play_segment_with_vieneu(text, None)
        except Exception as err:
            print(f"TTS error (VieNeu playback): {err}")
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
            self._play_text_with_vieneu(item)

    def speak(self, text):
        prepared = self.prepare_text(text)
        if not prepared:
            return

        # Infer + queue in background (infer THEN queue)
        def infer_and_queue():
            self._get_cached_audio(prepared)  # Wait for inference
            self.text_queue.put(prepared)      # Queue only after cached

        threading.Thread(target=infer_and_queue, daemon=True).start()

    def prepare_text(self, text):
        normalized = self._normalize_tts_text(text)
        if not normalized:
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
