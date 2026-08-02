"""Shared exact-field editor for mapped Python-2 game database records."""

from __future__ import annotations

import math
from pathlib import Path
import struct

from nascar_modding.core.modules import load_module
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.formats.python2_pyc import root_constants, root_layout
from nascar_modding.games.installation import GameInstallation


DB_GAME = 'DB_GAME_LOCAL_SCRIPT.PYC'
DB_AI = 'DB_AICONFIG_SCRIPT.PYC'
AI_TRACK_FIELDS = (
    'FormationOffsetDirection', 'TurnOneBearsLeft', 'UsePenaltySystem',
    'BumpDraftingEnabled', 'BumpDraftingConsiderGearing', 'PABBMaxInflationZ',
    'BumpDraftingMaxPackSize', 'BumpDraftingRoadStraightness',
    'CatchupWantSpeedModifierEasy', 'CatchupWantSpeedModifierHard',
    'UseDrivingControllerSmoothing', 'PacecarIgnorePitEntryTripwire',
    'HasDoubleYellowLine', 'BumpdraftPlayerRoadStraightness',
    'PitStrategy100PitChance', 'PitStrategy100FuelOnlyChance',
    'PitStrategy100TwoTyresChance', 'PitStrategy100FourTyresChance',
    'PitStrategy75PitChance', 'PitStrategy75FuelOnlyChance',
    'PitStrategy75TwoTyresChance', 'PitStrategy75FourTyresChance',
    'PitStrategy50PitChance', 'PitStrategy50FuelOnlyChance',
    'PitStrategy50TwoTyresChance', 'PitStrategy50FourTyresChance',
    'PitStrategy25PitChance', 'PitStrategy25FuelOnlyChance',
    'PitStrategy25TwoTyresChance', 'PitStrategy25FourTyresChance',
    'PitStrategy0FuelOnlyChance', 'PitStrategy0TwoTyresChance',
    'PitStrategy0FourTyresChance', 'PitStrategy0PitChance',
    'CanSwitchFromStagnantRacingLine', 'StayBehindRegionScaled',
    'StateMachineWeightingOvertake', 'StateMachineWeightingBumpDraft',
    'StayAlongsideGap', 'StayBehindRegion', 'ThrottleLiftToleranceDeg',
    'CatchupPowModifier',
)
AI_GLOBAL_FIELDS = (
    'OutbrakingEffort', 'AggressionVariation', 'DesireForRacingLine',
    'AggressionRivalModifier', 'AggressionTeamModifier',
    'PitstopStrategyGreenWindowPercentage', 'PitstopStrategyGreenWindowLapReserve',
)
WORLD_PACE_FIELDS = (
    'PracticeEasyBestTime', 'PracticeEasyWorstTime', 'PracticeHardBestTime',
    'PracticeHardWorstTime', 'QualifyBaseTimeModifier', 'QualRecSpeed',
    'RaceRecSpeed', 'TempAirC', 'TempTrackC', 'TempAirCEnd', 'TempTrackCEnd',
)
WORKFLOWS = {
    'race_laps': (DB_GAME, 'RACEDATA_c', ('RaceLaps',)),
    'ai_track': (DB_AI, 'AIRACINGTRACKCONFIG_c', AI_TRACK_FIELDS),
    'ai_global': (DB_AI, 'AIRACINGGLOBALCONFIG_c', AI_GLOBAL_FIELDS),
    'world_pace': (DB_GAME, 'WORLDSCRIPT_c', WORLD_PACE_FIELDS),
}

_MAPPER = None


def _mapper_module():
    global _MAPPER
    if _MAPPER is not None:
        return _MAPPER
    path = Path(__file__).resolve().parents[2] / 'internal_tools' / 'nascar15_pyc_record_mapper_v5_teams.py'
    module = load_module(
        path, 'nascar_modding_shared_pyc_mapper',
        missing_message=f'PYC mapper is missing: {path}',
        load_message=f'could not load PYC mapper: {path}',
    )
    _MAPPER = module
    return module


def _ops(code: bytes):
    index, extended = 0, 0
    while index < len(code):
        offset, opcode = index, code[index]
        index += 1
        argument = argument_offset = None
        if opcode >= 90:
            if index + 2 > len(code):
                break
            raw = code[index] | code[index + 1] << 8
            argument, argument_offset = raw | extended, index
            index += 2
            if opcode == 143:
                extended = raw << 16
                continue
            extended = 0
        yield offset, opcode, argument, argument_offset


def _same(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, int) and not isinstance(left, bool):
        return isinstance(right, int) and not isinstance(right, bool) and left == right
    if isinstance(left, float):
        return isinstance(right, (int, float)) and not isinstance(right, bool) and abs(left - float(right)) < 1e-12
    return left == right


def _coerce(old, value):
    if isinstance(old, bool):
        if isinstance(value, bool):
            return value
        folded = str(value).strip().casefold()
        if folded in ('true', '1', 'yes', 'on'):
            return True
        if folded in ('false', '0', 'no', 'off'):
            return False
        raise ValueError('boolean fields accept True or False')
    if isinstance(old, int) and not isinstance(old, bool):
        number = float(value)
        if not math.isfinite(number) or abs(number - round(number)) > 1e-9:
            raise ValueError('this field requires a whole number')
        number = int(round(number))
        if not -(2**63) <= number < 2**63:
            raise ValueError('integer is outside the supported range')
        return number
    if isinstance(old, float):
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1_000_000_000:
            raise ValueError('floating-point value is outside the supported range')
        return number
    raise ValueError('field is nested or non-scalar and remains read-only')


def _marshal_scalar(value, old) -> bytes:
    if isinstance(value, bool):
        return b'T' if value else b'F'
    if isinstance(old, int) and not isinstance(old, bool):
        if -(2**31) <= value < 2**31:
            return b'i' + struct.pack('<i', value)
        return b'I' + struct.pack('<q', value)
    return b'g' + struct.pack('<d', float(value))


def _mapped(payload: bytes, class_name: str, fields: tuple[str, ...]):
    mapper = _mapper_module()
    root = mapper.parse_pyc(payload)
    schemas = mapper.build_schemas(root)
    records = mapper.map_records(root, schemas)
    rows = []
    for record in records:
        if record.class_name != class_name:
            continue
        row = {'uid': mapper.value_plain_for_compare(record.uid)}
        for field in fields:
            row[field] = mapper.value_plain_for_compare(record.fields.get(field)) if field in record.fields else None
        rows.append(row)
    return rows, root, records, schemas


def _diff(before: list[dict], after: list[dict], fields: tuple[str, ...]):
    indexed = {str(row.get('uid')): row for row in before}
    changes = []
    for row in after:
        old = indexed.get(str(row.get('uid')))
        if old is None:
            continue
        for field in fields:
            if not _same(old.get(field), row.get(field)):
                changes.append((str(row.get('uid')), field, old.get(field), row.get(field)))
    return changes


def _root_index(root, value):
    mapper = _mapper_module()
    for index, candidate in enumerate(root.value.consts):
        if candidate is value:
            return index
        if isinstance(value, mapper.MVal) and isinstance(candidate, mapper.MVal) and candidate.tag_offset == value.tag_offset:
            return index
    return None


def _existing_index(root, value):
    mapper = _mapper_module()
    for index, candidate in enumerate(root.value.consts):
        if _same(mapper.value_plain_for_compare(candidate), value):
            return index
    return None


def _patch_operand(payload: bytes, operand_offset: int, existing_index: int | None, new, old) -> bytes:
    output = bytearray(payload)
    if existing_index is not None:
        if existing_index > 0xFFFF:
            raise ValueError('constant index requires EXTENDED_ARG')
        struct.pack_into('<H', output, operand_offset, existing_index)
        return bytes(output)
    layout = root_layout(payload)
    new_index = layout['count']
    if new_index > 0xFFFF:
        raise ValueError('constant table has reached the LOAD_CONST limit')
    struct.pack_into('<H', output, operand_offset, new_index)
    struct.pack_into('<i', output, layout['count_pos'], new_index + 1)
    output[layout['const_end']:layout['const_end']] = _marshal_scalar(new, old)
    values = root_constants(bytes(output))
    if len(values) != new_index + 1 or not _same(values[new_index], new):
        raise ValueError('rebuilt PYC constant read-back failed')
    return bytes(output)


def _exact_variant(payload: bytes, class_name: str, uid, field: str, value, fields: tuple[str, ...]):
    mapper = _mapper_module()
    before, root, records, _schemas = _mapped(payload, class_name, fields)
    record = mapper.find_record_for_patch(records, class_name, uid)
    if record is None or field not in record.fields:
        raise ValueError(f'UID {uid} / {field} was not found')
    raw_old = record.fields[field]
    old = mapper.value_plain_for_compare(raw_old)
    new = _coerce(old, value)
    if _same(old, new):
        raise ValueError('value already matches the live field')
    root_code, layout = root.value, root_layout(payload)
    old_indices = []
    if isinstance(raw_old, mapper.MVal):
        index = _root_index(root, raw_old)
        if index is not None:
            old_indices.append(index)
    if not old_indices:
        old_indices = [
            index for index, candidate in enumerate(root_code.consts)
            if _same(mapper.value_plain_for_compare(candidate), old)
        ]
    candidates = []
    for code_offset, opcode, argument, argument_offset in _ops(root_code.code_bytes):
        if opcode == 100 and argument in old_indices and code_offset < record.call_offset:
            candidates.append(('const', layout['code_off'] + argument_offset, code_offset))
    if isinstance(old, bool):
        old_name, new_name = ('True' if old else 'False'), ('True' if new else 'False')
        if old_name in root_code.names and new_name in root_code.names:
            old_index, new_index = root_code.names.index(old_name), root_code.names.index(new_name)
            for code_offset, opcode, argument, argument_offset in _ops(root_code.code_bytes):
                if opcode in (101, 116) and argument == old_index and code_offset < record.call_offset:
                    candidates.append(('name', layout['code_off'] + argument_offset, code_offset, new_index))
    if not candidates:
        raise ValueError('could not locate an isolated bytecode source for this field')
    existing = _existing_index(root, new)
    wanted = {(str(uid), field)}
    for candidate in sorted(candidates, key=lambda item: item[2], reverse=True)[:128]:
        try:
            if candidate[0] == 'name':
                output = bytearray(payload)
                struct.pack_into('<H', output, candidate[1], candidate[3])
                rebuilt = bytes(output)
            else:
                rebuilt = _patch_operand(payload, candidate[1], existing, new, old)
            after, _root, _records, _schemas = _mapped(rebuilt, class_name, fields)
            changes = _diff(before, after, fields)
            if {(record_uid, changed_field) for record_uid, changed_field, _old, _new in changes} == wanted:
                return rebuilt, {'uid': str(uid), 'field': field, 'old': old, 'new': new}
        except (ValueError, IndexError, struct.error):
            continue
    raise ValueError('no candidate bytecode operand produced an exact one-field diff')


def mapped_rows_from_pyc_bytes(payload: bytes, class_name: str, fields):
    return _mapped(payload, class_name, tuple(fields or ()))


scalar_same_type = _same
coerce_scalar_like = _coerce


def patch_load_const_operand(
    payload: bytes, operand_offset: int, existing_index: int,
    new_value=None, old_value=None,
):
    index = existing_index if new_value is None else None
    rebuilt = _patch_operand(payload, operand_offset, index, new_value, old_value)
    if new_value is None:
        return rebuilt, False, existing_index
    return rebuilt, True, root_layout(rebuilt)['count'] - 1


def exact_field_variant(payload: bytes, class_name: str, uid, field: str, value, fields=None):
    try:
        rebuilt, change = _exact_variant(
            payload, class_name, uid, field, value,
            tuple(fields or (field,)),
        )
        return {
            'handled': True, 'ok': True, 'pyc': rebuilt,
            'old': change['old'], 'new': change['new'],
            'method': 'isolated_operand_repoint',
        }
    except ValueError as exc:
        return {'handled': True, 'ok': False, 'error': str(exc)}


class PycRecordEditor:
    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.resources = ResourceEditor(installation)

    def workflow(self, workflow: str):
        try:
            return WORKFLOWS[workflow]
        except KeyError as exc:
            raise ValueError(f'unknown PYC workflow: {workflow}') from exc

    def _payload(self, filename: str, *, pristine: bool = False):
        archive_key, entry = self.installation.find_entry(filename)
        return archive_key, entry.name, self.resources.read(
            entry.name, archive_key, pristine=pristine,
        )

    def records(self, workflow: str, *, pristine: bool = False) -> list[dict]:
        filename, class_name, fields = self.workflow(workflow)
        return self.records_for(filename, class_name, fields, pristine=pristine)

    def records_for(self, filename: str, class_name: str, fields, *, pristine: bool = False) -> list[dict]:
        _archive, _name, payload = self._payload(filename, pristine=pristine)
        rows, _root, _records, _schemas = _mapped(payload, class_name, tuple(fields or ()))
        return rows

    def preview(self, workflow: str, changes: list[dict]) -> dict:
        filename, class_name, fields = self.workflow(workflow)
        archive_key, entry_name, payload = self._payload(filename)
        if not changes:
            raise ValueError('no PYC record changes supplied')
        if len(changes) > 100:
            raise ValueError('PYC batches are limited to 100 fields')
        seen, rebuilt, summary = set(), payload, []
        for change in changes:
            identity = (str(change['uid']), str(change['field']))
            if identity in seen:
                raise ValueError(f'duplicate PYC target UID {identity[0]} / {identity[1]}')
            if identity[1] not in fields:
                raise ValueError(f'{identity[1]} is not editable in {workflow}')
            seen.add(identity)
            rebuilt, result = _exact_variant(
                rebuilt, class_name, identity[0], identity[1], change.get('value'), fields,
            )
            summary.append(result)
        before, _root, _records, _schemas = _mapped(payload, class_name, fields)
        after, _root, _records, _schemas = _mapped(rebuilt, class_name, fields)
        actual = {(uid, field) for uid, field, _old, _new in _diff(before, after, fields)}
        if actual != seen:
            raise ValueError('final class-wide PYC diff did not exactly match requested fields')
        plan = self.resources.plan(entry_name, archive_key, rebuilt)
        return {
            'ok': True, 'dry_run': True, 'workflow': workflow,
            'archive': archive_key, 'entry': entry_name,
            'affected_count': len(summary), 'changes': summary,
            'method': plan['method'], 'old_size': len(payload), 'new_size': len(rebuilt),
            '_payload': rebuilt,
        }

    def apply(self, workflow: str, changes: list[dict]) -> dict:
        preview = self.preview(workflow, changes)
        payload = preview.pop('_payload')
        original = self.resources.read(preview['entry'], preview['archive'])
        installed = False
        try:
            result = self.resources.replace(preview['entry'], preview['archive'], payload)
            installed = True
            rows = {str(row['uid']): row for row in self.records(workflow)}
            failures = [
                change for change in preview['changes']
                if not _same(rows.get(change['uid'], {}).get(change['field']), change['new'])
            ]
            if failures:
                raise IOError('PYC live read-back failed after install')
        except Exception as install_error:
            if installed:
                try:
                    self.resources.replace(preview['entry'], preview['archive'], original)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'PYC install failed ({install_error}) and rollback failed: {rollback_error}'
                    ) from install_error
            raise
        preview.update(dry_run=False, verified=True, write=result)
        return preview

    def restore(self, workflow: str) -> dict:
        filename, _class_name, _fields = self.workflow(workflow)
        archive_key, entry_name, _payload = self._payload(filename)
        return self.resources.restore(entry_name, archive_key)
