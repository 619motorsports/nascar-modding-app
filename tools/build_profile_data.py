#!/usr/bin/env python3
"""Build compact runtime maps from mapper CSVs and a verified installation."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nascar_modding.games.installation import GameInstallation
from nascar_modding.games.assets import matches_primary_livery
from nascar_modding.games.profiles import get_profile
from nascar_modding.games.driver_names import VERIFIED_DRIVER_NAMES
from nascar_modding.core.files import atomic_write_json


_REF_RE = re.compile(r'^([A-Za-z0-9_]+)\((-?\d+)(?:,\s*([^,()]*))?')


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def _ref(value: str) -> tuple[int | None, str]:
    match = _REF_RE.match(str(value or ''))
    if not match:
        return None, ''
    return int(match.group(2)), (match.group(3) or '').strip()


def _last_argument(value: str) -> str:
    text = str(value or '').rstrip()
    if not text.endswith(')'):
        return ''
    depth = 0
    for index in range(len(text) - 2, -1, -1):
        char = text[index]
        if char == ')':
            depth += 1
        elif char == '(':
            if depth == 0:
                return text[index + 1:-1].strip()
            depth -= 1
        elif char == ',' and depth == 0:
            return text[index + 1:-1].strip()
    return ''


def _normal(value: str) -> str:
    return re.sub(r'[^A-Z0-9]+', '', str(value).upper())


def build_driver_links(installation: GameInstallation, data_dir: Path) -> list[dict]:
    configs = _rows(data_dir / 'DRIVERCONFIG_c.csv')
    drivers = {int(row['uid']): row for row in _rows(data_dir / 'DRIVER_c.csv')}
    liveries = {int(row['uid']): row for row in _rows(data_dir / 'LIVERIE_c.csv')}
    indexed_liveries = [
        entry.name
        for key in installation.archive_pairs
        for entry in installation.entries(key)
        if entry.name.upper().startswith('LIVERY_')
    ]

    grouped: dict[int, list[dict[str, str]]] = {}
    for row in configs:
        if row.get('Selectable') != 'True' or row.get('BaseArcName', '').upper().startswith('CUSTOM'):
            continue
        series_uid, _series_token = _ref(row.get('Series', ''))
        if installation.profile.series_uid is not None and series_uid != installation.profile.series_uid:
            continue
        driver_uid, _token = _ref(row.get('DRIVER', ''))
        if driver_uid is not None:
            grouped.setdefault(driver_uid, []).append(row)

    base_catalog_path = ROOT / 'data' / 'drivers.json'
    base_catalog = {
        int(row['driver_uid']): row
        for row in _rows_json(base_catalog_path)
    } if base_catalog_path.is_file() and data_dir.resolve() != (ROOT / 'data').resolve() else {}
    result = []
    preferred_prefix = str(installation.profile.content_season) + '_'
    for driver_uid, options in grouped.items():
        options.sort(
            key=lambda row: (
                not row.get('BaseArcName', '').startswith(preferred_prefix),
                row.get('Livery') in ('', 'None'),
                int(row['uid']),
            )
        )
        row = options[0]
        driver_row = drivers.get(driver_uid, {})
        _unused, driver_token = _ref(row.get('DRIVER', ''))
        team_uid, team_token = _ref(row.get('TEAM', ''))
        profile_uid, _profile_token = _ref(row.get('AIDriverProfile', ''))
        livery_uid, _livery_token = _ref(row.get('Livery', ''))
        script_name = liveries.get(livery_uid or -1, {}).get('ScriptName', '')
        script_key = _normal(script_name)
        base_key = _normal(row.get('BaseArcName', '').split('_', 1)[-1])
        slot_candidates = [
            name for name in indexed_liveries
            if script_key and script_key in _normal(name)
        ]
        if not slot_candidates:
            slot_candidates = [
                name for name in indexed_liveries
                if base_key and base_key in _normal(name)
            ]
        exact_script_name = f'LIVERY_{script_name}.ARC'.upper()
        slot_candidates.sort(key=lambda name: (
            name.upper() != exact_script_name,
            not matches_primary_livery(installation.profile, name),
            len(name),
            name,
        ))
        slot = slot_candidates[0] if slot_candidates else ''
        number_asset = _last_argument(row.get('NUMBER', ''))
        number = number_asset.split('_', 1)[0] if '_' in number_asset else ''
        output = {
                'base': row.get('BaseArcName', ''),
                'slot': slot,
                'number': number,
                'profile_id': profile_uid,
                'handle': driver_row.get('Twitter', ''),
                'driver_uid': driver_uid,
                'driver_token': driver_token,
                'driver_config_uid': int(row['uid']),
                'config_uid': int(row['uid']),
                'team_uid': team_uid,
                'team_token': team_token,
            }
        known = base_catalog.get(driver_uid, {})
        display_name = known.get('display_name') or VERIFIED_DRIVER_NAMES.get(driver_uid)
        if display_name:
            output['display_name'] = display_name
            output['name_candidates'] = list(known.get('name_candidates') or [display_name])
        result.append(output)
    return sorted(result, key=lambda row: (int(row['number']) if row['number'].isdigit() else 9999, row['base']))


def build_teams(data_dir: Path) -> list[dict]:
    result = []
    for row in _rows(data_dir / 'RACETEAM_c.csv'):
        manufacturer_uid, manufacturer_token = _ref(row.get('MANUFACTURER', ''))
        result.append(
            {
                'uid': int(row['uid']),
                'token': row.get('NAME', ''),
                'manufacturer_uid': manufacturer_uid,
                'manufacturer_token': manufacturer_token,
                'twitter': row.get('Twitter', ''),
            }
        )
    return result


def build_ai_profiles(installation: GameInstallation, data_dir: Path) -> list[dict]:
    records = _rows(data_dir / 'AIRACEDRIVERPROFILE_c.csv')
    pyc = installation.read_entry('DB_AICONFIG_SCRIPT.PYC', '0')
    fields = (
        ('profile_id', 'UID'),
        ('skill', 'Skill'),
        ('aggression', 'Aggression'),
        ('skill_intermediate', 'SkillIntermediate'),
        ('skill_plate', 'SkillPlate'),
        ('skill_road_course', 'SkillRoadCourse'),
        ('skill_short', 'SkillShort'),
        ('skill_superspeedway', 'SkillSuperSpeedway'),
    )
    result = []
    # Mapper call/load offsets are relative to root co_code. These games share
    # the Python 2.5 PYC header/root layout whose bytecode begins at file +30.
    code_base = 30
    for record in records:
        call_offset = int(record['call_offset'], 16)
        first_load = call_offset - len(fields) * 3
        row = {'call_offset_hex': record['call_offset']}
        for index, (output_name, source_name) in enumerate(fields):
            load_offset = first_load + index * 3
            if pyc[code_base + load_offset] != 0x64:
                raise ValueError(
                    f"profile {record['uid']} {output_name} is not loaded by LOAD_CONST"
                )
            const_index = struct.unpack_from('<H', pyc, code_base + load_offset + 1)[0]
            value = record[source_name]
            row[output_name] = int(value) if output_name == 'profile_id' else float(value)
            row[f'{output_name}_const_index'] = const_index
            row[f'{output_name}_load_offset_hex'] = f'0x{load_offset:X}'
        result.append(row)
    return result


def _write_json(path: Path, value) -> None:
    atomic_write_json(path, value, indent=1)


def _rows_json(path: Path) -> list[dict]:
    import json
    value = json.loads(path.read_text(encoding='utf-8'))
    return value if isinstance(value, list) else []


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('game_id', choices=('nascar13', 'nascar14', 'nascar15'))
    parser.add_argument('install_root', type=Path)
    parser.add_argument('--data-dir', type=Path, default=None)
    args = parser.parse_args()
    profile = get_profile(args.game_id)
    data_dir = args.data_dir or ROOT / 'data' / profile.data_subdir
    installation = GameInstallation(profile, args.install_root)
    drivers = build_driver_links(installation, data_dir)
    teams = build_teams(data_dir)
    ai_profiles = build_ai_profiles(installation, data_dir)
    _write_json(data_dir / 'drivers.json', drivers)
    _write_json(data_dir / 'teams.json', teams)
    _write_csv(data_dir / f'ai_profiles_{args.game_id}.csv', ai_profiles)
    print(f'wrote {len(drivers)} driver links, {len(teams)} teams, {len(ai_profiles)} AI profiles')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
