"""Native failure-focused whole-install audit and repair."""

from __future__ import annotations

import json

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from nascar_modding.core.files import atomic_write_json
from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.full_repair import FullRepairEditor
from .common import page_title
from .workers import FunctionWorker


class FullRepairPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Failure-focused full repair',
            'Audit app-created runtime dependencies, then reconstruct only proven assets under an exact whole-file rollback.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        self.check_button = QPushButton('Run failure scan')
        self.apply_button = QPushButton('Apply repair...')
        self.apply_button.setObjectName('primary')
        self.report_button = QPushButton('Export last report...')
        for button in (self.check_button, self.apply_button, self.report_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.status = QLabel('Select NASCAR 15 on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.check_button.clicked.connect(self._check)
        self.apply_button.clicked.connect(self._apply)
        self.report_button.clicked.connect(self._export_report)
        state.installation_changed.connect(lambda _installation: self._enabled())
        self._enabled()

    def _enabled(self):
        enabled = bool(self.state.installation and
                       self.state.installation.profile.id == 'nascar15')
        for button in (self.check_button, self.apply_button, self.report_button):
            button.setEnabled(enabled)
        if enabled:
            self.status.setText('Ready. Run the read-only failure scan first.')

    def _editor(self):
        return FullRepairEditor(
            self.state.installation, default_app_data_root(), app_version='native',
        )

    def _run(self, function, finished):
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(lambda detail: QMessageBox.critical(
            self, 'Full repair failed', detail,
        ))
        self._worker = worker
        self.pool.start(worker)

    def _check(self):
        self.status.setText('Scanning runtime dependencies without changing files...')
        self._run(self._editor().check, self._finished)

    def _apply(self):
        if QMessageBox.question(
            self, 'Apply full repair',
            'Run the failure-focused repair now? NASCAR 15 must remain closed. '
            'Every touched archive, index, and state file is copied for exact rollback first.',
        ) == QMessageBox.StandardButton.Yes:
            self.status.setText('Repairing and verifying; keep NASCAR 15 closed...')
            self._run(self._editor().apply, self._finished)

    def _finished(self, result):
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.status.setText('Completed successfully.' if result.get('ok') else 'Scan completed with issues.')

    def _export_report(self):
        try:
            report = self._editor().report()
            path, _filter = QFileDialog.getSaveFileName(
                self, 'Export full-repair report', 'nascar15_full_repair.json', 'JSON (*.json)',
            )
            if path:
                atomic_write_json(path, report, indent=2)
                self.status.setText(f'Saved {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Report unavailable', str(exc))
