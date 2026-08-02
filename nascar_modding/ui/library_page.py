"""Native AI preset library and pit-strategy observation log."""

from __future__ import annotations

import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.user_library import UserLibrary, profile_config_path

from .common import page_title


class LibraryPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._library: UserLibrary | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Presets and pit notes',
            'Keep reusable AI field sets and structured pit-strategy observations in the selected game profile.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        preset_panel = QWidget()
        preset_layout = QVBoxLayout(preset_panel)
        preset_layout.addWidget(QLabel('AI presets'))
        preset_form = QFormLayout()
        self.preset_name = QLineEdit()
        self.preset_kind = QComboBox()
        for value in ('track', 'global', 'pit'):
            self.preset_kind.addItem(value.title(), value)
        self.preset_note = QLineEdit()
        self.preset_changes = QTextEdit()
        self.preset_changes.setPlaceholderText('[{"field": "RaceLaps", "value": 100}]')
        self.preset_changes.setMaximumHeight(80)
        preset_form.addRow('Name', self.preset_name)
        preset_form.addRow('Kind', self.preset_kind)
        preset_form.addRow('Note', self.preset_note)
        preset_form.addRow('Field changes (JSON)', self.preset_changes)
        preset_layout.addLayout(preset_form)
        preset_actions = QHBoxLayout()
        self.save_preset = QPushButton('Save preset')
        self.delete_preset = QPushButton('Delete selected')
        self.import_presets = QPushButton('Import...')
        self.export_presets = QPushButton('Export...')
        for button in (self.save_preset, self.delete_preset, self.import_presets, self.export_presets):
            preset_actions.addWidget(button)
        preset_actions.addStretch(1)
        preset_layout.addLayout(preset_actions)
        self.preset_table = QTableWidget(0, 5)
        self.preset_table.setHorizontalHeaderLabels(('Name', 'Kind', 'Fields', 'Note', 'ID'))
        self.preset_table.horizontalHeader().setStretchLastSection(True)
        self.preset_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.preset_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        preset_layout.addWidget(self.preset_table)
        splitter.addWidget(preset_panel)

        pit_panel = QWidget()
        pit_layout = QVBoxLayout(pit_panel)
        pit_layout.addWidget(QLabel('Pit-strategy observation log'))
        pit_form = QHBoxLayout()
        self.pit_track = QLineEdit()
        self.pit_track.setPlaceholderText('Track')
        self.pit_preset = QLineEdit()
        self.pit_preset.setPlaceholderText('Preset')
        self.pit_result = QComboBox()
        for value in ('untested', 'improved', 'neutral', 'worse', 'failed'):
            self.pit_result.addItem(value.title(), value)
        self.pit_note = QLineEdit()
        self.pit_note.setPlaceholderText('Observation')
        self.add_pit = QPushButton('Add note')
        self.delete_pit = QPushButton('Delete selected')
        for widget in (
            self.pit_track, self.pit_preset, self.pit_result, self.pit_note,
            self.add_pit, self.delete_pit,
        ):
            pit_form.addWidget(widget)
        pit_layout.addLayout(pit_form)
        self.pit_table = QTableWidget(0, 6)
        self.pit_table.setHorizontalHeaderLabels(('Created', 'Track', 'Preset', 'Result', 'Note', 'ID'))
        self.pit_table.horizontalHeader().setStretchLastSection(True)
        self.pit_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pit_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        pit_layout.addWidget(self.pit_table)
        splitter.addWidget(pit_panel)

        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.save_preset.clicked.connect(self._save_preset)
        self.delete_preset.clicked.connect(self._delete_preset)
        self.import_presets.clicked.connect(self._import_presets)
        self.export_presets.clicked.connect(self._export_presets)
        self.add_pit.clicked.connect(self._add_pit)
        self.delete_pit.clicked.connect(self._delete_pit)
        self.preset_table.itemSelectionChanged.connect(self._preset_selected)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_enabled(self, enabled):
        for widget in (
            self.save_preset, self.delete_preset, self.import_presets, self.export_presets,
            self.add_pit, self.delete_pit,
        ):
            widget.setEnabled(enabled)

    def _installation_changed(self, installation):
        root = default_app_data_root()
        self._library = UserLibrary(profile_config_path(root, installation.profile.id))
        self._set_enabled(True)
        self._reload()

    def _reload(self):
        if not self._library:
            return
        presets, pits = self._library.presets(), self._library.pit_entries()
        self.preset_table.setRowCount(len(presets))
        for row_index, row in enumerate(presets):
            values = (row.get('name'), row.get('kind'), len(row.get('changes') or []), row.get('note'), row.get('id'))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ''))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.preset_table.setItem(row_index, column, item)
        self.pit_table.setRowCount(len(pits))
        for row_index, row in enumerate(pits):
            values = tuple(row.get(key, '') for key in ('created', 'track', 'preset', 'result', 'note', 'id'))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.pit_table.setItem(row_index, column, item)
        self.status.setText(f'{len(presets)} presets and {len(pits)} pit observations loaded.')

    def _preset_selected(self):
        item = self.preset_table.item(self.preset_table.currentRow(), 0)
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not row:
            return
        self.preset_name.setText(row.get('name', ''))
        index = self.preset_kind.findData(row.get('kind'))
        if index >= 0:
            self.preset_kind.setCurrentIndex(index)
        self.preset_note.setText(row.get('note', ''))
        self.preset_changes.setPlainText(json.dumps(row.get('changes') or [], indent=2))

    def _save_preset(self):
        try:
            changes = json.loads(self.preset_changes.toPlainText())
            result = self._library.save_preset({
                'name': self.preset_name.text(), 'kind': self.preset_kind.currentData(),
                'note': self.preset_note.text(), 'changes': changes,
            })
            self.status.setText(f"Saved {result['preset']['name']}.")
            self._reload()
        except Exception as exc:
            QMessageBox.critical(self, 'Preset not saved', str(exc))

    def _delete_preset(self):
        item = self.preset_table.item(self.preset_table.currentRow(), 0)
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        if row and QMessageBox.question(self, 'Delete preset', f"Delete {row['name']}?") == QMessageBox.StandardButton.Yes:
            self._library.delete_preset(row['id'])
            self._reload()

    def _import_presets(self):
        path, _chosen = QFileDialog.getOpenFileName(self, 'Import presets', '', 'JSON (*.json)')
        if path:
            try:
                result = self._library.import_presets_bytes(Path(path).read_bytes())
                self.status.setText(f"Imported {result['imported']} valid presets.")
                self._reload()
            except Exception as exc:
                QMessageBox.critical(self, 'Preset import failed', str(exc))

    def _export_presets(self):
        path, _chosen = QFileDialog.getSaveFileName(self, 'Export presets', 'nascar_ai_presets.json', 'JSON (*.json)')
        if path:
            Path(path).write_bytes(self._library.export_presets_bytes())
            self.status.setText(f'Saved {path}.')

    def _add_pit(self):
        try:
            self._library.add_pit_entry({
                'track': self.pit_track.text(), 'preset': self.pit_preset.text(),
                'result': self.pit_result.currentData(), 'note': self.pit_note.text(),
            })
            self.pit_note.clear()
            self._reload()
        except Exception as exc:
            QMessageBox.critical(self, 'Pit observation not saved', str(exc))

    def _delete_pit(self):
        item = self.pit_table.item(self.pit_table.currentRow(), 0)
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        if row and QMessageBox.question(self, 'Delete observation', 'Delete the selected pit observation?') == QMessageBox.StandardButton.Yes:
            self._library.delete_pit_entry(row['id'])
            self._reload()
