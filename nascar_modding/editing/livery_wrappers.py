"""NASCAR 15 native page-mapped SD/HD livery wrapper writer."""

from __future__ import annotations

import struct
import numpy as np
from PIL import Image, ImageChops

from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.games.installation import GameInstallation


RAW_OFFSET = 0x100
SD_ENTRY_SIZE = 0x164161
HD_ENTRY_SIZE = 0x564161

SD_OFFSETS = (
    0x000000, 0x100000, 0x140000, 0x150000, 0x154000, 0x156000,
    0x158000, 0x15A000, 0x15C000, 0x15E000, 0x160000, 0x162000,
)
SD_PITCHES = (
    0x1000, 0x0800, 0x0400, 0x0200, 0x0100, 0x0100,
    0x0100, 0x0100, 0x0100, 0x0100, 0x0100, 0x0100,
)
SD_DIMS = (
    (2048, 1024), (1024, 512), (512, 256), (256, 128),
    (128, 64), (64, 32), (32, 16), (16, 8),
    (8, 4), (4, 2), (2, 1), (1, 1),
)
SD_ROLLS = (0, -10, -15, -18, -19)

HD_OFFSETS = (
    0x000000, 0x400000, 0x500000, 0x540000, 0x550000, 0x554000,
    0x556000, 0x558000, 0x55A000, 0x55C000, 0x55E000, 0x560000,
    0x562000,
)
HD_PITCHES = (
    0x2000, 0x1000, 0x0800, 0x0400, 0x0200, 0x0100, 0x0100,
    0x0100, 0x0100, 0x0100, 0x0100, 0x0100, 0x0100,
)
HD_DIMS = (
    (4096, 2048), (2048, 1024), (1024, 512), (512, 256),
    (256, 128), (128, 64), (64, 32), (32, 16),
    (16, 8), (8, 4), (4, 2), (2, 1), (1, 1),
)
HD_ROLLS = (0, -10, -15, -18, -19, -20)


class NativeLiveryWrapperEditor:
    """Generate the exact full-image wrapper layout used by added paints."""

    def __init__(self, installation: GameInstallation):
        if installation.profile.id != 'nascar15':
            raise ValueError('native page-mapped livery wrappers currently apply only to NASCAR 15')
        self.encoder = TextureBankEditor(installation)

    @staticmethod
    def _validate(wrapper: bytes, size: int, offsets: tuple, pitches: tuple) -> None:
        if len(wrapper) != size:
            raise ValueError(f'livery entry is {len(wrapper):#x}; expected {size:#x}')
        if wrapper[:4] != b'ARCC' or wrapper[0xCC:0xD0] != b'DXT1':
            raise ValueError('livery wrapper does not have the expected ARCC/DXT1 header')
        table = len(wrapper) - 0x89
        if struct.unpack_from(f'<{len(offsets)}I', wrapper, table) != offsets:
            raise ValueError('unexpected native mip offset table')
        if struct.unpack_from(f'<{len(pitches)}I', wrapper, table + len(offsets) * 4) != pitches:
            raise ValueError('unexpected native mip pitch table')

    def _patch(
        self, pristine: bytes, image: Image.Image, *, size: int, offsets: tuple,
        pitches: tuple, dimensions: tuple, rolls: tuple, terminal_level: int,
        direct_through: int, atlas_roll: int = 0, layer_alpha: Image.Image | None = None,
    ) -> tuple[bytes, list[str], int]:
        self._validate(pristine, size, offsets, pitches)
        output = bytearray(pristine)
        box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
        base = image.convert('RGB')
        if base.size != dimensions[0]:
            base = base.resize(dimensions[0], box)
        if atlas_roll:
            base = ImageChops.offset(base, atlas_roll, 0)
        alpha = layer_alpha.convert('L') if layer_alpha is not None else None
        if alpha is not None and alpha.size != dimensions[0]:
            alpha = alpha.resize(dimensions[0], box)
        levels = []
        for level in range(terminal_level):
            width, height = dimensions[level]
            mip = base if level == 0 else base.resize((width, height), box)
            mask = alpha if alpha is None or level == 0 else alpha.resize((width, height), box)
            if level < len(rolls) and rolls[level]:
                mip = ImageChops.offset(mip, rolls[level], 0)
                if mask is not None:
                    mask = ImageChops.offset(mask, rolls[level], 0)
            encoded = self.encoder.encode_surface(mip, 'DXT1')
            blocks_w, blocks_h = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
            needed = blocks_w * blocks_h * 8
            encoded = encoded[:needed] + bytes(max(0, needed - len(encoded)))
            touched = None
            if mask is not None:
                pixels = np.asarray(mask, dtype=np.uint8)
                padded = np.zeros((blocks_h * 4, blocks_w * 4), dtype=np.uint8)
                padded[:height, :width] = pixels[:height, :width]
                touched = padded.reshape(blocks_h, 4, blocks_w, 4).max(axis=(1, 3)) > 2
            base_offset, pitch = RAW_OFFSET + offsets[level], pitches[level]
            for y in range(blocks_h):
                for x in range(blocks_w):
                    if touched is not None and not touched[y, x]:
                        continue
                    dx, dy = (x, y) if level <= direct_through else ((x - 5) % 32, (y - 1) % 32)
                    destination = base_offset + dy * pitch + dx * 8
                    end = destination + 8
                    if destination < RAW_OFFSET or end > RAW_OFFSET + offsets[terminal_level]:
                        raise ValueError(f'L{level} write exceeds its native texture pages')
                    source = (y * blocks_w + x) * 8
                    output[destination:end] = encoded[source:source + 8]
            levels.append(f'L{level}:{width}x{height}/{blocks_w * blocks_h} blocks')
        terminal = RAW_OFFSET + offsets[terminal_level]
        if output[terminal:] != pristine[terminal:] or len(output) != len(pristine):
            raise ValueError('terminal mip/footer changed; install refused')
        changed = sum(left != right for left, right in zip(pristine, output))
        return bytes(output), levels, changed

    def patch_sd(
        self, pristine: bytes, image: Image.Image, layer_alpha: Image.Image | None = None,
    ) -> tuple[bytes, list[str], int]:
        return self._patch(
            pristine, image, size=SD_ENTRY_SIZE, offsets=SD_OFFSETS,
            pitches=SD_PITCHES, dimensions=SD_DIMS, rolls=SD_ROLLS,
            terminal_level=11, direct_through=4, layer_alpha=layer_alpha,
        )

    def patch_hd(
        self, pristine: bytes, image: Image.Image, *, stock_atlas_alignment: bool = False,
    ) -> tuple[bytes, list[str], int]:
        return self._patch(
            pristine, image, size=HD_ENTRY_SIZE, offsets=HD_OFFSETS,
            pitches=HD_PITCHES, dimensions=HD_DIMS, rolls=HD_ROLLS,
            terminal_level=12, direct_through=5,
            atlas_roll=20 if stock_atlas_alignment else 0,
        )
