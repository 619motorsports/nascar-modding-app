"""Read-only access to an installed game's archive/index pairs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from nascar_modding.core.cdf import CdfEntry, read_cdf
from .profiles import GameProfile, get_profile


_CDF_RE = re.compile(r'^cdfiles(\d*)\.dat$', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ArchivePair:
    key: str
    archive: Path
    index: Path


class GameInstallation:
    def __init__(self, profile: GameProfile | str, root: str | Path):
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.root = Path(root).resolve()
        self.data_dir = self.root / 'data'
        if not self.data_dir.is_dir():
            raise ValueError(f'game data folder does not exist: {self.data_dir}')
        self._entries: dict[str, tuple[CdfEntry, ...]] = {}
        self._pairs = self._discover_pairs()

    def _discover_pairs(self) -> dict[str, ArchivePair]:
        pairs: dict[str, ArchivePair] = {}
        for index in self.data_dir.iterdir():
            match = _CDF_RE.match(index.name)
            if not match:
                continue
            key = match.group(1) or '0'
            archive = self.data_dir / f'ARCHIVE{key}.AR'
            if archive.is_file():
                pairs[key] = ArchivePair(key, archive, index)
        return pairs

    @property
    def archive_pairs(self) -> dict[str, ArchivePair]:
        return dict(self._pairs)

    @property
    def missing_required_archives(self) -> tuple[str, ...]:
        return tuple(key for key in self.profile.required_archives if key not in self._pairs)

    def entries(self, archive_key: str) -> tuple[CdfEntry, ...]:
        key = str(archive_key)
        if key not in self._pairs:
            raise KeyError(f'archive/index pair {key} is not installed')
        if key not in self._entries:
            self._entries[key] = tuple(read_cdf(self._pairs[key].index))
        return self._entries[key]

    def invalidate_archive(self, archive_key: str) -> None:
        """Discard one parsed index after an editor atomically replaces it."""
        self._entries.pop(str(archive_key), None)

    def find_entry(self, name: str, archive_key: str | None = None) -> tuple[str, CdfEntry]:
        wanted = name.casefold()
        keys = (str(archive_key),) if archive_key is not None else tuple(self._pairs)
        for key in keys:
            for entry in self.entries(key):
                if entry.name.casefold() == wanted:
                    return key, entry
        raise KeyError(f'{name} is not indexed by {self.profile.name}')

    def read_entry(self, name: str, archive_key: str | None = None) -> bytes:
        key, entry = self.find_entry(name, archive_key)
        pair = self._pairs[key]
        with pair.archive.open('rb') as handle:
            handle.seek(entry.archive_offset)
            data = handle.read(entry.size)
        if len(data) != entry.size:
            raise IOError(
                f'short read for {entry.name}: expected {entry.size}, got {len(data)}'
            )
        return data

    def available_body_models(self) -> list[tuple[str, str]]:
        available = []
        for key, name in self.profile.body_models:
            try:
                self.find_entry(name, key)
            except (KeyError, ValueError):
                continue
            available.append((key, name))
        return available
