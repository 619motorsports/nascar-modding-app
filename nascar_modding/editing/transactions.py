"""Reusable rollback snapshots for multi-archive append/repoint workflows."""

from __future__ import annotations

import hashlib
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from nascar_modding.core.files import atomic_write_bytes, atomic_write_json
from nascar_modding.games.installation import GameInstallation


class ExactFileTransaction:
    """Disk-backed exact rollback for aggregate workflows with in-place writes."""

    def snapshot(self, files, *, directories=None) -> dict:
        root = Path(tempfile.mkdtemp(prefix='nascar_exact_transaction_'))
        captured_files, captured_directories = [], []
        try:
            for index, raw_path in enumerate(files):
                path = Path(raw_path).resolve()
                backup = root / 'files' / f'{index:04d}.bin'
                exists = path.is_file()
                if exists:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
                captured_files.append({
                    'path': str(path), 'exists': exists,
                    'backup': str(backup) if exists else None,
                })
            for index, raw_path in enumerate(directories or ()):
                path = Path(raw_path).resolve()
                backup = root / 'directories' / f'{index:04d}'
                exists = path.is_dir()
                if exists:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(path, backup)
                captured_directories.append({
                    'path': str(path), 'exists': exists,
                    'backup': str(backup) if exists else None,
                })
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return {'root': str(root), 'files': captured_files,
                'directories': captured_directories}

    @staticmethod
    def restore(snapshot: dict) -> list[str]:
        errors = []
        for item in snapshot.get('files', []):
            path = Path(item['path'])
            try:
                if item['exists']:
                    temporary = path.with_name(path.name + '.exact_transaction.restore')
                    shutil.copy2(item['backup'], temporary)
                    os.replace(temporary, path)
                else:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f'{path}: {exc}')
        for item in snapshot.get('directories', []):
            path = Path(item['path'])
            try:
                if path.exists():
                    shutil.rmtree(path)
                if item['exists']:
                    shutil.copytree(item['backup'], path)
            except Exception as exc:
                errors.append(f'{path}: {exc}')
        return errors

    @staticmethod
    def clear(snapshot: dict) -> None:
        root = Path(snapshot.get('root') or '')
        if root.is_dir() and root.name.startswith('nascar_exact_transaction_'):
            shutil.rmtree(root)


class AppendRepointTransaction:
    """Capture archive lengths plus exact indexes and app-owned sidecar state."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation

    def snapshot(
        self, archive_keys, *, state_files: dict[str, str | Path] | None = None,
        directories: dict[str, str | Path] | None = None,
        inplace_region: dict | None = None,
        inplace_regions: list[dict] | None = None,
    ) -> dict:
        groups = {}
        for raw_key in archive_keys:
            key = str(raw_key)
            pair = self.installation.archive_pairs.get(key)
            if pair is None:
                raise KeyError(f'archive/index pair {key} is unavailable')
            groups[key] = {
                'archive': str(pair.archive), 'archive_size': pair.archive.stat().st_size,
                'index': str(pair.index), 'index_bytes': pair.index.read_bytes(),
            }
        states = {}
        for name, raw_path in (state_files or {}).items():
            path = Path(raw_path).resolve()
            states[str(name)] = {
                'path': str(path), 'exists': path.is_file(),
                'bytes': path.read_bytes() if path.is_file() else None,
            }
        captured_directories = {}
        for name, raw_path in (directories or {}).items():
            path = Path(raw_path).resolve()
            files = {}
            if path.is_dir():
                for child in path.rglob('*'):
                    if child.is_file():
                        files[child.relative_to(path).as_posix()] = child.read_bytes()
            captured_directories[str(name)] = {'path': str(path), 'files': files}
        captured_regions = []
        requested_regions = list(inplace_regions or [])
        if inplace_region:
            requested_regions.append(inplace_region)
        for requested in requested_regions:
            archive = Path(requested['archive']).resolve()
            offset, size = int(requested['offset']), int(requested['size'])
            raw = requested.get('raw')
            if raw is None:
                with archive.open('rb') as handle:
                    handle.seek(offset)
                    raw = handle.read(size)
            if len(raw) != size:
                raise IOError('in-place rollback region is incomplete')
            captured_regions.append({
                'archive': str(archive), 'offset': offset, 'size': size,
                'raw': bytes(raw), 'name': requested.get('name'),
            })
        inplace = captured_regions[0] if len(captured_regions) == 1 else None
        return {'groups': groups, 'states': states, 'directories': captured_directories,
                'inplace_region': inplace, 'inplace_regions': captured_regions}

    def restore(self, snapshot: dict) -> list[str]:
        errors = []
        regions = list(snapshot.get('inplace_regions') or [])
        if not regions and snapshot.get('inplace_region'):
            regions.append(snapshot['inplace_region'])
        for inplace in regions:
            try:
                archive = Path(inplace['archive'])
                with archive.open('r+b') as handle:
                    handle.seek(int(inplace['offset']))
                    handle.write(inplace['raw'])
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception as exc:
                label = inplace.get('name') or f"{inplace.get('offset', 0):#x}"
                errors.append(f'in-place region {label}: {exc}')
        for key, item in snapshot.get('groups', {}).items():
            try:
                archive = Path(item['archive'])
                with archive.open('r+b') as handle:
                    handle.truncate(int(item['archive_size']))
                    handle.flush()
                    os.fsync(handle.fileno())
                atomic_write_bytes(item['index'], item['index_bytes'], '.transaction.rollback')
                self.installation.invalidate_archive(str(key))
            except Exception as exc:
                errors.append(f'archive {key}: {exc}')
        for name, item in snapshot.get('states', {}).items():
            try:
                path = Path(item['path'])
                if item['exists']:
                    atomic_write_bytes(path, item['bytes'] or b'', '.transaction.rollback')
                else:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f'state {name}: {exc}')
        for name, item in snapshot.get('directories', {}).items():
            try:
                root = Path(item['path'])
                root.mkdir(parents=True, exist_ok=True)
                wanted = set(item['files'])
                for child in root.rglob('*'):
                    if child.is_file() and child.relative_to(root).as_posix() not in wanted:
                        child.unlink()
                for relative, payload in item['files'].items():
                    destination = root / Path(*relative.split('/'))
                    atomic_write_bytes(destination, payload, '.transaction.rollback')
            except Exception as exc:
                errors.append(f'directory {name}: {exc}')
        return errors


class ManagedPaintTransaction:
    """Compatibility adapter for the existing paint transaction metadata."""

    def __init__(self, installation: GameInstallation, state_path: str | Path, image_dir: str | Path):
        self.base = AppendRepointTransaction(installation)
        self.state_path = Path(state_path)
        self.image_dir = Path(image_dir)

    def snapshot(self, archive_keys, inplace_thumbnail=None) -> dict:
        inplace = None
        if inplace_thumbnail:
            archive, row, raw, _entry = inplace_thumbnail
            inplace = {'archive': archive, 'offset': int(row['offset']),
                       'size': int(row['size']), 'raw': bytes(raw)}
        canonical = self.base.snapshot(
            archive_keys, state_files={'managed_paints': self.state_path},
            inplace_region=inplace,
        )
        state = canonical['states']['managed_paints']
        image_files = {
            child.relative_to(self.image_dir).as_posix()
            for child in self.image_dir.rglob('*') if child.is_file()
        } if self.image_dir.is_dir() else set()
        groups = {
            key: {'archive': item['archive'], 'archive_size': item['archive_size'],
                  'cdf': item['index'], 'cdf_bytes': item['index_bytes']}
            for key, item in canonical['groups'].items()
        }
        legacy_inplace = canonical['inplace_region']
        if legacy_inplace:
            legacy_inplace = {**legacy_inplace, 'raw': bytes(legacy_inplace['raw'])}
        return {'groups': groups, 'state_exists': state['exists'], 'state_bytes': state['bytes'],
                'image_files': image_files, 'image_overwrites': {},
                'inplace_thumbnail': legacy_inplace, '_canonical': canonical}

    def restore(self, snapshot: dict) -> list[str]:
        canonical = snapshot.get('_canonical')
        if canonical is None:
            canonical = {
                'groups': {
                    key: {'archive': item['archive'], 'archive_size': item['archive_size'],
                          'index': item['cdf'], 'index_bytes': item['cdf_bytes']}
                    for key, item in snapshot.get('groups', {}).items()
                },
                'states': {'managed_paints': {
                    'path': str(self.state_path), 'exists': bool(snapshot.get('state_exists')),
                    'bytes': snapshot.get('state_bytes'),
                }},
                'directories': {},
                'inplace_region': snapshot.get('inplace_thumbnail'),
            }
        errors = self.base.restore(canonical)
        try:
            self.image_dir.mkdir(parents=True, exist_ok=True)
            for name, payload in (snapshot.get('image_overwrites') or {}).items():
                atomic_write_bytes(self.image_dir / Path(*name.split('/')), payload,
                                   '.transaction.rollback')
            before = set(snapshot.get('image_files', set()))
            for child in self.image_dir.rglob('*'):
                if child.is_file() and child.relative_to(self.image_dir).as_posix() not in before:
                    child.unlink()
        except Exception as exc:
            errors.append('source images: ' + str(exc))
        return errors


class TeamAssetTransaction:
    """Compatibility adapter for team-art archive and sidecar rollbacks."""

    def __init__(
        self, installation: GameInstallation, team_state_path: str | Path,
        managed_paint_state_path: str | Path,
    ):
        self.base = AppendRepointTransaction(installation)
        self.team_state_path = Path(team_state_path)
        self.managed_paint_state_path = Path(managed_paint_state_path)

    def snapshot(self, archive_keys=('0', '1')) -> dict:
        canonical = self.base.snapshot(
            archive_keys,
            state_files={
                'team_manager': self.team_state_path,
                'managed_paints': self.managed_paint_state_path,
            },
        )
        team_state = canonical['states']['team_manager']
        paint_state = canonical['states']['managed_paints']
        groups = {
            key: {
                'archive': item['archive'], 'size': item['archive_size'],
                'cdf': item['index'], 'cdf_bytes': item['index_bytes'],
            }
            for key, item in canonical['groups'].items()
        }
        return {
            'groups': groups,
            'state_exists': team_state['exists'],
            'state_bytes': team_state['bytes'],
            'extra_state_captured': True,
            'extra_state_exists': paint_state['exists'],
            'extra_state_bytes': paint_state['bytes'],
            '_canonical': canonical,
        }

    def restore(self, snapshot: dict) -> list[str]:
        canonical = snapshot.get('_canonical')
        if canonical is None:
            states = {
                'team_manager': {
                    'path': str(self.team_state_path),
                    'exists': bool(snapshot.get('state_exists')),
                    'bytes': snapshot.get('state_bytes'),
                }
            }
            if snapshot.get('extra_state_captured'):
                states['managed_paints'] = {
                    'path': str(self.managed_paint_state_path),
                    'exists': bool(snapshot.get('extra_state_exists')),
                    'bytes': snapshot.get('extra_state_bytes'),
                }
            canonical = {
                'groups': {
                    key: {
                        'archive': item['archive'], 'archive_size': item['size'],
                        'index': item['cdf'], 'index_bytes': item['cdf_bytes'],
                    }
                    for key, item in snapshot.get('groups', {}).items()
                },
                'states': states,
                'directories': {},
                'inplace_region': None,
            }
        return self.base.restore(canonical)


class ManagedPaintCheckpoint:
    """Durable one-level undo checkpoint for managed-paint transactions."""

    FORMAT = 'nascar15-extra-scheme-rollback-v2'

    def __init__(
        self, transaction: ManagedPaintTransaction, rollback_dir: str | Path,
    ):
        self.transaction = transaction
        self.rollback_dir = Path(rollback_dir)
        self.state_path = transaction.state_path
        self.image_dir = transaction.image_dir

    @staticmethod
    def _sha256_file(path: str | Path, start=0, size=None) -> str:
        digest = hashlib.sha256()
        with Path(path).open('rb') as handle:
            handle.seek(int(start))
            left = None if size is None else int(size)
            while True:
                chunk = handle.read(1024 * 1024 if left is None else min(1024 * 1024, left))
                if not chunk:
                    break
                digest.update(chunk)
                if left is not None:
                    left -= len(chunk)
                    if left <= 0:
                        break
        return digest.hexdigest()

    def _image_fingerprint(self) -> dict[str, str]:
        if not self.image_dir.is_dir():
            return {}
        return {
            child.name: self._sha256_file(child)
            for child in sorted(self.image_dir.iterdir()) if child.is_file()
        }

    def clear(self) -> bool:
        existed = self.rollback_dir.is_dir()
        if existed:
            shutil.rmtree(self.rollback_dir)
        return existed

    def persist(self, snapshot: dict, label: str, operation: dict | None = None) -> dict:
        if not snapshot:
            raise ValueError('paint rollback snapshot is empty')
        temporary = Path(str(self.rollback_dir) + '.tmp')
        if temporary.is_dir():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        groups = {}
        for key, item in snapshot.get('groups', {}).items():
            backup_name = f'cdf_{key}.bin'
            (temporary / backup_name).write_bytes(item['cdf_bytes'])
            groups[str(key)] = {
                'archive': str(item['archive']),
                'archive_size': int(item['archive_size']),
                'cdf': str(item['cdf']),
                'cdf_backup': backup_name,
            }
        state_name = None
        if snapshot.get('state_exists'):
            state_name = 'extra_schemes_state.bin'
            (temporary / state_name).write_bytes(snapshot.get('state_bytes') or b'')
        images = temporary / 'images'
        images.mkdir()
        if self.image_dir.is_dir():
            for child in self.image_dir.iterdir():
                if child.is_file():
                    shutil.copy2(child, images / child.name)
        inplace_meta = None
        inplace = snapshot.get('inplace_thumbnail')
        if inplace:
            raw_name = 'inplace_thumbnail.bin'
            (temporary / raw_name).write_bytes(inplace['raw'])
            inplace_meta = {
                'archive': str(inplace['archive']), 'offset': int(inplace['offset']),
                'size': int(inplace['size']), 'raw_backup': raw_name,
            }
        manifest = {
            'format': self.FORMAT, 'version': 2, 'mode': 'restore_pre',
            'label': str(label or 'Last paint change'),
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            'groups': groups, 'state_exists': bool(snapshot.get('state_exists')),
            'state_backup': state_name, 'inplace_thumbnail': inplace_meta,
            'operation': dict(operation or {}), 'post_state': None,
        }
        atomic_write_json(temporary / 'manifest.json', manifest, indent=2)
        self.clear()
        os.replace(temporary, self.rollback_dir)
        return manifest

    def seal(self, operation: dict | None = None) -> dict:
        path = self.rollback_dir / 'manifest.json'
        if not path.is_file():
            raise ValueError('paint rollback manifest vanished before it could be sealed')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('format') != self.FORMAT:
            raise ValueError('paint rollback manifest is not the reversible v2 format')
        post_groups = {}
        for key, item in (manifest.get('groups') or {}).items():
            archive = Path(item['archive']).resolve()
            index = Path(item['cdf']).resolve()
            pre_size = int(item['archive_size'])
            post_size = archive.stat().st_size
            if post_size < pre_size:
                raise ValueError(f'ARCHIVE{key} shrank during paint creation')
            post_groups[str(key)] = {
                'archive_size': post_size,
                'tail_size': post_size - pre_size,
                'tail_sha256': self._sha256_file(archive, pre_size, post_size - pre_size),
                'cdf_size': index.stat().st_size,
                'cdf_sha256': self._sha256_file(index),
            }
        state_exists = self.state_path.is_file()
        manifest['post_state'] = {
            'groups': post_groups,
            'state_exists': state_exists,
            'state_sha256': self._sha256_file(self.state_path) if state_exists else None,
            'images': self._image_fingerprint(),
        }
        if operation:
            manifest['operation'] = dict(operation)
        atomic_write_json(path, manifest, indent=2)
        return manifest

    def verify_post_state(self, manifest: dict) -> bool:
        post = manifest.get('post_state') or {}
        if not post.get('groups'):
            raise ValueError(
                'this paint checkpoint predates exact delete support; restore a clean game '
                'and create the slot again with this release'
            )
        for key, expected in post['groups'].items():
            item = (manifest.get('groups') or {}).get(str(key)) or {}
            archive = Path(item.get('archive') or '').resolve()
            index = Path(item.get('cdf') or '').resolve()
            pre_size = int(item.get('archive_size', -1))
            if not archive.is_file() or not index.is_file():
                raise ValueError(f'ARCHIVE{key} checkpoint target is missing')
            post_size = int(expected['archive_size'])
            if archive.stat().st_size != post_size:
                raise ValueError(f'ARCHIVE{key} changed after this slot was created; exact delete is blocked')
            tail_size = int(expected['tail_size'])
            if post_size - pre_size != tail_size:
                raise ValueError(f'ARCHIVE{key} append geometry no longer matches the creation checkpoint')
            if self._sha256_file(archive, pre_size, tail_size) != expected['tail_sha256']:
                raise ValueError(f'ARCHIVE{key} appended bytes changed after this slot was created; exact delete is blocked')
            if (index.stat().st_size != int(expected['cdf_size']) or
                    self._sha256_file(index) != expected['cdf_sha256']):
                raise ValueError(f'cdfiles{key}.dat changed after this slot was created; exact delete is blocked')
        state_exists = self.state_path.is_file()
        if bool(post.get('state_exists')) != state_exists:
            raise ValueError('the app-created paint state changed after this slot was created')
        if state_exists and self._sha256_file(self.state_path) != post.get('state_sha256'):
            raise ValueError('the app-created paint state changed after this slot was created')
        if self._image_fingerprint() != (post.get('images') or {}):
            raise ValueError('saved paint/thumbnail files changed after this slot was created')
        return True

    def load(self) -> tuple[dict, dict]:
        path = self.rollback_dir / 'manifest.json'
        if not path.is_file():
            raise ValueError('there is no paint change to undo')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('format') not in (
            'nascar15-extra-scheme-rollback-v1', self.FORMAT,
        ):
            raise ValueError('the saved paint rollback manifest is not recognized')
        groups = {}
        for key, item in (manifest.get('groups') or {}).items():
            key = str(key)
            pair = self.transaction.base.installation.archive_pairs.get(key)
            archive = Path(item['archive']).resolve()
            index = Path(item['cdf']).resolve()
            if pair is None or pair.archive.resolve() != archive or pair.index.resolve() != index:
                raise ValueError('the saved paint undo belongs to a different NASCAR 15 installation')
            if not archive.is_file() or not index.is_file():
                raise ValueError(f'the saved paint undo target for ARCHIVE{key} no longer exists')
            original_size = int(item['archive_size'])
            if archive.stat().st_size < original_size:
                raise ValueError(f'ARCHIVE{key} is smaller than the saved pre-change size; refusing an unsafe undo')
            cdf_bytes = (self.rollback_dir / item['cdf_backup']).read_bytes()
            if len(cdf_bytes) < 64 or cdf_bytes[:4] != b'filC':
                raise ValueError(f'the saved CDF backup for ARCHIVE{key} is invalid')
            groups[key] = {
                'archive': str(archive), 'archive_size': original_size,
                'cdf': str(index), 'cdf_bytes': cdf_bytes,
            }
        state_bytes = None
        if manifest.get('state_exists') and manifest.get('state_backup'):
            state_bytes = (self.rollback_dir / manifest['state_backup']).read_bytes()
        image_overwrites = {}
        image_files = set()
        images = self.rollback_dir / 'images'
        if images.is_dir():
            for child in images.iterdir():
                if child.is_file():
                    image_files.add(child.name)
                    image_overwrites[child.name] = child.read_bytes()
        inplace = None
        metadata = manifest.get('inplace_thumbnail')
        if metadata:
            inplace = {
                'archive': metadata['archive'], 'offset': int(metadata['offset']),
                'size': int(metadata['size']),
                'raw': (self.rollback_dir / metadata['raw_backup']).read_bytes(),
            }
        return {
            'groups': groups, 'state_exists': bool(manifest.get('state_exists')),
            'state_bytes': state_bytes, 'image_files': image_files,
            'image_overwrites': image_overwrites, 'inplace_thumbnail': inplace,
        }, manifest

    def prepare_delete_redo(self, manifest: dict, uid: int) -> Path:
        """Capture the exact post-create state before restoring its pre-state."""
        temporary = Path(str(self.rollback_dir) + '.redo.tmp')
        if temporary.is_dir():
            shutil.rmtree(temporary)
        shutil.copytree(self.rollback_dir, temporary)
        redo_groups = {}
        for key, item in (manifest.get('groups') or {}).items():
            archive = Path(item['archive']).resolve()
            index = Path(item['cdf']).resolve()
            base_size = int(item['archive_size'])
            current_size = archive.stat().st_size
            tail_name = f'redo_tail_{key}.bin'
            with archive.open('rb') as handle:
                handle.seek(base_size)
                tail = handle.read(current_size - base_size)
            (temporary / tail_name).write_bytes(tail)
            index_name = f'redo_cdf_{key}.bin'
            shutil.copy2(index, temporary / index_name)
            redo_groups[str(key)] = {
                'tail_backup': tail_name, 'tail_size': len(tail),
                'cdf_backup': index_name,
            }
        redo_state_name = None
        state_exists = self.state_path.is_file()
        if state_exists:
            redo_state_name = 'redo_state.bin'
            shutil.copy2(self.state_path, temporary / redo_state_name)
        redo_images = temporary / 'redo_images'
        redo_images.mkdir()
        if self.image_dir.is_dir():
            for child in self.image_dir.iterdir():
                if child.is_file():
                    shutil.copy2(child, redo_images / child.name)
        updated = dict(manifest)
        updated['mode'] = 'redo_post'
        updated['label'] = f'Undo deletion of paint slot UID {int(uid)}'
        updated['redo'] = {
            'groups': redo_groups, 'state_exists': state_exists,
            'state_backup': redo_state_name, 'images_dir': 'redo_images',
        }
        atomic_write_json(temporary / 'manifest.json', updated, indent=2)
        return temporary

    def verify_pre_state(self, snapshot: dict) -> bool:
        for key, item in snapshot.get('groups', {}).items():
            if Path(item['archive']).stat().st_size != int(item['archive_size']):
                raise ValueError(f'ARCHIVE{key} is not at the exact pre-create size; deleted-slot undo is blocked')
            if Path(item['cdf']).read_bytes() != item['cdf_bytes']:
                raise ValueError(f'cdfiles{key}.dat is not at the exact pre-create state; deleted-slot undo is blocked')
        state_exists = self.state_path.is_file()
        if bool(snapshot.get('state_exists')) != state_exists:
            raise ValueError('the app-created paint state is not at the exact pre-create state')
        if state_exists and self.state_path.read_bytes() != (snapshot.get('state_bytes') or b''):
            raise ValueError('the app-created paint state is not at the exact pre-create state')
        current_images = {
            child.name for child in self.image_dir.iterdir() if child.is_file()
        } if self.image_dir.is_dir() else set()
        if current_images != set(snapshot.get('image_files') or set()):
            raise ValueError('saved paint/thumbnail files are not at the exact pre-create state')
        for name, raw in (snapshot.get('image_overwrites') or {}).items():
            path = self.image_dir / name
            if not path.is_file() or path.read_bytes() != raw:
                raise ValueError('saved paint/thumbnail files are not at the exact pre-create state')
        return True

    def reapply_deleted_slot(self, snapshot: dict, manifest: dict) -> dict:
        self.verify_pre_state(snapshot)
        redo = manifest.get('redo') or {}
        current = self.transaction.snapshot(tuple(snapshot.get('groups', {})))
        try:
            for key, item in snapshot['groups'].items():
                redo_group = (redo.get('groups') or {}).get(str(key)) or {}
                tail = (self.rollback_dir / redo_group['tail_backup']).read_bytes()
                if len(tail) != int(redo_group['tail_size']):
                    raise ValueError(f'ARCHIVE{key} redo tail is incomplete')
                with Path(item['archive']).open('ab') as handle:
                    handle.write(tail)
                    handle.flush()
                    os.fsync(handle.fileno())
                index_bytes = (self.rollback_dir / redo_group['cdf_backup']).read_bytes()
                atomic_write_bytes(item['cdf'], index_bytes, '.transaction.redo')
                self.transaction.base.installation.invalidate_archive(str(key))
            if redo.get('state_exists'):
                raw = (self.rollback_dir / redo['state_backup']).read_bytes()
                atomic_write_bytes(self.state_path, raw, '.transaction.redo')
            else:
                self.state_path.unlink(missing_ok=True)
            self.image_dir.mkdir(parents=True, exist_ok=True)
            for child in self.image_dir.iterdir():
                if child.is_file():
                    child.unlink()
            redo_images = self.rollback_dir / (redo.get('images_dir') or 'redo_images')
            if redo_images.is_dir():
                for child in redo_images.iterdir():
                    if child.is_file():
                        shutil.copy2(child, self.image_dir / child.name)
            self.verify_post_state(manifest)
        except Exception:
            self.transaction.restore(current)
            raise
        updated = dict(manifest)
        updated['mode'] = 'restore_pre'
        uid = int((updated.get('operation') or {}).get('uid', -1))
        updated['label'] = f'Delete paint slot UID {uid}' if uid >= 0 else 'Undo restored paint slot'
        atomic_write_json(self.rollback_dir / 'manifest.json', updated, indent=2)
        return updated


class TeamAssetCheckpoint:
    """Durable one-level checkpoint for team presentation transactions."""

    FORMAT = 'nascar15-team-asset-rollback-v1'

    def __init__(self, transaction: TeamAssetTransaction, rollback_dir: str | Path):
        self.transaction = transaction
        self.rollback_dir = Path(rollback_dir)

    @staticmethod
    def _active_paint_uids(payload: bytes | None) -> set[int]:
        if not payload:
            return set()
        try:
            state = json.loads(payload.decode('utf-8'))
            return {
                int(item['uid']) for item in state.get('schemes', [])
                if item.get('uid') is not None and not item.get('superseded_by')
            }
        except Exception:
            return set()

    def clear(self) -> bool:
        existed = self.rollback_dir.is_dir()
        if existed:
            shutil.rmtree(self.rollback_dir)
        return existed

    def restore_block(self, manifest: dict) -> str | None:
        current_payload = (
            self.transaction.managed_paint_state_path.read_bytes()
            if self.transaction.managed_paint_state_path.is_file() else None
        )
        current = self._active_paint_uids(current_payload)
        if int((manifest or {}).get('safety_version') or 0) >= 2:
            captured = {
                int(value) for value in
                ((manifest or {}).get('captured_active_scheme_uids') or [])
            }
            added = sorted(current - captured)
            if added:
                ids = ', '.join(str(value) for value in added[:8])
                return (
                    f'This restore point is older than paint slots you have since added ({ids}). '
                    'Using it would leave the game and the app disagreeing about what exists.'
                )
        elif current:
            return (
                'This legacy restore point did not capture app-created paint state and is '
                'disabled to prevent archive/state desynchronization.'
            )
        return None

    def persist(self, snapshot: dict, label: str) -> dict:
        temporary = Path(str(self.rollback_dir) + '.tmp')
        if temporary.is_dir():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        captured_uids = self._active_paint_uids(snapshot.get('extra_state_bytes'))
        now = datetime.datetime.now().astimezone()
        manifest = {
            'format': self.FORMAT, 'safety_version': 2,
            'created': now.isoformat(timespec='seconds'),
            'created_epoch': time.time(),
            'label': str(label or 'Team presentation change'),
            'groups': {}, 'state_exists': bool(snapshot.get('state_exists')),
            'extra_state_captured': True,
            'extra_state_exists': bool(snapshot.get('extra_state_exists')),
            'captured_active_scheme_uids': sorted(captured_uids),
        }
        for key, item in snapshot.get('groups', {}).items():
            backup_name = f'cdf_{key}.bin'
            (temporary / backup_name).write_bytes(item['cdf_bytes'])
            manifest['groups'][str(key)] = {
                'archive': str(Path(item['archive']).resolve()),
                'size': int(item['size']),
                'cdf': str(Path(item['cdf']).resolve()),
                'cdf_backup': backup_name,
            }
        if snapshot.get('state_exists'):
            (temporary / 'team_state.bin').write_bytes(snapshot.get('state_bytes') or b'')
            manifest['state_backup'] = 'team_state.bin'
        if snapshot.get('extra_state_exists'):
            (temporary / 'extra_scheme_state.bin').write_bytes(
                snapshot.get('extra_state_bytes') or b''
            )
            manifest['extra_state_backup'] = 'extra_scheme_state.bin'
        atomic_write_json(temporary / 'manifest.json', manifest, indent=2)
        self.clear()
        os.replace(temporary, self.rollback_dir)
        return manifest

    def info(self) -> dict:
        try:
            manifest = json.loads((self.rollback_dir / 'manifest.json').read_text(encoding='utf-8'))
            blocked = self.restore_block(manifest)
            return {
                'available': not bool(blocked), 'created': manifest.get('created'),
                'label': manifest.get('label'), 'blocked_reason': blocked,
                'safety_version': int(manifest.get('safety_version') or 0),
            }
        except Exception:
            return {'available': False}

    def load(self) -> tuple[dict, dict]:
        manifest = json.loads((self.rollback_dir / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('format') != self.FORMAT:
            raise ValueError('the saved team rollback has an unknown format')
        blocked = self.restore_block(manifest)
        if blocked:
            raise ValueError(blocked)
        snapshot = {
            'groups': {}, 'state_exists': bool(manifest.get('state_exists')),
            'state_bytes': None,
            'extra_state_captured': bool(manifest.get('extra_state_captured')),
            'extra_state_exists': bool(manifest.get('extra_state_exists')),
            'extra_state_bytes': None,
        }
        installation = self.transaction.base.installation
        for key, item in (manifest.get('groups') or {}).items():
            pair = installation.archive_pairs.get(str(key))
            archive = Path(item['archive']).resolve()
            index = Path(item['cdf']).resolve()
            if pair is None or pair.archive.resolve() != archive or pair.index.resolve() != index:
                raise ValueError('the saved rollback belongs to a different NASCAR 15 installation')
            saved_size = int(item['size'])
            if archive.stat().st_size < saved_size:
                raise ValueError(f'ARCHIVE{key} is smaller than the saved pre-change size; refusing an unsafe team undo')
            index_bytes = (self.rollback_dir / item['cdf_backup']).read_bytes()
            if len(index_bytes) < 64 or index_bytes[:4] != b'filC':
                raise ValueError(f'the saved CDF backup for ARCHIVE{key} is invalid')
            snapshot['groups'][str(key)] = {
                'archive': str(archive), 'size': saved_size,
                'cdf': str(index), 'cdf_bytes': index_bytes,
            }
        if snapshot['state_exists']:
            snapshot['state_bytes'] = (
                self.rollback_dir / (manifest.get('state_backup') or 'team_state.bin')
            ).read_bytes()
        if snapshot['extra_state_captured'] and snapshot['extra_state_exists']:
            snapshot['extra_state_bytes'] = (
                self.rollback_dir /
                (manifest.get('extra_state_backup') or 'extra_scheme_state.bin')
            ).read_bytes()
        return snapshot, manifest
