"""Native managed paint catalog and AI named-race assignment page."""

from __future__ import annotations

import json

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.managed_paints import ManagedPaintEditor
from nascar_modding.editing.full_repair import FullRepairEditor
from .common import page_title
from .workers import FunctionWorker


class ManagedPaintsPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._catalog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Managed paints & AI schedule',
            'Review created schemes and assign any driver livery to a named race using the recovered EVENTINIT writer.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        choices = QHBoxLayout()
        self.event = QComboBox()
        self.driver = QComboBox()
        self.livery = QComboBox()
        self.save_button = QPushButton('Save assignment')
        self.save_button.setObjectName('primary')
        choices.addWidget(QLabel('Race'))
        choices.addWidget(self.event, 1)
        choices.addWidget(QLabel('Driver'))
        choices.addWidget(self.driver, 1)
        choices.addWidget(QLabel('Paint'))
        choices.addWidget(self.livery, 1)
        choices.addWidget(self.save_button)
        layout.addLayout(choices)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton('Refresh')
        self.preview_button = QPushButton('Preview AI patch')
        self.apply_button = QPushButton('Apply AI schedule')
        self.restore_button = QPushButton('Restore AI schedule')
        self.audit_button = QPushButton('Audit managed paints')
        self.export_button = QPushButton('Export library...')
        self.undo_button = QPushButton('Undo last paint change')
        self.remove_button = QPushButton('Remove selected latest paint')
        self.create_button = QPushButton('Create paint slot...')
        self.repair_button = QPushButton('Repair managed runtime')
        self.preview_repair_button = QPushButton('Repair missing previews')
        self.finalize_button = QPushButton('Finalize livery registry')
        self.state_repair_button = QPushButton('Reconcile managed state')
        self.thumbnail_button = QPushButton('Replace selected thumbnail...')
        self.uid_button = QPushButton('UID pool diagnostics')
        for button in (self.refresh_button, self.preview_button, self.apply_button,
                       self.restore_button, self.audit_button, self.export_button,
                       self.undo_button, self.remove_button, self.create_button,
                       self.repair_button, self.preview_repair_button,
                       self.finalize_button, self.state_repair_button,
                       self.thumbnail_button, self.uid_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('Driver', 'Paint', 'UID', 'Year', 'State'))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(180)
        layout.addWidget(self.details)
        self.status = QLabel('Select NASCAR 15 on Setup.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.driver.currentIndexChanged.connect(self._driver_changed)
        self.refresh_button.clicked.connect(self.refresh)
        self.save_button.clicked.connect(self._save_assignment)
        self.preview_button.clicked.connect(lambda: self._run(self._editor().preview_ai, self._show))
        self.audit_button.clicked.connect(lambda: self._run(self._audit, self._show))
        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.export_button.clicked.connect(self._export)
        self.undo_button.clicked.connect(self._undo)
        self.remove_button.clicked.connect(self._remove_selected)
        self.create_button.clicked.connect(self._create)
        self.repair_button.clicked.connect(self._repair_runtime)
        self.preview_repair_button.clicked.connect(lambda: self._confirm_repair(
            'Repair missing previews', 'Install missing managed Paint Select previews?',
            self._editor().repair_missing_previews))
        self.finalize_button.clicked.connect(lambda: self._confirm_repair(
            'Finalize livery registry', 'Finalize managed livery registry records and verify them?',
            self._editor().finalize_registry))
        self.state_repair_button.clicked.connect(lambda: self._confirm_repair(
            'Reconcile managed state', 'Rebuild app-owned state from verified live records?',
            self._editor().repair_state_from_live))
        self.thumbnail_button.clicked.connect(self._replace_thumbnail)
        self.uid_button.clicked.connect(self._uid_diagnostics)
        state.installation_changed.connect(lambda _installation: self.refresh())
        if state.installation:
            self.refresh()

    def _editor(self):
        if not self.state.installation:
            raise ValueError('select NASCAR 15 first')
        state_path = default_app_data_root() / 'extra_schemes_v1.json'
        return ManagedPaintEditor(self.state.installation, state_path)

    def _set_enabled(self, enabled):
        for widget in (self.event, self.driver, self.livery, self.save_button,
                       self.refresh_button, self.preview_button, self.apply_button,
                       self.restore_button, self.audit_button, self.export_button,
                       self.undo_button, self.remove_button, self.create_button,
                       self.repair_button, self.preview_repair_button,
                       self.finalize_button, self.state_repair_button,
                       self.thumbnail_button, self.uid_button):
            widget.setEnabled(enabled)

    def _run(self, function, finished):
        self._set_enabled(False)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def refresh(self):
        enabled = bool(self.state.installation and self.state.installation.profile.id == 'nascar15')
        self._set_enabled(enabled)
        if enabled:
            self.status.setText('Loading the live paint catalog...')
            self._run(self._editor().catalog, self._loaded)
        else:
            self.table.setRowCount(0)
            self.status.setText('Managed paint creation and AI scheduling currently apply only to NASCAR 15.')

    def _loaded(self, catalog):
        self._catalog = catalog
        self.event.clear()
        self.driver.clear()
        for event in catalog['events']:
            self.event.addItem(f"{event['track']} - {event['event']}", event)
        for driver in catalog['drivers']:
            self.driver.addItem(driver['label'], driver)
        rows = [(driver, scheme) for driver in catalog['drivers'] for scheme in driver['schemes']]
        self._rows = rows
        self.table.setRowCount(len(rows))
        for index, (driver, scheme) in enumerate(rows):
            state = 'Managed' if scheme.get('managed') else 'Stock/DLC'
            values = (driver['label'], scheme['label'], scheme['uid'], scheme.get('year') or '-', state)
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self._driver_changed()
        self._set_enabled(True)
        assignments = sum(len(value) for value in catalog['assignments'].values())
        managed = sum(scheme.get('managed') for driver in catalog['drivers'] for scheme in driver['schemes'])
        self.status.setText(f'Loaded {len(rows)} paints; {managed} managed and {assignments} named-race assignments.')

    def _driver_changed(self):
        self.livery.clear()
        driver = self.driver.currentData()
        if not driver:
            return
        self.livery.addItem('Use normal selection', None)
        for scheme in driver['schemes']:
            if scheme.get('uid') is not None:
                self.livery.addItem(scheme['label'], int(scheme['uid']))

    def _save_assignment(self):
        event, driver = self.event.currentData(), self.driver.currentData()
        if event and driver:
            self._run(lambda: self._editor().set_assignment(
                event['key'], driver['uid'], self.livery.currentData()), self._saved)

    def _saved(self, result):
        self._show(result)
        self.refresh()

    def _audit(self):
        return FullRepairEditor(
            self.state.installation, default_app_data_root(), app_version='native',
        ).paint_system_check()

    def _apply(self):
        if QMessageBox.question(self, 'Apply AI paint schedule',
                                'Patch EVENTINIT from the saved assignments and verify read-back?') == QMessageBox.StandardButton.Yes:
            self._run(self._editor().apply_ai, self._show)

    def _restore(self):
        if QMessageBox.question(self, 'Restore AI paint schedule',
                                'Restore the saved clean EVENTINIT base?') == QMessageBox.StandardButton.Yes:
            self._run(self._editor().restore_ai, self._show)

    def _export(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Export managed-paint library',
            'nascar15_managed_paints.zip', 'ZIP (*.zip)',
        )
        if not path:
            return
        try:
            with open(path, 'wb') as handle:
                handle.write(self._editor().export_library_bytes())
            self.status.setText(f'Exported the managed-paint library to {path}.')
        except Exception as exc:
            self._failed(str(exc))

    def _undo(self):
        status = self._editor().undo_status()
        if not status.get('available'):
            QMessageBox.information(self, 'No paint checkpoint', 'There is no paint change to undo.')
            return
        if QMessageBox.question(
            self, 'Undo paint change',
            f"Restore the exact checkpoint for {status.get('label', 'the last paint change')}?",
        ) == QMessageBox.StandardButton.Yes:
            self._run(self._editor().undo, self._saved)

    def _remove_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(getattr(self, '_rows', [])):
            QMessageBox.information(self, 'Choose a paint', 'Select a managed paint in the table first.')
            return
        _driver, scheme = self._rows[row]
        if not scheme.get('managed') or scheme.get('uid') is None:
            QMessageBox.information(self, 'Stock paint protected', 'Only an app-managed paint can be removed.')
            return
        uid = int(scheme['uid'])
        if QMessageBox.question(
            self, 'Remove latest managed paint',
            f'Remove paint UID {uid} by restoring its exact creation checkpoint?',
        ) == QMessageBox.StandardButton.Yes:
            self._run(lambda: self._editor().remove(uid), self._saved)

    def _create(self):
        driver = self.driver.currentData()
        if not driver:
            return
        name, accepted = QInputDialog.getText(self, 'New paint name', 'Display name')
        if not accepted or not name.strip():
            return
        paint, _filter = QFileDialog.getOpenFileName(
            self, 'Choose the 2:1 paint atlas', '', 'Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)',
        )
        if not paint:
            return
        thumbnail, _filter = QFileDialog.getOpenFileName(
            self, 'Choose the Paint Select thumbnail', '', 'Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)',
        )
        if not thumbnail:
            return
        if QMessageBox.question(
            self, 'Create managed paint',
            f"Create a native paint slot for {driver['label']} with exact rollback coverage?",
        ) == QMessageBox.StandardButton.Yes:
            self._run(
                lambda: self._editor().create(
                    int(driver['uid']), name.strip(), paint, thumbnail, quality='auto',
                ),
                self._saved,
            )

    def _repair_runtime(self):
        if QMessageBox.question(
            self, 'Repair managed paint runtime',
            'Rebuild every managed database record and SD/HD wrapper from saved source images?',
        ) == QMessageBox.StandardButton.Yes:
            self._run(self._editor().repair_runtime, self._saved)

    def _confirm_repair(self, title, prompt, operation):
        if QMessageBox.question(self, title, prompt) == QMessageBox.StandardButton.Yes:
            self._run(operation, self._saved)

    def _replace_thumbnail(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(getattr(self, '_rows', [])):
            QMessageBox.information(self, 'Choose a paint', 'Select a managed paint first.')
            return
        _driver, scheme = self._rows[row]
        if not scheme.get('managed') or scheme.get('uid') is None:
            QMessageBox.information(self, 'Stock paint protected', 'Only managed paint thumbnails can be replaced here.')
            return
        source, _filter = QFileDialog.getOpenFileName(
            self, 'Choose the new Paint Select thumbnail', '',
            'Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)',
        )
        if source:
            self._run(
                lambda: self._editor().replace_thumbnail(int(scheme['uid']), source),
                self._saved,
            )

    def _uid_diagnostics(self):
        try:
            state = self._editor().uid_pool()
        except Exception as exc:
            self._failed(str(exc))
            return
        self._show(state)
        candidate = state.get('next_candidate')
        if candidate is None:
            QMessageBox.information(self, 'UID pool', 'There are no untested UIDs in the recovered range.')
            return
        if QMessageBox.question(
            self, 'Record a game test?',
            f'Next untested UID: {candidate}. Record a result from an already completed in-game test?',
        ) != QMessageBox.StandardButton.Yes:
            return
        uid, accepted = QInputDialog.getInt(
            self, 'Tested UID', 'UID', int(candidate),
            ManagedPaintEditor.UID_FLOOR, ManagedPaintEditor.UID_CEILING - 1,
        )
        if not accepted:
            return
        verdict, accepted = QInputDialog.getItem(
            self, 'Game-test result', 'Result',
            ('works', 'not_visible', 'broken', 'untested'), 0, False,
        )
        if not accepted:
            return
        note, accepted = QInputDialog.getText(self, 'Test note', 'Optional note')
        if accepted:
            self._run(
                lambda: self._editor().record_uid_verdict(uid, verdict, note),
                self._show,
            )

    def _show(self, result):
        self._set_enabled(True)
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.status.setText('Operation completed and its result is shown below.')

    def _failed(self, detail):
        self._set_enabled(True)
        self.status.setText('Managed paint operation failed.')
        QMessageBox.critical(self, 'Managed paint operation failed', detail)
