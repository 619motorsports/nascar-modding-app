"""Native read-only track resource inventory and comparison page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.games.installation import GameInstallation
from nascar_modding.verification.tracks import TrackInventory

from .common import page_title
from .workers import FunctionWorker


class TracksPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._installation: GameInstallation | None = None
        self._summary = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Track file inventory',
            'Inspect likely track-owned resources across every archive, compare two tracks by normalized file pattern, or export a read-only research bundle.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        filters = QHBoxLayout()
        self.track = QComboBox()
        self.category = QComboBox()
        self.query = QLineEdit()
        self.query.setPlaceholderText('Filter resource names')
        self.reload_button = QPushButton('Rescan')
        self.report_button = QPushButton('Save report...')
        self.export_button = QPushButton('Export selected track...')
        filters.addWidget(QLabel('Track'))
        filters.addWidget(self.track)
        filters.addWidget(QLabel('Category'))
        filters.addWidget(self.category)
        filters.addWidget(self.query, 1)
        filters.addWidget(self.reload_button)
        filters.addWidget(self.report_button)
        filters.addWidget(self.export_button)
        layout.addLayout(filters)

        compare = QHBoxLayout()
        self.compare_a = QComboBox()
        self.compare_b = QComboBox()
        self.compare_button = QPushButton('Compare track patterns')
        compare.addWidget(QLabel('Compare'))
        compare.addWidget(self.compare_a)
        compare.addWidget(QLabel('with'))
        compare.addWidget(self.compare_b)
        compare.addWidget(self.compare_button)
        compare.addStretch(1)
        layout.addLayout(compare)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ('Track', 'Category', 'Resource', 'Archive', 'Bytes', 'Type', 'Confidence')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.track.currentIndexChanged.connect(self._render)
        self.category.currentIndexChanged.connect(self._render)
        self.query.textChanged.connect(self._render)
        self.reload_button.clicked.connect(self._scan)
        self.report_button.clicked.connect(self._report)
        self.export_button.clicked.connect(self._export)
        self.compare_button.clicked.connect(self._compare)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _inventory(self):
        if not self._installation:
            raise ValueError('Select a game installation first.')
        return TrackInventory(self._installation)

    def _set_enabled(self, enabled):
        for widget in (
            self.track, self.category, self.query, self.reload_button,
            self.report_button, self.export_button, self.compare_a,
            self.compare_b, self.compare_button,
        ):
            widget.setEnabled(enabled)

    def _run(self, message, function, finished):
        self._set_enabled(False)
        self.status.setText(message)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _installation_changed(self, installation):
        self._installation = installation
        self._scan()

    def _scan(self):
        self._run('Classifying indexed track resources...', self._inventory().summary, self._loaded)

    def _loaded(self, summary):
        self._summary = summary
        self.track.clear()
        self.track.addItem('All tracks', 'all')
        self.category.clear()
        self.category.addItem('All categories', 'all')
        self.compare_a.clear()
        self.compare_b.clear()
        for value in summary['tracks']:
            self.track.addItem(value, value)
            if value != 'Shared / Unmapped':
                self.compare_a.addItem(value, value)
                self.compare_b.addItem(value, value)
        for value in summary['categories']:
            self.category.addItem(value, value)
        if self.compare_b.count() > 1:
            self.compare_b.setCurrentIndex(1)
        self._set_enabled(True)
        self._render()

    def _render(self):
        if not self._summary:
            return
        track = self.track.currentData() or 'all'
        category = self.category.currentData() or 'all'
        wanted = self.query.text().strip().casefold()
        rows = [
            row for row in self._summary['rows']
            if (track == 'all' or row['track'] == track)
            and (category == 'all' or row['category'] == category)
            and (not wanted or wanted in row['name'].casefold())
        ]
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row['track'], row['category'], row['name'], 'ARCHIVE' + row['archive'],
                f"{row['size']:,}", row['extension'], row['confidence'],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row_index, column, item)
        self.export_button.setEnabled(track not in ('all', 'Shared / Unmapped'))
        self.status.setText(f'{len(rows):,} of {len(self._summary["rows"]):,} classified resource links shown. Read-only.')

    def _compare(self):
        first, second = self.compare_a.currentData(), self.compare_b.currentData()
        self._run(
            f'Comparing {first} and {second}...',
            lambda: self._inventory().compare(first, second), self._compared,
        )

    def _compared(self, result):
        self._set_enabled(True)
        summary = result['summary']
        self.status.setText(
            f"{result['a']} vs {result['b']}: {summary['shared']} shared patterns, "
            f"{summary['only_a']} unique to the first, {summary['only_b']} unique to the second."
        )

    def _report(self):
        track = self.track.currentData() or 'all'
        path, _chosen = QFileDialog.getSaveFileName(
            self, 'Save track inventory report', f'track_files_{track}.zip', 'ZIP (*.zip)'
        )
        if not path:
            return
        def task():
            Path(path).write_bytes(self._inventory().report_bytes(track))
            return Path(path)
        self._run('Building track inventory report...', task, self._saved)

    def _export(self):
        track = self.track.currentData()
        if track in (None, 'all', 'Shared / Unmapped'):
            return
        path, _chosen = QFileDialog.getSaveFileName(
            self, 'Export selected track resources', f'{track}_track_files.zip', 'ZIP (*.zip)'
        )
        if path:
            self._run(
                f'Exporting {track} resources...',
                lambda: self._inventory().export_track(track, path), self._exported,
            )

    def _saved(self, path):
        self._set_enabled(True)
        self.status.setText(f'Saved {path}.')

    def _exported(self, result):
        self._set_enabled(True)
        self.status.setText(f"Exported {result['count']:,} {result['track']} resources to {result['path']}.")

    def _failed(self, detail):
        self._set_enabled(bool(self._installation))
        self.status.setText('Track inventory operation failed.')
        QMessageBox.critical(self, 'Track inventory failed', detail)
