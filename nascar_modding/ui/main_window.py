"""Main native window and first migrated workflows."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QThreadPool, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from nascar_modding.formats.eutechnyx_mesh import assemble_car_scene, load_mesh
from nascar_modding.editing.archive import ArchiveEntryEditor
from nascar_modding.editing.car_materials import resolve_game_materials
from nascar_modding.editing.stock_paints import StockPaintEditor
from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.games.assets import classify_livery_slot
from nascar_modding.games.discovery import discover_install, validate_install_root
from nascar_modding.games.installation import GameInstallation
from nascar_modding.games.profiles import PROFILES, get_profile
from nascar_modding.verification.mappings import audit_installation
from .car_preview import CarPreviewWidget
from .audio_page import AudioPage
from .appdata_page import AppDataPage
from .backups_page import BackupsPage
from .career_page import CareerPage
from .full_repair_page import FullRepairPage
from .common import page_title
from .names_page import NamesPage
from .library_page import LibraryPage
from .managed_paints_page import ManagedPaintsPage
from .ratings_page import RatingsPage
from .resources_page import ResourcesPage
from .pyc_page import PycPage
from .scr_page import ScrPage
from .support_page import SupportPage
from .schedule_page import SchedulePage
from .season_packs_page import SeasonPacksPage
from .text_page import TextPage
from .textures_page import TexturesPage
from .tracks_page import TracksPage
from .teams_page import TeamsPage
from .workers import FunctionWorker


class ApplicationState(QObject):
    installation_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.settings = QSettings('NASCARModdingApp', 'NativeDesktop')
        self.installation: GameInstallation | None = None

    def saved_path(self, game_id: str) -> str:
        return str(self.settings.value(f'games/{game_id}/path', '') or '')

    def select_installation(self, game_id: str, path: str | Path) -> GameInstallation:
        profile = get_profile(game_id)
        root = validate_install_root(profile, path)
        if root is None:
            required = ', '.join(f'ARCHIVE{key}.AR' for key in profile.required_archives)
            raise ValueError(f'{path} is not a valid {profile.name} install ({required})')
        installation = GameInstallation(profile, root)
        self.settings.setValue(f'games/{game_id}/path', str(root))
        self.settings.setValue('last_game', game_id)
        self.installation = installation
        self.installation_changed.emit(installation)
        return installation


class DashboardPage(QWidget):
    def __init__(self, state: ApplicationState, parent=None):
        super().__init__(parent)
        self.state = state
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        title, subtitle = page_title(
            'Game setup',
            'Choose an installed game. Archive discovery and validation run directly in the desktop app.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName('card')
        form = QFormLayout(card)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)
        self.game_combo = QComboBox()
        for game_id in ('nascar15', 'nascar14', 'nascar13'):
            profile = PROFILES[game_id]
            self.game_combo.addItem(profile.name, game_id)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText('Game install folder (the folder containing data)')
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse)
        self.use_button = QPushButton('Use this installation')
        self.use_button.setObjectName('primary')
        self.use_button.clicked.connect(self._select)
        form.addRow('Game', self.game_combo)
        form.addRow('Install folder', path_row)
        form.addRow('', self.use_button)
        layout.addWidget(card)

        self.status = QLabel('No installation selected.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.game_combo.currentIndexChanged.connect(self._game_changed)

        last = str(self.state.settings.value('last_game', 'nascar15'))
        index = self.game_combo.findData(last)
        if index >= 0:
            self.game_combo.setCurrentIndex(index)
        self._game_changed()

    def _game_changed(self):
        game_id = self.game_combo.currentData()
        saved = self.state.saved_path(game_id)
        discovered = discover_install(game_id) if not saved else None
        self.path_edit.setText(saved or (str(discovered) if discovered else ''))
        profile = get_profile(game_id)
        self.status.setText(
            f'{profile.name} content mapping: season {profile.content_season}, '
            f'paint archive {profile.paint_primary_archive}, '
            f'{len(profile.body_models)} verified body model(s).'
        )

    def _browse(self):
        path = QFileDialog.getExistingDirectory(
            self, 'Choose game installation', self.path_edit.text()
        )
        if path:
            self.path_edit.setText(path)

    def _select(self):
        try:
            installation = self.state.select_installation(
                self.game_combo.currentData(), self.path_edit.text()
            )
        except Exception as exc:
            QMessageBox.critical(self, 'Installation not valid', str(exc))
            return
        self.status.setText(
            f'✓ {installation.profile.name} ready — '
            f'{len(installation.archive_pairs)} archive/index pairs found.'
        )


class CarsPage(QWidget):
    def __init__(self, state: ApplicationState, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._paint_worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title, subtitle = page_title(
            '3D car preview',
            'Native ARCC geometry rendered with OpenGL. Drag to orbit and use the mouse wheel to zoom.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.model_combo = QComboBox()
        self.manufacturer_combo = QComboBox()
        self.load_button = QPushButton('Load car model')
        self.load_button.setObjectName('primary')
        self.materials_checkbox = QCheckBox('Use game materials')
        self.materials_checkbox.setChecked(True)
        self.reset_button = QPushButton('Reset view')
        controls.addWidget(QLabel('Generation'))
        controls.addWidget(self.model_combo, 1)
        controls.addWidget(QLabel('Manufacturer'))
        controls.addWidget(self.manufacturer_combo, 1)
        controls.addWidget(self.load_button)
        controls.addWidget(self.materials_checkbox)
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)

        paint_controls = QHBoxLayout()
        self.paint_combo = QComboBox()
        self.paint_combo.setMinimumContentsLength(22)
        self.installed_paint_button = QPushButton('Use installed paint')
        self.livery_button = QPushButton('Use image file…')
        self.clear_button = QPushButton('Clear paint')
        paint_controls.addWidget(QLabel('Paint'))
        paint_controls.addWidget(self.paint_combo, 1)
        paint_controls.addWidget(self.installed_paint_button)
        paint_controls.addWidget(self.livery_button)
        paint_controls.addWidget(self.clear_button)
        layout.addLayout(paint_controls)
        self.preview = CarPreviewWidget()
        self.preview.setToolTip('Drag with the left mouse button to orbit; use the wheel to zoom.')
        layout.addWidget(self.preview, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setObjectName('status')
        layout.addWidget(self.status)

        self.load_button.clicked.connect(self._load)
        self.model_combo.activated.connect(lambda _index: self._load())
        self.manufacturer_combo.activated.connect(lambda _index: self._load())
        self.materials_checkbox.toggled.connect(lambda _checked: self._load())
        self.reset_button.clicked.connect(self.preview.reset_view)
        self.installed_paint_button.clicked.connect(self._use_installed_paint)
        self.livery_button.clicked.connect(self._choose_livery)
        self.clear_button.clicked.connect(self.preview.clear_livery)
        self.state.installation_changed.connect(self._installation_changed)
        if self.state.installation:
            self._installation_changed(self.state.installation)

    def _installation_changed(self, installation: GameInstallation):
        self.model_combo.blockSignals(True)
        self.manufacturer_combo.blockSignals(True)
        self.model_combo.clear()
        self.manufacturer_combo.clear()
        self.paint_combo.clear()
        for archive, name in installation.available_body_models():
            self.model_combo.addItem(name.replace('_BODY0_0.ARC', ''), (archive, name))
        for alternative, label in installation.profile.car_manufacturers:
            self.manufacturer_combo.addItem(label, alternative)
        paints = []
        for key in installation.archive_pairs:
            for entry in installation.entries(key):
                slot = classify_livery_slot(installation.profile, entry.name)
                if slot is not None:
                    paints.append((slot.number, slot.label, key, entry.name))
        for number, label, key, name in sorted(
            paints,
            key=lambda row: (
                int(row[0]) if row[0].isdigit() else 9999, row[1], row[3],
            ),
        ):
            self.paint_combo.addItem(f'#{number} — {label}', (key, name))
        self.model_combo.blockSignals(False)
        self.manufacturer_combo.blockSignals(False)
        self.status.setText(
            f'{installation.profile.name}: {self.model_combo.count()} generation(s), '
            f'{self.manufacturer_combo.count()} manufacturer(s). Choose either or click Load.'
        )
        ready = self.model_combo.count() > 0 and self.manufacturer_combo.count() > 0
        self.load_button.setEnabled(ready)
        self.model_combo.setEnabled(ready)
        self.manufacturer_combo.setEnabled(ready)
        self.installed_paint_button.setEnabled(self.paint_combo.count() > 0)

    def _load(self):
        installation = self.state.installation
        selection = self.model_combo.currentData()
        alternative = self.manufacturer_combo.currentData()
        manufacturer = self.manufacturer_combo.currentText()
        use_materials = self.materials_checkbox.isChecked()
        if not installation or not selection or alternative is None:
            return
        archive, name = selection
        self.load_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.manufacturer_combo.setEnabled(False)
        self.status.setText(f'Loading {manufacturer} geometry from {name}…')

        def task():
            raw = installation.read_entry(name, archive)
            body = load_mesh(raw, source_name=name, alternative=int(alternative))
            wheel_name = name.replace('_BODY0_0.ARC', '_ALLOY0_0.ARC')
            try:
                wheel_archive, _entry = installation.find_entry(wheel_name)
                wheel = load_mesh(
                    installation.read_entry(wheel_name, wheel_archive),
                    source_name=wheel_name,
                )
            except Exception:
                wheel = None
            scene = assemble_car_scene(body, wheel)
            report = None
            if use_materials:
                try:
                    report = resolve_game_materials(installation, name, scene)
                except Exception as exc:
                    report = {'error': str(exc), 'decoded_textures': 0}
            return manufacturer, scene, report

        worker = FunctionWorker(task)
        worker.signals.finished.connect(self._loaded)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _loaded(self, result):
        manufacturer, scene, report = result
        self.preview.set_scene(scene)
        self.load_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.manufacturer_combo.setEnabled(True)
        material_status = ''
        if report and report.get('error'):
            material_status = f" Game materials unavailable: {report['error']}"
        elif report:
            material_status = (
                f" {report['decoded_textures']} game textures loaded from "
                f"{report['container']}."
            )
        self.status.setText(
            f'✓ {manufacturer} / {scene.source_name}: {scene.vertex_count:,} vertices, '
            f'{scene.triangle_count:,} triangles, {len(scene.parts)} parts.{material_status}'
        )

    def _failed(self, detail: str):
        self.load_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.manufacturer_combo.setEnabled(True)
        self.status.setText('Model load failed. See the error dialog for details.')
        QMessageBox.critical(self, '3D model load failed', detail)

    def _choose_livery(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Choose paint image', '',
            'Images (*.png *.jpg *.jpeg *.bmp *.tga *.tif *.tiff)',
        )
        if path:
            try:
                self.preview.set_livery(path)
            except Exception as exc:
                QMessageBox.critical(self, 'Paint image failed', str(exc))

    def _use_installed_paint(self):
        installation = self.state.installation
        selection = self.paint_combo.currentData()
        if not installation or not selection:
            return
        archive, container_name = selection
        self.installed_paint_button.setEnabled(False)
        self.status.setText(f'Loading installed paint from {container_name}…')

        def task():
            editor = TextureBankEditor(installation)
            entries = editor.entries(archive, container_name)
            livery = next(
                (row for row in entries if row['name'].casefold() == 'img_liv'),
                None,
            )
            if livery is None:
                raise ValueError(f'{container_name} has no IMG_LIV texture')
            return (
                container_name,
                editor.read_image(archive, container_name, livery['name']),
            )

        worker = FunctionWorker(task)
        worker.signals.finished.connect(self._paint_loaded)
        worker.signals.failed.connect(self._paint_failed)
        self._paint_worker = worker
        self.pool.start(worker)

    def _paint_loaded(self, result):
        container_name, image = result
        self.preview.set_livery_image(image)
        self.installed_paint_button.setEnabled(True)
        self.status.setText(f'✓ Installed paint loaded from {container_name}.')

    def _paint_failed(self, detail: str):
        self.installed_paint_button.setEnabled(True)
        self.status.setText('Installed paint could not be loaded.')
        QMessageBox.critical(self, 'Installed paint failed', detail)


class PaintAssetsPage(QWidget):
    """Native, profile-driven raw paint container editor."""

    def __init__(self, state: ApplicationState, parent=None):
        super().__init__(parent)
        self.state = state
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Paint assets',
            'Browse primary-season paint containers. Raw replacement is exact-size, backed up, verified after writing, and rolled back on failure.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        controls = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText('Filter by car number or paint name')
        self.export_button = QPushButton('Export raw...')
        self.import_button = QPushButton('Exact raw import...')
        self.image_button = QPushButton('Smart image import...')
        self.image_button.setEnabled(False)
        self.restore_button = QPushButton('Restore original')
        controls.addWidget(self.filter_edit, 1)
        controls.addWidget(self.export_button)
        controls.addWidget(self.import_button)
        controls.addWidget(self.image_button)
        controls.addWidget(self.restore_button)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(('Car', 'Paint', 'Archive', 'Bytes'))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Select a game on the Setup page first.')
        self.status.setWordWrap(True)
        self.status.setObjectName('status')
        layout.addWidget(self.status)
        self.filter_edit.textChanged.connect(self._render)
        self.export_button.clicked.connect(self._export)
        self.import_button.clicked.connect(self._import)
        self.image_button.clicked.connect(self._smart_import)
        self.restore_button.clicked.connect(self._restore)
        self.state.installation_changed.connect(self._installation_changed)
        if state.installation:
            self._installation_changed(state.installation)

    def _installation_changed(self, installation: GameInstallation):
        rows = []
        for key in installation.archive_pairs:
            for entry in installation.entries(key):
                slot = classify_livery_slot(installation.profile, entry.name)
                if slot is not None:
                    rows.append((slot.number, slot.label, key, entry))
        self._rows = sorted(rows, key=lambda row: (
            int(row[0]) if row[0].isdigit() else 9999, row[1], row[3].name
        ))
        self._render()
        self.status.setText(
            f'{installation.profile.name}: {len(self._rows)} primary-season paint containers.'
        )
        self.image_button.setEnabled(installation.profile.id == 'nascar15')

    def _render(self):
        query = self.filter_edit.text().strip().casefold()
        rows = [row for row in self._rows if not query or query in f'{row[0]} {row[1]} {row[3].name}'.casefold()]
        self.table.setRowCount(len(rows))
        for index, (number, label, key, entry) in enumerate(rows):
            values = (number, label, f'ARCHIVE{key}', f'{entry.size:,}')
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (key, entry.name))
                self.table.setItem(index, column, item)
        if rows and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _selection(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _editor(self):
        if not self.state.installation:
            raise ValueError('Select a game installation first.')
        return ArchiveEntryEditor(self.state.installation)

    def _export(self):
        selected = self._selection()
        if not selected:
            return
        key, name = selected
        path, _filter = QFileDialog.getSaveFileName(self, 'Export raw paint container', name, 'ARC containers (*.arc);;All files (*)')
        if not path:
            return
        try:
            self._editor().export_entry(name, path, key)
            self.status.setText(f'Exported {name} to {path}.')
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def _import(self):
        selected = self._selection()
        if not selected:
            return
        key, name = selected
        path, _filter = QFileDialog.getOpenFileName(self, 'Choose exact raw replacement', '', 'ARC containers (*.arc);;All files (*)')
        if not path:
            return
        if QMessageBox.question(self, 'Install raw paint', f'Replace {name} with the exact-size raw container? A pristine archive backup will be kept.') != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._editor().replace_entry(name, Path(path).read_bytes(), key)
            self.status.setText(f"Installed and verified {result['name']} ({result['size']:,} bytes).")
        except Exception as exc:
            QMessageBox.critical(self, 'Raw import failed', str(exc))

    def _smart_import(self):
        selected = self._selection()
        if not selected or not self.state.installation:
            return
        _key, name = selected
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Choose a 2:1 paint atlas', '',
            'Images (*.png *.jpg *.jpeg *.bmp *.tga *.tif *.tiff)',
        )
        if not path:
            return
        if QMessageBox.question(
            self, 'Install smart paint image',
            f'Encode and install {name} through the verified native SD/HD mip layout?',
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = StockPaintEditor(self.state.installation).install(name, path)
            self.status.setText(
                f"Installed and verified {result['sd']}"
                + (f" and {result['hd']}" if result.get('hd') else '') + '.'
            )
        except Exception as exc:
            QMessageBox.critical(self, 'Smart paint import failed', str(exc))

    def _restore(self):
        selected = self._selection()
        if not selected:
            return
        key, name = selected
        try:
            result = self._editor().restore_entry(name, key)
            self.status.setText(f"Restored and verified {result['name']} from the pristine backup.")
        except Exception as exc:
            QMessageBox.critical(self, 'Restore failed', str(exc))


class MappingAuditPage(QWidget):
    def __init__(self, state: ApplicationState, parent=None):
        super().__init__(parent)
        self.state = state
        self.pool = QThreadPool.globalInstance()
        self._worker = None
        self._report = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title, subtitle = page_title(
            'Live mapping verification',
            'Checks every cdfiles index, payload boundary, primary-season assets, database, number-card bank, and 3D body mapping against the selected installation.',
        )
        layout.addWidget(title)
        layout.addWidget(subtitle)
        row = QHBoxLayout()
        self.run_button = QPushButton('Verify selected game')
        self.run_button.setObjectName('primary')
        self.save_button = QPushButton('Save report…')
        self.save_button.setEnabled(False)
        row.addWidget(self.run_button)
        row.addWidget(self.save_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(('Archive', 'Entries', 'Layout', 'Bounds'))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.summary = QLabel('Select an installation, then run verification.')
        self.summary.setWordWrap(True)
        self.summary.setObjectName('status')
        layout.addWidget(self.summary)
        self.run_button.clicked.connect(self._run)
        self.save_button.clicked.connect(self._save)

    def _run(self):
        installation = self.state.installation
        if not installation:
            QMessageBox.information(self, 'No game selected', 'Select a game on the Setup page first.')
            return
        self.run_button.setEnabled(False)
        self.summary.setText('Reading every installed archive index…')
        worker = FunctionWorker(audit_installation, installation)
        worker.signals.finished.connect(self._finished)
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.pool.start(worker)

    def _finished(self, report):
        self._report = report
        self.run_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.table.setRowCount(len(report['indexes']))
        for row, item in enumerate(report['indexes']):
            values = (
                item['archive'], f"{item['entries']:,}",
                ', '.join(f'{key}:{value}' for key, value in item['layouts'].items()),
                'OK' if not item['out_of_bounds'] else f"{len(item['out_of_bounds'])} invalid",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        status = 'PASS' if report['ok'] else 'FAIL'
        self.summary.setText(
            f"{status}: {report['facts']['total_entries']:,} resources verified; "
            f"{report['facts']['expected_livery_entries']} primary-season liveries; "
            f"{len(report['facts']['body_models'])} body model mappings."
        )

    def _failed(self, detail: str):
        self.run_button.setEnabled(True)
        self.summary.setText('Verification failed.')
        QMessageBox.critical(self, 'Mapping verification failed', detail)

    def _save(self):
        if not self._report:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Save mapping report',
            f"{self._report['game_id']}_mapping_audit.json", 'JSON (*.json)',
        )
        if path:
            Path(path).write_text(json.dumps(self._report, indent=2) + '\n', encoding='utf-8')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        version_file = Path(__file__).resolve().parents[2] / 'VERSION.txt'
        try:
            window_title = version_file.read_text(encoding='utf-8').splitlines()[0]
        except (OSError, IndexError):
            window_title = 'NASCAR Modding App'
        self.setWindowTitle(window_title)
        self.resize(1220, 780)
        self.state = ApplicationState()

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(242)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 14, 10, 10)
        app_title = QLabel('NASCAR Modding App')
        app_title.setObjectName('appTitle')
        sidebar_layout.addWidget(app_title)
        self.selected_game_label = QLabel('No game selected')
        self.selected_game_label.setObjectName('selectedGame')
        self.selected_game_label.setWordWrap(True)
        sidebar_layout.addWidget(self.selected_game_label)
        self.navigation_search = QLineEdit()
        self.navigation_search.setPlaceholderText('Find a tool…  Ctrl+K')
        self.navigation_search.setClearButtonEnabled(True)
        sidebar_layout.addWidget(self.navigation_search)
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        sidebar_layout.addWidget(self.navigation, 1)
        self.stack = QStackedWidget()
        self.dashboard_page = DashboardPage(self.state)
        self.paint_assets_page = PaintAssetsPage(self.state)
        self.resources_page = ResourcesPage(self.state)
        self.backups_page = BackupsPage(self.state)
        self.career_page = CareerPage(self.state)
        self.full_repair_page = FullRepairPage(self.state)
        self.names_page = NamesPage(self.state)
        self.library_page = LibraryPage(self.state)
        self.managed_paints_page = ManagedPaintsPage(self.state)
        self.text_page = TextPage(self.state)
        self.scr_page = ScrPage(self.state)
        self.support_page = SupportPage(self.state)
        self.pyc_page = PycPage(self.state)
        self.audio_page = AudioPage(self.state)
        self.appdata_page = AppDataPage(self.state)
        self.textures_page = TexturesPage(self.state)
        self.schedule_page = SchedulePage(self.state)
        self.season_packs_page = SeasonPacksPage(self.state)
        self.tracks_page = TracksPage(self.state)
        self.teams_page = TeamsPage(self.state)
        self.ratings_page = RatingsPage(self.state)
        self.cars_page = CarsPage(self.state)
        self.mapping_page = MappingAuditPage(self.state)
        pages = (
            ('Setup', self.dashboard_page),
            ('Paint Assets', self.paint_assets_page),
            ('Managed Paints & AI', self.managed_paints_page),
            ('Archive Resources', self.resources_page),
            ('Backups & Restore', self.backups_page),
            ('NTG2013 Career', self.career_page),
            ('Driver Names', self.names_page),
            ('Interface Text', self.text_page),
            ('Racing Controls', self.scr_page),
            ('Game Database', self.pyc_page),
            ('Audio Banks', self.audio_page),
            ('App Data Transfer', self.appdata_page),
            ('Images & Textures', self.textures_page),
            ('Season Schedule', self.schedule_page),
            ('Composite Season Packs', self.season_packs_page),
            ('Track Files', self.tracks_page),
            ('Teams & Manufacturers', self.teams_page),
            ('AI Ratings', self.ratings_page),
            ('Presets & Pit Notes', self.library_page),
            ('Checkup & Support', self.support_page),
            ('Full Repair', self.full_repair_page),
            ('3D Cars', self.cars_page),
            ('Mapping Audit', self.mapping_page),
        )
        for label, page in pages:
            self.navigation.addItem(QListWidgetItem(label))
            self.stack.addWidget(page)
        root.addWidget(sidebar)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.navigation.currentRowChanged.connect(self._page_changed)
        self.navigation_search.textChanged.connect(self._filter_navigation)
        self.state.installation_changed.connect(self._selected_game_changed)
        self.search_shortcut = QShortcut(QKeySequence('Ctrl+K'), self)
        self.search_shortcut.activated.connect(self._focus_navigation_search)
        saved_page = int(self.state.settings.value('ui/page', 0) or 0)
        self.navigation.setCurrentRow(max(0, min(saved_page, len(pages) - 1)))
        self.setStyleSheet(_STYLE)
        self.statusBar().showMessage('Select an installation on Setup to begin.')

        saved_id = str(self.state.settings.value('last_game', 'nascar15'))
        saved_path = self.state.saved_path(saved_id)
        if saved_path:
            try:
                self.state.select_installation(saved_id, saved_path)
            except Exception:
                pass

    def _page_changed(self, index: int):
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        self.state.settings.setValue('ui/page', index)
        item = self.navigation.item(index)
        if item:
            self.statusBar().showMessage(f'{item.text()} — ready')

    def _filter_navigation(self, query: str):
        wanted = query.strip().casefold()
        first_visible = -1
        for index in range(self.navigation.count()):
            item = self.navigation.item(index)
            visible = not wanted or wanted in item.text().casefold()
            item.setHidden(not visible)
            if visible and first_visible < 0:
                first_visible = index
        current = self.navigation.currentItem()
        if first_visible >= 0 and (current is None or current.isHidden()):
            self.navigation.setCurrentRow(first_visible)

    def _focus_navigation_search(self):
        self.navigation_search.setFocus()
        self.navigation_search.selectAll()

    def _selected_game_changed(self, installation: GameInstallation):
        self.selected_game_label.setText(
            f'{installation.profile.name}\n{installation.root}'
        )
        self.selected_game_label.setToolTip(str(installation.root))
        self.statusBar().showMessage(
            f'{installation.profile.name} ready — '
            f'{len(installation.archive_pairs)} archives found.'
        )


_STYLE = """
QMainWindow, QWidget { background: #111821; color: #e8edf3; font-size: 13px; }
#sidebar { background: #0b1118; border-right: 1px solid #263545; }
#appTitle { font-size: 17px; font-weight: 700; padding: 3px 5px; }
#selectedGame { color: #9eabb9; padding: 2px 5px 8px 5px; }
#navigation { background: #0b1118; border: 0; padding: 6px 0; font-size: 14px; }
#navigation::item { padding: 12px 14px; margin: 2px 0; border-radius: 6px; }
#navigation::item:selected { background: #1c6dd0; color: white; }
#card { background: #18222e; border: 1px solid #293849; border-radius: 9px; }
#subtitle { color: #9eabb9; }
#status { color: #b9c8d8; background: #18222e; padding: 10px; border-radius: 6px; }
QLineEdit, QComboBox, QTextEdit, QTableWidget { background: #0d141c; border: 1px solid #34475b; border-radius: 5px; padding: 7px; }
QPushButton { background: #253446; border: 1px solid #3b5067; border-radius: 5px; padding: 8px 12px; }
QPushButton:hover { background: #30445b; }
QPushButton#primary { background: #1c6dd0; border-color: #287ee6; color: white; font-weight: 600; }
QPushButton:disabled { color: #687483; background: #1a232d; }
QHeaderView::section { background: #1b2734; color: #dbe5ee; padding: 7px; border: 0; }
"""
