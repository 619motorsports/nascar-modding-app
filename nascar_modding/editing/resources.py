"""Generic indexed-resource inspection, replacement, and package workflows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile

from nascar_modding.editing.archive import ArchiveEntryEditor, backup_path
from nascar_modding.games.installation import GameInstallation


PACKAGE_FORMAT = 'nascar-modding-resource-package-v1'
_SAFE_NAME = re.compile(r'[^A-Za-z0-9_.-]+')


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def resource_category(name: str) -> str:
    """Return the shared presentation category for an indexed resource."""
    upper = str(name or '').upper()
    if upper.endswith('.PYC'):
        return 'Database / PYC'
    if upper.endswith('.LDA') or upper.startswith('TEXT'):
        return 'UI Text / localization'
    if upper.endswith(('.FSB', '.SND')) or 'SOUND' in upper or 'MUSIC' in upper:
        return 'Audio'
    if any(word in upper for word in (
        'BODY', 'INTERIORASS', 'WHEEL', 'BRAKEKIT', 'SUSPENSION', 'CHASSIS', 'PHY.ARC',
    )):
        return 'Vehicle / model'
    if (
        any(word in upper for word in ('TRACK', 'RACEWAY', 'SPEEDWAY', 'REGION', 'GLOBALPHY'))
        or re.search(r'000[ANP]\.ARC$', upper)
    ):
        return 'Track'
    if any(word in upper for word in (
        'TEXTURE', 'MENU', 'HUD', 'TEAMSHOP', 'DRIVERSELECT', 'THUMB', 'FEI', 'LOGO',
    )):
        return 'Images / UI'
    if upper.endswith('.ARC'):
        return 'ARC container'
    return 'Other'


def resource_magic(payload: bytes) -> str:
    head = bytes(payload[:16])
    if head[:4] == b'ARCC':
        return 'ARCC'
    if head[:4] == b'filC':
        return 'filC'
    if head[:3] == b'FSB':
        return head[:4].decode('ascii', 'replace')
    if head[:4] == b'PK\x03\x04':
        return 'ZIP'
    if head[:4] == b'DDS ':
        return 'DDS'
    return head[:8].hex(' ').upper() or '(empty)'


class ResourceEditor:
    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.archive_editor = ArchiveEntryEditor(installation)

    def archives(self) -> list[dict]:
        return [
            {
                'key': key,
                'archive': pair.archive.name,
                'index': pair.index.name,
                'entries': len(self.installation.entries(key)),
                'bytes': pair.archive.stat().st_size,
            }
            for key, pair in sorted(
                self.installation.archive_pairs.items(),
                key=lambda item: (len(item[0]), item[0]),
            )
        ]

    def resources(self, archive_key: str, query: str = '') -> list[dict]:
        wanted = str(query).strip().casefold()
        rows = []
        for entry in self.installation.entries(str(archive_key)):
            if wanted and wanted not in entry.name.casefold():
                continue
            rows.append({
                'archive': str(archive_key),
                'name': entry.name,
                'size': entry.size,
                'offset': entry.archive_offset,
                'layout': entry.layout,
                'category': resource_category(entry.name),
                'extension': Path(entry.name).suffix.upper() or '(none)',
            })
        return sorted(rows, key=lambda row: row['name'].casefold())

    def inspect(self, name: str, archive_key: str) -> dict:
        key, entry = self.installation.find_entry(name, archive_key)
        payload = self.installation.read_entry(name, key)
        pair = self.installation.archive_pairs[key]
        stock = None
        backup_archive = Path(backup_path(pair.archive))
        backup_index = Path(backup_path(pair.index))
        if backup_archive.is_file() and backup_index.is_file():
            from nascar_modding.core.cdf import read_cdf

            matches = [
                item for item in read_cdf(backup_index)
                if item.name.casefold() == name.casefold()
            ]
            if len(matches) == 1:
                original = matches[0]
                with backup_archive.open('rb') as handle:
                    handle.seek(original.archive_offset)
                    stock_payload = handle.read(original.size)
                if len(stock_payload) == original.size:
                    stock = {
                        'size': original.size,
                        'offset': original.archive_offset,
                        'sha256': _payload_sha256(stock_payload),
                        'magic': resource_magic(stock_payload),
                        'modified': stock_payload != payload,
                    }
        return {
            'archive': key,
            'name': entry.name,
            'size': entry.size,
            'offset': entry.archive_offset,
            'layout': entry.layout,
            'sha256': _payload_sha256(payload),
            'prefix_hex': payload[:32].hex(' '),
            'magic': resource_magic(payload),
            'category': resource_category(entry.name),
            'stock': stock,
        }

    def read(self, name: str, archive_key: str, pristine: bool = False) -> bytes:
        if not pristine:
            return self.installation.read_entry(name, archive_key)
        pair = self.installation.archive_pairs[str(archive_key)]
        archive = Path(backup_path(pair.archive))
        index = Path(backup_path(pair.index))
        if not archive.is_file() or not index.is_file():
            raise FileNotFoundError('the paired pristine archive/index backup is unavailable')
        from nascar_modding.core.cdf import read_cdf

        matches = [entry for entry in read_cdf(index) if entry.name.casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError(f'backup index does not identify exactly one {name}')
        entry = matches[0]
        with archive.open('rb') as handle:
            handle.seek(entry.archive_offset)
            payload = handle.read(entry.size)
        if len(payload) != entry.size:
            raise IOError(f'backup entry is truncated: {name}')
        return payload

    def export(
        self,
        name: str,
        archive_key: str,
        destination: str | Path,
        pristine: bool = False,
    ) -> Path:
        payload = self.read(name, archive_key, pristine)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        return output

    def plan(self, name: str, archive_key: str, payload: bytes) -> dict:
        if not payload:
            raise ValueError('replacement payload is empty')
        key, entry = self.installation.find_entry(name, archive_key)
        pair = self.installation.archive_pairs[key]
        archive_size = pair.archive.stat().st_size
        new_offset = entry.archive_offset if len(payload) == entry.size else (archive_size + 15) & ~15
        if new_offset + len(payload) >= 2**32:
            raise ValueError('replacement would exceed the 32-bit archive offset limit')
        warnings = []
        if Path(entry.name).suffix.casefold() == '.arc' and payload[:4] != b'ARCC':
            warnings.append(
                f'ARC target expects ARCC, but replacement begins with {resource_magic(payload)}'
            )
        return {
            'archive': key,
            'entry': entry.name,
            'category': resource_category(entry.name),
            'old_offset': entry.archive_offset,
            'old_size': entry.size,
            'new_offset': new_offset,
            'new_size': len(payload),
            'archive_size': archive_size,
            'projected_archive_size': max(archive_size, new_offset + len(payload)),
            'growth': max(0, new_offset + len(payload) - archive_size),
            'sha256': _payload_sha256(payload),
            'magic': resource_magic(payload),
            'warnings': warnings,
            'method': 'in_place' if len(payload) == entry.size else 'append_repoint',
        }

    def replace(self, name: str, archive_key: str, payload: bytes) -> dict:
        return self.archive_editor.replace_or_repoint_entry(name, payload, archive_key)

    def replace_file(self, name: str, archive_key: str, source: str | Path) -> dict:
        return self.replace(name, archive_key, Path(source).read_bytes())

    def restore(self, name: str, archive_key: str) -> dict:
        return self.archive_editor.restore_entry(name, archive_key)

    def export_package(
        self,
        selections: list[tuple[str, str]],
        destination: str | Path,
    ) -> Path:
        if not selections:
            raise ValueError('select at least one resource to export')
        manifest = {
            'format': PACKAGE_FORMAT,
            'game_id': self.installation.profile.id,
            'entries': [],
        }
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
            for sequence, (archive_key, name) in enumerate(selections):
                payload = self.installation.read_entry(name, archive_key)
                safe_name = _SAFE_NAME.sub('_', name)
                member = f'entries/{sequence:04d}_{archive_key}_{safe_name}'
                package.writestr(member, payload)
                manifest['entries'].append({
                    'archive': str(archive_key), 'name': name, 'member': member,
                    'size': len(payload), 'sha256': _payload_sha256(payload),
                })
            package.writestr('manifest.json', json.dumps(manifest, indent=2) + '\n')
        return output

    def preview_package(self, source: str | Path) -> dict:
        with zipfile.ZipFile(source, 'r') as package:
            try:
                manifest = json.loads(package.read('manifest.json').decode('utf-8'))
            except KeyError as exc:
                raise ValueError('resource package has no manifest.json') from exc
            if manifest.get('format') != PACKAGE_FORMAT:
                raise ValueError('unsupported resource package format')
            if manifest.get('game_id') != self.installation.profile.id:
                raise ValueError(
                    f"package targets {manifest.get('game_id')}, not {self.installation.profile.id}"
                )
            rows, seen = [], set()
            for item in manifest.get('entries') or []:
                key, name, member = str(item['archive']), str(item['name']), str(item['member'])
                identity = (key, name.casefold())
                if identity in seen:
                    raise ValueError(f'package contains duplicate resource {name}')
                seen.add(identity)
                payload = package.read(member)
                if len(payload) != int(item['size']) or _payload_sha256(payload) != item['sha256']:
                    raise ValueError(f'package payload validation failed for {name}')
                _found_key, live_entry = self.installation.find_entry(name, key)
                rows.append({
                    'archive': key, 'name': live_entry.name,
                    'old_size': live_entry.size, 'new_size': len(payload),
                    'size_delta': len(payload) - live_entry.size,
                    'member': member, 'sha256': item['sha256'],
                })
        return {'format': PACKAGE_FORMAT, 'entries': rows, 'count': len(rows)}

    def install_package(self, source: str | Path) -> dict:
        preview = self.preview_package(source)
        grouped, originals = {}, {}
        with zipfile.ZipFile(source, 'r') as package:
            for row in preview['entries']:
                grouped.setdefault(row['archive'], {})[row['name']] = package.read(row['member'])
                originals.setdefault(row['archive'], {})[row['name']] = self.installation.read_entry(
                    row['name'], row['archive']
                )
        installed = []
        try:
            for key, replacements in grouped.items():
                installed.append((key, self.archive_editor.replace_entries(replacements, key)))
        except Exception as install_error:
            rollback_errors = []
            for key, _result in reversed(installed):
                try:
                    self.archive_editor.replace_entries(originals[key], key)
                except Exception as exc:
                    rollback_errors.append(f'ARCHIVE{key}: {exc}')
            if rollback_errors:
                raise RuntimeError(
                    f'package install failed ({install_error}) and rollback failed: '
                    + '; '.join(rollback_errors)
                ) from install_error
            raise
        return {
            'entries': preview['count'],
            'archives': len(installed),
            'verified': True,
            'writes': [result for _key, result in installed],
        }
