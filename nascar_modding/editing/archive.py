"""Safe fixed-size archive entry editing shared by all frontends."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct
import threading

from nascar_modding.core.cdf import read_cdf
from nascar_modding.core.files import atomic_write_bytes
from nascar_modding.games.installation import GameInstallation


MOD_BACKUP_SUFFIX = '.n15mod.bak'
LEGACY_BACKUP_SUFFIX = '.gridapp.bak'


def backup_path(live_path: str | Path) -> str:
    """Return the oldest compatible pristine backup path by release history."""
    modern = str(live_path) + MOD_BACKUP_SUFFIX
    legacy = str(live_path) + LEGACY_BACKUP_SUFFIX
    if os.path.exists(legacy):
        return legacy
    if os.path.exists(modern):
        return modern
    return modern


def _durable_region_write(path: Path, offset: int, payload: bytes) -> None:
    with path.open('r+b') as handle:
        handle.seek(offset)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class ArchiveEntryEditor:
    """Exact-size entry replacement with backup, verification, and rollback."""

    _write_lock = threading.RLock()

    def __init__(self, installation: GameInstallation):
        self.installation = installation

    def _location(self, name: str, archive_key: str | None = None):
        key, entry = self.installation.find_entry(name, archive_key)
        return key, self.installation.archive_pairs[key].archive, entry

    def ensure_backup(self, archive_key: str) -> Path:
        pair = self.installation.archive_pairs[str(archive_key)]
        destination = Path(backup_path(pair.archive))
        if not destination.exists():
            shutil.copy2(pair.archive, destination)
        if destination.stat().st_size <= 0:
            raise IOError(f'backup is empty for {pair.archive.name}')
        if destination.stat().st_size > pair.archive.stat().st_size:
            raise IOError(f'backup is larger than live {pair.archive.name}')
        return destination

    @staticmethod
    def _ensure_file_backup(path: Path) -> Path:
        destination = Path(backup_path(path))
        if not destination.exists():
            shutil.copy2(path, destination)
        return destination

    def export_entry(self, name: str, destination: str | Path, archive_key: str | None = None) -> Path:
        payload = self.installation.read_entry(name, archive_key)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        return output

    def replace_entry(self, name: str, payload: bytes, archive_key: str | None = None) -> dict:
        key, archive, entry = self._location(name, archive_key)
        if len(payload) != entry.size:
            raise ValueError(
                f'{name} requires exactly {entry.size} bytes; received {len(payload)}'
            )
        with self._write_lock:
            self.ensure_backup(key)
            before = self.installation.read_entry(name, key)
            try:
                _durable_region_write(archive, entry.archive_offset, payload)
                verified = self.installation.read_entry(name, key)
                if verified != payload:
                    raise IOError(f'read-back verification failed for {name}')
            except Exception as install_error:
                try:
                    _durable_region_write(archive, entry.archive_offset, before)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'entry write and rollback both failed: {install_error}; '
                        f'rollback: {rollback_error}'
                    ) from install_error
                raise
        return {'archive': key, 'name': entry.name, 'size': entry.size, 'verified': True}

    def replace_or_repoint_entry(
        self,
        name: str,
        payload: bytes,
        archive_key: str | None = None,
    ) -> dict:
        """Replace an entry, appending and atomically repointing if size changed."""
        key, archive, entry = self._location(name, archive_key)
        if not payload:
            raise ValueError('replacement payload is empty')
        if len(payload) == entry.size:
            result = self.replace_entry(name, payload, key)
            result['method'] = 'in_place'
            return result

        pair = self.installation.archive_pairs[key]
        with self._write_lock:
            self.ensure_backup(key)
            self._ensure_file_backup(pair.index)
            old_archive_size = archive.stat().st_size
            old_index = pair.index.read_bytes()
            new_offset = (old_archive_size + 15) & ~15
            if new_offset + len(payload) >= 2**32:
                raise ValueError('replacement would exceed the 32-bit archive offset limit')
            try:
                with archive.open('ab') as handle:
                    handle.write(b'\0' * (new_offset - old_archive_size))
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                with archive.open('rb') as handle:
                    handle.seek(new_offset)
                    if handle.read(len(payload)) != payload:
                        raise IOError(f'payload read-back failed for {name}')

                updated = bytearray(old_index)
                if entry.layout == 'A':
                    size_pos, offset_pos = entry.record_offset + 8, entry.record_offset + 20
                elif entry.layout == 'B':
                    size_pos, offset_pos = entry.record_offset + 16, entry.record_offset + 28
                else:
                    raise ValueError(f'unsupported cdf layout {entry.layout}')
                struct.pack_into('<I', updated, size_pos, len(payload))
                struct.pack_into('<I', updated, offset_pos, new_offset)
                atomic_write_bytes(pair.index, bytes(updated), '.repoint.tmp')
                self.installation.invalidate_archive(key)
                verify_key, verify_entry = self.installation.find_entry(name, key)
                if (
                    verify_key != key
                    or verify_entry.archive_offset != new_offset
                    or verify_entry.size != len(payload)
                    or self.installation.read_entry(name, key) != payload
                ):
                    raise IOError(f'cdf/payload read-back failed for {name}')
            except Exception as install_error:
                rollback_errors = []
                try:
                    with archive.open('r+b') as handle:
                        handle.truncate(old_archive_size)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception as exc:
                    rollback_errors.append(f'archive: {exc}')
                try:
                    atomic_write_bytes(pair.index, old_index, '.rollback.tmp')
                    self.installation.invalidate_archive(key)
                except Exception as exc:
                    rollback_errors.append(f'index: {exc}')
                if rollback_errors:
                    raise RuntimeError(
                        f'install failed ({install_error}) and rollback failed '
                        + '; '.join(rollback_errors)
                    ) from install_error
                raise
        return {
            'archive': key,
            'name': entry.name,
            'size': len(payload),
            'old_size': entry.size,
            'offset': new_offset,
            'method': 'append_repoint',
            'verified': True,
        }

    def replace_entries(self, replacements: dict[str, bytes], archive_key: str) -> dict:
        """Install several entries as one archive/index transaction."""
        key = str(archive_key)
        if not replacements:
            raise ValueError('no archive entry replacements were supplied')
        folded = [name.casefold() for name in replacements]
        if len(set(folded)) != len(folded):
            raise ValueError('duplicate archive entry names were supplied')
        pair = self.installation.archive_pairs[key]
        with self._write_lock:
            locations = {}
            for name, payload in replacements.items():
                found_key, _archive, entry = self._location(name, key)
                if found_key != key:
                    raise ValueError(f'{name} is not in ARCHIVE{key}')
                locations[name] = (entry, self.installation.read_entry(name, key))
                if not payload:
                    raise ValueError(f'{name} replacement payload is empty')
            self.ensure_backup(key)
            self._ensure_file_backup(pair.index)
            original_size = pair.archive.stat().st_size
            original_index = pair.index.read_bytes()
            results = []
            try:
                for name, payload in replacements.items():
                    results.append(self.replace_or_repoint_entry(name, payload, key))
                for name, payload in replacements.items():
                    if self.installation.read_entry(name, key) != payload:
                        raise IOError(f'batch read-back failed for {name}')
            except Exception as install_error:
                rollback_errors = []
                try:
                    with pair.archive.open('r+b') as handle:
                        handle.truncate(original_size)
                        for entry, original in locations.values():
                            handle.seek(entry.archive_offset)
                            handle.write(original)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception as exc:
                    rollback_errors.append(f'archive: {exc}')
                try:
                    atomic_write_bytes(pair.index, original_index, '.batch_rollback.tmp')
                    self.installation.invalidate_archive(key)
                except Exception as exc:
                    rollback_errors.append(f'index: {exc}')
                if rollback_errors:
                    raise RuntimeError(
                        f'batch install failed ({install_error}) and rollback failed: '
                        + '; '.join(rollback_errors)
                    ) from install_error
                raise
        return {
            'archive': key,
            'entries': len(results),
            'verified': True,
            'writes': results,
        }

    def restore_entry(self, name: str, archive_key: str | None = None) -> dict:
        key, archive, entry = self._location(name, archive_key)
        source = Path(backup_path(archive))
        if not source.is_file():
            raise FileNotFoundError(f'no app backup exists for {archive.name}')
        pair = self.installation.archive_pairs[key]
        backup_index = Path(backup_path(pair.index))
        pristine_entry = entry
        if backup_index.is_file():
            matches = [item for item in read_cdf(backup_index) if item.name.casefold() == name.casefold()]
            if len(matches) != 1:
                raise IOError(f'backup index does not identify exactly one {name}')
            pristine_entry = matches[0]
        with source.open('rb') as handle:
            handle.seek(pristine_entry.archive_offset)
            payload = handle.read(pristine_entry.size)
        if len(payload) != pristine_entry.size:
            raise IOError(f'backup entry is truncated: {name}')
        return self.replace_or_repoint_entry(name, payload, key)
