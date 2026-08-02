"""Read-only NTG2013 career/frontend and retail-save research page."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from nascar_modding.games.installation import GameInstallation
from nascar_modding.verification.retail_saves import (
    analyze_ntg2013_transition, compare_ntg2013_saves, discover_ntg2013_saves,
    inspect_ntg2013_save,
)
from nascar_modding.verification.career import audit_ntg2013_career

from .common import page_title
from .workers import FunctionWorker


class CareerPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._installation: GameInstallation | None = None
        self._rows = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'NTG2013 Single Season research',
            'Read-only validation of the compiled frontend path and retail GFS save slots. This page never patches an executable or save.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        evidence = QLabel(
            'Frontend evidence: the normal menu gate initializes to -1 and includes text 0x09DF '
            '(Single Season). FUN_00adf690 opens driver/season selection; SINGLE_SEASON_MODE '
            'then hands off to Team Shop. The remaining boundary is a disposable profile '
            'create → race → save → reload test.'
        )
        evidence.setText(
            'Frontend evidence: retail transitions 43/44 and GSSingleSeasonModeInterface are '
            'compiled in. Boot state 30 hard-loads SPRINTNUMS2012.ARC, so that archive is not '
            'an unused swap target. The remaining boundary is a disposable profile '
            'create -> race -> save -> reload test.'
        )
        evidence.setWordWrap(True)
        evidence.setObjectName('status')
        layout.addWidget(evidence)

        actions = QHBoxLayout()
        self.discover_button = QPushButton('Discover Steam saves')
        self.discover_button.setObjectName('primary')
        self.audit_button = QPushButton('Audit frontend readiness')
        self.choose_button = QPushButton('Inspect another save...')
        self.compare_button = QPushButton('Compare before/after saves...')
        self.experiment_button = QPushButton('Analyze transition folders...')
        actions.addWidget(self.discover_button)
        actions.addWidget(self.audit_button)
        actions.addWidget(self.choose_button)
        actions.addWidget(self.compare_button)
        actions.addWidget(self.experiment_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(('File', 'Role', 'Bytes', 'GFS version', 'Section', 'Validation'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(180)
        layout.addWidget(self.details)
        self.status = QLabel('Select NASCAR The Game: 2013 on Setup to inspect its saves.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.discover_button.clicked.connect(self._discover)
        self.audit_button.clicked.connect(self._audit_frontend)
        self.choose_button.clicked.connect(self._choose)
        self.compare_button.clicked.connect(self._compare)
        self.experiment_button.clicked.connect(self._experiment)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _set_enabled(self, enabled: bool):
        self.discover_button.setEnabled(enabled)
        self.audit_button.setEnabled(enabled)
        self.choose_button.setEnabled(enabled)
        self.compare_button.setEnabled(enabled)
        self.experiment_button.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._installation = installation
        enabled = installation.profile.id == 'nascar13'
        self._set_enabled(enabled)
        self.table.setRowCount(0)
        self.details.clear()
        if enabled:
            self.status.setText('Ready for read-only Steam Cloud save discovery.')
            self._discover()
        else:
            self.status.setText('This research workflow applies only to NASCAR The Game: 2013.')

    def _discover(self):
        if not self._installation:
            return

        def task():
            return [inspect_ntg2013_save(path) for path in discover_ntg2013_saves(self._installation)]

        self._run('Discovering and validating NTG2013 save slots...', task)

    def _choose(self):
        path, _filter = QFileDialog.getOpenFileName(self, 'Choose an NTG2013 save slot')
        if path:
            self._run('Validating selected GFS save...', lambda: [inspect_ntg2013_save(path)])

    def _compare(self):
        before, _filter = QFileDialog.getOpenFileName(self, 'Choose the baseline NTG2013 save')
        if not before:
            return
        after, _filter = QFileDialog.getOpenFileName(self, 'Choose the changed NTG2013 save')
        if not after:
            return
        self._run(
            'Comparing decrypted save payloads without modifying either file...',
            lambda: compare_ntg2013_saves(before, after),
            self._comparison_loaded,
        )

    def _experiment(self):
        count, accepted = QInputDialog.getInt(
            self, 'Transition phases',
            'How many ordered copied-save folders do you have?', 2, 2, 8,
        )
        if not accepted:
            return
        defaults = ('baseline', 'profile_created', 'season_started', 'race_completed',
                    'game_restarted', 'save_reloaded', 'restored', 'final')
        phases = []
        for index in range(count):
            label, accepted = QInputDialog.getText(
                self, f'Phase {index + 1} label', 'Short phase label',
                text=defaults[index],
            )
            if not accepted or not label.strip():
                return
            folder = QFileDialog.getExistingDirectory(
                self, f'Choose copied-save folder for {label.strip()}',
            )
            if not folder:
                return
            phases.append((label.strip(), folder))
        self._run(
            'Comparing ordered copied-save phases; source folders remain untouched...',
            lambda: analyze_ntg2013_transition(phases), self._experiment_loaded,
        )

    def _audit_frontend(self):
        if not self._installation:
            return
        data_dir = Path(__file__).resolve().parents[2] / 'data' / 'nascar13'
        self._run(
            'Checking installed frontend resources and executable strings...',
            lambda: [{'frontend_audit': audit_ntg2013_career(self._installation, data_dir)}],
        )

    def _run(self, label: str, function, finished=None):
        self._set_enabled(False)
        self.status.setText(label)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished or self._loaded)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _comparison_loaded(self, result: dict):
        self._set_enabled(bool(self._installation and self._installation.profile.id == 'nascar13'))
        self.details.setPlainText(json.dumps(result, indent=2))
        self.status.setText(
            f"Read-only comparison found {result.get('changed_bytes', 0):,} changed "
            f"payload bytes and {len(result.get('known_prefix_scalar_changes', []))} "
            'changes in the proven contiguous scalar prefix.'
        )

    def _experiment_loaded(self, result: dict):
        self._set_enabled(bool(self._installation and self._installation.profile.id == 'nascar13'))
        self.details.setPlainText(json.dumps(result, indent=2))
        changed = sum(len(row.get('changed', ())) for row in result.get('transitions', ()))
        prefix = sum(row.get('proven_prefix_change_count', 0) for row in result.get('transitions', ()))
        self.status.setText(
            f"Read-only analysis covered {result.get('phase_count', 0)} phases, "
            f'{changed} changed slot transitions, and {prefix} proven-prefix scalar changes.'
        )

    def _loaded(self, rows: list[dict]):
        self._rows = rows
        if rows and 'frontend_audit' in rows[0]:
            audit = rows[0]['frontend_audit']
            self.table.setRowCount(0)
            self.details.setPlainText(json.dumps(audit, indent=2))
            self._set_enabled(bool(self._installation and self._installation.profile.id == 'nascar13'))
            self.status.setText(
                'Frontend transition and required SprintNums2012 preload verified; '
                'no executable or asset patch is warranted.'
                if audit.get('frontend_transition_intact') else
                'Frontend evidence differs from the verified retail build; no patch was attempted.'
            )
            return
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                row['name'], row.get('role', 'unknown retail slot'), f"{row['file_size']:,}",
                row.get('version', '—'), row.get('section_name') or '—',
                'Valid' if row.get('valid') else 'Invalid',
            )
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self._set_enabled(bool(self._installation and self._installation.profile.id == 'nascar13'))
        profiles = sum(1 for row in rows if row.get('retail_profile_candidate'))
        self.status.setText(
            f'Validated {sum(1 for row in rows if row.get("valid"))} of {len(rows)} slots; '
            f'{profiles} contains a substantial PROFILEDATA payload.'
        )
        if rows:
            self.table.selectRow(0)

    def _selection_changed(self):
        selected = self.table.currentRow()
        if 0 <= selected < len(self._rows):
            row = dict(self._rows[selected])
            row.pop('path', None)
            self.details.setPlainText(json.dumps(row, indent=2))

    def _failed(self, detail: str):
        self._set_enabled(bool(self._installation and self._installation.profile.id == 'nascar13'))
        self.status.setText('Save discovery failed; no save was modified.')
        QMessageBox.critical(self, 'Retail save inspection failed', detail)
