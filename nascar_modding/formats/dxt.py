"""Deterministic DXT1 surface encoder used by native livery mip writers."""

from __future__ import annotations

import numpy as np


def _rgb565(red, green, blue):
    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def _dxt1_rgb(value):
    value = np.asarray(value, dtype=np.uint16)
    red = ((value >> 11) & 31).astype(np.uint32) * 255 // 31
    green = ((value >> 5) & 63).astype(np.uint32) * 255 // 63
    blue = (value & 31).astype(np.uint32) * 255 // 31
    return np.stack((red, green, blue), axis=-1)


def _dxt1_assign(blocks, color0, color1):
    first, second = _dxt1_rgb(color0).astype(np.float64), _dxt1_rgb(color1).astype(np.float64)
    palette = np.stack((first, second, (2 * first + second) / 3, (first + 2 * second) / 3), 1)
    distances = ((blocks[:, None, :, :] - palette[:, :, None, :]) ** 2).sum(-1)
    indexes = distances.argmin(1)
    errors = np.take_along_axis(distances, indexes[:, None, :], 1)[:, 0, :].sum(1)
    return indexes.astype(np.uint32), errors


def _pack(color0, color1, indexes):
    swap = color0 < color1
    first, second = np.where(swap, color1, color0), np.where(swap, color0, color1)
    indexes = np.where(swap[:, None], indexes ^ 1, indexes)
    indexes = np.where((first == second)[:, None], 0, indexes).astype(np.uint32)
    bits = np.zeros(len(first), np.uint32)
    for index in range(16):
        bits |= indexes[:, index] << (2 * index)
    output = np.zeros((len(first), 8), np.uint8)
    output[:, 0], output[:, 1] = first & 0xFF, first >> 8
    output[:, 2], output[:, 3] = second & 0xFF, second >> 8
    for index in range(4):
        output[:, 4 + index] = (bits >> (8 * index)) & 0xFF
    return output.tobytes()


def encode_dxt1(pixels: np.ndarray, iterations: int = 3) -> bytes:
    height, width, _channels = pixels.shape
    blocks = pixels.reshape(height // 4, 4, width // 4, 4, 3)
    blocks = blocks.transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3).astype(np.float64)
    mean = blocks.mean(1, keepdims=True)
    centered = blocks - mean
    covariance = np.einsum('nij,nik->njk', centered, centered)
    vector = np.ones((len(blocks), 3))
    for _ in range(4):
        vector = np.einsum('njk,nk->nj', covariance, vector)
        norm = np.linalg.norm(vector, axis=1, keepdims=True)
        norm[norm == 0] = 1
        vector /= norm
    projected = np.einsum('nij,nj->ni', centered, vector)
    endpoint0 = np.clip(mean[:, 0] + vector * projected.max(1)[:, None], 0, 255)
    endpoint1 = np.clip(mean[:, 0] + vector * projected.min(1)[:, None], 0, 255)
    color0 = _rgb565(*(endpoint0.T.astype(np.int32)))
    color1 = _rgb565(*(endpoint1.T.astype(np.int32)))
    indexes, errors = _dxt1_assign(blocks, color0, color1)
    weights = np.array((1.0, 0.0, 2 / 3, 1 / 3))
    for _ in range(iterations):
        weight = weights[indexes]
        ww = (weight * weight).sum(1)
        wo = (weight * (1 - weight)).sum(1)
        oo = ((1 - weight) ** 2).sum(1)
        determinant = ww * oo - wo * wo
        bad = np.abs(determinant) < 1e-9
        determinant[bad] = 1
        bw = np.einsum('ni,nij->nj', weight, blocks)
        bo = np.einsum('ni,nij->nj', 1 - weight, blocks)
        next0 = np.clip((oo[:, None] * bw - wo[:, None] * bo) / determinant[:, None], 0, 255)
        next1 = np.clip((-wo[:, None] * bw + ww[:, None] * bo) / determinant[:, None], 0, 255)
        candidate0 = _rgb565(*(next0.T.astype(np.int32)))
        candidate1 = _rgb565(*(next1.T.astype(np.int32)))
        candidate_indexes, candidate_errors = _dxt1_assign(blocks, candidate0, candidate1)
        better = (candidate_errors < errors) & ~bad
        color0 = np.where(better, candidate0, color0)
        color1 = np.where(better, candidate1, color1)
        indexes = np.where(better[:, None], candidate_indexes, indexes)
        errors = np.where(better, candidate_errors, errors)
    return _pack(color0, color1, indexes)
