"""Canonical loader for bundled Python backend helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


def load_module(
    path: str | Path,
    module_name: str,
    *,
    add_parent: bool = False,
    missing_message: str | None = None,
    load_message: str | None = None,
) -> ModuleType:
    """Load a helper by path while keeping import validation and cleanup uniform."""
    helper_path = Path(path).resolve()
    if not helper_path.is_file():
        raise RuntimeError(missing_message or f'backend helper is missing: {helper_path}')

    if add_parent:
        parent = str(helper_path.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

    existing = sys.modules.get(module_name)
    if existing is not None:
        existing_file = getattr(existing, '__file__', None)
        if existing_file and Path(existing_file).resolve() == helper_path:
            return existing

    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(load_message or f'could not load backend helper: {helper_path}')

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module
