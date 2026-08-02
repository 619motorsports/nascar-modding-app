"""Driver-name editor independent of Flask or Qt."""

from __future__ import annotations

import json
from pathlib import Path
import struct

import containers as container_formats

from nascar_modding.editing.archive import ArchiveEntryEditor
from nascar_modding.core.files import atomic_write_json
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.editing.text_tables import LanguageTableStore
from nascar_modding.formats.python2_pyc import root_layout
from nascar_modding.games.installation import GameInstallation


class DriverNameEditor:
    def __init__(self, installation: GameInstallation, data_root: str | Path | None = None):
        self.installation = installation
        root = Path(data_root) if data_root else Path(__file__).resolve().parents[2] / 'data'
        subdir = installation.profile.data_subdir
        self.data_dir = root / subdir if subdir else root
        self.archive_editor = ArchiveEntryEditor(installation)
        self.text_store = LanguageTableStore(installation)

    def _links(self) -> list[dict]:
        value = json.loads((self.data_dir / 'drivers.json').read_text(encoding='utf-8'))
        if not isinstance(value, list):
            raise ValueError('drivers.json is not a list')
        return value

    def _text_blobs(self, pristine: bool) -> dict[str, bytes]:
        return self.text_store.blobs(pristine)

    @staticmethod
    def _find(blobs: dict[str, bytes], candidates: list[str]):
        for candidate in candidates:
            wanted = str(candidate).strip().casefold()
            if not wanted:
                continue
            matches = []
            for name, blob in blobs.items():
                for entry in container_formats.lda_entries(blob):
                    text = entry['raw'].decode('latin1', 'replace')
                    if text.strip().casefold() == wanted:
                        matches.append((name, int(entry['index']), text))
            if matches:
                return matches
        return []

    def drivers(self) -> list[dict]:
        stock_blobs = self._text_blobs(True)
        live_blobs = self._text_blobs(False)
        rows = []
        for link in self._links():
            display = str(link.get('display_name') or '').strip()
            candidates = [display, *(link.get('name_candidates') or [])]
            matches = self._find(stock_blobs, candidates)
            available = bool(matches)
            current = display
            targets = []
            if available:
                current_values = []
                for source, index, stock_text in matches:
                    peers = {int(item['index']): item for item in container_formats.lda_entries(live_blobs[source])}
                    if index not in peers:
                        available = False
                        break
                    targets.append({'source': source, 'index': index})
                    current_values.append(peers[index]['raw'].decode('latin1', 'replace').rstrip())
                    display = stock_text
                if available and len(set(current_values)) == 1:
                    current = current_values[0]
                else:
                    available = False
            rows.append({
                'driver_uid': int(link['driver_uid']),
                'number': str(link.get('number') or ''),
                'original': display,
                'current': current,
                'available': available,
                'targets': targets,
                'reason': '' if available else f'{len(matches)} language-table targets did not resolve consistently',
            })
        return sorted(rows, key=lambda row: (
            int(row['number']) if row['number'].isdigit() else 9999,
            row['current'].casefold(),
        ))

    def rename(self, driver_uid: int, new_name: str) -> dict:
        text = str(new_name).strip()
        if not text:
            raise ValueError('driver name cannot be empty')
        if '\0' in text:
            raise ValueError('driver name cannot contain a NUL character')
        try:
            encoded = text.encode('latin1')
        except UnicodeEncodeError as exc:
            raise ValueError('driver name contains characters unsupported by the game') from exc
        if len(encoded) > 255:
            raise ValueError('driver name is too long for a game menu string')
        row = next((item for item in self.drivers() if item['driver_uid'] == int(driver_uid)), None)
        if not row or not row['available']:
            raise ValueError('driver does not have one verified language-table target')
        if row['current'] == text:
            return {
                'driver_uid': int(driver_uid),
                'current': text,
                'tables': 0,
                'verified': True,
                'writes': [],
            }
        live_blobs = self._text_blobs(False)
        grouped = {}
        for target in row['targets']:
            grouped.setdefault(target['source'], {})[target['index']] = encoded
        rebuilt = {}
        for source, replacements in grouped.items():
            payload, changed = container_formats.lda_rebuild_indices(live_blobs[source], replacements)
            if changed != len(replacements):
                raise ValueError(f'{source} rebuild changed {changed}/{len(replacements)} strings')
            rebuilt[source] = payload
        installed = []
        try:
            for source, payload in rebuilt.items():
                result = self.archive_editor.replace_or_repoint_entry(source, payload, '0')
                installed.append((source, result))
            verified = next(item for item in self.drivers() if item['driver_uid'] == int(driver_uid))
            if verified['current'] != text:
                raise IOError('renamed driver did not pass live read-back verification')
        except Exception as install_error:
            rollback_errors = []
            for source, _result in reversed(installed):
                try:
                    self.archive_editor.replace_or_repoint_entry(source, live_blobs[source], '0')
                except Exception as exc:
                    rollback_errors.append(f'{source}: {exc}')
            if rollback_errors:
                raise RuntimeError(
                    f'name install failed ({install_error}) and rollback failed: '
                    + '; '.join(rollback_errors)
                ) from install_error
            try:
                rolled_back = next(
                    item for item in self.drivers()
                    if item['driver_uid'] == int(driver_uid)
                )
                if rolled_back['current'] != row['current']:
                    raise IOError('previous driver name was not restored')
            except Exception as rollback_verify_error:
                raise RuntimeError(
                    f'name install failed ({install_error}) and rollback verification failed: '
                    f'{rollback_verify_error}'
                ) from install_error
            raise
        return {
            'driver_uid': int(driver_uid),
            'current': text,
            'tables': len(installed),
            'verified': True,
            'writes': [result for _source, result in installed],
        }

    def restore(self, driver_uid: int) -> dict:
        row = next((item for item in self.drivers() if item['driver_uid'] == int(driver_uid)), None)
        if not row:
            raise ValueError('driver was not found')
        return self.rename(driver_uid, row['original'])


class DriverHandleEditor:
    """Length-changing driver-card handle editor with persistent identity links."""

    ENTRY = 'DB_GAME_LOCAL_SCRIPT.PYC'

    def __init__(self, installation: GameInstallation, config_path: str | Path,
                 data_root: str | Path | None = None):
        self.installation = installation
        self.config_path = Path(config_path)
        root = Path(data_root) if data_root else Path(__file__).resolve().parents[2] / 'data'
        subdir = installation.profile.data_subdir
        self.data_dir = root / subdir if subdir else root
        self.resources = ResourceEditor(installation)

    def _links(self) -> list[dict]:
        value = json.loads((self.data_dir / 'drivers.json').read_text(encoding='utf-8'))
        return value if isinstance(value, list) else []

    def _config(self) -> dict:
        try:
            value = json.loads(self.config_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _marker(payload: bytes, value: bytes) -> bool:
        return any(
            payload.find(bytes((tag,)) + struct.pack('<i', len(value)) + value) >= 0
            for tag in (ord('s'), ord('t'), ord('u'), ord('s') | 0x80,
                        ord('t') | 0x80, ord('u') | 0x80)
        )

    @staticmethod
    def _rebuild(payload: bytes, old: bytes, new: bytes) -> tuple[bytes, int]:
        hits = []
        for tag in (ord('s'), ord('t'), ord('u')):
            for encoded_tag in (tag, tag | 0x80):
                needle = bytes((encoded_tag,)) + struct.pack('<i', len(old)) + old
                start = 0
                while True:
                    position = payload.find(needle, start)
                    if position < 0:
                        break
                    hits.append((position, len(needle)))
                    start = position + 1
        if not hits:
            raise ValueError('current handle was not found as a game-data string')
        rebuilt = bytearray(payload)
        for position, total in sorted(set(hits), reverse=True):
            rebuilt[position + 1:position + 5] = struct.pack('<i', len(new))
            rebuilt[position + 5:position + total] = new
        result = bytes(rebuilt)
        root_layout(result)
        return result, len(set(hits))

    def handles(self) -> list[dict]:
        payload = self.resources.read(self.ENTRY, '0')
        aliases = self._config().get('handles', {})
        aliases = aliases if isinstance(aliases, dict) else {}
        rows = []
        for link in self._links():
            original = str(link.get('handle') or '').strip().lstrip('@')
            if not original:
                continue
            current = str(aliases.get(original, original)).rstrip('_ ')
            try:
                encoded = current.encode('latin1')
            except UnicodeEncodeError:
                encoded = b''
            rows.append({
                'driver_uid': int(link['driver_uid']), 'number': str(link.get('number') or ''),
                'original': original, 'current': current,
                'available': bool(encoded and self._marker(payload, encoded)),
            })
        return rows

    def rename(self, driver_uid: int, new_handle: str) -> dict:
        row = next((item for item in self.handles()
                    if int(item['driver_uid']) == int(driver_uid)), None)
        if row is None or not row['available']:
            raise ValueError('driver handle does not have one verified game-data target')
        cleaned = str(new_handle).strip().lstrip('@')
        if not cleaned or '\0' in cleaned:
            raise ValueError('handle must be non-empty and cannot contain NUL')
        try:
            old, new = row['current'].encode('latin1'), cleaned.encode('latin1')
        except UnicodeEncodeError as exc:
            raise ValueError('handle contains characters unsupported by the game') from exc
        if len(new) > 255:
            raise ValueError('handle is too long for a game menu identifier')
        if old == new:
            return {'ok': True, 'verified': True, 'driver_uid': int(driver_uid),
                    'current': cleaned, 'patched': 0}
        before = self.resources.read(self.ENTRY, '0')
        rebuilt, patched = self._rebuild(before, old, new)
        try:
            write = self.resources.replace(self.ENTRY, '0', rebuilt)
            live = self.resources.read(self.ENTRY, '0')
            root_layout(live)
            if not self._marker(live, new):
                raise IOError('handle marker failed live read-back')
        except Exception as exc:
            try:
                self.resources.replace(self.ENTRY, '0', before)
            except Exception as rollback:
                raise RuntimeError(f'handle install failed ({exc}); rollback failed: {rollback}') from exc
            raise
        config = self._config()
        aliases = config.setdefault('handles', {})
        aliases[row['original']] = cleaned
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.config_path, config, indent=1)
        return {'ok': True, 'verified': True, 'driver_uid': int(driver_uid),
                'original': row['original'], 'current': cleaned,
                'patched': patched, 'write': write}

    def rename_current(self, current: str, new_handle: str) -> dict:
        wanted = str(current).strip().lstrip('@').casefold()
        matches = [row for row in self.handles() if row['current'].casefold() == wanted]
        if len(matches) != 1:
            raise ValueError('current handle does not resolve to exactly one mapped driver')
        return self.rename(matches[0]['driver_uid'], new_handle)

    def restore(self, driver_uid: int) -> dict:
        row = next((item for item in self.handles()
                    if int(item['driver_uid']) == int(driver_uid)), None)
        if row is None:
            raise ValueError('driver handle was not found')
        result = self.rename(driver_uid, row['original'])
        config = self._config()
        aliases = config.get('handles') if isinstance(config.get('handles'), dict) else {}
        aliases.pop(row['original'], None)
        if aliases:
            config['handles'] = aliases
        else:
            config.pop('handles', None)
        atomic_write_json(self.config_path, config, indent=1)
        return result
