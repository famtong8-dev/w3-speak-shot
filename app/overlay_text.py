import re
import unicodedata


class OCRTextProcessor:
    @staticmethod
    def make_compare_key(value):
        normalized = value.lower().strip()
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @staticmethod
    def is_supported_letter(ch):
        if not ch or not ch.isalpha():
            return False

        decomposed = unicodedata.normalize("NFD", ch)
        base_letters = [c for c in decomposed if unicodedata.category(c).startswith("L")]
        if not base_letters:
            return False

        base = base_letters[0].lower()
        if base not in "abcdefghijklmnopqrstuvwxyzđ":
            return False

        allowed_marks = {
            "\u0300",  # grave
            "\u0301",  # acute
            "\u0303",  # tilde
            "\u0309",  # hook above
            "\u0323",  # dot below
            "\u0306",  # breve
            "\u0302",  # circumflex
            "\u031b",  # horn
        }
        for c in decomposed:
            if unicodedata.category(c).startswith("M") and c not in allowed_marks:
                return False
        return True

    @staticmethod
    def strip_marks(value):
        decomposed = unicodedata.normalize("NFD", value)
        return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")

    def is_garbled_token(self, token):
        letters = "".join(ch for ch in token if ch.isalpha())
        if len(letters) < 3:
            return False
        base = re.sub(r"[^a-zđ]", "", self.strip_marks(letters).lower())
        if len(base) < 3:
            return True
        # Drop artifacts like "aáa", "ooo", "ụụu" that are unlikely words.
        if len(set(base)) == 1:
            return True
        return False

    def sanitize_ocr_text(self, value):
        allowed_punct = set(" .,;:!?-()/+&%$@#'\"")
        filtered = []
        for ch in value:
            if ch.isspace():
                filtered.append(" ")
                continue
            if ch in allowed_punct:
                filtered.append(ch)
                continue
            if "0" <= ch <= "9":
                filtered.append(ch)
                continue
            if self.is_supported_letter(ch):
                filtered.append(ch)
                continue

        normalized = " ".join("".join(filtered).split()).strip()
        if not normalized:
            return ""

        parts = [part for part in normalized.split() if not self.is_garbled_token(part)]
        normalized = " ".join(parts).strip()
        if not normalized:
            return ""

        # OCR often appends a dangling 1-char token (for example: "... , h").
        parts = normalized.split()
        if (
            len(parts) >= 4
            and len(parts[-1]) == 1
            and parts[-1].isalpha()
            and parts[-1].lower() not in {"a", "i"}
        ):
            normalized = " ".join(parts[:-1]).strip()
        return normalized

    @staticmethod
    def is_noise_text(value):
        if not value:
            return True
        letters = sum(1 for ch in value if ch.isalpha())
        digits = sum(1 for ch in value if ch.isdigit())
        if letters < 3:
            return True
        if digits > max(letters, 1):
            return True
        if re.search(r"(.)\1{6,}", value):
            return True
        return False
