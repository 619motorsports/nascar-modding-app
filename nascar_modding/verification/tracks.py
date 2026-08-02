"""Read-only track-resource inventory and comparison shared by both frontends."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import re
import zipfile

from nascar_modding.games.installation import GameInstallation


TRACK_ALIAS_HINTS = {
    'Auto Club': ('AUTOCLUB', 'FONTANA'), 'Charlotte': ('CHARLOTTE', 'LOWES'),
    'Sonoma': ('SONOMA', 'INFINEON'), 'Watkins Glen': ('WATKINSGLEN', 'WATKINS', 'GLEN'),
    'New Hampshire': ('NEWHAMPSHIRE', 'LOUDON'), 'Indianapolis': ('INDIANAPOLIS', 'INDY'),
    'Darlington': ('DARLINGTON',), 'Homestead': ('HOMESTEAD',),
    'Talladega': ('TALLADEGA',), 'Daytona': ('DAYTONA',),
    'Martinsville': ('MARTINSVILLE',), 'Bristol': ('BRISTOL',),
    'Richmond': ('RICHMOND',), 'Dover': ('DOVER',), 'Pocono': ('POCONO',),
    'Michigan': ('MICHIGAN',), 'Kansas': ('KANSAS',), 'Atlanta': ('ATLANTA',),
    'Texas': ('TEXAS',), 'Phoenix': ('PHOENIX',),
    'Las Vegas': ('LASVEGAS', 'VEGAS'), 'Kentucky': ('KENTUCKY',),
    'Chicagoland': ('CHICAGOLAND', 'CHICAGO'),
}


def normalize_track_name(value: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def track_category(name: str) -> str:
    upper = name.upper()
    if upper.endswith('_SCR.ARC') or any(word in upper for word in ('PHYS', 'CHASSIS', 'TIRE', 'TYRE', 'AERO')):
        return 'Physics / Vehicle Config'
    if any(word in upper for word in ('AICONFIG', 'AI_', '_AI', 'RACINGLINE', 'RACE_LINE')):
        return 'AI / Racing Line'
    if any(word in upper for word in ('CAMERA', 'CAM_', 'REPLAY')):
        return 'Cameras'
    if any(word in upper for word in ('TRACKCARD', 'TRACKDETAIL', 'CALENDAR_TRACK', 'TRACKSELECT', 'MINIMAP', 'MAPIMAGE', 'LOADING')):
        return 'UI / Track Images'
    if any(word in upper for word in ('FSB', 'SOUND', 'AUDIO', 'AMBIENT')):
        return 'Audio / Ambience'
    if any(word in upper for word in ('RACEDATA', 'EVENT', 'SCHEDULE', 'RACESETTING')):
        return 'Race Metadata'
    if any(word in upper for word in ('WORLD', 'MESH', 'MODEL', 'GEOM', 'COLLISION', 'BARRIER', 'WALL', 'TRACK')):
        return 'World / Geometry'
    if any(word in upper for word in ('TEXTURE', 'TEX', 'DDS', 'MATERIAL', 'BILLBOARD', 'SPONSOR')):
        return 'Textures / Materials'
    return 'Unknown / Other'


class TrackInventory:
    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self._rows: list[dict] | None = None

    def aliases(self) -> dict[str, set[str]]:
        aliases = {
            track: {normalize_track_name(track), *(normalize_track_name(value) for value in values)}
            for track, values in TRACK_ALIAS_HINTS.items()
        }
        for key in self.installation.archive_pairs:
            for entry in self.installation.entries(key):
                upper = entry.name.upper()
                if not upper.endswith('_SCR.ARC'):
                    continue
                stem = upper.removesuffix('_SCR.ARC')
                if stem.startswith('NASCAR'):
                    stem = stem[6:]
                for tail in ('PLAYER', 'AI'):
                    if stem.endswith(tail):
                        stem = stem[:-len(tail)]
                normalized = normalize_track_name(stem)
                for track, values in aliases.items():
                    if any(value in normalized or normalized in value for value in values if len(value) >= 4):
                        values.add(normalized)
                        break
        return {track: {value for value in values if len(value) >= 4} for track, values in aliases.items()}

    def rows(self, force: bool = False) -> list[dict]:
        if self._rows is not None and not force:
            return list(self._rows)
        alias_pairs = [
            (alias, track)
            for track, aliases in self.aliases().items()
            for alias in aliases
        ]
        alias_pairs.sort(key=lambda item: len(item[0]), reverse=True)
        rows = []
        for key in self.installation.archive_pairs:
            for entry in self.installation.entries(key):
                normalized = normalize_track_name(entry.name)
                matched = {track for alias, track in alias_pairs if alias in normalized}
                generic = any(word in entry.name.upper() for word in (
                    'TRACK', 'RACEWAY', 'SPEEDWAY', 'CIRCUIT', 'ROADCOURSE',
                ))
                if not matched and not generic:
                    continue
                for track in sorted(matched or {'Shared / Unmapped'}):
                    rows.append({
                        'track': track, 'archive': str(key), 'name': entry.name,
                        'offset': entry.archive_offset, 'size': entry.size,
                        'category': track_category(entry.name),
                        'extension': Path(entry.name).suffix.upper() or '(none)',
                        'confidence': 'likely' if matched else 'unknown',
                    })
        self._rows = sorted(
            rows, key=lambda row: (row['track'], row['category'], row['name'], int(row['archive']))
        )
        return list(self._rows)

    def filter(self, track: str = 'all', category: str = 'all', query: str = '') -> list[dict]:
        wanted = str(query).casefold()
        return [
            row for row in self.rows()
            if (track == 'all' or row['track'] == track)
            and (category == 'all' or row['category'] == category)
            and (not wanted or wanted in (
                row['name'] + ' ' + row['category'] + ' ' + row['track']
                + ' ARCHIVE' + row['archive']
            ).casefold())
        ]

    def summary(self) -> dict:
        rows = self.rows()
        tracks = sorted({row['track'] for row in rows}, key=lambda value: (value == 'Shared / Unmapped', value))
        categories = sorted({row['category'] for row in rows})
        counts = {track: sum(row['track'] == track for row in rows) for track in tracks}
        return {'rows': rows, 'tracks': tracks, 'categories': categories, 'track_counts': counts}

    def compare(self, first: str, second: str) -> dict:
        if not first or not second or first == second:
            raise ValueError('choose two different tracks')
        rows, aliases = self.rows(), self.aliases()

        def patterns(track):
            result = {}
            for row in rows:
                if row['track'] != track:
                    continue
                pattern = normalize_track_name(row['name'])
                for alias in aliases.get(track, ()):
                    pattern = pattern.replace(alias, 'TRACK')
                result.setdefault((row['category'], pattern), []).append(row)
            return result

        left, right = patterns(first), patterns(second)

        def public(keys, source):
            return [{'category': key[0], 'pattern': key[1], 'files': source[key]} for key in keys]

        shared = sorted(set(left) & set(right))
        only_left = sorted(set(left) - set(right))
        only_right = sorted(set(right) - set(left))
        return {
            'a': first, 'b': second, 'shared': public(shared, left),
            'only_a': public(only_left, left), 'only_b': public(only_right, right),
            'summary': {'shared': len(shared), 'only_a': len(only_left), 'only_b': len(only_right)},
        }

    def report_bytes(self, track: str = 'all') -> bytes:
        rows = self.filter(track=track)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=(
            'track', 'archive', 'name', 'offset', 'size', 'category', 'extension', 'confidence',
        ))
        writer.writeheader()
        writer.writerows(rows)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as package:
            package.writestr('track_files.csv', output.getvalue().encode('utf-8-sig'))
            package.writestr(
                'SUMMARY.txt',
                f'{self.installation.profile.name} Track Files Report\nTrack: {track}\nEntries: {len(rows)}\n\nRead-only heuristic inventory.\n',
            )
        return buffer.getvalue()

    def export_track(self, track: str, destination: str | Path) -> dict:
        if not track or track in ('all', 'Shared / Unmapped'):
            raise ValueError('choose one mapped track')
        rows = self.filter(track=track)
        total = sum(row['size'] for row in rows)
        if total > 512 * 1024 * 1024:
            raise ValueError('selected track exceeds the 512 MB safety limit')
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as package:
            for index, row in enumerate(rows):
                payload = self.installation.read_entry(row['name'], row['archive'])
                clean = re.sub(r'[^A-Za-z0-9_.-]+', '_', row['name'])
                package.writestr(f"ARCHIVE{row['archive']}/{index:04d}_{clean}", payload)
            package.writestr('manifest.json', json.dumps({
                'game_id': self.installation.profile.id, 'track': track,
                'count': len(rows), 'files': rows,
            }, indent=2))
        return {'path': output, 'track': track, 'count': len(rows), 'bytes': total}
