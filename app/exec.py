import sys
import mss
import pytesseract
import pyttsx3
from PIL import Image
from PyQt5.QtWidgets import QApplication, QWidget, QPushButton
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QPainter, QPen, QColor, QCursor


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

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )

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

        # Button
        self.button = QPushButton("Start", self)
        self.button.move(15, 15)
        self.button.clicked.connect(self.toggle_capture)
        self.button.setStyleSheet("background: rgba(255,255,255,200);")

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

    # ================= START / STOP =================
    def toggle_capture(self):
        if not self.running:
            self.running = True
            self.button.setText("Stop")
            self.timer.start(2000)
        else:
            self.running = False
            self.button.setText("Start")
            self.timer.stop()

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
    overlay = Overlay()
    overlay.show()
    sys.exit(app.exec_())