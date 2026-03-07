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
        self.fasttext_model = None
        self.fasttext_cache = {}
        self.fasttext_cache_max = 512
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
        hint_words_raw = os.getenv(
            "W3_TTS_EN_HINT_WORDS",
            "api,sdk,kms,aws,cloud,http,https,json,token,generate,data,datakey,key,encrypt,decrypt,random,byte,kilobyte",
        )
        self.english_hint_words = {
            pydash_strings.trim(word).lower()
            for word in hint_words_raw.split(",")
            if pydash_strings.trim(word)
        }

        if self.use_say_tts:
            self.available_say_voices = self._load_available_say_voices()
            self.fasttext_model = self._load_fasttext_model()
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
            if self.detect_english_words and self.fasttext_model is None:
                print("TTS language detector: fastText unavailable (falling back to acronym/camel-case only)")
        else:
            print("TTS disabled: macOS 'say' is unavailable on this platform")

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

    def _log_lang_debug(self, message):
        if self.debug_lang_detection:
            print(f"TTS lang-debug: {message}")

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
                print(f"TTS language detector: fastText ({model_path})")
                return model
            except Exception as err:
                print(f"TTS language detector: failed to load {model_path}: {err}")
        return None

    @staticmethod
    def _is_english_label(label):
        if not label:
            return False
        if label.startswith("__label__"):
            label = label[len("__label__"):]
        label = label.lower()
        return label == "en" or label.startswith("en_")

    @staticmethod
    def _is_camel_or_pascal(token):
        if not token or not token.isalpha():
            return False
        has_lower = any(ch.islower() for ch in token)
        has_upper = any(ch.isupper() for ch in token)
        if not (has_lower and has_upper):
            return False
        # Plain title-case words are too noisy in Vietnamese OCR streams.
        return not token.istitle()

    def _is_english_phrase_with_fasttext(self, phrase):
        phrase = pydash_strings.trim(re.sub(r"\s+", " ", phrase))
        if not phrase:
            return False, "", 0.0, 0.0

        cached = self.fasttext_cache.get(phrase)
        if cached is not None:
            return cached

        if self.fasttext_model is None:
            payload = (False, "", 0.0, 0.0)
        else:
            try:
                labels, probs = self.fasttext_model.predict(phrase, k=2)
            except Exception:
                labels, probs = [], []

            if not labels:
                payload = (False, "", 0.0, 0.0)
            else:
                top_label = labels[0]
                top_prob = float(probs[0]) if len(probs) > 0 else 0.0
                second_prob = float(probs[1]) if len(probs) > 1 else 0.0
                result = (
                    self._is_english_label(top_label)
                    and top_prob >= self.fasttext_en_threshold
                    and (top_prob - second_prob) >= self.fasttext_margin_threshold
                )
                payload = (result, top_label, top_prob, second_prob)

        if len(self.fasttext_cache) >= self.fasttext_cache_max:
            self.fasttext_cache.clear()
        self.fasttext_cache[phrase] = payload
        return payload

    def _window_is_contiguous_phrase(self, text, matches, start_idx, end_idx):
        if end_idx <= start_idx:
            return False
        for idx in range(start_idx, end_idx - 1):
            gap = text[matches[idx].end():matches[idx + 1].start()]
            if not re.fullmatch(r"[\s/_-]+", gap):
                return False
        return True

    def _is_technical_english_token(self, token):
        if not token:
            return False
        lowered = token.lower()
        if lowered in self.english_hint_words:
            return True
        if token.isupper() and len(token) >= 2 and self.spell_english_acronyms:
            return True
        if self._is_camel_or_pascal(token):
            return True
        return False

    def _get_technical_english_positions(self, tokens):
        if not tokens:
            return []

        normalized_tokens = [token.lower() for token in tokens]
        if not any(token in self.english_hint_words for token in normalized_tokens):
            return []

        positions = [idx for idx, token in enumerate(tokens) if self._is_technical_english_token(token)]
        if len(positions) >= 2:
            return positions

        # Allow single-token detection only for clear acronyms (API/KMS/SDK...),
        # never for generic lowercase words.
        if len(positions) == 1:
            token = tokens[positions[0]]
            if token.isupper() and len(token) >= 2:
                return positions
        return []

    def _find_english_token_indexes(self, text, matches):
        english_indexes = set()
        debug_reasons = {}

        for idx, match in enumerate(matches):
            token = match.group(0)
            if token.isupper() and len(token) >= 2 and self.spell_english_acronyms:
                english_indexes.add(idx)
                debug_reasons[idx] = "acronym"
                continue
            if self._is_camel_or_pascal(token):
                english_indexes.add(idx)
                debug_reasons[idx] = "camel/pascal"

        if not self.detect_english_words or self.fasttext_model is None:
            if english_indexes:
                selected = [f"{matches[i].group(0)}[{debug_reasons.get(i, 'rule')}]" for i in sorted(english_indexes)]
                self._log_lang_debug("EN tokens: " + ", ".join(selected))
            return english_indexes

        total = len(matches)
        for window_size in range(self.fasttext_max_phrase_words, self.fasttext_min_phrase_words - 1, -1):
            if window_size > total:
                continue
            for start_idx in range(0, total - window_size + 1):
                end_idx = start_idx + window_size
                if not self._window_is_contiguous_phrase(text, matches, start_idx, end_idx):
                    continue
                tokens = [matches[pos].group(0) for pos in range(start_idx, end_idx)]
                if sum(1 for token in tokens if len(token) >= 3) < 2:
                    continue
                technical_positions = self._get_technical_english_positions(tokens)
                if technical_positions:
                    absolute_positions = [start_idx + relative for relative in technical_positions]
                    new_positions = [pos for pos in absolute_positions if pos not in english_indexes]
                    english_indexes.update(absolute_positions)
                    for pos in absolute_positions:
                        debug_reasons[pos] = "technical-hint"
                    if new_positions:
                        self._log_lang_debug(f"EN phrase (technical): '{' '.join(tokens)}'")
                    continue
                phrase = " ".join(tokens)
                is_en, top_label, top_prob, second_prob = self._is_english_phrase_with_fasttext(phrase)
                if not is_en:
                    continue
                new_positions = [pos for pos in range(start_idx, end_idx) if pos not in english_indexes]
                english_indexes.update(range(start_idx, end_idx))
                for pos in range(start_idx, end_idx):
                    debug_reasons[pos] = "fasttext"
                if new_positions:
                    self._log_lang_debug(
                        f"EN phrase (fastText): '{phrase}' label={top_label} "
                        f"p1={top_prob:.3f} p2={second_prob:.3f}"
                    )
        if english_indexes:
            selected = [f"{matches[i].group(0)}[{debug_reasons.get(i, 'rule')}]" for i in sorted(english_indexes)]
            self._log_lang_debug("EN tokens: " + ", ".join(selected))
        return english_indexes

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
        matches = list(pattern.finditer(text))
        if not matches:
            return [(text, self.say_voice)]

        english_indexes = self._find_english_token_indexes(text, matches)
        segments = []
        cursor = 0
        for idx, match in enumerate(matches):
            start, end = match.span()
            token = match.group(0)
            use_english_voice = idx in english_indexes
            target_voice = self.say_english_voice if use_english_voice else self.say_voice

            if start > cursor:
                prefix = text[cursor:start]
                if prefix:
                    if prefix.isspace() and segments and segments[-1][1] == target_voice:
                        prev_text, prev_voice = segments[-1]
                        segments[-1] = (prev_text + prefix, prev_voice)
                    else:
                        segments.append((prefix, self.say_voice))
            token_text = token
            if use_english_voice and token.isupper() and len(token) >= 2 and self.spell_english_acronyms:
                token_text = " ".join(token)
            segments.append((token_text, target_voice))
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
