"""Native app-state backup and restore page."""

from __future__ import annotations

import datetime
from pathlib import Path
import zipfile

from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import AppDataManager, default_app_data_root
from .common import page_title


class AppDataPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.manager = AppDataManager(default_app_data_root(), '1.0.2')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'App data transfer',
            'Move presets, saved paint art, editor history, and per-game settings. Game archives are never included.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        export_button = QPushButton('Export app data...')
        export_button.setObjectName('primary')
        import_button = QPushButton('Import app data...')
        refresh_button = QPushButton('Refresh inventory')
        actions.addWidget(export_button)
        actions.addWidget(import_button)
        actions.addWidget(refresh_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)
        export_button.clicked.connect(self._export)
        import_button.clicked.connect(self._import)
        refresh_button.clicked.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        rows = self.manager.members()
        self.details.setPlainText('\n'.join(f'{name}  ({path.stat().st_size:,} bytes)' for name, path in rows))
        self.status.setText(f'{len(rows)} app-owned files are available for transfer. No game files are listed.')

    def _export(self):
        stamp = datetime.datetime.now().strftime('%Y%m%d')
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Export app data', f'nascar_app_data_{stamp}.zip', 'ZIP archives (*.zip)',
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self.manager.export_bytes())
            self.status.setText(f'App data exported to {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def _import(self):
        path, _filter = QFileDialog.getOpenFileName(self, 'Import app data', '', 'ZIP archives (*.zip)')
        if not path:
            return
        answer = QMessageBox.question(
            self, 'Import app data',
            'Import recognized app-owned files? Existing app data will first be saved to a recovery ZIP.',
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.manager.import_bytes(Path(path).read_bytes())
            self._refresh()
            self.status.setText(
                f"Restored {result['restored']} files. Restart the app to reload them. "
                + (f"Previous data: {result['recovery_path']}" if result['recovery_path'] else '')
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(self, 'Import failed', str(exc))
