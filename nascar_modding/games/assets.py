"""Canonical classification of game asset names.

Keep filename knowledge here so the web compatibility layer, native UI, data
builders, and verification tools cannot grow separate game-specific parsers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .profiles import GameProfile, get_profile


@dataclass(frozen=True, slots=True)
class LiverySlot:
    number: str
    label: str
    kind: str
    season: int | None = None
    dormant: bool = False


_STANDARD = re.compile(r'^LIVERY_(14|15)_(\d+[A-Z]?)_(.+?)\.ARC$', re.I)
_NTG2013 = re.compile(
    r'^LIVERY_DLC_LIV_(\d+)_([A-Z0-9_]+?)_2013(?:_(\d+))?\.ARC$', re.I
)
_NTG2012_PREFIXED = re.compile(r'^LIVERY_12_?(.+?)\.ARC$', re.I)
_NTG2012_UNPREFIXED = re.compile(r'^LIVERY_([A-Z][A-Z0-9_]+)\.ARC$', re.I)
_CAREER = re.compile(r'^LIVERY_CAREER_(\w+?)_(\d+)\.ARC$', re.I)
_BONUS = re.compile(r'^LIVERY_(LENOVO\d*)_?(\w+)\.ARC$', re.I)
_DLC = re.compile(r'^LIVERY_DLC_(\d+)_([A-Z]+)_(\d+)\.ARC$', re.I)
_DORMANT_NTG2012 = (
    _NTG2012_PREFIXED,
    re.compile(
        r'^(?:HD)?LIVERY_(?!DLC_|CAREER_|LENOVO|TELEMETRY)[A-Z][A-Z0-9_]+\.ARC$',
        re.I,
    ),
)


def _profile(value: str | GameProfile) -> GameProfile:
    return get_profile(value) if isinstance(value, str) else value


def matches_primary_livery(value: str | GameProfile, name: str) -> bool:
    profile = _profile(value)
    if profile.livery_style == 'ntg2013_dlc':
        return _NTG2013.match(str(name).removeprefix('HD')) is not None
    if profile.livery_style == 'numbered_season':
        return _STANDARD.match(str(name).removeprefix('HD')) is not None and str(name).upper().removeprefix('HD').startswith(f'LIVERY_{profile.season_prefix}_')
    raise ValueError(f'unsupported livery style: {profile.livery_style}')


def matches_dormant_livery(value: str | GameProfile, name: str) -> bool:
    profile = _profile(value)
    return profile.id == 'nascar13' and any(pattern.match(str(name)) for pattern in _DORMANT_NTG2012)


def livery_pattern_strings(value: str | GameProfile) -> dict[str, list[str]]:
    """Expose the canonical patterns for reproducible mapping reports."""
    profile = _profile(value)
    primary = [_NTG2013.pattern] if profile.livery_style == 'ntg2013_dlc' else [_STANDARD.pattern]
    dormant = [pattern.pattern for pattern in _DORMANT_NTG2012] if profile.id == 'nascar13' else []
    return {'primary': primary, 'dormant': dormant}


def classify_livery_slot(
    value: str | GameProfile,
    name: str,
    *,
    include_dormant: bool = False,
) -> LiverySlot | None:
    """Classify one SD livery filename for an editor grid.

    HD names deliberately return ``None``; callers pair them with the matching
    SD slot. Dormant season assets are excluded unless an investigative caller
    opts in explicitly.
    """
    profile = _profile(value)
    upper = str(name).upper()
    if upper.startswith('HDLIVERY_'):
        return None

    if profile.id == 'nascar13':
        match = _NTG2013.match(upper)
        if match:
            alt = match.group(3)
            suffix = f' Alt {alt}' if alt else ''
            return LiverySlot(
                number=match.group(1),
                label=f"{match.group(2).replace('_', ' ').title()}{suffix} (2013)",
                kind='driver' if not alt else 'dlc',
                season=2013,
            )
        if include_dormant:
            legacy = _NTG2012_PREFIXED.match(upper)
            if legacy:
                return LiverySlot('', legacy.group(1).replace('_', ' ').title(), 'legacy', 2012, True)
            legacy = _NTG2012_UNPREFIXED.match(upper)
            if legacy and not legacy.group(1).startswith(('CAREER_', 'DLC_', 'LENOVO', 'TELEMETRY')):
                return LiverySlot('', legacy.group(1).replace('_', ' ').title(), 'legacy', 2012, True)
        return None

    match = _STANDARD.match(upper)
    if match and match.group(1) == profile.season_prefix:
        return LiverySlot(match.group(2), match.group(3).replace('_', ' ').title(), 'driver', profile.content_season)

    match = _CAREER.match(upper)
    if match:
        return LiverySlot('', f'Career {match.group(1).title()} {match.group(2)}', 'career')
    match = _BONUS.match(upper)
    if match:
        return LiverySlot('', f'{match.group(1).title()} {match.group(2).title()}', 'bonus')
    match = _DLC.match(upper)
    if match:
        return LiverySlot(match.group(1), f'{match.group(2).title()} Alt {match.group(3)} (DLC)', 'dlc')
    return None


def livery_asset_words(value: str | GameProfile, name: str) -> str:
    """Return stable display words from any supported livery filename."""
    profile = _profile(value)
    upper = str(name or '').upper()
    if profile.id == 'nascar13':
        match = _NTG2013.match(upper.removeprefix('HD'))
        if match:
            return match.group(2).replace('_', ' ').title()
    slot = classify_livery_slot(profile, upper.removeprefix('HD'), include_dormant=True)
    if slot:
        label = re.sub(r'\s+(?:Alt\s+\d+\s+)?\((?:2013|DLC)\)$', '', slot.label, flags=re.I)
        return label
    stem = re.sub(r'^(?:HD)?LIVERY_', '', upper)
    stem = re.sub(r'\.ARC$', '', stem)
    return stem.replace('_', ' ').title()
