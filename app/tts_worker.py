import os
import queue
import re
import subprocess
import sys
import threading

from pydash import strings as pydash_strings

try:
    from lingua import Language, LanguageDetectorBuilder
except Exception:
    Language = None
    LanguageDetectorBuilder = None


class TTSWorker(threading.Thread):
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
        self.language_detector = None
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

        if self.use_say_tts:
            self.available_say_voices = self._load_available_say_voices()
            self.language_detector = self._build_language_detector()
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
            print("TTS backend: macOS say")
            print(f"TTS voices: vi={self.say_voice or 'default'} en={self.say_english_voice or 'default'}")
            if self.detect_english_words and self.language_detector is None:
                print("TTS language detector: unavailable (falling back to acronym-only)")
        else:
            print("TTS disabled: macOS 'say' is unavailable on this platform")

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
            print(f"Voice `{requested}` not found, selecting fallback.")

        for name in fallback_names:
            if name in voices:
                return name

        for name, locale in voices.items():
            if locale in preferred_locales:
                return name

        return ""

    def _build_language_detector(self):
        if LanguageDetectorBuilder is None or Language is None:
            return None
        try:
            return LanguageDetectorBuilder.from_languages(
                Language.ENGLISH,
                Language.VIETNAMESE,
            ).build()
        except Exception:
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
        if not self.say_english_voice or self.say_english_voice == self.say_voice:
            return text

        segments = self._segment_text_for_say(text)
        if len(segments) == 1 and segments[0][1] == self.say_voice:
            return text

        parts = []
        for segment_text, voice in segments:
            if voice == self.say_english_voice:
                parts.append(f"[[voice {self.say_english_voice}]] {segment_text} [[voice {self.say_voice}]]")
            else:
                parts.append(segment_text)
        return "".join(parts).strip()

    def _segment_text_for_say(self, text):
        if not self.say_english_voice:
            return [(text, self.say_voice)]
        if self.say_english_voice == self.say_voice:
            return [(text, self.say_voice)]

        pattern = re.compile(r"\b[A-Za-z][A-Za-z']*\b")
        segments = []
        cursor = 0
        for match in pattern.finditer(text):
            start, end = match.span()
            token = match.group(0)
            use_english_voice = self._should_use_english_voice_for_token(token)
            if not use_english_voice:
                continue

            if start > cursor:
                prefix = text[cursor:start]
                if prefix:
                    if prefix.isspace() and segments and segments[-1][1] == self.say_english_voice:
                        prev_text, prev_voice = segments[-1]
                        segments[-1] = (prev_text + prefix, prev_voice)
                    else:
                        segments.append((prefix, self.say_voice))
            token_text = " ".join(token) if token.isupper() and len(token) >= 2 else token
            segments.append((token_text, self.say_english_voice))
            cursor = end

        if cursor < len(text):
            tail = text[cursor:]
            if tail:
                if tail.isspace() and segments and segments[-1][1] == self.say_english_voice:
                    prev_text, prev_voice = segments[-1]
                    segments[-1] = (prev_text + tail, prev_voice)
                else:
                    segments.append((tail, self.say_voice))

        if not segments:
            return [(text, self.say_voice)]

        merged = []
        for segment_text, voice in segments:
            if not segment_text:
                continue
            if merged and merged[-1][1] == voice:
                merged[-1] = (merged[-1][0] + segment_text, voice)
            else:
                merged.append((segment_text, voice))
        return merged

    def _should_use_english_voice_for_token(self, token):
        if not token:
            return False
        if token.isupper() and len(token) >= 2:
            return self.spell_english_acronyms
        if not self.detect_english_words:
            return False
        if self.language_detector is None:
            return False
        if not re.fullmatch(r"[A-Za-z][A-Za-z']*", token):
            return False
        if len(token) < 3:
            return False

        try:
            confidence_values = self.language_detector.compute_language_confidence_values(token)
        except Exception:
            return False

        english_confidence = 0.0
        vietnamese_confidence = 0.0
        for confidence in confidence_values:
            if confidence.language == Language.ENGLISH:
                english_confidence = confidence.value
            elif confidence.language == Language.VIETNAMESE:
                vietnamese_confidence = confidence.value

        return english_confidence >= 0.55 and (english_confidence - vietnamese_confidence) >= 0.15

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
            print("TTS error (say playback): timeout")
            with self.runtime_lock:
                if self.current_proc and self.current_proc.poll() is None:
                    self.current_proc.kill()
                self.current_proc = None
        except Exception as err:
            print(f"TTS error (say playback): {err}")
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
        prepared = self.prepare_text(text)
        if not prepared:
            return
        self.text_queue.put(prepared)

    def prepare_text(self, text):
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
        with self.config_lock:
            self.rate_multiplier = max(0.1, float(multiplier))

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

        with self.runtime_lock:
            if self.current_proc and self.current_proc.poll() is None:
                try:
                    self.current_proc.terminate()
                    self.current_proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.current_proc.kill()
                except Exception as err:
                    print(f"TTS stop error (playback): {err}")
            self.current_proc = None

    def stop(self):
        self.text_queue.put(self.stop_token)
