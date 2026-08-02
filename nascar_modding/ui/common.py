"""Small shared Qt presentation helpers."""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel


def page_title(title: str, subtitle: str) -> tuple[QLabel, QLabel]:
    heading = QLabel(title)
    font = QFont()
    font.setPointSize(20)
    font.setBold(True)
    heading.setFont(font)
    detail = QLabel(subtitle)
    detail.setWordWrap(True)
    detail.setObjectName('subtitle')
    return heading, detail
