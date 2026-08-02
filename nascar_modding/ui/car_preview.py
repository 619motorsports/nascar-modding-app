"""Interactive OpenGL car preview for the native desktop application."""

from __future__ import annotations

import ctypes
import hashlib
import math

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QMatrix4x4, QVector3D
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from OpenGL import GL
from OpenGL.GL import shaders

from nascar_modding.formats.eutechnyx_mesh import MeshScene


_VERTEX_SHADER = """
#version 330 core
layout(location=0) in vec3 position;
layout(location=1) in vec3 normal;
layout(location=2) in vec2 uv;
layout(location=3) in vec3 color;
layout(location=4) in float textured;
uniform mat4 mvp;
uniform mat4 model;
out vec3 v_normal;
out vec2 v_uv;
out vec3 v_color;
out float v_textured;
void main() {
    gl_Position = mvp * vec4(position, 1.0);
    v_normal = mat3(model) * normal;
    v_uv = uv;
    v_color = color;
    v_textured = textured;
}
"""

_FRAGMENT_SHADER = """
#version 330 core
in vec3 v_normal;
in vec2 v_uv;
in vec3 v_color;
in float v_textured;
uniform sampler2D livery;
uniform bool has_livery;
uniform sampler2D game_texture;
uniform bool has_game_texture;
uniform float material_specular;
out vec4 fragment;
void main() {
    vec3 base = v_color;
    if (has_game_texture) {
        base = texture(game_texture, v_uv).rgb;
    }
    if (has_livery && v_textured > 0.5) {
        base = texture(livery, v_uv).rgb;
    }
    vec3 n = normalize(v_normal);
    vec3 light = normalize(vec3(-0.35, -0.45, 0.82));
    float diffuse = max(dot(n, light), 0.0);
    float rim = pow(1.0 - abs(n.z), 3.0) * 0.18;
    float shine = pow(max(dot(reflect(-light, n), vec3(0.0, 0.0, 1.0)), 0.0), 32.0);
    fragment = vec4(base * (0.28 + diffuse * 0.72) + rim + shine * material_specular, 1.0);
}
"""


def _material_color(name: str) -> tuple[float, float, float]:
    low = name.casefold()
    if 'glass' in low:
        return (0.16, 0.24, 0.31)
    if 'tyre' in low:
        return (0.035, 0.04, 0.045)
    if 'chrome' in low:
        return (0.48, 0.51, 0.55)
    if 'underbody' in low:
        return (0.11, 0.12, 0.13)
    digest = hashlib.blake2b(name.encode('utf-8'), digest_size=3).digest()
    return tuple(0.28 + component / 255 * 0.42 for component in digest)


def _is_paint_material(name: str) -> bool:
    low = name.casefold()
    return low in ('upg0', 'lod0-upg0') or low.startswith('lod0-upg0-')


def _calculated_normals(vertices: np.ndarray, indices: np.ndarray) -> np.ndarray:
    normals = np.zeros_like(vertices)
    triangles = indices.reshape(-1, 3)
    face = np.cross(
        vertices[triangles[:, 1]] - vertices[triangles[:, 0]],
        vertices[triangles[:, 2]] - vertices[triangles[:, 0]],
    )
    for column in range(3):
        np.add.at(normals, triangles[:, column], face)
    lengths = np.linalg.norm(normals, axis=1)
    good = lengths > 1e-8
    normals[good] /= lengths[good, None]
    normals[~good, 2] = 1.0
    return normals


class CarPreviewWidget(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._program = None
        self._vao = None
        self._vbo = None
        self._ebo = None
        self._texture = None
        self._game_textures = {}
        self._texture_sources = {}
        self._material_texture_keys = {}
        self._material_specular = {}
        self._batches = []
        self._vertex_data = None
        self._index_data = None
        self._index_count = 0
        self._has_livery = False
        self._texture_pixels = None
        self._yaw = math.radians(32)
        self._pitch = math.radians(18)
        self._radius = 7.4
        self._center = np.array((0.0, 0.0, 0.25), dtype=np.float32)
        self._last_mouse = None
        self._scene_radius = self._radius

    def initializeGL(self):
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_CULL_FACE)
        GL.glCullFace(GL.GL_BACK)
        self._program = shaders.compileProgram(
            shaders.compileShader(_VERTEX_SHADER, GL.GL_VERTEX_SHADER),
            shaders.compileShader(_FRAGMENT_SHADER, GL.GL_FRAGMENT_SHADER),
        )
        self._vao = GL.glGenVertexArrays(1)
        self._vbo = GL.glGenBuffers(1)
        self._ebo = GL.glGenBuffers(1)
        self._texture = GL.glGenTextures(1)
        self._upload_mesh()
        self._upload_texture()
        self._upload_game_textures()

    def resizeGL(self, width, height):
        GL.glViewport(0, 0, width, max(1, height))

    def paintGL(self):
        GL.glClearColor(0.035, 0.047, 0.063, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self._program or not self._index_count:
            return

        aspect = self.width() / max(1, self.height())
        projection = QMatrix4x4()
        projection.perspective(42.0, aspect, 0.05, 100.0)
        view = QMatrix4x4()
        horizontal = math.cos(self._pitch) * self._radius
        eye = QVector3D(
            math.sin(self._yaw) * horizontal,
            -math.cos(self._yaw) * horizontal,
            math.sin(self._pitch) * self._radius + float(self._center[2]),
        )
        target = QVector3D(*map(float, self._center))
        view.lookAt(eye, target, QVector3D(0, 0, 1))
        model = QMatrix4x4()
        mvp = projection * view * model

        GL.glUseProgram(self._program)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._program, 'mvp'), 1, GL.GL_FALSE,
            np.asarray(mvp.data(), dtype=np.float32),
        )
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._program, 'model'), 1, GL.GL_FALSE,
            np.asarray(model.data(), dtype=np.float32),
        )
        GL.glUniform1i(
            GL.glGetUniformLocation(self._program, 'has_livery'),
            int(self._has_livery),
        )
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._texture)
        GL.glUniform1i(GL.glGetUniformLocation(self._program, 'livery'), 0)
        GL.glUniform1i(GL.glGetUniformLocation(self._program, 'game_texture'), 1)
        GL.glBindVertexArray(self._vao)
        for index_offset, index_count, material_name in self._batches:
            key = self._material_texture_keys.get(material_name)
            texture_id = self._game_textures.get(key)
            GL.glUniform1i(
                GL.glGetUniformLocation(self._program, 'has_game_texture'),
                int(texture_id is not None),
            )
            GL.glUniform1f(
                GL.glGetUniformLocation(self._program, 'material_specular'),
                self._material_specular.get(material_name, 0.12),
            )
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id or 0)
            GL.glDrawElements(
                GL.GL_TRIANGLES, index_count, GL.GL_UNSIGNED_INT,
                ctypes.c_void_p(index_offset * 4),
            )
        GL.glBindVertexArray(0)

    def set_scene(self, scene: MeshScene):
        rows = []
        indices = []
        batches = []
        vertex_base = 0
        index_offset = 0
        for part in scene.parts:
            normals = part.normals
            if not len(normals) or not np.any(np.abs(normals) > 1e-8):
                normals = _calculated_normals(part.vertices, part.indices)
            color = np.tile(_material_color(part.material_name), (len(part.vertices), 1))
            textured = np.full(
                (len(part.vertices), 1), float(_is_paint_material(part.material_name)),
                dtype=np.float32,
            )
            rows.append(
                np.column_stack((part.vertices, normals, part.uvs, color, textured))
            )
            indices.append(part.indices + vertex_base)
            batches.append((index_offset, len(part.indices), part.material_name))
            index_offset += len(part.indices)
            vertex_base += len(part.vertices)
        self._vertex_data = np.ascontiguousarray(np.vstack(rows), dtype=np.float32)
        self._index_data = np.ascontiguousarray(np.concatenate(indices), dtype=np.uint32)
        self._index_count = len(self._index_data)
        self._batches = batches
        self._texture_sources = dict(scene.texture_images)
        self._material_texture_keys = {}
        self._material_specular = {}
        for name, material in scene.materials.items():
            diffuse = material.texture_for(0)
            if diffuse and diffuse.casefold() in self._texture_sources:
                self._material_texture_keys[name] = diffuse.casefold()
            low = name.casefold()
            self._material_specular[name] = (
                0.65 if material.texture_for(2, 12)
                else 0.5 if 'chrome' in low or 'glass' in low
                else 0.12
            )
        low, high = scene.bounds
        self._center = ((low + high) * 0.5).astype(np.float32)
        extent = float(np.linalg.norm(high - low))
        self._radius = max(4.5, extent * 1.18)
        self._scene_radius = self._radius
        if self.context() and self.context().isValid():
            self.makeCurrent()
            self._upload_mesh()
            self._upload_game_textures()
            self.doneCurrent()
        self.update()

    def set_livery(self, path: str):
        self.set_livery_image(Image.open(path))

    def set_livery_image(self, source: Image.Image):
        image = source.convert('RGBA').transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self._texture_pixels = (image.width, image.height, image.tobytes())
        self._has_livery = True
        if self.context() and self.context().isValid():
            self.makeCurrent()
            self._upload_texture()
            self.doneCurrent()
        self.update()

    def clear_livery(self):
        self._has_livery = False
        self.update()

    def reset_view(self):
        self._yaw = math.radians(32)
        self._pitch = math.radians(18)
        self._radius = self._scene_radius
        self.update()

    def _upload_mesh(self):
        if self._vertex_data is None or self._index_data is None or not self._vao:
            return
        GL.glBindVertexArray(self._vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        GL.glBufferData(
            GL.GL_ARRAY_BUFFER, self._vertex_data.nbytes,
            self._vertex_data, GL.GL_STATIC_DRAW,
        )
        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        GL.glBufferData(
            GL.GL_ELEMENT_ARRAY_BUFFER, self._index_data.nbytes,
            self._index_data, GL.GL_STATIC_DRAW,
        )
        stride = self._vertex_data.shape[1] * 4
        for location, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24), (3, 3, 32), (4, 1, 44)):
            GL.glEnableVertexAttribArray(location)
            GL.glVertexAttribPointer(
                location, size, GL.GL_FLOAT, GL.GL_FALSE, stride,
                ctypes.c_void_p(offset),
            )
        GL.glBindVertexArray(0)

    def _upload_texture(self):
        if not self._texture:
            return
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._texture)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
        if self._texture_pixels:
            width, height, pixels = self._texture_pixels
        else:
            width, height, pixels = 1, 1, b'\xff\xff\xff\xff'
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0,
            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, pixels,
        )
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)

    def _upload_game_textures(self):
        if self._game_textures:
            GL.glDeleteTextures(list(self._game_textures.values()))
            self._game_textures = {}
        for key, source in self._texture_sources.items():
            texture_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            GL.glTexImage2D(
                GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, source.width, source.height, 0,
                GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, source.rgba,
            )
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            self._game_textures[key] = texture_id

    def mousePressEvent(self, event):
        self._last_mouse = event.position()

    def mouseMoveEvent(self, event):
        if self._last_mouse is None:
            return
        delta = event.position() - self._last_mouse
        self._last_mouse = event.position()
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._yaw += delta.x() * 0.009
            self._pitch = max(-1.2, min(1.2, self._pitch + delta.y() * 0.007))
            self.update()

    def mouseReleaseEvent(self, event):
        self._last_mouse = None

    def wheelEvent(self, event):
        self._radius *= math.pow(0.88, event.angleDelta().y() / 120.0)
        self._radius = max(2.0, min(30.0, self._radius))
        self.update()
