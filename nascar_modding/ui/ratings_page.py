"""Native AI rating editor using the shared transactional backend."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.ratings import RATING_FIELDS, RATING_LABELS, RatingsEditor
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class RatingsPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._editor: RatingsEditor | None = None
        self._generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Driver AI ratings',
            'Edit the mapped Python-2 AI profile constants on the game’s native 0–100 scale. '
            'All fields for a driver are installed and verified together.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.apply_button = QPushButton('Apply selected ratings')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore selected')
        self.refresh_button = QPushButton('Refresh')
        controls.addWidget(self.apply_button)
        controls.addWidget(self.restore_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 2 + len(RATING_FIELDS))
        self.table.setHorizontalHeaderLabels(('Car', 'Driver', *RATING_LABELS))
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
        self.refresh_button.clicked.connect(self.refresh)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_busy(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_busy(self, busy: bool):
        ready = not busy and self._editor is not None
        self.apply_button.setEnabled(ready)
        self.restore_button.setEnabled(ready)
        self.refresh_button.setEnabled(ready)

    def _installation_changed(self, installation: GameInstallation):
        self._generation += 1
        self._editor = RatingsEditor(installation)
        self.table.setRowCount(0)
        self.refresh()

    def refresh(self):
        if not self._editor:
            return
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Reading mapped driver AI constants...')
        worker = FunctionWorker(self._editor.ratings)
        worker.signals.finished.connect(lambda rows, current=token: self._loaded(current, rows))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    @staticmethod
    def _read_only(value) -> QTableWidgetItem:
        item = QTableWidgetItem(str(value))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _loaded(self, token: int, rows: list[dict]):
        if token != self._generation:
            return
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            number = self._read_only(row['number'])
            number.setData(Qt.ItemDataRole.UserRole, row['profile_id'])
            self.table.setItem(row_index, 0, number)
            self.table.setItem(row_index, 1, self._read_only(row['label']))
            for column, field in enumerate(RATING_FIELDS, start=2):
                self.table.setItem(row_index, column, QTableWidgetItem(str(row['stats'][field])))
        if rows:
            self.table.selectRow(0)
        profile_name = self._editor.installation.profile.name if self._editor else 'Game'
        self.status.setText(f'{profile_name}: {len(rows)} verified driver AI profiles.')
        self._set_busy(False)

    def _selection(self):
        row = self.table.currentRow()
        profile_item = self.table.item(row, 0)
        if not profile_item:
            return None
        values = {}
        for column, field in enumerate(RATING_FIELDS, start=2):
            item = self.table.item(row, column)
            if not item:
                return None
            values[field] = item.text().strip()
        return int(profile_item.data(Qt.ItemDataRole.UserRole)), values

    def _apply(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        profile_id, values = selected
        if QMessageBox.question(
            self,
            'Apply AI ratings',
            'Apply and verify all seven displayed ratings for this driver?',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.set_ratings, profile_id, values)

    def _restore(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        profile_id, _values = selected
        if QMessageBox.question(
            self,
            'Restore AI ratings',
            'Restore all seven mapped ratings for this driver to the verified stock values?',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.restore, profile_id)

    def _run_write(self, function, *args):
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Installing and verifying AI ratings...')
        worker = FunctionWorker(function, *args)
        worker.signals.finished.connect(lambda result, current=token: self._written(current, result))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    def _written(self, token: int, result: dict):
        if token != self._generation:
            return
        self.status.setText(
            f"Verified {len(result['applied'])} ratings for AI profile {result['profile_id']}."
        )
        self.refresh()

    def _failed(self, token: int, detail: str):
        if token != self._generation:
            return
        self._set_busy(False)
        self.status.setText('Rating operation failed; no unverified change was accepted.')
        QMessageBox.critical(self, 'AI rating operation failed', detail)
