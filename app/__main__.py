import sys

from PyQt5.QtWidgets import QApplication, QMenuBar, QAction

try:
    from .app_icon import create_app_icon
    from .overlay import Overlay
except ImportError:
    from app_icon import create_app_icon
    from overlay import Overlay


def main():
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
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
