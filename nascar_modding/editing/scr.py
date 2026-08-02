"""Shared, transaction-safe editor for NUL-tokenized SCR physics containers."""

from __future__ import annotations

from collections import defaultdict
import math
import re
import struct

from nascar_modding.editing.archive import ArchiveEntryEditor
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.games.installation import GameInstallation


NUMBER_RE = re.compile(r'^-?\d+(?:\.\d+)?$')
KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{1,95}$')
DRAFT_KEYS = {
    'FRONT-DRAFT-DRAG', 'REAR-DRAFT-DRAG', 'SIDE-DRAFT-DRAG',
    'FRONT-DRAFT-DOWNFORCE', 'REAR-DRAFT-DOWNFORCE',
    'OVERALL-DOWNFORCE-SCALE',
}
RECOMMENDED_KEYS = DRAFT_KEYS | {
    'TAPE', 'SPLITTER', 'AI-LAT-GRIP-BOOST', 'MAXLATFRICTION',
    'MAXLONGFRICTION', 'OPTSLIPANGLE', 'OPTSLIPRATIO',
    'TREADWEAR-GRADE', 'TREADWEAR-DATA', 'PRESSURE',
    'SPRING-STIFFNESS', 'DAMPING-RATIO', 'REBOUND-DAMPING-RATIO',
    'ROLL-CENTRE-HEIGHT', 'STIFFNESS', 'BUMP-STOP-STRENGTH', 'CAMBER',
    'TOE', 'JOUNCE-LIMIT', 'BRAKE-BIAS', 'MAX-TORQUE', 'FADE',
    'BRAKE-DIAMETER', 'BRAKE-THICKNESS', 'BRAKE-MATERIAL', 'FINAL-RATIO',
    'GEARS', 'EFFICIENCY', 'RPM-TORQUE', 'CHANGE-UP-POINT',
    'CHANGE-DOWN-POINT', 'LIMITER-RANGE', 'MAX-TORQUE-CAPACITY',
    'MAX-STEERING-ANGLE', 'ACKERMANN',
}
CATEGORY_ORDER = (
    'Draft / Aero', 'Grip / Tires', 'Suspension', 'Brakes',
    'Engine / Gearing', 'Steering', 'AI Behavior', 'Camera / Visual', 'Other',
)


def _clean_ascii(raw: bytes) -> str:
    if not raw:
        return ''
    value = raw.decode('latin1', 'ignore')
    value = re.sub(r'^[^\x20-\x7E]+', '', value)
    value = re.sub(r'[^\x20-\x7E]+$', '', value)
    if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
        return ''
    return value


def parse_numeric_rows(data: bytes) -> list[dict]:
    """Parse all named numeric tokens, preserving duplicate occurrences."""
    tokens, position = [], 0
    for raw in data.split(b'\0'):
        tokens.append((position, _clean_ascii(raw)))
        position += len(raw) + 1

    stack, previous, counts, rows = [], '', {}, []
    for index, (key_offset, token) in enumerate(tokens):
        if token == '{':
            if previous and previous not in ('{', '}') and not NUMBER_RE.fullmatch(previous):
                stack.append(previous)
            previous = ''
            continue
        if token == '}':
            if stack:
                stack.pop()
            previous = ''
            continue
        if not token:
            continue
        if index + 1 < len(tokens):
            value_offset, value = tokens[index + 1]
            if KEY_RE.fullmatch(token) and NUMBER_RE.fullmatch(value):
                folded = token.upper()
                occurrence = counts.get(folded, 0)
                counts[folded] = occurrence + 1
                rows.append({
                    'key': token, 'value': value, 'length': len(value),
                    'occurrence': occurrence, 'key_rel': key_offset,
                    'value_rel': value_offset, 'path': '/'.join(stack),
                })
        previous = token
    return rows


def _role(name: str) -> str | None:
    upper = name.upper()
    if upper.startswith('PACECAR'):
        return None
    if upper.endswith('PLAYER_SCR.ARC'):
        return 'player'
    if upper.endswith('AI_SCR.ARC'):
        return 'ai'
    return None


def _track(name: str) -> str:
    upper = name.upper().replace('_SCR.ARC', '')
    if upper.startswith('NASCAR'):
        upper = upper[6:]
    for suffix in ('PLAYER', 'AI'):
        if upper.endswith(suffix):
            upper = upper[:-len(suffix)]
    return upper.title() or name


def _wheel(path: str) -> str:
    upper = path.upper().replace('_', '-')
    for raw, label in (
        ('FRONT-RIGHT', 'Front Right'), ('FRONT-LEFT', 'Front Left'),
        ('REAR-RIGHT', 'Rear Right'), ('REAR-LEFT', 'Rear Left'),
    ):
        if raw in upper:
            return label
    return ''


def _context(path: str) -> str:
    upper = path.upper()
    wheel = _wheel(path)
    if wheel:
        return wheel
    if 'HANDBRAKE' in upper:
        return 'Handbrake'
    for raw, label in (
        ('AERODYNAMICS', 'Aerodynamics'), ('ANTI-ROLL-BAR', 'Anti-roll bar'),
        ('SUSPENSION', 'Suspension'), ('BRAKES', 'Brakes'),
        ('GEARBOX', 'Gearbox'), ('ENGINE', 'Engine'),
        ('DRIVETRAIN', 'Drivetrain'), ('STEERING', 'Steering'),
        ('GSCHASSIS', 'AI chassis'), ('CHASSIS', 'Chassis'),
        ('MOVER', 'Vehicle mover'),
    ):
        if raw in upper:
            return label
    parts = [part for part in path.split('/') if part and part not in (
        'VEHICLE', '!VEHICLE', 'GSRACECAR', 'DATA',
    )]
    return parts[-1].replace('-', ' ').title() if parts else 'General'


def _category(key: str, path: str) -> str:
    key, path = key.upper(), path.upper()
    if key in DRAFT_KEYS or 'AERODYNAMICS' in path or any(
        part in key for part in ('DRAFT', 'DOWNFORCE', 'DRAG-CDA', 'SPLITTER', 'TAPE')
    ):
        return 'Draft / Aero'
    if any(part in key for part in ('GRIP', 'FRICTION', 'SLIP', 'TREADWEAR', 'PRESSURE', 'TYRE', 'TIRE')) or '/TYRE' in path:
        return 'Grip / Tires'
    if any(part in key for part in ('SPRING', 'DAMPING', 'CAMBER', 'TOE', 'JOUNCE', 'ROLL-CENTRE', 'BUMP-STOP')) or any(part in path for part in ('SUSPENSION', 'ANTI-ROLL-BAR')):
        return 'Suspension'
    if any(part in key for part in ('BRAKE', 'FADE')) or 'BRAKES' in path or 'HANDBRAKE' in path:
        return 'Brakes'
    if any(part in key for part in ('GEAR', 'RATIO', 'RPM', 'TORQUE', 'LIMITER', 'CLUTCH', 'DIFF', 'EFFICIENCY', 'FUEL')) or any(part in path for part in ('ENGINE', 'GEARBOX', 'DRIVETRAIN')):
        return 'Engine / Gearing'
    if any(part in key for part in ('STEER', 'ACKERMANN')) or 'STEERING' in path:
        return 'Steering'
    if key.startswith(('AI-', 'AI_')) or 'AI-' in key:
        return 'AI Behavior'
    if any(part in key for part in ('VIEW', 'CAMERA', 'TILT', 'FOV', 'LOD', 'LIGHT', 'SHADOW', 'SOUND')):
        return 'Camera / Visual'
    return 'Other'


# Public classification helpers keep legacy adapters and native UI code on the
# same parser vocabulary without copying the rules into either frontend.
scr_role = _role
scr_track = _track
scr_wheel = _wheel
scr_context = _context
scr_category = _category


def _pack_type_size(record_type: int, size: int) -> int:
    if not 0 <= int(size) < 0x1000000:
        raise ValueError('SCR ARCC record exceeds its 24-bit size field')
    return int.from_bytes(bytes((int(record_type) & 0xFF,)) + int(size).to_bytes(3, 'big'), 'little')


def _arcc_records(raw: bytes) -> tuple[int, int, list[dict]]:
    if raw[:4] != b'ARCC' or len(raw) < 0x80:
        raise ValueError('SCR entry is not an ARCC container')
    count = struct.unpack_from('<I', raw, 4)[0]
    base = 0x80 + count * 16
    if count <= 0 or count > 4096 or base > len(raw):
        raise ValueError('invalid SCR ARCC record table')
    records, maximum = [], base
    for index in range(count):
        key, offset, name_ref, packed = struct.unpack_from('<4I', raw, 0x80 + index * 16)
        encoded = packed.to_bytes(4, 'little')
        record_type, size = encoded[0], int.from_bytes(encoded[1:4], 'big')
        absolute = base + offset
        if absolute < base or absolute + size > len(raw):
            raise ValueError(f'SCR ARCC record {index} exceeds the file')
        records.append({
            'index': index, 'key': key, 'name_ref': name_ref,
            'record_type': record_type, 'absolute': absolute,
            'payload': bytes(raw[absolute:absolute + size]),
        })
        maximum = max(maximum, absolute + size)
    if any(raw[maximum:]):
        raise ValueError('SCR ARCC has an unknown non-zero tail; rebuild refused')
    return count, base, records


def rebuild_arcc(raw: bytes, replacements: list[tuple[int, int, bytes, str]]) -> bytes:
    """Rebuild selected byte ranges while preserving all untouched records."""
    count, base, records = _arcc_records(raw)
    by_record: dict[int, list[tuple[int, int, bytes, str]]] = defaultdict(list)
    for absolute, old_length, new_bytes, label in replacements:
        hit = next((record for record in records if record['absolute'] <= absolute and absolute + old_length <= record['absolute'] + len(record['payload'])), None)
        if hit is None:
            raise ValueError(f'{label}: value range is outside every SCR record')
        by_record[hit['index']].append((absolute - hit['absolute'], old_length, bytes(new_bytes), label))

    payloads = []
    for record in records:
        output, cursor = bytearray(), 0
        for relative, old_length, new_bytes, label in sorted(by_record.get(record['index'], [])):
            if relative < cursor or relative + old_length > len(record['payload']):
                raise ValueError(f'{label}: overlapping or invalid SCR edit')
            output += record['payload'][cursor:relative]
            output += new_bytes
            cursor = relative + old_length
        output += record['payload'][cursor:]
        payloads.append(bytes(output))

    table, data, cursor = bytearray(count * 16), bytearray(), 0
    for record, payload in zip(records, payloads):
        absolute = (base + cursor + 15) & ~15
        data += bytes(absolute - (base + cursor))
        cursor = absolute - base
        struct.pack_into(
            '<4I', table, record['index'] * 16, record['key'], cursor,
            record['name_ref'], _pack_type_size(record['record_type'], len(payload)),
        )
        data += payload
        cursor += len(payload)
    rebuilt = bytes(raw[:0x80] + table + data)
    _, _, check = _arcc_records(rebuilt)
    changed = set(by_record)
    for old, new in zip(records, check):
        if (old['key'], old['name_ref'], old['record_type']) != (new['key'], new['name_ref'], new['record_type']):
            raise ValueError('SCR rebuild changed record identity')
        if old['index'] not in changed and old['payload'] != new['payload']:
            raise ValueError(f"SCR rebuild changed untouched record {old['index']}")
    return rebuilt


def _value_map(payload: bytes) -> dict[tuple[str, int], str]:
    return {(row['key'].upper(), row['occurrence']): row['value'] for row in parse_numeric_rows(payload)}


class ScrEditor:
    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.archive_editor = ArchiveEntryEditor(installation)
        self.resources = ResourceEditor(installation)

    def inventory(
        self, *, track: str = '', role: str = 'all', query: str = '',
        recommended_only: bool = False, include_stock: bool = False,
    ) -> list[dict]:
        track_filter, role_filter, query_filter = track.casefold(), role.casefold(), query.casefold()
        rows, stock_cache = [], {}
        for archive_key in self.installation.archive_pairs:
            for entry in self.installation.entries(archive_key):
                entry_role = _role(entry.name)
                if not entry_role or not entry.name.upper().endswith('_SCR.ARC'):
                    continue
                entry_track = _track(entry.name)
                if track_filter and entry_track.casefold() != track_filter:
                    continue
                if role_filter not in ('', 'all') and entry_role != role_filter:
                    continue
                payload = self.installation.read_entry(entry.name, archive_key)
                if b'AERODYNAMICS' not in payload:
                    continue
                stock = None
                if include_stock:
                    try:
                        pristine = self.resources.read(entry.name, archive_key, pristine=True)
                        stock = _value_map(pristine)
                    except (FileNotFoundError, KeyError, ValueError, IOError):
                        stock = None
                    stock_cache[(archive_key, entry.name)] = stock
                for raw in parse_numeric_rows(payload):
                    key_upper, path = raw['key'].upper(), raw['path']
                    recommended = key_upper in RECOMMENDED_KEYS
                    category, context = _category(raw['key'], path), _context(path)
                    searchable = ' '.join((entry_track, entry_role, raw['key'], path, context, category, raw['value'])).casefold()
                    if recommended_only and not recommended:
                        continue
                    if query_filter and query_filter not in searchable:
                        continue
                    identity = (key_upper, raw['occurrence'])
                    stock_value = stock.get(identity) if stock else None
                    rows.append({
                        'id': f'{archive_key}|{entry.name.upper()}|{key_upper}|{raw["occurrence"]}',
                        'archive': archive_key, 'arc': archive_key,
                        'name': entry.name, 'track': entry_track,
                        'role': entry_role, 'key': raw['key'], 'value': raw['value'],
                        'length': raw['length'], 'occurrence': raw['occurrence'],
                        'path': path, 'context': context, 'wheel': _wheel(path),
                        'category': category, 'recommended': recommended,
                        'status': 'tested' if key_upper == 'FRONT-DRAFT-DRAG' else ('existing' if key_upper in DRAFT_KEYS else ('candidate' if recommended else 'raw')),
                        'stock': stock_value, 'modded': stock_value is not None and stock_value != raw['value'],
                    })
        order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
        return sorted(rows, key=lambda row: (
            row['track'], order.get(row['category'], 99), row['context'],
            row['key'], row['occurrence'], 0 if row['role'] == 'player' else 1,
        ))

    def _prepare(self, changes: list[dict]) -> tuple[str, dict[str, bytes], list[dict]]:
        if not changes:
            raise ValueError('no pending SCR changes')
        if len(changes) > 2500:
            raise ValueError('SCR batches are limited to 2500 values')
        inventory = self.inventory()
        indexed = {(row['archive'], row['name'].upper(), row['key'].upper(), row['occurrence']): row for row in inventory}
        requested, seen, archive_keys = [], set(), set()
        for item in changes:
            identity = (
                str(item.get('archive', item.get('arc', '0'))), str(item['name']).upper(),
                str(item['key']).upper(), int(item.get('occurrence', 0)),
            )
            if identity in seen:
                raise ValueError(f'duplicate SCR target: {identity[1]}/{identity[2]}#{identity[3]}')
            seen.add(identity)
            row = indexed.get(identity)
            if row is None:
                raise ValueError(f'SCR target not found: {identity[1]}/{identity[2]}#{identity[3]}')
            value = str(item.get('value', '')).strip()
            if not NUMBER_RE.fullmatch(value):
                raise ValueError(f'{row["key"]}: value must be a plain finite number')
            if len(value) > 32 or not math.isfinite(float(value)) or abs(float(value)) > 1_000_000_000:
                raise ValueError(f'{row["key"]}: value is outside the supported range')
            if value != row['value']:
                requested.append((identity, row, value))
                archive_keys.add(row['archive'])
        if not requested:
            raise ValueError('all pending SCR values already match the game')
        if len(archive_keys) != 1:
            raise ValueError('one atomic SCR batch may target only one archive')

        grouped = defaultdict(list)
        for item in requested:
            grouped[item[1]['name']].append(item)
        replacements, summary = {}, []
        for name, items in grouped.items():
            archive_key = items[0][1]['archive']
            current = self.installation.read_entry(name, archive_key)
            before_map = _value_map(current)
            parsed = {(row['key'].upper(), row['occurrence']): row for row in parse_numeric_rows(current)}
            edits, intended = [], set()
            for _identity, row, value in items:
                key = (row['key'].upper(), row['occurrence'])
                live = parsed.get(key)
                if live is None or live['value'] != row['value']:
                    raise ValueError(f'{row["key"]}: live SCR changed since inventory; reload the editor')
                edits.append((live['value_rel'], live['length'], value.encode('ascii'), f'{name}/{row["key"]}#{row["occurrence"]}'))
                intended.add(key)
                summary.append({
                    'id': row['id'], 'track': row['track'], 'role': row['role'],
                    'key': row['key'], 'occurrence': row['occurrence'],
                    'context': row['context'], 'old': row['value'], 'new': value,
                    'old_width': row['length'], 'new_width': len(value),
                })
            if all(old_length == len(new_bytes) for _offset, old_length, new_bytes, _label in edits):
                rebuilt = bytearray(current)
                for offset, old_length, new_bytes, _label in edits:
                    rebuilt[offset:offset + old_length] = new_bytes
                rebuilt = bytes(rebuilt)
            else:
                rebuilt = rebuild_arcc(current, edits)
            after_map = _value_map(rebuilt)
            changed = {key for key in set(before_map) | set(after_map) if before_map.get(key) != after_map.get(key)}
            if changed != intended:
                raise ValueError(f'{name}: full SCR diff guard rejected collateral changes')
            replacements[name] = rebuilt
        return next(iter(archive_keys)), replacements, summary

    def preview(self, changes: list[dict]) -> dict:
        archive_key, replacements, summary = self._prepare(changes)
        plans = [self.resources.plan(name, archive_key, payload) for name, payload in replacements.items()]
        return {
            'ok': True, 'dry_run': True, 'archive': archive_key,
            'affected_count': len(summary), 'track_count': len({row['track'] for row in summary}),
            'file_count': len(replacements), 'changes': summary, 'files': plans,
            'method': 'append_repoint' if any(plan['method'] == 'append_repoint' for plan in plans) else 'in_place',
        }

    def apply(self, changes: list[dict]) -> dict:
        archive_key, replacements, summary = self._prepare(changes)
        originals = {
            name: self.installation.read_entry(name, archive_key)
            for name in replacements
        }
        installed = False
        try:
            write = self.archive_editor.replace_entries(replacements, archive_key)
            installed = True
            live = {(row['archive'], row['name'].upper(), row['key'].upper(), row['occurrence']): row['value'] for row in self.inventory()}
            failures = []
            for row in summary:
                identity = (archive_key, row['id'].split('|')[1], row['key'].upper(), row['occurrence'])
                if live.get(identity) != row['new']:
                    failures.append(row['id'])
            if failures:
                raise IOError('SCR batch read-back failed: ' + ', '.join(failures[:10]))
        except Exception as install_error:
            if installed:
                try:
                    self.archive_editor.replace_entries(originals, archive_key)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'SCR install failed ({install_error}) and rollback failed: {rollback_error}'
                    ) from install_error
            raise
        return {
            'ok': True, 'verified': True, 'archive': archive_key,
            'affected_count': len(summary), 'track_count': len({row['track'] for row in summary}),
            'file_count': len(replacements), 'changes': summary, 'write': write,
        }

    def restore_entry(self, name: str, archive_key: str) -> dict:
        return self.resources.restore(name, archive_key)
