"""Native browser/editor for every indexed game resource."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.games.installation import GameInstallation

from .common import page_title


class ResourcesPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._editor: ResourceEditor | None = None
        self._rows = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Archive resources',
            'Inspect, export, replace, repoint, restore, and package any existing indexed resource. '
            'Variable-size imports update the CDF atomically.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        filters = QHBoxLayout()
        self.archive_combo = QComboBox()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText('Filter by resource name')
        filters.addWidget(QLabel('Archive'))
        filters.addWidget(self.archive_combo)
        filters.addWidget(self.filter_edit, 1)
        layout.addLayout(filters)

        actions = QHBoxLayout()
        self.inspect_button = QPushButton('Inspect')
        self.export_button = QPushButton('Export raw...')
        self.replace_button = QPushButton('Replace / repoint...')
        self.replace_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore original')
        self.package_export_button = QPushButton('Export package...')
        self.package_import_button = QPushButton('Install package...')
        for button in (
            self.inspect_button, self.export_button, self.replace_button,
            self.restore_button, self.package_export_button, self.package_import_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('Name', 'Bytes', 'Offset', 'CDF layout', 'Archive'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)

        self.archive_combo.currentIndexChanged.connect(self._load)
        self.filter_edit.textChanged.connect(self._render)
        self.inspect_button.clicked.connect(self._inspect)
        self.export_button.clicked.connect(self._export)
        self.replace_button.clicked.connect(self._replace)
        self.restore_button.clicked.connect(self._restore)
        self.package_export_button.clicked.connect(self._package_export)
        self.package_import_button.clicked.connect(self._package_import)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_enabled(self, enabled: bool):
        for button in (
            self.inspect_button, self.export_button, self.replace_button,
            self.restore_button, self.package_export_button, self.package_import_button,
        ):
            button.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._editor = ResourceEditor(installation)
        self.archive_combo.blockSignals(True)
        self.archive_combo.clear()
        for row in self._editor.archives():
            self.archive_combo.addItem(
                f"ARCHIVE{row['key']} ({row['entries']:,} resources)", row['key']
            )
        self.archive_combo.blockSignals(False)
        self._set_enabled(self.archive_combo.count() > 0)
        self._load()

    def _load(self):
        if not self._editor or self.archive_combo.currentData() is None:
            return
        key = str(self.archive_combo.currentData())
        self._rows = self._editor.resources(key)
        self._render()
        self.status.setText(f'ARCHIVE{key}: {len(self._rows):,} indexed resources.')

    def _render(self):
        query = self.filter_edit.text().strip().casefold()
        rows = [row for row in self._rows if not query or query in row['name'].casefold()]
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row['name'], f"{row['size']:,}", f"0x{row['offset']:08X}",
                row['layout'], f"ARCHIVE{row['archive']}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (row['archive'], row['name']))
                self.table.setItem(row_index, column, item)
        if rows:
            self.table.selectRow(0)

    def _selections(self):
        selected = []
        for index in self.table.selectionModel().selectedRows(0):
            item = self.table.item(index.row(), 0)
            if item:
                selected.append(item.data(Qt.ItemDataRole.UserRole))
        return selected

    def _one(self):
        selected = self._selections()
        return selected[0] if len(selected) == 1 else None

    def _inspect(self):
        selected = self._one()
        if not selected or not self._editor:
            return
        key, name = selected
        try:
            row = self._editor.inspect(name, key)
            stock = row['stock']
            stock_text = (
                f"\nStock: {stock['size']:,} bytes, modified={stock['modified']}"
                if stock else '\nStock: no paired pristine backup yet'
            )
            QMessageBox.information(
                self, 'Resource details',
                f"{row['name']}\nARCHIVE{row['archive']} / layout {row['layout']}\n"
                f"Offset: 0x{row['offset']:08X}\nSize: {row['size']:,} bytes\n"
                f"SHA-256: {row['sha256']}\nPrefix: {row['prefix_hex']}" + stock_text,
            )
        except Exception as exc:
            QMessageBox.critical(self, 'Inspection failed', str(exc))

    def _export(self):
        selected = self._one()
        if not selected or not self._editor:
            return
        key, name = selected
        path, _filter = QFileDialog.getSaveFileName(self, 'Export resource', name, 'All files (*)')
        if not path:
            return
        try:
            self._editor.export(name, key, path)
            self.status.setText(f'Exported {name} to {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Resource export failed', str(exc))

    def _replace(self):
        selected = self._one()
        if not selected or not self._editor:
            return
        key, name = selected
        path, _filter = QFileDialog.getOpenFileName(self, 'Choose replacement resource', '', 'All files (*)')
        if not path:
            return
        size = Path(path).stat().st_size
        if QMessageBox.question(
            self, 'Replace indexed resource',
            f'Replace {name} with the {size:,}-byte file? A pristine archive/index backup will be retained.',
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._editor.replace_file(name, key, path)
            self._load()
            self.status.setText(f"Installed and verified {name} using {result['method']}.")
        except Exception as exc:
            QMessageBox.critical(self, 'Resource replacement failed', str(exc))

    def _restore(self):
        selected = self._one()
        if not selected or not self._editor:
            return
        key, name = selected
        try:
            self._editor.restore(name, key)
            self._load()
            self.status.setText(f'Restored and verified {name}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Resource restore failed', str(exc))

    def _package_export(self):
        selections = self._selections()
        if not selections or not self._editor:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Export resource package', 'resources.nmrp.zip', 'ZIP (*.zip)'
        )
        if not path:
            return
        try:
            self._editor.export_package(selections, path)
            self.status.setText(f'Exported {len(selections)} resources to {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Package export failed', str(exc))

    def _package_import(self):
        if not self._editor:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Install resource package', '', 'ZIP (*.zip)'
        )
        if not path:
            return
        try:
            preview = self._editor.preview_package(path)
        except Exception as exc:
            QMessageBox.critical(self, 'Package validation failed', str(exc))
            return
        growth = sum(row['size_delta'] for row in preview['entries'])
        if QMessageBox.question(
            self, 'Install resource package',
            f"Install {preview['count']} verified resources? Combined size delta: {growth:+,} bytes.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._editor.install_package(path)
            self._load()
            self.status.setText(
                f"Installed and verified {result['entries']} resources across {result['archives']} archives."
            )
        except Exception as exc:
            QMessageBox.critical(self, 'Package install failed', str(exc))
