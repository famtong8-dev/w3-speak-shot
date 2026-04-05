import logging
import os
import sys


from dotenv import load_dotenv

load_dotenv()

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QActionGroup, QApplication, QMenuBar, QAction

# Configure logging - prints to console by default
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)

try:
    from .overlay import Overlay
except ImportError:
    from overlay import Overlay


def _load_app_icon():
    store_dir = os.path.join(os.path.dirname(__file__), "store")
    for name in ("logo.icns", "logo.ico"):
        path = os.path.join(store_dir, name)
        if os.path.exists(path):
            return QIcon(path)
    # Fallback: generate icon programmatically (Linux or missing file)
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(220, 20, 60))
    painter.drawRoundedRect(16, 16, 224, 224, 48, 48)
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(QFont("Arial", 128, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "S2")
    painter.end()
    return QIcon(pixmap)


def main():
    app = QApplication(sys.argv)
    app_icon = _load_app_icon()
    app.setWindowIcon(app_icon)

    overlay = Overlay()
    overlay.setWindowIcon(app_icon)

    menu_bar = QMenuBar()
    menu_bar.setNativeMenuBar(True)
    options_menu = menu_bar.addMenu("Options")
    start_action = QAction("Start", menu_bar)
    stop_action = QAction("Stop", menu_bar)
    close_action = QAction("Close App", menu_bar)
    start_action.triggered.connect(overlay.start_capture)
    stop_action.triggered.connect(overlay.stop_capture)
    close_action.triggered.connect(app.quit)
    options_menu.addAction(start_action)
    options_menu.addAction(stop_action)

    speed_menu = options_menu.addMenu("Speed")
    speed_group = QActionGroup(menu_bar)
    speed_group.setExclusive(True)
    speed_levels = [("0.75x", 0.75), ("1x", 1.0), ("1.25x", 1.25), ("1.5x", 1.5), ("1.75x", 1.75), ("2x", 2.0)]
    default_multiplier = overlay.tts_worker.get_rate_multiplier()
    speed_actions = []
    for label, value in speed_levels:
        action = QAction(label, menu_bar)
        action.setCheckable(True)
        action.setChecked(value == default_multiplier)
        action.triggered.connect(lambda checked, v=value: overlay.tts_worker.set_base_rate_multiplier(v))
        speed_group.addAction(action)
        speed_menu.addAction(action)
        speed_actions.append((value, action))
    overlay.bind_speed_actions(speed_actions)

    auto_speed_action = QAction("Auto Speed", menu_bar)
    auto_speed_action.setCheckable(True)
    auto_speed_action.setChecked(overlay.tts_worker.auto_speed)
    auto_speed_action.triggered.connect(lambda checked: setattr(overlay.tts_worker, "auto_speed", checked))
    options_menu.addAction(auto_speed_action)

    options_menu.addAction(close_action)
    overlay.bind_menu_actions(start_action, stop_action)

    # Keep Python references so menu objects are not garbage-collected.
    app.speed_group = speed_group

    # Keep a Python reference so the native menu bar is not garbage-collected.
    app.menu_bar = menu_bar

    overlay.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
