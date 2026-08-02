"""Native pristine-backup and whole-install restore page."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.backups import BackupManager
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class BackupsPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._manager: BackupManager | None = None
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Backups & restore',
            'Create the paired pristine archive/index backups used by every editor, or restore all available pairs as one verified transaction.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        actions = QHBoxLayout()
        self.backup_button = QPushButton('Create missing pristine backups')
        self.backup_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore all pristine files...')
        self.refresh_button = QPushButton('Refresh')
        actions.addWidget(self.backup_button)
        actions.addWidget(self.restore_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ('Archive', 'File', 'Kind', 'Live bytes', 'Backup bytes', 'Backup state')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.backup_button.clicked.connect(self._create)
        self.restore_button.clicked.connect(self._restore)
        self.refresh_button.clicked.connect(self._refresh)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_enabled(self, enabled: bool):
        self.backup_button.setEnabled(enabled)
        self.restore_button.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._manager = BackupManager(installation)
        self._set_enabled(True)
        self._refresh()

    def _refresh(self):
        if not self._manager:
            return
        rows = self._manager.status()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row['archive'], row['name'], row['kind'],
                f"{row['live_size']:,}" if row['live_size'] is not None else 'missing',
                f"{row['backup_size']:,}" if row['backup_size'] is not None else '—',
                'Ready' if row['backup_valid'] else ('Invalid' if row['backup_size'] else 'Not created'),
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        ready = sum(1 for row in rows if row['backup_valid'])
        self.status.setText(f'{ready} of {len(rows)} archive/index files have valid pristine backups.')

    def _run(self, label: str, function):
        self._set_enabled(False)
        self.status.setText(label)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(self._finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _create(self):
        if self._manager:
            self._run('Creating and verifying missing backups...', self._manager.create_missing)

    def _restore(self):
        if not self._manager:
            return
        answer = QMessageBox.warning(
            self,
            'Restore every available pristine file?',
            'This replaces modified game archives and indexes with their first pristine backups. '
            'The complete set is staged and rolled back if any swap fails.',
            QMessageBox.StandardButton.RestoreDefaults | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.RestoreDefaults:
            self._run('Staging and restoring pristine files...', self._manager.restore_all)

    def _finished(self, result: dict):
        self._set_enabled(True)
        self._refresh()
        if result.get('restored'):
            self.status.setText(
                f"Restored and verified {len(result['restored'])} files; "
                f"{len(result.get('skipped') or [])} files had no backup."
            )
            if self.state.installation:
                self.state.installation_changed.emit(self.state.installation)
        else:
            self.status.setText(
                f"Created {len(result.get('created') or [])} backups; "
                f"{result.get('existing', 0)} already existed."
            )
        if result.get('failed'):
            QMessageBox.warning(self, 'Some backups failed', '\n'.join(result['failed']))

    def _failed(self, detail: str):
        self._set_enabled(True)
        self._refresh()
        self.status.setText('Backup/restore failed; any committed restore swaps were rolled back.')
        QMessageBox.critical(self, 'Backup/restore failed', detail)
