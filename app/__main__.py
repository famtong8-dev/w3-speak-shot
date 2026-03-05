import sys
import queue
import threading
import subprocess
import mss
import pytesseract
from PIL import Image
from PyQt5.QtWidgets import QApplication, QWidget, QMenuBar, QAction, QInputDialog, QMessageBox
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QPainter, QPen, QColor, QCursor, QIcon, QPixmap, QFont


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
        self.stop_token = object()
        self.voice = self._detect_vietnamese_voice()
        self.base_rate_wpm = max(120, int(base_rate_wpm))
        self.rate_multiplier = max(0.1, float(rate_multiplier))
        self.config_lock = threading.Lock()
        self.proc_lock = threading.Lock()
        self.current_proc = None

    def _detect_vietnamese_voice(self):
        try:
            result = subprocess.run(
                ["say", "-v", "?"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                lower = line.lower()
                if "vi_vn" in lower or "vietnam" in lower:
                    return line.split()[0]
        except Exception:
            return None
        return None

    def run(self):
        while True:
            text = self.text_queue.get()
            if text is self.stop_token:
                self.stop_speaking()
                break
            cmd = ["say"]
            cmd.extend(["-r", str(self.get_rate_wpm())])
            if self.voice:
                cmd.extend(["-v", self.voice])
            cmd.append(text)
            try:
                with self.proc_lock:
                    self.current_proc = subprocess.Popen(cmd)
                self.current_proc.wait()
            except Exception as err:
                print(f"TTS error: {err}")
            finally:
                with self.proc_lock:
                    self.current_proc = None

    def speak(self, text):
        self.text_queue.put(text)

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
                self.text_queue.get_nowait()
            except queue.Empty:
                break

        with self.proc_lock:
            proc = self.current_proc

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception as err:
                print(f"TTS stop error: {err}")

    def stop(self):
        self.text_queue.put(self.stop_token)

class Overlay(QWidget):
    def __init__(self):
        super().__init__()

        self.setGeometry(300, 200, 600, 250)

        self.base_window_flags = Qt.FramelessWindowHint | Qt.Window
        self.stay_on_top = True
        self.apply_window_flags()

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self.margin = 12
        self.resizing = None

        # TTS
        self.last_text = ""
        self.running = False
        self.ocr_in_progress = False
        self.state_lock = threading.Lock()
        self.tts_worker = TTSWorker(base_rate_wpm=260, rate_multiplier=1.0)
        self.tts_worker.start()

        # Timer OCR
        self.timer = QTimer()
        self.timer.timeout.connect(self.capture_area)

        self.start_action = None
        self.stop_action = None

    # ================= DRAW =================
    def paintEvent(self, event):
        painter = QPainter(self)

        # Border
        pen = QPen(QColor(255, 0, 0), 3)
        painter.setPen(pen)
        painter.drawRect(self.rect())

        # Drag bar
        painter.fillRect(0, 0, self.width(), 35, QColor(255, 0, 0, 40))

    # ================= MOUSE EVENTS =================
    def mousePressEvent(self, event):
        self.start_pos = event.globalPos()
        self.start_geom = self.geometry()

        rect = self.rect()

        if event.pos().x() < self.margin:
            self.resizing = "left"
        elif event.pos().x() > rect.width() - self.margin:
            self.resizing = "right"
        elif event.pos().y() < self.margin:
            self.resizing = "top"
        elif event.pos().y() > rect.height() - self.margin:
            self.resizing = "bottom"
        else:
            self.resizing = "move"

    def mouseMoveEvent(self, event):
        rect = self.rect()

        # Cursor change
        if event.pos().x() < self.margin or event.pos().x() > rect.width() - self.margin:
            self.setCursor(QCursor(Qt.SizeHorCursor))
        elif event.pos().y() < self.margin or event.pos().y() > rect.height() - self.margin:
            self.setCursor(QCursor(Qt.SizeVerCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

        if not hasattr(self, "resizing") or not self.resizing:
            return

        delta = event.globalPos() - self.start_pos
        geom = QRect(self.start_geom)

        if self.resizing == "move":
            self.move(self.start_geom.topLeft() + delta)

        elif self.resizing == "right":
            geom.setWidth(max(200, self.start_geom.width() + delta.x()))
            self.setGeometry(geom)

        elif self.resizing == "left":
            geom.setLeft(self.start_geom.left() + delta.x())
            self.setGeometry(geom)

        elif self.resizing == "bottom":
            geom.setHeight(max(100, self.start_geom.height() + delta.y()))
            self.setGeometry(geom)

        elif self.resizing == "top":
            geom.setTop(self.start_geom.top() + delta.y())
            self.setGeometry(geom)

    def mouseReleaseEvent(self, event):
        self.resizing = None

    def closeEvent(self, event):
        self.stop_capture()
        self.tts_worker.stop()
        self.tts_worker.join(timeout=1.0)
        super().closeEvent(event)

    def apply_window_flags(self):
        flags = self.base_window_flags
        if self.stay_on_top:
            flags |= Qt.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
            if self.stay_on_top:
                self.raise_()

    def set_stay_on_top(self, enabled):
        self.stay_on_top = enabled
        self.apply_window_flags()

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
        self.running = True
        self.timer.start(2000)
        self.update_capture_actions()

    def stop_capture(self):
        if not self.running:
            return
        self.running = False
        self.timer.stop()
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
            text = pytesseract.image_to_string(img, lang="vie").strip()
            if not text:
                return

            should_speak = False
            with self.state_lock:
                if self.running and (text != self.last_text):
                    self.last_text = text
                    should_speak = True

            if should_speak:
                print("Detected:", text)
                self.tts_worker.speak(text)
        except Exception as err:
            print(f"OCR error: {err}")
        finally:
            with self.state_lock:
                self.ocr_in_progress = False


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
    stay_on_top_action = QAction("Stay On Top", menu_bar)
    stay_on_top_action.setCheckable(True)
    stay_on_top_action.setChecked(True)
    close_action = QAction("Close App", menu_bar)
    start_action.triggered.connect(overlay.start_capture)
    stop_action.triggered.connect(overlay.stop_capture)
    speed_action.triggered.connect(overlay.prompt_speed_multiplier)
    stay_on_top_action.toggled.connect(overlay.set_stay_on_top)
    close_action.triggered.connect(app.quit)
    options_menu.addAction(start_action)
    options_menu.addAction(stop_action)
    options_menu.addAction(speed_action)
    options_menu.addAction(stay_on_top_action)
    options_menu.addAction(close_action)
    overlay.bind_menu_actions(start_action, stop_action)

    # Keep a Python reference so the native menu bar is not garbage-collected.
    app.menu_bar = menu_bar

    overlay.show()
    sys.exit(app.exec_())
