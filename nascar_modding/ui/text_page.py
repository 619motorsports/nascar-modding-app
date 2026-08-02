"""Native indexed language-table editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from nascar_modding.editing.text_tables import TextTableEditor
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class TextPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._editor: TextTableEditor | None = None
        self._rows = []
        self._generation = 0
        self._export_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Interface text',
            'Edit one exact indexed string in the installed TEXT*.LDA tables. '
            'Format tokens are protected and longer text uses verified archive repointing.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        selectors = QHBoxLayout()
        self.file_combo = QComboBox()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText('Filter current table')
        self.internal_check = QCheckBox('Show technical/internal strings')
        selectors.addWidget(QLabel('Table'))
        selectors.addWidget(self.file_combo)
        selectors.addWidget(self.filter_edit, 1)
        selectors.addWidget(self.internal_check)
        layout.addLayout(selectors)

        actions = QHBoxLayout()
        self.apply_button = QPushButton('Apply selected text')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore selected')
        self.restore_file_button = QPushButton('Restore table')
        self.export_button = QPushButton('Export CSV...')
        self.import_button = QPushButton('Import CSV...')
        self.refresh_button = QPushButton('Refresh')
        for button in (
            self.apply_button, self.restore_button, self.restore_file_button,
            self.export_button, self.import_button, self.refresh_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ('Index', 'Category', 'Screen', 'Stock', 'Current (editable)', 'Bytes', 'Tokens')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)

        self.file_combo.currentIndexChanged.connect(self.refresh)
        self.filter_edit.textChanged.connect(self._render)
        self.internal_check.toggled.connect(self._render)
        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.restore_file_button.clicked.connect(self._restore_file)
        self.export_button.clicked.connect(self._export)
        self.import_button.clicked.connect(self._import)
        self.refresh_button.clicked.connect(self.refresh)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_busy(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_busy(self, busy: bool):
        ready = not busy and self._editor is not None and self.file_combo.count() > 0
        for button in (
            self.apply_button, self.restore_button, self.restore_file_button,
            self.export_button, self.import_button, self.refresh_button,
        ):
            button.setEnabled(ready)
        self.file_combo.setEnabled(not busy and self._editor is not None)

    def _installation_changed(self, installation: GameInstallation):
        self._generation += 1
        self._editor = TextTableEditor(installation)
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        try:
            for row in self._editor.files():
                label = row['name'] + ('' if row['has_stock'] else ' (no backup yet)')
                self.file_combo.addItem(label, row['name'])
        except Exception as exc:
            self.status.setText(f'Could not enumerate language tables: {exc}')
        finally:
            self.file_combo.blockSignals(False)
        self.refresh()

    def refresh(self):
        if not self._editor or not self.file_combo.currentData():
            return
        self._generation += 1
        token = self._generation
        file_name = self.file_combo.currentData()
        self._set_busy(True)
        self.status.setText(f'Reading {file_name}...')
        worker = FunctionWorker(self._editor.entries, file_name)
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
        self._rows = rows
        self._render()
        modified = sum(row['modified'] for row in rows)
        user_facing = sum(row['user_facing'] for row in rows)
        self.status.setText(
            f"{rows[0]['file'] if rows else self.file_combo.currentData()}: "
            f'{len(rows):,} strings, {user_facing:,} user-facing, {modified:,} modified.'
        )
        self._set_busy(False)

    def _render(self):
        query = self.filter_edit.text().strip().casefold()
        include_internal = self.internal_check.isChecked()
        rows = [
            row for row in self._rows
            if (include_internal or row['user_facing'])
            and (
                not query
                or query in ' '.join((
                    str(row['index']), row['category'], row['screen'], row['current'],
                    row['stock'] or '',
                )).casefold()
            )
        ]
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            index_item = self._read_only(row['index'])
            index_item.setData(Qt.ItemDataRole.UserRole, (row['file'], row['index']))
            values = (
                index_item,
                self._read_only(row['category']),
                self._read_only(row['screen']),
                self._read_only(row['stock'] if row['stock'] is not None else '—'),
                QTableWidgetItem(row['current']),
                self._read_only(row['current_length']),
                self._read_only(' | '.join(row['tokens'])),
            )
            for column, item in enumerate(values):
                self.table.setItem(row_index, column, item)
        if rows:
            self.table.selectRow(0)

    def _selection(self):
        row = self.table.currentRow()
        index_item = self.table.item(row, 0)
        current_item = self.table.item(row, 4)
        if not index_item or not current_item:
            return None
        file_name, index = index_item.data(Qt.ItemDataRole.UserRole)
        return file_name, int(index), current_item.text()

    def _apply(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        file_name, index, value = selected
        try:
            plan = self._editor.plan(file_name, index, value)
        except Exception as exc:
            QMessageBox.critical(self, 'Text validation failed', str(exc))
            return
        if not plan['changed']:
            self.status.setText('The selected text already matches the live value.')
            return
        if QMessageBox.question(
            self, 'Apply interface text',
            f"Replace string #{index} in {file_name}? File size change: {plan['size_delta']:+,} bytes.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.apply, file_name, index, value)

    def _restore(self):
        selected = self._selection()
        if not selected or not self._editor:
            return
        file_name, index, _value = selected
        self._run_write(self._editor.restore, file_name, index)

    def _restore_file(self):
        if not self._editor or not self.file_combo.currentData():
            return
        file_name = self.file_combo.currentData()
        if QMessageBox.question(
            self, 'Restore language table', f'Restore every string in {file_name} from its pristine backup?'
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.restore_file, file_name)

    def _run_write(self, function, *args):
        self._generation += 1
        token = self._generation
        self._set_busy(True)
        self.status.setText('Installing and verifying language-table changes...')
        worker = FunctionWorker(function, *args)
        worker.signals.finished.connect(lambda result, current=token: self._written(current, result))
        worker.signals.failed.connect(lambda detail, current=token: self._failed(current, detail))
        self._worker = worker
        self.pool.start(worker)

    def _written(self, token: int, _result: dict):
        if token != self._generation:
            return
        self.refresh()

    def _export(self):
        if not self._editor:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Export interface text', 'nascar_interface_text.csv', 'CSV (*.csv)'
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self._editor.export_csv_bytes())
            self.status.setText(f'Exported interface text to {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Text export failed', str(exc))

    def _import(self):
        if not self._editor:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Import interface text', '', 'CSV (*.csv)'
        )
        if not path:
            return
        try:
            changes = self._editor.preview_csv(Path(path).read_bytes())
            invalid = [change for change in changes if not change['valid']]
            valid = [change for change in changes if change['valid']]
            if invalid:
                raise ValueError(
                    f'{len(invalid)} CSV rows are invalid; first error: {invalid[0]["error"]}'
                )
            if not valid:
                self.status.setText('The CSV contains no changes from the live tables.')
                return
        except Exception as exc:
            QMessageBox.critical(self, 'Text import validation failed', str(exc))
            return
        if QMessageBox.question(
            self, 'Import interface text',
            f'Apply {len(valid):,} validated string changes as one archive transaction?'
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_write(self._editor.apply_batch, valid)

    def _failed(self, token: int, detail: str):
        if token != self._generation:
            return
        self._set_busy(False)
        self.status.setText('Text operation failed; no unverified change was accepted.')
        QMessageBox.critical(self, 'Interface text operation failed', detail)
