import logging
import re

from pydash import strings as pydash_strings

logger = logging.getLogger(__name__)


class TTSSegmenter:
    """
    Segments text for bilingual English/Vietnamese text-to-speech playback.

    Detects English phrases and tokens using fastText language detection and rule-based
    heuristics (acronyms, camelCase), enabling per-segment voice switching.
    """
    def __init__(
        self,
        *,
        spell_english_acronyms,
        detect_english_words,
        debug_lang_detection,
        fasttext_en_threshold,
        fasttext_margin_threshold,
        fasttext_min_phrase_words,
        fasttext_max_phrase_words,
    ):
        self.spell_english_acronyms = bool(spell_english_acronyms)
        self.detect_english_words = bool(detect_english_words)
        self.debug_lang_detection = bool(debug_lang_detection)
        self.fasttext_en_threshold = float(fasttext_en_threshold)
        self.fasttext_margin_threshold = float(fasttext_margin_threshold)
        self.fasttext_min_phrase_words = max(2, int(fasttext_min_phrase_words))
        self.fasttext_max_phrase_words = max(self.fasttext_min_phrase_words, int(fasttext_max_phrase_words))
        self.fasttext_model = None
        self.fasttext_cache = {}
        self.fasttext_cache_max = 512

    def set_fasttext_model(self, model):
        """Set the fastText language detection model."""
        self.fasttext_model = model
        self.fasttext_cache.clear()

    def _log_lang_debug(self, message):
        """Log language detection debug message if debugging is enabled."""
        if self.debug_lang_detection:
            logger.debug(message)

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

    @staticmethod
    def _window_is_contiguous_phrase(text, matches, start_idx, end_idx):
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
        if token.isupper() and len(token) >= 2 and self.spell_english_acronyms:
            return True
        if self._is_camel_or_pascal(token):
            return True
        return False

    def _get_technical_english_positions(self, tokens):
        if not tokens:
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

    def segment_text_for_say(self, text, say_voice, say_english_voice):
        """Segment text into (text, voice) tuples for bilingual playback."""
        if not say_english_voice:
            return [(text, say_voice)]
        if say_english_voice == say_voice:
            return [(text, say_voice)]

        pattern = re.compile(r"\b[A-Za-z][A-Za-z']*\b")
        matches = list(pattern.finditer(text))
        if not matches:
            return [(text, say_voice)]

        english_indexes = self._find_english_token_indexes(text, matches)
        segments = []
        cursor = 0
        for idx, match in enumerate(matches):
            start, end = match.span()
            token = match.group(0)
            use_english_voice = idx in english_indexes
            target_voice = say_english_voice if use_english_voice else say_voice

            if start > cursor:
                prefix = text[cursor:start]
                if prefix:
                    if prefix.isspace() and segments and segments[-1][1] == target_voice:
                        prev_text, prev_voice = segments[-1]
                        segments[-1] = (prev_text + prefix, prev_voice)
                    else:
                        segments.append((prefix, say_voice))
            token_text = token
            if use_english_voice and token.isupper() and len(token) >= 2 and self.spell_english_acronyms:
                token_text = " ".join(token)
            segments.append((token_text, target_voice))
            cursor = end

        if cursor < len(text):
            tail = text[cursor:]
            if tail:
                if tail.isspace() and segments and segments[-1][1] == say_english_voice:
                    prev_text, prev_voice = segments[-1]
                    segments[-1] = (prev_text + tail, prev_voice)
                else:
                    segments.append((tail, say_voice))

        if not segments:
            return [(text, say_voice)]

        merged = []
        for segment_text, voice in segments:
            if not segment_text:
                continue
            if merged and merged[-1][1] == voice:
                merged[-1] = (merged[-1][0] + segment_text, voice)
            else:
                merged.append((segment_text, voice))
        return merged

    def build_multivoice_markup(self, text, say_voice, say_english_voice):
        """Build SSML-like voice markup for macOS say command."""
        if not say_english_voice or say_english_voice == say_voice:
            return text

        segments = self.segment_text_for_say(text, say_voice, say_english_voice)
        if len(segments) == 1 and segments[0][1] == say_voice:
            return text

        parts = []
        for segment_text, voice in segments:
            if voice == say_english_voice:
                parts.append(f"[[voice {say_english_voice}]] {segment_text} [[voice {say_voice}]]")
            else:
                parts.append(segment_text)
        return "".join(parts).strip()
