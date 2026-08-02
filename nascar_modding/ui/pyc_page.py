"""Native exact-field editor for mapped game-database PYC records."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.pyc_records import PycRecordEditor, WORKFLOWS
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class PycPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._installation: GameInstallation | None = None
        self._rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Game database records',
            'Edit one proven scalar field on one Python-2 database record. Every preview performs a class-wide diff and rejects shared-constant collateral changes.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.workflow = QComboBox()
        self.workflow.addItem('Race laps', 'race_laps')
        self.workflow.addItem('AI track behavior', 'ai_track')
        self.workflow.addItem('AI global behavior', 'ai_global')
        self.workflow.addItem('World pace / conditions', 'world_pace')
        self.field = QComboBox()
        self.query = QLineEdit()
        self.query.setPlaceholderText('Filter UID or value')
        self.reload_button = QPushButton('Reload')
        controls.addWidget(self.workflow)
        controls.addWidget(self.field, 1)
        controls.addWidget(self.query)
        controls.addWidget(self.reload_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(('UID', 'Current value'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)

        editor_row = QWidget()
        form = QFormLayout(editor_row)
        self.value = QLineEdit()
        self.value.setPlaceholderText('Select a record')
        actions = QHBoxLayout()
        self.preview_button = QPushButton('Preview exact-field edit')
        self.apply_button = QPushButton('Apply exact-field edit')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore database file')
        actions.addWidget(self.preview_button)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.restore_button)
        actions.addStretch(1)
        form.addRow('New value', self.value)
        form.addRow('', actions)
        layout.addWidget(editor_row)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.workflow.currentIndexChanged.connect(self._workflow_changed)
        self.field.currentIndexChanged.connect(self._render)
        self.query.textChanged.connect(self._render)
        self.table.itemSelectionChanged.connect(self._selected)
        self.reload_button.clicked.connect(self._reload)
        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.state.installation_changed.connect(self._installation_changed)
        self._workflow_changed()
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _editor(self) -> PycRecordEditor:
        if not self._installation:
            raise ValueError('Select a game installation first.')
        return PycRecordEditor(self._installation)

    def _set_enabled(self, enabled: bool):
        for widget in (self.reload_button, self.preview_button, self.apply_button, self.restore_button):
            widget.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._installation = installation
        self._reload()

    def _workflow_changed(self):
        workflow = self.workflow.currentData()
        self.field.clear()
        if workflow:
            _filename, _class_name, fields = WORKFLOWS[workflow]
            self.field.addItems(fields)
        if self._installation:
            self._reload()

    def _run(self, label, function, finished):
        self._set_enabled(False)
        self.status.setText(label)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _reload(self):
        if not self._installation or not self.workflow.currentData():
            return
        workflow = self.workflow.currentData()
        self._run('Mapping live Python-2 database records...', lambda: self._editor().records(workflow), self._loaded)

    def _loaded(self, rows):
        self._rows = rows
        self._render()
        self._set_enabled(True)
        self.status.setText(f'{len(rows):,} records mapped. Nested/non-scalar fields remain read-only.')

    def _render(self):
        field = self.field.currentText()
        query = self.query.text().strip().casefold()
        rows = [row for row in self._rows if not query or query in f"{row.get('uid')} {row.get(field)}".casefold()]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            uid = QTableWidgetItem(str(row.get('uid')))
            uid.setData(Qt.ItemDataRole.UserRole, row)
            uid.setFlags(uid.flags() & ~Qt.ItemFlag.ItemIsEditable)
            value = QTableWidgetItem(str(row.get(field)))
            value.setFlags(value.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(index, 0, uid)
            self.table.setItem(index, 1, value)
        if rows:
            self.table.selectRow(0)

    def _selected(self):
        item = self.table.item(self.table.currentRow(), 0)
        if item:
            row = item.data(Qt.ItemDataRole.UserRole)
            self.value.setText(str(row.get(self.field.currentText())))

    def _change(self):
        item = self.table.item(self.table.currentRow(), 0)
        if not item:
            raise ValueError('Select one database record first.')
        row = item.data(Qt.ItemDataRole.UserRole)
        return {'uid': row['uid'], 'field': self.field.currentText(), 'value': self.value.text().strip()}

    def _preview(self):
        try:
            change = self._change()
        except Exception as exc:
            QMessageBox.information(self, 'No record selected', str(exc))
            return
        workflow = self.workflow.currentData()
        self._run('Rebuilding and class-wide diffing a disposable PYC copy...', lambda: self._editor().preview(workflow, [change]), self._previewed)

    def _previewed(self, result):
        result.pop('_payload', None)
        self._set_enabled(True)
        change = result['changes'][0]
        self.status.setText(
            f"Preview passed: UID {change['uid']} / {change['field']} changes "
            f"from {change['old']} to {change['new']}; no other record changes."
        )

    def _apply(self):
        try:
            change = self._change()
        except Exception as exc:
            QMessageBox.information(self, 'No record selected', str(exc))
            return
        if QMessageBox.question(
            self, 'Apply database edit',
            f"Apply UID {change['uid']} / {change['field']} = {change['value']}? A pristine backup will be kept.",
        ) != QMessageBox.StandardButton.Yes:
            return
        workflow = self.workflow.currentData()
        self._run('Installing and verifying exact PYC edit...', lambda: self._editor().apply(workflow, [change]), self._applied)

    def _applied(self, result):
        self.status.setText(f"Applied and verified {result['affected_count']} exact PYC field.")
        self._reload()

    def _restore(self):
        if QMessageBox.question(
            self, 'Restore database file', 'Restore this workflow’s PYC file from its pristine paired backup?',
        ) != QMessageBox.StandardButton.Yes:
            return
        workflow = self.workflow.currentData()
        self._run('Restoring database file...', lambda: self._editor().restore(workflow), self._restored)

    def _restored(self, result):
        self.status.setText(f"Restored and verified {result['name']}.")
        self._reload()

    def _failed(self, detail: str):
        self._set_enabled(bool(self._installation))
        self.status.setText('Database operation failed; no unverified edit was accepted.')
        QMessageBox.critical(self, 'Game database operation failed', detail)
