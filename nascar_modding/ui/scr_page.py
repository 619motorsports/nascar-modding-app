"""Native racing-controls editor backed by the shared SCR service."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.scr import CATEGORY_ORDER, ScrEditor
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class ScrPage(QWidget):
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
            'Racing controls',
            'Edit numeric Player/AI SCR physics values. Batches are rebuilt, diff-checked, backed up, installed transactionally, and verified.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        filters = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText('Filter track, key, category, or context')
        self.role = QComboBox()
        self.role.addItem('Player and AI', 'all')
        self.role.addItem('Player', 'player')
        self.role.addItem('AI', 'ai')
        self.category = QComboBox()
        self.category.addItem('All categories', '')
        for category in CATEGORY_ORDER:
            self.category.addItem(category, category)
        self.recommended = QComboBox()
        self.recommended.addItem('Recommended + raw', False)
        self.recommended.addItem('Recommended only', True)
        self.reload_button = QPushButton('Reload')
        filters.addWidget(self.query, 1)
        filters.addWidget(self.role)
        filters.addWidget(self.category)
        filters.addWidget(self.recommended)
        filters.addWidget(self.reload_button)
        layout.addLayout(filters)

        actions = QHBoxLayout()
        self.preview_button = QPushButton('Preview pending changes')
        self.apply_button = QPushButton('Apply pending changes')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore selected SCR')
        actions.addWidget(self.preview_button)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.restore_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ('Track', 'Role', 'Category', 'Context', 'Key', '#', 'Value', 'Stock')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.query.textChanged.connect(self._render)
        self.role.currentIndexChanged.connect(self._render)
        self.category.currentIndexChanged.connect(self._render)
        self.recommended.currentIndexChanged.connect(self._render)
        self.reload_button.clicked.connect(self._reload)
        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _editor(self) -> ScrEditor:
        if not self._installation:
            raise ValueError('Select a game installation first.')
        return ScrEditor(self._installation)

    def _set_enabled(self, enabled: bool):
        for widget in (self.reload_button, self.preview_button, self.apply_button, self.restore_button):
            widget.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._installation = installation
        self._reload()

    def _run(self, label: str, function, finished):
        self._set_enabled(False)
        self.status.setText(label)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _reload(self):
        if not self._installation:
            return
        self._run(
            'Scanning indexed Player/AI SCR containers...',
            lambda: self._editor().inventory(include_stock=True), self._loaded,
        )

    def _loaded(self, rows: list[dict]):
        self._rows = rows
        self._render()
        self._set_enabled(True)
        self.status.setText(
            f'{len(rows):,} numeric settings loaded from '
            f'{len({(row["archive"], row["name"]) for row in rows})} SCR containers. '
            'Only the Value column is editable.'
        )

    def _visible_rows(self) -> list[dict]:
        query = self.query.text().strip().casefold()
        role = self.role.currentData()
        category = self.category.currentData()
        recommended = bool(self.recommended.currentData())
        return [row for row in self._rows if (
            (role == 'all' or row['role'] == role)
            and (not category or row['category'] == category)
            and (not recommended or row['recommended'])
            and (not query or query in ' '.join((
                row['track'], row['role'], row['category'], row['context'],
                row['key'], row['value'], row['path'],
            )).casefold())
        )]

    def _render(self):
        rows = self._visible_rows()
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                row['track'], row['role'].upper(), row['category'], row['context'],
                row['key'], row['occurrence'], row['value'],
                row['stock'] if row['stock'] is not None else '—',
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                if column != 6:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)

    def _pending(self) -> list[dict]:
        changes = []
        for index in range(self.table.rowCount()):
            row = self.table.item(index, 0).data(Qt.ItemDataRole.UserRole)
            value = self.table.item(index, 6).text().strip()
            if value != row['value']:
                changes.append({
                    'archive': row['archive'], 'name': row['name'], 'key': row['key'],
                    'occurrence': row['occurrence'], 'value': value,
                })
        return changes

    def _preview(self):
        changes = self._pending()
        if not changes:
            QMessageBox.information(self, 'No changes', 'Edit one or more Value cells first.')
            return
        self._run('Building and diff-checking the SCR preview...', lambda: self._editor().preview(changes), self._previewed)

    def _previewed(self, result: dict):
        self._set_enabled(True)
        self.status.setText(
            f"Preview passed: {result['affected_count']} values in {result['file_count']} files; "
            f"write method {result['method']}. No game file was changed."
        )

    def _apply(self):
        changes = self._pending()
        if not changes:
            QMessageBox.information(self, 'No changes', 'Edit one or more Value cells first.')
            return
        if QMessageBox.question(
            self, 'Apply SCR settings',
            f'Apply {len(changes)} pending values? A pristine archive/index backup will be kept.',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run('Installing and verifying SCR changes...', lambda: self._editor().apply(changes), self._applied)

    def _applied(self, result: dict):
        self.status.setText(f"Applied and verified {result['affected_count']} SCR values.")
        self._reload()

    def _restore(self):
        row_index = self.table.currentRow()
        if row_index < 0:
            return
        row = self.table.item(row_index, 0).data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(
            self, 'Restore SCR container', f"Restore {row['name']} from the pristine backup?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(
            f"Restoring {row['name']}...",
            lambda: self._editor().restore_entry(row['name'], row['archive']), self._restored,
        )

    def _restored(self, result: dict):
        self.status.setText(f"Restored and verified {result['name']}.")
        self._reload()

    def _failed(self, detail: str):
        self._set_enabled(bool(self._installation))
        self.status.setText('SCR operation failed; transactional writes were rolled back when needed.')
        QMessageBox.critical(self, 'Racing controls failed', detail)
