"""Validated helpers for the Python-2 marshal layout embedded in game PYC files."""

from __future__ import annotations

import struct


class _MarshalReader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    def _take(self, count: int) -> bytes:
        if count < 0 or self.offset + count > len(self.data):
            raise ValueError('truncated Python-2 marshal object')
        result = self.data[self.offset:self.offset + count]
        self.offset += count
        return result

    def _byte(self) -> int:
        return self._take(1)[0]

    def _i32(self) -> int:
        return struct.unpack('<i', self._take(4))[0]

    def object(self, depth: int = 0):
        if depth > 300:
            raise ValueError('marshal nesting is too deep')
        kind = chr(self._byte() & 0x7f)
        if kind == 'N':
            return None
        if kind in 'FT':
            return kind == 'T'
        if kind == 'i':
            return self._i32()
        if kind == 'I':
            return struct.unpack('<q', self._take(8))[0]
        if kind == 'g':
            return struct.unpack('<d', self._take(8))[0]
        if kind == 'f':
            return float(self._take(self._byte()).decode('ascii'))
        if kind in 'st':
            return self._take(self._i32())
        if kind == 'u':
            return self._take(self._i32()).decode('utf-8', 'replace')
        if kind == 'R':
            self._i32()
            return None
        if kind in '([':
            count = self._i32()
            if count < 0 or count > 10_000_000:
                raise ValueError('invalid marshal sequence length')
            return tuple(self.object(depth + 1) for _ in range(count))
        if kind == 'l':
            self._take(abs(self._i32()) * 2)
            return None
        if kind == 'c':
            self._take(16)
            self.object(depth + 1)  # bytecode
            constants = self.object(depth + 1)
            for _ in range(6):
                self.object(depth + 1)
            self._take(4)
            self.object(depth + 1)
            return constants
        raise ValueError(f'unsupported Python-2 marshal type {kind!r}')


class _MarshalSkipper:
    """Position-only walker used to locate the root code and constant tuple."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, count: int) -> int:
        if count < 0 or self.offset + count > len(self.data):
            raise ValueError('truncated Python-2 marshal object')
        start = self.offset
        self.offset += count
        return start

    def i32(self) -> int:
        return struct.unpack_from('<i', self.data, self.take(4))[0]

    def object(self, depth: int = 0):
        if depth > 300:
            raise ValueError('marshal nesting is too deep')
        kind = chr(self.data[self.take(1)] & 0x7f)
        if kind in ('N', 'T', 'F', 'S', '.', '0'):
            return
        if kind == 'i':
            self.take(4)
            return
        if kind in ('I', 'g'):
            self.take(8)
            return
        if kind == 'y':
            self.take(16)
            return
        if kind == 'f':
            self.take(self.data[self.take(1)])
            return
        if kind == 'x':
            self.take(self.data[self.take(1)])
            self.take(self.data[self.take(1)])
            return
        if kind == 'l':
            self.take(abs(self.i32()) * 2)
            return
        if kind in ('s', 't', 'u'):
            size = self.i32()
            if size < 0:
                raise ValueError('negative marshal string length')
            self.take(size)
            return
        if kind == 'R':
            self.take(4)
            return
        if kind in ('(', '['):
            count = self.i32()
            if count < 0 or count > 10_000_000:
                raise ValueError('invalid marshal sequence length')
            for _ in range(count):
                self.object(depth + 1)
            return
        if kind == '{':
            while True:
                if self.offset >= len(self.data):
                    raise ValueError('unterminated marshal dict')
                if chr(self.data[self.offset] & 0x7f) == '0':
                    self.offset += 1
                    break
                self.object(depth + 1)
                self.object(depth + 1)
            return
        if kind == 'c':
            self.take(16)
            for _ in range(8):
                self.object(depth + 1)
            self.take(4)
            self.object(depth + 1)
            return
        raise ValueError(f'unsupported Python-2 marshal type {kind!r}')


def root_constants(pyc: bytes) -> tuple:
    """Decode the root code object's constant tuple from a Python-2 PYC."""
    if len(pyc) < 9:
        raise ValueError('PYC file is too small')
    value = _MarshalReader(pyc, 8).object()
    if not isinstance(value, tuple):
        raise ValueError('PYC root constants are not a tuple')
    return value


def root_layout(pyc: bytes) -> dict:
    """Return exact byte locations needed for safe root-constant rebuilding."""
    if len(pyc) < 31 or pyc[8] & 0x7f != ord('c'):
        raise ValueError('unexpected PYC root layout')
    reader = _MarshalSkipper(pyc)
    reader.offset = 9
    reader.take(16)
    kind = chr(pyc[reader.take(1)] & 0x7f)
    if kind not in ('s', 't'):
        raise ValueError('PYC root bytecode is not a string object')
    code_len = reader.i32()
    code_offset = reader.take(code_len)
    const_type_offset = reader.offset
    if chr(pyc[reader.take(1)] & 0x7f) != '(':
        raise ValueError('PYC root constants are not a tuple')
    count_position = reader.offset
    count = reader.i32()
    items_start = reader.offset
    for _ in range(count):
        reader.object(1)
    return {
        'code_off': code_offset,
        'code_len': code_len,
        'const_type_off': const_type_offset,
        'count_pos': count_position,
        'count': count,
        'items_start': items_start,
        'const_end': reader.offset,
    }


def rebuild_with_float_constant(pyc: bytes, load_offset: int, target: float):
    """Append one float constant and point one validated LOAD_CONST at it."""
    layout = root_layout(pyc)
    constants = root_constants(pyc)
    if layout['count'] != len(constants):
        raise ValueError('PYC constant-count validation failed')
    if layout['count'] >= 65535:
        raise ValueError('PYC constant table has reached the 16-bit LOAD_CONST limit')
    code_end = layout['code_off'] + layout['code_len']
    if (
        load_offset < layout['code_off']
        or load_offset + 3 > code_end
        or pyc[load_offset] != 0x64
    ):
        raise ValueError('rating LOAD_CONST offset is invalid')
    new_index = layout['count']
    output = bytearray(pyc)
    struct.pack_into('<H', output, load_offset + 1, new_index)
    struct.pack_into('<i', output, layout['count_pos'], new_index + 1)
    output[layout['const_end']:layout['const_end']] = b'g' + struct.pack('<d', float(target))
    checked = root_constants(bytes(output))
    if (
        len(checked) != new_index + 1
        or not isinstance(checked[new_index], float)
        or abs(checked[new_index] - target) > 1e-12
    ):
        raise ValueError('rebuilt rating PYC failed constant readback')
    if output[load_offset] != 0x64 or struct.unpack_from('<H', output, load_offset + 1)[0] != new_index:
        raise ValueError('rebuilt rating PYC failed LOAD_CONST readback')
    return bytes(output), new_index
