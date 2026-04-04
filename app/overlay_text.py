import re


class OCRTextProcessor:
    """Minimal OCR text processor - keeps raw output unchanged."""

    @staticmethod
    def make_compare_key(value):
        """Normalize for similarity comparison: lowercase, collapse whitespace."""
        normalized = value.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @staticmethod
    def sanitize_ocr_text(value):
        """Keep OCR output exactly as-is - only collapse extra whitespace."""
        if not value:
            return ""
        # Only collapse multiple spaces/tabs/newlines into single space
        normalized = re.sub(r"\s+", " ", value).strip()
        return normalized

    @staticmethod
    def is_noise_text(value):
        """Reject only empty text."""
        return not value or not value.strip()
