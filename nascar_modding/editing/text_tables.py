"""Shared access and editing for indexed Eutechnyx LDA language tables."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
from pathlib import Path
import re

import containers as container_formats

from nascar_modding.core.cdf import read_cdf
from nascar_modding.editing.archive import ArchiveEntryEditor, backup_path
from nascar_modding.games.installation import GameInstallation


TEXT_NAME_PATTERN = re.compile(r'^TEXT\d*\.LDA$', re.IGNORECASE)
FORMAT_TOKEN_PATTERN = re.compile(
    r'%(?:\([^)]+\))?[#0\- +]?\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]|'
    r'\{[^{}\r\n]+\}|\$[A-Za-z_][A-Za-z0-9_]*\$|'
    r'\\[nrt]'
)
TEXT_CATEGORIES = (
    'Menus & Navigation', 'Race, HUD & Session Text', 'Career & Championship',
    'Paint, Garage & Team Shop', 'Prompts & Controls', 'Errors & Warnings',
    'Loading, Tips & Help', 'Formatted Templates', 'Other User Text',
    'Technical / Internal',
)
MAX_BATCH_CHANGES = 5000
_MAIN_MENU_TEXT = {
    'race now', 'career', 'single season', 'multiplayer', 'paint booth', 'options',
    'extras', 'my nascar', 'team shop', 'driver select', 'track select', 'continue',
    'quick race', 'championship', 'livery studio', 'quit', 'exit game',
}


def format_tokens(value: str) -> list[str]:
    return [token for token in FORMAT_TOKEN_PATTERN.findall(str(value)) if token != '%%']


def classify_text(value: str) -> tuple[str, str, bool]:
    text = str(value)
    lowered = text.strip().casefold()
    category, screen, user_facing = 'Other User Text', 'General UI', True
    if not text:
        return category, screen, False
    printable = sum(char.isprintable() or char in '\r\n\t' for char in text) / len(text)
    technical = (
        printable < 0.92
        or bool(re.search(r'[/\\]|\.(?:arc|dds|tga|png|pyc|lda|xml|csv)$', lowered))
        or re.match(r'^[A-Z0-9_]{4,}$', text) is not None
        or (len(text) > 2 and ' ' not in text and text.count('_') >= 2)
    )
    if technical:
        return 'Technical / Internal', 'Internal identifier', False
    if lowered in _MAIN_MENU_TEXT:
        return 'Menus & Navigation', 'Main Menu', True
    if any(word in lowered for word in (
        'race', 'lap', 'qualif', 'practice', 'pit', 'caution', 'restart',
        'green flag', 'checkered', 'draft', 'damage', 'fuel', 'tyre', 'tire',
    )):
        category, screen = 'Race, HUD & Session Text', 'Race / Garage / HUD'
    elif any(word in lowered for word in (
        'career', 'season', 'championship', 'standings', 'points', 'sponsor',
        'contract', 'calendar', 'playoff', 'chase',
    )):
        category, screen = 'Career & Championship', 'Career / Single Season'
    elif any(word in lowered for word in (
        'paint', 'scheme', 'livery', 'colour', 'color', 'decal', 'vinyl', 'team shop',
    )):
        category, screen = 'Paint, Garage & Team Shop', 'Paint Booth / Team Shop'
    elif any(word in lowered for word in (
        'controller', 'keyboard', 'button', 'press ', 'select ', 'confirm',
        'cancel', 'back', 'continue', 'yes', 'no',
    )):
        category, screen = 'Prompts & Controls', 'Prompts / Controls'
    elif any(word in lowered for word in (
        'error', 'failed', 'unable', 'warning', 'invalid', 'not available',
        'connection', 'disconnected',
    )):
        category, screen = 'Errors & Warnings', 'Dialog / Error'
    elif any(word in lowered for word in ('loading', 'tip:', 'did you know', 'trivia', 'fact:')) or len(text) > 180:
        category, screen = 'Loading, Tips & Help', 'Loading / Help'
    elif format_tokens(text):
        category, screen = 'Formatted Templates', 'Dynamic UI text'
    elif len(text) <= 42 and (text.istitle() or text.isupper()):
        category, screen = 'Menus & Navigation', 'Menu / Heading'
    return category, screen, user_facing


class LanguageTableStore:
    """Canonical live/pristine archive access shared by all text editors."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation

    @property
    def has_pristine(self) -> bool:
        pair = self.installation.archive_pairs['0']
        return Path(backup_path(pair.archive)).is_file() and Path(backup_path(pair.index)).is_file()

    def _source(self, pristine: bool, required: bool = False):
        pair = self.installation.archive_pairs['0']
        if pristine and self.has_pristine:
            return Path(backup_path(pair.archive)), Path(backup_path(pair.index))
        if pristine and required:
            raise FileNotFoundError('no pristine ARCHIVE0/index backup pair exists yet')
        return pair.archive, pair.index

    def files(self, pristine: bool = False, required: bool = False) -> list[dict]:
        _archive, index = self._source(pristine, required)
        return [
            {'name': entry.name, 'size': entry.size, 'offset': entry.archive_offset}
            for entry in read_cdf(index)
            if TEXT_NAME_PATTERN.match(entry.name)
        ]

    def read(self, name: str, pristine: bool = False, required: bool = False):
        archive, index = self._source(pristine, required)
        matches = [
            entry for entry in read_cdf(index)
            if entry.name.casefold() == str(name).casefold()
        ]
        if len(matches) != 1:
            raise KeyError(f'{name} does not identify one language table')
        entry = matches[0]
        with archive.open('rb') as handle:
            handle.seek(entry.archive_offset)
            payload = handle.read(entry.size)
        if len(payload) != entry.size:
            raise IOError(f'short read for {entry.name}')
        return payload, container_formats.lda_entries(payload), entry

    def blobs(self, pristine: bool = False, required: bool = False) -> dict[str, bytes]:
        result = {}
        for row in self.files(pristine, required):
            payload, _entries, entry = self.read(row['name'], pristine, required)
            result[entry.name] = payload
        return result


class TextTableEditor:
    """Exact-index text editor with token validation and atomic batches."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.store = LanguageTableStore(installation)
        self.archive_editor = ArchiveEntryEditor(installation)

    @staticmethod
    def _encode(value: str) -> bytes:
        text = str(value)
        if '\0' in text:
            raise ValueError('text cannot contain a NUL character')
        try:
            return text.encode('latin1')
        except UnicodeEncodeError as exc:
            raise ValueError('text contains characters outside the game Latin-1 encoding') from exc

    @staticmethod
    def _missing_tokens(old: str, new: str) -> list[str]:
        old_counts, new_counts = Counter(format_tokens(old)), Counter(format_tokens(new))
        return [
            token
            for token, count in old_counts.items()
            for _ in range(max(0, count - new_counts[token]))
        ]

    def files(self) -> list[dict]:
        stock_names = {
            row['name'].casefold() for row in self.store.files(True)
        } if self.store.has_pristine else set()
        return [
            {**row, 'has_stock': row['name'].casefold() in stock_names}
            for row in self.store.files()
        ]

    def entries(self, file_name: str) -> list[dict]:
        payload, entries, metadata = self.store.read(file_name)
        stock_entries = []
        if self.store.has_pristine:
            try:
                _stock_payload, stock_entries, _stock_metadata = self.store.read(file_name, True, True)
            except KeyError:
                stock_entries = []
        counts = Counter(item['raw'].decode('latin1', 'replace') for item in entries)
        rows = []
        for item in entries:
            current = item['raw'].decode('latin1', 'replace')
            stock = (
                stock_entries[item['index']]['raw'].decode('latin1', 'replace')
                if item['index'] < len(stock_entries) else None
            )
            category, screen, user_facing = classify_text(current)
            rows.append({
                'file': metadata.name,
                'index': item['index'],
                'current': current,
                'stock': stock,
                'current_length': len(item['raw']),
                'stock_length': (
                    len(stock_entries[item['index']]['raw'])
                    if item['index'] < len(stock_entries) else None
                ),
                'category': category,
                'screen': screen,
                'user_facing': user_facing,
                'modified': stock is not None and stock != current,
                'tokens': format_tokens(current),
                'reference_count': counts[current],
                'shared': counts[current] > 1,
                'file_size': len(payload),
            })
        return rows

    def replace_exact(self, old_text, new_text: str, *, strip=True) -> dict:
        """Replace every complete matching table entry through the batch writer."""
        source = old_text if isinstance(old_text, (set, tuple, list)) else (old_text,)
        wanted = {str(value).strip() if strip else str(value) for value in source}
        changes = []
        for file_row in self.files():
            for row in self.entries(file_row['name']):
                current = str(row['current'])
                comparable = current.strip() if strip else current
                if comparable in wanted:
                    changes.append({'file': row['file'], 'index': row['index'], 'new': str(new_text)})
        if not changes:
            raise ValueError('text was not found as a complete language-table entry')
        return self.apply_batch(changes, force_tokens=True)

    def plan(self, file_name: str, index: int, new_text: str, force_tokens: bool = False) -> dict:
        payload, entries, metadata = self.store.read(file_name)
        item_index = int(index)
        if item_index < 0 or item_index >= len(entries):
            raise IndexError(f'string index {item_index} is outside {metadata.name}')
        old = entries[item_index]['raw'].decode('latin1', 'replace')
        encoded = self._encode(new_text)
        missing = self._missing_tokens(old, str(new_text))
        if missing and not force_tokens:
            raise ValueError('replacement removes required format token(s): ' + ', '.join(missing))
        rebuilt, changed = container_formats.lda_rebuild_indices(payload, {item_index: encoded})
        return {
            'file': metadata.name,
            'index': item_index,
            'old': old,
            'new': str(new_text),
            'old_bytes': len(entries[item_index]['raw']),
            'new_bytes': len(encoded),
            'file_size': len(payload),
            'rebuilt_size': len(rebuilt),
            'size_delta': len(rebuilt) - len(payload),
            'repoint': len(rebuilt) != len(payload),
            'missing_tokens': missing,
            'format_tokens': format_tokens(old),
            'changed': bool(changed),
        }

    def apply(self, file_name: str, index: int, new_text: str, force_tokens: bool = False) -> dict:
        plan = self.plan(file_name, index, new_text, force_tokens)
        if not plan['changed']:
            return {'plan': plan, 'verified': True, 'write': None}
        payload, _entries, metadata = self.store.read(file_name)
        rebuilt, changed = container_formats.lda_rebuild_indices(
            payload, {int(index): self._encode(new_text)}
        )
        if changed != 1:
            raise ValueError('text-table rebuild did not change exactly one index')
        write = self.archive_editor.replace_or_repoint_entry(metadata.name, rebuilt, '0')
        try:
            live_rows = self.entries(metadata.name)
            if live_rows[int(index)]['current'] != str(new_text):
                raise IOError('text edit failed semantic read-back verification')
        except Exception as verification_error:
            try:
                self.archive_editor.replace_or_repoint_entry(metadata.name, payload, '0')
            except Exception as rollback_error:
                raise RuntimeError(
                    f'text verification failed ({verification_error}) and rollback failed: {rollback_error}'
                ) from verification_error
            raise
        return {'plan': plan, 'verified': True, 'write': write}

    def restore(self, file_name: str, index: int) -> dict:
        _payload, entries, metadata = self.store.read(file_name, True, True)
        item_index = int(index)
        if item_index < 0 or item_index >= len(entries):
            raise IndexError(f'string index {item_index} is outside {metadata.name}')
        stock = entries[item_index]['raw'].decode('latin1', 'replace')
        result = self.apply(metadata.name, item_index, stock, True)
        result['stock'] = stock
        return result

    def restore_file(self, file_name: str) -> dict:
        stock, _entries, metadata = self.store.read(file_name, True, True)
        write = self.archive_editor.replace_or_repoint_entry(metadata.name, stock, '0')
        if self.installation.read_entry(metadata.name, '0') != stock:
            raise IOError('restored language table failed read-back verification')
        return {'file': metadata.name, 'verified': True, 'write': write}

    def apply_batch(self, changes: list[dict], force_tokens: bool = False) -> dict:
        if not changes:
            raise ValueError('no text changes were supplied')
        if len(changes) > MAX_BATCH_CHANGES:
            raise ValueError(f'batch exceeds {MAX_BATCH_CHANGES} changes')
        grouped = defaultdict(dict)
        original = {}
        changed_count = 0
        for change in changes:
            file_name = str(change['file'])
            index = int(change['index'])
            new_text = str(change.get('new', ''))
            plan = self.plan(file_name, index, new_text, force_tokens)
            if not plan['changed']:
                continue
            grouped[plan['file']][index] = self._encode(new_text)
        replacements = {}
        for file_name, values in grouped.items():
            payload, _entries, metadata = self.store.read(file_name)
            original[metadata.name] = payload
            rebuilt, count = container_formats.lda_rebuild_indices(payload, values)
            changed_count += count
            replacements[metadata.name] = rebuilt
        if not replacements:
            return {'files': 0, 'changes': 0, 'verified': True, 'writes': []}
        result = self.archive_editor.replace_entries(replacements, '0')
        try:
            for file_name, values in grouped.items():
                rows = self.entries(file_name)
                for index, encoded in values.items():
                    if rows[index]['current'].encode('latin1') != encoded:
                        raise IOError(f'{file_name} #{index} failed batch verification')
        except Exception as verification_error:
            try:
                self.archive_editor.replace_entries(original, '0')
            except Exception as rollback_error:
                raise RuntimeError(
                    f'text batch verification failed ({verification_error}) and rollback failed: '
                    f'{rollback_error}'
                ) from verification_error
            raise
        return {
            'files': len(replacements),
            'changes': changed_count,
            'verified': True,
            'writes': result['writes'],
        }

    def export_csv_bytes(self, file_name: str | None = None) -> bytes:
        selected = [file_name] if file_name else [row['name'] for row in self.files()]
        output = io.StringIO(newline='')
        fields = (
            'file', 'index', 'category', 'screen', 'stock_text', 'current_text',
            'new_text', 'current_bytes', 'reference_count', 'format_tokens',
        )
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for name in selected:
            for row in self.entries(name):
                writer.writerow({
                    'file': row['file'], 'index': row['index'],
                    'category': row['category'], 'screen': row['screen'],
                    'stock_text': row['stock'] or '', 'current_text': row['current'],
                    'new_text': row['current'], 'current_bytes': row['current_length'],
                    'reference_count': row['reference_count'],
                    'format_tokens': ' | '.join(row['tokens']),
                })
        return output.getvalue().encode('utf-8-sig')

    def preview_csv(self, payload: bytes) -> list[dict]:
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError('CSV exceeds the 8 MB safety limit')
        reader = csv.DictReader(io.StringIO(payload.decode('utf-8-sig')))
        changes, seen = [], set()
        for line, row in enumerate(reader, 2):
            file_name = (row.get('file') or '').strip()
            index_text = (row.get('index') or '').strip()
            new_text = row.get('new_text')
            if new_text is None:
                new_text = row.get('current_text')
            if not file_name or not index_text:
                continue
            key = (file_name.casefold(), int(index_text))
            if key in seen:
                raise ValueError(f'duplicate file/index at CSV line {line}')
            seen.add(key)
            try:
                plan = self.plan(file_name, int(index_text), new_text or '')
                if plan['changed']:
                    changes.append({
                        'file': plan['file'], 'index': int(index_text),
                        'new': new_text or '', 'valid': True, 'plan': plan, 'line': line,
                    })
            except Exception as exc:
                changes.append({
                    'file': file_name, 'index': int(index_text), 'new': new_text or '',
                    'valid': False, 'error': str(exc), 'line': line,
                })
            if len(changes) > MAX_BATCH_CHANGES:
                raise ValueError(f'CSV has more than {MAX_BATCH_CHANGES} changes')
        return changes
