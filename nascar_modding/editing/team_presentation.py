"""Transactional NASCAR 15 team presentation and link workflows."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading

from PIL import Image

from nascar_modding.core.files import atomic_write_json
from nascar_modding.core.modules import load_module
from nascar_modding.core.processes import assert_process_closed
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.managed_paints import ManagedPaintEditor
from nascar_modding.editing.teams import TeamEditor
from nascar_modding.editing.textures import prepare_image
from nascar_modding.editing.text_tables import TextTableEditor
from nascar_modding.editing.transactions import TeamAssetCheckpoint, TeamAssetTransaction
from nascar_modding.games.installation import GameInstallation


SUPPORTED_SPARE_TEAM_UIDS = (2403, 2405, 2406)
UNSUPPORTED_TEAM_UIDS = (2404,)
UNSUPPORTED_MANUFACTURER_UIDS = (1074,)
TEAM_DEFAULT_LOGO_DONORS = {2403: 1333, 2405: 1336, 2406: 1335}
TEAM_DISPLAY_NAMES = {
    1325: 'Richard Petty Motorsports', 1326: 'JTG Daugherty Racing',
    1327: 'Front Row Motorsports', 1331: 'Roush Fenway Racing',
    1333: 'Richard Childress Racing', 1335: 'Joe Gibbs Racing',
    1336: 'Team Penske', 1340: 'Hendrick Motorsports',
    1341: 'Wood Brothers Racing', 1347: 'Tommy Baldwin Racing',
    1349: 'Chip Ganassi Racing', 1351: 'Stewart-Haas Racing',
    1352: 'Germain Racing', 1354: 'Michael Waltrip Racing',
    1355: 'Furniture Row Racing', 5762: 'JR Motorsports',
    11444: 'Phil Parsons Racing', 2403: 'Custom Chevrolet',
    2405: 'Custom Ford', 2406: 'Custom Toyota',
    8739: 'Leavine Family Racing', 10518: 'BK Racing',
    23035: 'Premium Motorsports', 25381: 'HScott Motorsports',
    25430: 'Go FAS Racing',
}


class TeamPresentationEditor:
    """Own presentation-bank writes and their corresponding database links."""

    _module = None
    _lock = threading.RLock()

    def __init__(self, installation: GameInstallation, app_data_root: str | Path):
        if installation.profile.id != 'nascar15':
            raise ValueError('team presentation editing currently applies only to NASCAR 15')
        self.installation = installation
        self.app_data_root = Path(app_data_root)
        self.state_path = self.app_data_root / 'team_manager_state.json'
        self.paint_state_path = self.app_data_root / 'extra_schemes_v1.json'
        self.rollback_dir = self.app_data_root / 'team_asset_rollback_v1'
        self.teams = TeamEditor(installation)
        self.paints = ManagedPaintEditor(installation, self.paint_state_path)
        self.transaction = TeamAssetTransaction(
            installation, self.state_path, self.paint_state_path,
        )
        self.checkpoint = TeamAssetCheckpoint(self.transaction, self.rollback_dir)

    @classmethod
    def _backend(cls):
        if cls._module is None:
            path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_team_assets_v1.py'
            cls._module = load_module(
                path, 'nascar_modding_shared_team_presentation', add_parent=True,
                load_message=f'could not load team presentation backend: {path}',
            )
        return cls._module

    @staticmethod
    def _base_state() -> dict:
        return {
            'format': 'nascar15-team-manager-v1', 'version': 1,
            'driver_teams': {}, 'team_manufacturers': {}, 'team_names': {},
            'driver_source_teams': {}, 'team_logo_donors': {},
            'thumbnail_overrides': {}, 'history': [],
        }

    def load_state(self) -> dict:
        state = self._base_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return state
        if not isinstance(raw, dict):
            return state
        for key in ('driver_teams', 'team_manufacturers', 'driver_source_teams', 'team_logo_donors'):
            if isinstance(raw.get(key), dict):
                state[key] = {str(name): int(value) for name, value in raw[key].items()}
        if isinstance(raw.get('team_names'), dict):
            state['team_names'] = {
                str(name): str(value) for name, value in raw['team_names'].items()
                if str(value).strip()
            }
        if isinstance(raw.get('thumbnail_overrides'), dict):
            state['thumbnail_overrides'] = {
                str(name): dict(value) for name, value in raw['thumbnail_overrides'].items()
                if isinstance(value, dict)
            }
        if isinstance(raw.get('history'), list):
            state['history'] = list(raw['history'][-100:])
        return state

    def save_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state, indent=2)

    def catalog(self) -> dict:
        catalog = self.teams.catalog()
        state = self.load_state()
        try:
            paint_catalog = self.paints.catalog()
        except Exception:
            paint_catalog = {'drivers': []}
        paint_by_driver = {
            int(row['uid']): row for row in paint_catalog.get('drivers', [])
            if row.get('uid') is not None
        }
        for driver in catalog.get('drivers', []):
            paint_driver = paint_by_driver.get(int(driver.get('driver_uid', -1)), {})
            driver['schemes'] = list(paint_driver.get('schemes', []))
            driver['created_scheme_uids'] = sorted({
                int(row['uid']) for row in driver['schemes']
                if row.get('uid') is not None and row.get('managed')
                and not row.get('superseded')
            })
        backend = self._backend()
        statuses = backend.team_asset_statuses(
            self.installation.root, [row['uid'] for row in catalog.get('teams', [])],
        )
        for team in catalog.get('teams', []):
            uid = int(team['uid'])
            original = TEAM_DISPLAY_NAMES.get(uid, str(team.get('label') or uid))
            team['original_label'] = original
            team['label'] = state.get('team_names', {}).get(str(uid), original)
            team['presentation'] = statuses.get(int(team['uid']), {})
        labels = {int(row['uid']): row['label'] for row in catalog.get('teams', [])}
        for driver in catalog.get('drivers', []):
            driver['team_label'] = labels.get(int(driver['team_uid']), driver.get('team_label'))
        catalog['presentation_undo'] = self.checkpoint.info()
        return catalog

    def status(self, team_uid: int) -> dict:
        return self._backend().team_asset_status(self.installation.root, int(team_uid))

    def _driver(self, key: int, catalog: dict | None = None) -> dict:
        rows = (catalog or self.catalog()).get('drivers', [])
        matches = [row for row in rows if int(row.get('config_uid', -1)) == int(key)]
        if not matches:
            matches = [row for row in rows if int(row.get('driver_uid', -1)) == int(key)]
        if len(matches) != 1:
            raise ValueError('driver was not found uniquely in the active Cup team catalog')
        return matches[0]

    @staticmethod
    def _native_livery_uids(driver: dict) -> list[int]:
        return sorted({
            int(row['uid']) for row in driver.get('schemes', [])
            if row.get('uid') is not None and not row.get('managed')
            and not row.get('superseded')
        })

    def _source_team_uid(self, driver: dict, state: dict) -> int:
        return int(state.get('driver_source_teams', {}).get(
            str(int(driver['config_uid'])), int(driver['team_uid']),
        ))

    def _begin(self, label: str) -> tuple[dict, dict]:
        assert_process_closed('NASCAR15.exe', 'editing team presentation assets')
        backup = BackupManager(self.installation).create_missing(('0', '1'))
        if not backup['ok']:
            raise IOError('could not create team-asset backups: ' + '; '.join(backup['failed']))
        snapshot = self.transaction.snapshot(('0', '1'))
        manifest = self.checkpoint.persist(snapshot, label)
        return snapshot, manifest

    def _rollback(self, snapshot: dict, error: Exception):
        errors = self.transaction.restore(snapshot)
        if not errors:
            self.checkpoint.clear()
        detail = str(error)
        if errors:
            detail += ' | Rollback warnings: ' + '; '.join(errors)
        raise RuntimeError(detail) from error

    def _ensure_driver(self, driver: dict, destination_uid: int, source_uid: int) -> dict:
        result = self._backend().ensure_driver_assets(
            self.installation.root, int(destination_uid), int(source_uid),
            int(driver['driver_uid']), self._native_livery_uids(driver),
        )
        result['transfer_strategy'] = 'public_v1_direct_revision'
        return result

    def prepare_team(self, team_uid: int) -> dict:
        team_uid = int(team_uid)
        if team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('that incomplete reserve team is not supported')
        with self._lock:
            catalog, state = self.catalog(), self.load_state()
            team = next((row for row in catalog['teams'] if int(row['uid']) == team_uid), None)
            if team is None:
                raise ValueError('team was not found')
            snapshot, _manifest = self._begin(f"Build or repair presentation assets for {team['label']}")
            try:
                reports = []
                for driver in catalog['drivers']:
                    if int(driver['team_uid']) != team_uid:
                        continue
                    source_uid = self._source_team_uid(driver, state)
                    reports.append(self._ensure_driver(driver, team_uid, source_uid))
                    state['driver_source_teams'].setdefault(str(int(driver['config_uid'])), source_uid)
                donor = int(state['team_logo_donors'].get(
                    str(team_uid), TEAM_DEFAULT_LOGO_DONORS.get(
                        team_uid, reports[0].get('source_team_uid', team_uid) if reports else team_uid,
                    ),
                ))
                logo = self._backend().ensure_team_logo(
                    self.installation.root, team_uid, donor,
                )
                state['team_logo_donors'][str(team_uid)] = donor
                self.save_state(state)
                verified = self.status(team_uid)
                if not verified.get('presentation_ready'):
                    raise IOError('team presentation semantic read-back failed')
            except Exception as error:
                self._rollback(snapshot, error)
            return {'ok': True, 'verified': True, 'team_uid': team_uid,
                    'drivers': reports, 'logo': logo, 'status': verified,
                    'rollback_available': True}

    def move_driver(self, config_uid: int, target_team_uid: int, *, dry_run=False) -> dict:
        config_uid, target_team_uid = int(config_uid), int(target_team_uid)
        if target_team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('that incomplete reserve team is not supported')
        with self._lock:
            catalog = self.catalog()
            driver = self._driver(config_uid, catalog)
            target = next((row for row in catalog['teams'] if int(row['uid']) == target_team_uid), None)
            if target is None:
                raise ValueError('destination team was not found')
            old_uid = int(driver['team_uid'])
            preview = {
                'kind': 'driver_team', 'config_uid': config_uid,
                'driver': driver.get('label'), 'old_team_uid': old_uid,
                'old_team': driver.get('team_label'), 'new_team_uid': target_team_uid,
                'new_team': target.get('label'),
            }
            created = list(driver.get('created_scheme_uids') or [])
            if target_team_uid != old_uid and created:
                preview.update(allowed=False, app_created_scheme_uids=created,
                               blocked_reason='Move managed paint slots back to a stock-team-safe state first.')
            else:
                preview['allowed'] = True
            if dry_run:
                return {'ok': True, 'dry_run': True, 'preview': preview}
            if not preview['allowed']:
                raise ValueError(preview['blocked_reason'])
            if target_team_uid == old_uid:
                return {'ok': True, 'verified': True, 'changed': False, 'preview': preview}
            state = self.load_state()
            source_uid = self._source_team_uid(driver, state)
            label = f"Move {driver.get('label', config_uid)} to {target.get('label', target_team_uid)}"
            snapshot, manifest = self._begin(label)
            try:
                assets = self._ensure_driver(driver, target_team_uid, source_uid)
                logo = None
                if not self.status(target_team_uid).get('logo_ready'):
                    logo = self._backend().ensure_team_logo(
                        self.installation.root, target_team_uid, source_uid,
                    )
                link = self.teams.apply([{
                    'kind': 'driver_team', 'config_uid': config_uid,
                    'team_uid': target_team_uid,
                }])
                state['driver_teams'][str(config_uid)] = target_team_uid
                state['driver_source_teams'].setdefault(str(config_uid), source_uid)
                state['history'].append({
                    'kind': 'driver_team', 'uid': config_uid, 'old_uid': old_uid,
                    'new_uid': target_team_uid, 'label': label,
                    'rollback_created': manifest.get('created'),
                    'rollback_created_epoch': manifest.get('created_epoch'),
                    'rollback_label': manifest.get('label'),
                })
                state['history'] = state['history'][-100:]
                self.save_state(state)
                readback = self._driver(config_uid, self.catalog())
                if int(readback['team_uid']) != target_team_uid:
                    raise IOError('driver-team semantic read-back failed')
            except Exception as error:
                self._rollback(snapshot, error)
            return {'ok': True, 'verified': True, 'changed': True,
                    'preview': preview, 'assets': assets, 'logo': logo,
                    'link': link, 'rollback_available': True}

    def set_manufacturer(self, team_uid: int, manufacturer_uid: int, *, dry_run=False) -> dict:
        team_uid, manufacturer_uid = int(team_uid), int(manufacturer_uid)
        if team_uid in UNSUPPORTED_TEAM_UIDS or manufacturer_uid in UNSUPPORTED_MANUFACTURER_UIDS:
            raise ValueError('that team/manufacturer combination is unsupported')
        catalog = self.catalog()
        team = next((row for row in catalog['teams'] if int(row['uid']) == team_uid), None)
        manufacturer = next((row for row in catalog['manufacturers'] if int(row['uid']) == manufacturer_uid), None)
        if team is None or manufacturer is None:
            raise ValueError('team or manufacturer was not found')
        old_uid = int(team['manufacturer_uid'])
        preview = {'kind': 'team_manufacturer', 'team_uid': team_uid,
                   'old_manufacturer_uid': old_uid, 'new_manufacturer_uid': manufacturer_uid,
                   'team': team.get('label'), 'manufacturer': manufacturer.get('label')}
        if dry_run:
            return {'ok': True, 'dry_run': True, 'preview': preview}
        if old_uid == manufacturer_uid:
            return {'ok': True, 'verified': True, 'changed': False, 'preview': preview}
        with self._lock:
            snapshot, manifest = self._begin(
                f"Change {team.get('label', team_uid)} manufacturer to {manufacturer.get('label', manufacturer_uid)}",
            )
            try:
                result = self.teams.apply([{
                    'kind': 'team_manufacturer', 'team_uid': team_uid,
                    'manufacturer_uid': manufacturer_uid,
                }])
                state = self.load_state()
                state['team_manufacturers'][str(team_uid)] = manufacturer_uid
                state['history'].append({
                    'kind': 'team_manufacturer', 'uid': team_uid,
                    'old_uid': old_uid, 'new_uid': manufacturer_uid,
                    'rollback_created': manifest.get('created'),
                    'rollback_created_epoch': manifest.get('created_epoch'),
                    'rollback_label': manifest.get('label'),
                })
                state['history'] = state['history'][-100:]
                self.save_state(state)
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'changed': True,
                'preview': preview, 'result': result, 'rollback_available': True}

    def rename_team(self, team_uid: int, new_name: str) -> dict:
        team_uid = int(team_uid)
        new_name = str(new_name).strip()
        if not new_name or len(new_name) > 80:
            raise ValueError('team name must contain 1 to 80 characters')
        if team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('that incomplete reserve team is not supported')
        with self._lock:
            catalog = self.catalog()
            team = next((row for row in catalog['teams'] if int(row['uid']) == team_uid), None)
            if team is None:
                raise ValueError('team was not found')
            current = str(team['label'])
            if current == new_name:
                return {'ok': True, 'verified': True, 'changed': False, 'name': new_name}
            original = str(team.get('original_label') or current)
            snapshot, manifest = self._begin(f'Rename {current} to {new_name}')
            try:
                text = TextTableEditor(self.installation)
                candidates = {current, original}
                patched = text.replace_exact(candidates, new_name)
                state = self.load_state()
                state['team_names'][str(team_uid)] = new_name
                state['history'].append({
                    'kind': 'team_name', 'uid': team_uid,
                    'old_name': current, 'new_name': new_name,
                    'patched': int(patched['changes']),
                    'rollback_created': manifest.get('created'),
                    'rollback_created_epoch': manifest.get('created_epoch'),
                    'rollback_label': manifest.get('label'),
                })
                state['history'] = state['history'][-100:]
                self.save_state(state)
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'changed': True, 'name': new_name,
                'patched': int(patched['changes']), 'text': patched,
                'warning': None if patched['changes'] else
                'Saved by stable team UID; this slot has no matching live localization string.',
                'rollback_available': True}

    def replace_logo(self, team_uid: int, source: str | Path) -> dict:
        team_uid = int(team_uid)
        state = self.load_state()
        donor = int(state['team_logo_donors'].get(
            str(team_uid), TEAM_DEFAULT_LOGO_DONORS.get(team_uid, team_uid),
        ))
        backend = self._backend()
        spec = backend.team_logo_spec(self.installation.root, team_uid, donor)
        image = Image.open(source); image.load()
        prepared = prepare_image(image, (int(spec['width']), int(spec['height'])), 'fit')
        with self._lock, tempfile.TemporaryDirectory(prefix='n15_team_logo_') as folder:
            path = Path(folder) / 'logo.png'; prepared.save(path, 'PNG')
            snapshot, _manifest = self._begin(f'Install or replace TEAM_{team_uid} logo')
            try:
                if self.status(team_uid).get('logo_ready'):
                    result = backend.replace_team_logo(self.installation.root, team_uid, path)
                else:
                    result = backend.ensure_team_logo(self.installation.root, team_uid, donor, path)
                state['team_logo_donors'][str(team_uid)] = donor
                self.save_state(state)
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'result': result,
                'preparation': {'source': list(image.size), 'target': list(prepared.size)},
                'rollback_available': True}

    def replace_driver_art(self, driver_key: int, kind: str, source: str | Path,
                           *, resize_mode='fit') -> dict:
        kind = str(kind).strip().lower()
        with self._lock:
            catalog, state = self.catalog(), self.load_state()
            driver = self._driver(int(driver_key), catalog)
            driver_uid, team_uid = int(driver['driver_uid']), int(driver['team_uid'])
            source_uid = self._source_team_uid(driver, state)
            backend = self._backend()
            snapshot, _manifest = self._begin(
                f"Replace Driver Select art for {driver.get('label', driver_uid)}",
            )
            try:
                try:
                    spec = backend.driver_art_spec(self.installation.root, team_uid, driver_uid, kind)
                except Exception:
                    self._ensure_driver(driver, team_uid, source_uid)
                    spec = backend.driver_art_spec(self.installation.root, team_uid, driver_uid, kind)
                image = Image.open(source); image.load()
                prepared = prepare_image(
                    image, (int(spec['width']), int(spec['height'])), resize_mode,
                )
                with tempfile.TemporaryDirectory(prefix='n15_driver_art_') as folder:
                    path = Path(folder) / 'art.png'; prepared.save(path, 'PNG')
                    result = backend.replace_driver_art(
                        self.installation.root, team_uid, driver_uid, kind, path,
                    )
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'result': result,
                'driver_uid': driver_uid, 'config_uid': int(driver['config_uid']),
                'team_uid': team_uid, 'kind': kind,
                'preparation': {'source': list(image.size), 'target': list(prepared.size)},
                'rollback_available': True}

    def repair_driver_art(self, driver_key: int) -> dict:
        with self._lock:
            catalog, state = self.catalog(), self.load_state()
            driver = self._driver(int(driver_key), catalog)
            source_uid = self._source_team_uid(driver, state)
            team_uid, driver_uid = int(driver['team_uid']), int(driver['driver_uid'])
            snapshot, _manifest = self._begin(
                f"Repair Driver Select art for {driver.get('label', driver_uid)}",
            )
            try:
                result = self._ensure_driver(driver, team_uid, source_uid)
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'result': result,
                'driver_uid': driver_uid, 'config_uid': int(driver['config_uid']),
                'team_uid': team_uid, 'source_team_uid': source_uid,
                'rollback_available': True}

    def repair_saved_links(self) -> dict:
        """Reapply saved team/manufacturer links through the canonical PYC editor."""
        with self._lock:
            state = self.load_state()
            changes = [
                {'kind': 'driver_team', 'config_uid': int(uid), 'team_uid': int(target)}
                for uid, target in state.get('driver_teams', {}).items()
            ] + [
                {'kind': 'team_manufacturer', 'team_uid': int(uid),
                 'manufacturer_uid': int(target)}
                for uid, target in state.get('team_manufacturers', {}).items()
            ]
            if not changes:
                return {'ok': True, 'verified': True, 'changed': False, 'changes': []}
            snapshot, _manifest = self._begin('Repair and reapply saved team links')
            try:
                result = self.teams.apply(changes)
            except Exception as error:
                self._rollback(snapshot, error)
        return {'ok': True, 'verified': True, 'changed': bool(result.get('changed')),
                'result': result, 'rollback_available': True}

    def undo(self) -> dict:
        """Restore the exact pre-change checkpoint; never approximate with a DB-only flip."""
        assert_process_closed('NASCAR15.exe', 'undoing a team change')
        with self._lock:
            snapshot, manifest = self.checkpoint.load()
            errors = self.transaction.restore(snapshot)
            if errors:
                raise RuntimeError('exact team undo reported: ' + '; '.join(errors))
            self.checkpoint.clear()
        return {'ok': True, 'verified': True, 'exact': True,
                'restored': manifest.get('label'), 'created': manifest.get('created')}

    def read_logo(self, team_uid: int) -> Image.Image:
        backend = self._backend()
        spec = backend.team_logo_spec(self.installation.root, int(team_uid), int(team_uid))
        from nascar_modding.editing.textures import TextureBankEditor
        return TextureBankEditor(self.installation).read_image('0', backend.MENU_CONTAINER, spec['entry'])

    def resource_locations(self) -> dict:
        """Return read-only presentation-resource locations by resource name."""
        return self._backend().resource_locations(self.installation.root)

    def team_resource_names(self, team_uid: int) -> set[str]:
        return set(self._backend().team_container_resource_names(
            self.installation.root, int(team_uid),
        ))

    def read_driver_art(self, driver_key: int, kind: str) -> Image.Image:
        driver = self._driver(int(driver_key))
        backend = self._backend()
        resolved = backend.resolve_driver_art_container(
            self.installation.root, int(driver['team_uid']), int(driver['driver_uid']),
        )
        return backend.read_driver_art_image(
            self.installation.root, int(resolved['team_uid']), int(driver['driver_uid']), kind,
        )
