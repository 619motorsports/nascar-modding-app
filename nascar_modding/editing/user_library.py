"""Shared persistent AI-preset and pit-observation library."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
from pathlib import Path
import threading

from nascar_modding.core.files import atomic_write_json


def profile_config_path(base_dir: str | Path, game_id: str) -> Path:
    base = Path(base_dir)
    return base / 'config.json' if game_id == 'nascar15' else base / 'profiles' / game_id / 'config.json'


class UserLibrary:
    _lock = threading.RLock()

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else {}
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, value, indent=1)

    def presets(self) -> list[dict]:
        rows = self._load().get('custom_ai_presets', [])
        return list(rows) if isinstance(rows, list) else []

    @staticmethod
    def _clean_preset(value: dict) -> dict:
        name = str(value.get('name', '')).strip()[:80]
        kind = str(value.get('kind', 'track'))
        if not name:
            raise ValueError('preset name is required')
        if kind not in ('track', 'global', 'pit'):
            raise ValueError('preset kind must be track, global, or pit')
        changes = []
        for change in (value.get('changes') or [])[:100]:
            field = str(change.get('field', '')).strip()
            if field:
                changes.append({'field': field, 'value': change.get('value')})
        if not changes:
            raise ValueError('preset has no fields')
        return {
            'id': hashlib.sha1(f'{kind}|{name}'.encode()).hexdigest()[:12],
            'name': name, 'kind': kind, 'note': str(value.get('note', ''))[:500],
            'changes': changes,
        }

    def save_preset(self, value: dict) -> dict:
        item = self._clean_preset(value)
        with self._lock:
            config = self._load()
            rows = [
                row for row in config.get('custom_ai_presets', [])
                if row.get('id') != item['id']
                and not (row.get('kind') == item['kind'] and row.get('name') == item['name'])
            ]
            rows.append(item)
            config['custom_ai_presets'] = rows[-200:]
            self._save(config)
        return {'preset': item, 'count': len(config['custom_ai_presets'])}

    def delete_preset(self, preset_id: str) -> dict:
        with self._lock:
            config = self._load()
            rows = config.get('custom_ai_presets', [])
            kept = [row for row in rows if str(row.get('id')) != str(preset_id)]
            config['custom_ai_presets'] = kept
            self._save(config)
        return {'deleted': len(rows) - len(kept), 'count': len(kept)}

    def export_presets_bytes(self) -> bytes:
        return json.dumps({
            'format': 'nascar-ai-presets', 'version': 1, 'presets': self.presets(),
        }, indent=2, ensure_ascii=False).encode('utf-8')

    def import_presets_bytes(self, payload: bytes) -> dict:
        value = json.loads(payload.decode('utf-8-sig'))
        rows = value.get('presets', []) if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError('preset file has no preset list')
        accepted = []
        for row in rows[:200]:
            try:
                accepted.append(self._clean_preset(row))
            except (TypeError, ValueError):
                continue
        with self._lock:
            config = self._load()
            merged = {
                (row.get('kind'), row.get('name')): row
                for row in config.get('custom_ai_presets', [])
            }
            for row in accepted:
                merged[(row['kind'], row['name'])] = row
            config['custom_ai_presets'] = list(merged.values())[-200:]
            self._save(config)
        return {'count': len(config['custom_ai_presets']), 'imported': len(accepted)}

    def pit_entries(self) -> list[dict]:
        rows = self._load().get('pit_strategy_test_log', [])
        return list(rows) if isinstance(rows, list) else []

    def merge_bundle(self, presets: list[dict], pit_entries: list[dict]) -> dict:
        accepted_presets = []
        for row in list(presets or [])[:200]:
            try:
                accepted_presets.append(self._clean_preset(row))
            except (TypeError, ValueError):
                continue
        accepted_pits = [dict(row) for row in list(pit_entries or [])[:1000]
                         if isinstance(row, dict) and str(row.get('id') or '').strip()]
        with self._lock:
            config = self._load()
            preset_map = {
                (row.get('kind'), row.get('name')): row
                for row in config.get('custom_ai_presets', [])
            }
            before_presets = dict(preset_map)
            for row in accepted_presets:
                preset_map[(row['kind'], row['name'])] = row
            pit_map = {str(row.get('id')): row for row in config.get('pit_strategy_test_log', [])}
            before_pits = dict(pit_map)
            for row in accepted_pits:
                pit_map[str(row['id'])] = row
            config['custom_ai_presets'] = list(preset_map.values())[-200:]
            config['pit_strategy_test_log'] = list(pit_map.values())[-1000:]
            self._save(config)
        return {
            'presets': sum(before_presets.get(key) != value for key, value in preset_map.items()),
            'pit_log': sum(before_pits.get(key) != value for key, value in pit_map.items()),
        }

    def add_pit_entry(self, value: dict) -> dict:
        note = str(value.get('note', '')).strip()[:2000]
        if not note:
            raise ValueError('enter an observation')
        now = _datetime.datetime.now()
        item = {
            'id': hashlib.sha1((now.isoformat() + note).encode()).hexdigest()[:12],
            'created': now.isoformat(timespec='seconds'),
            'track': str(value.get('track', ''))[:80],
            'preset': str(value.get('preset', ''))[:80],
            'result': str(value.get('result', 'untested'))[:40],
            'note': note,
        }
        with self._lock:
            config = self._load()
            rows = config.setdefault('pit_strategy_test_log', [])
            rows.append(item)
            config['pit_strategy_test_log'] = rows[-1000:]
            self._save(config)
        return {'item': item, 'count': len(config['pit_strategy_test_log'])}

    def delete_pit_entry(self, entry_id: str) -> dict:
        with self._lock:
            config = self._load()
            rows = config.get('pit_strategy_test_log', [])
            kept = [row for row in rows if str(row.get('id')) != str(entry_id)]
            config['pit_strategy_test_log'] = kept
            self._save(config)
        return {'deleted': len(rows) - len(kept), 'count': len(kept)}
