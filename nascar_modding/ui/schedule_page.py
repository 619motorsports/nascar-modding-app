"""Native verified 36-race schedule-order page."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.user_library import profile_config_path
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class SchedulePage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._installation: GameInstallation | None = None
        self._rows: list[dict] = []
        self._catalog: list[dict] = []
        self._custom_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Season schedule order',
            'Reorder the verified 36-race Cup calendar, or fill fixed calendar slots from existing race definitions (including repeats). Every preview checks visible and runtime event links.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        self.up_button = QPushButton('Move up')
        self.down_button = QPushButton('Move down')
        self.preview_button = QPushButton('Preview reorder')
        self.apply_button = QPushButton('Apply reorder')
        self.apply_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore schedule PYC')
        self.reload_button = QPushButton('Reload')
        for button in (self.up_button, self.down_button, self.preview_button, self.apply_button, self.restore_button, self.reload_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        custom = QHBoxLayout()
        self.event_choice = QComboBox()
        self.event_laps = QSpinBox()
        self.event_laps.setRange(1, 999)
        self.set_event_button = QPushButton('Set selected slot')
        self.preview_custom_button = QPushButton('Preview custom calendar')
        self.apply_custom_button = QPushButton('Apply custom calendar')
        self.apply_custom_button.setObjectName('primary')
        self.preview_laps_button = QPushButton('Preview event lap default')
        self.apply_laps_button = QPushButton('Apply event lap default')
        custom.addWidget(QLabel('Existing event'))
        custom.addWidget(self.event_choice, 1)
        custom.addWidget(QLabel('Laps'))
        custom.addWidget(self.event_laps)
        custom.addWidget(self.set_event_button)
        custom.addWidget(self.preview_custom_button)
        custom.addWidget(self.apply_custom_button)
        custom.addWidget(self.preview_laps_button)
        custom.addWidget(self.apply_laps_button)
        layout.addLayout(custom)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(('Slot', 'Track / event', 'UID', 'Date', 'Laps', 'Drivers'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.reload_button.clicked.connect(self._reload)
        self.set_event_button.clicked.connect(self._set_event)
        self.preview_custom_button.clicked.connect(self._preview_custom)
        self.apply_custom_button.clicked.connect(self._apply_custom)
        self.preview_laps_button.clicked.connect(lambda: self._event_laps(False))
        self.apply_laps_button.clicked.connect(lambda: self._event_laps(True))
        self.event_choice.currentIndexChanged.connect(self._catalog_selected)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _editor(self):
        if not self._installation:
            raise ValueError('Select a game installation first.')
        config = profile_config_path(
            default_app_data_root(), self._installation.profile.id
        )
        return ScheduleEditor(self._installation, config)

    def _set_enabled(self, enabled):
        for button in (
            self.up_button, self.down_button, self.preview_button, self.apply_button,
            self.restore_button, self.reload_button, self.set_event_button,
            self.preview_custom_button, self.apply_custom_button,
            self.preview_laps_button, self.apply_laps_button,
        ):
            button.setEnabled(enabled)
        self.event_choice.setEnabled(enabled)
        self.event_laps.setEnabled(enabled)

    def _installation_changed(self, installation):
        self._installation = installation
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
        if self._installation:
            self._run(
                'Mapping the live calendar and pristine event catalog...',
                lambda: (self._editor().rows(), self._editor().catalog()), self._loaded,
            )

    def _loaded(self, result):
        rows, catalog = result
        self._rows = rows
        self._catalog = catalog
        self._custom_mode = False
        self.event_choice.clear()
        for row in catalog:
            self.event_choice.addItem(
                f"{row.get('track') or row['event']} - {row['event']} ({row['laps']} laps)", row
            )
        self._render()
        self._set_enabled(True)
        self.status.setText(f"Verified {len(rows)} races for the {self._installation.profile.content_season} season.")

    def _render(self, selection=0):
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            values = (index + 1, row.get('track') or row.get('event'), row['uid'], row['date'], row['laps'], row['drivers'])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        if self._rows:
            self.table.selectRow(max(0, min(selection, len(self._rows) - 1)))

    def _move(self, direction):
        index = self.table.currentRow()
        target = index + direction
        if not 0 <= index < len(self._rows) or not 0 <= target < len(self._rows):
            return
        self._rows[index], self._rows[target] = self._rows[target], self._rows[index]
        self._render(target)
        self.status.setText(
            'Pending custom slot order changed.' if self._custom_mode else
            'Pending order changed. Preview performs a full schedule and collateral-field check.'
        )

    def _uids(self):
        return [row['uid'] for row in self._rows]

    def _preview(self):
        if self._custom_mode:
            self._preview_custom()
            return
        self._run('Repointing a disposable PYC copy and validating all 36 records...', lambda: self._editor().preview_order(self._uids()), self._previewed)

    def _previewed(self, result):
        result.pop('_payload', None)
        self._set_enabled(True)
        self.status.setText(f"Preview passed: {result['change_count']} races move; no game file was changed.")

    def _apply(self):
        if self._custom_mode:
            self._apply_custom()
            return
        if QMessageBox.question(
            self, 'Apply season order',
            'Apply this 36-race order? Existing career saves may cache their old calendar; test with a disposable new season.',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run('Installing and verifying the complete schedule order...', lambda: self._editor().apply_order(self._uids()), self._applied)

    def _catalog_selected(self):
        row = self.event_choice.currentData()
        if row:
            self.event_laps.setValue(int(row['laps']))

    def _set_event(self):
        slot = self.table.currentRow()
        source = self.event_choice.currentData()
        if not 0 <= slot < len(self._rows) or not source:
            return
        self._rows[slot] = {**source, 'laps': self.event_laps.value()}
        self._custom_mode = True
        self._render(slot)
        self.status.setText(
            'Pending custom calendar changed. Preview verifies constructor fields and runtime WorldPointer links without writing.'
        )

    def _custom_slots(self):
        return [
            {'source_uid': int(row['uid']), 'laps': int(row['laps'])}
            for row in self._rows
        ]

    def _preview_custom(self):
        self._run(
            'Building a disposable repeated-event calendar and validating both link layers...',
            lambda: self._editor().preview_custom(self._custom_slots()), self._custom_previewed,
        )

    def _custom_previewed(self, result):
        result.pop('_payload', None)
        self._set_enabled(True)
        self.status.setText(
            f"Custom preview passed: {result['change_count']} changed slots and "
            f"{len(result['runtime_changes'])} runtime links checked; no game file was changed."
        )

    def _apply_custom(self):
        if QMessageBox.question(
            self, 'Apply custom calendar',
            'Install this 36-slot calendar? Existing saves may cache their old calendar; test with a new disposable season.',
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(
            'Installing and semantically verifying the custom calendar...',
            lambda: self._editor().apply_custom(self._custom_slots()), self._custom_applied,
        )

    def _custom_applied(self, result):
        self.status.setText(f"Applied and verified {result['change_count']} custom calendar slots.")
        self._reload()

    def _event_laps(self, apply):
        source = self.event_choice.currentData()
        if not source:
            return
        update = [{
            'profile_key': f"{int(source['event_uid'])}:{source['event']}",
            'laps': self.event_laps.value(),
        }]
        if apply and QMessageBox.question(
            self, 'Apply event lap default',
            f"Set every scheduled occurrence of {source['event']} to {self.event_laps.value()} laps and remember that default?",
        ) != QMessageBox.StandardButton.Yes:
            return
        method = self._editor().apply_event_laps if apply else self._editor().preview_event_laps
        self._run(
            ('Applying' if apply else 'Previewing') + ' the named event lap default...',
            lambda: method(update),
            self._event_laps_done,
        )

    def _event_laps_done(self, result):
        result.pop('_payload', None)
        self._set_enabled(True)
        if result.get('dry_run'):
            self.status.setText(
                f"Event-lap preview passed for {result.get('matched_occurrences', 0)} scheduled occurrence(s); no game file changed."
            )
        else:
            self.status.setText('Event lap default was installed, persisted, and verified.')
            self._reload()

    def _applied(self, result):
        self.status.setText(f"Applied and verified {result['change_count']} moved races.")
        self._reload()

    def _restore(self):
        if QMessageBox.question(self, 'Restore schedule', 'Restore the schedule database from its pristine backup?') != QMessageBox.StandardButton.Yes:
            return
        self._run('Restoring schedule database...', self._editor().restore, self._restored)

    def _restored(self, result):
        self.status.setText(f"Restored and verified {result['name']}.")
        self._reload()

    def _failed(self, detail):
        self._set_enabled(bool(self._installation))
        self.status.setText('Schedule operation failed; no unverified order was accepted.')
        QMessageBox.critical(self, 'Schedule operation failed', detail)
