import logging
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

from pydash import strings as pydash_strings

try:
    import fasttext
except Exception:
    fasttext = None

try:
    from .tts_segmenter import TTSSegmenter
except ImportError:
    from tts_segmenter import TTSSegmenter

logger = logging.getLogger(__name__)


class TTSWorker(threading.Thread):
    """
    Background thread worker for text-to-speech playback.

    Manages a queue of text to speak and orchestrates playback via macOS 'say' command.
    Supports bilingual English/Vietnamese playback with language detection and voice switching.
    """

    def __init__(self, base_rate_wpm=260, rate_multiplier=1.0):
        super().__init__(daemon=True)
        self.text_queue = queue.Queue()
        self.stop_token = object()
        self.base_rate_wpm = max(120, int(base_rate_wpm))
        self.rate_multiplier = max(0.1, float(rate_multiplier))
        self.config_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.current_proc = None
        self.stop_requested = threading.Event()
        self.use_say_tts = (sys.platform == "darwin")
        self.say_voice = ""
        self.say_english_voice = ""
        self.available_say_voices = {}
        self.fasttext_model = None
        self.say_multi_voice_mode = os.getenv("W3_TTS_MULTI_VOICE_MODE", "segment").strip().lower()
        self.spell_english_acronyms = os.getenv("W3_TTS_SPELL_ACRONYMS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.detect_english_words = os.getenv("W3_TTS_DETECT_EN_WORDS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.debug_lang_detection = os.getenv("W3_TTS_DEBUG_LANG", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.fasttext_en_threshold = self._read_env_float("W3_TTS_FASTTEXT_EN_THRESHOLD", 0.80)
        self.fasttext_margin_threshold = self._read_env_float("W3_TTS_FASTTEXT_MARGIN_THRESHOLD", 0.12)
        self.fasttext_min_phrase_words = self._read_env_int("W3_TTS_FASTTEXT_MIN_PHRASE_WORDS", 2)
        self.fasttext_max_phrase_words = self._read_env_int("W3_TTS_FASTTEXT_MAX_PHRASE_WORDS", 4)
        if self.fasttext_min_phrase_words < 2:
            self.fasttext_min_phrase_words = 2
        if self.fasttext_max_phrase_words < self.fasttext_min_phrase_words:
            self.fasttext_max_phrase_words = self.fasttext_min_phrase_words
        self.segmenter = TTSSegmenter(
            spell_english_acronyms=self.spell_english_acronyms,
            detect_english_words=self.detect_english_words,
            debug_lang_detection=self.debug_lang_detection,
            fasttext_en_threshold=self.fasttext_en_threshold,
            fasttext_margin_threshold=self.fasttext_margin_threshold,
            fasttext_min_phrase_words=self.fasttext_min_phrase_words,
            fasttext_max_phrase_words=self.fasttext_max_phrase_words,
        )

        if self.use_say_tts:
            self.available_say_voices = self._load_available_say_voices()
            self.fasttext_model = self._load_fasttext_model()
            self.segmenter.set_fasttext_model(self.fasttext_model)
            requested_vi_voice = os.getenv("W3_TTS_SAY_VOICE", "Linh").strip()
            requested_en_voice = os.getenv("W3_TTS_SAY_EN_VOICE", "").strip()
            self.say_voice = self._resolve_say_voice(
                requested=requested_vi_voice,
                preferred_locales={"vi_VN"},
                fallback_names=["Linh"],
            )
            self.say_english_voice = self._resolve_say_voice(
                requested=requested_en_voice,
                preferred_locales={"en_US", "en_GB"},
                fallback_names=[
                    "Samantha",
                    "Daniel",
                    "Fred",
                    "Albert",
                    "Eddy (English (US))",
                    "Eddy (English (UK))",
                    "Flo (English (US))",
                    "Flo (English (UK))",
                ],
            )
            if not self.say_voice:
                self.say_voice = ""
            if not self.say_english_voice:
                self.say_english_voice = self.say_voice
            logger.info("TTS backend: macOS say")
            logger.info(f"TTS voices: vi={self.say_voice or 'default'} en={self.say_english_voice or 'default'}")
            if self.detect_english_words and self.fasttext_model is None:
                logger.warning("TTS language detector: fastText unavailable (falling back to acronym/camel-case only)")
        else:
            logger.warning("TTS disabled: macOS 'say' is unavailable on this platform")

    @staticmethod
    def _read_env_float(key, default):
        raw = os.getenv(key, "").strip()
        if not raw:
            return float(default)
        try:
            return float(raw)
        except Exception:
            return float(default)

    @staticmethod
    def _read_env_int(key, default):
        raw = os.getenv(key, "").strip()
        if not raw:
            return int(default)
        try:
            return int(raw)
        except Exception:
            return int(default)

    def _load_available_say_voices(self):
        voices = {}
        try:
            output = subprocess.check_output(["say", "-v", "?"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return voices

        for line in output.splitlines():
            # Example: "Daniel              en_GB    # Hello! My name is Daniel."
            match = re.match(r"^(.*?)\s{2,}([a-z]{2}_[A-Z]{2})\s+#", line.strip())
            if not match:
                continue
            name = match.group(1).strip()
            locale = match.group(2).strip()
            if name:
                voices[name] = locale
        return voices

    def _resolve_say_voice(self, requested, preferred_locales, fallback_names):
        voices = self.available_say_voices
        if not voices:
            return requested or ""

        if requested:
            if requested in voices:
                return requested
            logger.warning(f"Voice `{requested}` not found, selecting fallback.")

        for name in fallback_names:
            if name in voices:
                return name

        for name, locale in voices.items():
            if locale in preferred_locales:
                return name

        return ""

    def _load_fasttext_model(self):
        if fasttext is None:
            return None

        configured_model = os.getenv("W3_TTS_FASTTEXT_MODEL", "").strip()
        candidates = []
        if configured_model:
            candidates.append(Path(configured_model).expanduser())

        base_dir = Path(__file__).resolve().parent
        candidates.extend(
            [
                base_dir / "models" / "lid.176.ftz",
                base_dir / "models" / "lid.176.bin",
                base_dir.parent / "models" / "lid.176.ftz",
                base_dir.parent / "models" / "lid.176.bin",
            ]
        )

        checked = set()
        for model_path in candidates:
            model_path = model_path.resolve()
            if str(model_path) in checked:
                continue
            checked.add(str(model_path))
            if not model_path.exists():
                continue
            try:
                model = fasttext.load_model(str(model_path))
                logger.info(f"TTS language detector: fastText ({model_path})")
                return model
            except Exception as err:
                logger.warning(f"TTS language detector: failed to load {model_path}: {err}")
        return None

    def _play_segment_with_say(self, text, voice):
        words = max(1, len(text.split()))
        expected_sec = (words / max(self.get_rate_wpm(), 1)) * 60.0
        timeout_sec = max(5.0, min(45.0, expected_sec * 2.5 + 2.0))
        cmd = ["say", "-r", str(self.get_rate_wpm())]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        with self.runtime_lock:
            self.current_proc = subprocess.Popen(cmd)
        self.current_proc.wait(timeout=timeout_sec)

    def _build_multivoice_markup(self, text):
        return self.segmenter.build_multivoice_markup(
            text=text,
            say_voice=self.say_voice,
            say_english_voice=self.say_english_voice,
        )

    def _segment_text_for_say(self, text):
        return self.segmenter.segment_text_for_say(
            text=text,
            say_voice=self.say_voice,
            say_english_voice=self.say_english_voice,
        )

    def _play_text_with_say(self, text):
        try:
            if self.stop_requested.is_set():
                return
            if self.say_multi_voice_mode == "markup":
                speech_text = self._build_multivoice_markup(text)
                self._play_segment_with_say(speech_text, self.say_voice)
            else:
                for segment_text, voice in self._segment_text_for_say(text):
                    if self.stop_requested.is_set():
                        return
                    self._play_segment_with_say(segment_text, voice)
        except subprocess.TimeoutExpired:
            logger.error("TTS error (say playback): timeout")
            with self.runtime_lock:
                if self.current_proc and self.current_proc.poll() is None:
                    self.current_proc.kill()
                self.current_proc = None
        except Exception as err:
            logger.error(f"TTS error (say playback): {err}")
            with self.runtime_lock:
                self.current_proc = None
        finally:
            with self.runtime_lock:
                self.current_proc = None

    def run(self):
        while True:
            try:
                item = self.text_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is self.stop_token:
                break
            self.stop_requested.clear()
            self._play_text_with_say(item)

    def speak(self, text):
        """Queue text for asynchronous TTS playback."""
        prepared = self.prepare_text(text)
        if not prepared:
            return
        self.text_queue.put(prepared)

    def prepare_text(self, text):
        """Normalize and prepare text for TTS synthesis."""
        normalized = self._normalize_tts_text(text)
        if not normalized:
            return ""
        normalized = pydash_strings.trim_end(normalized, " ,:;.!?")
        # Avoid synthesizing tiny fragments too frequently; this hurts latency.
        if not normalized or len(normalized) < 2:
            return ""
        return normalized

    def _normalize_tts_text(self, text):
        normalized = pydash_strings.trim(text)
        if not normalized:
            return ""
        # pydash words handles snake_case and camelCase/PascalCase split
        # without forcing title-case on full Vietnamese sentences.
        tokens = pydash_strings.words(normalized)
        if not tokens:
            return ""
        return pydash_strings.trim(" ".join(tokens))

    def set_rate_multiplier(self, multiplier):
        """Set the speech rate multiplier (1.0 = normal, 2.0 = double speed)."""
        with self.config_lock:
            self.rate_multiplier = max(0.1, float(multiplier))

    def get_rate_multiplier(self):
        with self.config_lock:
            return self.rate_multiplier

    def get_rate_wpm(self):
        with self.config_lock:
            return max(120, int(self.base_rate_wpm * self.rate_multiplier))

    def stop_speaking(self):
        """Stop current playback and clear pending queue."""
        self.stop_requested.set()
        while True:
            try:
                item = self.text_queue.get_nowait()
                if item is self.stop_token:
                    self.text_queue.put(self.stop_token)
                    break
            except queue.Empty:
                break

        with self.runtime_lock:
            if self.current_proc and self.current_proc.poll() is None:
                try:
                    self.current_proc.terminate()
                    self.current_proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.current_proc.kill()
                except Exception as err:
                    logger.error(f"TTS stop error (playback): {err}")
            self.current_proc = None

    def stop(self):
        """Stop the TTS worker thread gracefully."""
        self.text_queue.put(self.stop_token)
