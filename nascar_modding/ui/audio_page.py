"""Native audio-bank browser and transactional sample editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QThreadPool, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.audio import AudioBankEditor
from nascar_modding.editing.audio_tools import AudioToolsManager
from nascar_modding.editing.appdata import default_app_data_root
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class AudioPage(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._installation: GameInstallation | None = None
        self._banks: list[dict] = []
        self._sample_info: dict | None = None
        self._audio_buffer = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Audio banks',
            'Browse FSB5/SND samples, preview or export them, and install fixed-slot or full-song replacements through the verified shared editor.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        tools_row = QHBoxLayout()
        self.tools_status = QLabel()
        self.tools_button = QPushButton('Install/update FFmpeg...')
        tools_row.addWidget(self.tools_status, 1)
        tools_row.addWidget(self.tools_button)
        layout.addLayout(tools_row)

        bank_row = QHBoxLayout()
        self.category = QComboBox()
        self.category.addItem('All categories', '')
        self.bank_filter = QLineEdit()
        self.bank_filter.setPlaceholderText('Filter sound groups')
        self.bank = QComboBox()
        self.load_button = QPushButton('Load samples')
        self.load_button.setObjectName('primary')
        bank_row.addWidget(QLabel('Category'))
        bank_row.addWidget(self.category)
        bank_row.addWidget(self.bank_filter)
        bank_row.addWidget(self.bank, 1)
        bank_row.addWidget(self.load_button)
        layout.addLayout(bank_row)

        volume_row = QHBoxLayout()
        self.volume_mode = QComboBox()
        self.volume_mode.addItem('Match stock loudness', 'match_stock')
        self.volume_mode.addItem('Keep source loudness', 'source')
        self.volume_mode.addItem('Custom gain', 'custom')
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-36.0, 12.0)
        self.gain.setDecimals(1)
        self.gain.setSuffix(' dB')
        self.gain.setEnabled(False)
        self.preview_button = QPushButton('Play preview')
        self.export_button = QPushButton('Export sample...')
        self.export_bank_button = QPushButton('Export bank...')
        self.replace_button = QPushButton('Fit to original...')
        self.full_button = QPushButton('Replace full song...')
        self.restore_button = QPushButton('Restore sample')
        self.restore_bank_button = QPushButton('Restore bank')
        volume_row.addWidget(QLabel('Replacement volume'))
        volume_row.addWidget(self.volume_mode)
        volume_row.addWidget(self.gain)
        volume_row.addStretch(1)
        for button in (
            self.preview_button, self.export_button, self.export_bank_button,
            self.replace_button, self.full_button, self.restore_button,
            self.restore_bank_button,
        ):
            volume_row.addWidget(button)
        layout.addLayout(volume_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ('#', 'Sample', 'Format', 'Duration', 'Bytes', 'State')
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)

        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

        self.category.currentIndexChanged.connect(self._render_banks)
        self.bank_filter.textChanged.connect(self._render_banks)
        self.load_button.clicked.connect(self._load_samples)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.volume_mode.currentIndexChanged.connect(
            lambda: self.gain.setEnabled(self.volume_mode.currentData() == 'custom')
        )
        self.preview_button.clicked.connect(self._preview)
        self.export_button.clicked.connect(self._export_sample)
        self.export_bank_button.clicked.connect(self._export_bank)
        self.replace_button.clicked.connect(lambda: self._replace(False))
        self.full_button.clicked.connect(lambda: self._replace(True))
        self.restore_button.clicked.connect(self._restore_sample)
        self.restore_bank_button.clicked.connect(self._restore_bank)
        self.tools_button.clicked.connect(self._install_tools)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_busy(False, ready=False)
        if state.installation:
            self._installation_changed(state.installation)
        self._refresh_tools()

    def _tools(self):
        return AudioToolsManager(default_app_data_root())

    def _refresh_tools(self):
        status = self._tools().status()
        self.tools_status.setText(
            f"FFmpeg ready: {status.get('version') or status.get('path')}"
            if status['ready'] else 'FFmpeg is not installed; compressed-audio conversion is limited.'
        )

    def _install_tools(self):
        answer = QMessageBox.question(
            self, 'Install managed FFmpeg',
            'Download and checksum-verify the current Windows LGPL FFmpeg build?',
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.tools_button.setEnabled(False)
        self._run('Downloading and validating FFmpeg...', self._tools().install, self._tools_installed)

    def _tools_installed(self, result):
        self.tools_button.setEnabled(True)
        self._set_busy(False)
        self._refresh_tools()
        self.status.setText(f"Managed FFmpeg installed: {result['version']}")

    def _editor(self) -> AudioBankEditor:
        if not self._installation:
            raise ValueError('Select a game installation first.')
        return AudioBankEditor(self._installation)

    def _set_busy(self, busy: bool, ready: bool | None = None):
        if ready is None:
            ready = bool(self._installation and self.bank.currentData())
        self.category.setEnabled(not busy and bool(self._installation))
        self.bank_filter.setEnabled(not busy and bool(self._installation))
        self.bank.setEnabled(not busy and bool(self._installation))
        self.load_button.setEnabled(not busy and ready)
        for button in (
            self.preview_button, self.export_button, self.export_bank_button,
            self.replace_button, self.full_button, self.restore_button,
            self.restore_bank_button,
        ):
            button.setEnabled(False if busy else ready)
        if not busy:
            self._selection_changed()

    def _run(self, message: str, function, finished):
        self._set_busy(True)
        self.status.setText(message)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _installation_changed(self, installation: GameInstallation):
        self._installation = installation
        self._banks = []
        self._sample_info = None
        self.table.setRowCount(0)
        self.bank.clear()
        self.category.clear()
        self.category.addItem('All categories', '')
        self._run('Scanning indexed FSB5 and SND sound groups...', self._editor().banks, self._banks_loaded)

    def _banks_loaded(self, rows):
        self._banks = rows
        for category in sorted({row['category'] for row in rows}):
            self.category.addItem(category, category)
        self._render_banks()
        self._set_busy(False, ready=bool(rows))
        self.status.setText(f'{len(rows):,} sound groups found. Select one and load its samples.')

    def _render_banks(self):
        selected = self.bank.currentData() or {}
        selected_key = (selected.get('archive'), selected.get('name'))
        wanted_category = self.category.currentData() or ''
        wanted_text = self.bank_filter.text().strip().casefold()
        self.bank.blockSignals(True)
        self.bank.clear()
        restore_index = -1
        for row in self._banks:
            if wanted_category and row['category'] != wanted_category:
                continue
            if wanted_text and wanted_text not in row['name'].casefold() and wanted_text not in row['first_sample'].casefold():
                continue
            label = f"{row['name']}  [{row['codec'] or '?'}]"
            self.bank.addItem(label, row)
            if (row['archive'], row['name']) == selected_key:
                restore_index = self.bank.count() - 1
        if restore_index >= 0:
            self.bank.setCurrentIndex(restore_index)
        self.bank.blockSignals(False)
        self.load_button.setEnabled(self.bank.count() > 0)

    def _load_samples(self):
        bank = self.bank.currentData()
        if not bank:
            return
        self._run(
            f"Parsing {bank['name']}...",
            lambda: self._editor().samples(bank['archive'], bank['name']),
            self._samples_loaded,
        )

    def _samples_loaded(self, info):
        self._sample_info = info
        rows = info['samples']
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            duration = '' if row['duration'] is None else f"{row['duration']:.2f}s"
            values = (
                row['index'], row['name'], row['spec'], duration,
                f"{row['bytes']:,}", 'Modified' if row['modified'] else 'Stock',
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row_index, column, item)
        self._set_busy(False, ready=True)
        support = 'full-song replacement ready' if info['full_length_supported'] else info['full_length_reason']
        self.status.setText(
            f"{len(rows):,} samples; {info['modified_count']:,} modified; {info['codec']}. "
            f"{support}."
        )
        if rows:
            self.table.selectRow(0)

    def _row(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selection_changed(self):
        row = self._row()
        info = self._sample_info or {}
        ready = bool(row and self._installation)
        self.preview_button.setEnabled(ready and (row['export_wav'] or row['export_mpeg']))
        self.export_button.setEnabled(ready)
        self.export_bank_button.setEnabled(bool(info))
        self.replace_button.setEnabled(ready and row['editable'])
        self.full_button.setEnabled(ready and row['editable'] and info.get('full_length_supported'))
        self.restore_button.setEnabled(ready and info.get('has_backup'))
        self.restore_bank_button.setEnabled(bool(info and info.get('has_backup')))

    def _preview(self):
        row, info = self._row(), self._sample_info
        if not row or not info:
            return
        def task():
            try:
                return self._editor().sample_payload(info['archive'], info['bank'], row['index'], 'wav')
            except Exception:
                return self._editor().sample_payload(info['archive'], info['bank'], row['index'], 'mpeg')
        self._run(f"Preparing {row['name']}...", task, self._preview_ready)

    def _preview_ready(self, item):
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(QByteArray(item['payload']))
        self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.player.setSourceDevice(self._audio_buffer)
        self.player.play()
        self._set_busy(False, ready=True)
        self.status.setText(f"Playing {item['filename']}.")

    def _export_sample(self):
        row, info = self._row(), self._sample_info
        if not row or not info:
            return
        modes = []
        if row['export_wav']:
            modes.append(('WAV', 'wav'))
        if row['export_mpeg']:
            modes.append(('MPEG', 'mpeg'))
        modes.append(('Raw slot', 'raw'))
        selected_filter = ';;'.join(f'{label} (*.*)' for label, _mode in modes)
        path, chosen = QFileDialog.getSaveFileName(
            self, 'Export audio sample', row['name'], selected_filter,
        )
        if not path:
            return
        mode = modes[0][1]
        for (label, candidate) in modes:
            if chosen.startswith(label):
                mode = candidate
                break
        self._run(
            f"Exporting {row['name']}...",
            lambda: self._editor().export_sample(info['archive'], info['bank'], row['index'], path, mode),
            lambda output: self._operation_done({'message': f'Exported to {output}.'}, reload=False),
        )

    def _export_bank(self):
        info = self._sample_info
        if not info:
            return
        default = str(Path(info['bank']).with_suffix('')) + '.zip'
        path, _chosen = QFileDialog.getSaveFileName(self, 'Export raw sample bank', default, 'ZIP (*.zip)')
        if not path:
            return
        self._run(
            f"Exporting {info['bank']}...",
            lambda: self._editor().export_bank(info['archive'], info['bank'], path),
            lambda result: self._operation_done({'message': f"Exported {result['exported']} samples to {result['path']}."}, reload=False),
        )

    def _replace(self, full_song: bool):
        row, info = self._row(), self._sample_info
        if not row or not info:
            return
        path, _chosen = QFileDialog.getOpenFileName(
            self, 'Choose replacement audio', '',
            'Audio (*.wav *.mp3 *.mp2 *.flac *.ogg *.m4a *.aac *.pcm *.raw);;All files (*)',
        )
        if not path:
            return
        operation = 'rebuild the complete bank for a full-length song' if full_song else 'fit the audio to the existing sample slot'
        if QMessageBox.question(
            self, 'Install audio replacement',
            f"This will {operation} for {row['name']}. A pristine archive/index backup will be retained. Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return
        raw = Path(path).read_bytes()
        mode, gain = self.volume_mode.currentData(), self.gain.value()
        if full_song:
            task = lambda: self._editor().replace_full_song(
                info['archive'], info['bank'], row['index'], raw, path, mode, gain)
        else:
            task = lambda: self._editor().replace_sample(
                info['archive'], info['bank'], row['index'], raw, path, mode, gain)
        self._run('Encoding, validating, installing, and checking read-back...', task, self._operation_done)

    def _restore_sample(self):
        row, info = self._row(), self._sample_info
        if not row or not info:
            return
        if QMessageBox.question(self, 'Restore sample', f"Restore {row['name']} from the pristine backup?") != QMessageBox.StandardButton.Yes:
            return
        self._run(
            f"Restoring {row['name']}...",
            lambda: self._editor().restore_sample(info['archive'], info['bank'], row['index']),
            self._operation_done,
        )

    def _restore_bank(self):
        info = self._sample_info
        if not info:
            return
        if QMessageBox.question(self, 'Restore audio bank', f"Restore every sample in {info['bank']} from the pristine backup?") != QMessageBox.StandardButton.Yes:
            return
        self._run(
            f"Restoring {info['bank']}...",
            lambda: self._editor().restore_bank(info['archive'], info['bank']),
            self._operation_done,
        )

    def _operation_done(self, result, reload=True):
        self._set_busy(False, ready=True)
        if 'message' in result:
            self.status.setText(result['message'])
        else:
            self.status.setText(f"Verified {result.get('sample') or result.get('bank')}.")
        if reload:
            self._load_samples()

    def _failed(self, detail: str):
        self.tools_button.setEnabled(True)
        self._set_busy(False, ready=bool(self.bank.currentData()))
        self.status.setText('Audio operation failed; unverified bytes were not accepted.')
        QMessageBox.critical(self, 'Audio operation failed', detail)
