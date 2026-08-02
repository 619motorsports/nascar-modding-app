"""Native NASCAR 15 team catalog and safe link preview."""

from __future__ import annotations

import json

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.editing.team_presentation import TeamPresentationEditor
from nascar_modding.editing.teams import TeamPresentationRecovery
from .common import page_title
from .workers import FunctionWorker


class TeamsPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._catalog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Teams & manufacturers',
            'Move drivers and maintain their team logos and Driver Select presentation through one recoverable transaction.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        row = QHBoxLayout()
        self.driver = QComboBox()
        self.team = QComboBox()
        self.preview = QPushButton('Preview driver move')
        self.move_button = QPushButton('Apply driver move...')
        self.move_button.setObjectName('primary')
        self.refresh_button = QPushButton('Refresh')
        self.restore_button = QPushButton('Restore presentation checkpoint')
        row.addWidget(QLabel('Driver'))
        row.addWidget(self.driver, 1)
        row.addWidget(QLabel('Destination'))
        row.addWidget(self.team, 1)
        row.addWidget(self.preview)
        row.addWidget(self.move_button)
        row.addWidget(self.refresh_button)
        row.addWidget(self.restore_button)
        layout.addLayout(row)
        assets = QHBoxLayout()
        self.manufacturer = QComboBox()
        self.team_name = QLineEdit()
        self.team_name.setPlaceholderText('New destination-team name')
        self.rename_button = QPushButton('Rename destination...')
        self.manufacturer_button = QPushButton('Set destination manufacturer...')
        self.prepare_button = QPushButton('Build/repair destination assets...')
        self.logo_button = QPushButton('Replace destination logo...')
        self.tile_button = QPushButton('Replace driver tile...')
        self.number_button = QPushButton('Replace 3D number...')
        self.art_repair_button = QPushButton('Repair driver art...')
        assets.addWidget(QLabel('Manufacturer'))
        assets.addWidget(self.manufacturer)
        assets.addWidget(self.team_name)
        assets.addWidget(self.rename_button)
        for button in (self.manufacturer_button, self.prepare_button, self.logo_button,
                       self.tile_button, self.number_button, self.art_repair_button):
            assets.addWidget(button)
        layout.addLayout(assets)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('Team', 'Manufacturer', 'Type', 'Drivers', 'UID'))
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
        self.preview.clicked.connect(self._preview)
        self.move_button.clicked.connect(self._move)
        self.refresh_button.clicked.connect(self.refresh)
        self.restore_button.clicked.connect(self._restore_checkpoint)
        self.manufacturer_button.clicked.connect(self._set_manufacturer)
        self.rename_button.clicked.connect(self._rename_team)
        self.prepare_button.clicked.connect(self._prepare_team)
        self.logo_button.clicked.connect(self._replace_logo)
        self.tile_button.clicked.connect(lambda: self._replace_art('tile'))
        self.number_button.clicked.connect(lambda: self._replace_art('number'))
        self.art_repair_button.clicked.connect(self._repair_art)
        state.installation_changed.connect(lambda _installation: self.refresh())
        if state.installation:
            self.refresh()

    def _editor(self):
        if not self.state.installation:
            raise ValueError('select NASCAR 15 first')
        return TeamPresentationEditor(self.state.installation, default_app_data_root())

    def _run(self, function, finished):
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(lambda detail: QMessageBox.critical(self, 'Team workflow failed', detail))
        self._worker = worker
        self.pool.start(worker)

    def refresh(self):
        enabled = bool(self.state.installation and self.state.installation.profile.id == 'nascar15')
        for button in (self.preview, self.move_button, self.refresh_button,
                       self.restore_button, self.manufacturer_button,
                       self.rename_button, self.prepare_button, self.logo_button, self.tile_button,
                       self.number_button, self.art_repair_button):
            button.setEnabled(enabled)
        if enabled:
            self.status.setText('Loading team database...')
            self._run(self._editor().catalog, self._loaded)
        else:
            self.table.setRowCount(0)
            self.status.setText('This recovered team workflow currently applies only to NASCAR 15.')

    def _loaded(self, catalog):
        self._catalog = catalog
        self.driver.clear()
        self.team.clear()
        self.manufacturer.clear()
        for driver in catalog['drivers']:
            self.driver.addItem(f"#{driver['number']} {driver['label']} - {driver['team_label']}", driver)
        for team in catalog['teams']:
            self.team.addItem(team['label'], team)
        for manufacturer in catalog['manufacturers']:
            self.manufacturer.addItem(manufacturer['label'], manufacturer)
        self.table.setRowCount(len(catalog['teams']))
        for index, team in enumerate(catalog['teams']):
            ready = bool(team.get('presentation', {}).get('presentation_ready'))
            values = (team['label'], team['manufacturer_label'],
                      f"{team['category']} / {'ready' if ready else 'assets needed'}",
                      len(team['drivers']), team['uid'])
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self.status.setText(f"Loaded {len(catalog['drivers'])} drivers and {len(catalog['teams'])} teams.")

    def _preview(self):
        driver, team = self.driver.currentData(), self.team.currentData()
        if not driver or not team:
            return
        self._run(
            lambda: self._editor().move_driver(driver['config_uid'], team['uid'], dry_run=True),
            lambda result: self.details.setPlainText(json.dumps(result, indent=2, default=str)),
        )

    def _confirm(self, title, message, operation):
        if QMessageBox.question(self, title, message) == QMessageBox.StandardButton.Yes:
            self._run(operation, self._changed)

    def _move(self):
        driver, team = self.driver.currentData(), self.team.currentData()
        if driver and team:
            self._confirm(
                'Apply driver move',
                f"Move {driver['label']} to {team['label']} and build the required presentation assets?",
                lambda: self._editor().move_driver(driver['config_uid'], team['uid']),
            )

    def _set_manufacturer(self):
        team, manufacturer = self.team.currentData(), self.manufacturer.currentData()
        if team and manufacturer:
            self._confirm(
                'Change manufacturer',
                f"Set {team['label']} to {manufacturer['label']}?",
                lambda: self._editor().set_manufacturer(team['uid'], manufacturer['uid']),
            )

    def _prepare_team(self):
        team = self.team.currentData()
        if team:
            self._confirm(
                'Build presentation assets',
                f"Build or repair presentation assets for {team['label']}?",
                lambda: self._editor().prepare_team(team['uid']),
            )

    def _rename_team(self):
        team, name = self.team.currentData(), self.team_name.text().strip()
        if team and name:
            self._confirm(
                'Rename team', f"Rename {team['label']} to {name}?",
                lambda: self._editor().rename_team(team['uid'], name),
            )

    def _replace_logo(self):
        team = self.team.currentData()
        if not team:
            return
        source, _filter = QFileDialog.getOpenFileName(
            self, 'Choose team logo', '', 'Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)',
        )
        if source:
            self._confirm(
                'Replace team logo', f"Install this logo for {team['label']}?",
                lambda: self._editor().replace_logo(team['uid'], source),
            )

    def _replace_art(self, kind):
        driver = self.driver.currentData()
        if not driver:
            return
        source, _filter = QFileDialog.getOpenFileName(
            self, 'Choose Driver Select image', '',
            'Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)',
        )
        if source:
            self._confirm(
                'Replace Driver Select art', f"Replace {kind} art for {driver['label']}?",
                lambda: self._editor().replace_driver_art(driver['config_uid'], kind, source),
            )

    def _repair_art(self):
        driver = self.driver.currentData()
        if driver:
            self._confirm(
                'Repair Driver Select art', f"Repair presentation art for {driver['label']}?",
                lambda: self._editor().repair_driver_art(driver['config_uid']),
            )

    def _changed(self, result):
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.refresh()

    def _restore_checkpoint(self):
        recovery = TeamPresentationRecovery(self.state.installation, default_app_data_root())
        status = recovery.status()
        if not status.get('available'):
            QMessageBox.information(
                self, 'No safe checkpoint',
                status.get('blocked_reason') or 'There is no team presentation checkpoint to restore.',
            )
            return
        if QMessageBox.question(
            self, 'Restore team presentation checkpoint',
            f"Restore {status.get('label', 'the latest team presentation change')} exactly?",
        ) == QMessageBox.StandardButton.Yes:
            self._run(recovery.restore, self._restored)

    def _restored(self, result):
        self.details.setPlainText(json.dumps(result, indent=2, default=str))
        self.refresh()
