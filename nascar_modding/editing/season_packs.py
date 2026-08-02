"""Portable, previewable season packs composed from shared editing services."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import time
import zipfile

from nascar_modding.core.files import atomic_write_bytes
from nascar_modding.core.processes import assert_process_closed
from nascar_modding.editing.audio import AudioBankEditor
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.names import DriverHandleEditor, DriverNameEditor
from nascar_modding.editing.pyc_records import PycRecordEditor, WORKFLOWS
from nascar_modding.editing.ratings import RatingsEditor
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.scr import ScrEditor
from nascar_modding.editing.text_tables import TextTableEditor
from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.editing.transactions import ExactFileTransaction
from nascar_modding.editing.user_library import UserLibrary, profile_config_path
from nascar_modding.games.installation import GameInstallation


PACK_FORMAT = 'nascar-modding-shared-pack'
PACK_VERSION = 3
PACK_CATEGORIES = (
    'schemes', 'names', 'ratings', 'text', 'pyc', 'schedule', 'scr',
    'textures', 'audio', 'presets', 'pit_log',
)
MAX_MEMBERS = 20000
MAX_MEMBER = 128 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024


def _pack_same(left, right) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return str(left) == str(right)


class SeasonPackEditor:
    """Export and atomically import proven shared-editor workflows."""

    def __init__(self, installation: GameInstallation, app_data_root: str | Path):
        self.installation = installation
        self.app_data_root = Path(app_data_root).resolve()
        self.scheme_dir = self.app_data_root / 'schemes'
        self.config_path = profile_config_path(self.app_data_root, installation.profile.id)
        self.names = DriverNameEditor(installation)
        self.handles = DriverHandleEditor(installation, self.config_path)
        self.ratings = RatingsEditor(installation)
        self.text = TextTableEditor(installation)
        self.pyc = PycRecordEditor(installation)
        self.schedule = ScheduleEditor(installation, self.config_path)
        self.scr = ScrEditor(installation)
        self.textures = TextureBankEditor(installation)
        self.audio = AudioBankEditor(installation)
        self.library = UserLibrary(self.config_path)

    @staticmethod
    def _json(value) -> bytes:
        return json.dumps(value, indent=2, ensure_ascii=False).encode('utf-8')

    @staticmethod
    def _safe_member(name: str) -> str:
        normalized = str(name).replace('\\', '/').strip('/')
        parts = PurePosixPath(normalized).parts
        if not normalized or ':' in normalized or '..' in parts:
            raise ValueError('pack contains an unsafe member path')
        return '/'.join(parts)

    @staticmethod
    def _write_payload(archive: zipfile.ZipFile, name: str, payload: bytes) -> dict:
        raw = bytes(payload)
        archive.writestr(name, raw)
        return {'file': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def _pyc_changes(self) -> dict[str, list[dict]]:
        result = {}
        for workflow, (_filename, _class_name, fields) in WORKFLOWS.items():
            try:
                live = {str(row['uid']): row for row in self.pyc.records(workflow)}
                stock = {str(row['uid']): row for row in self.pyc.records(workflow, pristine=True)}
            except Exception:
                continue
            rows = []
            for uid, current in live.items():
                original = stock.get(uid)
                if original is None:
                    continue
                for field in fields:
                    if not _pack_same(current.get(field), original.get(field)):
                        rows.append({'uid': uid, 'field': field, 'value': current.get(field)})
            if rows:
                result[workflow] = rows
        return result

    def export_bytes(self) -> tuple[bytes, dict]:
        items = {category: [] for category in PACK_CATEGORIES}
        warnings = []
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            if self.scheme_dir.is_dir():
                for path in sorted(self.scheme_dir.iterdir()):
                    if not path.is_file():
                        continue
                    name = f'schemes/{path.name}'
                    metadata = self._write_payload(archive, name, path.read_bytes())
                    items['schemes'].append({'name': path.name, **metadata})

            for row in self.names.drivers():
                if row['available'] and row['current'] != row['original']:
                    items['names'].append({
                        'kind': 'driver_name', 'driver_uid': int(row['driver_uid']),
                        'name': row['current'],
                    })
            for row in self.handles.handles():
                if row['available'] and row['current'] != row['original']:
                    items['names'].append({
                        'kind': 'driver_handle', 'driver_uid': int(row['driver_uid']),
                        'name': row['current'],
                    })

            try:
                live = {int(row['profile_id']): row for row in self.ratings.ratings()}
                stock = {int(row['profile_id']): row for row in self.ratings.ratings(pristine=True)}
                for profile_id, row in live.items():
                    base = stock.get(profile_id)
                    if base is None:
                        continue
                    changes = {field: value for field, value in row['stats'].items()
                               if not _pack_same(value, base['stats'].get(field))}
                    if changes:
                        items['ratings'].append({'profile_id': profile_id, 'stats': changes})
            except Exception as exc:
                warnings.append('Ratings: ' + str(exc))

            for file_row in self.text.files():
                try:
                    for row in self.text.entries(file_row['name']):
                        if row['modified']:
                            items['text'].append({
                                'file': row['file'], 'index': int(row['index']),
                                'text': row['current'],
                            })
                except Exception as exc:
                    warnings.append(f"Text {file_row['name']}: {exc}")

            pyc_changes = self._pyc_changes()
            items['pyc'] = [
                {'workflow': workflow, 'changes': changes}
                for workflow, changes in pyc_changes.items()
            ]

            try:
                live = sorted(self.schedule.rows(), key=lambda row: int(row['order']))
                stock = {int(row['uid']): row for row in self.schedule.catalog()}
                stock_order = sorted(stock.values(), key=lambda row: int(row['order']))
                changed = len(live) == len(stock_order) and any(
                    int(left['event_uid']) != int(right['event_uid'])
                    or str(left['event']) != str(right['event'])
                    or int(left['laps']) != int(right['laps'])
                    for left, right in zip(live, stock_order)
                )
                if changed:
                    by_event = {(int(row['event_uid']), str(row['event'])): row for row in stock.values()}
                    items['schedule'] = [{
                        'slot': int(row['order']),
                        'source_uid': int(by_event[(int(row['event_uid']), str(row['event']))]['uid']),
                        'laps': int(row['laps']),
                    } for row in live]
            except Exception as exc:
                warnings.append('Schedule: ' + str(exc))

            try:
                items['scr'] = [{
                    key: row[key] for key in ('archive', 'name', 'key', 'occurrence', 'value')
                } for row in self.scr.inventory(include_stock=True) if row['modded']]
            except Exception as exc:
                warnings.append('SCR: ' + str(exc))

            texture_index = 0
            texture_pristine_missing = False
            for archive_key in self.installation.archive_pairs:
                for container in self.textures.containers(archive_key):
                    try:
                        upper = container['name'].upper()
                        if (container['name'].casefold() !=
                                self.installation.profile.number_container.casefold()
                                and 'DRIVERSELECTTD_' not in upper):
                            continue
                        try:
                            live_container = self.textures.resources.read(
                                container['name'], archive_key,
                            )
                            stock_container = self.textures.resources.read(
                                container['name'], archive_key, pristine=True,
                            )
                        except (FileNotFoundError, KeyError):
                            texture_pristine_missing = True
                            continue
                        if live_container == stock_container:
                            continue
                        for row in self.textures.entries(archive_key, container['name']):
                            if not row['replace_supported']:
                                continue
                            live = self.textures.raw_entry(archive_key, container['name'], row['name'])
                            stock = self.textures.raw_entry(
                                archive_key, container['name'], row['name'], pristine=True,
                            )
                            if live['payload'] == stock['payload']:
                                continue
                            member = f'textures/{texture_index:05d}.bin'
                            meta = self._write_payload(archive, member, live['payload'])
                            items['textures'].append({
                                'archive': str(archive_key), 'container': container['name'],
                                'entry': row['name'], 'width': live['width'],
                                'height': live['height'], 'format': live['format'], **meta,
                            })
                            texture_index += 1
                    except Exception as exc:
                        warnings.append(f"Texture {container['name']}: {exc}")
            if texture_pristine_missing:
                warnings.append('Textures: one or more archive groups have no pristine backup; those groups were skipped')

            audio_index = 0
            audio_pristine_missing = False
            for bank in self.audio.banks():
                try:
                    try:
                        live_bank = self.audio.resources.read(bank['name'], bank['archive'])
                        stock_bank = self.audio.resources.read(
                            bank['name'], bank['archive'], pristine=True,
                        )
                    except (FileNotFoundError, KeyError):
                        audio_pristine_missing = True
                        continue
                    if live_bank == stock_bank:
                        continue
                    status = self.audio.samples(bank['archive'], bank['name'])
                    for row in status['samples']:
                        if not row['modified']:
                            continue
                        sample = self.audio.raw_sample(bank['archive'], bank['name'], row['index'])
                        member = f'audio/{audio_index:05d}.bin'
                        meta = self._write_payload(archive, member, sample['payload'])
                        items['audio'].append({
                            'archive': str(bank['archive']), 'bank': bank['name'],
                            'index': int(row['index']), 'name': sample['name'],
                            'mode': sample['mode'], **meta,
                        })
                        audio_index += 1
                except Exception as exc:
                    warnings.append(f"Audio {bank['name']}: {exc}")
            if audio_pristine_missing:
                warnings.append('Audio: one or more archive groups have no pristine backup; those groups were skipped')

            items['presets'] = self.library.presets()
            items['pit_log'] = self.library.pit_entries()
            counts = {name: len(rows) for name, rows in items.items()}
            manifest = {
                'format': PACK_FORMAT, 'version': PACK_VERSION,
                'game': self.installation.profile.id,
                'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                'categories': list(PACK_CATEGORIES), 'counts': counts,
                'items': items, 'warnings': warnings,
            }
            archive.writestr('manifest.json', self._json(manifest))
            archive.writestr('README.txt', (
                'NASCAR Modding App shared season pack v3.\n'
                'Preview this pack before applying it.\n'
            ))
        return output.getvalue(), {'counts': counts, 'warnings': warnings}

    def export_file(self, destination: str | Path) -> dict:
        payload, report = self.export_bytes()
        path = Path(destination)
        if path.suffix.casefold() != '.gridpack':
            path = path.with_suffix(path.suffix + '.gridpack' if path.suffix else '.gridpack')
        atomic_write_bytes(path, payload, '.pack.tmp')
        return {'ok': True, 'path': str(path), 'bytes': len(payload), **report}

    def _convert_legacy_v1(self, payload: bytes) -> tuple[bytes, list[str]]:
        """Convert the safely identifiable subset of public gridpack v1."""
        migrations = []
        items = {category: [] for category in PACK_CATEGORIES}
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(
            output, 'w', zipfile.ZIP_DEFLATED,
        ) as destination:
            manifest = json.loads(source.read('manifest.json').decode('utf-8-sig'))
            if manifest.get('format') != 'gridpack' or int(manifest.get('version', 1)) != 1:
                raise ValueError('this legacy pack is not gridpack v1')
            for info in source.infolist():
                safe = self._safe_member(info.filename)
                if not safe.startswith('schemes/') or info.is_dir():
                    continue
                name = Path(PurePosixPath(safe).name).name
                raw = source.read(info)
                member = f'schemes/{name}'
                items['schemes'].append({
                    'name': name, **self._write_payload(destination, member, raw),
                })
            try:
                names = json.loads(source.read('names.json').decode('utf-8-sig'))
            except (KeyError, UnicodeError, ValueError):
                names = {}
            drivers = self.names.drivers()
            for old, new in (names.get('renames') or {}).items():
                matches = [row for row in drivers if str(old).casefold() in {
                    str(row['original']).casefold(), str(row['current']).casefold(),
                }]
                if len(matches) == 1:
                    items['names'].append({
                        'kind': 'driver_name', 'driver_uid': int(matches[0]['driver_uid']),
                        'name': str(new),
                    })
                else:
                    migrations.append(f'Legacy rename {old!r} was skipped because its driver identity is ambiguous')
            handles = self.handles.handles()
            for old, new in (names.get('handles') or {}).items():
                matches = [row for row in handles if str(old).casefold() in {
                    str(row['original']).casefold(), str(row['current']).casefold(),
                }]
                if len(matches) == 1:
                    items['names'].append({
                        'kind': 'driver_handle', 'driver_uid': int(matches[0]['driver_uid']),
                        'name': str(new),
                    })
                else:
                    migrations.append(f'Legacy handle {old!r} was skipped because its driver identity is ambiguous')
            try:
                ratings = json.loads(source.read('stats.json').decode('utf-8-sig'))
            except (KeyError, UnicodeError, ValueError):
                ratings = []
            for row in ratings if isinstance(ratings, list) else []:
                if isinstance(row, dict) and isinstance(row.get('stats'), dict):
                    items['ratings'].append({
                        'profile_id': int(row['profile_id']), 'stats': dict(row['stats']),
                    })
            if any(self._safe_member(info.filename).startswith('menus/') for info in source.infolist()):
                migrations.append(
                    'Legacy menu images were skipped: v1 lacks the raw format/spec hashes required by the shared texture writer',
                )
            counts = {name: len(rows) for name, rows in items.items()}
            converted = {
                'format': PACK_FORMAT, 'version': PACK_VERSION,
                'game': self.installation.profile.id,
                'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                'categories': list(PACK_CATEGORIES), 'counts': counts,
                'items': items, 'warnings': migrations,
                'migrated_from': {'format': 'gridpack', 'version': 1},
            }
            destination.writestr('manifest.json', self._json(converted))
        return output.getvalue(), migrations

    @staticmethod
    def _manifest_identity(payload: bytes) -> tuple[str | None, int]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8-sig'))
            pack_format = manifest.get('format')
            default_version = 1 if pack_format == 'gridpack' else 0
            return pack_format, int(manifest.get('version', default_version) or default_version)
        except Exception:
            return None, 0

    def inspect_bytes(self, payload: bytes) -> dict:
        source_format, source_version = self._manifest_identity(payload)
        migrated = None
        if source_format == 'gridpack' and source_version == 1:
            payload, migrated = self._convert_legacy_v1(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise ValueError('pack contains too many files')
            total = 0
            names = set()
            for info in infos:
                safe = self._safe_member(info.filename)
                if safe in names:
                    raise ValueError(f'pack contains duplicate member {safe}')
                names.add(safe)
                if info.file_size > MAX_MEMBER:
                    raise ValueError(f'pack member is too large: {safe}')
                total += info.file_size
                if total > MAX_TOTAL:
                    raise ValueError('uncompressed pack exceeds 2 GiB')
            try:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8-sig'))
            except (KeyError, UnicodeError, ValueError) as exc:
                raise ValueError('pack manifest is missing or invalid') from exc
            if manifest.get('format') != PACK_FORMAT or int(manifest.get('version', 0)) != PACK_VERSION:
                raise ValueError('this is not a shared season pack v3')
            if manifest.get('game') != self.installation.profile.id:
                raise ValueError('pack targets a different game')
            for category in PACK_CATEGORIES:
                if not isinstance((manifest.get('items') or {}).get(category, []), list):
                    raise ValueError(f'pack category {category} is invalid')
            for category in ('schemes', 'textures', 'audio'):
                for item in manifest['items'][category]:
                    member = self._safe_member(item.get('file'))
                    raw = archive.read(member)
                    if len(raw) != int(item.get('size', -1)):
                        raise ValueError(f'pack member size mismatch: {member}')
                    if hashlib.sha256(raw).hexdigest() != str(item.get('sha256')):
                        raise ValueError(f'pack member hash mismatch: {member}')
        result = {
            'ok': True, 'format': PACK_FORMAT, 'version': PACK_VERSION,
            'game': manifest['game'], 'created': manifest.get('created'),
            'categories': list(manifest.get('categories') or []),
            'counts': dict(manifest.get('counts') or {}),
            'warnings': list(manifest.get('warnings') or []),
            'uncompressed_bytes': total,
        }
        if migrated is not None:
            result.update(legacy=True, migrated_from={'format': 'gridpack', 'version': 1},
                          migrations=migrated)
        return result

    def inspect_file(self, source: str | Path) -> dict:
        return self.inspect_bytes(Path(source).read_bytes())

    def _load(self, payload: bytes) -> tuple[zipfile.ZipFile, dict]:
        self.inspect_bytes(payload)
        archive = zipfile.ZipFile(io.BytesIO(payload))
        manifest = json.loads(archive.read('manifest.json').decode('utf-8-sig'))
        return archive, manifest

    def _preflight(self, archive: zipfile.ZipFile, items: dict, selected: set[str]) -> None:
        if 'names' in selected:
            drivers = {int(row['driver_uid']) for row in self.names.drivers() if row['available']}
            handles = {int(row['driver_uid']) for row in self.handles.handles() if row['available']}
            if any(
                int(row['driver_uid']) not in (
                    handles if row.get('kind') == 'driver_handle' else drivers
                ) for row in items['names']
            ):
                raise ValueError('pack contains a driver name target unavailable in this install')
        if 'text' in selected:
            for row in items['text']:
                self.text.plan(row['file'], int(row['index']), str(row['text']), True)
        if 'pyc' in selected:
            for row in items['pyc']:
                changes = row.get('changes') or []
                for start in range(0, len(changes), 100):
                    self.pyc.preview(row['workflow'], changes[start:start + 100])
        if 'schedule' in selected and items['schedule']:
            self.schedule.preview_custom(items['schedule'])
        if 'scr' in selected:
            grouped = {}
            for row in items['scr']:
                grouped.setdefault(str(row['archive']), []).append(row)
            for rows in grouped.values():
                self.scr.preview(rows)
        if 'textures' in selected:
            for row in items['textures']:
                current = self.textures.raw_entry(row['archive'], row['container'], row['entry'])
                if (current['width'], current['height'], current['format'], current['payload_size']) != (
                    int(row['width']), int(row['height']), str(row['format']), int(row['size']),
                ):
                    raise ValueError(f"texture spec changed: {row['container']}/{row['entry']}")
        if 'audio' in selected:
            for row in items['audio']:
                current = self.audio.raw_sample(row['archive'], row['bank'], int(row['index']))
                if current['name'] != row['name'] or current['mode'] != int(row['mode']) or current['length'] != int(row['size']):
                    raise ValueError(f"audio spec changed: {row['bank']}/{row['name']}")

    def import_bytes(self, payload: bytes, categories=None) -> dict:
        assert_process_closed(
            self.installation.profile.id == 'nascar15' and 'NASCAR15.exe'
            or self.installation.profile.id == 'nascar14' and 'NASCAR14.exe'
            or 'NTG2013.exe', 'importing a season pack',
        )
        source_format, source_version = self._manifest_identity(payload)
        migrations = []
        if source_format == 'gridpack' and source_version == 1:
            payload, migrations = self._convert_legacy_v1(payload)
        archive, manifest = self._load(payload)
        try:
            items = manifest['items']
            selected = set(categories or manifest.get('categories') or PACK_CATEGORIES)
            selected &= set(PACK_CATEGORIES)
            self._preflight(archive, items, selected)
            affected = set()
            if selected & {'names', 'ratings', 'text', 'pyc', 'schedule'}:
                affected.add('0')
            if 'scr' in selected:
                affected.update(str(row['archive']) for row in items['scr'])
            if 'textures' in selected:
                affected.update(str(row['archive']) for row in items['textures'])
            if 'audio' in selected:
                affected.update(str(row['archive']) for row in items['audio'])
            backup = BackupManager(self.installation).create_missing(sorted(affected))
            if not backup['ok']:
                raise IOError('could not create pack backups: ' + '; '.join(backup['failed']))
            files = []
            for key in sorted(affected):
                pair = self.installation.archive_pairs[key]
                files.extend((pair.archive, pair.index))
            if selected & {'presets', 'pit_log', 'schedule'}:
                files.append(self.config_path)
            transaction = ExactFileTransaction()
            snapshot = transaction.snapshot(
                files, directories=[self.scheme_dir] if 'schemes' in selected else [],
            )
            applied = {category: 0 for category in PACK_CATEGORIES}
            try:
                if 'schemes' in selected:
                    self.scheme_dir.mkdir(parents=True, exist_ok=True)
                    for row in items['schemes']:
                        destination = self.scheme_dir / Path(row['name']).name
                        raw = archive.read(self._safe_member(row['file']))
                        if not destination.is_file() or destination.read_bytes() != raw:
                            atomic_write_bytes(destination, raw, '.pack.tmp')
                            applied['schemes'] += 1
                if 'names' in selected:
                    for row in items['names']:
                        editor = self.handles if row.get('kind') == 'driver_handle' else self.names
                        result = editor.rename(int(row['driver_uid']), str(row['name']))
                        applied['names'] += int(bool(result.get('changed', True)))
                if 'ratings' in selected:
                    for row in items['ratings']:
                        self.ratings.set_ratings(int(row['profile_id']), row['stats'], experimental=True)
                        applied['ratings'] += len(row['stats'])
                if 'text' in selected and items['text']:
                    changes = [{'file': row['file'], 'index': int(row['index']), 'new': row['text']}
                               for row in items['text']]
                    for start in range(0, len(changes), 500):
                        result = self.text.apply_batch(changes[start:start + 500], force_tokens=True)
                        applied['text'] += int(result['changes'])
                if 'pyc' in selected:
                    for row in items['pyc']:
                        changes = row.get('changes') or []
                        for start in range(0, len(changes), 100):
                            result = self.pyc.apply(row['workflow'], changes[start:start + 100])
                            applied['pyc'] += int(result['affected_count'])
                if 'schedule' in selected and items['schedule']:
                    result = self.schedule.apply_custom(items['schedule'])
                    applied['schedule'] = int(result['change_count'])
                if 'scr' in selected:
                    grouped = {}
                    for row in items['scr']:
                        grouped.setdefault(str(row['archive']), []).append(row)
                    for rows in grouped.values():
                        result = self.scr.apply(rows)
                        applied['scr'] += int(result['change_count'])
                if 'textures' in selected:
                    for row in items['textures']:
                        self.textures.replace_raw_entry(
                            row['archive'], row['container'], row['entry'],
                            archive.read(self._safe_member(row['file'])),
                        )
                        applied['textures'] += 1
                if 'audio' in selected:
                    for row in items['audio']:
                        self.audio.replace_raw_sample(
                            row['archive'], row['bank'], int(row['index']),
                            archive.read(self._safe_member(row['file'])),
                            name=row['name'], mode=int(row['mode']),
                        )
                        applied['audio'] += 1
                if selected & {'presets', 'pit_log'}:
                    bundle = self.library.merge_bundle(
                        items['presets'] if 'presets' in selected else [],
                        items['pit_log'] if 'pit_log' in selected else [],
                    )
                    applied.update(bundle)
            except Exception as error:
                rollback = transaction.restore(snapshot)
                for key in affected:
                    self.installation.invalidate_archive(key)
                detail = str(error)
                if rollback:
                    detail += ' | Rollback warnings: ' + '; '.join(rollback)
                raise RuntimeError(detail) from error
            finally:
                transaction.clear(snapshot)
            for key in affected:
                self.installation.invalidate_archive(key)
            return {'ok': True, 'verified': True, 'selected': sorted(selected),
                    'applied': applied, 'warnings': manifest.get('warnings') or [],
                    'migrations': migrations,
                    'migrated_from': ({'format': 'gridpack', 'version': 1}
                                      if migrations or source_format == 'gridpack' else None)}
        finally:
            archive.close()

    def import_file(self, source: str | Path, categories=None) -> dict:
        return self.import_bytes(Path(source).read_bytes(), categories)
