import os
import queue
import subprocess
import sys
import threading

from pydash import strings as pydash_strings


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
        self.use_say_tts = (sys.platform == "darwin")
        self.say_voice = os.getenv("W3_TTS_SAY_VOICE", "Linh").strip()

        if self.use_say_tts:
            print("TTS backend: macOS say")
        else:
            print("TTS disabled: macOS 'say' is unavailable on this platform")

    def _play_text_with_say(self, text):
        words = max(1, len(text.split()))
        expected_sec = (words / max(self.get_rate_wpm(), 1)) * 60.0
        timeout_sec = max(5.0, min(45.0, expected_sec * 2.5 + 2.0))
        try:
            cmd = ["say", "-r", str(self.get_rate_wpm())]
            if self.say_voice:
                cmd.extend(["-v", self.say_voice])
            cmd.append(text)
            with self.runtime_lock:
                self.current_proc = subprocess.Popen(cmd)
            self.current_proc.wait(timeout=timeout_sec)
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
