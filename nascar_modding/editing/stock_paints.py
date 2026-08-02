"""Transactional smart-image installation for existing stock paint slots."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from nascar_modding.core.processes import assert_process_closed
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.livery_wrappers import (
    HD_ENTRY_SIZE, SD_ENTRY_SIZE, NativeLiveryWrapperEditor,
)
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.editing.transactions import AppendRepointTransaction
from nascar_modding.games.installation import GameInstallation


PROCESS_NAMES = {
    'nascar13': 'NTG2013.exe', 'nascar14': 'NASCAR14.exe', 'nascar15': 'NASCAR15.exe',
}


class StockPaintEditor:
    """Own the proven fixed-size SD/HD image writer used by both frontends."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.resources = ResourceEditor(installation)
        self.wrappers = NativeLiveryWrapperEditor(installation)

    def _commit(self, writes, keys) -> None:
        regions = [
            {'archive': self.installation.archive_pairs[key].archive,
             'offset': int(entry.archive_offset), 'size': int(entry.size), 'name': entry.name}
            for key, entry, _payload, _kind in writes
        ]
        transaction = AppendRepointTransaction(self.installation)
        snapshot = transaction.snapshot(keys, inplace_regions=regions)
        try:
            for key, entry, payload, kind in writes:
                archive = self.installation.archive_pairs[key].archive
                with archive.open('r+b') as handle:
                    handle.seek(entry.archive_offset)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.seek(entry.archive_offset)
                    if handle.read(entry.size) != payload:
                        raise IOError(f'{kind} paint read-back mismatch')
        except Exception as exc:
            errors = transaction.restore(snapshot)
            detail = str(exc)
            if errors:
                detail += ' | Rollback warnings: ' + '; '.join(errors)
            raise RuntimeError(detail) from exc

    def install(
        self, sd_name: str, image_path: str | Path, *, hd_name: str | None | bool = None,
        layer_path: str | Path | None = None,
    ) -> dict:
        assert_process_closed(PROCESS_NAMES[self.installation.profile.id], 'installing a stock paint')
        sd_key, sd_entry = self.installation.find_entry(str(sd_name))
        if int(sd_entry.size) != SD_ENTRY_SIZE:
            raise ValueError(f'{sd_entry.name} is not a proven native SD livery wrapper')
        resolved_hd = None if hd_name is False else (str(hd_name) if hd_name else 'HD' + str(sd_name))
        try:
            hd_key, hd_entry = self.installation.find_entry(resolved_hd) if resolved_hd else (None, None)
        except KeyError:
            hd_key = hd_entry = None
        if hd_entry is not None and int(hd_entry.size) != HD_ENTRY_SIZE:
            raise ValueError(f'{hd_entry.name} is not a proven native HD livery wrapper')
        keys = tuple(dict.fromkeys([sd_key] + ([hd_key] if hd_key is not None else [])))
        backup = BackupManager(self.installation).create_missing(keys)
        if not backup['ok']:
            raise IOError('could not create paint backups: ' + '; '.join(backup['failed']))
        image = Image.open(image_path)
        image.load()
        image = image.convert('RGB')
        layer = None
        if layer_path and Path(layer_path).is_file():
            layer_image = Image.open(layer_path)
            layer_image.load()
            layer = layer_image.split()[3] if layer_image.mode == 'RGBA' else layer_image.convert('L')
        sd_pristine = self.resources.read(sd_entry.name, sd_key, pristine=True)
        sd_payload, sd_levels, sd_changed = self.wrappers.patch_sd(sd_pristine, image, layer)
        writes = [(sd_key, sd_entry, sd_payload, 'SD')]
        hd_levels, hd_changed = [], 0
        if hd_entry is not None:
            hd_pristine = self.resources.read(hd_entry.name, hd_key, pristine=True)
            hd_payload, hd_levels, hd_changed = self.wrappers.patch_hd(
                hd_pristine, image, stock_atlas_alignment=True,
            )
            writes.append((hd_key, hd_entry, hd_payload, 'HD'))
        self._commit(writes, keys)
        return {
            'ok': True, 'verified': True, 'sd': sd_entry.name,
            'hd': hd_entry.name if hd_entry is not None else None,
            'sd_levels': sd_levels, 'sd_changed_bytes': sd_changed,
            'hd_levels': hd_levels, 'hd_changed_bytes': hd_changed,
            'layer_masked': layer is not None,
        }

    def restore(self, sd_name: str, *, hd_name: str | None | bool = None) -> dict:
        assert_process_closed(PROCESS_NAMES[self.installation.profile.id], 'restoring a stock paint')
        sd_key, sd_entry = self.installation.find_entry(str(sd_name))
        resolved_hd = None if hd_name is False else (str(hd_name) if hd_name else 'HD' + str(sd_name))
        try:
            hd_key, hd_entry = self.installation.find_entry(resolved_hd) if resolved_hd else (None, None)
        except KeyError:
            hd_key = hd_entry = None
        keys = tuple(dict.fromkeys([sd_key] + ([hd_key] if hd_key is not None else [])))
        backup = BackupManager(self.installation).create_missing(keys)
        if not backup['ok']:
            raise IOError('could not verify paint backups: ' + '; '.join(backup['failed']))
        writes = [(sd_key, sd_entry, self.resources.read(sd_entry.name, sd_key, pristine=True), 'SD')]
        if hd_entry is not None:
            writes.append((hd_key, hd_entry,
                           self.resources.read(hd_entry.name, hd_key, pristine=True), 'HD'))
        self._commit(writes, keys)
        return {'ok': True, 'verified': True, 'sd': sd_entry.name,
                'hd': hd_entry.name if hd_entry is not None else None}
