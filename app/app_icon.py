from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPainter, QColor, QIcon, QPixmap, QFont


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
