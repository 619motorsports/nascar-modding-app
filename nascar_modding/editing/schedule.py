"""Shared verified 36-race schedule-order editor."""

from __future__ import annotations

import json
from pathlib import Path
import threading

from nascar_modding.core.files import atomic_write_json
from nascar_modding.core.modules import load_module
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.games.installation import GameInstallation


SCHEDULE_ENTRY = 'DB_GAME_LOCAL_SCRIPT.PYC'
EVENT_LAP_PROFILE_KEY = 'schedule_event_lap_profiles_v3'
_HELPER = None
_LINK_HELPER = None
_PROFILE_LOCK = threading.RLock()


def _schedule_helper():
    global _HELPER
    if _HELPER is not None:
        return _HELPER
    path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_schedule_editor_v0_1.py'
    module = load_module(
        path, 'nascar_modding_shared_schedule',
        missing_message=f'schedule mapper is missing: {path}',
        load_message=f'could not load schedule mapper: {path}',
    )
    _HELPER = module
    return module


def _schedule_link_helper():
    global _LINK_HELPER
    if _LINK_HELPER is not None:
        return _LINK_HELPER
    path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_schedule_raceevent_links_v0_1.py'
    module = load_module(
        path, 'nascar_modding_shared_schedule_links',
        missing_message=f'schedule runtime-link mapper is missing: {path}',
        load_message=f'could not load schedule runtime-link mapper: {path}',
    )
    _LINK_HELPER = module
    return module


class ScheduleEditor:
    def __init__(self, installation: GameInstallation, config_path: str | Path | None = None):
        self.installation = installation
        self.resources = ResourceEditor(installation)
        self.config_path = Path(config_path) if config_path else None

    def _tools(self):
        root = Path(__file__).resolve().parents[2] / 'internal_tools'
        helper = _schedule_helper()
        helper.configure(self.installation.profile.content_season)
        mapper, repoint = helper.load_helpers(
            str(root / 'nascar15_pyc_record_mapper_v5_teams.py'),
            str(root / 'nascar15_const_repoint_v0_2.py'),
        )
        return helper, mapper, repoint

    def _source(self):
        matches = []
        for archive_key in self.installation.archive_pairs:
            for entry in self.installation.entries(archive_key):
                if entry.name.casefold() == SCHEDULE_ENTRY.casefold():
                    matches.append((archive_key, entry.name))
        if len(matches) != 1:
            raise ValueError(
                f'safe schedule ordering requires exactly one active {SCHEDULE_ENTRY}; found {len(matches)}'
            )
        archive_key, name = matches[0]
        return archive_key, name, self.installation.read_entry(name, archive_key)

    def rows(self) -> list[dict]:
        _archive, _name, payload = self._source()
        helper, mapper, _repoint = self._tools()
        rows, _records = helper.map_schedule(payload, mapper)
        return [row.public() for row in rows]

    def inspect_payload(self, payload: bytes) -> list[dict]:
        """Map a candidate database payload without touching an installation."""
        helper, mapper, _repoint = self._tools()
        rows, _records = helper.map_schedule(bytes(payload), mapper)
        return [row.public() for row in rows]

    def catalog(self) -> list[dict]:
        archive, name, live = self._source()
        try:
            payload = self.resources.read(name, archive, pristine=True)
        except Exception:
            payload = live
        helper, mapper, _repoint = self._tools()
        rows, _records = helper.map_schedule(payload, mapper)
        return [row.public() for row in rows]

    def _normalize_custom(self, slots: list[dict], current: list, catalog: list[dict]) -> list[dict]:
        if not isinstance(slots, list) or len(slots) != 36:
            raise ValueError('custom schedule must contain exactly 36 slots')
        current_by_order = {int(row.order): row for row in current}
        catalog_by_uid = {int(row['uid']): row for row in catalog}
        normalized = []
        for order, value in enumerate(slots, 1):
            source_uid = int(value.get('source_uid', value.get('uid', 0)))
            source = catalog_by_uid.get(source_uid)
            if source is None:
                raise ValueError(f'slot {order} references unknown source race UID {source_uid}')
            target = current_by_order[order]
            laps = int(value.get('laps', source['laps']))
            if not 1 <= laps <= 999:
                raise ValueError(f'slot {order} lap count must be 1-999')
            event_uid = source.get('event_uid')
            if event_uid is None:
                raise ValueError(f'slot {order} source race has no verified EVENT pointer')
            normalized.append({
                'slot': order, 'target_uid': int(target.uid), 'source_uid': source_uid,
                'event_uid': int(event_uid), 'event_name': str(source['event']), 'laps': laps,
            })
        return normalized

    def preview_custom(self, slots: list[dict]) -> dict:
        archive, name, payload = self._source()
        helper, mapper, repoint = self._tools()
        current, _records = helper.map_schedule(payload, mapper)
        catalog = self.catalog()
        normalized = self._normalize_custom(slots, current, catalog)
        try:
            reference = self.resources.read(name, archive, pristine=True)
        except Exception:
            reference = payload
        rebuilt, visible_changes, visible_diagnostics = helper.apply_custom(
            payload, normalized, mapper, repoint
        )
        linked, runtime_changes, runtime_diagnostics = _schedule_link_helper().patch_links(
            rebuilt, normalized, reference_pyc=reference
        )
        final_rows, _final_records = helper.map_schedule(linked, mapper)
        by_order = {int(row.order): row for row in final_rows}
        runtime_links = _schedule_link_helper().inspect_links(linked)
        for item in normalized:
            row = by_order[item['slot']]
            runtime_uid = runtime_links.get(item['target_uid'], {}).get('event_uid')
            if (
                int(row.uid) != item['target_uid'] or row.event != item['event_name']
                or int(row.event_uid) != item['event_uid'] or int(row.laps) != item['laps']
                or runtime_uid != item['event_uid']
            ):
                raise IOError(f"slot {item['slot']} failed visible/runtime schedule verification")
        plan = self.resources.plan(name, archive, linked)
        return {
            'ok': True, 'dry_run': True, 'archive': archive, 'entry': name,
            'season': self.installation.profile.content_season,
            'slots': normalized, 'changes': visible_changes,
            'change_count': sum(
                1 for change in visible_changes
                if change['old_event_uid'] != change['new_event_uid']
                or change['old_laps'] != change['new_laps']
            ),
            'runtime_changes': runtime_changes,
            'diagnostics': {'visible': visible_diagnostics, 'runtime': runtime_diagnostics},
            'method': plan['method'], '_payload': linked,
        }

    def apply_custom(self, slots: list[dict]) -> dict:
        preview = self.preview_custom(slots)
        payload = preview.pop('_payload')
        original = self.resources.read(preview['entry'], preview['archive'])
        installed = False
        try:
            result = self.resources.replace(preview['entry'], preview['archive'], payload)
            installed = True
            readback = self.resources.read(preview['entry'], preview['archive'])
            if readback != payload:
                raise IOError('custom schedule byte read-back failed')
            helper, mapper, _repoint = self._tools()
            final, _records = helper.map_schedule(readback, mapper)
            by_order = {int(row.order): row for row in final}
            runtime = _schedule_link_helper().inspect_links(readback)
            for item in preview['slots']:
                row = by_order[item['slot']]
                if (
                    row.event != item['event_name'] or int(row.event_uid) != item['event_uid']
                    or int(row.laps) != item['laps']
                    or runtime.get(item['target_uid'], {}).get('event_uid') != item['event_uid']
                ):
                    raise IOError(f"live custom schedule mismatch at slot {item['slot']}")
        except Exception as install_error:
            if installed:
                try:
                    self.resources.replace(preview['entry'], preview['archive'], original)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'custom schedule install failed ({install_error}) and rollback failed: {rollback_error}'
                    ) from install_error
            raise
        preview.update(dry_run=False, verified=True, write=result)
        return preview

    @staticmethod
    def _profile_key(row: dict) -> str:
        return f"{int(row['event_uid'])}:{str(row['event'])}"

    def _config(self) -> dict:
        if not self.config_path or not self.config_path.is_file():
            return {}
        try:
            value = json.loads(self.config_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_profiles(self, profiles: dict[str, int]) -> None:
        if not self.config_path:
            return
        with _PROFILE_LOCK:
            config = self._config()
            config[EVENT_LAP_PROFILE_KEY] = profiles
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.config_path, config, indent=1)

    def event_lap_profiles(self) -> dict:
        catalog = self.catalog()
        live = self.rows()
        stored = self._config().get(EVENT_LAP_PROFILE_KEY, {})
        profiles = {}
        rows = []
        for source in catalog:
            key = self._profile_key(source)
            try:
                laps = int(stored.get(key, source['laps']))
            except (TypeError, ValueError):
                laps = int(source['laps'])
            if not 1 <= laps <= 999:
                laps = int(source['laps'])
            profiles[key] = laps
            occurrences = sum(
                int(row.get('event_uid') or -1) == int(source['event_uid'])
                and str(row.get('event')) == str(source['event'])
                for row in live
            )
            rows.append({
                'profile_key': key, 'event_uid': int(source['event_uid']),
                'event_name': str(source['event']),
                'track': source.get('track') or source['event'],
                'stock_laps': int(source['laps']), 'laps': laps,
                'occurrences': occurrences,
            })
        return {'rows': rows, 'profiles': profiles, 'source': 'pristine schedule catalog'}

    def _slots_with_profile_updates(self, updates: list[dict]) -> tuple[list[dict], dict[str, int]]:
        if not isinstance(updates, list) or not updates:
            raise ValueError('no event lap defaults were supplied')
        profile_info = self.event_lap_profiles()
        known = {row['profile_key']: row for row in profile_info['rows']}
        identities = {
            (int(row['event_uid']), str(row['event_name'])): row['profile_key']
            for row in profile_info['rows']
        }
        profiles = dict(profile_info['profiles'])
        for item in updates:
            key = str(item.get('profile_key') or '')
            if key not in known and item.get('event_uid') is not None:
                key = identities.get(
                    (int(item['event_uid']), str(item.get('event_name') or '')), ''
                )
            if key not in known:
                raise ValueError(f'unknown event lap profile: {key}')
            laps = int(item.get('laps'))
            if not 1 <= laps <= 999:
                raise ValueError(f'{known[key]["event_name"]} laps must be 1-999')
            profiles[key] = laps
        catalog = self.catalog()
        by_identity = {
            (int(row['event_uid']), str(row['event'])): row for row in catalog
        }
        slots = []
        for row in self.rows():
            source = by_identity.get((int(row['event_uid']), str(row['event'])))
            if not source:
                raise ValueError(f"live event {row['event']} is absent from the pristine catalog")
            key = self._profile_key(source)
            slots.append({'source_uid': int(source['uid']), 'laps': profiles[key]})
        return slots, profiles

    def preview_event_laps(self, updates: list[dict]) -> dict:
        slots, profiles = self._slots_with_profile_updates(updates)
        result = self.preview_custom(slots)
        result['event_lap_profiles'] = profiles
        result['matched_occurrences'] = sum(
            row['occurrences'] for row in self.event_lap_profiles()['rows']
            if row['profile_key'] in {str(item.get('profile_key')) for item in updates}
        )
        return result

    def apply_event_laps(self, updates: list[dict]) -> dict:
        slots, profiles = self._slots_with_profile_updates(updates)
        result = self.apply_custom(slots)
        self._save_profiles(profiles)
        result['event_lap_profiles'] = profiles
        return result

    def restore_event_laps(self) -> dict:
        rows = self.event_lap_profiles()['rows']
        return self.apply_event_laps([
            {'profile_key': row['profile_key'], 'laps': row['stock_laps']} for row in rows
        ])

    def preview_order(self, ordered_uids: list[int | str]) -> dict:
        archive_key, name, payload = self._source()
        helper, mapper, repoint = self._tools()
        current, _records = helper.map_schedule(payload, mapper)
        if len(ordered_uids) != 36 or len({int(uid) for uid in ordered_uids}) != 36:
            raise ValueError('schedule order must contain 36 unique race UIDs')
        desired = {int(uid): order for order, uid in enumerate(ordered_uids, 1)}
        rebuilt, changes = helper.apply_order(payload, desired, mapper, repoint)
        if len(rebuilt) != len(payload):
            raise ValueError('schedule reorder changed the PYC size')
        plan = self.resources.plan(name, archive_key, rebuilt)
        return {
            'ok': True, 'dry_run': True, 'archive': archive_key, 'entry': name,
            'season': self.installation.profile.content_season,
            'changes': changes, 'change_count': len(changes),
            'method': plan['method'], '_payload': rebuilt,
        }

    def apply_order(self, ordered_uids: list[int | str]) -> dict:
        preview = self.preview_order(ordered_uids)
        payload = preview.pop('_payload')
        original = self.resources.read(preview['entry'], preview['archive'])
        installed = False
        try:
            result = self.resources.replace(preview['entry'], preview['archive'], payload)
            installed = True
            actual = [int(row['uid']) for row in self.rows()]
            wanted = [int(uid) for uid in ordered_uids]
            if actual != wanted:
                raise IOError('live 36-race schedule read-back did not match the requested order')
        except Exception as install_error:
            if installed:
                try:
                    self.resources.replace(preview['entry'], preview['archive'], original)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'schedule install failed ({install_error}) and rollback failed: {rollback_error}'
                    ) from install_error
            raise
        preview.update(dry_run=False, verified=True, write=result)
        return preview

    def restore(self) -> dict:
        archive_key, name, _payload = self._source()
        return self.resources.restore(name, archive_key)
