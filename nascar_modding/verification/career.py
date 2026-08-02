"""Evidence-based NTG 2013 career-mode readiness checks."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from nascar_modding.formats.gfs import PROFILE_CORE_PREFIX_FIELDS, PROFILE_CORE_SCALAR_FIELDS
from nascar_modding.games.installation import GameInstallation


_CAREER_RESOURCES = (
    'BUILDSETTINGS.PYC',
    'EVENTINIT.PYC',
    'GSCAREERCALENDARHELPER.PYC',
    'GSCAREERFUNCTIONSHELPER.PYC',
    'GSCAREERRACEINTRO.PYC',
    'GSCAREERRACESPAWN.PYC',
    '2CAREERUPGRADES.ARC',
    '2CALENDARREDONE.ARC',
    '2CHASECONTENDERS.ARC',
    '2DRIVERSELECTMENU.ARC',
    '2DRIVERSELECTTITLE.ARC',
    '2SINGLESEASON.ARC',
    'CAREERNUMBERS.ARC',
    'SPRINTNUMS2012.ARC',
)

_FRONTEND_STRINGS = (
    b'TES_DRIVERSELECTTOSINGLESEASON',
    b'TES_SINGLESEASONTODRIVER',
    b'GSSingleSeasonModeInterface_c',
    b'SINGLE_SEASON_MODE',
    b'SprintNums2012.arc',
)


def _frontend_binary_evidence(installation: GameInstallation) -> dict:
    executables = sorted(installation.root.rglob('*.exe'))
    executable = next(
        (path for path in executables if path.name.casefold() == 'ntg2013.exe'),
        executables[0] if executables else None,
    )
    if executable is None:
        return {'executable_present': False, 'all_strings_present': False, 'strings': {}}
    payload = executable.read_bytes()
    strings = {value.decode('ascii'): payload.find(value) >= 0 for value in _FRONTEND_STRINGS}
    return {
        'executable_present': True,
        'executable': executable.name,
        'sha256': hashlib.sha256(payload).hexdigest(),
        'all_strings_present': all(strings.values()),
        'strings': strings,
    }


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def audit_ntg2013_career(
    installation: GameInstallation,
    data_dir: str | Path,
) -> dict:
    """Report what is proven before any experimental retail patch is offered."""
    if installation.profile.id != 'nascar13':
        raise ValueError('career readiness audit currently applies only to NTG 2013')

    indexed = {
        entry.name.casefold()
        for key in installation.archive_pairs
        for entry in installation.entries(key)
    }
    resources = {
        name: name.casefold() in indexed
        for name in _CAREER_RESOURCES
    }

    root = Path(data_dir)
    series_rows = _csv_rows(root / 'RACESERIES_c.csv')
    series = next((row for row in series_rows if row.get('uid') == '18306'), None)
    dormant = next((row for row in series_rows if row.get('uid') == '9670'), None)
    race_rows = _csv_rows(root / 'RACEDATA_c.csv')
    primary_races = [
        row for row in race_rows
        if row.get('RaceSeries', '').startswith('RACESERIES_c(18306,')
    ]
    numbered_events = sorted({
        int(row['NumberInSeries'])
        for row in primary_races
        if row.get('NumberInSeries', '').isdigit()
        and 1 <= int(row['NumberInSeries']) <= 36
    })

    series_ok = bool(
        series
        and series.get('Year') == '2013'
        and series.get('Locked') == 'False'
    )
    dormant_ok = bool(
        dormant
        and dormant.get('Year') == '2012'
        and dormant.get('Locked') == 'True'
    )
    assets_ok = all(resources.values())
    calendar_ok = numbered_events == list(range(1, 37))
    data_layer_intact = series_ok and dormant_ok and calendar_ok

    binary = _frontend_binary_evidence(installation)
    frontend_transition_intact = binary['all_strings_present'] and all(
        resources[name]
        for name in (
            '2DRIVERSELECTMENU.ARC',
            '2DRIVERSELECTTITLE.ARC',
            '2SINGLESEASON.ARC',
        )
    )

    return {
        'status': 'frontend_present_save_validation_required',
        'safe_to_patch': False,
        'asset_layer_intact': assets_ok,
        'data_layer_intact': data_layer_intact,
        'frontend_transition_intact': frontend_transition_intact,
        'frontend_evidence': {
            'menu_label_index': '0x09DF',
            'menu_label': 'Single Season',
            'normal_menu_gate_initial_value': -1,
            'normal_menu_gate_condition': 'value < 0',
            'selection_callback': 'FUN_00adf690',
            'single_season_interface_constructor': 'FUN_00ba2df0',
            'selection_message': 'SINGLE_SEASON_MODE',
            'team_shop_handoff': 'FUN_00add3f0(1)',
            'registered_transitions': {
                '43': 'TES_DRIVERSELECTTOSINGLESEASON',
                '44': 'TES_SINGLESEASONTODRIVER',
            },
            'frontend_boot_state': 30,
            'frontend_boot_handler': '0x009C69EC',
            'frontend_boot_preload': 'SPRINTNUMS2012.ARC',
            'frontend_boot_preload_is_required': True,
            'binary_probe': binary,
        },
        'active_series': {
            'uid': 18306,
            'year': 2013,
            'locked': False,
            'verified': series_ok,
        },
        'dormant_series': {
            'uid': 9670,
            'year': 2012,
            'locked': True,
            'verified': dormant_ok,
        },
        'numbered_calendar_events': len(numbered_events),
        'career_resources': resources,
        'save_transition_evidence': {
            'provider': 'PLAYER',
            'resource_key': '0x15000039',
            'section': 'PROFILEDATA',
            'save_callback': 'FUN_00976FD0',
            'load_callback': 'FUN_009760A0',
            'validation_callback': 'FUN_00976100',
            'profile_object_size': 0x32188,
            'profile_object_count': 2,
            'static_scalar_transfers': len(PROFILE_CORE_SCALAR_FIELDS),
            'contiguous_prefix_transfers': len(PROFILE_CORE_PREFIX_FIELDS),
            'dynamic_serializer_boundary_resolved': False,
            'career_record_semantics_resolved': False,
            'retail_writer_ready': False,
        },
        'required_disposable_validation': (
            'create isolated Steam user/profile', 'start Single Season',
            'complete one race', 'save and exit', 'restart and reload',
            'restore the untouched control profile',
        ),
        'remaining_boundary': (
            'The retail frontend transition and its SprintNums2012 preload are present. '
            'Replacing or removing that archive is not safe. A disposable retail '
            'profile must still prove creation, race completion, save, reload, '
            'and restoration before the app modifies an executable or save.'
        ),
    }
