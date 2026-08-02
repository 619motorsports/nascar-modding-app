"""Shared decoded texture-bank inspection and guarded image replacement."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageOps

import containers
from nascar_modding.formats.dxt import encode_dxt1
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.games.installation import GameInstallation


def prepare_image(image: Image.Image, size: tuple[int, int], mode: str = 'fit') -> Image.Image:
    target = tuple(map(int, size))
    image = image.convert('RGBA')
    if mode == 'stretch':
        return image.resize(target, Image.Resampling.LANCZOS)
    if mode == 'fill':
        return ImageOps.fit(image, target, Image.Resampling.LANCZOS)
    if mode not in ('fit', 'nearest'):
        raise ValueError(f'unsupported resize mode: {mode}')
    resample = Image.Resampling.NEAREST if mode == 'nearest' else Image.Resampling.LANCZOS
    contained = ImageOps.contain(image, target, resample)
    output = Image.new('RGBA', target, (0, 0, 0, 0))
    output.alpha_composite(contained, ((target[0] - contained.width) // 2, (target[1] - contained.height) // 2))
    return output


class TextureBankEditor:
    decoder_revision = containers.TEXTURE_DECODER_REVISION

    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.resources = ResourceEditor(installation)

    def containers(self, archive_key: str, query: str = '') -> list[dict]:
        wanted = query.strip().casefold()
        rows = []
        for entry in self.installation.entries(str(archive_key)):
            upper = entry.name.upper()
            if not upper.endswith('.ARC'):
                continue
            if wanted and wanted not in entry.name.casefold():
                continue
            if not any(word in upper for word in (
                'MENU', 'IMAGE', 'TEXTURE', 'THUMB', 'PAINT', 'DRIVER',
                'NUM', 'LOGO', 'HUD', 'MAP_', 'TEAMSHOP', 'CALENDAR',
            )):
                continue
            rows.append({'archive': str(archive_key), 'name': entry.name, 'size': entry.size})
        return sorted(rows, key=lambda row: row['name'].casefold())

    def _known_dimensions(self, container_name: str):
        if container_name.casefold() == self.installation.profile.number_container.casefold():
            return (128, 64)
        return None

    def entries(self, archive_key: str, container_name: str, *, pristine: bool = False) -> list[dict]:
        payload = self.resources.read(container_name, archive_key, pristine=pristine)
        entries, _base = containers.parse_multi_arc(payload, known_dims=self._known_dimensions(container_name))
        return [{
            'archive': str(archive_key), 'container': container_name,
            'name': entry['name'], 'width': entry['w'], 'height': entry['h'],
            'format': entry['fmt'], 'payload_size': entry['payload_size'],
            'payload_offset': entry['payload_abs'],
            'row_pitch': entry.get('row_pitch'),
            'stored_width': entry.get('stored_width'),
            'stored_height': entry.get('stored_height'),
            'surface_layout': entry.get('surface_layout'),
            'replace_supported': self.replace_supported(
                container_name, entry['name'], entry['fmt'],
            ),
        } for entry in entries if entry['w'] > 0 and entry['h'] > 0]

    def _resolve(self, archive_key: str, container_name: str, entry_name: str, *, pristine=False):
        payload = self.resources.read(container_name, archive_key, pristine=pristine)
        entries, _base = containers.parse_multi_arc(payload, known_dims=self._known_dimensions(container_name))
        matches = [entry for entry in entries if entry['name'] == entry_name]
        if len(matches) != 1:
            raise ValueError(f'{container_name} does not contain exactly one {entry_name}')
        return payload, matches[0]

    def read_image(self, archive_key: str, container_name: str, entry_name: str, *, pristine=False) -> Image.Image:
        payload, entry = self._resolve(archive_key, container_name, entry_name, pristine=pristine)
        return containers.multi_read_png(payload, entry)

    def export_png(self, archive_key: str, container_name: str, entry_name: str, destination: str | Path) -> Path:
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.read_image(archive_key, container_name, entry_name).save(output, 'PNG')
        return output

    def image_png(self, archive_key: str, container_name: str, entry_name: str, *, pristine=False) -> bytes:
        buffer = BytesIO()
        self.read_image(archive_key, container_name, entry_name, pristine=pristine).save(buffer, 'PNG')
        return buffer.getvalue()

    def raw_entry(self, archive_key: str, container_name: str, entry_name: str,
                  *, pristine=False) -> dict:
        payload, entry = self._resolve(
            archive_key, container_name, entry_name, pristine=pristine,
        )
        start, size = int(entry['payload_abs']), int(entry['payload_size'])
        return {
            'payload': bytes(payload[start:start + size]),
            'width': int(entry['w']), 'height': int(entry['h']),
            'format': str(entry['fmt']), 'payload_size': size,
        }

    def replace_raw_entry(self, archive_key: str, container_name: str,
                          entry_name: str, raw: bytes) -> dict:
        payload, entry = self._resolve(archive_key, container_name, entry_name)
        replacement = bytes(raw)
        if len(replacement) != int(entry['payload_size']):
            raise ValueError('texture payload size no longer matches the pack')
        rebuilt = bytearray(payload)
        start = int(entry['payload_abs'])
        rebuilt[start:start + len(replacement)] = replacement
        candidate = bytes(rebuilt)
        parsed, _base = containers.parse_multi_arc(
            candidate, known_dims=self._known_dimensions(container_name),
        )
        check = next((row for row in parsed if row['name'] == entry_name), None)
        if check is None:
            raise ValueError('texture entry vanished during raw replacement')
        containers.multi_read_png(candidate, check)
        result = self.resources.replace(container_name, archive_key, candidate)
        live = self.raw_entry(archive_key, container_name, entry_name)
        if live['payload'] != replacement:
            raise IOError('texture raw read-back failed')
        return {'archive': str(archive_key), 'container': container_name,
                'entry': entry_name, 'verified': True, 'write': result}

    def replace_supported(
        self, container_name: str, entry_name: str, format_name: str | None = None,
    ) -> bool:
        if str(format_name or '').upper() == 'A1R5G5B5':
            return False
        if entry_name.upper().startswith('DRIVERPAINT'):
            return True
        return container_name.casefold() == self.installation.profile.number_container.casefold()

    def _texconv(self, image: Image.Image, format_name: str) -> bytes | None:
        root = Path(__file__).resolve().parents[2]
        executable = root / 'internal_tools' / 'texconv.exe'
        if not executable.is_file():
            executable = root / 'texconv.exe'
        if not executable.is_file():
            return None
        with tempfile.TemporaryDirectory(prefix='nascar_texture_') as folder:
            source = Path(folder) / 'source.png'
            image.save(source, 'PNG')
            process = subprocess.run(
                [str(executable), '-y', '-ft', 'dds', '-dx9', '-f', format_name, '-m', '1', '-o', folder, str(source)],
                capture_output=True, check=False,
            )
            output = Path(folder) / 'source.dds'
            if process.returncode != 0 or not output.is_file():
                return None
            return output.read_bytes()[128:]

    def _encode(self, image: Image.Image, format_name: str) -> bytes:
        source = image.convert('RGB' if format_name == 'DXT1' else 'RGBA')
        encoded = self._texconv(source, format_name)
        if encoded is not None:
            return encoded
        if format_name == 'DXT5':
            return containers.dxt5_encode(np.asarray(image.convert('RGBA')))
        if format_name == 'DXT1':
            pixels = np.asarray(image.convert('RGB'))
            height, width = pixels.shape[:2]
            padded_height, padded_width = max(4, (height + 3) // 4 * 4), max(4, (width + 3) // 4 * 4)
            if (padded_height, padded_width) != (height, width):
                padded = np.zeros((padded_height, padded_width, 3), np.uint8)
                padded[:height, :width] = pixels
                pixels = padded
            return encode_dxt1(pixels)
        raise RuntimeError(f'unsupported texture encoder: {format_name}')

    def encode_surface(self, image: Image.Image, format_name: str) -> bytes:
        """Encode one texture surface through the shared managed encoder."""
        if str(format_name).upper() == 'DXT1' and min(image.size) < 4:
            pixels = np.asarray(image.convert('RGB'))
            height, width = pixels.shape[:2]
            padded = np.zeros((max(4, height), max(4, width), 3), np.uint8)
            padded[:height, :width] = pixels
            return encode_dxt1(padded)
        return self._encode(image, str(format_name).upper())

    def replace_png(
        self, archive_key: str, container_name: str, entry_name: str,
        source: str | Path, *, resize_mode: str = 'fit', experimental: bool = False,
    ) -> dict:
        return self.replace_image(
            archive_key, container_name, entry_name, Image.open(source),
            resize_mode=resize_mode, experimental=experimental,
        )

    def replace_image(
        self, archive_key: str, container_name: str, entry_name: str,
        image: Image.Image, *, resize_mode: str = 'fit', experimental: bool = False,
    ) -> dict:
        payload, entry = self._resolve(archive_key, container_name, entry_name)
        if not experimental and not self.replace_supported(
            container_name, entry_name, entry['fmt'],
        ):
            raise ValueError(
                'decoded replacement is not proven safe for this texture family; '
                'use raw copy/export or explicitly enable experimental replacement'
            )
        image = prepare_image(image, (entry['w'], entry['h']), resize_mode)
        rebuilt = containers.multi_write_png_validated(
            payload, entry, image, encode_fn=self._encode,
            known_dims=self._known_dimensions(container_name),
        )
        result = self.resources.replace(container_name, archive_key, rebuilt)
        check = self.read_image(archive_key, container_name, entry_name)
        if check.size != (entry['w'], entry['h']):
            raise IOError('installed texture failed decoded read-back')
        return {
            'archive': str(archive_key), 'container': container_name,
            'entry': entry_name, 'width': entry['w'], 'height': entry['h'],
            'format': entry['fmt'], 'verified': True, 'write': result,
        }

    def copy_entry(
        self, source_archive: str, source_container: str, source_entry: str,
        destination_archive: str, destination_container: str, destination_entry: str,
    ) -> dict:
        source_payload, source = self._resolve(source_archive, source_container, source_entry)
        destination_payload, destination = self._resolve(destination_archive, destination_container, destination_entry)
        if (source['fmt'], source['payload_size']) != (destination['fmt'], destination['payload_size']):
            raise ValueError('source and destination texture formats/sizes do not match')
        raw = source_payload[source['payload_abs']:source['payload_abs'] + source['payload_size']]
        rebuilt = bytearray(destination_payload)
        start = destination['payload_abs']
        rebuilt[start:start + destination['payload_size']] = raw
        containers.parse_multi_arc(bytes(rebuilt), known_dims=self._known_dimensions(destination_container))
        result = self.resources.replace(destination_container, destination_archive, bytes(rebuilt))
        return {'verified': True, 'entry': destination_entry, 'write': result}

    def restore_entry(self, archive_key: str, container_name: str, entry_name: str) -> dict:
        stock_payload, stock = self._resolve(archive_key, container_name, entry_name, pristine=True)
        live_payload, live = self._resolve(archive_key, container_name, entry_name)
        if (stock['fmt'], stock['payload_size']) != (live['fmt'], live['payload_size']):
            raise ValueError('pristine and live texture layouts do not match')
        rebuilt = bytearray(live_payload)
        raw = stock_payload[stock['payload_abs']:stock['payload_abs'] + stock['payload_size']]
        rebuilt[live['payload_abs']:live['payload_abs'] + live['payload_size']] = raw
        containers.parse_multi_arc(bytes(rebuilt), known_dims=self._known_dimensions(container_name))
        result = self.resources.replace(container_name, archive_key, bytes(rebuilt))
        return {'verified': True, 'entry': entry_name, 'write': result}
