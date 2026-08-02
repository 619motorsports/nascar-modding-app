"""Read-only resolution of stock mesh materials from game texture packs."""

from __future__ import annotations

import re

from PIL import Image

import containers
from nascar_modding.formats.eutechnyx_mesh import MeshScene, MeshTextureImage
from nascar_modding.games.installation import GameInstallation


_BODY_CONTAINER = re.compile(r'^(NASCAR\d+)_BODY.*\.ARC$', re.IGNORECASE)


def texture_container_name(body_name: str) -> str:
    match = _BODY_CONTAINER.match(body_name)
    if not match:
        raise ValueError(f'cannot derive texture pack from {body_name}')
    return f'{match.group(1)}_TEXTURES_X.ARC'


def resolve_game_materials(
    installation: GameInstallation,
    body_name: str,
    scene: MeshScene,
    *,
    max_dimension: int = 1024,
) -> dict[str, object]:
    """Attach decoded diffuse textures used by a car scene and return a report."""
    container_name = texture_container_name(body_name)
    archive_key, _entry = installation.find_entry(container_name)
    payload = installation.read_entry(container_name, archive_key)
    entries, _base = containers.parse_multi_arc(payload)
    by_name = {str(entry['name']).casefold(): entry for entry in entries}

    used_materials = {part.material_name for part in scene.parts}
    requested: dict[str, str] = {}
    textured_materials = 0
    for name in used_materials:
        material = scene.materials.get(name)
        diffuse = material.texture_for(0) if material else None
        if diffuse:
            textured_materials += 1
            requested.setdefault(diffuse.casefold(), diffuse)

    decoded: dict[str, MeshTextureImage] = {}
    missing: list[str] = []
    for key, display_name in requested.items():
        entry = by_name.get(key)
        if entry is None:
            missing.append(display_name)
            continue
        image = containers.multi_read_png(payload, entry).convert('RGBA')
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        decoded[key] = MeshTextureImage(display_name, image.width, image.height, image.tobytes())

    scene.texture_images.update(decoded)
    return {
        'archive': archive_key,
        'container': container_name,
        'used_materials': len(used_materials),
        'textured_materials': textured_materials,
        'decoded_textures': len(decoded),
        'missing_textures': tuple(sorted(missing, key=str.casefold)),
    }
