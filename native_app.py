#!/usr/bin/env python3
"""Native desktop entry point for the NASCAR Modding App."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


SMOKE_TEST = '--smoke-test' in sys.argv
if SMOKE_TEST:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from nascar_modding.ui.main_window import MainWindow


def _smoke_check(window: MainWindow) -> bool:
    """Exercise bundled resources and dynamically loaded editing backends."""
    root = Path(__file__).resolve().parent
    required = (
        root / 'data' / 'nascar13',
        root / 'data' / 'nascar14',
        root / 'internal_tools' / 'texconv.exe',
    )
    if window.stack.count() < 1 or not all(path.exists() for path in required):
        return False

    from nascar_modding.editing.managed_paints import ManagedPaintEditor
    from nascar_modding.editing.pyc_records import _mapper_module
    from nascar_modding.editing.schedule import _schedule_helper, _schedule_link_helper
    from nascar_modding.editing.team_presentation import TeamPresentationEditor
    from nascar_modding.editing.teams import TeamEditor

    ManagedPaintEditor._backend()
    ManagedPaintEditor._fixed_backend()
    ManagedPaintEditor._thumbnail_backend()
    _mapper_module()
    _schedule_helper()
    _schedule_link_helper()
    TeamEditor._backend()
    TeamPresentationEditor._backend()

    # Exercise the packaged allocation-row decoder, not merely its import.
    import struct
    import containers

    if containers.TEXTURE_DECODER_REVISION != 'aligned-rows-v4':
        return False
    padded_layout = containers._surface_storage(64, 32, 'DXT5', 4096, 1)
    small_mip_layout = containers._surface_storage(64, 64, 'DXT1', 57344, 7)
    wide_layout = containers._surface_storage(512, 32, 'DXT1', 32768, 1)
    mip_layout = containers._surface_storage(2048, 1024, 'DXT1', 1458176, 12)
    if padded_layout['surface_layout'] != 'padded_rows' or padded_layout['row_pitch'] != 512:
        return False
    if mip_layout['surface_layout'] != 'tight_base' or mip_layout['row_pitch'] != 4096:
        return False
    if small_mip_layout['surface_layout'] != 'padded_rows' or small_mip_layout['row_pitch'] != 256:
        return False
    if wide_layout['surface_layout'] != 'tight_base' or wide_layout['row_pitch'] != 1024:
        return False
    a8_payload = (
        bytes((0, 0, 255, 255)) + b'padding-padding'[:12]
        + bytes((255, 0, 0, 255)) + b'padding-padding'[:12]
    )
    a8_entry = {
        'payload_abs': 0, 'payload_size': len(a8_payload), 'needed': 8,
        'w': 1, 'h': 2, 'fmt': 'A8R8G8B8', 'row_pitch': 16,
    }
    a8_image = containers.multi_read_png(a8_payload, a8_entry)
    if a8_image.getpixel((0, 0)) != (255, 0, 0, 255):
        return False
    if a8_image.getpixel((0, 1)) != (0, 0, 255, 255):
        return False

    def bc3_solid(rgb565):
        return b'\xff\xff' + b'\0' * 6 + struct.pack('<HHI', rgb565, rgb565, 0)

    bc3_payload = (
        bc3_solid(0xF800) + bc3_solid(0x001F)
        + bc3_solid(0x07E0) + bc3_solid(0x001F)
    )
    bc3_entry = {
        'payload_abs': 0, 'payload_size': len(bc3_payload), 'needed': 32,
        'w': 4, 'h': 8, 'fmt': 'DXT5', 'row_pitch': 32,
        'dxt5_swapped': False,
    }
    bc3_image = containers.multi_read_png(bc3_payload, bc3_entry)
    if bc3_image.getpixel((0, 0))[0] < 240 or bc3_image.getpixel((0, 7))[1] < 240:
        return False
    return True


def main() -> int:
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setApplicationName('NASCAR Modding App')
    app.setOrganizationName('NASCARModdingApp')
    window = MainWindow()
    if SMOKE_TEST:
        error_path = (
            Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else Path.cwd()
        ) / 'smoke-test-error.txt'
        try:
            error_path.unlink(missing_ok=True)
            app.processEvents()
            passed = _smoke_check(window)
            window.close()
            return 0 if passed else 2
        except BaseException:
            error_path.write_text(traceback.format_exc(), encoding='utf-8')
            window.close()
            return 3
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
