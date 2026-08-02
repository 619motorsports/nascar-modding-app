"""Native composite season-pack export, preview, and import."""

from __future__ import annotations

import json

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.season_packs import PACK_CATEGORIES, SeasonPackEditor
from .common import page_title
from .workers import FunctionWorker


class SeasonPacksPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._source = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Composite season packs',
            'Bundle proven edits from the shared editors, preview their contents, and import them under one exact rollback.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        self.export_button = QPushButton('Export current changes...')
        self.preview_button = QPushButton('Preview pack...')
        self.import_button = QPushButton('Import previewed categories...')
        self.import_button.setObjectName('primary')
        for button in (self.export_button, self.preview_button, self.import_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        categories = QHBoxLayout()
        categories.addWidget(QLabel('Import categories'))
        self.category_checks = {}
        for name in PACK_CATEGORIES:
            check = QCheckBox(name.replace('_', ' ').title())
            check.setChecked(True)
            self.category_checks[name] = check
            categories.addWidget(check)
        categories.addStretch(1)
        layout.addLayout(categories)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.export_button.clicked.connect(self._export)
        self.preview_button.clicked.connect(self._preview)
        self.import_button.clicked.connect(self._import)
        state.installation_changed.connect(lambda _installation: self._refresh_enabled())
        self._refresh_enabled()

    def _refresh_enabled(self):
        enabled = bool(self.state.installation)
        self.export_button.setEnabled(enabled)
        self.preview_button.setEnabled(enabled)
        self.import_button.setEnabled(enabled and bool(self._source))
        if enabled:
            self.status.setText('Ready. Export and preview run off the UI thread.')

    def _editor(self):
        if not self.state.installation:
            raise ValueError('select a game installation first')
        return SeasonPackEditor(self.state.installation, default_app_data_root())

    def _run(self, function, finished):
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(lambda detail: QMessageBox.critical(
            self, 'Season pack workflow failed', detail,
        ))
        self._worker = worker
        self.pool.start(worker)

    def _export(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Export composite season pack',
            f'{self.state.installation.profile.id}_season.gridpack',
            'Season packs (*.gridpack)',
        )
        if path:
            self.status.setText('Scanning shared editors for current changes...')
            self._run(lambda: self._editor().export_file(path), self._finished)

    def _preview(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Preview composite season pack', '', 'Season packs (*.gridpack *.zip)',
        )
        if path:
            self.status.setText('Validating pack paths, limits, hashes, and manifest...')
            self._run(lambda: self._editor().inspect_file(path),
                      lambda result: self._previewed(path, result))

    def _previewed(self, path, result):
        self._source = path
        available = set(result.get('categories') or [])
        for name, check in self.category_checks.items():
            check.setEnabled(name in available)
            check.setChecked(name in available)
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.status.setText('Pack validated. Choose categories, then import.')
        self._refresh_enabled()

    def _import(self):
        selected = [name for name, check in self.category_checks.items()
                    if check.isEnabled() and check.isChecked()]
        if not self._source or not selected:
            return
        if QMessageBox.question(
            self, 'Import season pack',
            'Apply the selected categories? The game must stay closed; all touched files '
            'will be restored if any category fails.',
        ) == QMessageBox.StandardButton.Yes:
            self.status.setText('Applying pack under exact aggregate rollback...')
            self._run(lambda: self._editor().import_file(self._source, selected), self._finished)

    def _finished(self, result):
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.status.setText('Season pack workflow completed and verified.')
