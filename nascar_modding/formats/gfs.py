"""Read-only parser for the recovered Eutechnyx GFS save envelope."""

from __future__ import annotations

from dataclasses import dataclass
import struct


GFS_HEADER_SIZE = 0x14
GFS_MAX_PAYLOAD = 0x400000
_SECOND_SEED = 0x6C93
_MULTIPLIER_A = 0x4C8F
_MULTIPLIER_B = 0x7953

# Ordered scalar transfers at the start of FUN_009765F0.  These forty fields
# are contiguous on the wire before the first dynamic/nested serializer.  The
# object offsets are structural evidence only; no player-facing meanings have
# been inferred for them.
PROFILE_CORE_SCALAR_FIELDS = (
    (0x0000, 4), (0x0004, 4), (0x0008, 4), (0x000C, 4),
    (0x0018, 4), (0x001C, 4), (0x0020, 4), (0x0024, 4),
    (0x21D0, 4), (0x21D4, 4), (0x21D8, 1), (0x21D9, 1),
    (0x21DA, 1), (0x21DC, 1), (0x21DD, 1), (0x21DE, 1),
    (0x21DF, 1), (0x21E0, 1), (0x21DB, 1), (0x21E4, 4),
    (0x21E8, 1), (0x21EC, 4), (0x21F4, 4), (0x21F0, 4),
    (0x21F8, 4), (0x21FC, 4), (0x2200, 4), (0x2214, 4),
    (0x2204, 4), (0x2208, 4), (0x220C, 4), (0x2210, 4),
    (0x2218, 4), (0x221C, 1), (0x2220, 4), (0x2224, 4),
    (0x2228, 4), (0x222C, 4), (0x2230, 4), (0x21E1, 1),
    (0x0028, 4), (0x2238, 1), (0x2239, 1), (0x0010, 4),
    (0x0014, 4), (0x2234, 4),
)

# The first 40 transfers are contiguous on the wire. A dynamic record
# serializer follows them; the final six statically recovered transfers occur
# later, so their wire offsets cannot be inferred merely by summing widths.
PROFILE_CORE_PREFIX_FIELDS = PROFILE_CORE_SCALAR_FIELDS[:40]


class _GfsRng:
    def __init__(self, seed: int):
        self.first = seed & 0xFFFFFFFF
        self.second = _SECOND_SEED

    def step(self) -> None:
        self.first = (((self.first & 0xFFFF) * _MULTIPLIER_A) + (self.first >> 16)) & 0xFFFFFFFF
        self.second = (((self.second & 0xFFFF) * _MULTIPLIER_B) + (self.second >> 16)) & 0xFFFFFFFF

    def modulo(self, limit: int) -> int:
        if not limit:
            return 0
        self.step()
        combined = ((self.second & 0xFFFF) + ((self.first * 0x10000) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return combined % limit

    def zero_random_draw(self) -> None:
        self.step()
        self.step()


def _xor_stream(payload: bytearray, seed: int) -> None:
    rng = _GfsRng(seed)
    for index in range(len(payload)):
        payload[index] ^= rng.modulo(0x100)


def _permute(payload: bytearray, seed: int) -> None:
    if len(payload) < 4:
        return
    rng = _GfsRng(seed)
    used = bytearray(len(payload))
    iterations = (len(payload) & ~3) // 4
    half = len(payload) // 2
    for index in range(iterations):
        source = half + rng.modulo(half)
        destination = index * 2
        while used[source]:
            source = half + rng.modulo(half)
        payload[source], payload[destination] = payload[destination], payload[source]
        used[source] = 1
        used[destination] = 1
        rng.zero_random_draw()


def decrypt_gfs_payload(payload: bytes, seed: int) -> bytes:
    result = bytearray(payload)
    _xor_stream(result, seed)
    _permute(result, seed)
    return bytes(result)


def encrypt_gfs_payload(payload: bytes, seed: int) -> bytes:
    """Expose the inverse for verification/tests; no save writer uses it."""
    result = bytearray(payload)
    _permute(result, seed)
    _xor_stream(result, seed)
    return bytes(result)


@dataclass(frozen=True, slots=True)
class GfsEnvelope:
    payload_size: int
    version: float
    checksum: int
    encrypted: bool
    seed: int
    payload: bytes
    trailing_size: int

    @property
    def section_name(self) -> str | None:
        # Retail PROFILEDATA is written as the exact 11-byte provider name,
        # without a terminator. Clean-room fixtures may retain a NUL separator.
        if self.payload.startswith(b'PROFILEDATA'):
            return 'PROFILEDATA'
        end = self.payload.find(b'\0', 0, 128)
        if end <= 0:
            return None
        raw = self.payload[:end]
        if any(byte < 0x20 or byte > 0x7E for byte in raw):
            return None
        return raw.decode('ascii')

    @property
    def section_data_offset(self) -> int | None:
        name = self.section_name
        if name is None:
            return None
        offset = len(name)
        if name == 'PROFILEDATA':
            return offset
        if offset < len(self.payload) and self.payload[offset] == 0:
            offset += 1
        return offset


def parse_gfs(data: bytes, *, require_zero_padding: bool = True) -> GfsEnvelope:
    if len(data) < GFS_HEADER_SIZE:
        raise ValueError('GFS file is shorter than its 20-byte header')
    payload_size, version_bits, expected_checksum, encrypted_word, seed = struct.unpack_from(
        '<5I', data
    )
    if payload_size > GFS_MAX_PAYLOAD:
        raise ValueError(f'GFS payload exceeds the recovered 4 MiB manager limit: {payload_size}')
    if encrypted_word not in (0, 1):
        raise ValueError(f'GFS encrypted flag is not boolean: {encrypted_word}')
    end = GFS_HEADER_SIZE + payload_size
    if end > len(data):
        raise ValueError(f'GFS payload is truncated: expected {payload_size}, have {len(data) - GFS_HEADER_SIZE}')
    trailing = data[end:]
    if require_zero_padding and any(trailing):
        raise ValueError('GFS fixed-slot padding contains non-zero bytes')
    stored = data[GFS_HEADER_SIZE:end]
    payload = decrypt_gfs_payload(stored, seed) if encrypted_word else stored
    actual_checksum = sum(payload) & 0xFFFFFFFF
    if actual_checksum != expected_checksum:
        raise ValueError(
            f'GFS checksum mismatch: header 0x{expected_checksum:08X}, payload 0x{actual_checksum:08X}'
        )
    version = struct.unpack('<f', struct.pack('<I', version_bits))[0]
    return GfsEnvelope(
        payload_size=payload_size,
        version=version,
        checksum=expected_checksum,
        encrypted=bool(encrypted_word),
        seed=seed,
        payload=payload,
        trailing_size=len(trailing),
    )


def inspect_gfs(data: bytes) -> dict:
    envelope = parse_gfs(data)
    offset = envelope.section_data_offset
    preamble = None
    if offset is not None:
        word_count = min(2, (len(envelope.payload) - offset) // 4)
        if word_count:
            preamble = list(struct.unpack_from(f'<{word_count}I', envelope.payload, offset))
    result = {
        'valid': True,
        'payload_size': envelope.payload_size,
        'version': round(envelope.version, 6),
        'checksum': envelope.checksum,
        'encrypted': envelope.encrypted,
        'seed': envelope.seed,
        'trailing_zero_bytes': envelope.trailing_size,
        'section_name': envelope.section_name,
        'section_data_offset': offset,
        'profile_preamble_words': preamble,
    }
    if envelope.section_name == 'PROFILEDATA' and offset is not None:
        core_offset = offset + 8
        cursor = core_offset
        fields = []
        for object_offset, width in PROFILE_CORE_PREFIX_FIELDS:
            if cursor + width > len(envelope.payload):
                break
            raw = envelope.payload[cursor:cursor + width]
            fields.append({
                'object_offset': object_offset,
                'wire_offset': cursor,
                'width': width,
                'value': int.from_bytes(raw, 'little'),
            })
            cursor += width
        result.update(
            profile_core_offset=core_offset,
            profile_core_prefix_bytes=cursor - core_offset,
            profile_core_prefix_fields=fields,
            profile_core_prefix_complete=len(fields) == len(PROFILE_CORE_PREFIX_FIELDS),
            profile_core_scalar_contract_count=len(PROFILE_CORE_SCALAR_FIELDS),
            profile_core_scalar_values_recovered=len(fields),
            profile_core_deferred_fields=[
                {
                    'object_offset': object_offset,
                    'width': width,
                    'wire_offset': None,
                    'reason': 'occurs after the unresolved dynamic record serializer',
                }
                for object_offset, width in PROFILE_CORE_SCALAR_FIELDS[len(fields):]
            ],
            retail_profile_writer_ready=False,
        )
    return result


def compare_gfs(before_data: bytes, after_data: bytes, *, max_ranges: int = 256) -> dict:
    """Compare decrypted retail payloads without assigning unknown semantics."""
    before = parse_gfs(before_data)
    after = parse_gfs(after_data)
    before_report = inspect_gfs(before_data)
    after_report = inspect_gfs(after_data)
    maximum = max(len(before.payload), len(after.payload))
    ranges = []
    changed_bytes = 0
    changed_by_region = {}
    start = None

    def region(report: dict, offset: int) -> str:
        section = report.get('section_data_offset')
        core = report.get('profile_core_offset')
        prefix = report.get('profile_core_prefix_bytes')
        if section is None or offset < section:
            return 'provider_header'
        if core is None or offset < core:
            return 'profile_preamble'
        if prefix is not None and offset < core + prefix:
            return 'proven_scalar_prefix'
        return 'opaque_dynamic_payload'

    for offset in range(maximum):
        left = before.payload[offset] if offset < len(before.payload) else None
        right = after.payload[offset] if offset < len(after.payload) else None
        different = left != right
        if different:
            changed_bytes += 1
            left_region = region(before_report, offset)
            right_region = region(after_report, offset)
            region_name = left_region if left_region == right_region else 'layout_boundary_mismatch'
            changed_by_region[region_name] = changed_by_region.get(region_name, 0) + 1
            if start is None:
                start = offset
        elif start is not None:
            if len(ranges) < max_ranges:
                ranges.append({'payload_offset': start, 'length': offset - start})
            start = None
    if start is not None and len(ranges) < max_ranges:
        ranges.append({'payload_offset': start, 'length': maximum - start})

    before_fields = {
        int(row['object_offset']): row
        for row in before_report.get('profile_core_prefix_fields', [])
    }
    after_fields = {
        int(row['object_offset']): row
        for row in after_report.get('profile_core_prefix_fields', [])
    }
    scalar_changes = []
    for object_offset in sorted(before_fields.keys() & after_fields.keys()):
        left, right = before_fields[object_offset], after_fields[object_offset]
        if left['value'] != right['value']:
            scalar_changes.append({
                'object_offset': object_offset,
                'wire_offset': left['wire_offset'],
                'width': left['width'],
                'before': left['value'],
                'after': right['value'],
            })
    section_offset = before.section_data_offset
    for row in ranges:
        row['section_relative_offset'] = (
            row['payload_offset'] - section_offset if section_offset is not None else None
        )
        row['regions'] = sorted({
            region(before_report, offset)
            for offset in range(row['payload_offset'], row['payload_offset'] + row['length'])
        })
    envelope_changes = {}
    for name, left, right in (
        ('payload_size', before.payload_size, after.payload_size),
        ('version', before.version, after.version),
        ('checksum', before.checksum, after.checksum),
        ('encrypted', before.encrypted, after.encrypted),
        ('seed', before.seed, after.seed),
        ('trailing_size', before.trailing_size, after.trailing_size),
    ):
        if left != right:
            envelope_changes[name] = {'before': left, 'after': right}
    return {
        'valid': True,
        'same_section': before.section_name == after.section_name,
        'before_section': before.section_name,
        'after_section': after.section_name,
        'before_payload_size': len(before.payload),
        'after_payload_size': len(after.payload),
        'payload_size_delta': len(after.payload) - len(before.payload),
        'changed_bytes': changed_bytes,
        'changed_bytes_by_region': changed_by_region,
        'changed_ranges': ranges,
        'changed_ranges_truncated': len(ranges) >= max_ranges and changed_bytes > sum(
            row['length'] for row in ranges
        ),
        'profile_preamble_before': before_report.get('profile_preamble_words'),
        'profile_preamble_after': after_report.get('profile_preamble_words'),
        'known_prefix_scalar_changes': scalar_changes,
        'envelope_changes': envelope_changes,
        'deferred_scalar_values_compared': False,
        'retail_profile_writer_ready': False,
    }
