import sys
import mss
import pytesseract
import pyttsx3
from PIL import Image
from PyQt5.QtWidgets import QApplication, QWidget, QMenuBar, QAction
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


def speak(text):
    engine = pyttsx3.init(driverName='nsss')
    voices = engine.getProperty('voices')
    for v in voices:
        if "vi" in v.id.lower():
            engine.setProperty('voice', v.id)
            break
    engine.say(text)
    engine.runAndWait()
    engine.stop()

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
        self.update_capture_actions()

    def update_capture_actions(self):
        if self.start_action is None or self.stop_action is None:
            return
        self.start_action.setEnabled(not self.running)
        self.stop_action.setEnabled(self.running)

    # ================= OCR CAPTURE =================
    def capture_area(self):
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

        text = pytesseract.image_to_string(img, lang="vie").strip()

        if text and (text != self.last_text):
            print("Detected:", text)
            speak(text)
            self.last_text = text


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
    stay_on_top_action = QAction("Stay On Top", menu_bar)
    stay_on_top_action.setCheckable(True)
    stay_on_top_action.setChecked(True)
    close_action = QAction("Close App", menu_bar)
    start_action.triggered.connect(overlay.start_capture)
    stop_action.triggered.connect(overlay.stop_capture)
    stay_on_top_action.toggled.connect(overlay.set_stay_on_top)
    close_action.triggered.connect(app.quit)
    options_menu.addAction(start_action)
    options_menu.addAction(stop_action)
    options_menu.addAction(stay_on_top_action)
    options_menu.addAction(close_action)
    overlay.bind_menu_actions(start_action, stop_action)

    # Keep a Python reference so the native menu bar is not garbage-collected.
    app.menu_bar = menu_bar

    overlay.show()
    sys.exit(app.exec_())
