bl_info = {
    "name": "Eutechnyx Mesh Importer",
    "author": "Chipicao (original MaxScript); Blender port",
    "version": (1, 1, 1),
    "blender": (3, 0, 0),
    "location": "File > Import > Eutechnyx ARC (.arc)",
    "description": "Imports Eutechnyx ARC mesh archives (Nascar, ACR, etc.)",
    "category": "Import-Export",
}

import os
import struct
import time
import bpy
from mathutils import Matrix
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator, AddonPreferences
from bpy_extras.io_utils import ImportHelper


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------
def _ru8(f):  return f.read(1)[0]
def _ru16(f): return struct.unpack('<H', f.read(2))[0]
def _ru32(f): return struct.unpack('<I', f.read(4))[0]
def _rs32(f): return struct.unpack('<i', f.read(4))[0]
def _rf32(f): return struct.unpack('<f', f.read(4))[0]


def _rcstr(f):
    out = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b'\x00':
            break
        out += b
    return out.decode('ascii', errors='replace')


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------
class _DataBlock:
    __slots__ = ('index', 'offset', 'nameOffset', 'type', 'size', 'name')
    def __init__(self, index, offset, nameOffset, type_, size):
        self.index = index
        self.offset = offset
        self.nameOffset = nameOffset
        self.type = type_
        self.size = size
        self.name = None


class _SubmeshData:
    __slots__ = ('meshID', 'matID', 'numverts', 'numinds',
                 'mask1', 'mask2', 'stride', 'VBoffset', 'IBoffset', 'mixed')
    def __init__(self, meshID, matID, numverts, numinds,
                 mask1, mask2, stride, VBoffset, IBoffset, mixed):
        self.meshID = meshID
        self.matID = matID
        self.numverts = numverts
        self.numinds = numinds
        self.mask1 = mask1
        self.mask2 = mask2
        self.stride = stride
        self.VBoffset = VBoffset
        self.IBoffset = IBoffset
        self.mixed = mixed


class _MeshNode:
    __slots__ = ('index', 'mesh', 'submeshes')
    def __init__(self, index):
        self.index = index
        self.mesh = None
        self.submeshes = []


# LH Y-up -> RH Z-up: (x, y, z) -> (x, -z, y)
_CONV = Matrix((
    (1.0, 0.0,  0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0,  0.0, 0.0),
    (0.0, 0.0,  0.0, 1.0),
))
_CONV_INV = _CONV.inverted()


# ---------------------------------------------------------------------------
# Add-on Preferences
# ---------------------------------------------------------------------------
class EutechnyxAddonPrefs(AddonPreferences):
    bl_idname = __name__

    default_texture_path: StringProperty(
        name="Default Texture Path",
        description=("Folder to look in first for .dds textures. Used when the "
                     "import dialog's 'Texture Path' field is empty."),
        default="",
        subtype='DIR_PATH',
    )

    recursive_search: BoolProperty(
        name="Recursive texture search",
        description=("If a texture is not found in the explicit paths, scan "
                     "the model folder recursively by filename"),
        default=True,
    )

    verbose: BoolProperty(
        name="Verbose logging",
        description="Print detailed [EMI] messages to the system console",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "default_texture_path")
        layout.prop(self, "recursive_search")
        layout.prop(self, "verbose")
        layout.label(
            text="Tip: open the System Console (Window menu) to see import logs.",
            icon='INFO')


def _get_prefs():
    try:
        return bpy.context.preferences.addons[__name__].preferences
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------
class _ArcImporter:
    def __init__(self, tex_path, lods, split_mesh,
                 fallback_tex_path="", recursive_search=True, verbose=True):
        self.tex_path = tex_path
        self.fallback_tex_path = fallback_tex_path
        self.recursive_search = recursive_search
        self.verbose = verbose
        self.lods = lods
        self.split_mesh = split_mesh
        self.model_path = ""
        self._image_cache = {}
        self._dds_index = None

    def _log(self, msg):
        if self.verbose:
            print(f"[EMI] {msg}")

    # ---------------------------------------------------------- nodes
    def _read_node(self, f, name, hierarchy):
        r1 = (_rf32(f), _rf32(f), _rf32(f)); f.seek(4, 1)
        r2 = (_rf32(f), _rf32(f), _rf32(f)); f.seek(4, 1)
        r3 = (_rf32(f), _rf32(f), _rf32(f)); f.seek(4, 1)
        r4 = (_rf32(f), _rf32(f), _rf32(f)); f.seek(4, 1)

        local_m = Matrix((
            (r1[0], r2[0], r3[0], r4[0]),
            (r1[1], r2[1], r3[1], r4[1]),
            (r1[2], r2[2], r3[2], r4[2]),
            (0.0,   0.0,   0.0,   1.0),
        ))
        local_m = _CONV @ local_m @ _CONV_INV

        f.seek(64, 1)
        f.seek(36, 1)
        parent_id = _rs32(f)
        _ = _ru8(f); _ = _ru8(f); _ = _ru8(f); _ = _ru8(f)
        _ = _ru32(f)

        obj = bpy.data.objects.new(name or "node", None)
        obj.empty_display_type = 'CUBE'
        obj.empty_display_size = 0.1
        bpy.context.collection.objects.link(obj)

        if 0 <= parent_id < len(hierarchy) and hierarchy[parent_id] is not None:
            parent_obj = hierarchy[parent_id]
            obj.parent = parent_obj
            obj.matrix_world = parent_obj.matrix_world @ local_m
        else:
            obj.matrix_world = local_m

        hierarchy.append(obj)
        return obj

    # ---------------------------------------------------- vertex buffer
    def _parse_vb(self, f, sub):
        verts, normals, colors, t1, t2, t3 = [], [], [], [], [], []
        m1, m2, mixed, stride = sub.mask1, sub.mask2, sub.mixed, sub.stride
        f.seek(sub.VBoffset)
        for _ in range(sub.numverts):
            if (m1 & 1) == 1:
                x = _rf32(f); y = _rf32(f); z = _rf32(f)
                verts.append((x, -z, y))
            if (m1 & 4) == 4:
                x = _rf32(f); y = _rf32(f); z = _rf32(f)
                normals.append((x, -z, y))
            if (m1 & 16384) == 16384:
                f.seek(4, 1)
                if mixed:
                    f.seek(24, 1)
            if (m1 & 2) == 2:
                r = _ru8(f); g = _ru8(f); b = _ru8(f); a = _ru8(f)
                colors.append((r / 255.0, g / 255.0, b / 255.0, a / 255.0))
            if (m1 & 8) == 8:
                x = _rf32(f); y = _rf32(f)
                t1.append((x, 1.0 - y))
            if (m1 & 16) == 16:
                x = _rf32(f); y = _rf32(f)
                t2.append((x, 1.0 - y))
            if (m1 & 128) == 128:
                f.seek(36, 1)
            if (m1 & 4096) == 4096:
                x = _rf32(f); y = _rf32(f)
                t3.append((x, 1.0 - y))
            if (m1 & 8192) == 8192:
                x = _rf32(f); y = _rf32(f); z = _rf32(f)
                verts.append((x, -z, y))
                f.seek(stride - 12, 1)
            if (m2 & 32) == 32:
                f.seek(8, 1)
            if (m2 & 64) == 64:
                f.seek(16, 1)
        return verts, normals, colors, t1, t2, t3

    # -------------------------------------------------- build mesh
    def _build_mesh(self, f, sub, material):
        verts, normals, colors, t1, t2, t3 = self._parse_vb(f, sub)
        f.seek(sub.IBoffset)
        raw = f.read(sub.numinds * 2)
        if len(raw) < sub.numinds * 2:
            return None
        indices = list(struct.unpack('<' + 'H' * sub.numinds, raw))
        if not indices:
            return None

        pad = min(indices)
        max_v = len(verts) - 1
        faces = []
        for i in range(len(indices) // 3):
            a = indices[i * 3]     - pad
            b = indices[i * 3 + 1] - pad
            c = indices[i * 3 + 2] - pad
            if 0 <= a <= max_v and 0 <= b <= max_v and 0 <= c <= max_v \
                    and len({a, b, c}) == 3:
                faces.append((c, b, a))

        if not verts or not faces:
            return None

        mesh = bpy.data.meshes.new("submesh")
        try:
            mesh.from_pydata(verts, [], faces)
        except Exception as e:
            self._log(f"from_pydata failed: {e}")
            bpy.data.meshes.remove(mesh)
            return None
        mesh.validate(clean_customdata=False)
        mesh.update()

        def _fill_uv(layer_name, data):
            if not data:
                return
            uv = mesh.uv_layers.new(name=layer_name)
            for poly in mesh.polygons:
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    if vi < len(data):
                        uv.data[li].uv = data[vi]
        _fill_uv("UV1", t1)
        _fill_uv("UV2", t2)
        _fill_uv("UV3", t3)

        if colors:
            try:
                col = mesh.color_attributes.new(
                    name="Color", type='BYTE_COLOR', domain='CORNER')
                for poly in mesh.polygons:
                    for li in poly.loop_indices:
                        vi = mesh.loops[li].vertex_index
                        if vi < len(colors):
                            col.data[li].color = colors[vi]
            except Exception as e:
                self._log(f"Vertex colors failed: {e}")

        if normals and len(normals) == len(verts):
            try:
                mesh.normals_split_custom_set_from_vertices(normals)
            except Exception as e:
                self._log(f"Custom normals failed: {e}")

        if material is not None:
            mesh.materials.append(material)

        mesh["imported_with"] = "Eutechnyx Mesh Importer"
        return mesh

    # ---------------------------------------------------------- textures
    def _build_dds_index(self):
        if self._dds_index is not None:
            return
        self._dds_index = {}
        if not self.model_path or not os.path.isdir(self.model_path):
            return
        self._log(f"Scanning '{self.model_path}' recursively for .dds files...")
        count = 0
        for root, _dirs, files in os.walk(self.model_path):
            for fn in files:
                if fn.lower().endswith('.dds'):
                    key = fn.lower()
                    if key not in self._dds_index:
                        self._dds_index[key] = os.path.join(root, fn)
                        count += 1
        self._log(f"Indexed {count} .dds files.")

    def _find_texture(self, tex_name, arc_file):
        base = os.path.splitext(os.path.basename(tex_name))[0] + ".dds"

        candidates = []
        if self.tex_path:
            candidates.append(os.path.join(self.tex_path, base))
        if self.fallback_tex_path:
            candidates.append(os.path.join(self.fallback_tex_path, base))
        candidates.append(os.path.join(self.model_path, base))

        arc_base = os.path.splitext(os.path.basename(arc_file))[0]
        candidates.append(os.path.join(self.model_path, arc_base, base))
        sub = arc_base.split('_')[0]
        candidates.append(os.path.join(self.model_path, sub + "_TEXTURES_X", base))

        for c in candidates:
            if os.path.isfile(c):
                return c

        if self.recursive_search:
            self._build_dds_index()
            hit = self._dds_index.get(base.lower()) if self._dds_index else None
            if hit:
                self._log(f"Recursive search resolved '{base}' -> {hit}")
                return hit

        return None

    def _load_image(self, path):
        if path in self._image_cache:
            return self._image_cache[path]
        try:
            img = bpy.data.images.load(path, check_existing=True)
            if img.size[0] == 0 and img.size[1] == 0:
                try:
                    img.reload()
                except Exception:
                    pass
        except Exception as e:
            self._log(f"Cannot load image '{path}': {e}")
            img = None
        self._image_cache[path] = img
        return img

    def _get_bsdf(self, node_tree):
        for n in node_tree.nodes:
            if n.bl_idname == 'ShaderNodeBsdfPrincipled':
                return n
        return node_tree.nodes.new('ShaderNodeBsdfPrincipled')

    def _get_output(self, node_tree):
        for n in node_tree.nodes:
            if n.bl_idname == 'ShaderNodeOutputMaterial':
                return n
        return node_tree.nodes.new('ShaderNodeOutputMaterial')

    def _create_material(self, name, tex_entries, arc_file):
        mat = bpy.data.materials.new(name=name or "material")
        mat.use_nodes = True
        nt = mat.node_tree
        nodes, links = nt.nodes, nt.links

        bsdf = self._get_bsdf(nt)
        out = self._get_output(nt)
        if not any(l.to_node == out and l.from_node == bsdf for l in links):
            try:
                links.new(bsdf.outputs[0], out.inputs[0])
            except Exception:
                pass

        if not tex_entries:
            self._log(f"Material '{name}': no texture entries found in block.")
            return mat

        entry_desc = ", ".join(f"({t},{n})" for t, n in tex_entries)
        self._log(f"Material '{name}': {len(tex_entries)} texture slot(s): {entry_desc}")

        type_names = {0: 'diffuse', 1: 'normal', 2: 'specular',
                      3: 'sphere', 4: 'ao'}

        y = 400
        attached = 0
        for tex_type, tex_name in tex_entries:
            path = self._find_texture(tex_name, arc_file)
            if path is None:
                self._log(f"  - MISSING: {tex_name} (type {tex_type})")
                continue
            img = self._load_image(path)
            if img is None:
                self._log(f"  - FAILED to load: {path}")
                continue

            kind = type_names.get(tex_type, f"type{tex_type}")
            self._log(f"  + {kind}: {path}")

            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-600, y)
            y -= 320
            attached += 1

            if tex_type == 0:
                try:
                    links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                except Exception:
                    pass
                if 'Alpha' in bsdf.inputs:
                    try:
                        links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
                    except Exception:
                        pass
                try:
                    mat.blend_method = 'HASHED'
                except Exception:
                    pass
            elif tex_type == 1:
                try:
                    img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                nm = nodes.new('ShaderNodeNormalMap')
                nm.location = (-300, y + 320)
                try:
                    links.new(tex_node.outputs['Color'], nm.inputs['Color'])
                    if 'Normal' in bsdf.inputs:
                        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])
                except Exception:
                    pass
            elif tex_type == 2:
                try:
                    img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                for key in ('Specular IOR Level', 'Specular', 'Roughness'):
                    if key in bsdf.inputs:
                        try:
                            links.new(tex_node.outputs['Color'], bsdf.inputs[key])
                        except Exception:
                            pass
                        break

        if attached == 0:
            self._log(f"Material '{name}': no textures were attached.")
        return mat

    # ------------------------------------------------------------ main
    def read_arc(self, arc_file):
        self.model_path = os.path.dirname(arc_file) + os.sep
        self._dds_index = None

        with open(arc_file, 'rb') as f:
            magic = f.read(4)
            try:
                file_type = magic.decode('ascii')
            except Exception:
                file_type = ''

            if file_type == 'ARCE':
                raise RuntimeError("Archive needs to be decompressed first (ARCE).")
            if file_type != 'ARCC':
                raise RuntimeError(f"Unknown file type: {file_type!r}")

            num_blocks = _ru32(f)
            f.seek(128)
            start_offset = num_blocks * 16 + 128

            blockArr = []
            meshArr  = {}
            IBarr    = {}
            VBarr    = {}
            texArr   = []
            matArr   = []
            hierarchy = []
            namesOffset = 0

            for i in range(num_blocks):
                index        = _rs32(f)
                block_offset = _ru32(f)
                name_offset  = _ru32(f)
                block_type   = _ru8(f)
                f.seek(-1, 1)
                raw_size = _ru32(f)
                s0 = (raw_size >> 8)  & 0xFF
                s1 = (raw_size >> 16) & 0xFF
                s2 = (raw_size >> 24) & 0xFF
                block_size = (s0 << 16) | (s1 << 8) | s2

                blk = _DataBlock(index, block_offset + start_offset,
                                 name_offset, block_type, block_size)
                blockArr.append(blk)

                if block_type == 9:
                    meshArr[index] = _MeshNode(i)
                elif block_type == 15:
                    IBarr[index] = i
                elif block_type == 16:
                    VBarr[index] = i
                elif block_type == 253:
                    namesOffset = block_offset

            for i, blk in enumerate(blockArr):
                if blk.nameOffset > 0:
                    blk.nameOffset += namesOffset + start_offset
                    f.seek(blk.nameOffset)
                    blk.name = _rcstr(f)

                f.seek(blk.offset)
                bt = blk.type

                if bt in (1, 52):
                    texArr.append(blk.name or f"tex_{i}")

                elif bt == 119:
                    meshID = _rs32(f)
                    _matID = _rs32(f)
                    _id3   = _rs32(f)
                    numv   = _ru32(f)
                    numi   = _ru32(f)
                    mask1  = _ru16(f)
                    mask2  = _ru16(f)
                    stride = _ru32(f)
                    _ = _ru32(f); _ = _ru32(f)
                    VBoff = f.tell()
                    f.seek(numv * stride, 1)
                    IBoff = f.tell()

                    if blk.size != (numv * stride + numi * 2 + 36):
                        self._log(f"VB #{blk.index} @{blk.offset} is zblib compressed, skipping.")
                    elif meshID >= 0 and meshID in meshArr:
                        meshArr[meshID].submeshes.append(
                            _SubmeshData(meshID, _matID, numv, numi,
                                         mask1, mask2, stride, VBoff, IBoff, True))

                elif bt == 2:
                    f.seek(40, 1)
                    numProps = _ru32(f)
                    numTex   = _ru32(f)
                    f.seek(16, 1)
                    for _ in range(numProps):
                        f.seek(20, 1)
                    toseek = blk.size - 64 - numProps * 20 - numTex * 24
                    if toseek > 0:
                        f.seek(toseek, 1)

                    tex_entries = []
                    for t in range(numTex):
                        tex_type = _ru8(f)
                        f.seek(11, 1)
                        _ = _ru32(f)
                        _ = _ru32(f)
                        savepos = f.tell()
                        f.seek((numTex - (t + 1)) * 20 + t * 4, 1)
                        tex_id = _rs32(f)
                        f.seek(savepos)
                        if 0 <= tex_id < len(texArr):
                            tex_entries.append((tex_type, texArr[tex_id]))

                    mat = self._create_material(blk.name or f"mat_{i}",
                                                tex_entries, arc_file)
                    matArr.append(mat)

            for i, blk in enumerate(blockArr):
                f.seek(blk.offset)
                bt = blk.type

                if bt == 28 or bt == 37:
                    self._read_node(f, blk.name or f"node_{i}", hierarchy)

                elif bt == 29:
                    adummy = self._read_node(f, blk.name or f"node_{i}", hierarchy)
                    mesh_id = _rs32(f)
                    f.seek(44, 1)

                    LODarr = []
                    if mesh_id >= 0 and mesh_id in meshArr:
                        mi = meshArr[mesh_id].index
                        grp = blockArr[mi + 1] if (mi + 1) < len(blockArr) else None
                        if grp is not None and grp.type == 82:
                            saved = f.tell()
                            f.seek(grp.offset)
                            f.seek(28, 1)
                            numAlt = _ru32(f)
                            for _ in range(numAlt):
                                lods = [_rs32(f) for _ in range(8)]
                                for idx in range(7):
                                    if self.lods[idx]:
                                        LODarr.append(lods[idx])
                            f.seek(saved)
                        else:
                            if self.lods[0]:
                                LODarr.append(mesh_id)

                    for alod in LODarr:
                        if alod < 0 or alod not in meshArr:
                            continue
                        theMesh = meshArr[alod]

                        if theMesh.mesh is not None:
                            inst = bpy.data.objects.new(theMesh.mesh.name, theMesh.mesh)
                            bpy.context.collection.objects.link(inst)
                            inst.parent = adummy
                            inst.matrix_world = adummy.matrix_world.copy()
                            continue

                        block_mesh = blockArr[theMesh.index]
                        f.seek(block_mesh.offset)
                        along = _ru32(f)
                        if along > 2:
                            f.seek(4, 1)
                        f.seek(24, 1)
                        subMeshCount = _ru32(f)

                        submeshArr = list(theMesh.submeshes)

                        for _s in range(subMeshCount):
                            matID = _rs32(f)
                            IBind = _rs32(f)
                            VBind = _rs32(f)
                            _ = _ru32(f); _ = _ru32(f)
                            _ = _ru32(f); _ = _ru32(f)
                            f1 = _ru8(f); _f2 = _ru8(f); _f3 = _ru8(f); _f4 = _ru8(f)

                            if (IBind >= 0 and VBind >= 0
                                    and VBind in VBarr and IBind in IBarr):
                                sp2 = f.tell()
                                theVB = blockArr[VBarr[VBind]]
                                f.seek(theVB.offset)
                                totalVerts = _ru32(f)
                                stride     = _ru32(f)
                                mask1      = _ru16(f)
                                mask2      = _ru16(f)
                                f.seek(sp2)

                                if theVB.size != (totalVerts * stride + 12):
                                    self._log(f"VB #{theVB.index} @{theVB.offset} "
                                              "is zblib compressed, skipping.")
                                    f.seek(f1 * 20, 1)
                                else:
                                    theIB = blockArr[IBarr[IBind]]
                                    for _ in range(f1):
                                        _cz      = _ru32(f)
                                        IBoffset = _ru32(f)
                                        numinds  = _ru32(f)
                                        VBoffset = _ru32(f)
                                        numverts = _ru32(f)
                                        VBoffset = VBoffset * stride + theVB.offset + 12
                                        IBoffset = IBoffset * 2 + theIB.offset + 4
                                        submeshArr.append(_SubmeshData(
                                            alod, matID, numverts, numinds,
                                            mask1, mask2, stride,
                                            VBoffset, IBoffset, False))
                            else:
                                f.seek(f1 * 20, 1)
                            f.seek(24, 1)

                        apply_trans = True
                        built_objs = []
                        base_name = block_mesh.name or f"mesh_{theMesh.index}"
                        for sd in submeshArr:
                            mat = None
                            if 0 <= sd.matID < len(matArr):
                                mat = matArr[sd.matID]
                            sub_mesh = self._build_mesh(f, sd, mat)
                            if sd.mixed:
                                apply_trans = False
                            if sub_mesh is None:
                                continue
                            nm = base_name
                            if mat is not None:
                                nm = f"{base_name}_{mat.name}"
                            sub_mesh.name = nm
                            obj = bpy.data.objects.new(nm, sub_mesh)
                            bpy.context.collection.objects.link(obj)
                            built_objs.append(obj)

                        if not self.split_mesh and len(built_objs) > 1:
                            joined = self._join_objects(built_objs, base_name)
                            if joined is not None:
                                joined.parent = adummy
                                if apply_trans:
                                    joined.matrix_world = adummy.matrix_world.copy()
                                else:
                                    joined.matrix_world = Matrix.Identity(4)
                                theMesh.mesh = joined.data
                        else:
                            for obj in built_objs:
                                obj.parent = adummy
                                if apply_trans:
                                    obj.matrix_world = adummy.matrix_world.copy()
                                else:
                                    obj.matrix_world = Matrix.Identity(4)
                            if len(built_objs) == 1:
                                theMesh.mesh = built_objs[0].data

    # ------------------------------------------------------------ join util
    def _join_objects(self, objs, final_name):
        if not objs:
            return None
        if len(objs) == 1:
            objs[0].name = final_name
            objs[0].data.name = final_name
            return objs[0]
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except Exception:
            pass
        target = objs[0]
        bpy.context.view_layer.objects.active = target
        for o in objs:
            try:
                o.select_set(True)
            except Exception:
                pass
        try:
            bpy.ops.object.join()
        except Exception as e:
            self._log(f"join() failed: {e}")
            return target
        target.name = final_name
        target.data.name = final_name
        return target


# ---------------------------------------------------------------------------
# Operator + menu
# ---------------------------------------------------------------------------
class IMPORT_OT_eutechnyx_arc(Operator, ImportHelper):
    bl_idname = "import_scene.eutechnyx_arc"
    bl_label = "Import Eutechnyx ARC"
    bl_description = "Import an Eutechnyx .arc mesh file"
    bl_options = {'UNDO'}

    filename_ext = ".arc"
    filter_glob: StringProperty(default="*.arc;*.ARC", options={'HIDDEN'})

    tex_path: StringProperty(
        name="Texture Path (override)",
        description=("Optional path to look in first for .dds textures. "
                     "Leave empty to use the default set in add-on Preferences."),
        default="",
    )

    load_lod0: BoolProperty(name="LOD0", default=True)
    load_lod1: BoolProperty(name="LOD1", default=False)
    load_lod2: BoolProperty(name="LOD2", default=False)
    load_lod3: BoolProperty(name="LOD3", default=False)
    load_lod4: BoolProperty(name="LOD4", default=False)
    load_lod5: BoolProperty(name="LOD5", default=False)
    load_lod6: BoolProperty(name="LOD6", default=False)
    split_mesh: BoolProperty(name="Split by material", default=False)
    hide_helpers: BoolProperty(name="Hide dummies", default=True)

    def draw(self, context):
        layout = self.layout
        prefs = _get_prefs()

        box = layout.box()
        box.label(text="Texture Path", icon='TEXTURE')
        box.prop(self, "tex_path", text="")
        if prefs is not None:
            default_text = prefs.default_texture_path or "(none)"
            row = box.row()
            row.enabled = False
            row.label(text=f"Default (from Preferences): {default_text}",
                      icon='PREFERENCES')

        box = layout.box()
        box.label(text="LOD options")
        row = box.row(align=True)
        row.prop(self, "load_lod0", toggle=True)
        row.prop(self, "load_lod1", toggle=True)
        row.prop(self, "load_lod2", toggle=True)
        row.prop(self, "load_lod3", toggle=True)
        row = box.row(align=True)
        row.prop(self, "load_lod4", toggle=True)
        row.prop(self, "load_lod5", toggle=True)
        row.prop(self, "load_lod6", toggle=True)

        layout.prop(self, "split_mesh")
        layout.prop(self, "hide_helpers")

    def execute(self, context):
        prefs = _get_prefs()
        fallback = ""
        recursive = True
        verbose = True
        if prefs is not None:
            fallback = bpy.path.abspath(prefs.default_texture_path) \
                if prefs.default_texture_path else ""
            recursive = prefs.recursive_search
            verbose = prefs.verbose

        tex_override = bpy.path.abspath(self.tex_path) if self.tex_path else ""

        lods = [self.load_lod0, self.load_lod1, self.load_lod2, self.load_lod3,
                self.load_lod4, self.load_lod5, self.load_lod6]

        importer = _ArcImporter(
            tex_path=tex_override,
            lods=lods,
            split_mesh=self.split_mesh,
            fallback_tex_path=fallback,
            recursive_search=recursive,
            verbose=verbose,
        )
        start = time.time()
        try:
            importer.read_arc(self.filepath)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}

        if self.hide_helpers:
            for obj in context.scene.objects:
                if obj.type == 'EMPTY':
                    try:
                        obj.hide_set(True)
                    except Exception:
                        pass
                    obj.hide_render = True

        elapsed = time.time() - start
        self.report({'INFO'},
                    f"Imported '{os.path.basename(self.filepath)}' in {elapsed:.2f}s")
        return {'FINISHED'}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_eutechnyx_arc.bl_idname,
                         text="Eutechnyx ARC (.arc)")


classes = (EutechnyxAddonPrefs, IMPORT_OT_eutechnyx_arc)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()