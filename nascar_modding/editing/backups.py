"""Pristine archive/index backup and transactional restore workflows."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import time

from nascar_modding.editing.archive import backup_path
from nascar_modding.games.installation import GameInstallation


def _valid_game_file(path: Path, kind: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 64:
            return False
        with path.open('rb') as handle:
            head = handle.read(4)
    except OSError:
        return False
    if kind == 'index':
        return head == b'filC'
    return path.stat().st_size > 1024


def _durable_copy(source: Path, destination: Path) -> None:
    temporary = Path(str(destination) + '.tmp')
    try:
        shutil.copy2(source, temporary)
        with temporary.open('rb+') as handle:
            os.fsync(handle.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            raise IOError(f'backup copy is incomplete: {source.name}')
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class BackupManager:
    """Own all whole-file backup/restore behavior for every frontend."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation

    def status(self) -> list[dict]:
        rows = []
        for key, pair in sorted(
            self.installation.archive_pairs.items(), key=lambda item: (len(item[0]), item[0])
        ):
            for kind, live in (('archive', pair.archive), ('index', pair.index)):
                backup = Path(backup_path(live))
                rows.append({
                    'archive': key,
                    'kind': kind,
                    'live': str(live),
                    'name': live.name,
                    'live_size': live.stat().st_size if live.is_file() else None,
                    'backup': str(backup),
                    'backup_size': backup.stat().st_size if backup.is_file() else None,
                    'backup_valid': _valid_game_file(backup, kind),
                })
        return rows

    def create_missing(self, archive_keys=None) -> dict:
        created, existing, failed = [], [], []
        selected = None if archive_keys is None else {str(key) for key in archive_keys}
        for row in self.status():
            if selected is not None and str(row['archive']) not in selected:
                continue
            live, destination = Path(row['live']), Path(row['backup'])
            if destination.exists():
                existing.append(row['name'])
                continue
            try:
                if not _valid_game_file(live, row['kind']):
                    raise ValueError(f'{live.name} does not look like a valid game file')
                _durable_copy(live, destination)
                if not _valid_game_file(destination, row['kind']):
                    raise IOError(f'{destination.name} failed backup validation')
                created.append(row['name'])
            except PermissionError:
                failed.append(f'{row["name"]} (file locked - close the game and retry)')
            except Exception as exc:
                failed.append(f'{row["name"]} ({exc})')
        return {'ok': not failed, 'created': created, 'existing': len(existing), 'failed': failed}

    def restore_all(self) -> dict:
        """Stage and atomically swap every available pristine pair as one set."""
        targets, skipped = [], []
        for row in self.status():
            live, backup = Path(row['live']), Path(row['backup'])
            if not backup.exists():
                skipped.append(f'{row["name"]} (no pristine backup)')
                continue
            if not _valid_game_file(backup, row['kind']):
                raise ValueError(f'{backup.name} looks invalid; nothing was restored')
            if not live.exists():
                raise ValueError(f'{live.name} is missing; nothing was restored')
            targets.append((row['kind'], live, backup, row['archive']))
        if not targets:
            raise ValueError('no valid pristine backups are available')

        token = f'{os.getpid()}_{time.time_ns()}'
        staged, committed = [], []
        try:
            for kind, live, backup, archive_key in targets:
                temporary = Path(str(live) + '.restore_new_' + token)
                previous = Path(str(live) + '.restore_old_' + token)
                shutil.copy2(backup, temporary)
                with temporary.open('rb+') as handle:
                    os.fsync(handle.fileno())
                if temporary.stat().st_size != backup.stat().st_size or not _valid_game_file(temporary, kind):
                    raise IOError(f'staged restore validation failed for {live.name}')
                staged.append((kind, live, backup, temporary, previous, archive_key))

            for item in staged:
                _kind, live, _backup, temporary, previous, _archive_key = item
                if previous.exists():
                    previous.unlink()
                os.replace(live, previous)
                try:
                    os.replace(temporary, live)
                except Exception:
                    os.replace(previous, live)
                    raise
                committed.append(item)

            for kind, live, backup, _temporary, _previous, _archive_key in committed:
                if live.stat().st_size != backup.stat().st_size or not _valid_game_file(live, kind):
                    raise IOError(f'restored file readback failed for {live.name}')
        except Exception as install_error:
            rollback_errors = []
            for _kind, live, _backup, _temporary, previous, _archive_key in reversed(committed):
                try:
                    if previous.exists():
                        failed_live = Path(str(live) + '.restore_failed_new_' + token)
                        if failed_live.exists():
                            failed_live.unlink()
                        if live.exists():
                            os.replace(live, failed_live)
                        os.replace(previous, live)
                        if failed_live.exists():
                            failed_live.unlink()
                except Exception as exc:
                    rollback_errors.append(f'{live.name}: {exc}')
            if rollback_errors:
                raise RuntimeError(
                    f'restore failed ({install_error}); rollback also failed: '
                    + '; '.join(rollback_errors)
                ) from install_error
            raise
        finally:
            for _kind, _live, _backup, temporary, _previous, _archive_key in staged:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        for _kind, _live, _backup, _temporary, previous, archive_key in committed:
            try:
                previous.unlink()
            except FileNotFoundError:
                pass
            self.installation.invalidate_archive(archive_key)
        return {
            'ok': True,
            'restored': [item[1].name for item in committed],
            'skipped': skipped,
            'verified': True,
        }
