"""Cross-frontend process guards for game-file editing."""

from __future__ import annotations

import os
import subprocess


def is_process_running(executable_name: str) -> bool:
    if os.name != 'nt':
        return False
    result = subprocess.run(
        ['tasklist', '/FI', f'IMAGENAME eq {executable_name}'],
        capture_output=True, text=True, errors='ignore', timeout=10,
    )
    return executable_name.casefold() in (result.stdout or '').casefold()


def assert_process_closed(executable_name: str, operation: str = 'editing game files') -> None:
    if is_process_running(executable_name):
        raise RuntimeError(
            f'{executable_name} is running; close the game before {operation}'
        )
