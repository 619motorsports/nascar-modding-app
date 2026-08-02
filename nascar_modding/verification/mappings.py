"""Produce reproducible mapping evidence from installed game files."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from nascar_modding.core.files import atomic_write_json
from nascar_modding.games.assets import (
    livery_pattern_strings,
    matches_dormant_livery,
    matches_primary_livery,
)
from nascar_modding.games.installation import GameInstallation
from nascar_modding.verification.career import audit_ntg2013_career


_DATABASE_FILES = ('DB_GAME_LOCAL_SCRIPT.PYC', 'DB_AICONFIG_SCRIPT.PYC')


def _index_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def audit_installation(installation: GameInstallation) -> dict:
    profile = installation.profile
    livery_patterns = livery_pattern_strings(profile)
    errors: list[str] = []
    indexes = []
    all_names: list[tuple[str, str]] = []

    for key, pair in sorted(
        installation.archive_pairs.items(), key=lambda item: (len(item[0]), item[0])
    ):
        entries = installation.entries(key)
        archive_size = pair.archive.stat().st_size
        out_of_bounds = [
            entry.name
            for entry in entries
            if entry.archive_offset + entry.size > archive_size
        ]
        if out_of_bounds:
            errors.append(f'ARCHIVE{key}: {len(out_of_bounds)} out-of-bounds entries')
        layouts = Counter(entry.layout for entry in entries)
        indexes.append(
            {
                'archive': key,
                'archive_size': archive_size,
                'index_file': pair.index.name,
                'index_size': pair.index.stat().st_size,
                'index_sha256': _index_sha256(pair.index),
                'entries': len(entries),
                'layouts': dict(sorted(layouts.items())),
                'out_of_bounds': out_of_bounds,
            }
        )
        all_names.extend((key, entry.name) for entry in entries)

    missing_required = list(installation.missing_required_archives)
    if missing_required:
        errors.append('missing required archives: ' + ', '.join(missing_required))

    expected_liveries = sum(matches_primary_livery(profile, name) for _key, name in all_names)
    if not expected_liveries:
        errors.append(f'no primary {profile.content_season} livery resources found')
    dormant_liveries = sum(matches_dormant_livery(profile, name) for _key, name in all_names)

    number_hits = [
        {'archive': key, 'name': name}
        for key, name in all_names
        if name.casefold() == profile.number_container.casefold()
    ]
    if not number_hits:
        errors.append(f'missing number-card container {profile.number_container}')

    database_hits = {
        expected: [key for key, name in all_names if name.casefold() == expected.casefold()]
        for expected in _DATABASE_FILES
    }
    for name, hits in database_hits.items():
        if not hits:
            errors.append(f'missing generated database {name}')

    body_models = []
    for archive, name in profile.body_models:
        try:
            raw = installation.read_entry(name, archive)
            magic = raw[:4].decode('ascii', 'replace')
            body_models.append(
                {
                    'archive': archive,
                    'name': name,
                    'size': len(raw),
                    'magic': magic,
                    'verified': magic == 'ARCC',
                }
            )
            if magic != 'ARCC':
                errors.append(f'{name} is not an ARCC mesh container')
        except Exception as exc:
            body_models.append(
                {'archive': archive, 'name': name, 'verified': False, 'error': str(exc)}
            )
            errors.append(f'body model unavailable: {name}')

    facts = {
        'total_entries': len(all_names),
        'expected_livery_entries': expected_liveries,
        'dormant_livery_entries': dormant_liveries,
        'number_container_hits': number_hits,
        'database_hits': database_hits,
        'body_models': body_models,
    }
    if profile.id == 'nascar13':
        data_dir = Path(__file__).resolve().parents[2] / 'data' / profile.data_subdir
        try:
            facts['career_mode'] = audit_ntg2013_career(installation, data_dir)
        except Exception as exc:
            facts['career_mode'] = {'status': 'audit_failed', 'safe_to_patch': False, 'error': str(exc)}
            errors.append(f'career readiness audit failed: {exc}')

    return {
        'format': 'nascar-modding-app-live-mapping-audit-v2',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'game_id': profile.id,
        'game_name': profile.name,
        'install_folder': installation.root.name,
        'profile': {
            'content_season': profile.content_season,
            'season_prefix': profile.season_prefix,
            'required_archives': list(profile.required_archives),
            'paint_primary_archive': profile.paint_primary_archive,
            'number_container': profile.number_container,
            'series_uid': profile.series_uid,
            'livery_style': profile.livery_style,
            'primary_livery_patterns': livery_patterns['primary'],
            'dormant_livery_patterns': livery_patterns['dormant'],
            'dormant_number_containers': list(profile.dormant_number_containers),
        },
        'indexes': indexes,
        'facts': facts,
        'ok': not errors,
        'errors': errors,
    }


def write_audit(installation: GameInstallation, output: str | Path) -> dict:
    report = audit_installation(installation)
    destination = Path(output)
    atomic_write_json(destination, report, indent=2)
    return report
