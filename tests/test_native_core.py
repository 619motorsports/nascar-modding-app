import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import io
import numpy as np
from PIL import Image
import containers

from nascar_modding.core.cdf import parse_cdf_bytes, read_cdf
from nascar_modding.formats.eutechnyx_mesh import (
    MeshMaterial, MeshPart, MeshScene, MaterialTextureBinding,
    assemble_car_scene, load_mesh,
)
from nascar_modding.editing.car_materials import resolve_game_materials, texture_container_name
from nascar_modding.formats.gfs import compare_gfs, encrypt_gfs_payload, inspect_gfs, parse_gfs
from nascar_modding.editing.archive import ArchiveEntryEditor
from nascar_modding.editing.appdata import AppDataManager
from nascar_modding.editing.audio import AudioBankEditor
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.names import DriverHandleEditor, DriverNameEditor
from nascar_modding.editing.ratings import RATING_FIELDS, RatingsEditor
from nascar_modding.editing.text_tables import TextTableEditor
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.editing.scr import ScrEditor, parse_numeric_rows
from nascar_modding.editing.pyc_records import PycRecordEditor
from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.editing.teams import TeamEditor, TeamPresentationRecovery
from nascar_modding.editing.managed_paints import ManagedPaintEditor
from nascar_modding.editing.full_repair import FullRepairEditor
from nascar_modding.editing.season_packs import SeasonPackEditor
from nascar_modding.editing.stock_paints import StockPaintEditor
from nascar_modding.editing.team_presentation import TeamPresentationEditor
from nascar_modding.editing.livery_wrappers import (
    RAW_OFFSET, SD_ENTRY_SIZE, SD_OFFSETS, SD_PITCHES, NativeLiveryWrapperEditor,
)
from nascar_modding.editing.transactions import (
    AppendRepointTransaction, ExactFileTransaction, ManagedPaintCheckpoint, ManagedPaintTransaction, TeamAssetCheckpoint,
    TeamAssetTransaction,
)
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.user_library import UserLibrary
from nascar_modding.core.cdf import CdfEntry
from nascar_modding.games.installation import ArchivePair
from nascar_modding.games.assets import classify_livery_slot, matches_primary_livery
from nascar_modding.games.installation import GameInstallation
from nascar_modding.games.profiles import PROFILES
from nascar_modding.verification.tracks import TrackInventory
from nascar_modding.verification.support import SupportReporter
from nascar_modding.verification.retail_saves import analyze_ntg2013_transition


ROOT = Path(__file__).resolve().parents[1]
INSTALLED_GAMES = {
    'nascar13': Path(r'G:\hdd\SteamLibrary\steamapps\common\NASCAR The Game 2013'),
    'nascar14': Path(r'G:\hdd\SteamLibrary\steamapps\common\NASCAR 14'),
    'nascar15': Path(r'G:\hdd\SteamLibrary\steamapps\common\NASCAR 15'),
}


def _small_layout_b_index():
    strings = b'\0FIRST.ARC\0SECOND.PYC\0'
    string_base = 0x50 + 3 * 32
    data = bytearray(string_base + len(strings))
    header = [0] * 12
    header[0] = 0x436C6966
    header[8] = 3
    header[10] = len(strings)
    struct.pack_into('<12I', data, 0, *header)
    first = [0] * 8
    first[3], first[4], first[7] = 1, 128, 0
    second = [0] * 8
    second[3], second[4], second[7] = 11, 64, 128
    sentinel = [0] * 8
    sentinel[7] = 0xFFFFFFFF
    struct.pack_into('<8I', data, 0x50, *first)
    struct.pack_into('<8I', data, 0x70, *second)
    struct.pack_into('<8I', data, 0x90, *sentinel)
    data[string_base:] = strings
    return bytes(data)


def _single_layout_b_index(name='TEST.ARC', size=8, offset=4):
    strings = b'\0' + name.encode('ascii') + b'\0'
    string_base = 0x50 + 32
    data = bytearray(string_base + len(strings))
    header = [0] * 12
    header[0], header[8], header[10] = 0x436C6966, 1, len(strings)
    struct.pack_into('<12I', data, 0, *header)
    record = [0] * 8
    record[3], record[4], record[7] = 1, size, offset
    struct.pack_into('<8I', data, 0x50, *record)
    data[string_base:] = strings
    return bytes(data)


def _layout_b_index(entries):
    """Build a minimal valid index from ``(name, size, offset)`` tuples."""
    names = bytearray(b'\0')
    name_refs = []
    for name, _size, _offset in entries:
        name_refs.append(len(names))
        names.extend(name.encode('ascii') + b'\0')
    string_base = 0x50 + len(entries) * 32
    data = bytearray(string_base + len(names))
    header = [0] * 12
    header[0], header[8], header[10] = 0x436C6966, len(entries), len(names)
    struct.pack_into('<12I', data, 0, *header)
    for index, ((_name, size, offset), name_ref) in enumerate(zip(entries, name_refs)):
        record = [0] * 8
        record[3], record[4], record[7] = name_ref, size, offset
        struct.pack_into('<8I', data, 0x50 + index * 32, *record)
    data[string_base:] = names
    return bytes(data)


def _lda(strings):
    """Build the verified Eutechnyx LDA string-table layout used by the editor."""
    header = bytearray(0x14)
    header[:4] = b'LDA\0'
    struct.pack_into('<I', header, 0x10, len(strings))
    pool = bytearray()
    offsets = []
    for value in strings:
        offsets.append(len(pool))
        pool.extend(value.encode('latin1') + b'\0')
    body = b''.join(struct.pack('<I', value) for value in offsets)
    result = bytes(header) + body + b'\0\0\0\0' + bytes(pool)
    result = bytearray(result)
    struct.pack_into('<I', result, 4, len(result))
    return bytes(result)


def _python2_rating_pyc(values):
    code = bytearray()
    for index in range(len(RATING_FIELDS)):
        code.extend((0x64, index, 0))

    def marshal_string(value):
        return b's' + struct.pack('<i', len(value)) + value

    empty_tuple = b'(\0\0\0\0'
    constants = b'(' + struct.pack('<i', len(values)) + b''.join(
        b'g' + struct.pack('<d', value) for value in values
    )
    code_object = (
        b'c' + struct.pack('<4i', 0, 0, 0, 0)
        + marshal_string(bytes(code))
        + constants
        + empty_tuple * 4
        + marshal_string(b'test.py')
        + marshal_string(b'<module>')
        + struct.pack('<i', 1)
        + marshal_string(b'')
    )
    return b'PYCHEAD!' + code_object


def _python2_handle_pyc(handle):
    def marshal_string(value):
        return b's' + struct.pack('<i', len(value)) + value

    empty_tuple = b'(\0\0\0\0'
    constants = b'(\x01\0\0\0' + marshal_string(handle.encode('latin1'))
    code_object = (
        b'c' + struct.pack('<4i', 0, 0, 0, 0)
        + marshal_string(b'') + constants + empty_tuple * 4
        + marshal_string(b'test.py') + marshal_string(b'<module>')
        + struct.pack('<i', 1) + marshal_string(b'')
    )
    return b'PYCHEAD!' + code_object


def _scr_container():
    payload = (
        b'AERODYNAMICS\0{\0FRONT-DRAFT-DRAG\0' b'0.91\0'
        b'REAR-DRAFT-DRAG\0' b'0.80\0}\0'
    )
    header = bytearray(0x80)
    header[:4] = b'ARCC'
    struct.pack_into('<I', header, 4, 1)
    packed = int.from_bytes(bytes((1,)) + len(payload).to_bytes(3, 'big'), 'little')
    table = struct.pack('<4I', 0x1234, 0, 0, packed)
    return bytes(header) + table + payload


class NativeCoreTests(unittest.TestCase):
    def test_exact_file_transaction_restores_files_and_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file_path, directory = root / 'state.bin', root / 'images'
            file_path.write_bytes(b'before')
            directory.mkdir()
            (directory / 'kept.png').write_bytes(b'old')
            transaction = ExactFileTransaction()
            snapshot = transaction.snapshot((file_path,), directories=(directory,))
            file_path.write_bytes(b'after')
            (directory / 'kept.png').write_bytes(b'new')
            (directory / 'added.png').write_bytes(b'added')
            self.assertEqual([], transaction.restore(snapshot))
            self.assertEqual(b'before', file_path.read_bytes())
            self.assertEqual(b'old', (directory / 'kept.png').read_bytes())
            self.assertFalse((directory / 'added.png').exists())
            transaction.clear(snapshot)

    def test_legacy_frontend_does_not_reintroduce_removed_backend_writers(self):
        source = (ROOT / 'app.py').read_text(encoding='utf-8')
        for removed in (
            'def _scr_key_batch_fixed', 'def _scr_variable_batch',
            'def _scr_rebuild_arcc', 'def _install_exact_field_variant',
            'def _read_container', 'def _pcm16_replace',
            'def _audio_raw_payload', 'def _audio_wav_payload',
            'def _ffmpeg_details', 'def _download_url', 'def _validate_managed_ffmpeg',
            'def _schedule_read', 'def _schedule_patch',
            'def schedule_mod', 'def schedule_link_mod',
            'def _appdata_safe_name', 'def _appdata_migrate_bytes',
            'Historical implementation retained temporarily below',
            'def patch_name_exp', 'def patch_name(', 'def patch_handle(',
        ):
            self.assertNotIn(removed, source)
        self.assertIn('return editor.preview(changes) if dry_run else editor.apply(changes)', source)
        self.assertIn("rows=_shared_audio_editor().banks()", source)
        self.assertIn('_shared_managed_paint_editor().create(', source)

    def test_all_three_game_profiles_are_canonical(self):
        self.assertEqual({'nascar13', 'nascar14', 'nascar15'}, set(PROFILES))
        self.assertEqual(2013, PROFILES['nascar13'].content_season)
        self.assertEqual('13', PROFILES['nascar13'].season_prefix)
        self.assertEqual(18306, PROFILES['nascar13'].series_uid)
        self.assertEqual('CAREERNUMBERS.ARC', PROFILES['nascar13'].number_container)
        self.assertIn('SPRINTNUMS2012.ARC', PROFILES['nascar13'].dormant_number_containers)

    def test_gfs_parser_validates_plain_and_encrypted_envelopes(self):
        core = b''.join(struct.pack('<I', value) for value in range(40))
        payload = b'PROFILEDATA' + struct.pack('<2I', 16, 4) + core
        version_bits = struct.unpack('<I', struct.pack('<f', 4.18))[0]
        for encrypted, seed in ((False, 0), (True, 0x12345678)):
            stored = encrypt_gfs_payload(payload, seed) if encrypted else payload
            archive = struct.pack(
                '<5I', len(payload), version_bits, sum(payload), int(encrypted), seed
            ) + stored + bytes(17)
            parsed = parse_gfs(archive)
            self.assertEqual(payload, parsed.payload)
            self.assertEqual('PROFILEDATA', parsed.section_name)
            self.assertAlmostEqual(4.18, parsed.version, places=4)
            self.assertEqual(17, parsed.trailing_size)
            report = inspect_gfs(archive)
            self.assertEqual(19, report['profile_core_offset'])
            self.assertTrue(report['profile_core_prefix_complete'])
            self.assertEqual(124, report['profile_core_prefix_bytes'])
            self.assertEqual(0x21D8, report['profile_core_prefix_fields'][10]['object_offset'])
            self.assertEqual(46, report['profile_core_scalar_contract_count'])
            self.assertEqual(40, report['profile_core_scalar_values_recovered'])
            self.assertEqual(6, len(report['profile_core_deferred_fields']))
            self.assertFalse(report['retail_profile_writer_ready'])

    def test_gfs_comparison_reports_wire_ranges_without_semantic_guesses(self):
        core = b''.join(struct.pack('<I', value) for value in range(40))
        before_payload = b'PROFILEDATA' + struct.pack('<2I', 16, 4) + core
        after_payload = bytearray(before_payload)
        struct.pack_into('<I', after_payload, 19, 99)
        version_bits = struct.unpack('<I', struct.pack('<f', 4.18))[0]

        def envelope(payload):
            return struct.pack('<5I', len(payload), version_bits, sum(payload), 0, 0) + bytes(payload)

        report = compare_gfs(envelope(before_payload), envelope(after_payload))
        self.assertEqual(1, report['changed_bytes'])
        self.assertEqual(19, report['changed_ranges'][0]['payload_offset'])
        self.assertEqual(0, report['known_prefix_scalar_changes'][0]['object_offset'])
        self.assertEqual(99, report['known_prefix_scalar_changes'][0]['after'])
        self.assertEqual({'proven_scalar_prefix': 1}, report['changed_bytes_by_region'])
        self.assertEqual(['proven_scalar_prefix'], report['changed_ranges'][0]['regions'])
        self.assertFalse(report['deferred_scalar_values_compared'])
        self.assertFalse(report['retail_profile_writer_ready'])

    def test_ntg2013_transition_experiment_compares_ordered_copies_read_only(self):
        core = b''.join(struct.pack('<I', value) for value in range(40))
        before_payload = b'PROFILEDATA' + struct.pack('<2I', 16, 4) + core + b'opaque'
        after_payload = bytearray(before_payload)
        struct.pack_into('<I', after_payload, 19, 77)
        after_payload[-1] = ord('!')
        version_bits = struct.unpack('<I', struct.pack('<f', 4.18))[0]

        def envelope(payload):
            return struct.pack('<5I', len(payload), version_bits, sum(payload), 0, 0) + bytes(payload)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            baseline, started = root / 'baseline', root / 'started'
            baseline.mkdir()
            started.mkdir()
            (baseline / 'SYS-DATA').write_bytes(envelope(before_payload))
            (started / 'SYS-DATA').write_bytes(envelope(after_payload))
            before_hash = (baseline / 'SYS-DATA').read_bytes()
            report = analyze_ntg2013_transition((('baseline', baseline), ('season_started', started)))
            transition = report['transitions'][0]
            self.assertEqual(['SYS-DATA'], transition['changed'])
            self.assertEqual(['SYS-DATA'], transition['profile_slots_changed'])
            self.assertEqual(1, transition['proven_prefix_change_count'])
            self.assertEqual(1, transition['opaque_dynamic_changed_bytes'])
            self.assertFalse(report['retail_profile_writer_ready'])
            self.assertEqual(before_hash, (baseline / 'SYS-DATA').read_bytes())

    def test_scr_editor_previews_and_applies_variable_width_without_collateral(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'data'
            data_dir.mkdir()
            payload = _scr_container()
            (data_dir / 'ARCHIVE0.AR').write_bytes(payload)
            (data_dir / 'cdfiles.dat').write_bytes(
                _single_layout_b_index('NASCARTESTPLAYER_SCR.ARC', len(payload), 0)
            )
            installation = GameInstallation(PROFILES['nascar15'], root)
            editor = ScrEditor(installation)
            rows = editor.inventory()
            self.assertEqual(2, len(rows))
            target = next(row for row in rows if row['key'] == 'FRONT-DRAFT-DRAG')
            change = {**target, 'value': '0.875'}
            before_size = (data_dir / 'ARCHIVE0.AR').stat().st_size
            preview = editor.preview([change])
            self.assertEqual('append_repoint', preview['method'])
            self.assertEqual(before_size, (data_dir / 'ARCHIVE0.AR').stat().st_size)
            result = editor.apply([change])
            self.assertTrue(result['verified'])
            values = {row['key']: row['value'] for row in editor.inventory()}
            self.assertEqual('0.875', values['FRONT-DRAFT-DRAG'])
            self.assertEqual('0.80', values['REAR-DRAFT-DRAG'])

    def test_installed_ntg2013_retail_profile_sample_validates_when_present(self):
        path = Path(
            r'C:\Program Files (x86)\Steam\userdata\68478236\225220\remote\SYS-DATA'
        )
        if not path.is_file():
            self.skipTest('retail NTG2013 Steam Cloud sample is not installed')
        parsed = parse_gfs(path.read_bytes())
        self.assertEqual('PROFILEDATA', parsed.section_name)
        self.assertEqual(100310, parsed.payload_size)
        self.assertAlmostEqual(4.18, parsed.version, places=4)

    def test_installed_pyc_record_editor_preview_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        editor = PycRecordEditor(installation)
        rows = editor.records('race_laps')
        self.assertGreater(len(rows), 30)
        archive = installation.archive_pairs['0'].archive
        before = archive.stat().st_size
        row = rows[0]
        preview = editor.preview('race_laps', [{
            'uid': row['uid'], 'field': 'RaceLaps', 'value': int(row['RaceLaps']) + 1,
        }])
        self.assertTrue(preview['dry_run'])
        self.assertEqual(1, preview['affected_count'])
        self.assertEqual(before, archive.stat().st_size)

    def test_installed_number_bank_decodes_through_shared_texture_service_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        archive_key, entry = installation.find_entry(installation.profile.number_container)
        editor = TextureBankEditor(installation)
        rows = editor.entries(archive_key, entry.name)
        self.assertGreater(len(rows), 40)
        replaceable = next(row for row in rows if row['replace_supported'])
        self.assertIn(replaceable['format'], ('DXT1', 'DXT5', 'A8R8G8B8'))
        self.assertEqual(
            (replaceable['width'], replaceable['height']),
            editor.read_image(
                archive_key, entry.name, replaceable['name'],
            ).size,
        )
        compact = next((row for row in rows if row['format'] == 'A1R5G5B5'), None)
        if compact:
            self.assertFalse(compact['replace_supported'])

    def test_padded_a8r8g8b8_rows_decode_and_write_without_touching_padding(self):
        # Two logical BGRA pixels per row inside a four-pixel allocation row.
        row0 = bytes((0, 0, 255, 255, 0, 255, 0, 255)) + b'PADPAD!!'
        row1 = bytes((255, 0, 0, 255, 255, 255, 255, 255)) + b'keepme!!'
        payload = row0 + row1
        entry = {
            'payload_abs': 0, 'payload_size': len(payload), 'needed': 16,
            'w': 2, 'h': 2, 'fmt': 'A8R8G8B8', 'row_pitch': 16,
        }
        decoded = np.asarray(containers.multi_read_png(payload, entry))
        np.testing.assert_array_equal(decoded[0, 0], (255, 0, 0, 255))
        np.testing.assert_array_equal(decoded[1, 0], (0, 0, 255, 255))

        replacement = Image.new('RGBA', (2, 2), (12, 34, 56, 78))
        rewritten = containers.multi_write_png(payload, entry, replacement)
        self.assertEqual(payload[8:16], rewritten[8:16])
        self.assertEqual(payload[24:32], rewritten[24:32])
        self.assertTrue(np.all(np.asarray(containers.multi_read_png(rewritten, entry)) == (12, 34, 56, 78)))

    def test_padded_dxt5_block_rows_decode_the_logical_surface(self):
        red = containers.dxt5_encode(np.full((4, 4, 4), (255, 0, 0, 255), np.uint8))
        green = containers.dxt5_encode(np.full((4, 4, 4), (0, 255, 0, 255), np.uint8))
        padding = containers.dxt5_encode(np.full((4, 4, 4), (0, 0, 255, 255), np.uint8))
        payload = red + padding + green + padding
        entry = {
            'payload_abs': 0, 'payload_size': len(payload), 'needed': 32,
            'w': 4, 'h': 8, 'fmt': 'DXT5', 'row_pitch': 32,
            'dxt5_swapped': False,
        }
        decoded = np.asarray(containers.multi_read_png(payload, entry))
        self.assertGreater(decoded[:4, :, 0].mean(), 240)
        self.assertGreater(decoded[4:, :, 1].mean(), 240)
        self.assertLess(decoded[:, :, 2].mean(), 10)
        replacement = Image.new('RGBA', (4, 8), (255, 255, 0, 255))
        rewritten = containers.multi_write_png(
            payload, entry, replacement,
            encode_fn=lambda image, _format: containers.dxt5_encode(np.asarray(image)),
        )
        self.assertEqual(payload[16:32], rewritten[16:32])
        self.assertEqual(payload[48:64], rewritten[48:64])
        replaced = np.asarray(containers.multi_read_png(rewritten, entry))
        self.assertGreater(replaced[:, :, :2].mean(), 240)
        self.assertLess(replaced[:, :, 2].mean(), 10)

    def test_ntg2013_padded_texture_banks_decode_when_installed(self):
        root = INSTALLED_GAMES['nascar13']
        if not root.is_dir():
            self.skipTest('NASCAR The Game: 2013 is not installed')
        installation = GameInstallation('nascar13', root)
        expected = {
            'CAREERNUMBERS.ARC': ('00', 'A8R8G8B8', (32, 16), 256),
            '2DRIVERSELECTTDGEN.ARC': ('DRIVER_M_PHOTO', 'A8R8G8B8', (274, 275), 1152),
            'DRIVERDUEL.ARC': ('IMG_SUFFIX_ST', 'DXT5', (64, 32), 512),
        }
        for bank, (texture, texture_format, size, row_pitch) in expected.items():
            archive, _entry = installation.find_entry(bank)
            payload = installation.read_entry(bank, archive)
            entries, _base = containers.parse_multi_arc(
                payload, known_dims=(128, 64) if bank == 'CAREERNUMBERS.ARC' else None,
            )
            record = next(item for item in entries if item['name'] == texture)
            self.assertEqual(texture_format, record['fmt'], bank)
            self.assertEqual(row_pitch, record['row_pitch'], bank)
            self.assertEqual('padded_rows', record['surface_layout'], bank)
            self.assertEqual(size, containers.multi_read_png(payload, record).size, bank)

    def test_livery_and_car_material_mip_chains_keep_a_tight_base_surface(self):
        tested = 0
        for game_id, root in INSTALLED_GAMES.items():
            if not root.is_dir():
                continue
            installation = GameInstallation(game_id, root)
            livery = next(
                (
                    (key, entry.name)
                    for key in installation.archive_pairs
                    for entry in installation.entries(key)
                    if classify_livery_slot(installation.profile, entry.name)
                ),
                None,
            )
            self.assertIsNotNone(livery, game_id)
            texture_bank = texture_container_name(
                installation.profile.body_models[0][1]
            )
            checks = (
                (livery[0], livery[1], 'IMG_LIV'),
                (*installation.find_entry(texture_bank)[:1], texture_bank, 'Chassis.dds'),
                (*installation.find_entry(texture_bank)[:1], texture_bank, 'Tyre02.dds'),
            )
            for archive, bank, texture_name in checks:
                payload = installation.read_entry(bank, archive)
                entries, _base = containers.parse_multi_arc(payload)
                record = next(
                    item for item in entries
                    if item['name'].casefold() == texture_name.casefold()
                )
                block_size = 8 if record['fmt'] == 'DXT1' else 16
                tight_pitch = max(1, (record['w'] + 3) // 4) * block_size
                self.assertGreater(record['mip_count'], 1, (game_id, bank, texture_name))
                self.assertEqual('tight_base', record['surface_layout'], (game_id, bank, texture_name))
                self.assertEqual(tight_pitch, record['row_pitch'], (game_id, bank, texture_name))
                self.assertEqual(
                    (record['w'], record['h']),
                    containers.multi_read_png(payload, record).size,
                )
            tested += 1
        if not tested:
            self.skipTest('no supported games are installed')

    def test_named_small_and_wide_texture_rows_use_their_native_alignment(self):
        expected = {
            'VICT_DRIVER.ARC': {
                '2012_reutimann_eye_d.dds': ('DXT1', 256, 'padded_rows'),
                'Shared_Eye_N.dds': ('DXT1', 256, 'padded_rows'),
            },
            'NASCAR3_TEXTURES_X.ARC': {
                'noiseMask.tga': ('DXT1', 256, 'padded_rows'),
                'windowbolt.dds': ('DXT5', 512, 'padded_rows'),
                'windowbolt_N.dds': ('DXT1', 256, 'padded_rows'),
                'winnoise.dds': ('DXT1', 256, 'padded_rows'),
            },
            'TEAMSHOPPASS.ARC': {
                'NascarLogoA.dds': ('DXT1', 1024, 'tight_base'),
            },
        }
        tested = 0
        for game_id, root in INSTALLED_GAMES.items():
            if not root.is_dir():
                continue
            installation = GameInstallation(game_id, root)
            for bank, texture_expectations in expected.items():
                try:
                    archive, _entry = installation.find_entry(bank)
                except KeyError:
                    continue
                payload = installation.read_entry(bank, archive)
                entries, _base = containers.parse_multi_arc(payload)
                by_name = {item['name'].casefold(): item for item in entries}
                for texture_name, (texture_format, row_pitch, layout) in texture_expectations.items():
                    record = by_name[texture_name.casefold()]
                    self.assertEqual(texture_format, record['fmt'], (game_id, bank, texture_name))
                    self.assertEqual(row_pitch, record['row_pitch'], (game_id, bank, texture_name))
                    self.assertEqual(layout, record['surface_layout'], (game_id, bank, texture_name))
                    self.assertEqual(
                        (record['w'], record['h']),
                        containers.multi_read_png(payload, record).size,
                    )
                    tested += 1
        if not tested:
            self.skipTest('none of the named texture banks are installed')

    def test_installed_schedule_reorder_preview_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        editor = ScheduleEditor(installation)
        rows = editor.rows()
        self.assertEqual(36, len(rows))
        order = [row['uid'] for row in rows]
        order[0], order[1] = order[1], order[0]
        archive = installation.archive_pairs['0'].archive
        before = archive.stat().st_size
        preview = editor.preview_order(order)
        self.assertEqual(2, preview['change_count'])
        self.assertEqual(before, archive.stat().st_size)

    def test_installed_custom_schedule_repeat_preview_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        editor = ScheduleEditor(installation)
        source = editor.catalog()[0]
        slots = [{'source_uid': source['uid'], 'laps': source['laps']}] * 36
        archive = installation.archive_pairs['0'].archive
        before = archive.stat().st_size
        preview = editor.preview_custom(slots)
        self.assertTrue(preview['dry_run'])
        self.assertEqual(36, len(preview['slots']))
        self.assertGreater(preview['change_count'], 0)
        self.assertEqual(before, archive.stat().st_size)

    def test_installed_track_inventory_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        before = {key: pair.archive.stat().st_size for key, pair in installation.archive_pairs.items()}
        inventory = TrackInventory(installation)
        summary = inventory.summary()
        self.assertGreater(len(summary['rows']), 100)
        self.assertIn('Daytona', summary['tracks'])
        comparison = inventory.compare('Daytona', 'Talladega')
        self.assertIn('shared', comparison['summary'])
        self.assertEqual(before, {key: pair.archive.stat().st_size for key, pair in installation.archive_pairs.items()})

    def test_installed_team_link_preview_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        editor = TeamEditor(installation)
        catalog = editor.catalog()
        driver = catalog['drivers'][0]
        destination = next(team for team in catalog['teams'] if team['uid'] != driver['team_uid'])
        archive = installation.archive_pairs['0'].archive
        before = archive.stat().st_size
        preview = editor.preview([{
            'kind': 'driver_team', 'config_uid': driver['config_uid'], 'team_uid': destination['uid'],
        }])
        self.assertTrue(preview['dry_run'])
        self.assertEqual(1, preview['change_count'])
        self.assertEqual(before, archive.stat().st_size)

    def test_installed_managed_paint_catalog_is_read_only_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        state = ROOT / 'extra_schemes_v1.json'
        before_state = state.read_bytes() if state.is_file() else None
        before_archives = {key: pair.archive.stat().st_size for key, pair in installation.archive_pairs.items()}
        catalog = ManagedPaintEditor(installation, state).catalog()
        self.assertGreater(len(catalog['drivers']), 40)
        self.assertEqual(36, len(catalog['events']))
        self.assertEqual(before_archives, {key: pair.archive.stat().st_size for key, pair in installation.archive_pairs.items()})
        self.assertEqual(before_state, state.read_bytes() if state.is_file() else None)

    def test_installed_team_presentation_and_full_repair_scans_are_read_only(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        before = {
            key: (pair.archive.stat().st_size, pair.archive.stat().st_mtime_ns,
                  pair.index.stat().st_size, pair.index.stat().st_mtime_ns)
            for key, pair in installation.archive_pairs.items()
        }
        with tempfile.TemporaryDirectory() as folder:
            catalog = TeamPresentationEditor(installation, folder).catalog()
            scan = FullRepairEditor(installation, folder, app_version='test').check()
        self.assertEqual(46, len(catalog['drivers']))
        self.assertEqual(48, len(catalog['teams']))
        self.assertTrue(scan['ok'])
        after = {
            key: (pair.archive.stat().st_size, pair.archive.stat().st_mtime_ns,
                  pair.index.stat().st_size, pair.index.stat().st_mtime_ns)
            for key, pair in installation.archive_pairs.items()
        }
        self.assertEqual(before, after)

    def test_installed_shared_season_pack_export_validates_read_only(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        before = {
            key: (pair.archive.stat().st_size, pair.archive.stat().st_mtime_ns,
                  pair.index.stat().st_size, pair.index.stat().st_mtime_ns)
            for key, pair in installation.archive_pairs.items()
        }
        with tempfile.TemporaryDirectory() as folder:
            editor = SeasonPackEditor(installation, folder)
            payload, report = editor.export_bytes()
            inspected = editor.inspect_bytes(payload)
        self.assertEqual(report['counts'], inspected['counts'])
        self.assertEqual(3, inspected['version'])
        after = {
            key: (pair.archive.stat().st_size, pair.archive.stat().st_mtime_ns,
                  pair.index.stat().st_size, pair.index.stat().st_mtime_ns)
            for key, pair in installation.archive_pairs.items()
        }
        self.assertEqual(before, after)

    def test_installed_audio_banks_use_shared_read_only_service_when_present(self):
        root = INSTALLED_GAMES['nascar15']
        if not root.is_dir():
            self.skipTest('NASCAR 15 is not installed')
        installation = GameInstallation('nascar15', root)
        editor = AudioBankEditor(installation)
        banks = editor.banks()
        self.assertGreater(len(banks), 20)
        bank = min(banks, key=lambda row: row['size'])
        archive = installation.archive_pairs[bank['archive']].archive
        before = archive.stat().st_size
        info = editor.samples(bank['archive'], bank['name'])
        self.assertGreater(len(info['samples']), 0)
        sample = editor.sample_payload(
            bank['archive'], bank['name'], info['samples'][0]['index'], 'raw'
        )
        self.assertGreater(len(sample['payload']), 0)
        self.assertEqual(before, archive.stat().st_size)

    def test_ntg2013_primary_and_dormant_liveries_are_not_mixed(self):
        profile = PROFILES['nascar13']
        primary = 'LIVERY_DLC_LIV_24_GORDON_2013.ARC'
        alternate = 'LIVERY_DLC_LIV_24_GORDON_2013_2.ARC'
        dormant = 'LIVERY_12_JEFFGORDONFOURTH.ARC'
        self.assertTrue(matches_primary_livery(profile, primary))
        self.assertTrue(matches_primary_livery(profile, alternate))
        self.assertFalse(matches_primary_livery(profile, dormant))
        self.assertEqual('24', classify_livery_slot(profile, primary).number)
        self.assertIsNone(classify_livery_slot(profile, dormant))
        self.assertTrue(classify_livery_slot(profile, dormant, include_dormant=True).dormant)

    def test_ntg2013_generated_roster_uses_only_the_2013_series(self):
        rows = json.loads((ROOT / 'data' / 'nascar13' / 'drivers.json').read_text(encoding='utf-8'))
        self.assertEqual(43, len(rows))
        self.assertTrue(all(row['base'].startswith('2013_') for row in rows))
        self.assertTrue(all(matches_primary_livery('nascar13', row['slot']) for row in rows))

    def test_ntg2013_career_readiness_is_evidence_gated(self):
        report = json.loads(
            (ROOT / 'data' / 'verified_mappings' / 'nascar13.json').read_text(encoding='utf-8')
        )
        career = report['facts']['career_mode']
        self.assertTrue(career['asset_layer_intact'])
        self.assertTrue(career['data_layer_intact'])
        self.assertTrue(career['frontend_transition_intact'])
        self.assertTrue(career['career_resources']['SPRINTNUMS2012.ARC'])
        self.assertEqual('SPRINTNUMS2012.ARC', career['frontend_evidence']['frontend_boot_preload'])
        self.assertTrue(career['frontend_evidence']['binary_probe']['all_strings_present'])
        self.assertEqual(36, career['numbered_calendar_events'])
        self.assertFalse(career['safe_to_patch'])

    def test_user_library_preserves_other_config_and_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'unrelated': {'keep': True}}), encoding='utf-8')
            library = UserLibrary(path)
            saved = library.save_preset({
                'name': 'Daytona draft', 'kind': 'track',
                'changes': [{'field': 'DraftGrip', 'value': 0.8}],
            })
            library.add_pit_entry({'track': 'Daytona', 'note': 'Four tires worked'})
            self.assertEqual(1, len(library.presets()))
            self.assertEqual(1, len(library.pit_entries()))
            self.assertTrue(json.loads(path.read_text(encoding='utf-8'))['unrelated']['keep'])
            exported = library.export_presets_bytes()
            library.delete_preset(saved['preset']['id'])
            self.assertEqual(1, library.import_presets_bytes(exported)['imported'])

    def test_appdata_transfer_is_allowlisted_and_recoverable(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source = Path(source_folder)
            target = Path(target_folder)
            (source / 'config.json').write_text(json.dumps({'renames': {'A': 'B'}}), encoding='utf-8')
            (source / 'schemes').mkdir()
            (source / 'schemes' / 'paint.png').write_bytes(b'png')
            payload = AppDataManager(source, 'test').export_bytes()
            (target / 'config.json').write_text(json.dumps({'old': True}), encoding='utf-8')
            result = AppDataManager(target, 'test').import_bytes(payload)
            self.assertEqual(2, result['restored'])
            self.assertTrue(Path(result['recovery_path']).is_file())
            self.assertEqual(b'png', (target / 'schemes' / 'paint.png').read_bytes())
            self.assertEqual({'A': 'B'}, json.loads((target / 'config.json').read_text())['renames'])

    def test_support_diagnostics_exclude_secrets_and_game_payloads(self):
        reporter = SupportReporter(None, ROOT, ROOT, 'test')
        payload = reporter.diagnostics_bytes({'theme': 'dark', 'api_token': 'do-not-include'})
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual({'README.txt', 'diagnostics.json'}, set(archive.namelist()))
            report = json.loads(archive.read('diagnostics.json'))
        self.assertEqual('dark', report['config']['theme'])
        self.assertNotIn('api_token', report['config'])
        self.assertEqual({}, report['archives'])

    def test_multi_archive_paint_transaction_restores_indexes_state_and_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            for key, suffix in (('0', ''), ('1', '1')):
                (data / f'ARCHIVE{key}.AR').write_bytes(b'A' * 2048)
                (data / f'cdfiles{suffix}.dat').write_bytes(_single_layout_b_index(f'TEST{key}.ARC', 8, 4))
            installation = GameInstallation(PROFILES['nascar15'], root)
            state = root / 'extra_schemes_v1.json'
            state.write_bytes(b'{"before":true}')
            images = root / 'schemes' / 'extra'
            images.mkdir(parents=True)
            (images / 'existing.png').write_bytes(b'old-image')
            transaction = ManagedPaintTransaction(installation, state, images)
            snapshot = transaction.snapshot(('0', '1'))
            snapshot['image_overwrites']['existing.png'] = b'old-image'
            for pair in installation.archive_pairs.values():
                with pair.archive.open('ab') as handle:
                    handle.write(b'APPENDED')
                pair.index.write_bytes(b'broken index')
            state.write_bytes(b'{"after":true}')
            (images / 'existing.png').write_bytes(b'new-image')
            (images / 'created.png').write_bytes(b'created')
            self.assertEqual([], transaction.restore(snapshot))
            self.assertEqual(b'{"before":true}', state.read_bytes())
            self.assertEqual(b'old-image', (images / 'existing.png').read_bytes())
            self.assertFalse((images / 'created.png').exists())
            self.assertTrue(all(pair.archive.stat().st_size == 2048 for pair in installation.archive_pairs.values()))
            self.assertTrue(all(pair.index.read_bytes().startswith(b'filC') for pair in installation.archive_pairs.values()))

    def test_team_asset_transaction_restores_both_sidecar_states(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            for key, suffix in (('0', ''), ('1', '1')):
                (data / f'ARCHIVE{key}.AR').write_bytes(b'A' * 2048)
                (data / f'cdfiles{suffix}.dat').write_bytes(
                    _single_layout_b_index(f'TEAM{key}.ARC', 8, 4)
                )
            installation = GameInstallation(PROFILES['nascar15'], root)
            team_state = root / 'team_manager_state.json'
            paint_state = root / 'extra_schemes_v1.json'
            team_state.write_bytes(b'{"team":"before"}')
            paint_state.write_bytes(b'{"paint":"before"}')
            transaction = TeamAssetTransaction(installation, team_state, paint_state)
            snapshot = transaction.snapshot()
            checkpoint = TeamAssetCheckpoint(transaction, root / 'team_asset_rollback_v1')
            checkpoint.persist(snapshot, 'Synthetic team art change')
            for pair in installation.archive_pairs.values():
                with pair.archive.open('ab') as handle:
                    handle.write(b'APPENDED')
                pair.index.write_bytes(b'broken index')
            team_state.write_bytes(b'{"team":"after"}')
            paint_state.unlink()
            recovery = TeamPresentationRecovery(installation, root)
            self.assertTrue(recovery.status()['available'])
            result = recovery.restore()
            self.assertTrue(result['verified'])
            self.assertEqual(b'{"team":"before"}', team_state.read_bytes())
            self.assertEqual(b'{"paint":"before"}', paint_state.read_bytes())
            self.assertTrue(all(pair.archive.stat().st_size == 2048 for pair in installation.archive_pairs.values()))
            self.assertTrue(all(pair.index.read_bytes().startswith(b'filC') for pair in installation.archive_pairs.values()))
            checkpoint.persist(snapshot, 'Older checkpoint')
            paint_state.write_text(json.dumps({'schemes': [{'uid': 99}]}), encoding='utf-8')
            info = checkpoint.info()
            self.assertFalse(info['available'])
            self.assertIn('99', info['blocked_reason'])

    def test_append_transaction_restores_multiple_inplace_regions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            archive = data / 'ARCHIVE0.AR'
            index = data / 'cdfiles.dat'
            archive.write_bytes(bytes(range(256)) * 8)
            index.write_bytes(_single_layout_b_index('REGIONS.ARC', 32, 0))
            state = root / 'config.json'
            state.write_bytes(b'before')
            installation = GameInstallation(PROFILES['nascar15'], root)
            transaction = AppendRepointTransaction(installation)
            snapshot = transaction.snapshot(
                ('0',), state_files={'config': state}, inplace_regions=[
                    {'archive': archive, 'offset': 10, 'size': 4, 'name': 'first'},
                    {'archive': archive, 'offset': 30, 'size': 5, 'name': 'second'},
                ],
            )
            original_size = archive.stat().st_size
            with archive.open('r+b') as handle:
                handle.seek(10)
                handle.write(b'xxxx')
                handle.seek(30)
                handle.write(b'yyyyy')
                handle.seek(0, 2)
                handle.write(b'appended')
            index.write_bytes(b'broken')
            state.write_bytes(b'after')
            self.assertEqual([], transaction.restore(snapshot))
            self.assertEqual(original_size, archive.stat().st_size)
            payload = archive.read_bytes()
            self.assertEqual(bytes(range(10, 14)), payload[10:14])
            self.assertEqual(bytes(range(30, 35)), payload[30:35])
            self.assertEqual(b'before', state.read_bytes())
            self.assertTrue(index.read_bytes().startswith(b'filC'))

    def test_managed_paint_checkpoint_seals_verifies_loads_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            for key, suffix in (('0', ''), ('1', '1')):
                (data / f'ARCHIVE{key}.AR').write_bytes(b'A' * 2048)
                (data / f'cdfiles{suffix}.dat').write_bytes(
                    _single_layout_b_index(f'PAINT{key}.ARC', 8, 4)
                )
            installation = GameInstallation(PROFILES['nascar15'], root)
            state = root / 'extra_schemes.json'
            state.write_bytes(b'{"state":"before"}')
            images = root / 'schemes' / 'extra'
            images.mkdir(parents=True)
            (images / 'before.png').write_bytes(b'before')
            transaction = ManagedPaintTransaction(installation, state, images)
            checkpoint = ManagedPaintCheckpoint(transaction, root / 'extra_scheme_rollback_v1')
            snapshot = transaction.snapshot(('0', '1'))
            checkpoint.persist(snapshot, 'Synthetic paint create', {'type': 'create', 'uid': 42})
            for pair in installation.archive_pairs.values():
                with pair.archive.open('ab') as handle:
                    handle.write(b'APPENDED')
            state.write_bytes(b'{"state":"after"}')
            (images / 'after.png').write_bytes(b'after')
            manifest = checkpoint.seal()
            self.assertTrue(checkpoint.verify_post_state(manifest))
            loaded, loaded_manifest = checkpoint.load()
            self.assertEqual(42, loaded_manifest['operation']['uid'])
            self.assertEqual({'0', '1'}, set(loaded['groups']))
            result = ManagedPaintEditor(installation, state).undo()
            self.assertTrue(result['verified'])
            self.assertEqual(b'{"state":"before"}', state.read_bytes())
            self.assertEqual({'before.png'}, {path.name for path in images.iterdir()})
            self.assertFalse(checkpoint.rollback_dir.exists())

    def test_managed_paint_library_export_contains_only_owned_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            installation = GameInstallation(PROFILES['nascar15'], root)
            state = root / 'extra_schemes_v1.json'
            state.write_text(json.dumps({
                'schemes': [{
                    'uid': 42, 'source_png': '42.png',
                    'thumbnail_source_png': '42.thumbnail.png',
                }],
                'assignments': {},
            }), encoding='utf-8')
            images = root / 'schemes' / 'extra'
            images.mkdir(parents=True)
            (images / '42.png').write_bytes(b'paint')
            (images / '42.thumbnail.png').write_bytes(b'thumbnail')
            (images / 'unowned.png').write_bytes(b'private')
            payload = ManagedPaintEditor(installation, state).export_library_bytes()
            with zipfile.ZipFile(io.BytesIO(payload)) as package:
                names = set(package.namelist())
            self.assertIn('extra_schemes_v1.json', names)
            self.assertIn('paints/42.png', names)
            self.assertIn('paints/42.thumbnail.png', names)
            self.assertNotIn('paints/unowned.png', names)

    def test_native_livery_wrapper_writer_preserves_terminal_mip_and_footer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            installation = GameInstallation(PROFILES['nascar15'], root)
            wrapper = bytearray(SD_ENTRY_SIZE)
            wrapper[:4] = b'ARCC'
            wrapper[0xCC:0xD0] = b'DXT1'
            terminal = RAW_OFFSET + SD_OFFSETS[11]
            wrapper[terminal:] = bytes((index * 17) & 0xFF for index in range(len(wrapper) - terminal))
            table = len(wrapper) - 0x89
            struct.pack_into('<12I', wrapper, table, *SD_OFFSETS)
            struct.pack_into('<12I', wrapper, table + 48, *SD_PITCHES)
            original_footer = bytes(wrapper[terminal:])
            payload, levels, changed = NativeLiveryWrapperEditor(installation).patch_sd(
                bytes(wrapper), Image.new('RGB', (2048, 1024), (31, 127, 211))
            )
            self.assertEqual(SD_ENTRY_SIZE, len(payload))
            self.assertEqual(original_footer, payload[terminal:])
            self.assertEqual(11, len(levels))
            self.assertGreater(changed, 0)
            untouched, _levels, masked_changed = NativeLiveryWrapperEditor(installation).patch_sd(
                bytes(wrapper), Image.new('RGB', (2048, 1024), (255, 0, 0)),
                Image.new('L', (2048, 1024), 0),
            )
            self.assertEqual(bytes(wrapper), untouched)
            self.assertEqual(0, masked_changed)

    def test_stock_paint_editor_installs_and_verifies_through_shared_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            wrapper = bytearray(SD_ENTRY_SIZE)
            wrapper[:4] = b'ARCC'
            wrapper[0xCC:0xD0] = b'DXT1'
            table = len(wrapper) - 0x89
            struct.pack_into('<12I', wrapper, table, *SD_OFFSETS)
            struct.pack_into('<12I', wrapper, table + 48, *SD_PITCHES)
            name = 'LIVERY_15_1_TEST_PRIMARY.ARC'
            (data / 'ARCHIVE2.AR').write_bytes(wrapper)
            (data / 'cdfiles2.dat').write_bytes(_single_layout_b_index(name, len(wrapper), 0))
            source = root / 'paint.png'
            Image.new('RGB', (2048, 1024), (12, 34, 56)).save(source)
            editor = StockPaintEditor(GameInstallation('nascar15', root))
            result = editor.install(name, source, hd_name=False)
            self.assertTrue(result['verified'])
            self.assertGreater(result['sd_changed_bytes'], 0)
            self.assertTrue((data / 'ARCHIVE2.AR.n15mod.bak').is_file())

    def test_managed_paint_deleted_slot_redo_round_trips_post_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            for key, suffix in (('0', ''), ('1', '1')):
                (data / f'ARCHIVE{key}.AR').write_bytes(b'A' * 2048)
                (data / f'cdfiles{suffix}.dat').write_bytes(
                    _single_layout_b_index(f'REDO{key}.ARC', 8, 4)
                )
            installation = GameInstallation(PROFILES['nascar15'], root)
            state = root / 'extra_schemes_v1.json'
            state.write_bytes(b'{"state":"before"}')
            images = root / 'schemes' / 'extra'
            images.mkdir(parents=True)
            (images / 'before.png').write_bytes(b'before')
            transaction = ManagedPaintTransaction(installation, state, images)
            checkpoint = ManagedPaintCheckpoint(transaction, root / 'extra_scheme_rollback_v1')
            snapshot = transaction.snapshot(('0', '1'))
            checkpoint.persist(snapshot, 'Create slot 42', {'type': 'create', 'uid': 42})
            for pair in installation.archive_pairs.values():
                with pair.archive.open('ab') as handle:
                    handle.write(b'APPENDED')
            state.write_bytes(b'{"state":"after"}')
            (images / 'after.png').write_bytes(b'after')
            checkpoint.seal()
            editor = ManagedPaintEditor(installation, state)
            removed = editor.remove_latest_created(42)
            self.assertTrue(removed['exact_rollback'])
            self.assertEqual(b'{"state":"before"}', state.read_bytes())
            result = editor.undo()
            self.assertTrue(result['verified'])
            self.assertEqual(b'{"state":"after"}', state.read_bytes())
            self.assertEqual({'before.png', 'after.png'}, {path.name for path in images.iterdir()})
            self.assertTrue(all(pair.archive.stat().st_size == 2056 for pair in installation.archive_pairs.values()))

    def test_managed_paint_repair_transaction_rolls_back_failed_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            archive = data / 'ARCHIVE1.AR'
            index = data / 'cdfiles1.dat'
            archive.write_bytes(b'A' * 2048)
            index.write_bytes(_single_layout_b_index('PREVIEW.ARC', 8, 4))
            state = root / 'extra_schemes_v1.json'
            state.write_bytes(b'{"before":true}')
            images = root / 'schemes' / 'extra'
            images.mkdir(parents=True)
            editor = ManagedPaintEditor(GameInstallation(PROFILES['nascar15'], root), state)

            def fail_after_writes():
                with archive.open('ab') as handle:
                    handle.write(b'APPENDED')
                index.write_bytes(b'broken')
                state.write_bytes(b'{"after":true}')
                (images / 'created.png').write_bytes(b'created')
                raise ValueError('synthetic repair failure')

            with self.assertRaisesRegex(RuntimeError, 'synthetic repair failure'):
                editor._run_repair_transaction(('1',), fail_after_writes)
            self.assertEqual(2048, archive.stat().st_size)
            self.assertTrue(index.read_bytes().startswith(b'filC'))
            self.assertEqual(b'{"before":true}', state.read_bytes())
            self.assertFalse((images / 'created.png').exists())

    def test_managed_paint_uid_verdict_is_shared_and_state_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            state = root / 'extra_schemes_v1.json'
            editor = ManagedPaintEditor(GameInstallation(PROFILES['nascar15'], root), state)
            before = editor.uid_pool(include_live=False)
            uid = before['next_candidate']
            self.assertIsNotNone(uid)
            saved = editor.record_uid_verdict(uid, 'works', 'synthetic game test')
            self.assertIn(uid, saved['usable'])
            after = editor.uid_pool(include_live=False)
            self.assertIn(uid, after['user_verified'])
            self.assertEqual('synthetic game test', after['notes'][str(uid)])
            editor.record_uid_verdict(uid, 'untested')
            reset = editor.uid_pool(include_live=False)
            self.assertNotIn(uid, reset['user_verified'])
            self.assertIn(uid, reset['untested'])

    def test_cdf_layout_b_accepts_terminal_sentinel(self):
        entries = parse_cdf_bytes(_small_layout_b_index())
        self.assertEqual(['FIRST.ARC', 'SECOND.PYC'], [entry.name for entry in entries])
        self.assertTrue(all(entry.layout == 'B' for entry in entries))

    def test_shared_archive_editor_verifies_and_restores_exact_regions(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'ARCHIVE0.AR'
            index = Path(folder) / 'cdfiles.dat'
            archive.write_bytes(b'HEAD' + b'original' + b'TAIL')
            index.write_bytes(b'index')
            entry = CdfEntry(0, 'TEST.ARC', 8, 4, 0, 'A')

            class StubInstallation:
                archive_pairs = {'0': ArchivePair('0', archive, index)}

                def find_entry(self, name, archive_key=None):
                    self.assert_name = name
                    return '0', entry

                def read_entry(self, name, archive_key=None):
                    with archive.open('rb') as handle:
                        handle.seek(entry.archive_offset)
                        return handle.read(entry.size)

            editor = ArchiveEntryEditor(StubInstallation())
            result = editor.replace_entry('TEST.ARC', b'modified', '0')
            self.assertTrue(result['verified'])
            self.assertEqual(b'HEADmodifiedTAIL', archive.read_bytes())
            editor.restore_entry('TEST.ARC', '0')
            self.assertEqual(b'HEADoriginalTAIL', archive.read_bytes())
            with self.assertRaises(ValueError):
                editor.replace_entry('TEST.ARC', b'short', '0')

    def test_shared_archive_editor_repoints_variable_size_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / 'data'
            data_dir.mkdir()
            archive = data_dir / 'ARCHIVE0.AR'
            index = data_dir / 'cdfiles.dat'
            archive.write_bytes(b'HEAD' + b'original' + b'TAIL')
            index.write_bytes(_single_layout_b_index())
            installation = GameInstallation('nascar13', folder)
            editor = ArchiveEntryEditor(installation)
            result = editor.replace_or_repoint_entry('TEST.ARC', b'longer-value', '0')
            self.assertEqual('append_repoint', result['method'])
            self.assertEqual(b'longer-value', installation.read_entry('TEST.ARC', '0'))
            restored = editor.restore_entry('TEST.ARC', '0')
            self.assertEqual('append_repoint', restored['method'])
            self.assertEqual(b'original', installation.read_entry('TEST.ARC', '0'))

    def test_resource_editor_packages_and_restores_indexed_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'data'
            data_dir.mkdir()
            first, second = b'first-value', b'second-value'
            (data_dir / 'ARCHIVE0.AR').write_bytes(first + second)
            (data_dir / 'cdfiles.dat').write_bytes(_layout_b_index((
                ('FIRST.ARC', len(first), 0),
                ('SECOND.PYC', len(second), len(first)),
            )))
            installation = GameInstallation('nascar13', root)
            editor = ResourceEditor(installation)
            package = root / 'resources.zip'
            editor.export_package([('0', 'FIRST.ARC'), ('0', 'SECOND.PYC')], package)
            preview = editor.preview_package(package)
            self.assertEqual(2, preview['count'])
            editor.archive_editor.replace_entries({
                'FIRST.ARC': b'a much longer first value',
                'SECOND.PYC': b'x',
            }, '0')
            self.assertNotEqual(first, installation.read_entry('FIRST.ARC', '0'))
            result = editor.install_package(package)
            self.assertTrue(result['verified'])
            self.assertEqual(first, installation.read_entry('FIRST.ARC', '0'))
            self.assertEqual(second, installation.read_entry('SECOND.PYC', '0'))

    def test_backup_manager_creates_pairs_and_restores_them_transactionally(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'data'
            data_dir.mkdir()
            payload = b'original payload'
            archive = data_dir / 'ARCHIVE0.AR'
            index = data_dir / 'cdfiles.dat'
            archive.write_bytes(payload + bytes(2048 - len(payload)))
            original_index = _single_layout_b_index('TEST.ARC', len(payload), 0)
            index.write_bytes(original_index)
            installation = GameInstallation('nascar13', root)
            manager = BackupManager(installation)
            made = manager.create_missing()
            self.assertTrue(made['ok'])
            self.assertEqual(2, len(made['created']))

            archive.write_bytes(b'changed!' + archive.read_bytes()[8:])
            mutated_index = bytearray(index.read_bytes())
            struct.pack_into('<I', mutated_index, 0x50 + 16, 7)
            index.write_bytes(mutated_index)
            installation.invalidate_archive('0')
            restored = manager.restore_all()
            self.assertTrue(restored['verified'])
            self.assertEqual(payload, installation.read_entry('TEST.ARC', '0'))
            self.assertEqual(original_index, index.read_bytes())

    def test_shared_name_editor_updates_all_locales_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'game' / 'data'
            maps_dir = root / 'maps' / 'nascar13'
            data_dir.mkdir(parents=True)
            maps_dir.mkdir(parents=True)
            first = _lda(('Menu', 'Jamie McMurray', 'Exit'))
            second = _lda(('Garage', 'Jamie McMurray', 'Race'))
            archive = data_dir / 'ARCHIVE0.AR'
            archive.write_bytes(first + second)
            (data_dir / 'cdfiles.dat').write_bytes(_layout_b_index((
                ('TEXT0000.LDA', len(first), 0),
                ('TEXT0100.LDA', len(second), len(first)),
            )))
            (maps_dir / 'drivers.json').write_text(json.dumps([{
                'driver_uid': 42,
                'number': '1',
                'display_name': 'Jamie McMurray',
                'name_candidates': ['Jamie McMurray'],
            }]), encoding='utf-8')

            installation = GameInstallation('nascar13', root / 'game')
            editor = DriverNameEditor(installation, root / 'maps')
            before = editor.drivers()[0]
            self.assertTrue(before['available'])
            self.assertEqual(2, len(before['targets']))

            renamed = editor.rename(42, 'Jamie McMurray-Smith')
            self.assertEqual(2, renamed['tables'])
            self.assertEqual('Jamie McMurray-Smith', editor.drivers()[0]['current'])
            no_op = editor.rename(42, 'Jamie McMurray-Smith')
            self.assertEqual(0, no_op['tables'])

            restored = editor.restore(42)
            self.assertEqual(2, restored['tables'])
            self.assertEqual('Jamie McMurray', editor.drivers()[0]['current'])

    def test_shared_driver_handle_editor_repoints_verifies_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game, maps = root / 'game', root / 'maps'
            (game / 'data').mkdir(parents=True)
            maps.mkdir()
            payload = _python2_handle_pyc('JAMIE_MAC')
            (game / 'data' / 'ARCHIVE0.AR').write_bytes(payload)
            (game / 'data' / 'cdfiles.dat').write_bytes(
                _single_layout_b_index('DB_GAME_LOCAL_SCRIPT.PYC', len(payload), 0)
            )
            (maps / 'drivers.json').write_text(json.dumps([{
                'driver_uid': 42, 'number': '1', 'handle': 'JAMIE_MAC',
            }]), encoding='utf-8')
            editor = DriverHandleEditor(
                GameInstallation('nascar15', game), root / 'config.json', maps,
            )
            self.assertTrue(editor.handles()[0]['available'])
            renamed = editor.rename(42, 'JAMIE_MCMURRAY')
            self.assertTrue(renamed['verified'])
            self.assertEqual('JAMIE_MCMURRAY', editor.handles()[0]['current'])
            restored = editor.restore(42)
            self.assertTrue(restored['verified'])
            self.assertEqual('JAMIE_MAC', editor.handles()[0]['current'])

    def test_shared_text_editor_validates_tokens_batches_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'data'
            data_dir.mkdir()
            first = _lda(('Lap %d', 'Career', 'Exit'))
            second = _lda(('Press A', 'Loading...', 'Error'))
            (data_dir / 'ARCHIVE0.AR').write_bytes(first + second)
            (data_dir / 'cdfiles.dat').write_bytes(_layout_b_index((
                ('TEXT0000.LDA', len(first), 0),
                ('TEXT0100.LDA', len(second), len(first)),
            )))
            installation = GameInstallation('nascar13', root)
            editor = TextTableEditor(installation)
            self.assertEqual(3, len(editor.entries('TEXT0000.LDA')))
            with self.assertRaises(ValueError):
                editor.apply('TEXT0000.LDA', 0, 'Lap')
            result = editor.apply('TEXT0000.LDA', 0, 'Current lap: %d')
            self.assertTrue(result['verified'])
            self.assertEqual('Current lap: %d', editor.entries('TEXT0000.LDA')[0]['current'])
            batch = editor.apply_batch([
                {'file': 'TEXT0000.LDA', 'index': 1, 'new': 'Season Career'},
                {'file': 'TEXT0100.LDA', 'index': 0, 'new': 'Press Enter'},
            ])
            self.assertEqual(2, batch['files'])
            self.assertEqual(2, batch['changes'])
            self.assertEqual('Season Career', editor.entries('TEXT0000.LDA')[1]['current'])
            self.assertEqual('Press Enter', editor.entries('TEXT0100.LDA')[0]['current'])
            exact = editor.replace_exact('Loading...', 'Please wait')
            self.assertEqual(1, exact['changes'])
            self.assertEqual('Please wait', editor.entries('TEXT0100.LDA')[1]['current'])
            exported = editor.export_csv_bytes('TEXT0000.LDA')
            self.assertIn(b'Current lap: %d', exported)
            restored = editor.restore('TEXT0000.LDA', 0)
            self.assertTrue(restored['verified'])
            self.assertEqual('Lap %d', editor.entries('TEXT0000.LDA')[0]['current'])
            editor.restore_file('TEXT0100.LDA')
            self.assertEqual('Press A', editor.entries('TEXT0100.LDA')[0]['current'])

    def test_shared_ratings_editor_repoints_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / 'game' / 'data'
            maps_dir = root / 'maps' / 'nascar13'
            data_dir.mkdir(parents=True)
            maps_dir.mkdir(parents=True)
            pyc = _python2_rating_pyc((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7))
            (data_dir / 'ARCHIVE0.AR').write_bytes(pyc)
            (data_dir / 'cdfiles.dat').write_bytes(
                _single_layout_b_index('DB_AICONFIG_SCRIPT.PYC', len(pyc), 0)
            )
            headers = ['profile_id']
            values = ['9001']
            for index, field in enumerate(RATING_FIELDS):
                headers.extend((field, field + '_load_offset_hex'))
                values.extend((str((index + 1) / 10), hex(index * 3)))
            (maps_dir / 'ai_profiles_nascar13.csv').write_text(
                ','.join(headers) + '\n' + ','.join(values) + '\n', encoding='utf-8'
            )
            (maps_dir / 'drivers.json').write_text(json.dumps([{
                'driver_uid': 42,
                'profile_id': 9001,
                'number': '1',
                'display_name': 'Synthetic Driver',
                'slot': 'TEST.ARC',
            }]), encoding='utf-8')

            installation = GameInstallation('nascar13', root / 'game')
            editor = RatingsEditor(installation, root / 'maps')
            self.assertEqual(10, editor.ratings()[0]['stats']['skill'])
            changed = editor.set_rating(9001, 'skill', 33)
            self.assertTrue(changed['verified'])
            self.assertTrue(changed['repoint'])
            self.assertEqual(33, editor.ratings()[0]['stats']['skill'])
            restored = editor.restore(9001)
            self.assertTrue(restored['verified'])
            self.assertEqual(10, editor.ratings()[0]['stats']['skill'])

    def test_committed_live_mapping_audits_pass(self):
        for game_id in PROFILES:
            report_path = ROOT / 'data' / 'verified_mappings' / f'{game_id}.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertTrue(report['ok'], game_id)
            self.assertFalse(report['errors'], game_id)
            self.assertGreater(report['facts']['total_entries'], 3000, game_id)
            self.assertEqual('nascar-modding-app-live-mapping-audit-v2', report['format'])

    def test_every_available_live_index_is_in_bounds(self):
        for game_id, root in INSTALLED_GAMES.items():
            if not root.is_dir():
                continue
            installation = GameInstallation(game_id, root)
            for key, pair in installation.archive_pairs.items():
                archive_size = pair.archive.stat().st_size
                for entry in installation.entries(key):
                    self.assertLessEqual(
                        entry.archive_offset + entry.size, archive_size,
                        f'{game_id} ARCHIVE{key} {entry.name}',
                    )

    def test_real_default_car_meshes_parse_when_installed(self):
        tested = 0
        for game_id, root in INSTALLED_GAMES.items():
            if not root.is_dir():
                continue
            installation = GameInstallation(game_id, root)
            archive, body_name = installation.profile.body_models[0]
            body = load_mesh(installation.read_entry(body_name, archive), body_name)
            wheel_name = body_name.replace('_BODY0_0.ARC', '_ALLOY0_0.ARC')
            wheel_archive, _entry = installation.find_entry(wheel_name)
            wheel = load_mesh(installation.read_entry(wheel_name, wheel_archive), wheel_name)
            scene = assemble_car_scene(body, wheel)
            material_report = resolve_game_materials(
                installation, body_name, scene, max_dimension=256,
            )
            self.assertGreater(scene.vertex_count, 70_000, game_id)
            self.assertGreater(scene.triangle_count, 80_000, game_id)
            low, high = scene.bounds
            self.assertGreater(high[0] - low[0], 1.8, game_id)
            self.assertGreater(high[1] - low[1], 4.8, game_id)
            self.assertGreater(material_report['decoded_textures'], 0, game_id)
            self.assertIn('tyre02.dds', scene.texture_images, game_id)
            self.assertEqual(
                scene.materials['Tyre'].texture_for(0), 'Tyre02.dds', game_id,
            )
            for corner, anchor_name in (
                ('front_right', 'Alloy01-AP'), ('front_left', 'Alloy02-AP'),
                ('rear_right', 'Alloy03-AP'), ('rear_left', 'Alloy04-AP'),
            ):
                wheel_parts = [
                    part for part in scene.parts if part.name.startswith(f'{corner}/')
                ]
                wheel_vertices = np.vstack([part.vertices for part in wheel_parts])
                center = (wheel_vertices.min(axis=0) + wheel_vertices.max(axis=0)) * 0.5
                np.testing.assert_allclose(
                    body.attachment_points[anchor_name], center, atol=1e-5,
                    err_msg=f'{game_id} {corner}',
                )

            variants = {
                load_mesh(
                    installation.read_entry(body_name, archive), body_name,
                    alternative=alternative,
                ).vertex_count
                for alternative, _label in installation.profile.car_manufacturers
            }
            self.assertEqual(
                len(installation.profile.car_manufacturers), len(variants), game_id
            )
            tested += 1
        if not tested:
            self.skipTest('no supported games are installed')

    def test_mesh_material_roles_and_texture_pack_mapping(self):
        material = MeshMaterial('Paint', (
            MaterialTextureBinding(1, 'paint_normal.dds'),
            MaterialTextureBinding(0, 'paint.dds'),
            MaterialTextureBinding(12, 'paint_specular.dds'),
        ))
        self.assertEqual(material.texture_for(0), 'paint.dds')
        self.assertEqual(material.texture_for(2, 12), 'paint_specular.dds')
        self.assertIsNone(material.texture_for(7))
        self.assertEqual(
            texture_container_name('NASCAR4_BODY0_0.ARC'),
            'NASCAR4_TEXTURES_X.ARC',
        )

    def test_car_wheels_use_authored_attachment_points(self):
        vertices = np.asarray(
            ((0.65, -1.95, -0.7), (0.95, -1.95, 0.1),
             (0.95, -1.25, -0.7), (0.65, -1.25, 0.1)),
            dtype=np.float32,
        )
        part = MeshPart(
            'alloy', 'Tyre', vertices, np.zeros_like(vertices),
            np.zeros((4, 2), dtype=np.float32),
            np.asarray((0, 1, 2, 1, 3, 2), dtype=np.uint32),
        )
        targets = {
            'Alloy01-AP': np.asarray((0.8, -1.6, -0.3), dtype=np.float32),
            'Alloy02-AP': np.asarray((-0.8, -1.6, -0.3), dtype=np.float32),
            'Alloy03-AP': np.asarray((0.8, 1.2, -0.3), dtype=np.float32),
            'Alloy04-AP': np.asarray((-0.8, 1.2, -0.3), dtype=np.float32),
        }
        body = MeshScene([], 'body', targets)
        scene = assemble_car_scene(body, MeshScene([part], 'wheel'))
        for corner, anchor_name in (
            ('front_right', 'Alloy01-AP'), ('front_left', 'Alloy02-AP'),
            ('rear_right', 'Alloy03-AP'), ('rear_left', 'Alloy04-AP'),
        ):
            placed = next(item for item in scene.parts if item.name.startswith(f'{corner}/'))
            center = (placed.vertices.min(axis=0) + placed.vertices.max(axis=0)) * 0.5
            np.testing.assert_allclose(targets[anchor_name], center, atol=1e-6)

    def test_profiles_expose_verified_manufacturer_geometry_ids(self):
        self.assertEqual(
            ((0, 'Chevrolet'), (2, 'Ford'), (3, 'Toyota')),
            PROFILES['nascar15'].car_manufacturers,
        )
        for game_id in ('nascar13', 'nascar14'):
            self.assertEqual(
                ((0, 'Chevrolet'), (1, 'Dodge'), (2, 'Ford'), (3, 'Toyota')),
                PROFILES[game_id].car_manufacturers,
            )

    def test_real_primary_rosters_have_verified_names_and_ratings(self):
        expected = {'nascar13': 43, 'nascar14': 48, 'nascar15': 46}
        tested = 0
        for game_id, root in INSTALLED_GAMES.items():
            if not root.is_dir():
                continue
            installation = GameInstallation(game_id, root)
            names = DriverNameEditor(installation).drivers()
            ratings = RatingsEditor(installation).ratings()
            self.assertEqual(expected[game_id], len(names), game_id)
            self.assertEqual(expected[game_id], len(ratings), game_id)
            self.assertTrue(all(row['available'] for row in names), game_id)
            self.assertEqual(
                {row['driver_uid'] for row in names},
                {row['driver_uid'] for row in ratings},
                game_id,
            )
            tested += 1
        if not tested:
            self.skipTest('no supported games are installed')


if __name__ == '__main__':
    unittest.main()
