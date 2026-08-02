"""Shared owner for NASCAR 15 managed paint state and AI race assignments."""

from __future__ import annotations

import io
import json
from pathlib import Path
import os
import re
import shutil
import zipfile
import time

from PIL import Image
import containers

from nascar_modding.editing.archive import backup_path
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.transactions import ManagedPaintCheckpoint, ManagedPaintTransaction
from nascar_modding.editing.transactions import AppendRepointTransaction
from nascar_modding.core.processes import assert_process_closed
from nascar_modding.core.modules import load_module
from nascar_modding.core.files import atomic_write_json
from nascar_modding.editing.livery_wrappers import (
    HD_ENTRY_SIZE, SD_ENTRY_SIZE, NativeLiveryWrapperEditor,
)
from nascar_modding.editing.teams import TeamEditor
from nascar_modding.editing.textures import prepare_image
from nascar_modding.games.installation import GameInstallation


class ManagedPaintEditor:
    UID_CEILING = 25600
    UID_FLOOR = 25560
    UID_STATE_NAME = 'extra_uid_candidates_v1.json'
    _module = None
    _fixed_module = None
    _thumbnail_module = None
    _shipped_uid_pool = None

    def __init__(self, installation: GameInstallation, state_path: str | Path):
        if installation.profile.id != 'nascar15':
            raise ValueError('managed extra paint schemes currently apply only to NASCAR 15')
        self.installation = installation
        self.state_path = Path(state_path)
        self.image_dir = self.state_path.parent / 'schemes' / 'extra'
        self.rollback_dir = self.state_path.parent / 'extra_scheme_rollback_v1'

    def _transaction(self) -> ManagedPaintTransaction:
        return ManagedPaintTransaction(self.installation, self.state_path, self.image_dir)

    def _checkpoint(self) -> ManagedPaintCheckpoint:
        return ManagedPaintCheckpoint(self._transaction(), self.rollback_dir)

    @property
    def uid_state_path(self) -> Path:
        return self.state_path.parent / self.UID_STATE_NAME

    @classmethod
    def _backend(cls):
        if cls._module is not None:
            return cls._module
        path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_extra_scheme_manager_v1.py'
        module = load_module(
            path, 'nascar_modding_shared_managed_paints', add_parent=True,
            load_message=f'could not load managed-paint backend: {path}',
        )
        cls._module = module
        return module

    @classmethod
    def apply_uid_pool(cls, module, uid_state_path: str | Path):
        """Apply app-recorded, game-tested UIDs to a loaded backend module."""
        if module is cls._module:
            if cls._shipped_uid_pool is None:
                cls._shipped_uid_pool = tuple(
                    int(value) for value in getattr(module, 'VERIFIED_SAFE_EXTRA_UIDS', ())
                )
            shipped = cls._shipped_uid_pool
        else:
            original = getattr(module, '_APP_SHIPPED_SAFE_EXTRA_UIDS', None)
            if original is None:
                original = tuple(int(value) for value in getattr(module, 'VERIFIED_SAFE_EXTRA_UIDS', ()))
                module._APP_SHIPPED_SAFE_EXTRA_UIDS = original
            shipped = tuple(original)
        blocked = {int(value) for value in getattr(module, 'VERIFIED_BLOCKED_EXTRA_UIDS', ())}
        store = cls._load_uid_store(uid_state_path)
        extra = {
            int(value) for value in store['verified']
            if cls.UID_FLOOR <= int(value) < cls.UID_CEILING
            and int(value) not in shipped and int(value) not in blocked
        }
        module.VERIFIED_SAFE_EXTRA_UIDS = shipped + tuple(sorted(extra))
        return module

    def _paint_backend(self):
        return self.apply_uid_pool(self._backend(), self.uid_state_path)

    @staticmethod
    def _load_uid_store(path: str | Path) -> dict:
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('UID state is not an object')
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            value = {}
        def integer_list(name: str) -> list[int]:
            source = value.get(name) if isinstance(value.get(name), list) else []
            result = []
            for item in source:
                try:
                    parsed = int(item)
                except (TypeError, ValueError):
                    continue
                if parsed not in result:
                    result.append(parsed)
            return result

        verified = integer_list('verified')
        rejected = integer_list('rejected')
        notes = value.get('notes') if isinstance(value.get('notes'), dict) else {}
        return {'verified': verified, 'rejected': rejected, 'notes': notes}

    def uid_pool(self, *, include_live: bool = True) -> dict:
        backend = self._paint_backend()
        shipped = list(self._shipped_uid_pool or ())
        blocked = [int(value) for value in getattr(backend, 'VERIFIED_BLOCKED_EXTRA_UIDS', ())]
        store = self._load_uid_store(self.uid_state_path)
        user_ok = [
            int(value) for value in store['verified']
            if self.UID_FLOOR <= int(value) < self.UID_CEILING
            and int(value) not in shipped and int(value) not in blocked
        ]
        user_no = [int(value) for value in store['rejected'] if int(value) not in shipped]
        known = set(shipped) | set(blocked) | set(user_ok) | set(user_no)
        untested = [uid for uid in range(self.UID_FLOOR, self.UID_CEILING) if uid not in known]
        usable = sorted(set(shipped) | set(user_ok))
        result = {
            'ok': True, 'shipped_safe': sorted(shipped), 'verified_broken': sorted(blocked),
            'user_verified': sorted(user_ok), 'user_rejected': sorted(user_no),
            'untested': untested, 'notes': store['notes'], 'usable': usable,
            'ceiling': self.UID_CEILING, 'capacity': len(usable),
            'next_candidate': untested[0] if untested else None,
            'remaining_now': None, 'in_use_now': None,
            'note': ('Only the game can decide whether a UID works. Create one scheme on a '
                     'candidate, launch the game, and confirm Paint Select lists it before '
                     'recording the result. UIDs at 25600 or above are never usable.'),
        }
        if include_live:
            catalog = backend.catalog(self.installation.root, self.state_path)
            remaining = [int(value) for value in catalog.get('verified_safe_uid_remaining') or []]
            result['remaining_now'] = len(remaining)
            result['in_use_now'] = len(usable) - len(remaining)
            result['created_limit_per_driver'] = int(catalog.get('created_limit_per_driver') or 0)
        return result

    def record_uid_verdict(self, uid: int, verdict: str, note: str = '') -> dict:
        uid = int(uid)
        verdict = str(verdict or '').strip().lower()
        if verdict not in {'works', 'not_visible', 'broken', 'untested'}:
            raise ValueError('verdict must be works, not_visible, broken or untested')
        if not self.UID_FLOOR <= uid < self.UID_CEILING:
            raise ValueError(
                f'UID {uid} is outside the testable range {self.UID_FLOOR}-{self.UID_CEILING - 1}; '
                f'Paint Select cannot see {self.UID_CEILING}+ at all'
            )
        backend = self._backend()
        shipped = set(self._shipped_uid_pool or getattr(backend, 'VERIFIED_SAFE_EXTRA_UIDS', ()))
        if uid in {int(value) for value in getattr(backend, 'VERIFIED_BLOCKED_EXTRA_UIDS', ())}:
            raise ValueError(f'UID {uid} is recorded as verified-broken and cannot be promoted')
        if uid in {int(value) for value in shipped}:
            raise ValueError(f'UID {uid} already ships as verified-safe')
        store = self._load_uid_store(self.uid_state_path)
        store['verified'] = [int(value) for value in store['verified'] if int(value) != uid]
        store['rejected'] = [int(value) for value in store['rejected'] if int(value) != uid]
        if verdict == 'works':
            store['verified'].append(uid)
        elif verdict in {'not_visible', 'broken'}:
            store['rejected'].append(uid)
        cleaned_note = str(note or '').strip()[:400]
        if cleaned_note:
            store['notes'][str(uid)] = cleaned_note
        else:
            store['notes'].pop(str(uid), None)
        atomic_write_json(self.uid_state_path, store, indent=2)
        state = self.uid_pool(include_live=False)
        return {'ok': True, 'uid': uid, 'verdict': verdict, 'capacity': state['capacity'],
                'usable': state['usable'],
                'note': f'Recorded. Usable UID pool is now {state["capacity"]}.'}

    @classmethod
    def _fixed_backend(cls):
        if cls._fixed_module is not None:
            return cls._fixed_module
        path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_fixed_template_stock_paint_rc10.py'
        module = load_module(
            path, 'nascar_modding_shared_fixed_paint', add_parent=True,
            load_message=f'could not load fixed-template paint backend: {path}',
        )
        cls._fixed_module = module
        return module

    @classmethod
    def _thumbnail_backend(cls):
        if cls._thumbnail_module is not None:
            return cls._thumbnail_module
        path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_thumbnail_native_v25.py'
        module = load_module(
            path, 'nascar_modding_shared_thumbnail', add_parent=True,
            load_message=f'could not load thumbnail backend: {path}',
        )
        cls._thumbnail_module = module
        return module

    @staticmethod
    def _prepare_paint(source: str | Path, quality='auto') -> tuple[Image.Image, dict]:
        image = Image.open(source)
        source_format, source_mode = (image.format or 'unknown').upper(), image.mode
        image.load()
        width, height = image.size
        target = (2048, 1024)
        requested = str(quality or 'auto').strip().lower()
        if requested not in {'auto', 'direct', '1', '2', '4'}:
            requested = 'auto'
        factor = (int(requested) if requested in {'2', '4'} else
                  1 if requested in {'direct', '1'} or width > 2048 or height > 1024 else 2)
        lanczos = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
        rgb = image.convert('RGB')
        intermediate = None
        if factor > 1:
            intermediate = (target[0] * factor, target[1] * factor)
            rgb = rgb.resize(intermediate, lanczos).resize(target, lanczos)
        elif rgb.size != target:
            rgb = rgb.resize(target, lanczos)
        return rgb, {
            'source': [width, height], 'target': list(target),
            'source_format': source_format, 'source_mode': source_mode,
            'quality_requested': requested, 'supersample_factor': factor,
            'intermediate': list(intermediate) if intermediate else None,
            'aspect_warning': abs(width / max(1, height) - 2.0) > 0.002,
        }

    @staticmethod
    def _prepare_thumbnail(source: str | Path, quality='auto') -> tuple[Image.Image, dict]:
        image = Image.open(source)
        image.load()
        original = image.size
        requested = str(quality or 'auto').strip().lower()
        factor = int(requested) if requested in {'2', '4'} else 1 if requested in {'direct', '1'} else (1 if max(original) > 256 else 2)
        target = (256, 256)
        if factor > 1:
            stage = prepare_image(image, (target[0] * factor, target[1] * factor), 'fit')
            result = stage.resize(target, Image.Resampling.LANCZOS)
        else:
            result = prepare_image(image, target, 'fit')
        return result.convert('RGBA'), {
            'source': list(original), 'target': list(target),
            'quality_requested': requested, 'supersample_factor': factor,
            'preserve_alpha': True,
        }

    def catalog(self) -> dict:
        reconciliation = self.reconcile_live_state()
        result = self._paint_backend().catalog(self.installation.root, self.state_path)
        result['live_reconciliation'] = reconciliation
        result['assignments'] = self.assignments()
        result['events'] = self.events()
        return result

    def state(self) -> dict:
        """Return normalized app-owned managed-paint state."""
        return self._paint_backend().load_state(self.state_path)

    def _recover_live_paint(self, item: dict) -> str:
        uid = int(item['uid'])
        script = str(item.get('script_name') or 'RECOVERED')
        name = f'{uid}__{script}.png'
        output = self.image_dir / name
        wrapper = self.installation.read_entry(str(item['sd_entry']), '2')
        byte_count = (2048 // 4) * (1024 // 4) * 8
        payload = wrapper[RAW_OFFSET:RAW_OFFSET + byte_count]
        if len(payload) != byte_count:
            raise ValueError(f"live SD paint wrapper is too short: {item['sd_entry']}")
        image = Image.fromarray(containers._dxt1_decode(payload, 2048, 1024)).convert('RGB')
        self.image_dir.mkdir(parents=True, exist_ok=True)
        image.save(output, 'PNG')
        return name

    def _recover_live_thumbnail(self, item: dict, team_uid: int) -> bool:
        uid = int(item['uid'])
        target = f'2DRIVERSELECTTD_{int(team_uid)}.ARC'
        backend = self._thumbnail_backend()
        name = f"{uid}__{item.get('script_name') or 'RECOVERED'}.thumbnail.png"
        output = self.image_dir / name
        existing_name = Path(str(item.get('thumbnail_source_png') or '')).name
        needs_image = not existing_name or not (self.image_dir / existing_name).is_file()
        needs_check = not bool(item.get('thumbnail_live_checked'))
        if not needs_image and not needs_check:
            return False
        changed = False
        try:
            hit = backend.find_target(
                self.installation.root, uid, target_container_name=target,
            )
            if not hit:
                raise ValueError(f'PAINTSCHEME_{uid} was not found in {target}')
            raw, legacy_entry = hit[2], hit[3]
            entries, _layout = containers.parse_multi_arc(raw)
            entry = next((row for row in entries if row['name'] == f'PAINTSCHEME_{uid}'), None)
            if entry is None:
                entry = legacy_entry if isinstance(legacy_entry, dict) else None
            if (not entry or str(entry.get('fmt')) != 'DXT5'
                    or int(entry.get('w', 0)) != 256 or int(entry.get('h', 0)) != 256
                    or int(entry.get('payload_size', 0)) < int(entry.get('needed', 0))):
                raise ValueError('live thumbnail is not a complete 256x256 DXT5 resource')
            if needs_image:
                self.image_dir.mkdir(parents=True, exist_ok=True)
                containers.multi_read_png(raw, entry).save(output, 'PNG')
                item['thumbnail_source_png'] = name
                changed = True
            try:
                identity = backend.inspect_thumbnail_identity(
                    self.installation.root, uid, target_container_name=target,
                ) or {}
            except Exception:
                identity = {}
            updates = {
                'thumbnail_live_checked': True, 'thumbnail_live_present': True,
                'thumbnail_game_safe': bool(identity.get('same_bank_valid')),
                'thumbnail_same_bank_identity': bool(
                    identity.get('identity_self_identifying') and identity.get('public_name_resolved')),
                'thumbnail_identity_name': identity.get('identity_name') or identity.get('identity_root_name'),
                'preview_status': 'detected_live_thumbnail', 'preview_container': target,
                'preview_entry': f'PAINTSCHEME_{uid}',
            }
            for key, value in updates.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True
            item.pop('thumbnail_live_error', None)
        except Exception as exc:
            updates = {'thumbnail_live_checked': True, 'thumbnail_live_error': str(exc)}
            for key, value in updates.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True
        return changed

    def reconcile_live_state(self) -> dict:
        """Adopt only structurally proven app-created slots from live game data.

        The game installation is read-only here. Only app-owned JSON and recovered
        convenience PNGs may be updated.
        """
        backend = self._paint_backend()
        state = backend.load_state(self.state_path)
        context = backend.base.load_context(str(self.installation.root))
        live = {}
        for record in backend.base.records_of(context, 'LIVERIE_c'):
            uid = backend.base.pointer_int(context, record.uid)
            if uid is None:
                continue
            try:
                driver_uid = backend.base.field_uid(context, record, 'Driver')
                script = str(backend.base.display(context, record.fields.get('ScriptName')) or '').strip()
            except Exception:
                continue
            live[int(uid)] = {'record': record, 'driver_uid': driver_uid, 'script_name': script}
        live_uids = set(live)
        active, stale, active_uids = [], [], set()
        for item in list(state.get('schemes', [])):
            try:
                uid = int(item.get('uid', -1))
            except (TypeError, ValueError):
                uid = -1
            if uid >= 0 and uid not in live_uids:
                orphan = dict(item)
                orphan.update(orphaned_at=int(time.time()),
                              orphaned_reason='livery UID absent after game-file restore or rollback')
                stale.append(orphan)
            else:
                active.append(item)
                if uid >= 0 and not item.get('superseded_by'):
                    active_uids.add(uid)

        safe_pool = {int(value) for value in getattr(backend, 'VERIFIED_SAFE_EXTRA_UIDS', ())}
        try:
            _primary, by_name = backend._asset_index(self.installation.root)
        except Exception:
            by_name = {}
        team_catalog = TeamEditor(self.installation).catalog()
        team_links = {
            int(row['driver_uid']): row for row in team_catalog.get('drivers', [])
            if row.get('driver_uid') is not None and row.get('team_uid') is not None
        }

        def looks_created(uid: int, script: str) -> bool:
            text = str(script or '').strip().upper()
            return bool(
                uid in safe_pool or (
                    uid < self.UID_CEILING and (
                        re.match(r'^15_[0-9]+[A-Z]?_[A-Z0-9]+_EXTRA(?:_SLOT)?_[0-9]+$', text)
                        or re.match(r'^CUSTOM_[0-9]+_[0-9]+_', text)
                    )
                )
            )

        discovered, rejections, recovered_thumbnails = [], [], []
        now = int(time.time())
        for uid in sorted(value for value in live_uids - active_uids
                          if looks_created(value, live[value].get('script_name'))):
            row, reasons = live[uid], []
            script, driver_uid = row.get('script_name') or '', row.get('driver_uid')
            if not script or driver_uid is None:
                continue
            try:
                pair, sd_size, hd_size, in_archive2 = backend._has_pair(by_name, script)
            except Exception:
                pair = in_archive2 = False
                sd_size = hd_size = None
            links = {}
            for field in ('Driver', 'Package', 'World', 'Season'):
                try:
                    links[field] = backend.base.field_uid(context, row['record'], field)
                except Exception:
                    links[field] = None
            if not pair:
                reasons.append('missing canonical SD/HD asset pair')
            if pair and not in_archive2:
                reasons.append('SD/HD pair is not fully indexed in ARCHIVE2')
            if int(sd_size or 0) != SD_ENTRY_SIZE:
                reasons.append(f'unexpected SD wrapper size {int(sd_size or 0)}')
            if int(hd_size or 0) != HD_ENTRY_SIZE:
                reasons.append(f'unexpected HD wrapper size {int(hd_size or 0)}')
            missing_links = [field for field, value in links.items() if value is None]
            if missing_links:
                reasons.append('missing live database link(s): ' + ', '.join(missing_links))
            if reasons:
                rejections.append({'uid': uid, 'script_name': script, 'reasons': reasons})
                continue
            item = {
                'uid': uid, 'driver_uid': int(driver_uid),
                'donor_uid': int(getattr(backend, 'PROVEN_EXTRA_DONOR_UID', 25580)),
                'donor_script_name': str(getattr(backend, 'PROVEN_EXTRA_DONOR_SCRIPT', '')),
                'script_name': script,
                'name': ('Additional Scheme' if re.search(r'(?:^|_)EXTRA(?:_|$)', script, re.I)
                         else backend._friendly_livery_label(script, '', '')),
                'sd_entry': f'LIVERY_{script}.ARC', 'hd_entry': f'HDLIVERY_{script}.ARC',
                'source_png': '', 'thumbnail_source_png': '', 'created': now,
                'preview_status': 'detected_from_live', 'thumbnail_game_safe': False,
                'thumbnail_live_checked': False, 'native_runtime_layout_version': 1,
                'structure_donor_uid': int(getattr(backend, 'PROVEN_EXTRA_DONOR_UID', 25580)),
                'structure_donor_script_name': str(getattr(backend, 'PROVEN_EXTRA_DONOR_SCRIPT', '')),
                'database_recipe': 'recovered_live_scan', 'discovered_from_live_files': True,
                'discovered_at': now,
            }
            try:
                item['source_png'] = self._recover_live_paint(item)
            except Exception:
                pass
            link = team_links.get(int(driver_uid))
            if link and self._recover_live_thumbnail(item, int(link['team_uid'])):
                recovered_thumbnails.append(uid)
            active.append(item)
            active_uids.add(uid)
            discovered.append(uid)

        thumbnail_changed = False
        for item in active:
            link = team_links.get(int(item.get('driver_uid', -1)))
            if link and self._recover_live_thumbnail(item, int(link['team_uid'])):
                thumbnail_changed = True
                recovered_thumbnails.append(int(item.get('uid', -1)))

        stale_uids = {int(item['uid']) for item in stale if item.get('uid') is not None}
        if stale:
            combined = list(state.get('orphaned_schemes', [])) + stale
            by_uid = {}
            for item in combined:
                try:
                    by_uid[int(item.get('uid', -1))] = item
                except (TypeError, ValueError):
                    continue
            state['orphaned_schemes'] = [by_uid[uid] for uid in sorted(by_uid)]
        removed_assignments = 0
        if stale_uids:
            clean = {}
            for event_key, rows in (state.get('assignments') or {}).items():
                if not isinstance(rows, dict):
                    continue
                kept = {}
                for driver_key, livery_uid in rows.items():
                    try:
                        stale_assignment = int(livery_uid) in stale_uids
                    except (TypeError, ValueError):
                        stale_assignment = False
                    if stale_assignment:
                        removed_assignments += 1
                    else:
                        kept[str(driver_key)] = livery_uid
                if kept:
                    clean[str(event_key)] = kept
            state['assignments'] = clean
            finalizer = state.get('registry_finalizer')
            if isinstance(finalizer, dict):
                try:
                    if int(finalizer.get('newest_uid', -1)) in stale_uids:
                        state.pop('registry_finalizer', None)
                except (TypeError, ValueError):
                    pass
        changed = bool(stale or discovered or thumbnail_changed)
        state['schemes'] = active
        state['last_live_state_reconciliation'] = {
            'at': now, 'removed_uids': sorted(stale_uids),
            'discovered_uids': sorted(discovered),
            'assignment_rows_removed': removed_assignments,
            'source_of_truth': 'live DB direct links + canonical ARCHIVE2 SD/HD pair',
            'candidate_rejections': rejections,
            'recovered_thumbnail_uids': sorted(set(recovered_thumbnails)),
        }
        if changed:
            backend.save_state(self.state_path, state)
        return {
            'changed': changed, 'removed_uids': sorted(stale_uids),
            'discovered_uids': sorted(discovered),
            'assignment_rows_removed': removed_assignments, 'quarantined_count': len(stale),
            'discovered_count': len(discovered),
            'recovered_thumbnail_uids': sorted(set(recovered_thumbnails)),
            'candidate_rejections': rejections,
        }

    def events(self) -> list[dict]:
        rows = ScheduleEditor(self.installation).catalog()
        result, seen = [], set()
        for row in rows:
            key = f"{int(row['event_uid'])}|{str(row['event'])}"
            if key in seen:
                continue
            seen.add(key)
            result.append({'key': key, 'event_uid': int(row['event_uid']),
                           'event': str(row['event']), 'track': str(row.get('track') or '')})
        return result

    def assignments(self) -> dict[str, dict[str, int]]:
        return self._paint_backend().assignments(self.state_path)

    def save_assignments(self, assignments: dict) -> dict:
        saved = self._paint_backend().save_assignments(self.state_path, assignments)
        return {'ok': True, 'assignments': saved, 'event_count': len(saved),
                'assignment_count': sum(len(rows) for rows in saved.values())}

    def set_assignment(self, event_key: str, driver_uid: int, livery_uid: int | None) -> dict:
        known_events = {row['key'] for row in self.events()}
        if event_key not in known_events:
            raise ValueError('the named race is not in the pristine 36-event catalog')
        catalog = self.catalog()
        driver = next((row for row in catalog['drivers'] if int(row['uid']) == int(driver_uid)), None)
        if driver is None:
            raise ValueError('driver is not in the active Cup paint catalog')
        if livery_uid not in (None, 0):
            known = {int(row['uid']) for row in driver['schemes'] if row.get('uid') is not None}
            if int(livery_uid) not in known:
                raise ValueError('selected livery does not belong to that driver')
        assignments = self.assignments()
        rows = assignments.setdefault(str(event_key), {})
        if livery_uid in (None, 0):
            rows.pop(str(int(driver_uid)), None)
        else:
            rows[str(int(driver_uid))] = int(livery_uid)
        if not rows:
            assignments.pop(str(event_key), None)
        return self.save_assignments(assignments)

    def _backup_pair(self) -> tuple[str | None, str | None]:
        pair = self.installation.archive_pairs.get('0')
        if pair is None:
            return None, None
        return backup_path(pair.archive), backup_path(pair.index)

    @staticmethod
    def _assert_game_closed():
        assert_process_closed('NASCAR15.exe')

    def preview_ai(self) -> dict:
        archive_backup, index_backup = self._backup_pair()
        return self._paint_backend().ai_plan(
            self.installation.root, self.state_path,
            backup_archive=archive_backup, backup_cdf=index_backup,
        )

    def ai_status(self) -> dict:
        archive_backup, index_backup = self._backup_pair()
        return self._paint_backend().ai_base_status(
            self.installation.root, self.state_path,
            backup_archive=archive_backup, backup_cdf=index_backup,
        )

    def apply_ai(self) -> dict:
        self._assert_game_closed()
        archive_backup, index_backup = self._backup_pair()
        self._paint_backend().ensure_ai_base(
            self.installation.root, self.state_path,
            backup_archive=archive_backup, backup_cdf=index_backup,
        )
        backup = BackupManager(self.installation).create_missing(('0',))
        if not backup['ok']:
            raise IOError('could not create the archive-0 backup: ' + '; '.join(backup['failed']))
        archive_backup, index_backup = self._backup_pair()
        result = self._paint_backend().apply_ai(
            self.installation.root, self.state_path,
            backup_archive=archive_backup, backup_cdf=index_backup,
        )
        result['verified'] = True
        return result

    def restore_ai(self) -> dict:
        self._assert_game_closed()
        backup = BackupManager(self.installation).create_missing(('0',))
        if not backup['ok']:
            raise IOError('could not verify the archive-0 backup: ' + '; '.join(backup['failed']))
        result = self._paint_backend().restore_ai(self.installation.root, self.state_path)
        result['verified'] = True
        return result

    def database_audit(self) -> dict:
        return self._paint_backend().inspect_managed_database(self.installation.root, self.state_path)

    def preview_audit(self) -> dict:
        return self._paint_backend().preview_audit(self.installation.root, self.state_path)

    def thumbnail_identity(self, uid: int, team_uid: int) -> dict:
        return self._thumbnail_backend().inspect_thumbnail_identity(
            self.installation.root, int(uid),
            target_container_name=f'2DRIVERSELECTTD_{int(team_uid)}.ARC',
        ) or {}

    def _run_repair_transaction(self, archive_keys, operation):
        keys = tuple(map(str, archive_keys))
        if keys:
            backup = BackupManager(self.installation).create_missing(keys)
            if not backup['ok']:
                raise IOError('could not create repair backups: ' + '; '.join(backup['failed']))
        transaction = self._transaction()
        snapshot = transaction.snapshot(keys)
        try:
            result = operation()
        except Exception as error:
            rollback_errors = transaction.restore(snapshot)
            detail = str(error)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            raise RuntimeError(detail) from error
        if isinstance(result, dict):
            result['verified'] = True
        return result

    def repair_missing_previews(self) -> dict:
        self._assert_game_closed()
        return self._run_repair_transaction(
            ('1',), lambda: self._paint_backend().repair_missing_previews(
                self.installation.root, self.state_path),
        )

    def finalize_registry(self) -> dict:
        self._assert_game_closed()
        return self._run_repair_transaction(
            ('0',), lambda: self._paint_backend().finalize_livery_registry(
                self.installation.root, self.state_path),
        )

    def repair_state_from_live(self) -> dict:
        self._assert_game_closed()
        return self._run_repair_transaction(
            (), lambda: self._paint_backend().repair_managed_state_from_live(
                self.installation.root, self.state_path),
        )

    def undo_status(self) -> dict:
        path = self.rollback_dir / 'manifest.json'
        if not path.is_file():
            return {'available': False}
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
            return {
                'available': True,
                'label': manifest.get('label') or 'Last paint change',
                'created': manifest.get('created') or '',
                'mode': manifest.get('mode', 'restore_pre'),
            }
        except Exception as exc:
            return {'available': False, 'blocked_reason': str(exc)}

    def undo(self) -> dict:
        """Restore a normal pre-write checkpoint; deleted-slot redo stays gated."""
        self._assert_game_closed()
        transaction = self._transaction()
        checkpoint = self._checkpoint()
        snapshot, manifest = checkpoint.load()
        if manifest.get('mode', 'restore_pre') == 'redo_post':
            checkpoint.reapply_deleted_slot(snapshot, manifest)
            return {
                'ok': True, 'verified': True, 'undone': manifest.get('label'),
                'created': manifest.get('created'),
                'note': (
                    'The deleted paint slot was restored byte-for-byte, including '
                    'archive tails, indexes, app state, and saved images.'
                ),
            }
        if manifest.get('post_state'):
            checkpoint.verify_post_state(manifest)
        errors = transaction.restore(snapshot)
        if errors:
            raise RuntimeError('paint rollback reported: ' + '; '.join(errors))
        checkpoint.clear()
        return {
            'ok': True, 'verified': True, 'undone': manifest.get('label'),
            'created': manifest.get('created'),
            'note': (
                'The previous paint transaction was restored exactly, including '
                'archive indexes, app state, and saved images.'
            ),
        }

    def remove_latest_created(self, uid: int) -> dict:
        """Remove only the exact latest creation, retaining a byte-for-byte redo."""
        self._assert_game_closed()
        checkpoint = self._checkpoint()
        transaction = self._transaction()
        snapshot, manifest = checkpoint.load()
        operation = manifest.get('operation') or {}
        if not (
            manifest.get('format') == ManagedPaintCheckpoint.FORMAT
            and manifest.get('mode', 'restore_pre') == 'restore_pre'
            and operation.get('type') == 'create'
            and int(operation.get('uid', -1)) == int(uid)
        ):
            raise ValueError(
                'exact removal is available only for the most recently created paint slot'
            )
        checkpoint.verify_post_state(manifest)
        redo_dir = checkpoint.prepare_delete_redo(manifest, int(uid))
        try:
            errors = transaction.restore(snapshot)
            if errors:
                raise RuntimeError('exact slot removal reported: ' + '; '.join(errors))
            checkpoint.clear()
            os.replace(redo_dir, checkpoint.rollback_dir)
        except Exception:
            if redo_dir.is_dir():
                shutil.rmtree(redo_dir, ignore_errors=True)
            raise
        return {
            'ok': True, 'removed_uid': int(uid), 'exact_rollback': True,
            'note': (
                'The latest scheme was removed by restoring its exact pre-create '
                'checkpoint. Undo Last Paint Change can restore it byte-for-byte.'
            ),
        }

    def remove(self, uid: int) -> dict:
        """Remove any managed livery, preferring exact latest-create rollback."""
        self._assert_game_closed()
        uid = int(uid)
        try:
            return self.remove_latest_created(uid)
        except Exception as exact_error:
            exact_detail = str(exact_error)
        backend = self._paint_backend()
        backend.repair_managed_state_from_live(self.installation.root, self.state_path)
        state = backend.load_state(self.state_path)
        item = next((row for row in state.get('schemes', [])
                     if int(row.get('uid', -1)) == uid and not row.get('superseded_by')), None)
        if item is None:
            raise ValueError('that active managed paint was not found in the live game files')
        backups = BackupManager(self.installation).create_missing(('0',))
        if not backups['ok']:
            raise IOError('could not create the archive-0 backup: ' + '; '.join(backups['failed']))
        transaction, checkpoint = self._transaction(), self._checkpoint()
        snapshot = transaction.snapshot(('0',))
        operation = {'type': 'delete_live', 'uid': uid}
        checkpoint.persist(snapshot, f'Delete added paint UID {uid}', operation)
        removed_assignments = 0
        ai_was_applied = bool((state.get('ai') or {}).get('applied'))
        try:
            for event_key, rows in list((state.get('assignments') or {}).items()):
                if not isinstance(rows, dict):
                    continue
                for driver_key, value in list(rows.items()):
                    try:
                        matches = int(value) == uid
                    except (TypeError, ValueError):
                        matches = False
                    if matches:
                        rows.pop(driver_key, None)
                        removed_assignments += 1
                if not rows:
                    state['assignments'].pop(event_key, None)
            item['superseded_by'] = 'removed_by_user'
            item['removed_at'] = int(time.time())
            item['removed_reason'] = 'user requested live-file deletion'
            retired = {int(value) for value in state.get('retired_uids', []) if value is not None}
            retired.add(uid)
            state['retired_uids'] = sorted(retired)
            backend.save_state(self.state_path, state)
            try:
                database = backend.remove_managed_livery_from_live_base(self.installation.root, uid)
                database['delete_method'] = str((database.get('meta') or {}).get('strategy') or 'live_applypatch_inverse')
            except Exception as surgical_error:
                team_editor = TeamEditor(self.installation)
                team_catalog = team_editor.catalog()
                preserved = [
                    {'class_name': 'DRIVERCONFIG_c', 'uid': int(row['config_uid']),
                     'field': 'TEAM', 'target_uid': int(row['team_uid'])}
                    for row in team_catalog['drivers']
                ] + [
                    {'class_name': 'RACETEAM_c', 'uid': int(row['uid']),
                     'field': 'MANUFACTURER', 'target_uid': int(row['manufacturer_uid'])}
                    for row in team_catalog['teams'] if row.get('manufacturer_uid') is not None
                ]
                donor = backend.proven_extra_donor(self.installation.root)
                archive_backup, index_backup = self._backup_pair()
                database = backend.rebuild_managed_database_from_clean_base(
                    self.installation.root, self.state_path,
                    backup_archive=archive_backup, backup_cdf=index_backup,
                    donor_uid=int(donor['uid']),
                )
                team_editor.apply(preserved)
                database['delete_method'] = 'verified_clean_base_rebuild'
                database['live_inverse_unavailable'] = str(surgical_error)
            schedule = None
            if ai_was_applied:
                archive_backup, index_backup = self._backup_pair()
                schedule = backend.apply_ai(
                    self.installation.root, self.state_path,
                    backup_archive=archive_backup, backup_cdf=index_backup,
                )
            live = backend.catalog(self.installation.root, self.state_path)
            if any(int(scheme.get('uid', -1)) == uid for row in live['drivers'] for scheme in row.get('schemes', [])):
                raise IOError('delete read-back failed: the livery UID is still present')
            checkpoint.seal(operation)
        except Exception as error:
            rollback_errors = transaction.restore(snapshot)
            if not rollback_errors:
                checkpoint.clear()
            detail = str(error) + ' | Exact checkpoint unavailable: ' + exact_detail
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            raise RuntimeError(detail) from error
        return {'ok': True, 'removed_uid': uid, 'exact_rollback': False,
                'live_file_delete': True, 'assignments_removed': removed_assignments,
                'installed_schedule_updated': bool(schedule), 'database': database,
                'verified': True}

    def export_library_bytes(self) -> bytes:
        """Package managed metadata and source images without game resources."""
        state = self._paint_backend().load_state(self.state_path)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
            package.writestr('extra_schemes_v1.json', json.dumps(state, indent=2))
            written = set()
            for item in state.get('schemes', []):
                for field in ('source_png', 'thumbnail_source_png'):
                    name = Path(str(item.get(field) or '')).name
                    source = self.image_dir / name
                    if name and name not in written and source.is_file():
                        package.write(source, 'paints/' + name)
                        written.add(name)
            package.writestr(
                'README.txt',
                'NASCAR 15 Modding App managed-paint library and named-race AI '
                'assignments. No copyrighted game archive is included.\n',
            )
        return output.getvalue()

    def create(
        self, driver_uid: int, display_name: str, paint_source: str | Path,
        thumbnail_source: str | Path, *, quality: str = 'auto',
    ) -> dict:
        """Create one proven stock-team paint slot as a single transaction."""
        self._assert_game_closed()
        display_name = str(display_name or '').strip()
        if not display_name:
            raise ValueError('enter a name for the new scheme')
        backend = self._paint_backend()
        try:
            backend.repair_managed_state_from_live(self.installation.root, self.state_path)
        except Exception:
            # An absent state needs no reconciliation; catalog/install still use
            # the live database as their source of truth.
            if self.state_path.exists():
                raise
        catalog = backend.catalog(self.installation.root, self.state_path)
        driver = next((row for row in catalog['drivers'] if int(row['uid']) == int(driver_uid)), None)
        if driver is None:
            raise ValueError('selected driver was not found in the live database')
        links = TeamEditor(self.installation).catalog()
        link = next((row for row in links['drivers'] if int(row.get('driver_uid', -1)) == int(driver_uid)), None)
        if link is None:
            raise ValueError('the driver has no current NASCAR 15 Cup team link')
        team_uid = int(link['team_uid'])
        if team_uid in (2403, 2405, 2406):
            raise ValueError('paint creation remains safety-locked for spare/custom teams')
        state = backend.load_state(self.state_path)
        created_count = sum(
            1 for item in state.get('schemes', [])
            if int(item.get('driver_uid', -1)) == int(driver_uid) and not item.get('superseded_by')
        )
        if created_count >= 8:
            raise ValueError('this driver already has the maximum 8 app-created schemes')
        donor = backend.proven_extra_donor(self.installation.root)
        donor_uid, donor_script = int(donor['uid']), str(donor['script_name'])
        identity = backend.suggest_identity(
            self.installation.root, int(driver_uid), display_name, self.state_path,
            donor_uid=donor_uid,
        )
        uid, script_name = int(identity['uid']), str(identity['script_name'])
        if uid >= 25600:
            raise ValueError('paint creation refused an unenumerated 25600+ livery UID')
        if (len(script_name) > 31 or len(f'LIVERY_{script_name}.ARC') > 42 or
                len(f'HDLIVERY_{script_name}.ARC') > 44):
            raise ValueError('generated runtime livery identity exceeds the measured stock name envelope')
        paint, paint_prep = self._prepare_paint(paint_source, quality)
        thumbnail, thumbnail_prep = self._prepare_thumbnail(thumbnail_source, quality)
        pair = backend.donor_asset_pair(self.installation.root, donor_script)
        if len(pair['sd']) != SD_ENTRY_SIZE or len(pair['hd']) != HD_ENTRY_SIZE:
            raise ValueError('the proven donor does not use the expected native SD/HD wrapper sizes')
        wrapper_editor = NativeLiveryWrapperEditor(self.installation)
        sd_payload, sd_levels, sd_changed = wrapper_editor.patch_sd(pair['sd'], paint)
        hd_payload, hd_levels, hd_changed = wrapper_editor.patch_hd(pair['hd'], paint)
        backup = BackupManager(self.installation).create_missing(('0', '1', '2'))
        if not backup['ok']:
            raise IOError('could not create required archive backups: ' + '; '.join(backup['failed']))
        transaction, checkpoint = self._transaction(), self._checkpoint()
        snapshot = transaction.snapshot(('0', '1', '2'))
        operation = {'type': 'create', 'uid': uid, 'driver_uid': int(driver_uid), 'script_name': script_name}
        checkpoint.persist(snapshot, f'Create paint slot UID {uid} for {display_name}', operation)
        source_name = f'{uid}__{script_name}.png'
        thumb_name = f'{uid}__{script_name}.thumbnail.png'
        self.image_dir.mkdir(parents=True, exist_ok=True)
        source_path, thumb_path = self.image_dir / source_name, self.image_dir / thumb_name
        try:
            result = backend.install_scheme(
                self.installation.root, self.state_path, driver_uid=int(driver_uid),
                donor_uid=donor_uid, new_uid=uid, script_name=script_name,
                display_name=display_name, sd_payload=sd_payload, hd_payload=hd_payload,
                source_png_name=source_name,
            )
            paint.save(source_path, 'PNG')
            thumbnail.save(thumb_path, 'PNG')
            team_driver_uids = sorted(
                int(row['driver_uid']) for row in links['drivers']
                if row.get('driver_uid') is not None and int(row['team_uid']) == team_uid
            )
            after = backend.catalog(self.installation.root, self.state_path)
            wanted_drivers = set(team_driver_uids)
            livery_uids = sorted({
                int(scheme['uid']) for row in after['drivers']
                if int(row['uid']) in wanted_drivers for scheme in row.get('schemes', [])
                if scheme.get('uid') is not None and not scheme.get('superseded')
            })
            preview = self._fixed_backend().install_fixed_template_thumbnail(
                self.installation.root,
                target_container=f'2DRIVERSELECTTD_{team_uid}.ARC',
                team_driver_uids=team_driver_uids, livery_uids=livery_uids,
                new_uid=uid, image_path=thumb_path,
            )
            if str(preview.get('encoder') or '').strip().casefold() != 'texconv dxt5':
                raise ValueError('game-safe thumbnail creation requires the bundled texconv.exe')
            state = backend.load_state(self.state_path)
            item = next((row for row in state.get('schemes', []) if int(row.get('uid', -1)) == uid), None)
            if item is None:
                raise ValueError('new scheme state record was not saved')
            item.update({
                'native_runtime_layout_version': 2, 'native_runtime_created': int(time.time()),
                'structure_donor_uid': donor_uid,
                'creation_pipeline': 'exact_v0.9_applypatch_plus_v0.10_fixed_template',
                'uid_range': 'sub_25600', 'preview_status': 'custom_same_bank',
                'preview_container': preview.get('container'),
                'preview_entry': f'PAINTSCHEME_{uid}',
                'preview_method': preview.get('method', 'fixed_template'),
                'preview_encoder': preview.get('encoder'),
                'preview_readback_verified': bool(preview.get('readback_verified')),
                'preview_game_verified': False, 'thumbnail_source_png': thumb_name,
                'thumbnail_requested': True, 'thumbnail_installed': int(time.time()),
                'thumbnail_game_safe': True,
            })
            backend.save_state(self.state_path, state)
            checkpoint.seal(operation)
        except Exception as error:
            rollback_errors = transaction.restore(snapshot)
            if not rollback_errors:
                checkpoint.clear()
            detail = str(error)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            raise RuntimeError(detail) from error
        return {
            'ok': True, 'scheme': result['scheme'], 'database': result['database'],
            'assets': result['assets'], 'preview': preview,
            'preparation': paint_prep, 'thumbnail_preparation': thumbnail_prep,
            'sd_levels': sd_levels, 'sd_changed_bytes': sd_changed,
            'hd_levels': hd_levels, 'hd_changed_bytes': hd_changed,
            'created_count': created_count + 1,
            'created_remaining': max(0, min(7 - created_count, int(preview.get('remaining_native_paint_slots', 0)))),
            'verified': True,
        }

    def repair_runtime(self) -> dict:
        """Rebuild managed DB records and SD/HD wrappers with exact rollback."""
        self._assert_game_closed()
        backend = self._paint_backend()
        state = backend.load_state(self.state_path)
        active = [row for row in state.get('schemes', []) if not row.get('superseded_by')]
        if not active:
            raise ValueError('there are no app-created schemes to repair')
        team_editor = TeamEditor(self.installation)
        team_catalog = team_editor.catalog()
        driver_links = {
            int(row['driver_uid']): row for row in team_catalog['drivers']
            if row.get('driver_uid') is not None
        }
        protected = [
            int(row['uid']) for row in active
            if int(driver_links.get(int(row.get('driver_uid', -1)), {}).get('team_uid', -1))
            in (2403, 2405, 2406)
        ]
        if protected:
            raise ValueError('runtime repair remains locked for custom-team paint UID(s): ' + ', '.join(map(str, protected)))
        donor = backend.proven_extra_donor(self.installation.root)
        pair = backend.donor_asset_pair(self.installation.root, donor['script_name'])
        archive2 = self.installation.archive_pairs['2'].archive
        index2 = self.installation.archive_pairs['2'].index
        cdf2 = backend.v06.parse_cdf_v6(index2.read_bytes())
        wrapper_editor = NativeLiveryWrapperEditor(self.installation)
        prepared, skipped, regions = [], [], []
        for item in active:
            source_name = Path(str(item.get('source_png') or '')).name
            source = self.image_dir / source_name
            if not source_name or not source.is_file():
                skipped.append({'uid': int(item.get('uid', -1)), 'source_png': source_name or None,
                                'reason': 'saved paint source PNG is missing'})
                continue
            image, _prep = self._prepare_paint(source, 'direct')
            sd, sd_levels, sd_changed = wrapper_editor.patch_sd(pair['sd'], image)
            hd, hd_levels, hd_changed = wrapper_editor.patch_hd(pair['hd'], image)
            rows = []
            for name, payload in ((item['sd_entry'], sd), (item['hd_entry'], hd)):
                _index, record = backend.v06.find_v6_file(cdf2, name)
                if int(record.data_size) != len(payload):
                    raise ValueError(f'{name} size no longer matches its indexed slot')
                offset = int(record.data_offset)
                rows.append((name, offset, payload))
                regions.append({'archive': archive2, 'offset': offset, 'size': len(payload), 'name': name})
            prepared.append((item, rows, sd_levels, hd_levels, sd_changed, hd_changed))
        if not prepared:
            return {'ok': True, 'repaired': 0, 'skipped': skipped, 'schemes': []}
        backups = BackupManager(self.installation).create_missing(('0', '2'))
        if not backups['ok']:
            raise IOError('could not create repair backups: ' + '; '.join(backups['failed']))
        transaction = AppendRepointTransaction(self.installation)
        snapshot = transaction.snapshot(
            ('0', '2'), state_files={'managed_paints': self.state_path},
            inplace_regions=regions,
        )
        current_changes = [
            {'class_name': 'DRIVERCONFIG_c', 'uid': int(row['config_uid']),
             'field': 'TEAM', 'target_uid': int(row['team_uid'])}
            for row in team_catalog['drivers']
        ] + [
            {'class_name': 'RACETEAM_c', 'uid': int(row['uid']),
             'field': 'MANUFACTURER', 'target_uid': int(row['manufacturer_uid'])}
            for row in team_catalog['teams'] if row.get('manufacturer_uid') is not None
        ]
        try:
            with archive2.open('r+b') as handle:
                for _item, rows, *_metadata in prepared:
                    for _name, offset, payload in rows:
                        handle.seek(offset)
                        handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                for _item, rows, *_metadata in prepared:
                    for name, offset, payload in rows:
                        handle.seek(offset)
                        if handle.read(len(payload)) != payload:
                            raise IOError(f'asset read-back mismatch for {name}')
            archive_backup, index_backup = self._backup_pair()
            database = backend.rebuild_managed_database_from_clean_base(
                self.installation.root, self.state_path,
                backup_archive=archive_backup, backup_cdf=index_backup,
                donor_uid=int(donor['uid']),
            )
            team_links = team_editor.apply(current_changes)
            state = backend.load_state(self.state_path)
            repaired_at = int(time.time())
            by_uid = {int(row['uid']): row for row in state.get('schemes', []) if row.get('uid') is not None}
            details = []
            for item, _rows, sd_levels, hd_levels, sd_changed, hd_changed in prepared:
                row = by_uid.get(int(item['uid']))
                if row is not None:
                    row['native_runtime_layout_version'] = 2
                    row['native_runtime_repaired'] = repaired_at
                details.append({'uid': int(item['uid']), 'name': item.get('name'),
                                'sd_levels': sd_levels, 'hd_levels': hd_levels,
                                'sd_changed_bytes': sd_changed, 'hd_changed_bytes': hd_changed})
            backend.save_state(self.state_path, state)
            live = backend.catalog(self.installation.root, self.state_path)
            live_uids = {int(scheme['uid']) for row in live['drivers'] for scheme in row.get('schemes', [])
                         if scheme.get('uid') is not None}
            missing = sorted(int(item['uid']) for item, *_rest in prepared if int(item['uid']) not in live_uids)
            if missing:
                raise IOError('database repair read-back missed UID(s): ' + ', '.join(map(str, missing)))
        except Exception as error:
            rollback_errors = transaction.restore(snapshot)
            detail = str(error)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            raise RuntimeError(detail) from error
        return {'ok': True, 'verified': True, 'repaired': len(prepared),
                'skipped': skipped, 'schemes': details, 'database': database,
                'team_links': team_links}

    def replace_thumbnail(
        self, uid: int, source: str | Path | None = None, *, quality='auto',
    ) -> dict:
        """Replace or identity-repair one managed Paint Select thumbnail."""
        self._assert_game_closed()
        uid = int(uid)
        backend, thumbnail_backend = self._paint_backend(), self._thumbnail_backend()
        state = backend.load_state(self.state_path)
        item = next((row for row in state.get('schemes', [])
                     if int(row.get('uid', -1)) == uid and not row.get('superseded_by')), None)
        if item is None:
            raise ValueError('that managed scheme was not found')
        catalog = backend.catalog(self.installation.root, self.state_path)
        driver = next((row for row in catalog['drivers']
                       if int(row['uid']) == int(item['driver_uid'])), None)
        if driver is None:
            raise ValueError('the scheme driver is no longer in the live catalog')
        team_catalog = TeamEditor(self.installation).catalog()
        link = next((row for row in team_catalog['drivers']
                     if int(row.get('driver_uid', -1)) == int(item['driver_uid'])), None)
        if link is None or int(link['team_uid']) in (2403, 2405, 2406):
            raise ValueError('custom thumbnails remain locked for drivers on custom teams')
        container_name = f"2DRIVERSELECTTD_{int(link['team_uid'])}.ARC"
        existing = thumbnail_backend.find_target(
            self.installation.root, uid, target_container_name=container_name)
        donor = None
        if existing:
            try:
                identity = thumbnail_backend.inspect_thumbnail_identity(
                    self.installation.root, uid, target_container_name=container_name)
            except Exception:
                identity = {}
            if identity.get('same_bank_valid'):
                donor = {'uid': uid, 'self_donor': True}
        if donor is None:
            schemes = sorted(driver.get('schemes', []), key=lambda row: (
                1 if row.get('managed') else 0,
                0 if str(row.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
                int(row.get('uid', 999999)),
            ))
            for scheme in schemes:
                donor_uid = int(scheme.get('uid', -1))
                if donor_uid < 0 or donor_uid == uid:
                    continue
                hit = thumbnail_backend.find_target(
                    self.installation.root, donor_uid,
                    target_container_name=container_name,
                )
                if hit:
                    entry = hit[3]
                    width = int(entry.get('w', 0) if isinstance(entry, dict) else getattr(entry, 'width', 0))
                    height = int(entry.get('h', 0) if isinstance(entry, dict) else getattr(entry, 'height', 0))
                    texture_format = str(entry.get('fmt') if isinstance(entry, dict) else getattr(entry, 'fmt', ''))
                    if (width, height, texture_format) == (256, 256, 'DXT5'):
                        donor = {'uid': donor_uid}
                        break
        if donor is None:
            raise ValueError('no structurally safe native thumbnail donor exists in the current team bank')
        prepared = preparation = None
        if source:
            prepared, preparation = self._prepare_thumbnail(source, quality)
        source_name = f"{uid}__{item.get('script_name', 'SCHEME')}.thumbnail.png"
        source_path = self.image_dir / source_name
        saved_path = None
        if prepared is None:
            candidate_name = Path(str(item.get('thumbnail_source_png') or '')).name
            candidate = self.image_dir / candidate_name
            if candidate_name and candidate.is_file():
                saved_path = candidate
        effective = source_path if prepared is not None else saved_path
        backups = BackupManager(self.installation).create_missing(('1',))
        if not backups['ok']:
            raise IOError('could not create the archive-1 backup: ' + '; '.join(backups['failed']))
        transaction, checkpoint = self._transaction(), self._checkpoint()
        snapshot = transaction.snapshot(('1',), existing)
        if source_path.is_file():
            snapshot['image_overwrites'][source_name] = source_path.read_bytes()
        checkpoint.persist(snapshot, f'Replace/repair thumbnail for paint UID {uid}',
                           {'type': 'thumbnail', 'uid': uid})
        try:
            if prepared is not None:
                self.image_dir.mkdir(parents=True, exist_ok=True)
                prepared.save(source_path, 'PNG')
            report = thumbnail_backend.install_or_replace_thumbnail(
                self.installation.root, uid, int(donor['uid']),
                str(effective) if effective else None,
                target_container_name=container_name,
            )
            if prepared is None and saved_path is None:
                hit = thumbnail_backend.find_target(
                    self.installation.root, uid, target_container_name=container_name)
                if hit:
                    raw = hit[2]
                    entries, _layout = containers.parse_multi_arc(raw)
                    entry = next(row for row in entries if row['name'] == f'PAINTSCHEME_{uid}')
                    image = containers.multi_read_png(raw, entry)
                    self.image_dir.mkdir(parents=True, exist_ok=True)
                    image.save(source_path, 'PNG')
            state = backend.load_state(self.state_path)
            current = next(row for row in state.get('schemes', []) if int(row.get('uid', -1)) == uid)
            current.update({
                'preview_status': 'custom_same_bank', 'preview_container': report.get('container'),
                'preview_entry': f'PAINTSCHEME_{uid}', 'preview_method': report.get('method'),
                'preview_encoder': report.get('encoder'),
                'preview_readback_verified': bool(report.get('readback_verified')),
                'thumbnail_source_png': source_name, 'thumbnail_requested': True,
                'thumbnail_installed': int(time.time()),
                'thumbnail_game_safe': bool(report.get('game_safe_same_bank_custom') or report.get('game_safe_raw_clone')),
            })
            backend.save_state(self.state_path, state)
            checkpoint.seal({'type': 'thumbnail', 'uid': uid})
        except Exception as error:
            rollback_errors = transaction.restore(snapshot)
            if not rollback_errors:
                checkpoint.clear()
            detail = str(error)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            raise RuntimeError(detail) from error
        return {'ok': True, 'uid': uid, 'preview': report,
                'preparation': preparation, 'verified': True}
