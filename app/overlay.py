import json
import logging
import os
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher

import mss
import pytesseract
from PIL import Image, ImageDraw
from PyQt5.QtCore import Qt, QTimer, QRect, QPointF
from PyQt5.QtGui import QPainter, QPen, QColor, QCursor
from PyQt5.QtWidgets import QWidget

logger = logging.getLogger(__name__)

try:
    from .overlay_text import OCRTextProcessor
    from .tts_worker import TTSWorker
except ImportError:
    from overlay_text import OCRTextProcessor
    from tts_worker import TTSWorker


class Overlay(QWidget):
    def __init__(self):
        super().__init__()

        self.setGeometry(300, 200, 600, 250)
        self.window_state_path = os.path.join(os.path.dirname(__file__), "store", ".overlay_state.json")

        self.base_window_flags = Qt.FramelessWindowHint | Qt.Window
        self.apply_window_flags()

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self.resizing = None
        self.start_pos = None
        self.start_geom = None
        self.min_width = 200
        self.min_height = 100
        self.resize_handle_size = 14
        self.move_handle_size = 14
        self.status_indicator_size = 14
        self.close_button_size = 14
        self.toggle_button_size = 14
        self.speed_button_size = 14
        self.setMouseTracking(True)
        self.restore_last_geometry()

        # TTS
        self.last_text = ""
        self.last_text_cmp = ""
        self.last_frame_signature = None
        self.recent_spoken = {}
        self.recent_spoken_ttl_sec = 18.0
        self.recent_spoken_max_size = 200  # Hard limit to prevent unbounded growth
        self.similarity_skip_threshold = 0.92
        self.running = False
        self.ocr_in_progress = False
        self.state_lock = threading.Lock()
        self.text_processor = OCRTextProcessor()
        # Enable debug mode if W3_DEBUG_TTS_OUTPUT env var is set
        debug_output = os.getenv("W3_DEBUG_TTS_OUTPUT")
        if debug_output:
            logger.warning(f"!!! DEBUG MODE ENABLED - buffering audio to {debug_output}")
        self.tts_worker = TTSWorker(base_rate_wpm=260, debug_output_file=debug_output)
        self.tts_worker.start()

        # Timer OCR - constants
        self.timer = QTimer()
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.capture_area)
        self.capture_interval_ms = 120
        self.frame_signature_size = (64, 36)  # Downsampled frame for change detection

        self.start_action = None
        self.stop_action = None

    # ================= DRAW =================
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Border
        pen = QPen(QColor(255, 0, 0), 3)
        painter.setPen(pen)
        painter.drawRect(self.rect())

        move_handle = self.move_handle_rect()
        resize_handle = self.resize_handle_rect()
        status_indicator = self.status_indicator_rect()
        close_button = self.close_button_rect()
        toggle_button = self.toggle_button_rect()
        speed_up_button = self.speed_up_button_rect()
        speed_down_button = self.speed_down_button_rect()

        painter.fillRect(move_handle, QColor(255, 255, 255, 210))
        painter.fillRect(resize_handle, QColor(255, 255, 255, 210))
        if self.running:
            painter.fillRect(status_indicator, QColor(46, 204, 113))
        else:
            painter.fillRect(status_indicator, QColor(170, 170, 170, 180))
        painter.fillRect(close_button, QColor(255, 255, 255, 210))
        painter.fillRect(toggle_button, QColor(255, 255, 255, 210))
        painter.fillRect(speed_up_button, QColor(255, 255, 255, 210))
        painter.fillRect(speed_down_button, QColor(255, 255, 255, 210))
        painter.setPen(QPen(QColor(255, 0, 0), 2))
        painter.drawRect(move_handle)
        painter.drawRect(resize_handle)
        painter.drawRect(status_indicator)
        painter.drawRect(close_button)
        painter.drawRect(toggle_button)
        painter.drawRect(speed_up_button)
        painter.drawRect(speed_down_button)
        self.draw_move_icon(painter, move_handle)
        self.draw_resize_icon(painter, resize_handle)
        self.draw_close_icon(painter, close_button)
        self.draw_toggle_icon(painter, toggle_button)
        self.draw_plus_icon(painter, speed_up_button)
        self.draw_minus_icon(painter, speed_down_button)

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

    def draw_close_icon(self, painter, rect):
        left = rect.left() + 4
        top = rect.top() + 4
        right = rect.right() - 4
        bottom = rect.bottom() - 4
        icon_pen = QPen(QColor(200, 0, 0), 2)
        painter.setPen(icon_pen)
        painter.drawLine(left, top, right, bottom)
        painter.drawLine(left, bottom, right, top)

    def draw_toggle_icon(self, painter, rect):
        icon_pen = QPen(QColor(200, 0, 0), 1.5)
        icon_pen.setCapStyle(Qt.RoundCap)
        icon_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(icon_pen)
        if self.running:
            # Stop icon: two vertical bars (■ simplified as ▐▌)
            lx = rect.left() + 4
            rx = rect.right() - 4
            ty = rect.top() + 3
            by = rect.bottom() - 3
            painter.drawLine(lx, ty, lx, by)
            painter.drawLine(rx, ty, rx, by)
        else:
            # Play icon: triangle ▶
            cx = rect.left() + 4
            cy_mid = rect.top() + rect.height() // 2
            tip = rect.right() - 3
            painter.setBrush(QColor(200, 0, 0))
            from PyQt5.QtGui import QPolygon
            from PyQt5.QtCore import QPoint
            triangle = QPolygon([
                QPoint(cx, rect.top() + 3),
                QPoint(cx, rect.bottom() - 3),
                QPoint(tip, cy_mid),
            ])
            painter.drawPolygon(triangle)
            painter.setBrush(Qt.NoBrush)

    def draw_plus_icon(self, painter, rect):
        icon_pen = QPen(QColor(200, 0, 0), 2)
        icon_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(icon_pen)
        cx = rect.left() + rect.width() // 2
        cy = rect.top() + rect.height() // 2
        arm = 3
        painter.drawLine(cx - arm, cy, cx + arm, cy)
        painter.drawLine(cx, cy - arm, cx, cy + arm)

    def draw_minus_icon(self, painter, rect):
        icon_pen = QPen(QColor(200, 0, 0), 2)
        icon_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(icon_pen)
        cx = rect.left() + rect.width() // 2
        cy = rect.top() + rect.height() // 2
        arm = 3
        painter.drawLine(cx - arm, cy, cx + arm, cy)

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

    def status_indicator_rect(self):
        padding = 6
        return QRect(
            padding,
            self.height() - self.status_indicator_size - padding,
            self.status_indicator_size,
            self.status_indicator_size,
        )

    def toggle_button_rect(self):
        padding = 6
        gap = 4
        status = self.status_indicator_rect()
        return QRect(
            padding,
            status.top() - self.toggle_button_size - gap,
            self.toggle_button_size,
            self.toggle_button_size,
        )

    def close_button_rect(self):
        padding = 6
        return QRect(
            self.width() - self.close_button_size - padding,
            self.height() - self.close_button_size - padding,
            self.close_button_size,
            self.close_button_size,
        )

    def speed_up_button_rect(self):
        gap = 4
        close = self.close_button_rect()
        return QRect(
            close.left(),
            close.top() - self.speed_button_size - gap,
            self.speed_button_size,
            self.speed_button_size,
        )

    def speed_down_button_rect(self):
        gap = 4
        up = self.speed_up_button_rect()
        return QRect(
            up.left(),
            up.top() - self.speed_button_size - gap,
            self.speed_button_size,
            self.speed_button_size,
        )

    # ================= MOUSE EVENTS =================
    def mousePressEvent(self, event):
        self.start_pos = event.globalPos()
        self.start_geom = self.geometry()

        if self.close_button_rect().contains(event.pos()):
            self.close()
            return
        if self.toggle_button_rect().contains(event.pos()):
            if self.running:
                self.stop_capture()
            else:
                self.start_capture()
            return
        if self.speed_up_button_rect().contains(event.pos()):
            new_rate = round(min(self.tts_worker.base_rate_multiplier + 0.25, 3.0), 2)
            self.tts_worker.set_base_rate_multiplier(new_rate)
            logger.info(f"[SPEED  ] {new_rate}x")
            return
        if self.speed_down_button_rect().contains(event.pos()):
            new_rate = round(max(self.tts_worker.base_rate_multiplier - 0.25, 0.5), 2)
            self.tts_worker.set_base_rate_multiplier(new_rate)
            logger.info(f"[SPEED  ] {new_rate}x")
            return
        if self.move_handle_rect().contains(event.pos()):
            self.resizing = "move_handle"
        elif self.resize_handle_rect().contains(event.pos()):
            self.resizing = "top_right_handle"
        else:
            self.resizing = None

    def mouseMoveEvent(self, event):
        # Cursor change
        if any(r.contains(event.pos()) for r in [
            self.close_button_rect(), self.toggle_button_rect(),
            self.speed_up_button_rect(), self.speed_down_button_rect(),
        ]):
            self.setCursor(QCursor(Qt.PointingHandCursor))
        elif self.move_handle_rect().contains(event.pos()):
            self.setCursor(QCursor(Qt.SizeAllCursor))
        elif self.resize_handle_rect().contains(event.pos()):
            self.setCursor(QCursor(Qt.SizeBDiagCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

        if not self.resizing:
            return

        delta = event.globalPos() - self.start_pos

        if self.resizing == "move_handle":
            self.move(self.start_geom.topLeft() + delta)

        elif self.resizing == "top_right_handle":
            new_width = max(self.min_width, self.start_geom.width() + delta.x())
            new_height = max(self.min_height, self.start_geom.height() - delta.y())

            geom = QRect(self.start_geom)
            geom.setWidth(new_width)
            bottom = self.start_geom.bottom()
            geom.setTop(bottom - new_height + 1)
            self.setGeometry(geom)

    def mouseReleaseEvent(self, event):
        had_active_resize = self.resizing is not None
        self.resizing = None
        if had_active_resize:
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

            # Validate keys exist and are convertible to int
            x = int(data.get("x", 300))
            y = int(data.get("y", 200))
            width = max(self.min_width, int(data.get("width", 600)))
            height = max(self.min_height, int(data.get("height", 250)))
            self.setGeometry(x, y, width, height)
        except (json.JSONDecodeError, ValueError, TypeError) as err:
            logger.warning(f"Window state restore error: {err}. Using defaults.")

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
        except (IOError, OSError, ValueError) as err:
            logger.warning(f"Failed to save window state: {err}")

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

    # ================= START / STOP =================
    def start_capture(self):
        if self.running:
            return
        self.running = True
        self.last_text = ""
        self.last_text_cmp = ""
        self.last_frame_signature = None
        self.recent_spoken.clear()
        self.timer.start(self.capture_interval_ms)
        self.update_capture_actions()
        self.update()

    def stop_capture(self):
        if not self.running:
            return
        self.running = False
        self.timer.stop()
        self.last_text_cmp = ""
        self.recent_spoken.clear()
        self.tts_worker.stop_speaking()
        self.update_capture_actions()
        self.update()

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

        try:
            geo = self.geometry()

            with mss.mss() as sct:
                monitor = {
                    "top": geo.y(),
                    "left": geo.x(),
                    "width": geo.width(),
                    "height": geo.height(),
                }
                screenshot = sct.grab(monitor)
                img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
                self.mask_overlay_artifacts_for_ocr(img)

            signature = img.convert("L").resize(self.frame_signature_size).tobytes()
            if signature == self.last_frame_signature:
                with self.state_lock:
                    self.ocr_in_progress = False
                return
            self.last_frame_signature = signature

            # Start worker thread - if it fails, flag will be reset in process_image finally
            try:
                threading.Thread(
                    target=self.process_image,
                    args=(img,),
                    daemon=True,
                ).start()
            except Exception as err:
                logger.error(f"Failed to start OCR thread: {err}")
                with self.state_lock:
                    self.ocr_in_progress = False
        except Exception as err:
            logger.error(f"Capture error: {err}")
            with self.state_lock:
                self.ocr_in_progress = False

    def mask_overlay_artifacts_for_ocr(self, img):
        # Remove overlay controls and border from OCR input without cropping content.
        draw = ImageDraw.Draw(img)
        width, height = img.size
        border = 4
        draw.rectangle((0, 0, width - 1, border), fill=(255, 255, 255))
        draw.rectangle((0, height - border - 1, width - 1, height - 1), fill=(255, 255, 255))
        draw.rectangle((0, 0, border, height - 1), fill=(255, 255, 255))
        draw.rectangle((width - border - 1, 0, width - 1, height - 1), fill=(255, 255, 255))

        expand = 3
        rects = [
            self.move_handle_rect(),
            self.resize_handle_rect(),
            self.status_indicator_rect(),
            self.close_button_rect(),
            self.toggle_button_rect(),
            self.speed_up_button_rect(),
            self.speed_down_button_rect(),
        ]
        for rect in rects:
            left = max(0, rect.left() - expand)
            top = max(0, rect.top() - expand)
            right = min(width - 1, rect.right() + expand)
            bottom = min(height - 1, rect.bottom() + expand)
            draw.rectangle((left, top, right, bottom), fill=(255, 255, 255))

    def process_image(self, img):
        try:
            gray = img.convert("L")
            text = pytesseract.image_to_string(
                gray,
                lang="vie+eng",
                config="--oem 1 --psm 6",
            ).strip()
            if not text:
                return

            # Log raw OCR output before any processing
            text_preview = text[:80] + "..." if len(text) > 80 else text
            logger.info(f"[OCR    ] {text_preview}")

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
                        # Wait for a minimally meaningful delta to reduce TTS churn.
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
                if self.is_noise_text(text_to_speak):
                    return
                speak_cmp = self.make_compare_key(text_to_speak)
                if not speak_cmp:
                    return
                if speak_cmp in self.recent_spoken:
                    return
                self.recent_spoken[speak_cmp] = time.monotonic()

            if text_to_speak:
                prepared_text = self.tts_worker.prepare_text(text_to_speak)
                if not prepared_text:
                    return
                text_preview = prepared_text[:70] + "..." if len(prepared_text) > 70 else prepared_text
                logger.info(f"[SPEAK  ] {text_preview}")
                self.tts_worker.speak(prepared_text)
                with self.state_lock:
                    self.last_text = next_last_text
                    self.last_text_cmp = next_last_cmp
        except Exception as err:
            logger.error(f"OCR error: {err}")
        finally:
            with self.state_lock:
                self.ocr_in_progress = False

    def make_compare_key(self, value):
        return self.text_processor.make_compare_key(value)

    def sanitize_ocr_text(self, value):
        return self.text_processor.sanitize_ocr_text(value)

    def is_noise_text(self, value):
        return self.text_processor.is_noise_text(value)

    def prune_recent_spoken_locked(self):
        now = time.monotonic()
        # First, remove entries exceeding TTL
        expired = [
            key for key, seen_at in self.recent_spoken.items()
            if now - seen_at > self.recent_spoken_ttl_sec
        ]
        for key in expired:
            del self.recent_spoken[key]

        # If dict still exceeds max size, prune oldest entries
        if len(self.recent_spoken) > self.recent_spoken_max_size:
            overage = len(self.recent_spoken) - self.recent_spoken_max_size
            oldest_keys = sorted(
                self.recent_spoken.items(),
                key=lambda x: x[1]
            )[:overage]
            for key, _ in oldest_keys:
                del self.recent_spoken[key]
