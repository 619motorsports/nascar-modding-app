"""Pure-Python Eutechnyx ARCC mesh reader used by the native 3D preview.

The block layout and vertex-mask handling are derived from the bundled Blender
add-on ``eutechnyx_mesh_importer.py``.  This module deliberately has no Blender
dependency: it returns NumPy arrays suitable for a native OpenGL widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import struct

import numpy as np


@dataclass(frozen=True, slots=True)
class Block:
    table_index: int
    index: int
    offset: int
    name_offset: int
    type: int
    size: int
    name: str = ''


@dataclass(slots=True)
class MeshPart:
    name: str
    material_name: str
    vertices: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray
    indices: np.ndarray


@dataclass(frozen=True, slots=True)
class MaterialTextureBinding:
    role: int
    texture_name: str


@dataclass(frozen=True, slots=True)
class MeshMaterial:
    name: str
    textures: tuple[MaterialTextureBinding, ...] = ()

    def texture_for(self, *roles: int) -> str | None:
        wanted = set(roles)
        return next(
            (binding.texture_name for binding in self.textures if binding.role in wanted),
            None,
        )


@dataclass(frozen=True, slots=True)
class MeshTextureImage:
    name: str
    width: int
    height: int
    rgba: bytes


@dataclass(slots=True)
class MeshScene:
    parts: list[MeshPart]
    source_name: str = ''
    attachment_points: dict[str, np.ndarray] = field(default_factory=dict)
    materials: dict[str, MeshMaterial] = field(default_factory=dict)
    texture_images: dict[str, MeshTextureImage] = field(default_factory=dict)

    @property
    def vertex_count(self) -> int:
        return sum(len(part.vertices) for part in self.parts)

    @property
    def triangle_count(self) -> int:
        return sum(len(part.indices) // 3 for part in self.parts)

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.parts:
            zero = np.zeros(3, dtype=np.float32)
            return zero.copy(), zero.copy()
        return (
            np.min(np.vstack([part.vertices for part in self.parts]), axis=0),
            np.max(np.vstack([part.vertices for part in self.parts]), axis=0),
        )


@dataclass(frozen=True, slots=True)
class _Submesh:
    mesh_id: int
    material_id: int
    vertex_count: int
    index_count: int
    mask1: int
    mask2: int
    stride: int
    vertex_offset: int
    index_offset: int
    mixed: bool = False


_CONVERSION = np.array(
    ((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)),
    dtype=np.float32,
)
_CONVERSION_INV = np.linalg.inv(_CONVERSION)


def _u8(data: bytes, offset: int) -> int:
    return data[offset]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<H', data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<I', data, offset)[0]


def _s32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<i', data, offset)[0]


def _cstring(data: bytes, offset: int) -> str:
    if not 0 <= offset < len(data):
        return ''
    end = data.find(b'\0', offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode('ascii', 'replace')


def _parse_blocks(data: bytes) -> list[Block]:
    if data[:4] == b'ARCE':
        raise ValueError('compressed ARCE meshes must be decompressed first')
    if data[:4] != b'ARCC':
        raise ValueError(f'not an ARCC mesh container: {data[:4]!r}')
    count = _u32(data, 4)
    table_end = 128 + count * 16
    if count <= 0 or count > 100_000 or table_end > len(data):
        raise ValueError(f'invalid ARCC block count: {count}')

    raw_blocks = []
    names_relative = None
    for table_index in range(count):
        pos = 128 + table_index * 16
        index, relative, name_offset = struct.unpack_from('<iII', data, pos)
        packed = _u32(data, pos + 12)
        block_type = packed & 0xFF
        raw = packed.to_bytes(4, 'little')
        size = int.from_bytes(raw[1:4], 'big')
        block = Block(
            table_index, index, table_end + relative, name_offset,
            block_type, size,
        )
        if block.offset + block.size > len(data):
            raise ValueError(
                f'block {table_index} extends past the ARCC container boundary'
            )
        raw_blocks.append(block)
        if block_type == 253:
            names_relative = relative

    name_base = table_end + names_relative if names_relative is not None else None
    blocks = []
    for block in raw_blocks:
        name = ''
        if name_base is not None and block.name_offset > 0:
            name = _cstring(data, name_base + block.name_offset)
        blocks.append(
            Block(
                block.table_index, block.index, block.offset, block.name_offset,
                block.type, block.size, name,
            )
        )
    return blocks


def _node_matrix(data: bytes, block: Block) -> tuple[np.ndarray, int]:
    values = []
    pos = block.offset
    for _ in range(4):
        values.append(struct.unpack_from('<3f', data, pos))
        pos += 16
    local = np.array(
        (
            (values[0][0], values[1][0], values[2][0], values[3][0]),
            (values[0][1], values[1][1], values[2][1], values[3][1]),
            (values[0][2], values[1][2], values[2][2], values[3][2]),
            (0, 0, 0, 1),
        ),
        dtype=np.float32,
    )
    parent_id = _s32(data, block.offset + 164)
    return _CONVERSION @ local @ _CONVERSION_INV, parent_id


def _read_vertices(data: bytes, submesh: _Submesh) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    vertices: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    pos = submesh.vertex_offset

    for _ in range(submesh.vertex_count):
        start = pos
        if submesh.mask1 & 1:
            x, y, z = struct.unpack_from('<3f', data, pos)
            vertices.append((x, -z, y))
            pos += 12
        if submesh.mask1 & 4:
            x, y, z = struct.unpack_from('<3f', data, pos)
            normals.append((x, -z, y))
            pos += 12
        if submesh.mask1 & 16384:
            pos += 4
            if submesh.mixed:
                pos += 24
        if submesh.mask1 & 2:
            pos += 4
        if submesh.mask1 & 8:
            u, v = struct.unpack_from('<2f', data, pos)
            uvs.append((u, 1.0 - v))
            pos += 8
        if submesh.mask1 & 16:
            pos += 8
        if submesh.mask1 & 128:
            pos += 36
        if submesh.mask1 & 4096:
            pos += 8
        if submesh.mask1 & 8192:
            x, y, z = struct.unpack_from('<3f', data, pos)
            if not vertices or len(vertices) <= len(uvs):
                vertices.append((x, -z, y))
            pos += 12
        if submesh.mask2 & 32:
            pos += 8
        if submesh.mask2 & 64:
            pos += 16

        consumed = pos - start
        if consumed > submesh.stride:
            raise ValueError(
                f'vertex mask consumes {consumed} bytes but stride is {submesh.stride}'
            )
        pos = start + submesh.stride

    vertex_array = np.asarray(vertices, dtype=np.float32)
    if len(vertex_array) != submesh.vertex_count:
        raise ValueError(
            f'vertex buffer yielded {len(vertex_array)} of {submesh.vertex_count} vertices'
        )
    normal_array = np.asarray(normals, dtype=np.float32)
    if len(normal_array) != len(vertex_array):
        normal_array = np.zeros_like(vertex_array)
    uv_array = np.asarray(uvs, dtype=np.float32)
    if len(uv_array) != len(vertex_array):
        uv_array = np.zeros((len(vertex_array), 2), dtype=np.float32)
    return vertex_array, normal_array, uv_array


def _read_indices(data: bytes, submesh: _Submesh) -> np.ndarray:
    end = submesh.index_offset + submesh.index_count * 2
    if end > len(data):
        raise ValueError('index buffer extends beyond the ARCC container')
    indices = np.frombuffer(
        data, dtype='<u2', count=submesh.index_count, offset=submesh.index_offset
    ).astype(np.uint32)
    if not len(indices):
        return indices
    indices -= int(indices.min())
    triangles = indices[: len(indices) // 3 * 3].reshape(-1, 3)
    valid = (
        (triangles < submesh.vertex_count).all(axis=1)
        & (triangles[:, 0] != triangles[:, 1])
        & (triangles[:, 1] != triangles[:, 2])
        & (triangles[:, 0] != triangles[:, 2])
    )
    # Match the Blender importer winding conversion.
    return triangles[valid][:, ::-1].reshape(-1).astype(np.uint32)


def _transform(
    vertices: np.ndarray, normals: np.ndarray, matrix: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    homogeneous = np.column_stack(
        (vertices, np.ones(len(vertices), dtype=np.float32))
    )
    vertices = (matrix @ homogeneous.T).T[:, :3].astype(np.float32)
    if len(normals):
        normals = (matrix[:3, :3] @ normals.T).T
        lengths = np.linalg.norm(normals, axis=1)
        good = lengths > 1e-8
        normals[good] /= lengths[good, None]
    return vertices, normals.astype(np.float32)


def _parse_materials(data: bytes, blocks: list[Block]) -> list[MeshMaterial]:
    """Decode material-to-texture bindings used by stock Eutechnyx meshes."""
    texture_names = [
        block.name or f'texture_{index}'
        for index, block in enumerate(block for block in blocks if block.type in (1, 52))
    ]
    materials: list[MeshMaterial] = []
    for index, block in enumerate(block for block in blocks if block.type == 2):
        bindings: list[MaterialTextureBinding] = []
        if block.size >= 64:
            property_count = _u32(data, block.offset + 40)
            texture_count = _u32(data, block.offset + 44)
            records_size = texture_count * 24
            cursor = block.offset + 64 + property_count * 20
            cursor += max(0, block.size - 64 - property_count * 20 - records_size)
            records_end = cursor + texture_count * 20
            if records_end + texture_count * 4 <= block.offset + block.size:
                for texture_index in range(texture_count):
                    role = _u8(data, cursor + texture_index * 20)
                    name_id = _s32(data, records_end + texture_index * 4)
                    if 0 <= name_id < len(texture_names):
                        bindings.append(MaterialTextureBinding(role, texture_names[name_id]))
        materials.append(
            MeshMaterial(block.name or f'material_{index}', tuple(bindings))
        )
    return materials


def load_mesh(
    data: bytes, source_name: str = '', *, alternative: int = 0,
    include_helpers: bool = False,
) -> MeshScene:
    blocks = _parse_blocks(data)
    by_type: dict[int, list[Block]] = {}
    by_index: dict[int, Block] = {}
    for block in blocks:
        by_type.setdefault(block.type, []).append(block)
        by_index[block.index] = block

    mesh_blocks = {block.index: block for block in by_type.get(9, [])}
    vertex_blocks = {block.index: block for block in by_type.get(16, [])}
    index_blocks = {block.index: block for block in by_type.get(15, [])}
    material_records = _parse_materials(data, blocks)
    material_names = [material.name for material in material_records]

    hierarchy: list[np.ndarray] = []
    attachment_points: dict[str, np.ndarray] = {}
    selected: list[tuple[int, np.ndarray, str]] = []
    for block in blocks:
        if block.type not in (28, 29, 37):
            continue
        local, parent_id = _node_matrix(data, block)
        world = hierarchy[parent_id] @ local if 0 <= parent_id < len(hierarchy) else local
        hierarchy.append(world)
        if block.type in (28, 37) and block.name:
            # Pure attachment nodes store their renderer-space translation in
            # the fourth on-disk matrix column. Alloy01-AP through Alloy04-AP
            # are the game's exact wheel centers.
            attachment_points[block.name] = np.asarray(
                struct.unpack_from('<3f', data, block.offset + 48),
                dtype=np.float32,
            )
        if block.type != 29:
            continue
        mesh_id = _s32(data, block.offset + 176)
        if mesh_id not in mesh_blocks:
            continue
        mesh_block = mesh_blocks[mesh_id]
        next_index = mesh_block.table_index + 1
        lod_ids: list[int] = []
        if next_index < len(blocks) and blocks[next_index].type == 82:
            group = blocks[next_index]
            count = _u32(data, group.offset + 28)
            cursor = group.offset + 32
            alternatives = []
            for _ in range(count):
                values = struct.unpack_from('<8i', data, cursor)
                cursor += 32
                alternatives.append(values[0])
            if alternatives:
                slot = min(max(0, alternative), len(alternatives) - 1)
                lod_id = alternatives[slot]
                if lod_id < 0:
                    lod_id = next((value for value in alternatives if value >= 0), mesh_id)
                lod_ids.append(lod_id)
        else:
            lod_ids.append(mesh_id)
        for lod_id in lod_ids:
            if lod_id in mesh_blocks:
                selected.append((lod_id, world, block.name or f'node_{block.index}'))

    parts: list[MeshPart] = []
    seen_instances: set[tuple[int, bytes]] = set()
    for mesh_id, matrix, node_name in selected:
        instance_key = (mesh_id, matrix.tobytes())
        if instance_key in seen_instances:
            continue
        seen_instances.add(instance_key)
        mesh = mesh_blocks[mesh_id]
        cursor = mesh.offset
        prefix = _u32(data, cursor)
        cursor += 4
        if prefix > 2:
            cursor += 4
        cursor += 24
        submesh_count = _u32(data, cursor)
        cursor += 4

        for sub_index in range(submesh_count):
            material_id, ib_id, vb_id = struct.unpack_from('<3i', data, cursor)
            cursor += 12
            cursor += 16
            range_count = _u8(data, cursor)
            cursor += 4
            ranges = []
            for _ in range(range_count):
                _unknown, index_start, index_count, vertex_start, vertex_count = (
                    struct.unpack_from('<5I', data, cursor)
                )
                cursor += 20
                ranges.append((index_start, index_count, vertex_start, vertex_count))
            cursor += 24

            if vb_id not in vertex_blocks or ib_id not in index_blocks:
                continue
            vb = vertex_blocks[vb_id]
            ib = index_blocks[ib_id]
            total_vertices, stride = struct.unpack_from('<2I', data, vb.offset)
            mask1, mask2 = struct.unpack_from('<2H', data, vb.offset + 8)
            if vb.size != total_vertices * stride + 12:
                continue
            for range_index, (index_start, index_count, vertex_start, vertex_count) in enumerate(ranges):
                submesh = _Submesh(
                    mesh_id, material_id, vertex_count, index_count,
                    mask1, mask2, stride,
                    vb.offset + 12 + vertex_start * stride,
                    ib.offset + 4 + index_start * 2,
                )
                vertices, normals, uvs = _read_vertices(data, submesh)
                indices = _read_indices(data, submesh)
                if not len(vertices) or not len(indices):
                    continue
                vertices, normals = _transform(vertices, normals, matrix)
                material_name = (
                    material_names[material_id]
                    if 0 <= material_id < len(material_names)
                    else f'material_{material_id}'
                )
                if not include_helpers and (
                    material_name.casefold().startswith(('ghost-', 'draftwake'))
                    or material_name.casefold().startswith('overheat')
                    or node_name.casefold().startswith(('draft', 'convexhull', 'overheat'))
                    or 'dmg_hidden' in (mesh.name or '').casefold()
                ):
                    continue
                parts.append(
                    MeshPart(
                        name=f'{node_name}/{mesh.name or mesh_id}/{sub_index}:{range_index}',
                        material_name=material_name,
                        vertices=vertices,
                        normals=normals,
                        uvs=uvs,
                        indices=indices,
                    )
                )

    if not parts:
        raise ValueError('no renderable LOD0 mesh parts were found')
    return MeshScene(
        parts=parts,
        source_name=source_name,
        attachment_points=attachment_points,
        materials={material.name: material for material in material_records},
    )


def assemble_car_scene(body: MeshScene, wheel: MeshScene | None = None) -> MeshScene:
    """Place the game's one-corner alloy mesh on its four authored anchors."""
    parts = list(body.parts)
    if wheel is None:
        return MeshScene(
            parts, body.source_name, dict(body.attachment_points),
            dict(body.materials), dict(body.texture_images),
        )

    wheel_low, wheel_high = wheel.bounds
    source_center = ((wheel_low + wheel_high) * 0.5).astype(np.float32)
    anchors = {name.casefold(): point for name, point in body.attachment_points.items()}
    authored = (
        ('front_right', 'alloy01-ap', False),
        ('front_left', 'alloy02-ap', True),
        ('rear_right', 'alloy03-ap', False),
        ('rear_left', 'alloy04-ap', True),
    )
    if all(name in anchors for _corner, name, _mirrored in authored):
        placements = [
            (corner, anchors[name].astype(np.float32), mirrored)
            for corner, name, mirrored in authored
        ]
    else:
        # Older/custom containers without attachment metadata use the verified
        # stock wheelbase instead of an unreliable body-bounds symmetry guess.
        rear = source_center + np.asarray((0.0, 2.7866, 0.0), dtype=np.float32)
        placements = (
            ('front_right', source_center, False),
            ('front_left', source_center * np.asarray((-1, 1, 1)), True),
            ('rear_right', rear, False),
            ('rear_left', rear * np.asarray((-1, 1, 1)), True),
        )

    for corner, target, mirrored in placements:
        placed_center = source_center.copy()
        if mirrored:
            placed_center[0] *= -1
        shift = target - placed_center
        for part in wheel.parts:
            vertices = part.vertices.copy()
            normals = part.normals.copy()
            indices = part.indices.copy()
            if mirrored:
                vertices[:, 0] *= -1
                normals[:, 0] *= -1
                indices = indices.reshape(-1, 3)[:, ::-1].reshape(-1).copy()
            vertices += shift
            parts.append(
                MeshPart(
                    name=f'{corner}/{part.name}',
                    material_name=part.material_name,
                    vertices=vertices,
                    normals=normals,
                    uvs=part.uvs.copy(),
                    indices=indices,
                )
            )
    materials = dict(body.materials)
    materials.update(wheel.materials)
    texture_images = dict(body.texture_images)
    texture_images.update(wheel.texture_images)
    return MeshScene(
        parts, body.source_name, dict(body.attachment_points),
        materials, texture_images,
    )


def load_mesh_file(path: str) -> MeshScene:
    with open(path, 'rb') as handle:
        return load_mesh(handle.read(), source_name=path)
