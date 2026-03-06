import sys
import queue
import threading
import subprocess
import time
import re
import asyncio
import tempfile
import os
import json
import unicodedata
from difflib import SequenceMatcher
import mss
import pytesseract
from PIL import Image
from PyQt5.QtWidgets import QApplication, QWidget, QMenuBar, QAction, QInputDialog, QMessageBox
from PyQt5.QtCore import Qt, QTimer, QRect, QPointF
from PyQt5.QtGui import QPainter, QPen, QColor, QCursor, QIcon, QPixmap, QFont

try:
    import edge_tts
except Exception:
    edge_tts = None


def create_app_icon():
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(220, 20, 60))
    painter.drawRoundedRect(16, 16, 224, 224, 48, 48)

    painter.setPen(QColor(255, 255, 255))
    font = QFont("Arial", 128, QFont.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "S2")
    painter.end()

    return QIcon(pixmap)


class TTSWorker(threading.Thread):
    def __init__(self, base_rate_wpm=260, rate_multiplier=1.0):
        super().__init__(daemon=True)
        self.text_queue = queue.Queue()
        self.audio_queue = queue.Queue()
        self.stop_token = object()
        self.base_rate_wpm = max(120, int(base_rate_wpm))
        self.rate_multiplier = max(0.1, float(rate_multiplier))
        self.config_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.current_proc = None
        self.edge_voice = "en-US-AndrewMultilingualNeural"
        self.use_edge_tts = edge_tts is not None
        self.max_pending_texts = 1
        self.max_pending_audio = 1
        self.synth_thread = threading.Thread(target=self._synth_loop, daemon=True)
        self.synth_thread.start()

        if self.use_edge_tts:
            print(f"TTS backend: edge-tts ({self.edge_voice})")
        else:
            print("TTS disabled: edge-tts is not installed")

    def _edge_rate(self):
        multiplier = self.get_rate_multiplier()
        percent = int(round((multiplier - 1.0) * 100))
        return f"{percent:+d}%"

    async def _edge_save(self, text, output_path):
        communicator = edge_tts.Communicate(
            text=text,
            voice=self.edge_voice,
            rate=self._edge_rate(),
        )
        await communicator.save(output_path)

    def _synthesize_edge_tts(self, text):
        if not self.use_edge_tts:
            return None

        media_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                media_path = tmp.name

            asyncio.run(self._edge_save(text, media_path))
            return media_path
        except Exception as err:
            print(f"TTS error (edge-tts synth): {err}")
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except OSError:
                    pass
            return None

    def _play_media_file(self, media_path):
        try:
            with self.runtime_lock:
                self.current_proc = subprocess.Popen(["afplay", media_path])
            self.current_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print("TTS error (edge-tts playback): timeout")
            with self.runtime_lock:
                if self.current_proc and self.current_proc.poll() is None:
                    self.current_proc.kill()
                self.current_proc = None
        except Exception as err:
            print(f"TTS error (edge-tts playback): {err}")
            with self.runtime_lock:
                self.current_proc = None
        finally:
            with self.runtime_lock:
                self.current_proc = None
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except OSError:
                    pass

    def _synth_loop(self):
        while True:
            try:
                text = self.text_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if text is self.stop_token:
                self.audio_queue.put(self.stop_token)
                break

            media_path = self._synthesize_edge_tts(text)
            if media_path:
                if self.max_pending_audio > 0:
                    self._trim_queue_for_realtime(
                        self.audio_queue,
                        keep_latest=max(0, self.max_pending_audio - 1),
                        cleanup_media=True,
                    )
                self.audio_queue.put(media_path)

    def run(self):
        while True:
            try:
                item = self.audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is self.stop_token:
                self.stop_speaking()
                break
            self._play_media_file(item)

    def speak(self, text):
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return
        # Reduce natural pause added by TTS at trailing punctuation boundaries.
        normalized = re.sub(r"\s*[,:;.!?]+\s*$", "", normalized).strip()
        if not normalized:
            return
        if self.max_pending_texts > 0:
            self._trim_queue_for_realtime(
                self.text_queue,
                keep_latest=max(0, self.max_pending_texts - 1),
            )
        self.text_queue.put(normalized)

    def _trim_queue_for_realtime(self, target_queue, keep_latest, cleanup_media=False):
        kept = []
        dropped = []
        saw_stop = False
        while True:
            try:
                item = target_queue.get_nowait()
                if item is self.stop_token:
                    saw_stop = True
                    continue
                kept.append(item)
            except queue.Empty:
                break

        if keep_latest < len(kept):
            dropped = kept[:-keep_latest] if keep_latest > 0 else kept
            kept = kept[-keep_latest:] if keep_latest > 0 else []

        for item in kept:
            target_queue.put(item)
        if saw_stop:
            target_queue.put(self.stop_token)

        if cleanup_media:
            for item in dropped:
                if isinstance(item, str) and os.path.exists(item):
                    try:
                        os.remove(item)
                    except OSError:
                        pass

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
        self._trim_queue_for_realtime(self.text_queue, keep_latest=0)
        self._trim_queue_for_realtime(self.audio_queue, keep_latest=0, cleanup_media=True)

        with self.runtime_lock:
            if self.current_proc and self.current_proc.poll() is None:
                try:
                    self.current_proc.terminate()
                    self.current_proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.current_proc.kill()
                except Exception as err:
                    print(f"TTS stop error (edge-tts playback): {err}")
            self.current_proc = None

    def stop(self):
        self.text_queue.put(self.stop_token)

class Overlay(QWidget):
    def __init__(self):
        super().__init__()

        self.setGeometry(300, 200, 600, 250)
        self.window_state_path = os.path.join(os.path.dirname(__file__), ".overlay_state.json")

        self.base_window_flags = Qt.FramelessWindowHint | Qt.Window
        self.apply_window_flags()

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self.margin = 12
        self.resizing = None
        self.min_width = 200
        self.min_height = 100
        self.resize_handle_size = 14
        self.move_handle_size = 14
        self.setMouseTracking(True)
        self.restore_last_geometry()

        # TTS
        self.last_text = ""
        self.last_text_cmp = ""
        self.last_frame_signature = None
        self.recent_spoken = {}
        self.recent_spoken_ttl_sec = 18.0
        self.similarity_skip_threshold = 0.92
        self.running = False
        self.ocr_in_progress = False
        self.state_lock = threading.Lock()
        self.tts_worker = TTSWorker(base_rate_wpm=260, rate_multiplier=1.35)
        self.tts_worker.start()

        # Timer OCR
        self.timer = QTimer()
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.capture_area)
        self.capture_interval_ms = 350

        self.start_action = None
        self.stop_action = None
        self.warned_tts_unavailable = False

    # ================= DRAW =================
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Border
        pen = QPen(QColor(255, 0, 0), 3)
        painter.setPen(pen)
        painter.drawRect(self.rect())

        # Top-right resize handle
        move_handle = self.move_handle_rect()
        resize_handle = self.resize_handle_rect()

        painter.fillRect(move_handle, QColor(255, 255, 255, 210))
        painter.fillRect(resize_handle, QColor(255, 255, 255, 210))
        painter.setPen(QPen(QColor(255, 0, 0), 2))
        painter.drawRect(move_handle)
        painter.drawRect(resize_handle)
        self.draw_move_icon(painter, move_handle)
        self.draw_resize_icon(painter, resize_handle)

    def draw_move_icon(self, painter, rect):
        cx = rect.x() + (rect.width() / 2.0)
        cy = rect.y() + (rect.height() / 2.0)
        icon_pen = QPen(QColor(200, 0, 0), 1.2)
        icon_pen.setCapStyle(Qt.RoundCap)
        icon_pen.setJoinStyle(Qt.RoundJoin)
        icon_pen.setCosmetic(True)
        painter.setPen(icon_pen)
        arm = 3.4
        head = 1.8

        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))

        # Left arrow
        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx - arm + head, cy - head))
        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx - arm + head, cy + head))
        # Right arrow
        painter.drawLine(QPointF(cx + arm, cy), QPointF(cx + arm - head, cy - head))
        painter.drawLine(QPointF(cx + arm, cy), QPointF(cx + arm - head, cy + head))
        # Up arrow
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx - head, cy - arm + head))
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx + head, cy - arm + head))
        # Down arrow
        painter.drawLine(QPointF(cx, cy + arm), QPointF(cx - head, cy + arm - head))
        painter.drawLine(QPointF(cx, cy + arm), QPointF(cx + head, cy + arm - head))

        painter.setBrush(QColor(200, 0, 0))
        painter.drawEllipse(QPointF(cx, cy), 0.9, 0.9)

    def draw_resize_icon(self, painter, rect):
        left = rect.left() + 3
        top = rect.top() + 3
        right = rect.right() - 3
        bottom = rect.bottom() - 3
        icon_pen = QPen(QColor(200, 0, 0), 2)
        painter.setPen(icon_pen)

        # Main diagonal
        painter.drawLine(left, bottom, right, top)
        # Arrow at top-right
        painter.drawLine(right, top, right - 3, top)
        painter.drawLine(right, top, right, top + 3)
        # Arrow at bottom-left
        painter.drawLine(left, bottom, left + 3, bottom)
        painter.drawLine(left, bottom, left, bottom - 3)

    def move_handle_rect(self):
        padding = 6
        return QRect(
            padding,
            padding,
            self.move_handle_size,
            self.move_handle_size,
        )

    def resize_handle_rect(self):
        padding = 6
        return QRect(
            self.width() - self.resize_handle_size - padding,
            padding,
            self.resize_handle_size,
            self.resize_handle_size,
        )

    # ================= MOUSE EVENTS =================
    def mousePressEvent(self, event):
        self.start_pos = event.globalPos()
        self.start_geom = self.geometry()

        if self.move_handle_rect().contains(event.pos()):
            self.resizing = "move_handle"
        elif self.resize_handle_rect().contains(event.pos()):
            self.resizing = "top_right_handle"
        else:
            self.resizing = None

    def mouseMoveEvent(self, event):
        # Cursor change
        if self.move_handle_rect().contains(event.pos()):
            self.setCursor(QCursor(Qt.SizeAllCursor))
        elif self.resize_handle_rect().contains(event.pos()):
            self.setCursor(QCursor(Qt.SizeBDiagCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

        if not hasattr(self, "resizing") or not self.resizing:
            return

        delta = event.globalPos() - self.start_pos
        geom = QRect(self.start_geom)

        if self.resizing == "move_handle":
            self.move(self.start_geom.topLeft() + delta)

        elif self.resizing == "top_right_handle":
            new_width = max(self.min_width, self.start_geom.width() + delta.x())
            new_height = max(self.min_height, self.start_geom.height() - delta.y())

            geom.setWidth(new_width)
            bottom = self.start_geom.bottom()
            geom.setTop(bottom - new_height + 1)
            self.setGeometry(geom)

    def mouseReleaseEvent(self, event):
        self.resizing = None
        self.save_current_geometry()

    def closeEvent(self, event):
        self.save_current_geometry()
        self.stop_capture()
        self.tts_worker.stop()
        self.tts_worker.join(timeout=1.0)
        super().closeEvent(event)

    def restore_last_geometry(self):
        try:
            if not os.path.exists(self.window_state_path):
                return
            with open(self.window_state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)

            x = int(data.get("x"))
            y = int(data.get("y"))
            width = max(self.min_width, int(data.get("width")))
            height = max(self.min_height, int(data.get("height")))
            self.setGeometry(x, y, width, height)
        except Exception as err:
            print(f"Window state restore error: {err}")

    def save_current_geometry(self):
        try:
            geom = self.geometry()
            payload = {
                "x": int(geom.x()),
                "y": int(geom.y()),
                "width": int(geom.width()),
                "height": int(geom.height()),
            }
            with open(self.window_state_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except Exception as err:
            print(f"Window state save error: {err}")

    def apply_window_flags(self):
        flags = self.base_window_flags | Qt.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
            self.raise_()

    def bind_menu_actions(self, start_action, stop_action):
        self.start_action = start_action
        self.stop_action = stop_action
        self.update_capture_actions()

    def prompt_speed_multiplier(self):
        current = self.tts_worker.get_rate_multiplier()
        text, ok = QInputDialog.getText(
            self,
            "Speed Multiplier",
            "Enter speed (for example: 1, 2, 2.5):",
            text=f"{current:g}",
        )
        if not ok:
            return

        multiplier = self.parse_speed_multiplier(text)
        if multiplier is None:
            QMessageBox.warning(
                self,
                "Invalid Speed",
                "Invalid value. Use 1, 2, or 2.5 (must be greater than 0).",
            )
            return

        self.tts_worker.set_rate_multiplier(multiplier)
        print(f"TTS speed set to {multiplier:g}x ({self.tts_worker.get_rate_wpm()} wpm)")

    def parse_speed_multiplier(self, value):
        raw = value.strip().lower()
        raw = raw.replace(",", ".")
        try:
            multiplier = float(raw)
        except ValueError:
            return None
        if multiplier <= 0:
            return None
        return multiplier

    # ================= START / STOP =================
    def start_capture(self):
        if self.running:
            return
        if not self.tts_worker.use_edge_tts:
            if not self.warned_tts_unavailable:
                QMessageBox.warning(
                    self,
                    "TTS Unavailable",
                    "edge-tts is not installed in the current Python environment.",
                )
                self.warned_tts_unavailable = True
            return
        self.running = True
        self.last_text = ""
        self.last_text_cmp = ""
        self.last_frame_signature = None
        self.recent_spoken.clear()
        self.timer.start(self.capture_interval_ms)
        self.update_capture_actions()

    def stop_capture(self):
        if not self.running:
            return
        self.running = False
        self.timer.stop()
        self.last_text_cmp = ""
        self.recent_spoken.clear()
        self.tts_worker.stop_speaking()
        self.update_capture_actions()

    def update_capture_actions(self):
        if self.start_action is None or self.stop_action is None:
            return
        self.start_action.setEnabled(not self.running)
        self.stop_action.setEnabled(self.running)

    # ================= OCR CAPTURE =================
    def capture_area(self):
        with self.state_lock:
            if self.ocr_in_progress:
                return
            self.ocr_in_progress = True

        worker_started = False
        try:
            self.setWindowOpacity(0)
            QApplication.processEvents()

            geo = self.geometry()

            with mss.mss() as sct:
                monitor = {
                    "top": geo.y(),
                    "left": geo.x(),
                    "width": geo.width(),
                    "height": geo.height()
                }
                screenshot = sct.grab(monitor)
                img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

            self.setWindowOpacity(1)
            signature = img.convert("L").resize((64, 36)).tobytes()
            if signature == self.last_frame_signature:
                with self.state_lock:
                    self.ocr_in_progress = False
                return
            self.last_frame_signature = signature
            threading.Thread(
                target=self.process_image,
                args=(img,),
                daemon=True,
            ).start()
            worker_started = True
        except Exception as err:
            print(f"Capture error: {err}")
            self.setWindowOpacity(1)
        finally:
            if not worker_started:
                with self.state_lock:
                    self.ocr_in_progress = False

    def process_image(self, img):
        try:
            gray = img.convert("L")
            text = pytesseract.image_to_string(
                gray,
                lang="vie",
                config="--oem 1 --psm 6",
            ).strip()
            if not text:
                return

            normalized_text = self.sanitize_ocr_text(text)
            if not normalized_text:
                return

            text_to_speak = None
            next_last_text = None
            next_last_cmp = None
            with self.state_lock:
                if not self.running:
                    return

                cmp_text = self.make_compare_key(normalized_text)
                if not cmp_text:
                    return

                self.prune_recent_spoken_locked()

                if self.last_text_cmp:
                    if normalized_text.startswith(self.last_text):
                        suffix = normalized_text[len(self.last_text):].strip()
                        if len(suffix) < 2:
                            return
                        text_to_speak = suffix
                    elif cmp_text.startswith(self.last_text_cmp):
                        return
                    else:
                        similarity = SequenceMatcher(None, cmp_text, self.last_text_cmp).ratio()
                        if similarity >= self.similarity_skip_threshold:
                            return
                        text_to_speak = normalized_text
                else:
                    text_to_speak = normalized_text

                next_last_text = normalized_text
                next_last_cmp = cmp_text
                text_to_speak = self.sanitize_ocr_text(text_to_speak)
                if self.is_noise_text(text_to_speak):
                    return
                speak_cmp = self.make_compare_key(text_to_speak)
                if not speak_cmp:
                    return
                if speak_cmp in self.recent_spoken:
                    return
                self.recent_spoken[speak_cmp] = time.monotonic()

            if text_to_speak:
                print("Detected:", text_to_speak)
                self.tts_worker.speak(text_to_speak)
                with self.state_lock:
                    self.last_text = next_last_text
                    self.last_text_cmp = next_last_cmp
        except Exception as err:
            print(f"OCR error: {err}")
        finally:
            with self.state_lock:
                self.ocr_in_progress = False

    def make_compare_key(self, value):
        normalized = value.lower().strip()
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

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
            if unicodedata.category(ch).startswith("L"):
                filtered.append(ch)
                continue

        normalized = " ".join("".join(filtered).split()).strip()
        if not normalized:
            return ""

        # OCR often appends a dangling 1-char token (for example: "... , h").
        parts = normalized.split()
        if len(parts) >= 4 and len(parts[-1]) == 1 and parts[-1].lower() not in {"a", "i"}:
            normalized = " ".join(parts[:-1]).strip()
        return normalized

    def is_noise_text(self, value):
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

    def prune_recent_spoken_locked(self):
        now = time.monotonic()
        expired = [
            key for key, seen_at in self.recent_spoken.items()
            if now - seen_at > self.recent_spoken_ttl_sec
        ]
        for key in expired:
            del self.recent_spoken[key]


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app_icon = create_app_icon()
    app.setWindowIcon(app_icon)
    overlay = Overlay()
    overlay.setWindowIcon(app_icon)

    menu_bar = QMenuBar()
    menu_bar.setNativeMenuBar(True)
    options_menu = menu_bar.addMenu("Options")
    start_action = QAction("Start", menu_bar)
    stop_action = QAction("Stop", menu_bar)
    speed_action = QAction("Set Speed...", menu_bar)
    close_action = QAction("Close App", menu_bar)
    start_action.triggered.connect(overlay.start_capture)
    stop_action.triggered.connect(overlay.stop_capture)
    speed_action.triggered.connect(overlay.prompt_speed_multiplier)
    close_action.triggered.connect(app.quit)
    options_menu.addAction(start_action)
    options_menu.addAction(stop_action)
    options_menu.addAction(speed_action)
    options_menu.addAction(close_action)
    overlay.bind_menu_actions(start_action, stop_action)

    # Keep a Python reference so the native menu bar is not garbage-collected.
    app.menu_bar = menu_bar

    overlay.show()
    sys.exit(app.exec_())
