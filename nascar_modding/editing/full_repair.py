"""Failure-focused whole-install audit and repair composed from shared editors."""

from __future__ import annotations

import json
from pathlib import Path
import time

from nascar_modding.core.files import atomic_write_json
from nascar_modding.core.processes import assert_process_closed
from nascar_modding.editing.audio import AudioBankEditor
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.managed_paints import ManagedPaintEditor
from nascar_modding.editing.names import DriverNameEditor
from nascar_modding.editing.ratings import RatingsEditor
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.team_presentation import (
    SUPPORTED_SPARE_TEAM_UIDS, TeamPresentationEditor,
)
from nascar_modding.editing.text_tables import TextTableEditor
from nascar_modding.editing.transactions import ExactFileTransaction
from nascar_modding.editing.user_library import profile_config_path
from nascar_modding.games.installation import GameInstallation
from nascar_modding.verification.support import SupportReporter


class FullRepairEditor:
    """Diagnose first, then repair only app-owned/reconstructable state."""

    def __init__(self, installation: GameInstallation, app_data_root: str | Path,
                 *, app_version='unknown'):
        if installation.profile.id != 'nascar15':
            raise ValueError('failure-focused full repair currently applies only to NASCAR 15')
        self.installation = installation
        self.root = Path(app_data_root).resolve()
        self.app_version = str(app_version)
        self.paint_state = self.root / 'extra_schemes_v1.json'
        self.paint_images = self.root / 'schemes' / 'extra'
        self.team_state = self.root / 'team_manager_state.json'
        self.config_path = profile_config_path(self.root, 'nascar15')
        self.report_path = self.root / 'last_whole_mod_repair.json'
        self.paints = ManagedPaintEditor(installation, self.paint_state)
        self.teams = TeamPresentationEditor(installation, self.root)

    @staticmethod
    def _issue(code, subsystem, severity, detail, *, repairable=True, **identity):
        return {'code': code, 'subsystem': subsystem, 'severity': severity,
                'detail': str(detail), 'repairable': bool(repairable), **identity}

    def check(self) -> dict:
        issues = []
        support_rows, support_summary = SupportReporter(
            self.installation, Path(__file__).resolve().parents[2], self.root,
            self.app_version,
        ).checks()
        for row in support_rows:
            if row['status'] == 'fail':
                issues.append(self._issue(
                    'support_check_failed', row['name'], 'fail', row['detail'],
                    repairable=False,
                ))

        state = self.paints.state()
        active = [row for row in state.get('schemes', [])
                  if row.get('uid') is not None and not row.get('superseded_by')]
        database = self.paints.database_audit()
        for row in database.get('issues', []):
            issues.append(self._issue(
                str(row.get('code') or 'managed_database_issue'),
                'Managed paint database',
                'fail' if row.get('fatal_possible', True) else 'warn',
                row.get('detail') or row,
                repairable=bool(row.get('repairable', True)), uid=row.get('uid'),
            ))

        missing_sources = []
        for row in active:
            uid = int(row['uid'])
            source = self.paint_images / Path(str(row.get('source_png') or '')).name
            if not row.get('source_png') or not source.is_file():
                missing_sources.append(uid)
                issues.append(self._issue(
                    'paint_source_missing', 'Managed paint runtime', 'fail',
                    f'UID {uid} has no saved paint source PNG.',
                    repairable=False, uid=uid,
                ))
        previews = self.paints.preview_audit()
        for uid in previews.get('missing', []):
            issues.append(self._issue(
                'managed_preview_missing', 'Paint Select thumbnails', 'fail',
                f'Managed paint UID {uid} has no live thumbnail.', uid=int(uid),
            ))

        team_catalog = self.teams.catalog()
        driver_by_team = {}
        for driver in team_catalog.get('drivers', []):
            driver_by_team.setdefault(int(driver['team_uid']), []).append(driver)
        for team in team_catalog.get('teams', []):
            uid = int(team['uid'])
            if uid in driver_by_team and not team.get('presentation', {}).get('presentation_ready'):
                issues.append(self._issue(
                    'team_presentation_incomplete', 'Teams and presentation', 'fail',
                    f"{team['label']} is missing its logo or Driver Select bank.", team_uid=uid,
                ))

        release_blockers = []
        by_driver = {int(row['driver_uid']): row for row in team_catalog.get('drivers', [])}
        for row in active:
            driver = by_driver.get(int(row.get('driver_uid', -1)))
            if driver and int(driver['team_uid']) in SUPPORTED_SPARE_TEAM_UIDS:
                release_blockers.append({
                    'uid': int(row['uid']), 'driver_uid': int(driver['driver_uid']),
                    'team_uid': int(driver['team_uid']),
                })

        fail_count = sum(row['severity'] == 'fail' for row in issues)
        warn_count = sum(row['severity'] == 'warn' for row in issues)
        return {
            'ok': True, 'healthy': fail_count == 0,
            'fail_count': fail_count, 'warn_count': warn_count,
            'issues': issues, 'release_blockers': release_blockers,
            'managed_paints': {'active': len(active), 'database': database,
                               'previews': previews, 'missing_sources': missing_sources},
            'teams': {'drivers': len(team_catalog.get('drivers', [])),
                      'teams': len(team_catalog.get('teams', []))},
            'support_summary': support_summary,
        }

    def paint_system_check(self) -> dict:
        """Read-only deep wiring audit shared by the Qt and compatibility UIs."""
        catalog = self.paints.catalog()
        state = self.paints.state()
        active = [row for row in state.get('schemes', [])
                  if row.get('uid') is not None and not row.get('superseded_by')]
        database = self.paints.database_audit()
        previews = self.paints.preview_audit()
        teams = self.teams.catalog()
        links = {int(row['driver_uid']): row for row in teams.get('drivers', [])}
        live_uids = {
            int(scheme['uid']) for driver in catalog.get('drivers', [])
            for scheme in driver.get('schemes', []) if scheme.get('uid') is not None
        }
        locations = self.teams.resource_locations()
        blocked = set(self.paints.uid_pool(include_live=False)['verified_broken'])
        checks, rows = [], []

        def add(name, status, detail):
            checks.append({'name': name, 'status': status, 'detail': detail})

        unsafe, missing_db, missing_sources, legacy = [], [], [], []
        missing_team, missing_art, missing_thumbnail = [], [], []
        invalid_structure, invalid_identity, unsafe_thumbnail, duplicates = [], [], [], []
        team_names = {}
        for item in active:
            uid, driver_uid = int(item['uid']), int(item.get('driver_uid', -1))
            if uid in blocked:
                unsafe.append(uid)
            if uid not in live_uids:
                missing_db.append(uid)
            if int(item.get('native_runtime_layout_version', 0)) < 1:
                legacy.append(uid)
            paint_name = Path(str(item.get('source_png') or '')).name
            thumb_name = Path(str(item.get('thumbnail_source_png') or '')).name
            paint_ready = bool(paint_name and (self.paint_images / paint_name).is_file())
            thumb_ready = bool(thumb_name and (self.paint_images / thumb_name).is_file())
            if not paint_ready or not thumb_ready:
                missing_sources.append(uid)
            driver = links.get(driver_uid)
            team_uid = int(driver['team_uid']) if driver else None
            target = f'2DRIVERSELECTTD_{team_uid}.ARC' if team_uid is not None else None
            resource_locations = list(locations.get(f'PAINTSCHEME_{uid}', []))
            current = bool(target and any(
                str(value).casefold() == target.casefold() for value in resource_locations
            ))
            identity = {}
            if driver is None:
                missing_team.append(uid)
            elif current:
                try:
                    identity = self.paints.thumbnail_identity(uid, team_uid)
                except Exception as exc:
                    identity = {'error': str(exc)}
            structural = bool(identity.get('structural_valid'))
            anchored = bool(
                identity.get('identity_self_identifying')
                and identity.get('public_name_resolved')
            )
            game_safe = bool(identity.get('same_bank_valid'))
            if not current:
                missing_thumbnail.append(uid)
            elif not structural:
                invalid_structure.append(uid)
            elif not anchored:
                invalid_identity.append(uid)
            elif not game_safe:
                unsafe_thumbnail.append(uid)
            if len(resource_locations) > 1:
                duplicates.append(uid)
            art_ready = False
            if driver is not None:
                if team_uid not in team_names:
                    team_names[team_uid] = self.teams.team_resource_names(team_uid)
                names = team_names[team_uid]
                art_ready = (
                    f'DRIVERPAINT_{driver_uid}_25041' in names
                    and f'DRIVER_{driver_uid}_3DNUM_25041' in names
                )
                if not art_ready:
                    missing_art.append(driver_uid)
            rows.append({
                'uid': uid, 'name': item.get('name') or item.get('script_name'),
                'driver_uid': driver_uid,
                'driver': (driver or {}).get('label') or str(driver_uid),
                'team_uid': team_uid, 'team': (driver or {}).get('team_label'),
                'target_container': target, 'database_ready': uid in live_uids,
                'paint_source_ready': paint_ready, 'thumbnail_source_ready': thumb_ready,
                'runtime_ready': int(item.get('native_runtime_layout_version', 0)) >= 1,
                'current_team_thumbnail': current,
                'thumbnail_structural_valid': structural,
                'thumbnail_same_bank_identity': anchored,
                'thumbnail_game_safe': game_safe,
                'thumbnail_identity_name': identity.get('identity_name'),
                'thumbnail_locations': resource_locations, 'driver_art_ready': art_ready,
            })

        add('Paint backends', 'pass', 'Managed-paint, thumbnail, and team services loaded.')
        add('Verified UID allocator', 'fail' if unsafe else 'pass',
            ('Blocked active UIDs: ' + ', '.join(map(str, unsafe))) if unsafe
            else 'All active app-created schemes use non-blocked UIDs.')
        add('Live livery records', 'fail' if missing_db else 'pass',
            ('Missing live UIDs: ' + ', '.join(map(str, missing_db))) if missing_db
            else f'All {len(active)} active managed livery records are present.')
        add('Saved paint + thumbnail sources', 'warn' if missing_sources else 'pass',
            ('Saved source image missing for UID(s): ' + ', '.join(map(str, missing_sources)))
            if missing_sources else 'Every managed scheme retains both source images.')
        add('Native paint structure', 'fail' if legacy else 'pass',
            ('Legacy runtime layout on UID(s): ' + ', '.join(map(str, legacy))) if legacy
            else 'Every managed scheme uses the proven native runtime layout.')
        add('Current team links', 'fail' if missing_team else 'pass',
            ('No current team link for UID(s): ' + ', '.join(map(str, missing_team)))
            if missing_team else 'Every managed scheme resolves to its current team.')
        add('Driver Select art', 'fail' if missing_art else 'pass',
            ('Missing art for driver UID(s): ' + ', '.join(map(str, sorted(set(missing_art)))))
            if missing_art else 'Every managed-scheme driver has current-team tile and number art.')
        add('Current-team thumbnails', 'fail' if missing_thumbnail else 'pass',
            ('Missing current-team thumbnail for UID(s): ' + ', '.join(map(str, missing_thumbnail)))
            if missing_thumbnail else 'Every managed scheme has a current-team thumbnail.')
        bad_identity = sorted(set(invalid_structure + invalid_identity + unsafe_thumbnail))
        add('Native thumbnail identity', 'fail' if bad_identity else 'pass',
            ('Unsafe or unresolved thumbnail UID(s): ' + ', '.join(map(str, bad_identity)))
            if bad_identity else 'Every current-team thumbnail has valid structure and same-bank identity.')
        add('Old-team thumbnail copies', 'warn' if duplicates else 'pass',
            ('Extra copies remain for UID(s): ' + ', '.join(map(str, duplicates)))
            if duplicates else 'No duplicate managed thumbnails were found.')
        rank = {'pass': 0, 'warn': 1, 'fail': 2}
        overall = max((row['status'] for row in checks), key=rank.get, default='pass')
        return {
            'ok': True, 'overall': overall, 'checks': checks, 'schemes': rows,
            'active_count': len(active), 'database': database, 'previews': previews,
            'live_reconciliation': catalog.get('live_reconciliation'),
            'read_only_game_files': True,
        }

    def _verification(self) -> list[dict]:
        checks = []
        probes = (
            ('Driver names', lambda: len(DriverNameEditor(self.installation).drivers())),
            ('Ratings', lambda: len(RatingsEditor(self.installation).ratings())),
            ('Schedule', lambda: len(ScheduleEditor(self.installation, self.config_path).rows())),
            ('Text tables', lambda: len(TextTableEditor(self.installation).files())),
            ('Audio banks', lambda: len(AudioBankEditor(self.installation).banks())),
        )
        for name, function in probes:
            try:
                count = function()
                checks.append({'name': name, 'status': 'pass', 'detail': f'{count} row(s) parsed'})
            except Exception as exc:
                checks.append({'name': name, 'status': 'fail', 'detail': str(exc)})
        return checks

    def apply(self) -> dict:
        assert_process_closed('NASCAR15.exe', 'running full repair')
        before = self.check()
        if before['release_blockers']:
            raise ValueError(
                'Full repair is blocked while an app-created paint belongs to a custom team; '
                'remove that paint or move the driver back first.'
            )
        unrepairable = [row for row in before['issues']
                        if row['severity'] == 'fail' and not row['repairable']]
        if unrepairable:
            raise ValueError('Unrepairable failure: ' + '; '.join(row['detail'] for row in unrepairable))

        backup = BackupManager(self.installation).create_missing(('0', '1', '2'))
        if not backup['ok']:
            raise IOError('could not create full-repair backups: ' + '; '.join(backup['failed']))
        files = []
        for key in ('0', '1', '2'):
            pair = self.installation.archive_pairs[key]
            files.extend((pair.archive, pair.index))
        files.extend((self.config_path, self.paint_state, self.team_state))
        transaction = ExactFileTransaction()
        snapshot = transaction.snapshot(
            files, directories=(self.paint_images, self.teams.rollback_dir),
        )
        report = {
            'ok': False, 'version': self.app_version,
            'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'scan_before': before, 'steps': [],
        }
        try:
            state = self.paints.state()
            active = [row for row in state.get('schemes', [])
                      if row.get('uid') is not None and not row.get('superseded_by')]
            if self.paint_state.is_file():
                result = self.paints.repair_state_from_live()
                report['steps'].append({'name': 'Managed paint identity', 'result': result})
            if active:
                result = self.paints.repair_runtime()
                report['steps'].append({'name': 'Managed paint runtime', 'result': result})
                result = self.paints.repair_missing_previews()
                report['steps'].append({'name': 'Managed paint previews', 'result': result})
                result = self.paints.finalize_registry()
                report['steps'].append({'name': 'Managed paint registry', 'result': result})

            team_state = self.teams.load_state()
            if team_state.get('driver_teams') or team_state.get('team_manufacturers'):
                result = self.teams.repair_saved_links()
                report['steps'].append({'name': 'Saved team links', 'result': result})
            refreshed = self.teams.catalog()
            occupied = {int(row['team_uid']) for row in refreshed.get('drivers', [])}
            for team in refreshed.get('teams', []):
                uid = int(team['uid'])
                if uid in occupied and not team.get('presentation', {}).get('presentation_ready'):
                    result = self.teams.prepare_team(uid)
                    report['steps'].append({
                        'name': f"Presentation: {team['label']}", 'result': result,
                    })

            state = self.paints.state()
            assignments = self.paints.assignments()
            if assignments and bool((state.get('ai') or {}).get('applied')):
                result = self.paints.apply_ai()
                report['steps'].append({'name': 'AI paint schedule', 'result': result})

            after = self.check()
            remaining = [row for row in after['issues'] if row['severity'] == 'fail']
            verification = self._verification()
            verification_failures = [row for row in verification if row['status'] == 'fail']
            if remaining or verification_failures:
                raise RuntimeError(
                    'post-repair verification failed: ' + '; '.join(
                        [row['detail'] for row in remaining] +
                        [row['detail'] for row in verification_failures]
                    )
                )
            report.update(ok=True, scan_after=after, verification=verification,
                          finished=time.strftime('%Y-%m-%d %H:%M:%S'),
                          summary=f"Repair cleared {before['fail_count']} fatal candidate(s).")
        except Exception as error:
            rollback = transaction.restore(snapshot)
            for key in ('0', '1', '2'):
                self.installation.invalidate_archive(key)
            report.update(error=str(error), rollback_errors=rollback,
                          rolled_back=not bool(rollback),
                          finished=time.strftime('%Y-%m-%d %H:%M:%S'))
            atomic_write_json(self.report_path, report, indent=2)
            detail = str(error)
            if rollback:
                detail += ' | Rollback warnings: ' + '; '.join(rollback)
            raise RuntimeError(detail) from error
        finally:
            transaction.clear(snapshot)
        for key in ('0', '1', '2'):
            self.installation.invalidate_archive(key)
        atomic_write_json(self.report_path, report, indent=2)
        return report

    def report(self) -> dict:
        if not self.report_path.is_file():
            raise ValueError('no full-repair report exists yet')
        return json.loads(self.report_path.read_text(encoding='utf-8'))
