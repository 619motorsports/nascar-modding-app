"""Native installation checkup and diagnostic export."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.verification.support import SupportReporter
from .common import page_title


class SupportPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.root = default_app_data_root()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Checkup & support',
            'Validate app components, archive bounds, and backup coverage; export reports without game data.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        refresh = QPushButton('Run checks')
        refresh.setObjectName('primary')
        report = QPushButton('Save support report...')
        diagnostics = QPushButton('Save diagnostics ZIP...')
        actions.addWidget(refresh)
        actions.addWidget(report)
        actions.addWidget(diagnostics)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(('Status', 'Check', 'Detail'))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setObjectName('status')
        layout.addWidget(self.status)
        refresh.clicked.connect(self.refresh)
        report.clicked.connect(self._report)
        diagnostics.clicked.connect(self._diagnostics)
        state.installation_changed.connect(lambda _installation: self.refresh())
        self.refresh()

    def _reporter(self):
        return SupportReporter(self.state.installation, self.root, self.root, '1.0.2', 'Public release')

    def refresh(self):
        rows, summary = self._reporter().checks()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate((row['status'].upper(), row['name'], row['detail'])):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.status.setText(
            f"{summary['pass_count']} pass, {summary['warn_count']} warning, {summary['fail_count']} fail."
        )

    def _save(self, title, suggested, file_filter, payload):
        path, _filter = QFileDialog.getSaveFileName(self, title, suggested, file_filter)
        if path:
            try:
                Path(path).write_bytes(payload)
                self.status.setText(f'Saved {path}.')
            except OSError as exc:
                QMessageBox.critical(self, 'Save failed', str(exc))

    def _report(self):
        self._save('Save support report', 'nascar_support.txt', 'Text (*.txt)', self._reporter().report_bytes())

    def _diagnostics(self):
        self._save('Save diagnostics', 'nascar_diagnostics.zip', 'ZIP (*.zip)', self._reporter().diagnostics_bytes())
