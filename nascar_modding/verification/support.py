"""Shared installation health checks and privacy-safe diagnostic bundles."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import io
import json
import platform
from pathlib import Path
import struct
import sys
import zipfile

from nascar_modding.editing.archive import backup_path
from nascar_modding.games.installation import GameInstallation


REQUIRED_COMPONENTS = (
    'nascar15_pyc_record_mapper_v5_teams.py',
    'nascar15_v11_probe_patcher.py',
    'nascar15_const_repoint_v0_2.py',
    'nascar15_schedule_editor_v0_1.py',
    'nascar15_schedule_raceevent_links_v0_1.py',
    'nascar15_team_assets_v1.py',
    'nascar15_thumbnail_native_v25.py',
    'ui_assets.csv',
)


class SupportReporter:
    def __init__(
        self, installation: GameInstallation | None, app_root: str | Path,
        user_root: str | Path | None = None, app_version: str = 'unknown',
        release_label: str = '',
    ):
        self.installation = installation
        self.app_root = Path(app_root).resolve()
        self.user_root = Path(user_root).resolve() if user_root else self.app_root
        self.app_version = str(app_version)
        self.release_label = str(release_label)

    def _component_path(self, name: str) -> Path:
        candidates = (
            self.app_root / 'internal_tools' / name,
            self.app_root / name,
            self.app_root / 'data' / name,
        )
        return next((path for path in candidates if path.exists()), candidates[0])

    def checks(self) -> tuple[list[dict], dict]:
        rows: list[dict] = []

        def add(name, status, detail, technical=False):
            rows.append({
                'name': name, 'status': status, 'detail': str(detail),
                'technical': bool(technical),
            })

        add('Application', 'pass', f'NASCAR Modding App v{self.app_version} {self.release_label}'.strip())
        for name in REQUIRED_COMPONENTS:
            path = self._component_path(name)
            add('Required component: ' + name, 'pass' if path.exists() else 'fail',
                'Ready' if path.exists() else f'Missing: {path}', True)
        if self.installation is None:
            add('Game installation', 'warn', 'No game installation is selected.')
        else:
            game = self.installation
            missing = game.missing_required_archives
            add('Game installation', 'pass' if not missing else 'fail', str(game.root))
            add('Required archives', 'pass' if not missing else 'fail',
                'All required archive/index pairs are present.' if not missing
                else 'Missing archive groups: ' + ', '.join(missing), True)
            pairs = game.archive_pairs
            protected = sum(
                pair.archive.with_name(pair.archive.name + '.n15mod.bak').is_file()
                and Path(backup_path(pair.index)).is_file()
                for pair in pairs.values()
            )
            add('Paired backups', 'pass' if protected == len(pairs) else 'warn',
                f'{protected}/{len(pairs)} archive/index groups have paired backups.', True)
            out_of_bounds = []
            for key, pair in pairs.items():
                size = pair.archive.stat().st_size
                out_of_bounds.extend(
                    f'ARCHIVE{key}:{entry.name}' for entry in game.entries(key)
                    if entry.archive_offset + entry.size > size
                )
            add('Index bounds', 'pass' if not out_of_bounds else 'fail',
                'All indexed resources are within their archive.' if not out_of_bounds
                else f'{len(out_of_bounds)} resources point outside their archive.', True)
        fail = sum(row['status'] == 'fail' for row in rows)
        warn = sum(row['status'] == 'warn' for row in rows)
        summary = {'pass_count': len(rows) - fail - warn, 'warn_count': warn,
                   'fail_count': fail, 'total': len(rows)}
        return rows, summary

    def report_bytes(self) -> bytes:
        checks, summary = self.checks()
        lines = [
            f'NASCAR Modding App v{self.app_version} support report',
            f'Created: {_datetime.datetime.now().isoformat(timespec="seconds")}',
            f'Result: {summary["pass_count"]} pass, {summary["warn_count"]} warning, {summary["fail_count"]} fail',
            '',
        ]
        lines.extend(f'[{row["status"].upper()}] {row["name"]}: {row["detail"]}' for row in checks)
        return ('\n'.join(lines) + '\n').encode('utf-8')

    @staticmethod
    def _file_info(path: Path) -> dict:
        if not path.is_file():
            return {'path': str(path), 'exists': False}
        stat = path.stat()
        with path.open('rb') as handle:
            first = handle.read(1 << 20)
            if stat.st_size > 1 << 20:
                handle.seek(max(0, stat.st_size - (1 << 20)))
                last = handle.read(1 << 20)
            else:
                last = b''
        digest = hashlib.sha256(struct.pack('<Q', stat.st_size) + first + last).hexdigest()
        return {'path': str(path), 'exists': True, 'size': stat.st_size,
                'mtime': stat.st_mtime, 'quick_sha256': digest}

    def diagnostics_bytes(self, config: dict | None = None) -> bytes:
        safe_config = {
            key: value for key, value in (config or {}).items()
            if not any(secret in str(key).casefold() for secret in ('token', 'password', 'secret'))
        }
        archives = {}
        if self.installation:
            for key, pair in sorted(self.installation.archive_pairs.items()):
                archives[key] = {
                    'archive': self._file_info(pair.archive),
                    'cdfiles': self._file_info(pair.index),
                    'archive_backup': self._file_info(pair.archive.with_name(pair.archive.name + '.n15mod.bak')),
                    'cdfiles_backup': self._file_info(backup_path(pair.index)),
                }
        schemes = []
        for base in (self.user_root / 'schemes', self.user_root / 'profiles'):
            if base.is_dir():
                schemes.extend({'name': path.relative_to(self.user_root).as_posix(), 'size': path.stat().st_size}
                               for path in base.rglob('*') if path.is_file())
        report = {
            'created': _datetime.datetime.now().isoformat(),
            'app_version': self.app_version, 'release_label': self.release_label,
            'python': sys.version, 'platform': platform.platform(),
            'game': str(self.installation.root) if self.installation else None,
            'game_id': self.installation.profile.id if self.installation else None,
            'config': safe_config, 'archives': archives, 'scheme_files': schemes,
            'components': {name: self._component_path(name).exists() for name in REQUIRED_COMPONENTS},
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('diagnostics.json', json.dumps(report, indent=2))
            archive.writestr('README.txt', 'Privacy-safe diagnostics; no game archive bytes are included.\n')
        return output.getvalue()
