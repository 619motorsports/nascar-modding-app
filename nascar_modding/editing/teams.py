"""Shared NASCAR 15 team-link catalog and transactional database editor."""

from __future__ import annotations

from pathlib import Path

from nascar_modding.core.modules import load_module
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.editing.transactions import TeamAssetCheckpoint, TeamAssetTransaction
from nascar_modding.core.processes import assert_process_closed
from nascar_modding.games.installation import GameInstallation


TEAM_DATABASE = 'DB_GAME_LOCAL_SCRIPT.PYC'


class TeamEditor:
    _module = None

    def __init__(self, installation: GameInstallation):
        if installation.profile.id != 'nascar15':
            raise ValueError('the recovered team-link editor currently applies only to NASCAR 15')
        self.installation = installation
        self.resources = ResourceEditor(installation)

    @classmethod
    def _backend(cls):
        if cls._module is not None:
            return cls._module
        path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_team_manager_v1.py'
        module = load_module(
            path, 'nascar_modding_shared_teams', add_parent=True,
            load_message=f'could not load team mapper: {path}',
        )
        cls._module = module
        return module

    def _source(self) -> tuple[str, str, bytes]:
        matches = []
        for archive in self.installation.archive_pairs:
            for entry in self.installation.entries(archive):
                if entry.name.casefold() == TEAM_DATABASE.casefold():
                    matches.append((archive, entry.name))
        if len(matches) != 1:
            raise ValueError(f'team editing requires one active {TEAM_DATABASE}; found {len(matches)}')
        archive, name = matches[0]
        return archive, name, self.installation.read_entry(name, archive)

    def catalog(self) -> dict:
        _archive, _name, payload = self._source()
        return self.catalog_payload(payload)

    def catalog_payload(self, payload: bytes) -> dict:
        result = self._backend().catalog(payload)
        result['legacy_team_patch'] = self._backend().legacy_patch_status(payload)
        manufacturers = {int(row['uid']): row for row in result['manufacturers']}
        teams = {int(row['uid']): row for row in result['teams']}
        drivers_by_config = {int(row['config_uid']): row for row in result['drivers']}
        for team in result['teams']:
            team['manufacturer_label'] = manufacturers.get(int(team['manufacturer_uid'] or -1), {}).get('label', 'Unknown')
            team['drivers'] = [drivers_by_config[uid] for uid in team['driver_config_uids'] if uid in drivers_by_config]
        for driver in result['drivers']:
            driver['team_label'] = teams.get(int(driver['team_uid']), {}).get('label', 'Unknown')
        return result

    @staticmethod
    def _normalize(changes: list[dict]) -> list[dict]:
        if not isinstance(changes, list) or not changes:
            raise ValueError('no team-link changes were supplied')
        normalized = []
        for item in changes:
            kind = str(item.get('kind', ''))
            if item.get('class_name'):
                normalized.append({'class_name': str(item['class_name']), 'field': str(item['field']),
                                   'uid': int(item['uid']), 'target_uid': int(item['target_uid'])})
            elif kind == 'driver_team':
                normalized.append({'class_name': 'DRIVERCONFIG_c', 'field': 'TEAM',
                                   'uid': int(item['config_uid']), 'target_uid': int(item['team_uid'])})
            elif kind == 'team_manufacturer':
                normalized.append({'class_name': 'RACETEAM_c', 'field': 'MANUFACTURER',
                                   'uid': int(item['team_uid']), 'target_uid': int(item['manufacturer_uid'])})
            else:
                raise ValueError(f'unsupported team-link change: {kind}')
        return normalized

    def preview(self, changes: list[dict]) -> dict:
        archive, name, payload = self._source()
        normalized = self._normalize(changes)
        rebuilt, metadata = self._backend().build_changes(payload, normalized)
        plan = self.resources.plan(name, archive, rebuilt)
        return {'ok': True, 'dry_run': True, 'archive': archive, 'entry': name,
                'changes': metadata['changes'], 'change_count': len(metadata['changes']),
                'recovery': metadata.get('recovery'), 'method': plan['method'], '_payload': rebuilt}

    def apply(self, changes: list[dict]) -> dict:
        preview = self.preview(changes)
        payload = preview.pop('_payload')
        if not preview['change_count']:
            preview.update(dry_run=False, verified=True, changed=False, write=None)
            return preview
        archive, name = preview['archive'], preview['entry']
        original = self.resources.read(name, archive)
        installed = False
        try:
            write = self.resources.replace(name, archive, payload)
            installed = True
            readback = self.resources.read(name, archive)
            if readback != payload:
                raise IOError('team-link database byte read-back failed')
            catalog = self._backend().catalog(readback)
            drivers = {int(row['config_uid']): row for row in catalog['drivers']}
            teams = {int(row['uid']): row for row in catalog['teams']}
            for change in preview['changes']:
                if change['class_name'] == 'DRIVERCONFIG_c':
                    actual = drivers[int(change['uid'])]['team_uid']
                else:
                    actual = teams[int(change['uid'])]['manufacturer_uid']
                if int(actual) != int(change['target_uid']):
                    raise IOError('team-link semantic read-back failed')
        except Exception as install_error:
            if installed:
                try:
                    self.resources.replace(name, archive, original)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'team-link install failed ({install_error}) and rollback failed: {rollback_error}'
                    ) from install_error
            raise
        preview.update(dry_run=False, verified=True, changed=bool(preview['change_count']), write=write)
        return preview

    def restore(self) -> dict:
        archive, name, _payload = self._source()
        return self.resources.restore(name, archive)


class TeamPresentationRecovery:
    """Shared status and exact restore for team presentation checkpoints."""

    def __init__(self, installation: GameInstallation, app_data_root: str | Path):
        root = Path(app_data_root)
        self.transaction = TeamAssetTransaction(
            installation, root / 'team_manager_state.json',
            root / 'extra_schemes_v1.json',
        )
        self.checkpoint = TeamAssetCheckpoint(
            self.transaction, root / 'team_asset_rollback_v1',
        )

    def status(self) -> dict:
        return self.checkpoint.info()

    def restore(self) -> dict:
        assert_process_closed('NASCAR15.exe', 'restoring team presentation assets')
        snapshot, manifest = self.checkpoint.load()
        errors = self.transaction.restore(snapshot)
        if errors:
            raise RuntimeError('team asset rollback reported: ' + '; '.join(errors))
        self.checkpoint.clear()
        return {
            'ok': True, 'verified': True, 'restored': manifest.get('label'),
            'created': manifest.get('created'),
        }
