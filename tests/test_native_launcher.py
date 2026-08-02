from pathlib import Path
import os
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NativeLauncherTests(unittest.TestCase):
    def test_default_launcher_runs_native_entrypoint(self):
        launcher = (ROOT / 'START_APP.bat').read_text(encoding='utf-8')
        self.assertIn('%PYTHON_CMD% native_app.py', launcher)
        self.assertIn('if /I "%~1"=="--legacy" goto legacy', launcher)

    def test_launcher_prefers_packaged_executable(self):
        launcher = (ROOT / 'START_APP.bat').read_text(encoding='utf-8')
        self.assertIn('NASCARModdingApp.exe', launcher)
        self.assertIn('goto find_python', launcher)

    def test_python_version_is_probed_without_parsing_version_text(self):
        launcher = (ROOT / 'START_APP.bat').read_text(encoding='utf-8')
        probe = (ROOT / 'FIND_PYTHON.bat').read_text(encoding='utf-8')
        installer = (ROOT / 'INSTALL_DEPENDENCIES.bat').read_text(encoding='utf-8')
        self.assertIn('FIND_PYTHON.bat', launcher)
        self.assertIn('FIND_PYTHON.bat', installer)
        self.assertIn('sys.version_info < (3, 10)', probe)
        self.assertNotIn('set "PYEXE=', launcher)

    def test_legacy_launcher_is_only_a_shared_bootstrap_alias(self):
        launcher = (ROOT / 'START_LEGACY_WEB_APP.bat').read_text(encoding='utf-8')
        self.assertIn('START_APP.bat" --legacy', launcher)
        self.assertNotIn('app.py', launcher)

    def test_windows_packaging_is_automated_and_smoke_tested(self):
        spec = (ROOT / 'NASCARModdingApp.spec').read_text(encoding='utf-8')
        workflow = (ROOT / '.github' / 'workflows' / 'build-windows-exe.yml').read_text(
            encoding='utf-8'
        )
        self.assertIn("data_tree('data')", spec)
        self.assertIn("data_tree('internal_tools')", spec)
        self.assertIn("python-version: '3.13'", workflow)
        self.assertIn('NASCARModdingApp.exe', workflow)
        self.assertIn('--smoke-test', workflow)
        self.assertIn('actions/upload-artifact@v4', workflow)

    def test_native_car_page_exposes_manufacturer_selection(self):
        source = (ROOT / 'nascar_modding' / 'ui' / 'main_window.py').read_text(
            encoding='utf-8'
        )
        self.assertIn("QLabel('Manufacturer')", source)
        self.assertIn('manufacturer_combo.activated.connect', source)
        self.assertIn('alternative=int(alternative)', source)
        self.assertIn('resolve_game_materials', source)
        self.assertIn("QCheckBox('Use game materials')", source)
        self.assertIn("QPushButton('Use installed paint')", source)

    def test_native_navigation_is_searchable_and_persistent(self):
        source = (ROOT / 'nascar_modding' / 'ui' / 'main_window.py').read_text(
            encoding='utf-8'
        )
        self.assertIn("QKeySequence('Ctrl+K')", source)
        self.assertIn("self.state.settings.setValue('ui/page', index)", source)
        self.assertIn('self.navigation_search.textChanged.connect', source)

    def test_texture_preview_ignores_source_image_dimensions(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QApplication, QSizePolicy
        from nascar_modding.ui.textures_page import TexturePreview, TexturesPage

        class EmptyState(QObject):
            installation_changed = Signal(object)
            installation = None

        app = QApplication.instance() or QApplication([])
        preview = TexturePreview()
        hint = preview.sizeHint()
        preview.set_texture(QPixmap(4096, 2048))
        self.assertEqual(hint, preview.sizeHint())
        self.assertEqual(
            QSizePolicy.Policy.Ignored,
            preview.sizePolicy().horizontalPolicy(),
        )
        preview.close()
        page = TexturesPage(EmptyState())
        page.resize(900, 650)
        page.show()
        app.processEvents()
        before = page.size()
        page.preview.set_texture(QPixmap(4096, 2048))
        app.processEvents()
        self.assertEqual(before, page.size())
        self.assertLessEqual(page.minimumSizeHint().width(), 900)
        page.close()
        app.processEvents()


if __name__ == '__main__':
    unittest.main()
