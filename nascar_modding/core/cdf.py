"""Reader for Eutechnyx ``cdfiles*.dat`` archive indexes.

The three supported games use the same ``filC`` container with two observed
record-table layouts.  Some DLC indexes contain a final sentinel record, so
layout detection must score actual payload rows instead of requiring every
declared row to resolve to a filename.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct


FILC_MAGIC = 0x436C6966
_LAYOUTS = (
    (0x40, 'A', 1, 2, 5),
    (0x50, 'B', 3, 4, 7),
)


@dataclass(frozen=True, slots=True)
class CdfEntry:
    index: int
    name: str
    size: int
    archive_offset: int
    record_offset: int
    layout: str


def _read_name(data: bytes, string_base: int, string_size: int, offset: int) -> str:
    if not 0 <= offset < string_size:
        return ''
    start = string_base + offset
    end = data.find(b'\0', start, string_base + string_size)
    if end <= start:
        return ''
    raw = data[start:end]
    if not raw or any(byte < 32 or byte >= 127 for byte in raw):
        return ''
    return raw.decode('ascii')


def parse_cdf_bytes(data: bytes) -> list[CdfEntry]:
    if len(data) < 0x40:
        raise ValueError('cdfiles index is shorter than its header')
    header = struct.unpack_from('<12I', data, 0)
    if header[0] != FILC_MAGIC:
        raise ValueError('cdfiles index does not start with filC')

    count = int(header[8])
    string_size = int(header[10])
    string_base = len(data) - string_size
    if count < 0 or count > 1_000_000:
        raise ValueError(f'implausible cdfiles record count: {count}')
    if string_size <= 0 or string_base < 0x40:
        raise ValueError('invalid cdfiles string table')

    candidates: list[tuple[int, int, list[CdfEntry]]] = []
    for table_start, layout, name_field, size_field, offset_field in _LAYOUTS:
        entries: list[CdfEntry] = []
        structurally_valid = 0
        for index in range(count):
            record_offset = table_start + index * 32
            if record_offset + 32 > string_base:
                break
            fields = struct.unpack_from('<8I', data, record_offset)
            name = _read_name(data, string_base, string_size, int(fields[name_field]))
            size = int(fields[size_field])
            archive_offset = int(fields[offset_field])

            # A terminal row uses zero size and/or UINT32_MAX offset. It is
            # part of the declared table but not an archive payload.
            if size == 0 or archive_offset == 0xFFFFFFFF:
                continue
            if not name:
                continue
            structurally_valid += 1
            entries.append(
                CdfEntry(
                    index=index,
                    name=name,
                    size=size,
                    archive_offset=archive_offset,
                    record_offset=record_offset,
                    layout=layout,
                )
            )

        # Prefer the layout that resolves the most real rows; use total
        # payload coverage as a stable tie-breaker.
        candidates.append(
            (structurally_valid, sum(entry.size for entry in entries), entries)
        )

    score, _coverage, entries = max(candidates, key=lambda item: (item[0], item[1]))
    expected_payload_rows = max(0, count - 1)
    minimum = 0 if count == 0 else max(1, min(expected_payload_rows, count) // 2)
    if score < minimum:
        raise ValueError('unrecognized cdfiles record layout')
    return entries


def read_cdf(path: str | Path) -> list[CdfEntry]:
    return parse_cdf_bytes(Path(path).read_bytes())


def legacy_tuples(path: str | Path) -> list[tuple[int, int, str]]:
    """Compatibility view for the original Flask backend."""
    return [
        (entry.archive_offset, entry.size, entry.name)
        for entry in read_cdf(path)
    ]
