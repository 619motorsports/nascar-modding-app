"""Read-only discovery and inspection of NTG2013 Steam Cloud save slots."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

from nascar_modding.formats.gfs import compare_gfs, inspect_gfs
from nascar_modding.games.installation import GameInstallation


NTG2013_STEAM_APP_ID = '225220'
MAX_EXPERIMENT_FILES = 256
MAX_EXPERIMENT_FILE_SIZE = 64 * 1024 * 1024


def _steam_roots(installation: GameInstallation | None = None) -> list[Path]:
    roots = []
    for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES'):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / 'Steam')
    if installation is not None:
        for parent in installation.root.parents:
            if parent.name.casefold() == 'steamapps':
                roots.append(parent.parent)
            elif (parent / 'steamapps').is_dir():
                roots.append(parent)
    unique = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def discover_ntg2013_saves(installation: GameInstallation | None = None) -> list[Path]:
    found = []
    for steam in _steam_roots(installation):
        userdata = steam / 'userdata'
        if not userdata.is_dir():
            continue
        for candidate in userdata.glob(f'*/{NTG2013_STEAM_APP_ID}/remote/*'):
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return sorted(found, key=lambda path: (path.parent.parent.parent.name, path.name.casefold()))


def inspect_ntg2013_save(path: str | Path) -> dict:
    source = Path(path)
    data = source.read_bytes()
    result = {
        'path': str(source),
        'name': source.name,
        'file_size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }
    try:
        result.update(inspect_gfs(data))
    except Exception as exc:
        result.update(valid=False, error=str(exc))
        return result
    section = result.get('section_name')
    result['profiledata'] = section == 'PROFILEDATA'
    result['retail_profile_candidate'] = bool(
        result['profiledata'] and result['payload_size'] >= 0x1000
    )
    result['role'] = {
        'SYS-DATA': 'current system/profile slot',
        'SYS-TU1.001': 'title-update/profile compatibility slot 1',
        'SYS-TU2.001': 'title-update/profile compatibility slot 2',
        'liv_save': 'custom livery slot',
    }.get(source.name, 'unknown retail slot')
    return result


def compare_ntg2013_saves(before_path: str | Path, after_path: str | Path) -> dict:
    before, after = Path(before_path), Path(after_path)
    before_data, after_data = before.read_bytes(), after.read_bytes()
    result = compare_gfs(before_data, after_data)
    result.update({
        'before_file': before.name,
        'after_file': after.name,
        'before_sha256': hashlib.sha256(before_data).hexdigest(),
        'after_sha256': hashlib.sha256(after_data).hexdigest(),
        'read_only': True,
    })
    return result


def analyze_ntg2013_transition(
    phases: Iterable[tuple[str, str | Path]],
) -> dict:
    """Compare ordered copies of retail save folders without writing to them."""
    ordered = [(str(label).strip(), Path(path).resolve()) for label, path in phases]
    if not 2 <= len(ordered) <= 8:
        raise ValueError('a transition experiment requires 2 to 8 ordered phases')
    if any(not label for label, _path in ordered):
        raise ValueError('every experiment phase needs a label')
    if len({label.casefold() for label, _path in ordered}) != len(ordered):
        raise ValueError('experiment phase labels must be unique')

    snapshots = []
    cached_bytes: list[dict[str, bytes]] = []
    for label, root in ordered:
        if not root.is_dir():
            raise ValueError(f'experiment phase folder does not exist: {root}')
        paths = sorted(path for path in root.rglob('*') if path.is_file())
        if len(paths) > MAX_EXPERIMENT_FILES:
            raise ValueError(f'experiment phase exceeds {MAX_EXPERIMENT_FILES} files: {root}')
        files, raw_files = [], {}
        for path in paths:
            size = path.stat().st_size
            if size > MAX_EXPERIMENT_FILE_SIZE:
                raise ValueError(f'experiment file exceeds 64 MiB: {path}')
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            raw_files[relative] = data
            inspected = inspect_ntg2013_save(path)
            inspected['relative_path'] = relative
            inspected.pop('path', None)
            files.append(inspected)
        snapshots.append({'label': label, 'path': str(root), 'file_count': len(files), 'files': files})
        cached_bytes.append(raw_files)

    transitions = []
    for index in range(1, len(snapshots)):
        before_files, after_files = cached_bytes[index - 1], cached_bytes[index]
        before_names, after_names = set(before_files), set(after_files)
        added = sorted(after_names - before_names)
        removed = sorted(before_names - after_names)
        changed, unchanged, comparisons = [], [], []
        for name in sorted(before_names & after_names):
            before_data, after_data = before_files[name], after_files[name]
            if before_data == after_data:
                unchanged.append(name)
                continue
            changed.append(name)
            try:
                comparison = compare_gfs(before_data, after_data)
            except Exception as exc:
                comparisons.append({'file': name, 'valid_gfs_pair': False, 'error': str(exc)})
            else:
                comparison.update({'file': name, 'valid_gfs_pair': True})
                comparisons.append(comparison)
        profile_comparisons = [
            row for row in comparisons
            if row.get('valid_gfs_pair') and row.get('before_section') == 'PROFILEDATA'
            and row.get('after_section') == 'PROFILEDATA'
        ]
        transitions.append({
            'from': snapshots[index - 1]['label'], 'to': snapshots[index]['label'],
            'added': added, 'removed': removed, 'changed': changed, 'unchanged': unchanged,
            'comparisons': comparisons,
            'profile_slots_changed': [row['file'] for row in profile_comparisons],
            'proven_prefix_change_count': sum(
                len(row.get('known_prefix_scalar_changes', ())) for row in profile_comparisons
            ),
            'opaque_dynamic_changed_bytes': sum(
                row.get('changed_bytes_by_region', {}).get('opaque_dynamic_payload', 0)
                for row in profile_comparisons
            ),
        })
    return {
        'format': 'ntg2013_transition_experiment', 'version': 1, 'read_only': True,
        'phase_count': len(snapshots), 'phases': snapshots, 'transitions': transitions,
        'retail_profile_writer_ready': False,
        'interpretation_boundary': (
            'This report identifies which copied slots and proven structural regions changed. '
            'It does not assign career meaning to opaque payload bytes or authorize a save writer.'
        ),
    }
