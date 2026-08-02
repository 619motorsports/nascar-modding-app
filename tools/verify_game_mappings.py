#!/usr/bin/env python3
"""Verify profile mappings against one or more installed games."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nascar_modding.games.installation import GameInstallation
from nascar_modding.verification.mappings import write_audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('game_id', choices=('nascar13', 'nascar14', 'nascar15'))
    parser.add_argument('install_root', type=Path)
    parser.add_argument(
        '--output', type=Path, default=None,
        help='JSON destination (defaults to data/verified_mappings/<game>.json)',
    )
    args = parser.parse_args()
    output = args.output or ROOT / 'data' / 'verified_mappings' / f'{args.game_id}.json'
    report = write_audit(GameInstallation(args.game_id, args.install_root), output)
    print(f"{args.game_id}: {report['facts']['total_entries']} indexed resources")
    print(f"audit: {'PASS' if report['ok'] else 'FAIL'} -> {output}")
    for error in report['errors']:
        print(f'  - {error}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
