"""Shared AI driver-rating editor for all supported games."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import struct

from nascar_modding.editing.archive import ArchiveEntryEditor
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.formats.python2_pyc import (
    rebuild_with_float_constant,
    root_constants,
    root_layout,
)
from nascar_modding.games.installation import GameInstallation


RATING_FIELDS = (
    'skill', 'aggression', 'skill_intermediate', 'skill_plate',
    'skill_road_course', 'skill_short', 'skill_superspeedway',
)
RATING_LABELS = (
    'Overall Skill', 'Aggression', 'Intermediate', 'Plate',
    'Road Course', 'Short Track', 'Superspeedway',
)
PYC_CODE_BASE = 30
EXPERIMENTAL_ABS_MAX = 1_000_000_000.0
PYC_ENTRY = 'DB_AICONFIG_SCRIPT.PYC'


def _display_percent(value: float):
    scaled = float(value) * 100.0
    return int(round(scaled)) if abs(scaled - round(scaled)) < 1e-9 else round(scaled, 6)


class RatingsEditor:
    """Read and transactionally update mapped LOAD_CONST rating operands."""

    def __init__(self, installation: GameInstallation, data_root: str | Path | None = None):
        self.installation = installation
        root = Path(data_root) if data_root else Path(__file__).resolve().parents[2] / 'data'
        subdir = installation.profile.data_subdir
        self.data_dir = root / subdir if subdir else root
        self.archive_editor = ArchiveEntryEditor(installation)
        self.resources = ResourceEditor(installation)

    def _profiles(self) -> list[dict]:
        path = self.data_dir / self.installation.profile.ai_profiles_file
        with path.open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def _links(self) -> list[dict]:
        value = json.loads((self.data_dir / 'drivers.json').read_text(encoding='utf-8'))
        if not isinstance(value, list):
            raise ValueError('drivers.json is not a list')
        return value

    @staticmethod
    def _load_offset(profile: dict, field: str) -> int:
        return PYC_CODE_BASE + int(profile[field + '_load_offset_hex'], 16)

    @staticmethod
    def _constant_values(pyc: bytes) -> dict[int, float]:
        return {
            index: value
            for index, value in enumerate(root_constants(pyc))
            if isinstance(value, float)
        }

    @staticmethod
    def _value_at(pyc: bytes, profile: dict, field: str) -> float:
        offset = RatingsEditor._load_offset(profile, field)
        if offset + 3 > len(pyc) or pyc[offset] != 0x64:
            raise ValueError('unexpected rating bytecode; mapped offsets no longer match')
        constant_index = struct.unpack_from('<H', pyc, offset + 1)[0]
        values = RatingsEditor._constant_values(pyc)
        if constant_index not in values:
            raise ValueError(f'{field} does not reference a float constant')
        return values[constant_index]

    def ratings(self, *, pristine: bool = False) -> list[dict]:
        profiles = {int(row['profile_id']): row for row in self._profiles()}
        pyc = None if pristine else self.resources.read(PYC_ENTRY, '0')
        result = []
        for link in self._links():
            profile_id = int(link.get('profile_id', -1))
            profile = profiles.get(profile_id)
            if profile is None:
                continue
            values = ({
                field: _display_percent(float(profile[field]))
                for field in RATING_FIELDS
            } if pristine else {
                field: _display_percent(self._value_at(pyc, profile, field))
                for field in RATING_FIELDS
            })
            result.append({
                'driver_uid': int(link['driver_uid']),
                'profile_id': profile_id,
                'number': str(link.get('number') or ''),
                'label': str(link.get('display_name') or link.get('base') or profile_id),
                'slot': link.get('slot'),
                'stats': values,
            })
        return sorted(result, key=lambda row: (
            int(row['number']) if row['number'].isdigit() else 9999,
            row['label'].casefold(),
        ))

    @staticmethod
    def _validated_percent(value, experimental: bool) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('rating must be a finite number') from exc
        if not math.isfinite(result):
            raise ValueError('NaN and infinity are blocked')
        if not experimental and not 0.0 <= result <= 100.0:
            raise ValueError('the original rating scale is 0-100')
        if abs(result) > EXPERIMENTAL_ABS_MAX:
            raise ValueError(f'absolute ratings above {EXPERIMENTAL_ABS_MAX:g} are blocked')
        return result

    def set_ratings(self, profile_id: int, changes: dict, experimental: bool = False) -> dict:
        requested = {
            str(field): self._validated_percent(value, experimental)
            for field, value in dict(changes).items()
        }
        invalid = sorted(set(requested) - set(RATING_FIELDS))
        if invalid:
            raise ValueError(f'unsupported rating field(s): {", ".join(invalid)}')
        if not requested:
            raise ValueError('no rating changes were supplied')
        profile = next(
            (row for row in self._profiles() if int(row['profile_id']) == int(profile_id)),
            None,
        )
        if profile is None:
            raise ValueError('driver AI profile was not found')

        original = self.installation.read_entry(PYC_ENTRY, '0')
        rebuilt = original
        applied = {}
        appended = 0
        for field, percent in requested.items():
            target = percent / 100.0
            offset = self._load_offset(profile, field)
            current = self._value_at(rebuilt, profile, field)
            if abs(current - target) <= 1e-12:
                applied[field] = _display_percent(target)
                continue
            constants = self._constant_values(rebuilt)
            existing = next(
                (index for index, value in constants.items() if abs(value - target) <= 1e-12),
                None,
            )
            if existing is not None:
                output = bytearray(rebuilt)
                if offset + 3 > len(output) or output[offset] != 0x64:
                    raise ValueError('rating LOAD_CONST offset is invalid')
                struct.pack_into('<H', output, offset + 1, existing)
                rebuilt = bytes(output)
            else:
                rebuilt, _new_index = rebuild_with_float_constant(rebuilt, offset, target)
                appended += 1
            applied[field] = _display_percent(target)

        if rebuilt == original:
            return {
                'profile_id': int(profile_id),
                'applied': applied,
                'method': 'no_op',
                'verified': True,
            }
        write = self.archive_editor.replace_or_repoint_entry(PYC_ENTRY, rebuilt, '0')
        try:
            live = self.installation.read_entry(PYC_ENTRY, '0')
            for field, percent in requested.items():
                if abs(self._value_at(live, profile, field) - percent / 100.0) > 1e-12:
                    raise IOError(f'{field} failed live rating read-back verification')
        except Exception as verification_error:
            try:
                self.archive_editor.replace_or_repoint_entry(PYC_ENTRY, original, '0')
                if self.installation.read_entry(PYC_ENTRY, '0') != original:
                    raise IOError('previous PYC payload was not restored')
            except Exception as rollback_error:
                raise RuntimeError(
                    f'rating verification failed ({verification_error}) and rollback failed: '
                    f'{rollback_error}'
                ) from verification_error
            raise
        return {
            'profile_id': int(profile_id),
            'applied': applied,
            'method': write['method'],
            'constants_appended': appended,
            'verified': True,
            'write': write,
        }

    def set_rating(self, profile_id: int, field: str, value, experimental: bool = False) -> dict:
        result = self.set_ratings(profile_id, {field: value}, experimental)
        result['applied'] = result['applied'][field]
        archive_method = result['method']
        if archive_method != 'no_op':
            if result.get('constants_appended'):
                result['method'] = 'append_constant_repoint'
                result['repoint'] = True
            else:
                result['method'] = 'existing_constant'
                result['repoint'] = False
        return result

    def restore(self, profile_id: int) -> dict:
        profile = next(
            (row for row in self._profiles() if int(row['profile_id']) == int(profile_id)),
            None,
        )
        if profile is None:
            raise ValueError('driver AI profile was not found')
        return self.set_ratings(
            profile_id,
            {field: float(profile[field]) * 100.0 for field in RATING_FIELDS},
            experimental=True,
        )
