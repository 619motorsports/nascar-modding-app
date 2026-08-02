"""Managed Windows FFmpeg installer shared by native and compatibility UIs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

from nascar_modding.formats.audio import ffmpeg_path


class AudioToolsManager:
    release_tag = 'latest'
    archive_name = 'ffmpeg-master-latest-win64-lgpl-shared.zip'
    release_base = 'https://github.com/BtbN/FFmpeg-Builds/releases/download'

    def __init__(self, user_root: str | Path):
        self.root = Path(user_root).resolve() / 'audio_tools'

    def status(self) -> dict:
        executable = ffmpeg_path()
        if not executable:
            return {'ready': False, 'path': None, 'source': None, 'version': None,
                    'managed_dir': str(self.root), 'release_tag': self.release_tag,
                    'archive': self.archive_name}
        path = Path(executable).resolve()
        try:
            path.relative_to(self.root)
            source = 'managed_audio_tools'
        except ValueError:
            source = 'path'
        version = None
        try:
            result = subprocess.run([path, '-hide_banner', '-version'], capture_output=True,
                                    text=True, timeout=15)
            lines = (result.stdout or result.stderr or '').splitlines()
            version = lines[0].strip() if lines else None
        except Exception:
            pass
        return {'ready': True, 'path': str(path), 'source': source, 'version': version,
                'managed_dir': str(self.root), 'release_tag': self.release_tag,
                'archive': self.archive_name}

    @staticmethod
    def _download(url: str, destination: Path, timeout: int = 180):
        request = urllib.request.Request(url, headers={
            'User-Agent': 'NASCAR-Modding-App-Audio-Tools/1.0.2',
        })
        with urllib.request.urlopen(request, timeout=timeout) as response, destination.open('wb') as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)

    @staticmethod
    def _validate(executable: Path) -> str:
        result = subprocess.run([executable, '-hide_banner', '-version'], capture_output=True,
                                text=True, timeout=30)
        output = (result.stdout or '') + '\n' + (result.stderr or '')
        if result.returncode or 'ffmpeg version' not in output.casefold():
            raise ValueError('downloaded ffmpeg.exe did not start correctly')
        if '--enable-gpl' in output.casefold() or '--enable-nonfree' in output.casefold():
            raise ValueError('downloaded build is not the required LGPL-only configuration')
        encoders = subprocess.run([executable, '-hide_banner', '-encoders'], capture_output=True,
                                  text=True, timeout=30)
        encoder_output = (encoders.stdout or '') + '\n' + (encoders.stderr or '')
        if encoders.returncode or 'libmp3lame' not in encoder_output:
            raise ValueError('downloaded LGPL build does not provide libmp3lame')
        return output.splitlines()[0].strip()

    def install(self) -> dict:
        if os.name != 'nt':
            raise ValueError('the managed audio-tools installer is for 64-bit Windows')
        archive_url = f'{self.release_base}/{self.release_tag}/{self.archive_name}'
        checksums_url = f'{self.release_base}/{self.release_tag}/checksums.sha256'
        self.root.parent.mkdir(parents=True, exist_ok=True)
        staging = self.root.with_name(self.root.name + '.installing')
        previous = self.root.with_name(self.root.name + '.previous')
        with tempfile.TemporaryDirectory(prefix='nascar_audio_tools_') as folder:
            temporary = Path(folder)
            archive_path = temporary / self.archive_name
            checksums_path = temporary / 'checksums.sha256'
            self._download(checksums_url, checksums_path)
            self._download(archive_url, archive_path)
            expected = None
            for line in checksums_path.read_text(encoding='utf-8', errors='replace').splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1].lstrip('*') == self.archive_name:
                    expected = parts[0].casefold()
                    break
            if not expected or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('release checksums did not contain the expected LGPL archive')
            actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError('download checksum mismatch; nothing was installed')
            with zipfile.ZipFile(archive_path) as archive:
                candidates = [info for info in archive.infolist()
                              if not info.is_dir() and Path(info.filename).name.casefold() == 'ffmpeg.exe']
                if len(candidates) != 1:
                    raise ValueError('downloaded archive did not contain one ffmpeg.exe')
                bin_prefix = str(PureZipPath(candidates[0].filename).parent).rstrip('/') + '/'
                members = [info for info in archive.infolist()
                           if not info.is_dir() and info.filename.startswith(bin_prefix)
                           and Path(info.filename).suffix.casefold() in ('.exe', '.dll')]
                if staging.exists():
                    shutil.rmtree(staging)
                target_bin = staging / 'bin'
                target_bin.mkdir(parents=True)
                for info in members:
                    (target_bin / Path(info.filename).name).write_bytes(archive.read(info))
            managed = target_bin / 'ffmpeg.exe'
            version = self._validate(managed)
            (staging / 'SOURCE_INFORMATION.txt').write_text(
                'Managed BtbN FFmpeg Windows LGPL shared build\n'
                f'Archive: {self.archive_name}\nArchive SHA-256: {actual}\nDownload: {archive_url}\n',
                encoding='utf-8',
            )
            if previous.exists():
                shutil.rmtree(previous)
            if self.root.exists():
                os.replace(self.root, previous)
            try:
                os.replace(staging, self.root)
            except Exception:
                if previous.exists() and not self.root.exists():
                    os.replace(previous, self.root)
                raise
            if previous.exists():
                shutil.rmtree(previous)
        return {'ready': True, 'version': version, 'path': str(self.root / 'bin' / 'ffmpeg.exe'),
                'release_tag': self.release_tag, 'checksum': actual, 'files': len(members)}


class PureZipPath:
    """Tiny POSIX-parent helper that avoids treating ZIP names as host paths."""

    def __init__(self, value: str):
        self.parts = tuple(part for part in value.replace('\\', '/').split('/') if part)

    @property
    def parent(self):
        return '/'.join(self.parts[:-1])

    def __str__(self):
        return '/'.join(self.parts)
