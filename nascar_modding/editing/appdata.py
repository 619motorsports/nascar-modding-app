"""Portable backup and restore for app-owned state (never game archives)."""

from __future__ import annotations

import datetime as _datetime
import io
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile

from nascar_modding.core.files import atomic_write_bytes


APPDATA_FILES = (
    'config.json', 'game_selector.json', 'extra_schemes_v1.json',
    'team_manager_state.json', 'repoint_history.json',
    'last_whole_mod_repair.json', 'extra_uid_candidates_v1.json',
)
APPDATA_DIRS = ('schemes', 'team_asset_rollback_v1', 'profiles')
APPDATA_MANIFEST = 'nascar_app_data.json'
APPDATA_VERSION = 2
MAX_IMPORT_MEMBER = 64 * 1024 * 1024
MAX_IMPORT_TOTAL = 512 * 1024 * 1024

LEGACY_MEMBER_RENAMES = {
    'LIVERY_2015_MIKEWALLACE.ARC': 'LIVERY_2015_DARRELLWALLACEJR.ARC',
    'HDLIVERY_2015_MIKEWALLACE.ARC': 'HDLIVERY_2015_DARRELLWALLACEJR.ARC',
}


def default_app_data_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]


class AppDataManager:
    def __init__(self, user_dir: str | Path, app_version: str = 'unknown'):
        self.root = Path(user_dir).resolve()
        self.app_version = str(app_version)

    def members(self) -> list[tuple[str, Path]]:
        rows = []
        for name in APPDATA_FILES:
            path = self.root / name
            if path.is_file():
                rows.append((name, path))
        for dirname in APPDATA_DIRS:
            base = self.root / dirname
            if not base.is_dir():
                continue
            for path in base.rglob('*'):
                if path.is_file():
                    rows.append((path.relative_to(self.root).as_posix(), path))
        return sorted(rows)

    def export_bytes(self) -> bytes:
        members = self.members()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(APPDATA_MANIFEST, json.dumps({
                'format': 'nascar_app_data', 'version': APPDATA_VERSION,
                'app_version': self.app_version,
                'schema_versions': {'config': 2, 'extra_schemes': 1, 'team_manager': 1, 'schemes': 2},
                'created': _datetime.datetime.now().isoformat(timespec='seconds'),
                'files': [name for name, _path in members],
            }, indent=2))
            for name, path in members:
                try:
                    archive.write(path, name)
                except OSError:
                    continue
        return output.getvalue()

    @staticmethod
    def _safe_name(name: str) -> str | None:
        normalized = str(name).replace('\\', '/').strip('/')
        if not normalized or normalized.endswith('/') or ':' in normalized:
            return None
        parts = PurePosixPath(normalized).parts
        if '..' in parts or parts[-1] == APPDATA_MANIFEST:
            return None
        if parts[-1] in APPDATA_FILES:
            return parts[-1]
        for index, part in enumerate(parts):
            if part in APPDATA_DIRS and index + 1 < len(parts):
                return '/'.join(parts[index:])
        return None

    @staticmethod
    def _current_name(name: str) -> str:
        path = PurePosixPath(name)
        renamed = LEGACY_MEMBER_RENAMES.get(path.name.upper(), path.name)
        return str(path.with_name(renamed))

    @classmethod
    def _replace_legacy_values(cls, value, migrations: list[str]):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                new_key = 'Darrell Wallace Jr.' if str(key).casefold() == 'mike wallace' else str(key)
                if new_key != str(key):
                    migrations.append('config name key Mike Wallace -> Darrell Wallace Jr.')
                result[new_key] = cls._replace_legacy_values(item, migrations)
            return result
        if isinstance(value, list):
            return [cls._replace_legacy_values(item, migrations) for item in value]
        if isinstance(value, str):
            result = value
            for old, current in LEGACY_MEMBER_RENAMES.items():
                result = result.replace(old, current)
            if result != value:
                migrations.append(f'{value} -> {result}')
            return result
        return value

    @classmethod
    def _migrate_bytes(cls, name: str, payload: bytes, migrations: list[str]) -> bytes:
        if not name.casefold().endswith('.json'):
            return payload
        try:
            value = json.loads(payload.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            return payload
        value = cls._replace_legacy_values(value, migrations)
        if name == 'config.json' and isinstance(value, dict):
            value.setdefault('renames', {})
            value.setdefault('handles', {})
            value['app_data_schema_version'] = APPDATA_VERSION
        return json.dumps(value, indent=2, ensure_ascii=False).encode('utf-8')

    def import_bytes(self, payload: bytes) -> dict:
        migrations: list[str] = []
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            try:
                manifest = json.loads(archive.read(APPDATA_MANIFEST).decode('utf-8-sig'))
            except (KeyError, UnicodeError, ValueError):
                manifest = {'format': 'nascar_app_data', 'version': 0, 'app_version': 'older/unversioned'}
            if manifest.get('format') != 'nascar_app_data':
                raise ValueError('that zip is not an app-data backup')
            from_version = int(manifest.get('version', 0) or 0)
            if from_version > APPDATA_VERSION:
                raise ValueError(
                    f'this backup uses schema {from_version}; install a newer app version to restore it'
                )
            wanted, seen, total = [], set(), 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                safe = self._safe_name(info.filename)
                if not safe:
                    continue
                current = self._current_name(safe)
                if current != safe:
                    migrations.append(f'{safe} -> {current}')
                if current in seen:
                    continue
                if info.file_size > MAX_IMPORT_MEMBER:
                    raise ValueError(f'app-data member is too large: {info.filename}')
                total += info.file_size
                if total > MAX_IMPORT_TOTAL:
                    raise ValueError('app-data backup exceeds the 512 MB safety limit')
                seen.add(current)
                wanted.append((info, current))
            if not wanted:
                raise ValueError('the zip held no app data to restore')

            stamp = _datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            existing = self.members()
            recovery = None
            if existing:
                recovery = self.root / f'app_data_replaced_{stamp}.zip'
                recovery.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(recovery, 'w', zipfile.ZIP_DEFLATED) as backup:
                    for name, path in existing:
                        backup.write(path, name)

            written = 0
            for info, safe in wanted:
                destination = (self.root / Path(*PurePosixPath(safe).parts)).resolve()
                try:
                    destination.relative_to(self.root)
                except ValueError:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                migrated = self._migrate_bytes(safe, archive.read(info), migrations)
                atomic_write_bytes(destination, migrated, '.appdata.tmp')
                written += 1

        unique = list(dict.fromkeys(migrations))
        return {
            'restored': written, 'previous_saved': len(existing),
            'recovery_path': str(recovery) if recovery else None,
            'from_version': manifest.get('app_version'), 'from_schema': from_version,
            'to_schema': APPDATA_VERSION,
            'converted': from_version < APPDATA_VERSION or bool(unique),
            'migrations': unique[:50],
        }
