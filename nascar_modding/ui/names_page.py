"""Native driver-name workflow backed by the shared editing service."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.names import DriverHandleEditor, DriverNameEditor
from nascar_modding.editing.user_library import profile_config_path
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class NamesPage(QWidget):
    """Edit verified roster-name references without owning archive logic."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._editor: DriverNameEditor | None = None
        self._handle_editor: DriverHandleEditor | None = None
        self._generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Driver names',
            'Edit exact roster-name references in every installed language table. '
            'Writes are backed up, verified, and rolled back as one operation.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.apply_button = QPushButton('Apply selected name')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore selected')
        self.handle_button = QPushButton('Apply selected handle')
        self.restore_handle_button = QPushButton('Restore handle')
        self.refresh_button = QPushButton('Refresh')
        controls.addWidget(self.apply_button)
        controls.addWidget(self.restore_button)
        controls.addWidget(self.handle_button)
        controls.addWidget(self.restore_handle_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ('Car', 'Original name', 'Current name (editable)',
             'Original handle', 'Current handle (editable)', 'Mapping')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)

        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.handle_button.clicked.connect(self._apply_handle)
        self.restore_handle_button.clicked.connect(self._restore_handle)
        self.refresh_button.clicked.connect(self.refresh)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_busy(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_busy(self, busy: bool):
        ready = not busy and self._editor is not None
        self.apply_button.setEnabled(ready)
        self.restore_button.setEnabled(ready)
        self.handle_button.setEnabled(ready and self._handle_editor is not None)
        self.restore_handle_button.setEnabled(ready and self._handle_editor is not None)
        self.refresh_button.setEnabled(ready)

    def _installation_changed(self, installation: GameInstallation):
        self._generation += 1
        self._editor = DriverNameEditor(installation)
        config = profile_config_path(default_app_data_root(), installation.profile.id)
        self._handle_editor = DriverHandleEditor(installation, config)
        self.table.setRowCount(0)
        self.refresh()

    def refresh(self):
        if not self._editor:
            return
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Resolving verified name references in the installed language tables...')
        worker = FunctionWorker(lambda: {
            'names': self._editor.drivers(),
            'handles': self._handle_editor.handles() if self._handle_editor else [],
        })
        worker.signals.finished.connect(lambda rows, current=token: self._loaded(current, rows))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    @staticmethod
    def _read_only(item: QTableWidgetItem) -> QTableWidgetItem:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _loaded(self, token: int, result: dict):
        if token != self._generation:
            return
        rows = result['names']
        handles = {int(row['driver_uid']): row for row in result.get('handles', [])}
        self.table.setRowCount(len(rows))
        available = 0
        for row_index, row in enumerate(rows):
            number = self._read_only(QTableWidgetItem(row['number']))
            number.setData(Qt.ItemDataRole.UserRole, row['driver_uid'])
            original = self._read_only(QTableWidgetItem(row['original']))
            current = QTableWidgetItem(row['current'])
            handle = handles.get(int(row['driver_uid']))
            original_handle = self._read_only(QTableWidgetItem(
                '@' + handle['original'] if handle else '-'
            ))
            current_handle = QTableWidgetItem(handle['current'] if handle else '')
            if not handle or not handle['available']:
                current_handle.setFlags(current_handle.flags() & ~Qt.ItemFlag.ItemIsEditable)
            mapping = self._read_only(QTableWidgetItem(
                f"Verified ({len(row['targets'])} tables)" if row['available'] else row['reason']
            ))
            if row['available']:
                available += 1
            else:
                current.setFlags(current.flags() & ~Qt.ItemFlag.ItemIsEditable)
            for column, item in enumerate((
                number, original, current, original_handle, current_handle, mapping,
            )):
                self.table.setItem(row_index, column, item)
        if rows:
            self.table.selectRow(0)
        profile_name = self._editor.installation.profile.name if self._editor else 'Game'
        self.status.setText(
            f'{profile_name}: {available}/{len(rows)} roster names have exact verified mappings.'
        )
        self._set_busy(False)

    def _selection(self):
        row = self.table.currentRow()
        uid_item = self.table.item(row, 0)
        name_item, handle_item = self.table.item(row, 2), self.table.item(row, 4)
        if not uid_item or not name_item:
            return None
        return (int(uid_item.data(Qt.ItemDataRole.UserRole)), name_item.text().strip(),
                handle_item.text().strip() if handle_item else '')

    def _apply(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        driver_uid, name, _handle = selected
        if QMessageBox.question(
            self,
            'Apply driver name',
            f'Write “{name}” to every verified language table for this driver?',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.rename, driver_uid, name)

    def _restore(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        driver_uid, _name, _handle = selected
        if QMessageBox.question(
            self,
            'Restore driver name',
            'Restore this driver’s original name from the pristine archive backup?',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.restore, driver_uid)

    def _apply_handle(self):
        selected = self._selection()
        if not selected or not self._handle_editor:
            return
        driver_uid, _name, handle = selected
        if QMessageBox.question(
            self, 'Apply driver handle', f'Write the driver-card handle "@{handle.lstrip("@")}"?',
        ) == QMessageBox.StandardButton.Yes:
            self._run_handle_write(self._handle_editor.rename, driver_uid, handle)

    def _restore_handle(self):
        selected = self._selection()
        if not selected or not self._handle_editor:
            return
        driver_uid, _name, _handle = selected
        if QMessageBox.question(
            self, 'Restore driver handle', "Restore this driver's original card handle?",
        ) == QMessageBox.StandardButton.Yes:
            self._run_handle_write(self._handle_editor.restore, driver_uid)

    def _run_handle_write(self, function, *args):
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Updating and verifying the mapped driver-card handle...')
        worker = FunctionWorker(function, *args)
        worker.signals.finished.connect(lambda result, current=token: self._handle_written(current, result))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    def _handle_written(self, token: int, result: dict):
        if token != self._generation:
            return
        self.status.setText(f"Installed and verified driver handle @{result['current']}.")
        self.refresh()

    def _run_write(self, function, *args):
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Updating and verifying all mapped language tables...')
        worker = FunctionWorker(function, *args)
        worker.signals.finished.connect(lambda result, current=token: self._written(current, result))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    def _written(self, token: int, result: dict):
        if token != self._generation:
            return
        self.status.setText(
            f"Installed and verified “{result['current']}” in {result['tables']} language tables."
        )
        self.refresh()

    def _failed(self, token: int, detail: str):
        if token != self._generation:
            return
        self._set_busy(False)
        self.status.setText('Name operation failed; no unverified change was accepted.')
        QMessageBox.critical(self, 'Driver name operation failed', detail)
