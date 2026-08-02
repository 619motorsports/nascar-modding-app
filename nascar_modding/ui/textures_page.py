"""Native decoded texture-bank browser and guarded editor."""

from __future__ import annotations

from PySide6.QtCore import QSize, QThreadPool, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.games.installation import GameInstallation

from .common import page_title
from .workers import FunctionWorker


class TexturePreview(QLabel):
    """Aspect-fit preview whose source dimensions never affect its parent layout."""

    def __init__(self, parent=None):
        super().__init__('Select a decoded texture.', parent)
        self._source = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet('background:#0b1118;border:1px solid #293849;')

    def sizeHint(self):
        return QSize(400, 320)

    def set_texture(self, pixmap: QPixmap):
        self._source = pixmap
        self.setText('')
        self._render()

    def set_message(self, message: str):
        self._source = QPixmap()
        self.clear()
        self.setText(message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def _render(self):
        if self._source.isNull() or self.width() <= 0 or self.height() <= 0:
            return
        super().setPixmap(self._source.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class TexturesPage(QWidget):
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
            'Images and texture banks',
            'Decode inner ARCC textures, export PNGs, and replace only proven-safe families with structural and decoded read-back validation.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QVBoxLayout()
        selectors = QHBoxLayout()
        actions = QHBoxLayout()
        self.archive = QComboBox()
        self.container = QComboBox()
        self.archive.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.archive.setMinimumContentsLength(8)
        self.container.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.container.setMinimumContentsLength(16)
        self.load_button = QPushButton('Load texture bank')
        self.export_button = QPushButton('Export PNG...')
        self.import_button = QPushButton('Replace PNG...')
        self.import_button.setObjectName('primary')
        self.restore_button = QPushButton('Restore texture')
        selectors.addWidget(QLabel('Archive'))
        selectors.addWidget(self.archive)
        selectors.addWidget(QLabel('Container'))
        selectors.addWidget(self.container, 1)
        selectors.addWidget(self.load_button)
        actions.addStretch(1)
        actions.addWidget(self.export_button)
        actions.addWidget(self.import_button)
        actions.addWidget(self.restore_button)
        controls.addLayout(selectors)
        controls.addLayout(actions)
        layout.addLayout(controls)

        splitter = QSplitter()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('Texture', 'Size', 'Format', 'Bytes', 'Replace'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.preview = TexturePreview()
        splitter.addWidget(self.table)
        splitter.addWidget(self.preview)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.status = QLabel('Select a game on Setup first.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.archive.currentIndexChanged.connect(self._archive_changed)
        self.load_button.clicked.connect(self._load)
        self.table.itemSelectionChanged.connect(self._selected)
        self.export_button.clicked.connect(self._export)
        self.import_button.clicked.connect(self._import)
        self.restore_button.clicked.connect(self._restore)
        self.state.installation_changed.connect(self._installation_changed)
        self._set_enabled(False)
        if state.installation:
            self._installation_changed(state.installation)

    def _editor(self):
        if not self._installation:
            raise ValueError('Select a game installation first.')
        return TextureBankEditor(self._installation)

    def _set_enabled(self, enabled: bool):
        for widget in (self.load_button, self.export_button, self.import_button, self.restore_button):
            widget.setEnabled(enabled)

    def _installation_changed(self, installation: GameInstallation):
        self._installation = installation
        self.archive.clear()
        for item in self._editor().resources.archives():
            self.archive.addItem(f"ARCHIVE{item['key']} ({item['entries']:,})", item['key'])
        self._archive_changed()

    def _archive_changed(self):
        if not self._installation or self.archive.currentData() is None:
            return
        key = self.archive.currentData()
        self.container.clear()
        rows = self._editor().containers(key)
        preferred = -1
        for index, row in enumerate(rows):
            self.container.addItem(row['name'], row)
            if row['name'].casefold() == self._installation.profile.number_container.casefold():
                preferred = index
        if preferred >= 0:
            self.container.setCurrentIndex(preferred)
        self._set_enabled(bool(rows))
        self.status.setText(f'{len(rows):,} likely texture-bank containers in ARCHIVE{key}.')

    def _run(self, label, function, finished):
        self._set_enabled(False)
        self.status.setText(label)
        worker = FunctionWorker(function)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _load(self):
        row = self.container.currentData()
        if not row:
            return
        self._run(
            f"Parsing {row['name']}...",
            lambda: self._editor().entries(row['archive'], row['name']), self._loaded,
        )

    def _loaded(self, rows):
        self._rows = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                row['name'], f"{row['width']}×{row['height']}", row['format'],
                f"{row['payload_size']:,}", 'Safe' if row['replace_supported'] else 'Export only',
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        self._set_enabled(True)
        self.status.setText(
            f'{len(rows):,} decoded textures using '
            f'{self._editor().decoder_revision}. '
            'Replacement remains locked for unproven families.'
        )
        if rows:
            self.table.selectRow(0)

    def _row(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected(self):
        row = self._row()
        if not row:
            return
        try:
            data = self._editor().image_png(row['archive'], row['container'], row['name'])
            pixmap = QPixmap()
            pixmap.loadFromData(data, 'PNG')
            self.preview.set_texture(pixmap)
            self.import_button.setEnabled(row['replace_supported'])
        except Exception as exc:
            self.preview.set_message(str(exc))
            self.import_button.setEnabled(False)

    def _export(self):
        row = self._row()
        if not row:
            return
        path, _filter = QFileDialog.getSaveFileName(self, 'Export decoded texture', row['name'] + '.png', 'PNG (*.png)')
        if path:
            try:
                self._editor().export_png(row['archive'], row['container'], row['name'], path)
                self.status.setText(f"Exported {row['name']} to {path}.")
            except Exception as exc:
                QMessageBox.critical(self, 'Texture export failed', str(exc))

    def _import(self):
        row = self._row()
        if not row or not row['replace_supported']:
            return
        path, _filter = QFileDialog.getOpenFileName(self, 'Choose replacement image', '', 'Images (*.png *.jpg *.jpeg *.bmp *.tga *.tif *.tiff)')
        if not path:
            return
        if QMessageBox.question(
            self, 'Replace decoded texture',
            f"Replace {row['name']} at {row['width']}×{row['height']}? A pristine backup will be kept.",
        ) != QMessageBox.StandardButton.Yes:
            return
        mode = 'stretch' if row['container'].casefold() == self._installation.profile.number_container.casefold() else 'fit'
        self._run('Encoding, validating, installing, and decoding read-back...', lambda: self._editor().replace_png(row['archive'], row['container'], row['name'], path, resize_mode=mode), self._changed)

    def _restore(self):
        row = self._row()
        if not row:
            return
        if QMessageBox.question(self, 'Restore texture', f"Restore {row['name']} from the pristine backup?") != QMessageBox.StandardButton.Yes:
            return
        self._run('Restoring and validating texture...', lambda: self._editor().restore_entry(row['archive'], row['container'], row['name']), self._changed)

    def _changed(self, result):
        self.status.setText(f"Verified {result['entry']}.")
        self._load()

    def _failed(self, detail: str):
        self._set_enabled(bool(self._installation))
        self.status.setText('Texture operation failed; unverified bytes were not accepted.')
        QMessageBox.critical(self, 'Texture operation failed', detail)
