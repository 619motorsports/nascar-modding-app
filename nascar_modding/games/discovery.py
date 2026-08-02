"""Locate supported game installations without relying on Flask config."""

from __future__ import annotations

import os
from pathlib import Path
import re
import string

from .profiles import GameProfile, get_profile


def steam_common_roots() -> list[Path]:
    roots: list[Path] = []
    vdfs = (
        Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'))
        / 'Steam/steamapps/libraryfolders.vdf',
        Path(os.environ.get('PROGRAMFILES', r'C:\Program Files'))
        / 'Steam/steamapps/libraryfolders.vdf',
    )
    for vdf in vdfs:
        try:
            text = vdf.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for match in re.finditer(r'"path"\s*"([^"]+)"', text):
            roots.append(Path(match.group(1).replace('\\\\', '\\')) / 'steamapps/common')

    if os.name == 'nt':
        for letter in string.ascii_uppercase:
            drive = Path(f'{letter}:\\')
            if not drive.exists():
                continue
            roots.extend(
                (
                    drive / 'SteamLibrary/steamapps/common',
                    drive / 'hdd/SteamLibrary/steamapps/common',
                    drive / 'Program Files (x86)/Steam/steamapps/common',
                )
            )
    unique = []
    seen = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def validate_install_root(profile: GameProfile | str, path: str | Path) -> Path | None:
    profile = get_profile(profile) if isinstance(profile, str) else profile
    candidate = Path(path).expanduser()
    candidates = [candidate]
    if candidate.name.casefold() == 'data':
        candidates.append(candidate.parent)
    candidates.extend(candidate / folder for folder in profile.folder_names)
    for root in candidates:
        data = root / 'data'
        if all((data / f'ARCHIVE{key}.AR').is_file() for key in profile.required_archives):
            return root.resolve()
    return None


def discover_install(profile: GameProfile | str) -> Path | None:
    profile = get_profile(profile) if isinstance(profile, str) else profile
    for common in steam_common_roots():
        for folder in profile.folder_names:
            root = validate_install_root(profile, common / folder)
            if root:
                return root
    return None
