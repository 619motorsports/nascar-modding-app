"""Eutechnyx archive, mesh, texture, and audio formats."""
from .gfs import GfsEnvelope, inspect_gfs, parse_gfs

__all__ = ('GfsEnvelope', 'inspect_gfs', 'parse_gfs')
