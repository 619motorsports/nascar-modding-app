"""Shared transactional editor for Eutechnyx FSB5 and SND audio banks."""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import zipfile

import numpy as np

from nascar_modding.editing.archive import backup_path
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.formats import audio as fmt
from nascar_modding.games.installation import GameInstallation


_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')


def audio_category(name: str) -> str:
    upper = str(name).upper()
    if 'ENGINE' in upper or 'VEHICLE' in upper:
        return 'Engines / Vehicles'
    if 'HUDSND' in upper or 'SPOTTER' in upper or upper.endswith('.SND'):
        return 'Spotter / Speech'
    if 'MUSIC' in upper:
        return 'Music'
    if 'PIT' in upper:
        return 'Pit Stop'
    if 'TRACK' in upper or 'AMBI' in upper or 'MATERIAL' in upper:
        return 'Track / Surface'
    if 'FRONTEND' in upper or 'MENU' in upper or 'GLOBAL_HUD' in upper:
        return 'Menus / HUD'
    return 'Other'


def safe_filename(value: str) -> str:
    return (_SAFE_NAME.sub('_', str(value)).strip('._')[:160] or 'sound')


def _flat_fsb(parsed: dict) -> list[dict]:
    return [
        dict(name=name, rel=rel, len=length, meta=meta, mode=parsed['mode'],
             ok=parsed['editable'])
        for name, rel, length, meta in parsed['slices']
    ]


def parse_container(name: str, payload: bytes) -> dict:
    if str(name).upper().endswith('.SND'):
        samples, sub_banks = fmt.parse_snd(payload)
        if not samples:
            raise ValueError('no FSB5 sub-banks were found in this SND container')
        return {
            'kind': 'snd', 'samples': samples, 'sub_banks': sub_banks,
            'codec': 'SPEECH (FSB5/MPEG chain)', 'editable': True,
        }
    parsed = fmt.parse_fsb5(payload)
    return {
        'kind': 'fsb', 'samples': _flat_fsb(parsed), 'fsb': parsed,
        'codec': parsed['codec'], 'editable': parsed['editable'],
    }


def describe_sample(container: bytes, sample: dict) -> dict:
    if sample['mode'] == 11:
        frames, _padded, info = fmt.fmod_walk(
            container[sample['rel']:sample['rel'] + sample['len']]
        )
        if not info:
            return {'spec': 'unusual first frame', 'duration': None, 'editable': False}
        samples_per_frame = 384 if info[0] == 1 else (576 if info[2] < 32000 else 1152)
        return {
            'spec': f'L{info[0]} {info[1]}k {info[2]}Hz '
                    + ('mono' if info[3] else 'stereo'),
            'duration': round(len(frames) * samples_per_frame / info[2], 2),
            'editable': bool(sample['ok']),
        }
    if sample['mode'] == 2:
        meta = sample['meta']
        channels = {1: 'mono', 2: 'stereo'}.get(meta['ch'], f"{meta['ch']}ch")
        return {
            'spec': f"PCM16 {meta['hz'] or '?'}Hz {channels}",
            'duration': round(meta['samples'] / meta['hz'], 2) if meta['hz'] else None,
            'editable': bool(sample['ok'] and meta['hz']),
        }
    return {
        'spec': fmt.FSB_MODES.get(sample['mode'], '?'),
        'duration': None,
        'editable': False,
    }


def full_length_candidate(bank_name: str, container: bytes, parsed: dict) -> tuple[bool, str]:
    fsb = parsed.get('fsb')
    samples = parsed['samples']
    if not isinstance(fsb, dict):
        return False, 'not an FSB5 bank'
    if fsb.get('mode') != 11:
        return False, 'not an MPEG FSB5 bank'
    if not fsb.get('editable'):
        return False, 'bank boundary validation did not pass'
    if fsb.get('layout') != 'sh6x16':
        return False, 'bank does not use the validated standard FSB5 layout'
    if audio_category(bank_name) == 'Music':
        return True, 'named music bank'
    if len(samples) == 1:
        duration = float(describe_sample(container, samples[0]).get('duration') or 0.0)
        if duration >= 45.0:
            return True, f'long-form single-track MPEG bank ({duration:.2f}s)'
    return False, 'bank is not identified as music or a long-form single-track MPEG bank'


def _raw_payload(container: bytes, sample: dict) -> tuple[bytes, str, str]:
    raw = container[sample['rel']:sample['rel'] + sample['len']]
    extension, mime = 'bin', 'application/octet-stream'
    if sample['mode'] == 2:
        extension = 'pcm'
    elif sample['mode'] == 11:
        _frames, padded, info = fmt.fmod_walk(raw)
        if not padded and info:
            extension, mime = ('mp2' if info[0] == 2 else 'mp3'), 'audio/mpeg'
        else:
            extension = 'fmod_mpeg.bin'
    return raw, extension, mime


def _mpeg_payload(container: bytes, sample: dict) -> tuple[bytes, str, str]:
    if sample['mode'] != 11:
        raise ValueError('sample is not MPEG')
    raw = container[sample['rel']:sample['rel'] + sample['len']]
    frames, _padded, info = fmt.fmod_walk(raw)
    if not frames or not info:
        raise ValueError('no MPEG frames were found in this sample')
    return b''.join(frames), ('mp2' if info[0] == 2 else 'mp3'), 'audio/mpeg'


def _wav_payload(container: bytes, sample: dict) -> tuple[bytes, str, str]:
    raw = container[sample['rel']:sample['rel'] + sample['len']]
    if sample['mode'] == 2 and sample['meta'].get('hz'):
        meta = sample['meta']
        needed = meta['samples'] * 2 * meta['ch']
        pcm = raw[:needed] if needed <= len(raw) else raw
        header = (
            b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt '
            + struct.pack('<IHHIIHH', 16, 1, meta['ch'], meta['hz'],
                          meta['hz'] * meta['ch'] * 2, meta['ch'] * 2, 16)
            + b'data' + struct.pack('<I', len(pcm))
        )
        return header + pcm, 'wav', 'audio/wav'
    if sample['mode'] == 11:
        frames, _padded, _info = fmt.fmod_walk(raw)
        if frames:
            raw = b''.join(frames)
    ffmpeg = fmt.ffmpeg_path()
    if not ffmpeg:
        raise ValueError('WAV export needs FFmpeg; lossless MPEG or raw export is still available')
    with tempfile.TemporaryDirectory(prefix='nascar_audio_export_') as temp:
        source, output = Path(temp) / 'input.bin', Path(temp) / 'output.wav'
        source.write_bytes(raw)
        result = subprocess.run(
            [ffmpeg, '-v', 'error', '-i', str(source), '-f', 'wav', str(output), '-y'],
            capture_output=True, text=True,
        )
        if result.returncode or not output.is_file():
            raise ValueError('FFmpeg could not convert this sound: ' + (result.stderr or '')[-240:])
        return output.read_bytes(), 'wav', 'audio/wav'


class AudioBankEditor:
    """One backend for native and legacy audio inspection/editing workflows."""

    def __init__(self, installation: GameInstallation):
        self.installation = installation
        self.resources = ResourceEditor(installation)

    def banks(self) -> list[dict]:
        rows = []
        for key, pair in sorted(self.installation.archive_pairs.items()):
            has_backup = (
                Path(backup_path(pair.archive)).is_file()
                and Path(backup_path(pair.index)).is_file()
            )
            with pair.archive.open('rb') as handle:
                for entry in self.installation.entries(key):
                    upper = entry.name.upper()
                    if not upper.endswith(('.FSB', '.SND')):
                        continue
                    handle.seek(entry.archive_offset)
                    header = handle.read(28)
                    codec, count, first_sample = '', 0, ''
                    try:
                        if upper.endswith('.SND'):
                            codec = 'SPEECH'
                        elif header[:4] == b'FSB5':
                            count = struct.unpack_from('<I', header, 8)[0]
                            codec = fmt.FSB_MODES.get(struct.unpack_from('<I', header, 24)[0], '?')
                            if count == 1 and entry.size <= 128 * 1024 * 1024:
                                handle.seek(entry.archive_offset)
                                parsed = fmt.parse_fsb5(handle.read(entry.size))
                                first_sample = str(parsed['slices'][0][0] or '') if parsed['slices'] else ''
                    except Exception:
                        first_sample = ''
                    rows.append({
                        'archive': key, 'name': entry.name, 'size': entry.size,
                        'codec': codec, 'sample_count': count, 'first_sample': first_sample,
                        'category': audio_category(entry.name), 'has_backup': has_backup,
                    })
        return rows

    def _read(self, archive: str, bank: str, pristine: bool = False) -> tuple[bytes, dict]:
        payload = self.resources.read(bank, archive, pristine=pristine)
        return payload, parse_container(bank, payload)

    @staticmethod
    def _sample(parsed: dict, index: int) -> dict:
        try:
            sample = parsed['samples'][int(index)]
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError('sample index is out of range') from exc
        return sample

    def samples(self, archive: str, bank: str) -> dict:
        container, parsed = self._read(archive, bank)
        pristine = pristine_parsed = None
        try:
            pristine, pristine_parsed = self._read(archive, bank, pristine=True)
        except Exception:
            pass
        rows, modified_count = [], 0
        for index, sample in enumerate(parsed['samples']):
            original_sample = None
            if pristine_parsed and index < len(pristine_parsed['samples']):
                original_sample = pristine_parsed['samples'][index]
            modified = False
            if pristine is not None and original_sample is not None:
                live = container[sample['rel']:sample['rel'] + sample['len']]
                stock = pristine[original_sample['rel']:original_sample['rel'] + original_sample['len']]
                modified = live != stock
            modified_count += int(modified)
            description = describe_sample(container, sample)
            try:
                _mpeg_payload(container, sample)
                export_mpeg = True
            except ValueError:
                export_mpeg = False
            rows.append({
                'index': index, 'name': sample['name'], 'bytes': sample['len'],
                'modified': modified, 'export_raw': True, 'export_mpeg': export_mpeg,
                'export_wav': bool(sample['mode'] == 2 or fmt.ffmpeg_path()),
                **description,
            })
        eligible, reason = full_length_candidate(bank, container, parsed)
        return {
            'archive': str(archive), 'bank': bank, 'samples': rows,
            'modified_count': modified_count, 'has_backup': pristine is not None,
            'kind': parsed['kind'], 'codec': parsed['codec'],
            'editable': parsed['editable'], 'full_length_candidate': eligible,
            'full_length_reason': reason, 'full_length_supported': bool(eligible and fmt.ffmpeg_path()),
            'ffmpeg': fmt.ffmpeg_path(),
        }

    def sample_payload(self, archive: str, bank: str, index: int, mode: str = 'wav') -> dict:
        container, parsed = self._read(archive, bank)
        sample = self._sample(parsed, index)
        mode = str(mode).lower()
        if mode == 'raw':
            payload, extension, mime = _raw_payload(container, sample)
        elif mode == 'mpeg':
            payload, extension, mime = _mpeg_payload(container, sample)
        elif mode == 'wav':
            payload, extension, mime = _wav_payload(container, sample)
        else:
            raise ValueError(f'unsupported audio export mode: {mode}')
        stem = safe_filename(Path(bank).stem + '__' + sample['name'])
        return {'payload': payload, 'extension': extension, 'mime': mime,
                'filename': f'{stem}.{extension}'}

    def raw_sample(self, archive: str, bank: str, index: int, *, pristine=False) -> dict:
        """Return one exact fixed-slot payload for portable, lossless packs."""
        container, parsed = self._read(archive, bank, pristine=pristine)
        sample = self._sample(parsed, index)
        start, length = int(sample['rel']), int(sample['len'])
        return {
            'payload': bytes(container[start:start + length]),
            'name': sample['name'], 'index': int(index), 'length': length,
            'mode': int(sample['mode']),
        }

    def replace_raw_sample(self, archive: str, bank: str, index: int,
                           payload: bytes, *, name: str | None = None,
                           mode: int | None = None) -> dict:
        """Install a pack's exact encoded sample after identity/spec validation."""
        container, parsed = self._read(archive, bank)
        sample = self._sample(parsed, index)
        if name is not None and str(sample['name']) != str(name):
            raise ValueError('audio sample name no longer matches the pack')
        if mode is not None and int(sample['mode']) != int(mode):
            raise ValueError('audio sample codec no longer matches the pack')
        start, length = int(sample['rel']), int(sample['len'])
        raw = bytes(payload)
        if len(raw) != length:
            raise ValueError('audio fixed-slot length no longer matches the pack')
        rebuilt = container[:start] + raw + container[start + length:]
        result = self._install(archive, bank, rebuilt, container)
        return {**result, 'sample': sample['name'], 'sample_index': int(index)}

    def export_sample(self, archive: str, bank: str, index: int, destination: str | Path,
                      mode: str = 'wav') -> Path:
        item = self.sample_payload(archive, bank, index, mode)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(item['payload'])
        return output

    def export_bank(self, archive: str, bank: str, destination: str | Path,
                    modified_only: bool = False) -> dict:
        info = self.samples(archive, bank)
        container, parsed = self._read(archive, bank)
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest, exported = [], 0
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
            for row, sample in zip(info['samples'], parsed['samples']):
                if modified_only and not row['modified']:
                    continue
                payload, extension, _mime = _raw_payload(container, sample)
                member = f"{row['index']:04d}__{safe_filename(row['name'])}.{extension}"
                package.writestr(member, payload)
                manifest.append({**row, 'file': member})
                exported += 1
            package.writestr('manifest.json', json.dumps({
                'bank': bank, 'archive': str(archive), 'modified_only': modified_only,
                'exported': exported, 'samples': manifest,
            }, indent=2))
        return {'path': output, 'exported': exported}

    @staticmethod
    def _validate(bank: str, payload: bytes) -> dict:
        return parse_container(bank, payload)

    def _install(self, archive: str, bank: str, payload: bytes, original: bytes) -> dict:
        self._validate(bank, payload)
        result = self.resources.replace(bank, archive, payload)
        try:
            readback = self.resources.read(bank, archive)
            if readback != payload:
                raise IOError('audio bank read-back did not match the planned payload')
            self._validate(bank, readback)
        except Exception as install_error:
            try:
                self.resources.replace(bank, archive, original)
            except Exception as rollback_error:
                raise RuntimeError(
                    f'audio validation failed ({install_error}) and rollback failed: {rollback_error}'
                ) from install_error
            raise
        return {**result, 'bank': bank, 'verified': True}

    def restore_bank(self, archive: str, bank: str) -> dict:
        pristine = self.resources.read(bank, archive, pristine=True)
        parsed = self._validate(bank, pristine)
        result = self.resources.restore(bank, archive)
        readback = self.resources.read(bank, archive)
        if readback != pristine:
            raise IOError('restored audio bank did not match the pristine backup')
        self._validate(bank, readback)
        return {**result, 'bank': bank, 'restored_samples': len(parsed['samples']), 'verified': True}

    def restore_sample(self, archive: str, bank: str, index: int) -> dict:
        live, live_parsed = self._read(archive, bank)
        pristine, pristine_parsed = self._read(archive, bank, pristine=True)
        sample = self._sample(live_parsed, index)
        stock = self._sample(pristine_parsed, index)
        if len(live) != len(pristine) or sample['rel'] != stock['rel'] or sample['len'] != stock['len']:
            raise ValueError('bank layout changed; restore the entire sound group instead')
        rebuilt = bytearray(live)
        rebuilt[sample['rel']:sample['rel'] + sample['len']] = \
            pristine[stock['rel']:stock['rel'] + stock['len']]
        result = self._install(archive, bank, bytes(rebuilt), live)
        return {**result, 'sample': sample['name'], 'sample_index': int(index)}

    @staticmethod
    def _convert_pcm(raw: bytes, filename: str, channels: int, rate: int) -> bytes:
        extension = Path(filename).suffix.lower()
        if extension in ('.pcm', '.raw'):
            return raw
        ffmpeg = fmt.ffmpeg_path()
        if not ffmpeg:
            if extension == '.wav' or raw[:4] == b'RIFF':
                return fmt._pcm16_from_wav(raw, channels, rate)
            raise ValueError('conversion needs FFmpeg unless the input is PCM WAV or raw PCM16')
        with tempfile.TemporaryDirectory(prefix='nascar_audio_pcm_') as temp:
            source = Path(temp) / ('input' + (extension or '.bin'))
            output = Path(temp) / 'output.pcm'
            source.write_bytes(raw)
            result = subprocess.run([
                ffmpeg, '-v', 'error', '-i', str(source), '-vn', '-map_metadata', '-1',
                '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le', '-f', 's16le',
                str(output), '-y',
            ], capture_output=True, text=True)
            if result.returncode or not output.is_file():
                raise ValueError('FFmpeg could not convert to PCM16: ' + (result.stderr or '')[-240:])
            return output.read_bytes()

    @staticmethod
    def _encode_fixed_mpeg(raw: bytes, filename: str, spec: tuple, sample: dict,
                           gain_db: float, volume_mode: str) -> bytes:
        frames = fmt.walk_frames(raw, limit=4)
        uploaded = frames[0][1] if frames and frames[0][0] == 0 else None
        if (uploaded and uploaded[:4] == spec[:4] and volume_mode == 'source'
                and not fmt._audio_is_loop_sample(sample.get('name'))):
            return raw
        ffmpeg = fmt.ffmpeg_path()
        if not ffmpeg:
            raise ValueError('replacement needs FFmpeg or a pre-matched MPEG stream in source-volume mode')
        extension = Path(filename).suffix
        output_format = 'mp2' if spec[0] == 2 else 'mp3'
        with tempfile.TemporaryDirectory(prefix='nascar_audio_mpeg_') as temp:
            source, output = Path(temp) / ('input' + (extension or '.bin')), Path(temp) / 'output.bin'
            source.write_bytes(raw)
            command = [ffmpeg, '-v', 'error']
            if fmt._audio_is_loop_sample(sample.get('name')):
                command += ['-stream_loop', '-1']
            command += ['-i', str(source), '-vn', '-map_metadata', '-1', '-ar', str(spec[2]),
                        '-ac', '1' if spec[3] else '2']
            meta = sample.get('meta') or {}
            duration = float(meta.get('samples') or 0) / float(meta.get('hz') or spec[2])
            if fmt._audio_is_loop_sample(sample.get('name')) and duration > 0:
                command += ['-t', f'{duration:.6f}']
            if abs(gain_db) > 0.01:
                command += ['-af', f'volume={gain_db:.3f}dB,alimiter=limit=0.97']
            command += ['-c:a', 'mp2' if spec[0] == 2 else 'libmp3lame',
                        '-b:a', f'{spec[1]}k']
            if spec[0] != 2:
                command += ['-write_xing', '0', '-id3v2_version', '0']
            command += ['-f', output_format, str(output), '-y']
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode or not output.is_file():
                raise ValueError('FFmpeg could not encode the file: ' + (result.stderr or '')[-240:])
            encoded = output.read_bytes()
        first = next((offset for offset in range(len(encoded)) if fmt.frame_info(encoded, offset)), None)
        if first is None:
            raise ValueError('MPEG encode produced no frames')
        stream = encoded[first:]
        encoded_spec = fmt.frame_info(stream, 0)
        if not encoded_spec or encoded_spec[:4] != spec[:4]:
            raise ValueError('FFmpeg output did not match the stock layer/bitrate/rate/channels')
        return stream

    def replace_sample(self, archive: str, bank: str, index: int, raw: bytes, filename: str,
                       volume_mode: str = 'match_stock', custom_gain_db: float = 0.0) -> dict:
        if not raw:
            raise ValueError('replacement audio is empty')
        container, parsed = self._read(archive, bank)
        sample = self._sample(parsed, index)
        if not sample['ok']:
            raise ValueError('this sample is read-only (unsupported codec or failed validation)')
        mode = fmt._audio_volume_mode(volume_mode)
        custom = fmt._audio_custom_gain_db(custom_gain_db, mode)
        start, length = sample['rel'], sample['len']
        original = container[start:start + length]
        if sample['mode'] == 2:
            meta = sample['meta']
            rate, channels, samples = int(meta.get('hz') or 0), int(meta.get('ch') or 0), int(meta.get('samples') or 0)
            if rate <= 0 or channels not in (1, 2) or samples <= 0:
                raise ValueError('PCM16 metadata is incomplete')
            frame_bytes, audio_length = channels * 2, samples * channels * 2
            if audio_length > length:
                raise ValueError('declared PCM16 sample does not fit its fixed slot')
            stream = self._convert_pcm(raw, filename, channels, rate)
            stream = stream[:len(stream) - len(stream) % frame_bytes]
            if fmt._audio_is_loop_sample(sample.get('name')) and len(stream) < audio_length:
                stream = fmt._loop_fill_pcm16(stream, audio_length, channels, rate)
            source_values = np.frombuffer(stream[:len(stream) - len(stream) % 2], dtype='<i2').astype(np.float64) / 32768.0 if stream else np.array([], dtype=np.float64)
            stock_values = np.frombuffer(original[:audio_length], dtype='<i2').astype(np.float64) / 32768.0
            gain = fmt._safe_gain_db(mode, fmt._active_pcm_stats(source_values),
                                     fmt._active_pcm_stats(stock_values), custom_gain_db=custom)
            stream = fmt._apply_pcm_gain_i16(stream, gain)
            used = min(len(stream), audio_length)
            used -= used % frame_bytes
            replacement = stream[:used] + b'\0' * (audio_length - used) + original[audio_length:]
            detail = {'frames': used // frame_bytes, 'applied_gain_db': round(float(gain), 2)}
        elif sample['mode'] == 11:
            spec = fmt.frame_info(container, start)
            if not spec:
                raise ValueError('stock sample has an unrecognized MPEG format')
            gain = fmt._mpeg_gain_db(mode, raw, filename, original, spec, custom_gain_db=custom)
            stream = self._encode_fixed_mpeg(raw, filename, spec, sample, gain, mode)
            replacement, fit = fmt._fit_mpeg_to_stock_topology(stream, original, sample.get('meta') or {})
            ok, error = fmt._verify_mpeg_decode(replacement)
            if not ok:
                raise ValueError('rebuilt MPEG did not decode cleanly: ' + str(error))
            detail = {**fit, 'applied_gain_db': round(float(gain), 2), 'topology_preserved': True}
        else:
            raise ValueError('only validated PCM16 and MPEG samples can be replaced')
        if len(replacement) != length:
            raise ValueError('fixed-slot replacement produced the wrong byte length')
        rebuilt = container[:start] + replacement + container[start + length:]
        installed = self._install(archive, bank, rebuilt, container)
        return {**installed, **detail, 'sample': sample['name'], 'sample_index': int(index),
                'volume_mode': mode}

    def replace_full_song(self, archive: str, bank: str, index: int, raw: bytes, filename: str,
                          volume_mode: str = 'match_stock', custom_gain_db: float = 0.0) -> dict:
        if not raw:
            raise ValueError('replacement audio is empty')
        container, parsed = self._read(archive, bank)
        eligible, reason = full_length_candidate(bank, container, parsed)
        if not eligible:
            raise ValueError('full-length rebuilding is unavailable: ' + reason)
        if not fmt.ffmpeg_path():
            raise ValueError('full-length music replacement needs FFmpeg')
        sample = self._sample(parsed, index)
        spec = fmt.frame_info(container, sample['rel'])
        if not spec:
            raise ValueError('stock song has an unrecognized MPEG format')
        mode = fmt._audio_volume_mode(volume_mode)
        custom = fmt._audio_custom_gain_db(custom_gain_db, mode)
        stock = container[sample['rel']:sample['rel'] + sample['len']]
        gain = fmt._mpeg_gain_db(mode, raw, filename, stock, spec, custom_gain_db=custom)
        stream, sample_count, _channels = fmt._encode_full_length_mpeg(raw, filename, spec, gain_db=gain)
        rebuilt, details = fmt._rebuild_fsb5_full_mpeg_sample(container, int(index), stream, sample_count)
        installed = self._install(archive, bank, rebuilt, container)
        duration = float(sample_count) / float(details['hz'] or spec[2])
        return {**installed, **details, 'full_length': True, 'duration': round(duration, 3),
                'applied_gain_db': round(float(gain), 2), 'volume_mode': mode,
                'sample': sample['name'], 'sample_index': int(index)}
