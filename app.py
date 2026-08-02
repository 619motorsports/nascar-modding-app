#!/usr/bin/env python3
# NASCAR Modding App v1.0.2 - public release
import csv, io, json, os, re, shutil, struct, subprocess, tempfile, webbrowser, math as _math, collections, time, threading, zipfile, hashlib
import numpy as np
from PIL import Image, ImageFilter, ImageDraw
from flask import Flask, jsonify, request, send_file, send_from_directory, after_this_request, Response, g
import containers as C
from nascar_modding.core.cdf import (
    legacy_tuples as _read_cdf_tuples,
    read_cdf as _read_cdf_entries,
)
from nascar_modding.core.files import atomic_write_bytes, atomic_write_json
from nascar_modding.core.modules import load_module
from nascar_modding.core.processes import is_process_running
from nascar_modding.editing.archive import (
    LEGACY_BACKUP_SUFFIX,
    MOD_BACKUP_SUFFIX,
    backup_path,
)
from nascar_modding.editing.backups import BackupManager
from nascar_modding.editing.audio import AudioBankEditor, audio_category as _shared_audio_category
from nascar_modding.editing.audio_tools import AudioToolsManager
from nascar_modding.editing.appdata import AppDataManager
from nascar_modding.editing.ratings import (
    PYC_CODE_BASE as SHARED_PYC_CODE_BASE,
    RATING_FIELDS,
    RATING_LABELS,
    RatingsEditor,
)
from nascar_modding.editing.names import DriverHandleEditor, DriverNameEditor
from nascar_modding.editing.resources import ResourceEditor
from nascar_modding.editing.pyc_records import (
    AI_GLOBAL_FIELDS as SHARED_AI_GLOBAL_FIELDS,
    AI_TRACK_FIELDS as SHARED_AI_TRACK_FIELDS,
    WORLD_PACE_FIELDS as SHARED_WORLD_PACE_FIELDS,
    PycRecordEditor,
    coerce_scalar_like as _shared_coerce_scalar_like,
    exact_field_variant as _shared_exact_field_variant,
    mapped_rows_from_pyc_bytes as _shared_mapped_rows_from_pyc_bytes,
    patch_load_const_operand as _shared_patch_load_const_operand,
    scalar_same_type as _shared_scalar_same_type,
)
from nascar_modding.editing.scr import (
    CATEGORY_ORDER as SCR_CATEGORY_ORDER,
    ScrEditor,
    parse_numeric_rows as _shared_scr_parse_numeric_rows,
    scr_category as _shared_scr_category,
    scr_context as _shared_scr_context,
    scr_role as _shared_scr_role,
    scr_track as _shared_scr_track,
    scr_wheel as _shared_scr_wheel,
)
from nascar_modding.editing.schedule import ScheduleEditor
from nascar_modding.editing.season_packs import SeasonPackEditor
from nascar_modding.editing.text_tables import (
    TEXT_CATEGORIES,
    TextTableEditor,
)
from nascar_modding.editing.textures import TextureBankEditor
from nascar_modding.editing.teams import TeamEditor, TeamPresentationRecovery
from nascar_modding.editing.team_presentation import TeamPresentationEditor
from nascar_modding.editing.managed_paints import ManagedPaintEditor
from nascar_modding.editing.full_repair import FullRepairEditor
from nascar_modding.editing.livery_wrappers import (
    HD_DIMS as _NATIVE_HD_DIMS, HD_ENTRY_SIZE as _NATIVE_HD_ENTRY_SIZE,
    HD_OFFSETS as _NATIVE_HD_MIP_OFFSETS, HD_PITCHES as _NATIVE_HD_MIP_PITCHES,
    HD_ROLLS as _NATIVE_HD_LARGE_ROLL, SD_DIMS as _NATIVE_SD_DIMS,
    SD_ENTRY_SIZE as _NATIVE_SD_ENTRY_SIZE, SD_OFFSETS as _NATIVE_SD_MIP_OFFSETS,
    SD_PITCHES as _NATIVE_SD_MIP_PITCHES, SD_ROLLS as _NATIVE_SD_LARGE_ROLL,
    NativeLiveryWrapperEditor,
)
from nascar_modding.editing.stock_paints import StockPaintEditor
from nascar_modding.editing.transactions import (
    AppendRepointTransaction, ManagedPaintCheckpoint, ManagedPaintTransaction, TeamAssetCheckpoint,
    TeamAssetTransaction,
)
from nascar_modding.editing.user_library import UserLibrary
from nascar_modding.formats import audio as _shared_audio
from nascar_modding.formats.python2_pyc import (
    rebuild_with_float_constant as _stat_rebuild_with_const,
    root_constants as _pyc_consts,
    root_layout as _stat_root_layout,
)
from nascar_modding.games.assets import classify_livery_slot, livery_asset_words
from nascar_modding.games.installation import GameInstallation
from nascar_modding.games.profiles import GAME_PROFILES
from nascar_modding.verification.tracks import TrackInventory
from nascar_modding.verification.support import SupportReporter

import sys
import datetime
from pathlib import Path
if getattr(sys, 'frozen', False):
    RES_DIR  = sys._MEIPASS                     # bundled static/data/texconv
    USER_DIR = os.path.dirname(sys.executable)  # config/schemes live next to the exe
else:
    RES_DIR  = os.path.dirname(os.path.abspath(__file__))
    USER_DIR = RES_DIR
APP_DIR = RES_DIR
DATA = os.path.join(RES_DIR, 'data')
INTERNAL_TOOLS_DIR = os.path.join(RES_DIR, 'internal_tools')
if os.path.isdir(INTERNAL_TOOLS_DIR) and INTERNAL_TOOLS_DIR not in sys.path:
    sys.path.insert(0, INTERNAL_TOOLS_DIR)

def component_path(name):
    """Locate a bundled component without exposing helper scripts in the app root."""
    candidates = [
        os.path.join(INTERNAL_TOOLS_DIR, name),
        os.path.join(APP_DIR, name),
        os.path.join(DATA, name),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def _load_module_from_path(
    path, module_name, *, missing_message=None, load_message=None
):
    """Load one backend module so import validation is implemented once."""
    return load_module(
        path,
        module_name,
        add_parent=True,
        missing_message=missing_message,
        load_message=load_message,
    )


def _load_internal_module(
    helper_name, module_name, missing_message=None, load_message=None
):
    """Load one bundled backend helper through the canonical component path."""
    return _load_module_from_path(
        component_path(helper_name),
        module_name,
        missing_message=(
            missing_message
            or f'{helper_name} is missing from the internal tools folder'
        ),
        load_message=load_message,
    )


SELECTOR_CONFIG = os.path.join(USER_DIR, 'game_selector.json')
ACTIVE_GAME = 'nascar15'
GAME_SESSION_SELECTED = False
_GAME_SWITCH_LOCK = threading.RLock()

def _profile_dir(game_id=None):
    gid = game_id or ACTIVE_GAME
    if gid == 'nascar15':
        return USER_DIR
    return os.path.join(USER_DIR, 'profiles', gid)

def _profile_config_path(game_id=None):
    return os.path.join(_profile_dir(game_id), 'config.json')

def _profile_schemes_path(game_id=None):
    return os.path.join(_profile_dir(game_id), 'schemes')

CONFIG = _profile_config_path(ACTIVE_GAME)
SCHEMES = _profile_schemes_path(ACTIVE_GAME)
os.makedirs(SCHEMES, exist_ok=True)
app = Flask(__name__, static_folder=os.path.join(RES_DIR,'static'))

RAW_OFFSET = 0x100
# Fallback only. NASCAR 15 normally builds the Names list from live RACETEAM_c
# records so active teams such as Phil Parsons Racing and JR Motorsports are not
# lost, and unused legacy teams are not shown.
TEAMS_2015 = ["Chip Ganassi Racing","Front Row Motorsports","Roush Fenway Racing",
 "Richard Childress Racing","Team Penske","Joe Gibbs Racing","Hendrick Motorsports",
 "Stewart-Haas Racing","Germain Racing","Michael Waltrip Racing","Richard Petty Motorsports",
 "Furniture Row Racing","Wood Brothers Racing","HScott Motorsports","JTG Daugherty Racing",
 "Go FAS Racing","BK Racing","Leavine Family Racing","Phil Parsons Racing","JR Motorsports",
 "Custom Chevrolet","Custom Ford","Custom Toyota"]
TEAMS_2014 = ["Richard Petty Motorsports","JTG Daugherty Racing","Front Row Motorsports",
 "Roush Fenway Racing","Richard Childress Racing","Joe Gibbs Racing","Team Penske",
 "Hendrick Motorsports","Stewart-Haas Racing","Michael Waltrip Racing","Furniture Row Racing",
 "Germain Racing","Tommy Baldwin Racing","Wood Brothers Racing","Chip Ganassi Racing",
 "Leavine Family Racing","Phil Parsons Racing","NEMCO Motorsports","Hillman Racing",
 "XXXtreme Motorsport","Swan Racing","BK Racing","Phoenix Racing","JR Motorsports","Go Green Racing","PH LLC"]

APP_NAME = 'NASCAR Modding App'
APP_VERSION = '1.0.2'
APP_RELEASE_LABEL = 'Public release'

def _valid_backup(path, kind):
    """Is this file trustworthy enough to copy back over a live game file?

    FIX (v1.0.2-dev6): /api/restore calls this three times, but the definition
    was lost when the module was reorganised for dev2.  Python only resolves a
    global at call time, so the package imported cleanly, every route listed
    fine, and the failure only appeared the moment a user pressed Restore --
    as a bare NameError.  That is the whole of "all the restoring stuff
    failed".  Behaviour below is the proven v1.0 rule, unchanged.

    kind is 'cdf' (must carry the filC magic) or 'ar' (an archive blob, whose
    outer header varies, so only a plausible size is required).
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 64:
            return False
        with open(path, 'rb') as fh:
            head = fh.read(4)
    except OSError:
        return False
    if kind == 'cdf':
        return head == b'filC'
    return os.path.getsize(path) > 1024


class RollbackFailed(RuntimeError):
    """An install failed AND its rollback also failed.

    This is a different situation from a plain install failure and needs
    different user action, so it must never be collapsed into the original
    error or silently discarded.
    """

    def __init__(self, install_error, rollback_error):
        self.install_error = install_error
        self.rollback_error = rollback_error
        super().__init__(
            'INSTALL FAILED AND ROLLBACK FAILED - this archive may be inconsistent. '
            'Do not launch the game. Use Restore from backup before doing anything else. '
            f'Install error: {install_error} | Rollback error: {rollback_error}')


def rollback_archive_cdf(v, archive_size, cdf_bytes, tmp_suffix, install_error):
    """Truncate an archive back to `archive_size` and restore exact cdf bytes.

    Raises RollbackFailed if the restore cannot complete. Callers must not
    wrap this in a bare `except Exception: pass` - a rollback that fails
    quietly leaves a truncated archive behind a stale index while telling the
    user the operation was atomic.
    """
    try:
        with open(v['ar'], 'r+b') as fh:
            fh.truncate(archive_size)
            fh.flush()
            os.fsync(fh.fileno())
        atomic_write_bytes(v['cdf'], cdf_bytes, tmp_suffix)
    except Exception as rb:
        raise RollbackFailed(install_error, rb) from install_error


# ---------------- config / selected game ----------------
def load_cfg():
    try:
        return json.load(open(CONFIG, encoding='utf-8')) if os.path.exists(CONFIG) else {}
    except Exception:
        return {}

def save_cfg(c):
    atomic_write_json(CONFIG, c, indent=1)

def _load_selector_cfg():
    try:
        return json.load(open(SELECTOR_CONFIG, encoding='utf-8')) if os.path.exists(SELECTOR_CONFIG) else {}
    except Exception:
        return {}

def _save_selector_cfg(data):
    atomic_write_json(SELECTOR_CONFIG, data, indent=1)

def active_game_profile():
    return GAME_PROFILES[ACTIVE_GAME]

def _limited_editor_profile():
    """Whether the compatibility editor must use conservative write policy."""
    return not bool(active_game_profile().get('full_feature_set'))

def _profile_scoped_state(default_path, filename):
    """Keep non-default game state isolated behind one path policy."""
    if active_game_profile().get('data_subdir'):
        return os.path.join(_profile_dir(),filename)
    return default_path

def active_game_name():
    return active_game_profile()['name']

def _lock_busy(lock):
    """True when another request currently owns a protected write lock."""
    try: acquired=lock.acquire(blocking=False)
    except TypeError: acquired=lock.acquire(False)
    if acquired:
        lock.release(); return False
    return True


def _protected_operation_busy():
    busy=[]
    for name in ('_EXTRA_CREATE_LOCK','_TEAM_MANAGER_LOCK','_FULL_REPAIR_LOCK','_RP_LOCK'):
        lock=globals().get(name)
        if lock is not None and _lock_busy(lock): busy.append(name)
    return busy


def _activate_game(game_id):
    global ACTIVE_GAME, CONFIG, SCHEMES
    global EXTRA_SCHEME_STATE, EXTRA_SCHEME_IMAGES, EXTRA_SCHEME_ROLLBACK_DIR
    global TEAM_MANAGER_STATE, TEAM_ASSET_ROLLBACK_DIR, _RP_HISTORY
    if game_id not in GAME_PROFILES: raise ValueError('unsupported game profile')
    busy=_protected_operation_busy()
    if busy: raise RuntimeError('cannot change games while a protected operation is running: '+', '.join(busy))
    snapshot=dict(active=ACTIVE_GAME,config=CONFIG,schemes=SCHEMES,
                  extra=globals().get('EXTRA_SCHEME_STATE'),
                  extra_images=globals().get('EXTRA_SCHEME_IMAGES'),extra_rollback=globals().get('EXTRA_SCHEME_ROLLBACK_DIR'),
                  team=globals().get('TEAM_MANAGER_STATE'),rollback=globals().get('TEAM_ASSET_ROLLBACK_DIR'),rp=globals().get('_RP_HISTORY'))
    new_config=_profile_config_path(game_id); new_schemes=_profile_schemes_path(game_id)
    os.makedirs(new_schemes, exist_ok=True)
    try:
        ACTIVE_GAME=game_id; CONFIG=new_config; SCHEMES=new_schemes
        if 'EXTRA_SCHEME_STATE' in globals(): EXTRA_SCHEME_STATE=os.path.join(_profile_dir(game_id),'extra_schemes_v1.json')
        if 'EXTRA_SCHEME_IMAGES' in globals(): EXTRA_SCHEME_IMAGES=os.path.join(SCHEMES,'extra')
        if 'EXTRA_SCHEME_ROLLBACK_DIR' in globals(): EXTRA_SCHEME_ROLLBACK_DIR=os.path.join(_profile_dir(game_id),'extra_scheme_rollback_v1')
        if 'TEAM_MANAGER_STATE' in globals(): TEAM_MANAGER_STATE=os.path.join(_profile_dir(game_id),'team_manager_state.json')
        if 'TEAM_ASSET_ROLLBACK_DIR' in globals(): TEAM_ASSET_ROLLBACK_DIR=os.path.join(_profile_dir(game_id),'team_asset_rollback_v1')
        if '_RP_HISTORY' in globals(): _RP_HISTORY=os.path.join(_profile_dir(game_id),'repoint_history.json')
        for cache_name in ('_UI_TEXT_FILE_CACHE','_BASELINE_VERIFY_CACHE','_UI_THUMB_CACHE','_TRACK_CACHE','_CDF_ENTRY_CACHE','_PYC_AUDIT_CACHE'):
            cache=globals().get(cache_name)
            if isinstance(cache,dict): cache.clear()
        text_cache=globals().get('_UI_TEXT_CACHE')
        if isinstance(text_cache,dict): text_cache.clear(); text_cache.update(signature=None,rows=None,files=None,errors=None)
        packaged=globals().get('_UI_PACKAGED_MAP_CACHE')
        if isinstance(packaged,dict): packaged.clear(); packaged.update(signature=None,rows={})
        ui_index=globals().get('_UI_INDEX_CACHE')
        if isinstance(ui_index,dict): ui_index.clear(); ui_index.update(signature=None,rows=None)
        selector=_load_selector_cfg(); selector['last_game']=game_id; _save_selector_cfg(selector)
    except Exception:
        ACTIVE_GAME=snapshot['active']; CONFIG=snapshot['config']; SCHEMES=snapshot['schemes']
        for name,key in (('EXTRA_SCHEME_STATE','extra'),('EXTRA_SCHEME_IMAGES','extra_images'),('EXTRA_SCHEME_ROLLBACK_DIR','extra_rollback'),('TEAM_MANAGER_STATE','team'),('TEAM_ASSET_ROLLBACK_DIR','rollback'),('_RP_HISTORY','rp')):
            if name in globals(): globals()[name]=snapshot[key]
        raise


def _steam_library_roots():
    """Real Steam library paths from libraryfolders.vdf, plus common fallbacks.

    The old hardcoded C:/D:/E: list missed any library on another drive or a
    non-default Steam install, which showed up as "game was not found" even
    when the game was installed and working.
    """
    roots = []
    for vdf in (r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf",
                r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"):
        try:
            if not os.path.exists(vdf):
                continue
            with open(vdf, encoding='utf-8', errors='replace') as fh:
                for m in re.finditer(r'"path"\s*"([^"]+)"', fh.read()):
                    p = m.group(1).replace('\\\\', '\\')
                    common = os.path.join(p, 'steamapps', 'common')
                    if os.path.isdir(common):
                        roots.append(common)
        except Exception:
            pass
    for fallback in (r"C:\Program Files (x86)\Steam\steamapps\common",
                     r"D:\SteamLibrary\steamapps\common",
                     r"E:\SteamLibrary\steamapps\common"):
        if fallback not in roots:
            roots.append(fallback)
    return roots


def _steam_guesses(game_id=None):
    profile = GAME_PROFILES[game_id or ACTIVE_GAME]
    out = []
    for root in _steam_library_roots():
        for folder in profile['folder_names']:
            base = os.path.join(root, folder)
            out.append(base)
            # Steam installs some of these titles into a duplicated subfolder
            # (…\NASCAR 14\NASCAR 14\data), which the flat probe never saw.
            for nested in profile['folder_names']:
                out.append(os.path.join(base, nested))
    return out


def _profile_root_from(path, game_id=None):
    """Return the install root for `path`, or None.

    Accepts what users actually paste, not just the one canonical form:
      - the install root            (…\\NASCAR 14)
      - the data folder itself      (…\\NASCAR 14\\data)
      - a duplicated nested folder  (…\\NASCAR 14\\NASCAR 14)
    Always returns the ROOT, because registry() appends 'data' downstream.
    """
    if not path:
        return None
    profile = GAME_PROFILES[game_id or ACTIVE_GAME]
    required = profile['required_archives']

    def has_archives(root):
        d = os.path.join(root, 'data')
        return all(os.path.exists(os.path.join(d, f'ARCHIVE{k}.AR')) for k in required)

    path = str(path).rstrip('\\/')
    candidates = [path]
    # user pasted the data folder -> its parent is the root
    if os.path.basename(path).lower() == 'data':
        candidates.append(os.path.dirname(path))
    # user pasted the outer folder of a duplicated install
    for folder in profile['folder_names']:
        candidates.append(os.path.join(path, folder))
    for root in candidates:
        if root and has_archives(root):
            return root
    return None


def _path_has_profile(path, game_id=None):
    return _profile_root_from(path, game_id) is not None

def detect_game(game_id=None):
    gid = game_id or ACTIVE_GAME
    cfg_path = _profile_config_path(gid)
    try:
        configured = json.load(open(cfg_path, encoding='utf-8')).get('game') if os.path.exists(cfg_path) else None
    except Exception:
        configured = None
    root = _profile_root_from(configured, gid)
    if root:
        return root
    for path in _steam_guesses(gid):
        root = _profile_root_from(path, gid)
        if root:
            return root
    return None


# ---------------- shared imported-image preparation ----------------
IMAGE_RESIZE_MODES = ('auto','fit','fill','stretch','nearest')

def prepare_import_image(img, target_size, mode='fit', preserve_alpha=True,
                         background=(0,0,0,0)):
    """Resize any imported image to an exact game texture size.

    fit:     preserve aspect ratio and pad/letterbox
    fill:    preserve aspect ratio and center-crop
    stretch: exact resize (best for fixed UV atlases such as car liveries)
    nearest: fit/pad using nearest-neighbour for pixel art

    RGBA is retained whenever preserve_alpha is true. Callers writing DXT1 may
    convert the result to RGB only after this function returns.
    """
    tw,th = map(int,target_size)
    if tw<=0 or th<=0:
        raise ValueError('invalid target image size')
    mode=(mode or 'fit').lower()
    if mode not in IMAGE_RESIZE_MODES:
        mode='fit'
    # `auto` requires target metadata and is resolved by the caller. Keep this
    # low-level helper deterministic when it is used by older routes.
    if mode=='auto':
        mode='fit'
    source_format=(getattr(img,'format',None) or 'unknown').upper()
    source_mode=str(getattr(img,'mode','unknown'))
    source_alpha=('A' in source_mode) or ('transparency' in getattr(img,'info',{}))
    src=img.convert('RGBA' if preserve_alpha else 'RGB')
    sw,sh=src.size
    if sw<=0 or sh<=0:
        raise ValueError('imported image has invalid dimensions')
    if (sw,sh)==(tw,th):
        return src, dict(resized=False, source=[sw,sh], target=[tw,th], mode=mode,
                         source_format=source_format, source_mode=source_mode,
                         source_alpha=bool(source_alpha), preserve_alpha=bool(preserve_alpha))
    lanczos=Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS
    nearest=Image.Resampling.NEAREST if hasattr(Image,'Resampling') else Image.NEAREST
    if mode=='stretch':
        out=src.resize((tw,th),lanczos)
    elif mode in ('fit','nearest'):
        filt=nearest if mode=='nearest' else lanczos
        scale=min(tw/sw,th/sh)
        nw=max(1,round(sw*scale)); nh=max(1,round(sh*scale))
        small=src.resize((nw,nh),filt)
        bg=background if preserve_alpha else background[:3]
        out=Image.new('RGBA' if preserve_alpha else 'RGB',(tw,th),bg)
        out.paste(small,((tw-nw)//2,(th-nh)//2),small if preserve_alpha else None)
    else: # fill
        scale=max(tw/sw,th/sh)
        nw=max(1,round(sw*scale)); nh=max(1,round(sh*scale))
        large=src.resize((nw,nh),lanczos)
        left=max(0,(nw-tw)//2); top=max(0,(nh-th)//2)
        out=large.crop((left,top,left+tw,top+th))
    return out, dict(resized=True, source=[sw,sh], target=[tw,th], mode=mode,
                     source_format=source_format, source_mode=source_mode,
                     source_alpha=bool(source_alpha), preserve_alpha=bool(preserve_alpha))

def request_resize_mode(default='fit'):
    """Read resize_mode from multipart form, JSON, or query string."""
    mode=request.form.get('resize_mode') or request.args.get('resize_mode')
    if not mode and request.is_json:
        try: mode=(request.get_json(silent=True) or {}).get('resize_mode')
        except Exception: mode=None
    mode=(mode or default).lower()
    return mode if mode in IMAGE_RESIZE_MODES else default

def registry():
    """All archive/cdfiles pairs present in the game's data folder.
    Keys: '0','2','544386974',... Values: dict(ar,cdf,bak)."""
    g = load_cfg().get('game') or detect_game()
    if not g: return None, {}
    d = os.path.join(g,'data')
    reg={}
    try:
        entries = os.listdir(d)
    except OSError:
        # Configured game folder has no readable data\ subfolder (moved, renamed,
        # wrong folder level, or permissions). Report "no archives" so callers use
        # their normal guidance path instead of surfacing a raw errno.
        return g, {}
    for f in entries:
        m=re.match(r'^cdfiles(\d*)\.dat$', f, re.I)
        if not m: continue
        suf=m.group(1) or '0'
        ar=os.path.join(d, f'ARCHIVE{suf}.AR')
        if os.path.exists(ar):
            reg[suf]=dict(ar=ar, cdf=os.path.join(d,f), bak=backup_path(ar))
    return g, reg

def need(reg, key):
    if key not in reg: raise ValueError(f'ARCHIVE{key} not found in game data folder')
    return reg[key]

# ---------------- cdfiles ----------------
def parse_cdfiles(path):
    return _read_cdf_tuples(path)

def find_entry(reg, arcid, name, pristine=False):
    cdf=need(reg,arcid)['cdf']
    if pristine and os.path.exists(backup_path(cdf)):
        cdf=backup_path(cdf)
    for o,s,n in parse_cdfiles(cdf):
        if n==name: return o,s
    raise ValueError(f'{name} not found in ARCHIVE{arcid}')

# ---------------- grid slots (multi-archive) ----------------
def slot_from_name(n, o, s, arcid, hd_map):
    classified = classify_livery_slot(ACTIVE_GAME, n)
    if classified is None:
        return None
    num, label, kind = classified.number, classified.label, classified.kind
    hn = 'HD' + n
    hd = hd_map.get(hn)
    return dict(name=n, hd=hn if hd else None, label=label,
        number=num, kind=kind, arc=arcid, sd_arc=arcid,
        sd_off=o, sd_size=s,
        hd_arc=hd[0] if hd else None,
        hd_off=hd[1] if hd else 0,
        hd_size=hd[2] if hd else 0,
        fei=None)


_GRID_SLOTS_CACHE={'sig':None,'rows':None}


def grid_slots():
    g, reg = registry()
    if not g:
        return []
    sig=[str(ACTIVE_GAME)]
    for arcid in sorted(reg):
        path=(reg.get(arcid) or {}).get('cdf')
        try:
            st=os.stat(path);sig.append((str(arcid),st.st_size,st.st_mtime_ns))
        except Exception:sig.append((str(arcid),0,0))
    sig=tuple(sig)
    if _GRID_SLOTS_CACHE.get('sig')==sig and _GRID_SLOTS_CACHE.get('rows') is not None:
        return [dict(x) for x in _GRID_SLOTS_CACHE['rows']]
    profile = active_game_profile()
    primary = profile['paint_primary_archive']
    order = sorted(reg.keys(), key=lambda k: (k != primary, k))
    entries_by_arc = {}
    for arcid in order:
        try:
            entries_by_arc[arcid] = parse_cdfiles(reg[arcid]['cdf'])
        except Exception:
            entries_by_arc[arcid] = []
    hd_map = {}
    for arcid in order:
        for off,size,name in entries_by_arc[arcid]:
            if name.startswith('HDLIVERY_'):
                hd_map[name] = (arcid, off, size)
    slots = {}
    for arcid in order:
        last_fei = None
        for off,size,name in entries_by_arc[arcid]:
            if name.startswith('FEI_LIV_'):
                last_fei = (name,off,size)
                continue
            if not name.startswith('LIVERY_'):
                continue
            slot = slot_from_name(name, off, size, arcid, hd_map)
            if not slot:
                continue
            if slot['kind'] == 'dlc' and last_fei:
                slot['fei'] = dict(name=last_fei[0], off=last_fei[1], size=last_fei[2])
            slots[name] = slot
    rows=sorted(slots.values(), key=lambda x: (x['sd_arc'] != primary, x['kind'], x['name']))
    _GRID_SLOTS_CACHE.update(sig=sig,rows=[dict(x) for x in rows])
    return rows

def driver_display_name(label):
    words=[w for w in str(label or '').split() if w.lower() not in
           ('primary','secondary','tertiary','beer') and not w.isdigit()]
    raw=' '.join(words)
    norm=re.sub(r'[^a-z0-9]+','',raw.casefold())
    mapped=_driver_display_aliases().get(norm) if '_driver_display_aliases' in globals() else None
    if mapped:return mapped
    fixed=[]
    for word in raw.split():
        u=word.upper().rstrip('.')
        if u in ('AJ','JJ'):fixed.append(u)
        elif u in ('JR','JNR'):fixed.append('Jr.')
        elif u=='MCDOWELL':fixed.append('McDowell')
        elif u=='MCMURRAY':fixed.append('McMurray')
        else:fixed.append(word)
    return ' '.join(fixed)

# ---------------- DXT1 (unchanged core from v0.3) ----------------
_565=C._565; _rgb=C._rgb
def _pack_blocks(c0,c1,idx):
    sw=c0<c1
    c0f=np.where(sw,c1,c0); c1f=np.where(sw,c0,c1)
    idxf=np.where(sw[:,None], idx^1, idx)
    idxf=np.where((c0f==c1f)[:,None],0,idxf).astype(np.uint32)
    bits=np.zeros(len(c0f),np.uint32)
    for i in range(16): bits |= idxf[:,i]<<(2*i)
    out=np.zeros((len(c0f),8),np.uint8)
    out[:,0],out[:,1]=c0f&0xFF,c0f>>8
    out[:,2],out[:,3]=c1f&0xFF,c1f>>8
    for i in range(4): out[:,4+i]=(bits>>(8*i))&0xFF
    return out.tobytes()
def _assign(bl,c0,c1):
    p0=_rgb(c0).astype(np.float64); p1=_rgb(c1).astype(np.float64)
    pal=np.stack([p0,p1,(2*p0+p1)/3,(p0+2*p1)/3],1)
    d=((bl[:,None,:,:]-pal[:,:,None,:])**2).sum(-1)
    idx=d.argmin(1)
    err=np.take_along_axis(d,idx[:,None,:],1)[:,0,:].sum(1)
    return idx.astype(np.uint32), err
def dxt1_encode_py(img, iters=3):
    H,W,_=img.shape
    bl=img.reshape(H//4,4,W//4,4,3).transpose(0,2,1,3,4).reshape(-1,16,3).astype(np.float64)
    mean=bl.mean(1,keepdims=True); cen=bl-mean
    cov=np.einsum('nij,nik->njk',cen,cen)
    v=np.ones((len(bl),3))
    for _ in range(4):
        v=np.einsum('njk,nk->nj',cov,v)
        n=np.linalg.norm(v,axis=1,keepdims=True); n[n==0]=1; v=v/n
    t=np.einsum('nij,nj->ni',cen,v)
    e0=np.clip(mean[:,0]+v*t.max(1)[:,None],0,255)
    e1=np.clip(mean[:,0]+v*t.min(1)[:,None],0,255)
    c0=_565(*(e0.T.astype(np.int32))); c1=_565(*(e1.T.astype(np.int32)))
    idx,err=_assign(bl,c0,c1)
    Wt=np.array([1.0,0.0,2/3,1/3])
    for _ in range(iters):
        w=Wt[idx]
        sw2=(w*w).sum(1); swo=(w*(1-w)).sum(1); so2=((1-w)**2).sum(1)
        det=sw2*so2-swo*swo; bad=np.abs(det)<1e-9; det[bad]=1
        bw=np.einsum('ni,nij->nj',w,bl); bo=np.einsum('ni,nij->nj',(1-w),bl)
        n0=np.clip(( so2[:,None]*bw-swo[:,None]*bo)/det[:,None],0,255)
        n1=np.clip((-swo[:,None]*bw+sw2[:,None]*bo)/det[:,None],0,255)
        nc0=_565(*(n0.T.astype(np.int32))); nc1=_565(*(n1.T.astype(np.int32)))
        nidx,nerr=_assign(bl,nc0,nc1)
        better=(nerr<err)&(~bad)
        c0=np.where(better,nc0,c0); c1=np.where(better,nc1,c1)
        idx=np.where(better[:,None],nidx,idx); err=np.where(better,nerr,err)
    return _pack_blocks(c0,c1,idx)
def dxt1_decode(payload,W,H):
    N=(W//4)*(H//4)
    need=N*8
    if len(payload) < need:
        raise ValueError(f'short DXT1 payload: read {len(payload)} bytes, expected {need}')
    a=np.frombuffer(payload[:need],np.uint8).reshape(N,8)
    c0=a[:,0].astype(np.uint16)|(a[:,1].astype(np.uint16)<<8)
    c1=a[:,2].astype(np.uint16)|(a[:,3].astype(np.uint16)<<8)
    bits=sum(a[:,4+i].astype(np.uint32)<<(8*i) for i in range(4))
    p0=_rgb(c0);p1=_rgb(c1);four=(c0>c1)[:,None]
    p2=np.where(four,(2*p0+p1)//3,(p0+p1)//2); p3=np.where(four,(p0+2*p1)//3,0)
    pal=np.stack([p0,p1,p2,p3],1).astype(np.uint8)
    idx=np.stack([(bits>>(2*i))&3 for i in range(16)],1)
    px=np.take_along_axis(pal,idx[:,:,None].astype(np.int64),1)
    return px.reshape(H//4,W//4,4,4,3).transpose(0,2,1,3,4).reshape(H,W,3)

def texconv_path():
    p=os.path.join(APP_DIR,'texconv.exe')
    return p if os.path.exists(p) and os.name=='nt' else None
def _texconv(img_pil, fmt):
    tx=texconv_path()
    if not tx: return None
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'l.png'); img_pil.save(src)
        r=subprocess.run([tx,'-y','-ft','dds','-dx9','-f',fmt,'-m','1','-o',td,src],
                         capture_output=True)
        dds=os.path.join(td,'l.dds')
        if r.returncode==0 and os.path.exists(dds):
            return open(dds,'rb').read()[128:]
    return None
def encode_image(img_pil):
    w,h=img_pil.size
    if w>=4 and h>=4:
        enc=_texconv(img_pil.convert('RGB'),'DXT1')
        if enc: return enc
    arr=np.asarray(img_pil.convert('RGB'))
    ph,pw=max(4,((h+3)//4)*4),max(4,((w+3)//4)*4)
    if (ph,pw)!=(h,w):
        pad=np.zeros((ph,pw,3),np.uint8); pad[:h,:w]=arr[:h,:w]; arr=pad
    return dxt1_encode_py(arr)
def encode_dxt5(img_pil):
    enc=_texconv(img_pil.convert('RGBA'),'DXT5')
    if enc: return enc
    return C.dxt5_encode(np.asarray(img_pil.convert('RGBA')))

def encode_any(img_pil, fmt='DXT5'):
    enc=_texconv(img_pil.convert('RGBA'),fmt)
    if enc: return enc
    if fmt=='DXT5': return C.dxt5_encode(np.asarray(img_pil.convert('RGBA')))
    return dxt1_encode_py(np.asarray(img_pil.convert('RGB')))

# ---------------- livery mip chain build: STOCK-MIP BAKE ----------------
def level_dims(w,h,L): return max(1,w>>L), max(1,h>>L)
def level_bytes(lw,lh): return max(1,(lw+3)//4)*max(1,(lh+3)//4)*8

# RC4 correction: NASCAR 15's native livery wrappers require the stock-proven
# horizontal compensation used by the original working v0.9.14 writer. These
# values are part of the native mip layout, not a cosmetic image shift. Removing
# them in RC3 made the full paint atlas visibly slide across the car. Full-image
# imports still replace every intended block, so the old donor/fade regression
# remains fixed.
STOCK_MIP_ROLL = {
    0:0, 1:-10, 2:-15, 3:-18, 4:-19, 5:-20,
    6:-20, 7:-20, 8:-20, 9:-20, 10:-20, 11:-20, 12:-20,
}

def level_offset(w,h,L):
    return sum(level_bytes(*level_dims(w,h,i)) for i in range(L))

def raw_chain_total(w,h,mips):
    return sum(level_bytes(*level_dims(w,h,L)) for L in range(mips))

def _roll_for_level(L):
    return STOCK_MIP_ROLL.get(L, STOCK_MIP_ROLL[max(STOCK_MIP_ROLL)])

def _roll_np_img(img, L):
    roll = _roll_for_level(L)
    if roll:
        return Image.fromarray(np.roll(np.asarray(img), roll, axis=1))
    return img

def _decode_stock_mip(stock_payload, w, h, L):
    """Decode the real stock mip L from backup bytes.

    This is the key difference from the older hard-bake builds:
    we do NOT resize stock mip0 to make distant base art. We use the actual stock
    mip that Eutechnyx shipped, then paint our edits onto it.
    """
    if stock_payload is None:
        return None
    lw,lh = level_dims(w,h,L)
    off = level_offset(w,h,L)
    need = level_bytes(lw,lh)
    if off + need > len(stock_payload):
        return None
    try:
        return Image.fromarray(dxt1_decode(stock_payload[off:off+need],lw,lh)).convert('RGB')
    except Exception:
        return None

def _fallback_full_composite_mip(composite, w, h, L):
    base = composite.convert('RGB').resize((w,h)) if composite.size!=(w,h) else composite.convert('RGB')
    lw,lh = level_dims(w,h,L)
    cur = base if L == 0 else base.resize((lw,lh), Image.BOX)
    return _roll_np_img(cur,L)

def _downsample_edit_layer(layer_rgba, w, h, L):
    """Downsample the edit layer as premultiplied RGBA, then roll it to stock-mip
    storage coordinates.

    A small alpha boost/dilation is applied only to lower mips so decals survive
    shallow-angle sampling, but the base itself remains the true stock mip.
    """
    if layer_rgba is None:
        return None, None
    lw,lh = level_dims(w,h,L)
    lay = layer_rgba.convert('RGBA').resize((w,h)) if layer_rgba.size!=(w,h) else layer_rgba.convert('RGBA')
    arr = np.asarray(lay).astype(np.float32)
    a = arr[:,:,3] / 255.0
    rgb = arr[:,:,:3]

    # premultiply before BOX downsample so transparent RGB does not bleed.
    pm = rgb * a[:,:,None]
    pm_img = Image.fromarray(np.clip(pm,0,255).astype(np.uint8),'RGB')
    a_img  = Image.fromarray(np.clip(a*255,0,255).astype(np.uint8),'L')

    pm_small = np.asarray(pm_img.resize((lw,lh), Image.BOX)).astype(np.float32)
    a_small_img = a_img.resize((lw,lh), Image.BOX)

    # Preserve fine edits in mips the game uses for AI/angle views.
    if L >= 2:
        k = 3 if L <= 4 else 5
        a_small_img = a_small_img.filter(ImageFilter.MaxFilter(k))

    a_small = np.asarray(a_small_img).astype(np.float32)/255.0
    boost = min(5.0, 1.0 + 0.60*L)
    a_small = np.clip(a_small*boost, 0.0, 1.0)
    a_small[a_small < 0.015] = 0.0

    # Use original non-dilated alpha for RGB unpremultiply denominator.
    a_den = np.asarray(a_img.resize((lw,lh), Image.BOX)).astype(np.float32)[:,:,None]/255.0
    a_den = np.maximum(a_den, 1.0/255.0)
    rgb_small = np.clip(pm_small / a_den, 0, 255)

    rgb_im = Image.fromarray(rgb_small.astype(np.uint8),'RGB')
    a_im = Image.fromarray(np.clip(a_small*255,0,255).astype(np.uint8),'L')

    # Stock mip bytes are already in rolled storage coordinates, so roll the edit
    # layer to match before compositing onto stock_mip.
    rgb_im = _roll_np_img(rgb_im,L)
    a_im = _roll_np_img(a_im,L)
    return rgb_im, a_im

def _composite_edit_onto_stock_mip(stock_mip, edit_rgb, edit_alpha):
    if stock_mip is None or edit_rgb is None or edit_alpha is None:
        return None
    base = stock_mip.convert('RGB')
    er = np.asarray(edit_rgb.convert('RGB')).astype(np.float32)
    ea = np.asarray(edit_alpha.convert('L')).astype(np.float32)/255.0
    sb = np.asarray(base).astype(np.float32)
    out = sb*(1.0-ea[:,:,None]) + er*ea[:,:,None]
    return Image.fromarray(np.clip(out,0,255).astype(np.uint8),'RGB')

# Compatibility wrappers for old callers/debug scripts.
def chain_levels(img, w, h, mips, shift=0, black_from=None):
    return [_fallback_full_composite_mip(img,w,h,L) for L in range(mips)]

def mask_levels(alpha_img, w, h, mips, shift=0):
    out=[]
    a = alpha_img.resize((w,h)) if alpha_img.size!=(w,h) else alpha_img
    for L in range(mips):
        lw,lh=level_dims(w,h,L)
        small=np.asarray(a if L==0 else a.resize((lw,lh),Image.BOX)).astype(np.float32)/255.0
        roll=_roll_for_level(L)
        if roll: small=np.roll(small,roll,axis=1)
        out.append(small)
    return out

def build_payload(composite, w, h, mips, stock_payload=None, layer_alpha=None,
                  shift=0, black_from=None, layer_rgba=None):
    """Stock-mip bake.

    The checker probe proved the game uses our SD/HD livery path at distance.
    The base stock scheme looks stable because its shipped lower mips are good.
    Therefore this build preserves those shipped stock mips as the base for every
    level, and paints the edit layer directly onto each stock mip.

    This should avoid the "few big color blocks" look caused by generating all
    distant base art from mip0 or from diagnostic checker patterns.
    """
    total = raw_chain_total(w,h,mips)
    payload=bytearray()

    for L in range(mips):
        lw,lh = level_dims(w,h,L)
        need_b = level_bytes(lw,lh)

        stock_mip = _decode_stock_mip(stock_payload,w,h,L)
        edit_rgb, edit_alpha = _downsample_edit_layer(layer_rgba,w,h,L) if layer_rgba is not None else (None,None)
        im = _composite_edit_onto_stock_mip(stock_mip,edit_rgb,edit_alpha)

        if im is None:
            # No usable layer/stock bytes: fall back to the current full-composite
            # stock-roll behavior.
            im = _fallback_full_composite_mip(composite,w,h,L)

        enc = encode_image(im)[:need_b]
        enc = enc + b'\0'*(need_b-len(enc))
        payload += enc

    if len(payload) < total:
        payload += b'\0'*(total-len(payload))
    return bytes(payload)


# === native SD mip L0-L10 writer v0.9.14 ===
_NATIVE_SD_WRAP_X_BLOCKS = -5
_NATIVE_SD_WRAP_Y_BLOCKS = -1
_NATIVE_SD_PHYS_BLOCKS = 32
_NATIVE_HD_WRAP_X_BLOCKS = -5
_NATIVE_HD_WRAP_Y_BLOCKS = -1
_NATIVE_HD_PHYS_BLOCKS = 32


def _native_sd_patch_wrapper(pristine_wrapper, composite, layer_alpha=None):
    return _shared_livery_wrapper_editor().patch_sd(pristine_wrapper, composite, layer_alpha)


def _native_hd_patch_wrapper_impl(pristine_wrapper, composite, atlas_x_roll):
    return _shared_livery_wrapper_editor().patch_hd(
        pristine_wrapper, composite, stock_atlas_alignment=bool(atlas_x_roll),
    )


def _native_hd_patch_wrapper(pristine_wrapper, composite):
    return _shared_livery_wrapper_editor().patch_hd(
        pristine_wrapper, composite, stock_atlas_alignment=True,
    )


def _native_hd_patch_wrapper_public_v1(pristine_wrapper, composite):
    return _shared_livery_wrapper_editor().patch_hd(pristine_wrapper, composite)

def ensure_backup(live,bak):
    # Create a backup only if one doesn't already exist, and only from a
    # plausibly-intact live file. Never overwrite an existing backup (the first
    # one, made before any edit, is the true pristine copy).
    if os.path.exists(bak):
        return
    if not os.path.exists(live) or os.path.getsize(live) < 64:
        raise ValueError('refusing to back up an empty/missing archive')
    # write to a temp file then atomically rename, so an interrupted copy can't
    # leave a truncated backup that restore would trust.
    tmp = bak + '.tmp'
    shutil.copyfile(live, tmp)
    if os.path.getsize(tmp) != os.path.getsize(live):
        os.remove(tmp); raise ValueError('backup copy incomplete; aborted')
    # Force the copy to disk before it becomes the pristine backup. Without
    # this the rename can commit while the bytes are still in the page cache,
    # so a crash leaves a backup that Restore trusts but cannot use.
    with open(tmp, 'rb+') as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, bak)


def _clear_ui_thumb_cache(*keys):
    """Invalidate unified Images & UI thumbnails after any archive image write.

    Legacy Menus/Paint Previews routes and first-time backup creation can change
    both the live thumbnail and its Stock/Modified comparison. With no keys, the
    complete cache is cleared; otherwise only the named (archive,container,entry)
    tuples are removed.
    """
    cache=globals().get('_UI_THUMB_CACHE')
    if cache is not None:
        if not keys:
            cache.clear()
        else:
            for key in keys:
                cache.pop(tuple(map(str,key)),None)
    for cache_name in ('_LIVE_PAINT_THUMB_CACHE','_PAINT_ATLAS_PREVIEW_CACHE','_STOCK_THUMB_SUPPORT_CACHE'):
        other=globals().get(cache_name)
        if other is not None: other.clear()
    idx=globals().get('_LIVE_LIVERY_INDEX_CACHE')
    if isinstance(idx,dict): idx.update(sig=None,script_to_uid={},uid_to_driver={})
    grid=globals().get('_GRID_SLOTS_CACHE')
    if isinstance(grid,dict): grid.update(sig=None,rows=None)

# ---------------- install ----------------
def write_career_thumb(reg, slot, thumb_img):
    """Compatibility adapter for the shared decoded-texture writer."""
    match = re.match(r'^LIVERY_CAREER_(\w+?)_(\d+)\.ARC$', slot['name'])
    if not match:
        raise ValueError('not a career slot')
    target = f"CAREER_{match.group(1).upper()}_{match.group(2)}"
    result = _shared_texture_editor().replace_image(
        '0', 'BASESCHEMETHUMBNAILS.ARC', target, thumb_img,
        resize_mode='fit', experimental=True,
    )
    _clear_ui_thumb_cache()
    return f"thumb {target} ({'verified' if result.get('verified') else 'unverified'})"

def slot_thumb_source(slot):
    """User-uploaded thumb if present, else auto-generated from the scheme."""
    tp=os.path.join(SCHEMES, slot['name']+'.thumb.png')
    if os.path.exists(tp): return Image.open(tp)
    sp=os.path.join(SCHEMES, slot['name']+'.png')
    if os.path.exists(sp): return C.make_thumb(Image.open(sp))
    return None

def install_slot(reg, slot, png_path, layer_path=None):
    """Compatibility adapter for the shared transactional stock-paint writer."""
    report = _shared_stock_paint_editor().install(
        slot['name'], png_path,
        hd_name=(slot.get('hd') if slot.get('hd') else False),
        layer_path=layer_path,
    )
    results = [
        f"SD native paint installed ({report['sd_changed_bytes']} changed bytes)"
    ]
    if report.get('hd'):
        results.append(f"HD native paint installed ({report['hd_changed_bytes']} changed bytes)")
    try:
        thumbnail = slot_thumb_source(slot)
        if thumbnail is not None and slot.get('kind') == 'career':
            results.append(write_career_thumb(reg, slot, thumbnail))
        elif thumbnail is not None and slot.get('fei'):
            results.append('DLC FEI preview preserved unchanged (safety lock)')
    except Exception as ex:
        results.append(f'thumb skipped: {ex}')
    _clear_ui_thumb_cache()
    return results

# ---------------- names / roster / handles ----------------
def text0000(reg, pristine=False):
    a=need(reg,'0')
    use_bak = pristine and os.path.exists(a['bak'])
    off,s=find_entry(reg,'0','TEXT0000.LDA', pristine=use_bak)
    src=a['bak'] if use_bak else a['ar']
    with open(src,'rb') as fh:
        fh.seek(off); return fh.read(s)

def find_exact_string(blob, candidate):
    """Find one complete stock string without surname or substring guessing.

    Some TEXT*.LDA files contain valid NUL-terminated strings that are not
    returned by the structured LDA entry iterator.  Darrell Wallace Jr. is one
    of those on the clean NASCAR 15 build.  Search both views, but still require
    an exact case-insensitive full-string match before enabling Rename.
    """
    wanted=str(candidate or '').strip().casefold()
    if not wanted:return None
    values=[];seen=set()
    try:
        for e in C.lda_entries(blob):
            text=e['raw'].decode('latin1','replace')
            key=text.casefold()
            if key not in seen:seen.add(key);values.append(text)
    except Exception:
        pass
    # Always include the raw NUL-delimited view.  Do not use substring or
    # surname fallback: exact matching is what prevents Mike/Darrell Wallace
    # and similar collisions.
    for raw in bytes(blob).split(b'\0'):
        if not raw:continue
        text=raw.decode('latin1','replace')
        key=text.casefold()
        if key not in seen:seen.add(key);values.append(text)
    for text in values:
        if text.strip().casefold()==wanted:return text
    return None


def _find_exact_stock_text(reg,candidates):
    """Find an exact name in every pristine TEXT*.LDA table.

    Driver display names are not all guaranteed to live in TEXT0000.LDA.  The
    old roster looked only there, which left Darrell Wallace Jr. disabled even
    though his exact name exists elsewhere in the clean language tables.
    """
    a=need(reg,'0')
    cdf=a['cdf']+'.gridapp.bak' if os.path.exists(a['cdf']+'.gridapp.bak') else a['cdf']
    source=a['bak'] if os.path.exists(a['bak']) else a['ar']
    wanted=[str(x or '').strip() for x in candidates if str(x or '').strip()]
    if not wanted:return None
    try:regions=[(o,sz,n) for o,sz,n in parse_cdfiles(cdf) if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')]
    except Exception:regions=[]
    with open(source,'rb') as fh:
        for off,sz,_name in regions:
            fh.seek(off);blob=fh.read(sz)
            for candidate in wanted:
                hit=find_exact_string(blob,candidate)
                if hit:return hit
    return None


def _game_data_path(filename):
    sub=str(active_game_profile().get('data_subdir') or '')
    return os.path.join(DATA,sub,filename) if sub else os.path.join(DATA,filename)


def load_driver_links():
    rows=json.load(open(_game_data_path('drivers.json'),encoding='utf-8'))
    if not isinstance(rows,list):raise ValueError('drivers.json must contain a list')
    return rows


_DRIVER_DISPLAY_ALIAS_CACHE={}


def _driver_asset_words(slot_name):
    parts=[p for p in livery_asset_words(ACTIVE_GAME,slot_name).split() if p]
    tails={'PRIMARY','SECONDARY','TERTIARY','ALT','ALTERNATE','BEER','THROWBACK','TEST','DEFAULT','SPECIAL','NIGHT','DAY'}
    while parts and parts[-1].upper() in tails:parts.pop()
    return ' '.join(parts)


def _driver_display_aliases():
    game_key=str(ACTIVE_GAME)
    if game_key in _DRIVER_DISPLAY_ALIAS_CACHE:return _DRIVER_DISPLAY_ALIAS_CACHE[game_key]
    out={}
    try:
        for link in load_driver_links():
            display=str(link.get('display_name') or '').strip()
            if not display:continue
            aliases=[display,_driver_asset_words(link.get('slot'))]+list(link.get('name_candidates') or [])
            for alias in aliases:
                norm=re.sub(r'[^a-z0-9]+','',str(alias).casefold())
                if norm:out[norm]=display
    except Exception:pass
    _DRIVER_DISPLAY_ALIAS_CACHE[game_key]=out
    return out


def _driver_display_from_link(link):
    display=str(link.get('display_name') or '').strip()
    if display:return display
    raw=_driver_asset_words(link.get('slot'));words=[]
    for word in raw.split():
        u=word.upper()
        if u in ('AJ','JJ'):words.append(u)
        elif u in ('JR','JNR'):words.append('Jr.')
        elif u=='MCDOWELL':words.append('McDowell')
        elif u=='MCMURRAY':words.append('McMurray')
        else:words.append(u.lower().capitalize())
    return ' '.join(words)


def _driver_name_candidates(link):
    values=[];seen=set()
    seeds=[link.get('display_name'),_driver_display_from_link(link),_driver_asset_words(link.get('slot'))]
    seeds.extend(link.get('name_candidates') or [])
    for value in seeds:
        text=str(value or '').strip();key=text.casefold()
        if text and key not in seen:seen.add(key);values.append(text)
    return values


def _live_text_for_stock_text(reg, stock_text):
    """Resolve the current live string at the same LDA index as a stock name.

    This makes a second rename work even when config/app state was deleted. The
    pristine table identifies the driver; the live table supplies the current
    text. No surname or substring guessing is used.
    """
    wanted = str(stock_text or '').strip().casefold()
    if not wanted:
        return None
    a = need(reg, '0')
    bar = a.get('bak') if os.path.exists(a.get('bak', '')) else None
    bcdf = backup_path(a['cdf']) if os.path.exists(backup_path(a['cdf'])) else None
    if not bar or not bcdf:
        return stock_text
    try:
        stock_regions = {n: (o, z) for o, z, n in parse_cdfiles(bcdf)
                         if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')}
        live_regions = {n: (o, z) for o, z, n in parse_cdfiles(a['cdf'])
                        if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')}
        with open(bar, 'rb') as sf, open(a['ar'], 'rb') as lf:
            for name, (soff, ssize) in stock_regions.items():
                if name not in live_regions:
                    continue
                loff, lsize = live_regions[name]
                sf.seek(soff); stock_blob = sf.read(ssize)
                lf.seek(loff); live_blob = lf.read(lsize)
                try:
                    stock_entries = C.lda_entries(stock_blob)
                    live_entries = C.lda_entries(live_blob)
                except Exception:
                    continue
                live_by_index = {int(e['index']): e for e in live_entries}
                for entry in stock_entries:
                    text = entry['raw'].decode('latin1', 'replace')
                    if text.strip().casefold() != wanted:
                        continue
                    peer = live_by_index.get(int(entry['index']))
                    if peer is None:
                        return stock_text
                    return peer['raw'].decode('latin1', 'replace')
    except Exception:
        pass
    return stock_text


def roster(reg):
    """Build the 46-driver 2015 roster from the clean DRIVERCONFIG map.

    The language table is used only to locate an exact rename target. A missing
    alias disables Rename instead of falling back to another driver with the
    same surname.
    """
    cfg=load_cfg();led=cfg.get('renames',{});hled=cfg.get('handles',{})
    blob=text0000(reg,pristine=True)
    goff,gsz=find_entry(reg,'0','DB_GAME_LOCAL_SCRIPT.PYC')
    with open(need(reg,'0')['ar'],'rb') as fh:fh.seek(goff);gloc=fh.read(gsz)
    drivers=[];seen_bases=set()
    for link in load_driver_links():
        base=str(link.get('base') or '').upper()
        if not base or base in seen_bases:continue
        seen_bases.add(base);display=_driver_display_from_link(link)
        candidates=_driver_name_candidates(link)
        stock_text=None
        for candidate in candidates:
            stock_text=find_exact_string(blob,candidate)
            if stock_text:break
        if not stock_text:
            stock_text=_find_exact_stock_text(reg,candidates)
        live_text=_live_text_for_stock_text(reg,stock_text) if stock_text else None
        renamed=led.get(stock_text,led.get(display)) if stock_text else led.get(display)
        current=str(live_text or renamed or display);patch_current=str(live_text or renamed or stock_text or '')
        h_orig=str(link.get('handle') or '');h_storage=str(hled.get(h_orig,h_orig));h_display=h_storage.rstrip('_ ')
        h_ok=bool(h_storage) and h_storage.encode('latin1') in gloc
        drivers.append(dict(base=base,number=str(link.get('number') or ''),original=display,current=current,
            stock_text=stock_text,patch_current=patch_current,rename_available=bool(stock_text),
            rename_note='' if stock_text else 'Exact stock name text was not found; rename is disabled to prevent changing another driver.',
            handle=h_orig,handle_current=h_display,handle_storage_current=h_storage,
            handle_found=h_ok,driver_uid=link.get('driver_uid'),
            config_uid=link.get('config_uid'),team_uid=link.get('team_uid'),profile_id=link.get('profile_id'),slot=link.get('slot')))
    drivers.sort(key=lambda x:(int(re.match(r'\d+',str(x.get('number') or '9999')).group(0)) if re.match(r'\d+',str(x.get('number') or '')) else 9999, str(x.get('number') or ''), x['current'].casefold()))
    teams=[]
    if not _limited_editor_profile():
        try:
            catalog=_team_friendly_catalog()
            for team in catalog.get('teams',[]):
                if str(team.get('category')) not in ('active','spare'):continue
                original=str(team.get('original_label') or team.get('label') or '').strip();current=str(team.get('label') or original).strip()
                if original and not any(x['original']==original for x in teams):teams.append(dict(original=original,current=current,uid=int(team.get('uid'))))
            teams.sort(key=lambda x:x['current'].casefold())
        except Exception:teams=[]
    if not teams:
        team_names=TEAMS_2014 if _limited_editor_profile() else TEAMS_2015
        for team_name in team_names:
            orig=find_exact_string(blob,team_name)
            if orig:teams.append(dict(original=orig,current=led.get(orig,orig)))
    return drivers,teams

def _apply_display_name(reg, old, new, experimental=True):
    """Route verified driver names to the shared editor; retain team-text compatibility."""
    game = detect_game()
    if game:
        editor = DriverNameEditor(_shared_installation(), DATA)
        wanted = str(old).strip().casefold()
        matches = [
            row for row in editor.drivers()
            if wanted in (row['current'].strip().casefold(), row['original'].strip().casefold())
        ]
        if len(matches) == 1 and matches[0]['available']:
            result = editor.rename(matches[0]['driver_uid'], new)
            return result['tables']
    return TextTableEditor(_shared_installation()).replace_exact(old, new)

# ==================== v0.9.25 UI TEXT EDITOR ====================
# NASCAR 15 stores user-facing interface strings in indexed TEXT*.LDA tables.
# Unlike the old roster rename path, this editor addresses one exact table
# index. Duplicate text remains independent, format tokens are protected, and
# longer strings use the proven append + cdfiles repoint transaction.
_UI_TEXT_CACHE={'signature':None,'rows':None,'files':None,'errors':None}
_UI_TEXT_FILE_CACHE={}
_UI_TEXT_CATEGORIES=list(TEXT_CATEGORIES)


def _shared_text_editor():
    return TextTableEditor(_shared_installation())


def _shared_installation():
    """One selected-installation adapter for every legacy Flask workflow."""
    game=detect_game()
    if not game:raise RuntimeError('game folder not found')
    return GameInstallation(ACTIVE_GAME,game)


def _shared_resource_editor():
    return ResourceEditor(_shared_installation())


def _shared_append_transaction():
    return AppendRepointTransaction(_shared_installation())


def _shared_backup_manager():
    return BackupManager(_shared_installation())


def _shared_audio_editor():
    return AudioBankEditor(_shared_installation())


def _shared_track_inventory():
    return TrackInventory(_shared_installation())


def _shared_schedule_editor():
    return ScheduleEditor(_shared_installation(), CONFIG)


def _shared_season_pack_editor():
    return SeasonPackEditor(_shared_installation(), USER_DIR)


def _shared_team_editor():
    return TeamEditor(_shared_installation())


def _shared_team_presentation_recovery():
    return TeamPresentationRecovery(_shared_installation(), USER_DIR)


def _shared_team_presentation_editor():
    return TeamPresentationEditor(_shared_installation(), USER_DIR)


def _shared_managed_paint_editor():
    return ManagedPaintEditor(_shared_installation(), EXTRA_SCHEME_STATE)


def _shared_full_repair_editor():
    return FullRepairEditor(_shared_installation(), USER_DIR, app_version=APP_VERSION)


def _shared_paint_transaction():
    return ManagedPaintTransaction(
        _shared_installation(), EXTRA_SCHEME_STATE, EXTRA_SCHEME_IMAGES,
    )


def _shared_paint_checkpoint():
    return ManagedPaintCheckpoint(_shared_paint_transaction(), EXTRA_SCHEME_ROLLBACK_DIR)


def _shared_team_asset_transaction():
    return TeamAssetTransaction(
        _shared_installation(), TEAM_MANAGER_STATE, EXTRA_SCHEME_STATE,
    )


def _shared_team_asset_checkpoint():
    return TeamAssetCheckpoint(_shared_team_asset_transaction(), TEAM_ASSET_ROLLBACK_DIR)


def _shared_user_library():
    return UserLibrary(CONFIG)


def _shared_scr_editor():
    return ScrEditor(_shared_installation())


def _shared_pyc_editor():
    return PycRecordEditor(_shared_installation())


def _shared_texture_editor():
    return TextureBankEditor(_shared_installation())


def _shared_livery_wrapper_editor():
    return NativeLiveryWrapperEditor(_shared_installation())


def _shared_stock_paint_editor():
    return StockPaintEditor(_shared_installation())


def _shared_driver_handle_editor():
    return DriverHandleEditor(_shared_installation(), CONFIG, DATA)


def _ui_text_signature(reg):
    if '0' not in reg:return None
    v=reg['0'];paths=[v['ar'],v['cdf'],v['bak'],backup_path(v['cdf'])]
    out=[]
    for path in paths:
        try:
            st=os.stat(path);out.append((path,st.st_size,st.st_mtime_ns))
        except OSError:out.append((path,None,None))
    return tuple(out)


def _ui_text_visible(text):
    return str(text).replace('\r','\\r').replace('\n','\\n').replace('\t','\\t')


def _ui_text_quick_status():
    """Return TEXT table metadata without decoding every LDA string.

    The old status endpoint synchronously read every live table and every stock
    copy before the tab could render. On a full install that could take minutes.
    This quick path only parses cdfiles0; individual tables are decoded lazily.
    """
    files=[dict(name=row['name'],size=row['size'],offset=row['offset'],has_stock=row['has_stock'],
                count=None,user_facing=None,modified=None)
           for row in _shared_text_editor().files()]
    return sorted(files,key=lambda x:x['name'].casefold())


def _ui_text_scan_file(file_name,force=False,include_stock=True):
    """Decode one TEXT table and cache it independently."""
    g,reg=registry()
    sig=_ui_text_signature(reg)
    key=(sig,str(file_name).casefold(),bool(include_stock))
    if not force and key in _UI_TEXT_FILE_CACHE:
        return _UI_TEXT_FILE_CACHE[key]
    rows=_shared_text_editor().entries(file_name);errors=[]
    result=(rows,dict(name=(rows[0]['file'] if rows else file_name),count=len(rows),
                      user_facing=sum(1 for x in rows if x['user_facing']),
                      modified=sum(1 for x in rows if x['modified']),
                      size=(rows[0]['file_size'] if rows else 0),
                      has_stock=any(x['stock'] is not None for x in rows)),errors)
    _UI_TEXT_FILE_CACHE[key]=result
    return result


def _ui_text_scan(force=False):
    g,reg=registry()
    if not g or '0' not in reg:raise ValueError('ARCHIVE0 is not available; configure the NASCAR 15 folder first')
    sig=_ui_text_signature(reg)
    if not force and _UI_TEXT_CACHE.get('rows') is not None and _UI_TEXT_CACHE.get('signature')==sig:
        return _UI_TEXT_CACHE['rows'],_UI_TEXT_CACHE['files'],_UI_TEXT_CACHE['errors']
    editor=_shared_text_editor();rows=[];files=[];errors=[];raw_counts=collections.Counter()
    for file_meta in editor.files():
        name=file_meta['name']
        try:
            values=editor.entries(name)
            rows.extend(values);raw_counts.update(item['current'] for item in values)
            files.append(dict(name=name,count=len(values),user_facing=sum(1 for x in values if x['user_facing']),
                              modified=sum(1 for x in values if x['modified']),size=file_meta['size'],
                              has_stock=bool(file_meta['has_stock'])))
        except Exception as ex:errors.append(f'{name}: {ex}')
    for item in rows:
        item['reference_count']=raw_counts[item['current']]
        item['shared']=item['reference_count']>1
    _UI_TEXT_CACHE.update(signature=sig,rows=rows,files=files,errors=errors)
    return rows,files,errors


def _ui_text_invalidate():
    _UI_TEXT_CACHE.update(signature=None,rows=None,files=None,errors=None)
    _UI_TEXT_FILE_CACHE.clear()


def _ui_text_plan(file_name,index,new_text,mode='auto',force_tokens=False):
    plan=_shared_text_editor().plan(file_name,index,new_text,force_tokens)
    plan['mode']='rebuild';plan['has_backup']=_shared_text_editor().store.has_pristine
    return plan


def _ui_text_apply_one(file_name,index,new_text,mode='auto',force_tokens=False):
    applied=_shared_text_editor().apply(file_name,index,new_text,force_tokens)
    plan=applied['plan'];plan['mode']='rebuild'
    result=dict(ok=True,verified=applied['verified'],mode='rebuild',write=applied['write'])
    _ui_text_invalidate()
    return result,plan


@app.route('/api/ui_text/status')
def ui_text_status():
    try:
        files=_ui_text_quick_status()
        return jsonify(dict(ok=True,lazy=True,files=files,file_count=len(files),
                            string_count=None,user_facing=None,modified_count=None,
                            shared_count=None,categories=_UI_TEXT_CATEGORIES,
                            category_counts={},errors=[],encoding='Latin-1',
                            long_text_repoint=True,
                            note='Tables are decoded on demand. Choose one table or enter a search to scan all tables.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/list',methods=['POST'])
def ui_text_list():
    q=request.get_json(silent=True) or {}
    query=str(q.get('q') or '').casefold().strip()
    file_name=str(q.get('file') or 'all')
    category=str(q.get('category') or 'all')
    modified=bool(q.get('modified_only'));include_internal=bool(q.get('include_internal'))
    page=max(0,int(q.get('page',0)));per=max(1,min(250,int(q.get('per',100))))
    try:
        # Initial tab load is intentionally non-blocking. Scanning every table
        # is deferred until the user chooses a table or enters a real search.
        if file_name=='all' and not query and category=='all' and not modified:
            return jsonify(dict(ok=True,total=0,page=page,per=per,rows=[],
                                files=_ui_text_quick_status(),errors=[],requires_filter=True,
                                note='Choose a TEXT table or type a search term to scan all tables.'))
        rows=[];files=[];errors=[]
        if file_name!='all':
            fr,fm,fe=_ui_text_scan_file(file_name,include_stock=True)
            rows=list(fr);files=[fm];errors.extend(fe)
        else:
            # Explicit all-table search. Decode one file at a time and retain
            # each result in the per-file cache, so subsequent searches are fast.
            for fm0 in _ui_text_quick_status():
                try:
                    fr,fm,fe=_ui_text_scan_file(fm0['name'],include_stock=True)
                    rows.extend(fr);files.append(fm);errors.extend(fe)
                except Exception as ex:errors.append(f"{fm0['name']}: {ex}")
        out=[]
        for r in rows:
            if category!='all' and r['category']!=category:continue
            if modified and not r['modified']:continue
            if not include_internal and not r['user_facing']:continue
            hay=' '.join((r['file'],str(r['index']),r['current'],r.get('stock') or '',r['category'],r['screen'])).casefold()
            if query and query not in hay:continue
            x=dict(r);x['current_display']=_ui_text_visible(x['current']);x['stock_display']=(_ui_text_visible(x['stock']) if x['stock'] is not None else None)
            out.append(x)
        total=len(out)
        return jsonify(dict(ok=True,total=total,page=page,per=per,
                            rows=out[page*per:(page+1)*per],files=files,errors=errors,
                            requires_filter=False))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/change',methods=['POST'])
def ui_text_change():
    q=request.get_json(silent=True) or {}
    try:
        plan=_ui_text_plan(q['file'],q['index'],q.get('new',''),q.get('mode','auto'),bool(q.get('force_tokens')))
        if q.get('dry_run'):return jsonify(dict(ok=True,dry_run=True,plan=plan))
        result,plan=_ui_text_apply_one(q['file'],q['index'],q.get('new',''),q.get('mode','auto'),bool(q.get('force_tokens')))
        return jsonify(dict(ok=True,plan=plan,result=result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/restore',methods=['POST'])
def ui_text_restore():
    q=request.get_json(silent=True) or {}
    try:
        result=_shared_text_editor().restore(q['file'],q['index'])
        _ui_text_invalidate()
        return jsonify(dict(ok=True,stock=result['stock'],plan=result['plan'],result=result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/restore_file',methods=['POST'])
def ui_text_restore_file():
    q=request.get_json(silent=True) or {}
    try:
        result=_shared_text_editor().restore_file(q['file'])
        _ui_text_invalidate();return jsonify(dict(ok=True,result=result,restored=result['file']))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/export')
def ui_text_export():
    try:
        rows,_files,_errors=_ui_text_scan();file_name=request.args.get('file','all');category=request.args.get('category','all')
        chosen=[r for r in rows if (file_name=='all' or r['file']==file_name) and (category=='all' or r['category']==category)]
        out=io.StringIO(newline='');fields=['file','index','category','screen','stock_text','current_text','new_text','current_bytes','reference_count','format_tokens']
        w=csv.DictWriter(out,fieldnames=fields);w.writeheader()
        for r in chosen:w.writerow(dict(file=r['file'],index=r['index'],category=r['category'],screen=r['screen'],stock_text=r.get('stock') or '',current_text=r['current'],new_text=r['current'],current_bytes=r['current_length'],reference_count=r['reference_count'],format_tokens=' | '.join(r['tokens'])))
        data=io.BytesIO(out.getvalue().encode('utf-8-sig'))
        return send_file(data,mimetype='text/csv',as_attachment=True,download_name='nascar15_ui_text_export.csv')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/import_preview',methods=['POST'])
def ui_text_import_preview():
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a CSV exported by the UI Text Editor')
        raw=up.read()
        if len(raw)>8*1024*1024:raise ValueError('CSV exceeds the 8 MB safety limit')
        changes=_shared_text_editor().preview_csv(raw)
        return jsonify(dict(ok=True,count=len(changes),valid_count=sum(1 for x in changes if x['valid']),invalid_count=sum(1 for x in changes if not x['valid']),changes=changes))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


def _ui_text_batch_apply_internal(changes,force_tokens=False,source_prefix='UI Text batch'):
    result=_shared_text_editor().apply_batch(changes,force_tokens)
    _ui_text_invalidate()
    return dict(ok=True,atomic=True,**result)


@app.route('/api/ui_text/batch_apply',methods=['POST'])
def ui_text_batch_apply():
    q=request.get_json(silent=True) or {}
    try:return jsonify(_ui_text_batch_apply_internal(q.get('changes') or [],bool(q.get('force_tokens')),'UI Text CSV'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end v0.9.25 UI TEXT EDITOR ====================

# ---------------- stats (base 0-100 + supported custom range) ----------------
STATS=list(RATING_FIELDS)
STAT_LABELS=list(RATING_LABELS)
PYC_CODE_BASE = SHARED_PYC_CODE_BASE
STAT_EXPERIMENTAL_ABS_MAX=1_000_000_000.0


def load_profiles():
    name=active_game_profile().get('ai_profiles_file','ai_profiles.csv')
    return list(csv.DictReader(open(_game_data_path(name),encoding='utf-8-sig')))


def _shared_ratings_editor():
    return RatingsEditor(_shared_installation(), DATA)


def read_stats(_reg):
    return _shared_ratings_editor().ratings()


def write_stat(_reg, profile_id, stat, value100, experimental=False):
    return _shared_ratings_editor().set_rating(profile_id, stat, value100, experimental)


def reset_stats(_reg, profile_id):
    result = _shared_ratings_editor().restore(profile_id)
    return len(result['applied'])

# ---------------- menus: numbers / custom thumbs ----------------
MENU_CONTAINERS = {
    'numbers': ('0','SPRINTNUMS2015.ARC'),
    'teams': ('0','2DRIVERSELECTMENUIMAGE.ARC'),
    'shoplogo': ('0','TEAMSHOPLOGO.ARC'),
    'shoplogo2': ('0','TEAMSHOPLOGO2.ARC'),
    'careerthumbs': ('0','BASESCHEMETHUMBNAILS.ARC'),
    'customthumbs': ('1','CUSTOMSCHEMETHUMBNAILS.ARC'),
}

# Clean-file mapping corrected the SPRINTNUMS parser itself.  Public v1 began
# every payload 40 bytes late, then used roll/seam hacks to make the corrupted
# preview look plausible.  With the native 16-byte records and +24-byte texture
# header mapped, the 128x64 / 64x128 atlases decode at their real orientation.
def _numcard_unroll(img):
    return img.copy()

# Both directions are the same identity transform for the corrected parser.
_numcard_reroll = _numcard_unroll

def _menu_containers():
    out=dict(MENU_CONTAINERS)
    out['numbers']=('0',active_game_profile().get('number_container','SPRINTNUMS2015.ARC'))
    for key in active_game_profile().get('unavailable_menu_keys',()):
        out.pop(str(key),None)
    return out


def menu_container(reg, key, live=True):
    arcid,name=_menu_containers()[key]
    a=need(reg,arcid)
    off,size=find_entry(reg,arcid,name)
    src=a['ar'] if live or not os.path.exists(a['bak']) else a['bak']
    with open(src,'rb') as fh:
        fh.seek(off); return arcid,off,size,fh.read(size)


def _menu_parse_entries(arc,key):
    """Parse menu banks with the mapped SPRINTNUMS storage geometry.

    SPRINTNUMS2015 has 94 DXT1 payloads that are all 4096 bytes and decode as
    128x64.  The 47 BIG_* records advertise 64x64 in one native header field,
    but the game consumes the complete 128x64 BC1 surface.  Treating those as
    64x64 decodes only the first half of the payload and produces the horizontal
    stripe preview seen in RC1.
    """
    return C.parse_multi_arc(arc,known_dims=(128,64) if key=='numbers' else None)

# ============ v0.7: paint-scheme preview containers (2DRIVERSELECTTD_*) ============
# Each team has a 2DRIVERSELECTTD_<teamid>.ARC in ARCHIVE1 holding DXT5 previews:
#   DRIVERPAINT_<id>   256x256  car render (the carousel image)
#   PAINTSCHEME_<id>   256x256  scheme thumbnail
#   DRIVER_<id>_3DNUM  512x256  3D number render
# Standard ARCC multi-texture containers -> reuse parse_multi_arc / multi_*.

def td_containers(reg):
    """Return list of (arcid, name, off, size) for every 2DRIVERSELECTTD_* file."""
    out=[]
    for arcid in reg:
        cdf=reg[arcid].get('cdf')
        if not cdf: continue
        try: ent=parse_cdfiles(cdf)
        except Exception: continue
        for o,s,n in ent:
            if n.startswith('2DRIVERSELECTTD_'):
                out.append((arcid, n, o, s))
    return sorted(out, key=lambda x:x[1])

def td_read(reg, container):
    """Read a TD container's bytes (live or backup) by its ARC name."""
    for arcid, name, off, size in td_containers(reg):
        if name==container:
            a=need(reg,arcid)
            with open(a['ar'],'rb') as fh:
                fh.seek(off); return arcid, off, size, fh.read(size)
    raise ValueError('TD container not found: '+container)

@app.route('/api/tdlist')
def api_tdlist():
    editor=_shared_texture_editor();result=[]
    for arcid in editor.installation.archive_pairs:
        for indexed in editor.installation.entries(arcid):
            name=indexed.name
            if not name.startswith('2DRIVERSELECTTD_'):continue
            team=name.replace('2DRIVERSELECTTD_','').replace('.ARC','')
            try:
                entries=[dict(name=e['name'],w=e['width'],h=e['height'],fmt=e['format']) for e in editor.entries(arcid,name)]
            except Exception:
                entries=[]
            result.append(dict(container=name,team=team,entries=entries))
    return jsonify(dict(ok=True,containers=sorted(result,key=lambda row:row['container'])))


@app.route('/api/td/<container>/<entry>', methods=['GET','POST'])
def api_td_entry(container, entry):
    try:
        editor=_shared_texture_editor();arcid,_indexed=editor.installation.find_entry(container)
        if request.method=='GET':
            return Response(editor.image_png(arcid,container,entry,pristine=bool(request.args.get('pristine'))),mimetype='image/png')
        f=request.files.get('file')
        if not f:return jsonify(dict(ok=False,error='no file')),400
        result=editor.replace_image(arcid,container,entry,Image.open(f.stream),
            resize_mode=request_resize_mode('fit'),experimental=bool(request.args.get('experimental')))
        _clear_ui_thumb_cache();return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/td_copy', methods=['POST'])
def api_td_copy():
    d=request.get_json(force=True)
    try:
        editor=_shared_texture_editor();src=d['src_container'];dst=d['dst_container']
        s_arcid,_=editor.installation.find_entry(src);d_arcid,_=editor.installation.find_entry(dst)
        result=editor.copy_entry(s_arcid,src,d['src_entry'],d_arcid,dst,d['dst_entry'])
        _clear_ui_thumb_cache();return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/td/<container>/<entry>/reset', methods=['POST'])
def api_td_reset(container, entry):
    try:
        editor=_shared_texture_editor();arcid,_=editor.installation.find_entry(container)
        result=editor.restore_entry(arcid,container,entry)
        _clear_ui_thumb_cache();return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/menu/<key>')
def api_menu_list(key):
    try:
        if key not in _menu_containers():return jsonify(dict(ok=False,error='bad key')),404
        arcid,name=_menu_containers()[key];entries=_shared_texture_editor().entries(arcid,name)
        return jsonify(dict(ok=True,entries=[dict(name=e['name'],w=e['width'],h=e['height']) for e in entries]))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex),entries=[])),400


@app.route('/api/menu/<key>/<name>', methods=['GET','POST'])
def api_menu_entry(key,name):
    try:
        if key not in _menu_containers():return jsonify(dict(ok=False,error='bad key')),404
        arcid,container=_menu_containers()[key];editor=_shared_texture_editor()
        if request.method=='GET':
            return Response(editor.image_png(arcid,container,name,pristine=bool(request.args.get('pristine'))),mimetype='image/png')
        if key in ('shoplogo','shoplogo2'):
            return jsonify(dict(ok=False,error='Team Shop logo replacement remains locked because its special short payload caused an in-game fatal error.')),400
        f=request.files.get('file')
        if not f:return jsonify(dict(ok=False,error='no file')),400
        requested=request_resize_mode('auto');mode='stretch' if key=='numbers' else ('fit' if requested=='auto' else requested)
        result=editor.replace_image(arcid,container,name,Image.open(f.stream),resize_mode=mode,experimental=(key!='numbers'))
        _clear_ui_thumb_cache();return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/menu/<key>/<name>/reset', methods=['POST'])
def api_menu_reset(key,name):
    try:
        if key not in _menu_containers():return jsonify(dict(ok=False,error='bad key')),404
        arcid,container=_menu_containers()[key];result=_shared_texture_editor().restore_entry(arcid,container,name)
        _clear_ui_thumb_cache();return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400



# Limited profiles start fail-closed. Route rules are listed exactly so future
# full-profile endpoints remain blocked until deliberately reviewed.
_LIMITED_PROFILE_ALLOWED_RULES={
 '/api/status','/api/app_settings','/api/diagnostics/export','/api/backup_now','/api/setpath','/api/restore',
 '/api/grid','/api/template/<name>','/api/thumb/<name>','/api/scheme_smart/<name>','/api/scheme/<name>',
 '/api/layer/<name>','/api/slotthumb/<name>','/api/build','/api/restore_slot','/api/import_scheme/<name>',
 '/api/previewedit/<name>','/api/roster','/api/name','/api/handle','/api/names/export','/api/names/restore_all',
 '/api/ui_text/status','/api/ui_text/list','/api/ui_text/change','/api/ui_text/restore','/api/ui_text/restore_file',
 '/api/ui_text/export','/api/ui_text/import_preview','/api/ui_text/batch_apply',
 '/api/stats','/api/stats/set','/api/stats/reset',
 '/api/audio/banks','/api/audio/samples','/api/audio/preview','/api/audio/export','/api/audio/export_bank',
 '/api/audio/restore_bank','/api/audio/replace','/api/audio/restore',
 '/api/pyc/status','/api/pyc/records','/api/pyc/set','/api/pyc/set_batch','/api/pyc/baseline','/api/pyc/restore',
 '/api/pyc/aitrack_crosswalk','/api/pyc/aiglobal','/api/pyc/worldpace',
 '/api/scr/list','/api/scr/set','/api/scr/keys','/api/scr/key/set','/api/scr/keys/batch',
 '/api/ui/discover','/api/ui/status','/api/ui/list','/api/ui/audit','/api/ui/tire_family','/api/ui/map',
 '/api/ui/manifest/export','/api/ui/mappings/export','/api/ui/export_raw','/api/ui/export','/api/ui/thumb',
 '/api/ui/replace_raw','/api/ui/copy','/api/ui/restore','/api/ui/bulk_restore',
 '/api/schedule','/api/schedule/stock','/api/schedule/preview','/api/schedule/apply','/api/schedule/restore',
 '/api/schedule/stock36_laps','/api/schedule/event_lap/preview','/api/schedule/event_lap/apply',
 '/api/schedule/event_laps/batch/preview','/api/schedule/event_laps/batch/apply','/api/schedule/stock36_laps/restore',
 '/api/tracks/files','/api/tracks/compare','/api/tracks/report','/api/tracks/export',
 '/api/presets','/api/presets/save','/api/presets/delete','/api/presets/export','/api/presets/import','/api/pitlog',
 '/api/pyc/audit','/api/pyc/audit/export','/api/support/check','/api/support/report','/api/help/request'
}

def _limited_profile_route_allowed(path):
    rule=getattr(getattr(request,'url_rule',None),'rule',None)
    return (rule or path) in _LIMITED_PROFILE_ALLOWED_RULES

_LIMITED_PROFILE_PYC_WRITE_POLICY={
 'AIRACINGTRACKCONFIG_C': ('DB_AICONFIG_SCRIPT.PYC', None),
 'AIRACINGGLOBALCONFIG_C': ('DB_AICONFIG_SCRIPT.PYC', None),
 'WORLDSCRIPT_C': ('DB_GAME_LOCAL_SCRIPT.PYC', None),
 'RACEDATA_C': ('DB_GAME_LOCAL_SCRIPT.PYC', {'RaceLaps'}),
}

def _limited_profile_pyc_write_allowed(rule,payload):
    if rule not in ('/api/pyc/set','/api/pyc/set_batch'): return True,None
    payload=payload if isinstance(payload,dict) else {}; game=active_game_name()
    cls=str(payload.get('class') or '').upper(); policy=_LIMITED_PROFILE_PYC_WRITE_POLICY.get(cls)
    if not policy: return False,f"PYC class {cls or '(missing)'} is read-only in {game}."
    expected_file,fields=policy
    if str(payload.get('file') or '').upper()!=expected_file:
        return False,f"{cls} writes are limited to {expected_file} in {game}."
    requested=[]
    if rule=='/api/pyc/set': requested=[str(payload.get('field') or '')]
    else: requested=[str(x.get('field') or '') for x in (payload.get('changes') or []) if isinstance(x,dict)]
    if not requested or any(not f for f in requested): return False,'PYC write request has no valid field.'
    if fields is not None and any(f not in fields for f in requested):
        return False,f'{cls} writes are limited to: '+', '.join(sorted(fields))+'.'
    return True,None

def _limited_profile_ui_write_allowed(rule):
    if rule not in ('/api/ui/replace_raw','/api/ui/copy','/api/ui/restore','/api/ui/bulk_restore'): return True,None
    allowed_archives={'0','1'}
    archives=[]
    if rule=='/api/ui/replace_raw': archives=[str(request.form.get('archive') or '')]
    else:
        q=request.get_json(silent=True) or {}
        if rule=='/api/ui/copy': archives=[str((q.get('dst') or {}).get('archive') or '')]
        elif rule=='/api/ui/restore': archives=[str(q.get('archive') or '')]
        else: archives=[str(x.get('archive') or '') for x in (q.get('targets') or []) if isinstance(x,dict)]
    if not archives or any(a not in allowed_archives for a in archives):
        return False,f"{active_game_name()} Graphics writes are currently limited to mapped ARCHIVE0/1 front-end assets. Track packages and paint archives remain read-only here."
    return True,None

@app.before_request
def _game_request_guard():
    path=request.path
    # Static assets and the shell document touch no game state, so they must not
    # queue behind a long install. Holding the global lock for them made the
    # whole UI appear frozen (rather than merely busy) during full repairs.
    _lock_exempt = (path=='/api/games/select' or path=='/' or path.startswith('/static/')
                    or path=='/favicon.ico')
    if not _lock_exempt:
        _GAME_SWITCH_LOCK.acquire(); g._game_switch_lock_owned=True
    public=(path=='/' or path=='/favicon.ico' or path.startswith('/static/') or path in ('/api/games/session','/api/games/select','/api/status','/api/appdata/export','/api/appdata/import'))
    if not GAME_SESSION_SELECTED and not public:
        return jsonify(dict(ok=False,error="Choose a supported NASCAR game before using the app.",code='game_not_selected')),409
    if GAME_SESSION_SELECTED and _limited_editor_profile() and path.startswith('/api/') and not public:
        rule=getattr(getattr(request,'url_rule',None),'rule',None) or path
        if not _limited_profile_route_allowed(path):
            return jsonify(dict(ok=False,error=f"{path} is not available for {active_game_name()} yet.",code='feature_not_available',game=active_game_name())),403
        allowed,reason=_limited_profile_pyc_write_allowed(rule,request.get_json(silent=True) if request.method in ('POST','PUT','PATCH') else None)
        if not allowed:
            return jsonify(dict(ok=False,error=reason,code='pyc_write_not_allowed',game=active_game_name())),403
        allowed,reason=_limited_profile_ui_write_allowed(rule)
        if not allowed:
            return jsonify(dict(ok=False,error=reason,code='graphics_write_not_allowed',game=active_game_name())),403

@app.after_request
def _log_api_failures(response):
    """Print the reason next to the access-log line for failed /api/ calls.

    Handlers here deliberately convert exceptions into {ok:false,error:...} with a
    4xx status, which keeps the UI friendly but leaves the console showing only
    `"GET /api/... " 400 -`. That is not enough to diagnose a bug report, so echo
    the message. Never touch streamed/file responses.
    """
    try:
        if (response.status_code >= 400
                and request.path.startswith('/api/')
                and not response.direct_passthrough
                and response.is_json):
            body = response.get_json(silent=True) or {}
            msg = body.get('error') or body.get('message')
            if msg:
                code = body.get('code')
                tag = f' [{code}]' if code else ''
                print(f'  -> {request.method} {request.path} {response.status_code}{tag}: {msg}',
                      file=sys.stderr, flush=True)
    except Exception:
        pass
    return response


@app.teardown_request
def _release_game_request_guard(_error=None):
    if getattr(g,'_game_switch_lock_owned',False):
        g._game_switch_lock_owned=False; _GAME_SWITCH_LOCK.release()

# ---------------- routes ----------------
@app.route('/')
def root():
    # The UI is inline JavaScript inside index.html.  Reusing a cached page from
    # an older RC can silently hide new controls while talking to a newer (or
    # older) backend, so the app shell must always be fetched fresh.
    response=send_from_directory(os.path.join(RES_DIR,'static'),'index.html',max_age=0)
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']='no-cache'
    response.headers['Expires']='0'
    return response

def _game_session_payload():
    profiles=[]
    for gid,profile in GAME_PROFILES.items():
        path=detect_game(gid)
        profiles.append(dict(id=gid,name=profile['name'],path=path,found=bool(path),
                             full_feature_set=bool(profile['full_feature_set']),
                             tabs=list(profile['tabs']),paint_modes=list(profile['paint_modes']),team_editor_mode=profile.get('team_editor_mode'),graphics_mode=profile.get('graphics_mode'),season_year=profile.get('season_year'),number_container=profile.get('number_container')))
    current=active_game_profile()
    return dict(ok=True,selected=bool(GAME_SESSION_SELECTED),active_game=ACTIVE_GAME,
                game_name=current['name'],profiles=profiles,tabs=list(current['tabs']),
                paint_modes=list(current['paint_modes']),full_feature_set=bool(current['full_feature_set']),team_editor_mode=current.get('team_editor_mode'),graphics_mode=current.get('graphics_mode'),season_year=current.get('season_year'),number_container=current.get('number_container'))

@app.route('/api/games/session')
def game_session():
    return jsonify(_game_session_payload())

@app.route('/api/games/select', methods=['POST'])
def game_select():
    global GAME_SESSION_SELECTED
    q=request.get_json(silent=True) or {}
    gid=str(q.get('game_id') or '').strip().lower()
    try:
        with _GAME_SWITCH_LOCK: _activate_game(gid)
    except RuntimeError as ex:
        return jsonify(dict(ok=False,error=str(ex),code='game_switch_busy')),409
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    GAME_SESSION_SELECTED=True
    return jsonify(_game_session_payload())

@app.route('/api/status')
def status():
    g,reg=registry()
    profile=active_game_profile()
    ok=bool(g and all(k in reg for k in profile['required_archives']))
    added_scheme_scan = None
    if ok and not _limited_editor_profile():
        try:
            added_scheme_scan = _shared_managed_paint_editor().reconcile_live_state()
        except Exception as ex:
            added_scheme_scan = {'changed': False, 'error': str(ex)}
    core=set(('0','1','2','3','4','5','6','7','8','314'))
    dlc=[k for k in reg if k not in core]
    nbak=sum(1 for v in reg.values() if os.path.exists(v['bak']))
    return jsonify(dict(game=g, ok=ok, archives=sorted(reg.keys()),
        game_id=ACTIVE_GAME,game_name=profile['name'],session_selected=bool(GAME_SESSION_SELECTED),
        tabs=list(profile['tabs']),paint_modes=list(profile['paint_modes']),
        full_feature_set=bool(profile['full_feature_set']),team_editor_mode=profile.get('team_editor_mode'),graphics_mode=profile.get('graphics_mode'),season_year=profile.get('season_year'),required_archives=list(profile['required_archives']),
        dlc_count=len(dlc), texconv=bool(texconv_path()), ffmpeg=bool(ffmpeg_path()),
        python_version='%d.%d.%d' % sys.version_info[:3],
        missing_archives=[k for k in profile['required_archives'] if k not in reg],
        app_name=APP_NAME, version=APP_VERSION, release_label=APP_RELEASE_LABEL, backup_count=nbak, archive_count=len(reg),
        added_scheme_scan=added_scheme_scan,
        backed_up=bool(reg and nbak)))

def _clean_project_destination(value, field_name):
    value=str(value or '').strip()
    if not value:
        return ''
    low=value.lower()
    if low.startswith(('https://','http://','mailto:')):
        return value
    raise ValueError(f'{field_name} must be an http(s) URL, mailto link, or blank')


def _app_settings_payload(cfg=None):
    cfg=cfg if isinstance(cfg,dict) else load_cfg()
    ui=cfg.get('app_settings') if isinstance(cfg.get('app_settings'),dict) else {}
    accent=str(ui.get('accent_color') or '#ffd23f').strip()
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent):
        accent='#ffd23f'
    accent2=str(ui.get('accent_color_2') or '#ffd23f').strip()
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent2):
        accent2='#ffd23f'
    def choice(name,allowed,default):
        value=str(ui.get(name) or default).strip()
        return value if value in allowed else default
    nav_default=['Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup','Repoint']
    raw_nav=ui.get('nav_order')
    nav_order=[]
    if isinstance(raw_nav,list):
        for value in raw_nav:
            value=str(value or '').strip()
            if value in nav_default and value not in nav_order:
                nav_order.append(value)
    nav_order += [value for value in nav_default if value not in nav_order]
    return dict(
        theme_preset=choice('theme_preset',{'classic','modern','subtle','custom'},'classic'),
        accent_color=accent.lower(),
        accent_color_2=accent2.lower(),
        gradient_direction=choice('gradient_direction',{'horizontal','vertical','diagonal','reverse_diagonal'},'horizontal'),
        accent_style=choice('accent_style',{'solid','subtle','bold'},'solid'),
        surface_style=choice('surface_style',{'flat','soft','contrast'},'flat'),
        nav_order=nav_order,
        remember_section_state=bool(ui.get('remember_section_state',True)),
        help_destination=str(ui.get('help_destination') or '').strip(),
        support_destination=str(ui.get('support_destination') or '').strip(),
        interface_density=choice('interface_density',{'compact','comfortable','spacious'},'comfortable'),
        text_size=choice('text_size',{'small','normal','large'},'normal'),
        page_width=choice('page_width',{'standard','wide','full'},'standard'),
        thumbnail_size=choice('thumbnail_size',{'small','normal','large'},'normal'),
        reduce_motion=bool(ui.get('reduce_motion',False)),
        remember_last_tab=bool(ui.get('remember_last_tab',True)),
        startup_tab=choice('startup_tab',{'Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup'},'Setup'),
        auto_open_browser=bool(ui.get('auto_open_browser',True)),
        # Whether the first-run walkthrough has been completed or dismissed.
        # Stored server-side so it survives clearing browser data and is the
        # same on every browser pointed at this install.
        tour_completed=bool(ui.get('tour_completed',False)),
    )


@app.route('/api/app_settings', methods=['GET','POST'])
def app_settings():
    cfg=load_cfg()
    if request.method=='GET':
        return jsonify(dict(ok=True,**_app_settings_payload(cfg)))
    q=request.get_json(silent=True) or {}
    current=_app_settings_payload(cfg)
    accent=str(q.get('accent_color',current['accent_color'])).strip().lower()
    accent2=str(q.get('accent_color_2',current['accent_color_2'])).strip().lower()
    if not re.fullmatch(r'#[0-9a-f]{6}',accent) or not re.fullmatch(r'#[0-9a-f]{6}',accent2):
        return jsonify(dict(ok=False,error='accent colors must use #RRGGBB format')),400
    try:
        help_destination=_clean_project_destination(q.get('help_destination',current['help_destination']),'help destination')
        support_destination=_clean_project_destination(q.get('support_destination',current['support_destination']),'support destination')
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    def selected(name,allowed):
        value=str(q.get(name,current[name])).strip()
        if value not in allowed:raise ValueError(f'invalid {name.replace("_"," ")} setting')
        return value
    try:
        saved=dict(
            theme_preset=selected('theme_preset',{'classic','modern','subtle','custom'}),
            accent_color=accent,
            accent_color_2=accent2,
            gradient_direction=selected('gradient_direction',{'horizontal','vertical','diagonal','reverse_diagonal'}),
            accent_style=selected('accent_style',{'solid','subtle','bold'}),
            surface_style=selected('surface_style',{'flat','soft','contrast'}),
            nav_order=current['nav_order'],
            remember_section_state=bool(q.get('remember_section_state',current['remember_section_state'])),
            help_destination=help_destination,
            support_destination=support_destination,
            interface_density=selected('interface_density',{'compact','comfortable','spacious'}),
            text_size=selected('text_size',{'small','normal','large'}),
            page_width=selected('page_width',{'standard','wide','full'}),
            thumbnail_size=selected('thumbnail_size',{'small','normal','large'}),
            reduce_motion=bool(q.get('reduce_motion',current['reduce_motion'])),
            remember_last_tab=bool(q.get('remember_last_tab',current['remember_last_tab'])),
            startup_tab=selected('startup_tab',{'Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup'}),
            auto_open_browser=bool(q.get('auto_open_browser',current['auto_open_browser'])),
            tour_completed=bool(q.get('tour_completed',current['tour_completed'])),
        )
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    if 'nav_order' in q:
        raw_nav=q.get('nav_order')
        if not isinstance(raw_nav,list):
            return jsonify(dict(ok=False,error='tab order must be a list')),400
        nav_default=['Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup','Repoint']
        nav_order=[]
        for value in raw_nav:
            value=str(value or '').strip()
            if value in nav_default and value not in nav_order:
                nav_order.append(value)
        saved['nav_order']=nav_order+[value for value in nav_default if value not in nav_order]
    cfg['app_settings']=saved
    save_cfg(cfg)
    return jsonify(dict(ok=True,**_app_settings_payload(cfg)))

@app.route('/api/diagnostics/export')
def diagnostics_export():
    """Small support bundle: no game archives or copyrighted payloads."""
    buf=io.BytesIO(_shared_support_reporter().diagnostics_bytes(load_cfg()))
    return send_file(buf,mimetype='application/zip',as_attachment=True,
                     download_name=f'nascar15_modding_app_v{APP_VERSION}_diagnostics.zip')

@app.route('/api/backup_now', methods=['POST'])
def backup_now():
    """Force pristine backups of every archive + cdfiles pair that doesn't
    already have one. Never overwrites an existing backup."""
    try:
        result=_shared_backup_manager().create_missing()
        if result['created']:_clear_ui_thumb_cache()
        return jsonify(result),(200 if result['ok'] else 400)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/setpath', methods=['POST'])
def setpath():
    gpath=(request.json or {}).get('path','').strip().strip('"').rstrip('\\/')
    profile=active_game_profile()
    root=_profile_root_from(gpath,ACTIVE_GAME)
    if not root:
        needed=', '.join('ARCHIVE'+k+'.AR' for k in profile['required_archives'])
        looked=os.path.join(gpath,'data') if gpath else '(no folder given)'
        hint=''
        if gpath and os.path.basename(gpath).lower()=='data':
            hint=(' That path already ends in the data folder - try the folder above it: '
                  +os.path.dirname(gpath))
        elif gpath and not os.path.isdir(gpath):
            hint=' That folder does not exist or is not readable.'
        return jsonify(dict(ok=False,error=f'{needed} were not all found in {looked}.{hint}')),400
    # Store the resolved install ROOT: registry() appends the data folder
    # downstream, so storing a data path here silently produces data\\data.
    c=load_cfg(); c['game']=root; save_cfg(c)
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,game_id=ACTIVE_GAME,game_name=profile['name'],path=root))

def custom_scheme_slots():
    """Enumerate player custom schemes (CUSTOM-named liveries) so they show in
    the grid. Assumes the LIVERY_ wrapper convention; adjust if your custom
    slots differ."""
    g, reg = registry()
    if not g: return []
    out = {}
    entries_by_arc = {}
    hd_map = {}
    for arcid, info in reg.items():
        cdf = info.get('cdf')
        if not cdf: continue
        try: entries_by_arc[arcid] = parse_cdfiles(cdf)
        except Exception: entries_by_arc[arcid] = []
        for o,s,n in entries_by_arc[arcid]:
            if n.startswith('HDLIVERY_') and 'CUSTOM' in n.upper():
                hd_map[n] = (arcid,o,s)
    for arcid,ent in entries_by_arc.items():
        for o,s,n in ent:
            if n.startswith('LIVERY_') and 'CUSTOM' in n.upper():
                sl = slot_from_name(n,o,s,arcid,hd_map)
                if sl:
                    sl['kind']='custom'; out[n]=sl
    return sorted(out.values(), key=lambda x:x['name'])

def _managed_extra_slot_names():
    """Return livery wrapper names owned by the app-created slot system.

    App-created liveries also exist in live cdfiles, so the legacy Grid browser
    used to mistake them for stock paint slots. That exposed stock Restore/Import
    actions and made /api/thumb seek their appended offsets in a pristine backup
    that predates the entries. Keep them exclusively in Existing Paints.
    """
    names=set()
    try:
        state=json.load(open(os.path.join(USER_DIR,'extra_schemes_v1.json'),'r',encoding='utf-8'))
    except Exception:
        state={}
    for item in state.get('schemes',[]) if isinstance(state,dict) else []:
        if not isinstance(item,dict):
            continue
        for field in ('sd_entry','hd_entry'):
            value=str(item.get(field) or '').strip()
            if value:
                names.add(value.upper())
        for field in ('script_name','identity_migrated_from'):
            token=str(item.get(field) or '').strip()
            token=re.sub(r'^(?:HD)?LIVERY_','',token,flags=re.I)
            token=re.sub(r'\.ARC$','',token,flags=re.I)
            if token:
                names.add(f'LIVERY_{token}.ARC'.upper())
                names.add(f'HDLIVERY_{token}.ARC'.upper())
    return names


def _is_managed_extra_slot(name):
    upper=str(name or '').upper()
    # _EXTRA_ was used by the pre-CUSTOM migration builds and is always an
    # app-created independent slot, even when an old state entry is incomplete.
    return upper in _managed_extra_slot_names() or ('_EXTRA_' in upper and upper.endswith('.ARC'))


def _managed_extra_action_error(name):
    return (f'{name} is an app-created independent paint. Use Paint Schemes > '
            'Existing Paints so its database record, current-team thumbnail, '
            'and AI schedule wiring stay synchronized.')


@app.route('/api/grid')
def grid():
    out=[]
    _base_names=set()
    managed=_managed_extra_slot_names()
    for s in grid_slots():
        if str(s.get('name','')).upper() in managed or _is_managed_extra_slot(s.get('name')):
            continue
        _base_names.add(s['name'])
        s=dict(s)
        s['has_scheme']=os.path.exists(os.path.join(SCHEMES,s['name']+'.png'))
        s['has_layer']=os.path.exists(os.path.join(SCHEMES,s['name']+'.layer.png'))
        s['has_thumb']=os.path.exists(os.path.join(SCHEMES,s['name']+'.thumb.png'))
        s['livery_uid']=_slot_livery_uid(s['name'])
        s['fei']=bool(s['fei'])
        out.append(s)
    # v0.6: append player custom schemes not already in the base grid
    try:
        for cs in custom_scheme_slots():
            if cs['name'] in _base_names or _is_managed_extra_slot(cs.get('name')):
                continue
            cs=dict(cs)
            cs['has_scheme']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.png'))
            cs['has_layer']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.layer.png'))
            cs['has_thumb']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.thumb.png'))
            cs['livery_uid']=_slot_livery_uid(cs['name'])
            cs['fei']=bool(cs.get('fei'))
            out.append(cs)
    except Exception:
        pass
    return jsonify(out)

def _slot(name):
    for s in grid_slots():
        if s['name']==name: return s
    return None




def _cdf_named_entry(cdf_path, name):
    """Resolve one CDF entry by name, case-insensitively."""
    wanted=str(name or '').upper()
    if not wanted or not os.path.exists(cdf_path):
        return None
    rows=parse_cdfiles(cdf_path)
    return next((x for x in rows if str(x[2]).upper()==wanted),None)


def _data_registry(data_dir, suffix=''):
    """Build an archive registry from a real data directory or paired backup suffix.

    ``suffix`` is used for paired files such as ``ARCHIVE2.AR.gridapp.bak`` and
    ``cdfiles.dat.gridapp.bak``. Pairing the same suffix prevents an archive from
    one backup generation being interpreted with another generation's CDF.
    """
    out={}
    if not data_dir or not os.path.isdir(data_dir):
        return out
    try:
        entries=os.listdir(data_dir)
    except OSError:
        return out
    if suffix:
        rx=re.compile(r'^cdfiles(\d*)\.dat'+re.escape(suffix)+r'$',re.I)
    else:
        rx=re.compile(r'^cdfiles(\d*)\.dat$',re.I)
    for fname in entries:
        m=rx.match(fname)
        if not m:
            continue
        arcid=m.group(1) or '0'
        arname=f'ARCHIVE{arcid}.AR' if arcid!='0' else 'ARCHIVE0.AR'
        ar=os.path.join(data_dir,arname+suffix)
        cdf=os.path.join(data_dir,fname)
        if os.path.exists(ar):
            out[str(arcid)]=dict(ar=ar,cdf=cdf,bak=ar,label=('backup '+suffix if suffix else 'data folder'),data_dir=data_dir)
    return out


def _norm_path(path):
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except Exception:
        return os.path.normcase(str(path or ''))


def _candidate_clean_data_dirs(game_folder=None):
    """Return verified read-only clean-data candidates, best candidate first."""
    cfg=load_cfg()
    raw=[]
    for key in ('clean_data_dir','paint_clean_data_dir','master_map_clean_data_dir'):
        if cfg.get(key): raw.append(cfg.get(key))
    env=os.environ.get('NASCAR15_CLEAN_DATA')
    if env: raw.append(env)
    # Master Mapper / project default used by this app's clean test lab.
    for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        raw.extend((
            drive+r':\SteamLibrary\data\data',
            drive+r':\NASCAR15_CLEAN_BASELINE\data',
            drive+r':\NASCAR15_AUTOLAB\CLEAN_GAME\data',
        ))
    try:
        entries=stock_baselines()
    except Exception:
        entries={}
    for entry in (entries or {}).values():
        p=(entry or {}).get('path') if isinstance(entry,dict) else None
        if p: raw.append(os.path.dirname(p))
    live_data=os.path.join(game_folder,'data') if game_folder else None
    live_norm=_norm_path(live_data) if live_data else None
    out=[]; seen=set()
    for p in raw:
        if not p: continue
        p=os.path.abspath(os.path.expandvars(os.path.expanduser(str(p))))
        n=_norm_path(p)
        if n in seen or (live_norm and n==live_norm):
            continue
        seen.add(n)
        if os.path.isfile(os.path.join(p,'cdfiles.dat')) and os.path.isfile(os.path.join(p,'ARCHIVE0.AR')):
            out.append(p)
    return out


def _find_resource_refs(search_reg, name, label):
    refs=[]
    wanted=str(name or '').upper()
    for arcid,info in sorted((search_reg or {}).items(),key=lambda kv:(len(str(kv[0])),str(kv[0]))):
        try:
            row=_cdf_named_entry(info['cdf'],wanted)
            if row is None: continue
            off,size,stored=row
            if int(off)<0 or int(size)<=0 or os.path.getsize(info['ar'])<int(off)+int(size):
                continue
            refs.append(dict(arcid=str(arcid),ar=info['ar'],cdf=info['cdf'],offset=int(off),size=int(size),
                             stored_name=stored,label=label,data_dir=info.get('data_dir') or os.path.dirname(info['cdf'])))
        except Exception:
            continue
    return refs


def _find_live_resource(reg, name, preferred_arc=None):
    """Inspect the actual game CDFs and return the real live location."""
    refs=_find_resource_refs(reg,name,'live game')
    if not refs:
        raise ValueError(f'live game does not contain {name} in any archive')
    if preferred_arc is not None:
        preferred=[r for r in refs if str(r['arcid'])==str(preferred_arc)]
        if preferred:
            return preferred[0],refs
    return refs[0],refs


def _pristine_search_sources(reg, game_folder=None):
    """Enumerate clean-baseline and paired-backup registries without guessing."""
    sources=[]
    for data_dir in _candidate_clean_data_dirs(game_folder):
        r=_data_registry(data_dir)
        if r:
            sources.append((f'clean baseline: {data_dir}',r))
    # Search both generations. Do not use backup_path() here because restore must
    # inspect every valid pair rather than trusting one filename preference.
    live_dirs=[]
    for info in (reg or {}).values():
        d=os.path.dirname(info['cdf'])
        if d not in live_dirs: live_dirs.append(d)
    for data_dir in live_dirs:
        for suffix,label in ((LEGACY_BACKUP_SUFFIX,'legacy pristine backup'),(MOD_BACKUP_SUFFIX,'modding-app pristine backup')):
            r=_data_registry(data_dir,suffix)
            if r:
                sources.append((label,r))
    return sources


def _find_pristine_resource(reg, name, expected_size=None, game_folder=None):
    searched=[]; wrong=[]
    for label,source_reg in _pristine_search_sources(reg,game_folder):
        searched.append(label)
        for ref in _find_resource_refs(source_reg,name,label):
            if expected_size is not None and int(ref['size'])!=int(expected_size):
                wrong.append(f'{label} ARCHIVE{ref["arcid"]}: {ref["size"]} bytes')
                continue
            return ref,searched,wrong
    return None,searched,wrong


def _read_exact_region(path, offset, size, label):
    if not os.path.exists(path):
        raise ValueError(f'{label} file is missing: {path}')
    if int(offset)<0 or int(size)<=0 or os.path.getsize(path)<int(offset)+int(size):
        raise ValueError(f'{label} region is outside the file: 0x{int(offset):X}+0x{int(size):X}')
    with open(path,'rb') as fh:
        fh.seek(int(offset)); data=fh.read(int(size))
    if len(data)!=int(size):
        raise ValueError(f'short read for {label}: {len(data)} of {int(size)} bytes')
    return data


def _native_wrapper_mip_image(wrapper, level, hd=False, logical=False):
    """Decode one page-mapped native livery mip from an exact wrapper."""
    if hd:
        dims=_NATIVE_HD_DIMS; offsets=_NATIVE_HD_MIP_OFFSETS; pitches=_NATIVE_HD_MIP_PITCHES
        wrap_x=_NATIVE_HD_WRAP_X_BLOCKS; wrap_y=_NATIVE_HD_WRAP_Y_BLOCKS
        phys=_NATIVE_HD_PHYS_BLOCKS; large_max=5; rolls=_NATIVE_HD_LARGE_ROLL
    else:
        dims=_NATIVE_SD_DIMS; offsets=_NATIVE_SD_MIP_OFFSETS; pitches=_NATIVE_SD_MIP_PITCHES
        wrap_x=_NATIVE_SD_WRAP_X_BLOCKS; wrap_y=_NATIVE_SD_WRAP_Y_BLOCKS
        phys=_NATIVE_SD_PHYS_BLOCKS; large_max=4; rolls=_NATIVE_SD_LARGE_ROLL
    if level<0 or level>=len(dims):
        raise ValueError('mip level out of range')
    w,h=dims[level]
    bw=max(1,(w+3)//4); bh=max(1,(h+3)//4)
    payload=bytearray()
    base=RAW_OFFSET+offsets[level]; pitch=pitches[level]
    for sy in range(bh):
        for sx in range(bw):
            if level<=large_max:
                dx,dy=sx,sy
            else:
                dx=(sx+wrap_x)%phys; dy=(sy+wrap_y)%phys
            pos=base+dy*pitch+dx*8
            end=pos+8
            if pos<0 or end>len(wrapper):
                raise ValueError(f'mip L{level} block ({sx},{sy}) is outside wrapper')
            payload += wrapper[pos:end]
    img=Image.fromarray(dxt1_decode(bytes(payload),w,h)).convert('RGB')
    if logical and level<=large_max:
        roll=int(rolls[level])
        if roll:
            img=Image.fromarray(np.roll(np.asarray(img),-roll,axis=1).astype(np.uint8),'RGB')
    return img


def _zip_write_image(zf, arcname, image):
    buf=io.BytesIO(); image.save(buf,'PNG'); zf.writestr(arcname,buf.getvalue())


def _paint_forensics_job(reg, slot, kind, game_folder=None):
    """Capture the exact resource that is physically installed in the game.

    A pristine source is optional. This intentionally remains useful even when
    old backup CDFs are stale, missing, or from a different archive revision.
    """
    hd=(kind=='hd')
    entry_name=slot.get('hd') if hd else slot.get('name')
    preferred=str((slot.get('hd_arc') if hd else slot.get('sd_arc')) or slot.get('arc'))
    live_ref,live_matches=_find_live_resource(reg,entry_name,preferred)
    live_bytes=_read_exact_region(live_ref['ar'],live_ref['offset'],live_ref['size'],f'live {entry_name}')
    pristine_ref,searched,wrong=_find_pristine_resource(reg,entry_name,live_ref['size'],game_folder)
    pristine_bytes=None
    if pristine_ref is not None:
        pristine_bytes=_read_exact_region(pristine_ref['ar'],pristine_ref['offset'],pristine_ref['size'],f'pristine {entry_name}')
    return dict(kind=kind,hd=hd,arcid=live_ref['arcid'],entry=entry_name,
                live_off=live_ref['offset'],size=live_ref['size'],live_bytes=live_bytes,
                live_archive=live_ref['ar'],live_cdf=live_ref['cdf'],live_matches=live_matches,
                pristine_ref=pristine_ref,pristine_bytes=pristine_bytes,
                pristine_search=searched,pristine_wrong_sizes=wrong,
                pristine_status=('found' if pristine_ref else 'not found; live capture still complete'))

def _backup_contains_named_entry(reg, arcid, name, offset, size):
    a=need(reg,arcid)
    archive_backup=a['bak']
    cdf_backup=backup_path(a['cdf'])
    if not (name and size and os.path.exists(archive_backup) and os.path.exists(cdf_backup)):
        return False
    try:
        match=next((x for x in parse_cdfiles(cdf_backup) if x[2]==name),None)
        if not match:
            return False
        off,entry_size,_name=match
        if int(off)!=int(offset) or int(entry_size)!=int(size):
            return False
        return os.path.getsize(archive_backup) >= int(off)+int(entry_size)
    except Exception:
        return False


def _backup_contains_slot_entry(reg, slot):
    """A pristine archive is usable only when its matching pristine CDF owns
    the same entry at the same offset/size. Added slots exist only in the live
    archive, even though a perfectly valid older archive backup also exists.
    """
    return _backup_contains_named_entry(
        reg,slot['arc'],slot['name'],slot['sd_off'],slot['sd_size'])


def _slot_sd_source(reg, slot, prefer_pristine=True):
    a=need(reg,slot['arc'])
    if prefer_pristine and _backup_contains_slot_entry(reg,slot):
        return a['bak'],'pristine backup'
    return a['ar'],'live added/current slot'


def _read_slot_mip0(reg,slot,prefer_pristine=True):
    need_bytes=(2048//4)*(1024//4)*8
    src,kind=_slot_sd_source(reg,slot,prefer_pristine=prefer_pristine)
    start=int(slot['sd_off'])+RAW_OFFSET
    if os.path.getsize(src) < start+need_bytes:
        raise ValueError(f'{kind} does not contain a complete mip-0 payload for {slot["name"]}')
    with open(src,'rb') as fh:
        fh.seek(start);payload=fh.read(need_bytes)
    if len(payload)!=need_bytes:
        raise ValueError(f'short read for {slot["name"]}: {len(payload)} of {need_bytes} bytes')
    return payload,kind


def _preview_placeholder(text='Preview unavailable'):
    img=Image.new('RGB',(192,96),(18,22,28))
    draw=ImageDraw.Draw(img)
    draw.rectangle((0,0,191,95),outline=(56,66,80))
    draw.text((12,40),str(text)[:28],fill=(170,180,192))
    buf=io.BytesIO();img.save(buf,'JPEG',quality=82);buf.seek(0)
    return buf


@app.route('/api/template/<name>')
def template(name):
    try:
        g,reg=registry(); s=_slot(name)
        if not s: return ('not found',404)
        payload,source_kind=_read_slot_mip0(reg,s,prefer_pristine=True)
        img=Image.fromarray(dxt1_decode(payload,2048,1024))
        buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
        response=send_file(buf,mimetype='image/png',download_name=name.replace('.ARC','_template.png'))
        response.headers['X-N15-Paint-Source']=source_kind
        return response
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),404


@app.route('/api/thumb/<name>')
def thumb(name):
    try:
        png=os.path.join(SCHEMES,name+'.png')
        if os.path.exists(png):
            st=os.stat(png);sig=('saved',name,st.st_size,st.st_mtime_ns)
        else:
            g,reg=registry();sig=('game',name,_paint_preview_signature(reg,('2',)))
        cached=_PAINT_ATLAS_PREVIEW_CACHE.get(sig)
        if cached is None:
            if os.path.exists(png):
                with Image.open(png) as opened:img=opened.convert('RGB');img.load()
                source_kind='saved app paint'
            else:
                s=_slot(name)
                if not s:return ('not found',404)
                payload,source_kind=_read_slot_mip0(reg,s,prefer_pristine=True)
                img=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
            img.thumbnail((192,96));buf=io.BytesIO();img.save(buf,'JPEG',quality=80,optimize=True)
            cached=(buf.getvalue(),source_kind)
            if len(_PAINT_ATLAS_PREVIEW_CACHE)>256:_PAINT_ATLAS_PREVIEW_CACHE.clear()
            _PAINT_ATLAS_PREVIEW_CACHE[sig]=cached
        data,source_kind=cached
        response=send_file(io.BytesIO(data),mimetype='image/jpeg',conditional=True,max_age=300)
        response.headers['X-N15-Paint-Source']=source_kind
        return response
    except Exception as ex:
        response=send_file(_preview_placeholder(),mimetype='image/jpeg',max_age=60)
        response.headers['X-N15-Preview-Error']=str(ex)[:240]
        return response


def auto_diff_layer(reg, slot, img):
    """Generate the touched-mask layer by diffing an import against a real base.

    Stock slots use the pristine entry. Appended slots correctly fall back to
    their live wrapper because they did not exist when the archive backup was made.
    """
    payload,_source_kind=_read_slot_mip0(reg,slot,prefer_pristine=True)
    stock=dxt1_decode(payload,2048,1024).astype(np.int32)
    up=np.asarray(img.convert('RGB')).astype(np.int32)
    diff=np.abs(up-stock).sum(2)
    mask=(diff>36).astype(np.uint8)*255      # tolerance ~12/channel
    la=np.dstack([up.astype(np.uint8), mask[:,:,None]])
    return Image.fromarray(la,'RGBA')

# ---- v0.9.26.10 paint-scheme Smart Import ----
SCHEME_TARGET_SIZE=(2048,1024)
SCHEME_SMART_QUALITIES={'auto','direct','1','2','4'}
SCHEME_LAYOUT_MODES={'auto','native','community_legacy'}
# Common hardware regions shared by stock and community Cup templates. These
# deliberately avoid most large sponsor/body-color areas and are used only to
# estimate a whole-atlas horizontal storage-layout offset.
_SCHEME_LAYOUT_RECTS=(
    (0,0,250,180),       # left headlight / fascia hardware
    (1000,0,1300,200),   # fuel circle / manufacturer hardware
    (1150,250,1510,570), # front grille and headlights
    (780,180,1120,650),  # rear lights / center hardware
    (1500,0,2048,1024),  # narrow utility / flame / light strips
    (0,600,900,1024),    # lower-left fixed parts and contingency region
)


def _scheme_edge_map(img):
    arr=np.asarray(img.convert('RGB')).astype(np.float32)
    gray=0.299*arr[:,:,0]+0.587*arr[:,:,1]+0.114*arr[:,:,2]
    gx=np.zeros_like(gray);gy=np.zeros_like(gray)
    gx[:,1:-1]=gray[:,2:]-gray[:,:-2]
    gy[1:-1,:]=gray[2:,:]-gray[:-2,:]
    return np.sqrt(gx*gx+gy*gy)


def _scheme_layout_mask(width,height):
    mask=np.zeros((height,width),dtype=bool)
    sx=width/2048.0;sy=height/1024.0
    for x1,y1,x2,y2 in _SCHEME_LAYOUT_RECTS:
        xa=max(0,min(width,int(round(x1*sx))))
        xb=max(0,min(width,int(round(x2*sx))))
        ya=max(0,min(height,int(round(y1*sy))))
        yb=max(0,min(height,int(round(y2*sy))))
        if xb>xa and yb>ya:mask[ya:yb,xa:xb]=True
    return mask


def _scheme_edge_overlap(source_edges,reference_edges,mask,shift,threshold=25.0):
    src=np.roll(source_edges,int(shift),axis=1)>threshold
    ref=reference_edges>threshold
    a=src[mask];b=ref[mask]
    den=float(np.sqrt(float(a.sum())*float(b.sum())))
    if den<=0.0:return 0.0
    return float(np.logical_and(a,b).sum())/den


def _detect_scheme_layout_shift(source,reference):
    """Estimate the circular X translation needed to match the native atlas.

    The community template supplied with the RC4 report was measured at +20 px
    relative to the app-exported native atlas. Rather than hard-coding that result
    for every image, Auto compares fixed vehicle hardware against the target
    slot's pristine mip-0 and applies a shift only when the evidence is strong.
    """
    box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
    small_size=(1024,512)
    src_small=source.convert('RGB').resize(small_size,box)
    ref_small=reference.convert('RGB').resize(small_size,box)
    se=_scheme_edge_map(src_small);re=_scheme_edge_map(ref_small)
    mask=_scheme_layout_mask(*small_size)
    coarse=[]
    for shift in range(-32,33):
        coarse.append((shift,_scheme_edge_overlap(se,re,mask,shift)))
    coarse_best=max(coarse,key=lambda x:x[1])
    candidate=int(coarse_best[0]*2)

    # Refine at native 2048x1024 resolution around the coarse candidate.
    src_full=source.convert('RGB')
    ref_full=reference.convert('RGB')
    if src_full.size!=(2048,1024):src_full=src_full.resize((2048,1024),box)
    if ref_full.size!=(2048,1024):ref_full=ref_full.resize((2048,1024),box)
    sfe=_scheme_edge_map(src_full);rfe=_scheme_edge_map(ref_full)
    full_mask=_scheme_layout_mask(2048,1024)
    scores=[]
    for shift in range(max(-64,candidate-4),min(64,candidate+4)+1):
        scores.append((shift,_scheme_edge_overlap(sfe,rfe,full_mask,shift)))
    best_shift,best_score=max(scores,key=lambda x:x[1])
    zero_score=_scheme_edge_overlap(sfe,rfe,full_mask,0)
    gain=best_score-zero_score
    relative=(gain/max(zero_score,0.01))
    accepted=(abs(best_shift)>=2 and best_score>=0.12 and gain>=0.03 and relative>=0.12)
    return dict(
        best_shift=int(best_shift),best_score=round(float(best_score),6),
        zero_score=round(float(zero_score),6),gain=round(float(gain),6),
        relative_gain=round(float(relative),6),accepted=bool(accepted),
        method='native hardware edge overlap / circular X search')


def _apply_scheme_layout(img,mode='auto',reference=None):
    requested=str(mode or 'auto').strip().lower()
    if requested not in SCHEME_LAYOUT_MODES:requested='auto'
    shift=0;detection=None;applied='native'
    if requested=='community_legacy':
        # Measured RC4 report: community atlas landmarks were 20 pixels to the
        # right of the app-exported native layout, so move the source left.
        shift=-20;applied='community_legacy'
    elif requested=='auto' and reference is not None:
        detection=_detect_scheme_layout_shift(img,reference)
        if detection.get('accepted'):
            shift=int(detection['best_shift'])
            applied='auto_aligned'
        else:
            applied='auto_native'
    elif requested=='auto':
        applied='auto_no_reference'
    arr=np.asarray(img.convert('RGB'))
    if shift:arr=np.roll(arr,shift,axis=1)
    out=Image.fromarray(arr.astype(np.uint8),'RGB')
    if shift:
        note=(f'Applied a {shift:+d}-pixel circular X alignment before mip generation. '
              'All SD/HD mip levels are generated from this corrected native atlas.')
    elif requested=='auto' and detection is not None:
        note='Auto layout check found no strong non-native atlas offset; source kept unchanged.'
    elif requested=='native':
        note='Native/app-exported layout selected; source kept unchanged.'
    else:
        note='No pristine reference was available, so Auto kept the source unchanged.'
    return out,dict(requested=requested,applied=applied,x_shift_pixels=int(shift),
                    reference='target slot pristine mip-0' if reference is not None else None,
                    detection=detection,note=note)


def _prepare_scheme_smart_image(stream,quality='auto'):
    """Prepare a livery atlas with an optional supersampled resize pass.

    Paint atlases must remain an exact 2:1 UV canvas, so this never crops or pads.
    Auto uses a direct Lanczos downsample for an already-oversized source and a
    2x intermediate render for target-size/smaller art. 2x/4x deliberately render
    to a larger intermediate canvas and then downsample to 2048x1024, which can
    smooth externally drawn text, numbers, and decal edges. It does not invent
    real detail; a genuinely high-resolution source is still the best input.
    """
    img=Image.open(stream)
    source_format=(img.format or 'unknown').upper();img.load()
    source_mode=str(img.mode);source_alpha=('A' in source_mode) or ('transparency' in img.info)
    src=img.convert('RGB');sw,sh=src.size;tw,th=SCHEME_TARGET_SIZE
    q=str(quality or 'auto').lower().strip()
    if q not in SCHEME_SMART_QUALITIES:q='auto'
    if q in ('direct','1'):
        factor=1;policy='direct Lanczos resize to the native 2048x1024 atlas'
    elif q in ('2','4'):
        factor=int(q);policy=f'{factor}x supersample, then Lanczos downsample to the native atlas'
    else:
        if sw>tw or sh>th:
            factor=1;policy='Auto: source is already oversized, so it is downsampled directly'
        else:
            factor=2;policy='Auto: 2x intermediate supersample, then downsample to native size'
    lanczos=Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS
    intermediate=None
    if factor>1:
        intermediate=(tw*factor,th*factor)
        stage=src if src.size==intermediate else src.resize(intermediate,lanczos)
        out=stage.resize((tw,th),lanczos)
    else:
        out=src if src.size==(tw,th) else src.resize((tw,th),lanczos)
    source_aspect=sw/max(1,sh);target_aspect=tw/th
    aspect_warning=(abs(source_aspect-target_aspect)>0.002)
    prep=dict(resized=(src.size!=(tw,th) or factor>1),source=[sw,sh],target=[tw,th],
              source_format=source_format,source_mode=source_mode,source_alpha=bool(source_alpha),
              preserve_alpha=False,mode='stretch (fixed UV atlas)',quality_requested=q,
              quality_policy=policy,supersample_factor=factor,
              intermediate=(list(intermediate) if intermediate else None),
              aspect_warning=aspect_warning,
              alpha_action=('flattened to opaque RGB' if source_alpha else 'not present'))
    return out.convert('RGB'),prep


def _scheme_preview_png(img):
    preview=img.copy();preview.thumbnail((720,360))
    b=io.BytesIO();preview.save(b,'PNG');return _b64.b64encode(b.getvalue()).decode()


# Stable stock references for each current body family.  Smart Import normally
# compares the source against the selected slot's pristine mip-0 to detect the
# community-template X offset.  That reference becomes invalid after a team is
# changed to another manufacturer (for example Chevrolet -> Ford), because the
# selected slot's pristine art still belongs to the old body.  Use an untouched
# stock paint authored for the CURRENT manufacturer instead of disabling Auto.
_MANUFACTURER_LAYOUT_REFERENCE_SLOTS = {
    1015: (
        'LIVERY_15_2_BRAD_KESELOWSKI_PRIMARY.ARC',
        'LIVERY_15_22_JOEY_LOGANO_PRIMARY.ARC',
        'LIVERY_15_43_ARIC_ALMIROLA_PRIMARY.ARC',
    ),
    1076: (
        'LIVERY_15_4_KEVIN_HARVICK_PRIMARY.ARC',
        'LIVERY_15_1_JAMIE_MCMURRAY_PRIMARY.ARC',
        'LIVERY_15_88_DALE_EARNHARDT_JR_PRIMARY.ARC',
    ),
    1078: (
        'LIVERY_15_18_KYLE_BUSCH_PRIMARY.ARC',
        'LIVERY_15_11_DENNY_HAMLIN_PRIMARY.ARC',
        'LIVERY_15_20_MATT_KENSETH_SECONDARY.ARC',
    ),
}


def _manufacturer_layout_reference(reg, manufacturer_uid, exclude_slot=None, game_folder=None):
    """Return a logical 2048x1024 stock reference for the live body family.

    Prefer a verified clean/paired-backup wrapper.  If no pristine source is
    available yet, use a different live stock slot from that manufacturer; the
    reference is read-only and is used only for horizontal layout detection.
    """
    try:
        manufacturer_uid=int(manufacturer_uid)
    except Exception:
        return None, None
    slots={str(x.get('name') or '').upper():x for x in grid_slots()}
    exclude=str(exclude_slot or '').upper()
    need_bytes=(2048//4)*(1024//4)*8
    errors=[]
    for candidate_name in _MANUFACTURER_LAYOUT_REFERENCE_SLOTS.get(manufacturer_uid, ()):
        if candidate_name.upper()==exclude:
            continue
        slot=slots.get(candidate_name.upper())
        if not slot:
            errors.append(candidate_name+': not indexed')
            continue
        try:
            ref,searched,wrong=_find_pristine_resource(
                reg,slot['name'],expected_size=int(slot['sd_size']),game_folder=game_folder)
            if ref is not None:
                wrapper=_read_exact_region(ref['ar'],ref['offset'],ref['size'],
                                           f'manufacturer reference {slot["name"]}')
                if len(wrapper)<RAW_OFFSET+need_bytes:
                    raise ValueError('wrapper does not contain a complete mip-0')
                payload=wrapper[RAW_OFFSET:RAW_OFFSET+need_bytes]
                source_kind=str(ref.get('label') or 'verified pristine source')
            else:
                payload,source_kind=_read_slot_mip0(reg,slot,prefer_pristine=True)
            image=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
            return image,dict(
                manufacturer_uid=manufacturer_uid,
                manufacturer=TEAM_MANUFACTURER_NAMES.get(manufacturer_uid,str(manufacturer_uid)),
                slot=slot['name'],source=source_kind,
                fallback_live=not str(source_kind).lower().startswith(('clean baseline','legacy pristine','modding-app pristine','pristine backup')),
            )
        except Exception as ex:
            errors.append(candidate_name+': '+str(ex))
    return None,dict(
        manufacturer_uid=manufacturer_uid,
        manufacturer=TEAM_MANUFACTURER_NAMES.get(manufacturer_uid,str(manufacturer_uid)),
        unavailable=True,errors=errors[:6],
    )


@app.route('/api/scheme_smart/<name>',methods=['POST'])
def scheme_smart_import(name):
    """Preview or apply a quality-controlled external paint-scheme import."""
    try:
        if _is_managed_extra_slot(name):
            raise ValueError(_managed_extra_action_error(name))
        f=request.files.get('file')
        if not f:raise ValueError('choose an image file')
        img,prep=_prepare_scheme_smart_image(f.stream,request.form.get('quality','auto'))
        s=_slot(name)
        layout_mode=request.form.get('layout_mode','auto')
        reference=None;reference_info=None
        manufacturer_context=_slot_manufacturer_context(name) if s else {'known':False,'mismatch':False}
        if s:
            try:
                _g,layout_reg=registry()
                if manufacturer_context.get('mismatch') and str(layout_mode or 'auto').lower()=='auto':
                    reference,reference_info=_manufacturer_layout_reference(
                        layout_reg,manufacturer_context.get('current_manufacturer_uid'),
                        exclude_slot=name,game_folder=_g)
                elif not manufacturer_context.get('mismatch'):
                    payload,_source_kind=_read_slot_mip0(layout_reg,s,prefer_pristine=True)
                    reference=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
                    reference_info=dict(slot=s.get('name'),source=_source_kind,
                                        manufacturer='selected slot original body')
            except Exception as ex:
                reference=None
                reference_info=dict(unavailable=True,error=str(ex))
        img,layout_info=_apply_scheme_layout(img,layout_mode,reference)
        if reference_info:
            layout_info['reference_detail']=reference_info
        if manufacturer_context.get('mismatch'):
            layout_info['manufacturer_mismatch']=True
            if reference is not None:
                layout_info['note']=(
                    f"The team now uses {manufacturer_context.get('current_manufacturer') or 'a different manufacturer'}. "
                    f"Auto alignment compared the import with current-body stock reference "
                    f"{reference_info.get('slot') if reference_info else 'unknown'} instead of the old body. "
                    + str(layout_info.get('note') or ''))
            else:
                layout_info['note']=(
                    'The team manufacturer changed after this stock paint was authored, and no '
                    'current-body stock reference was available. Auto kept the source unchanged; '
                    'choose Community legacy manually only when the source uses that older template layout. '
                    + str(layout_info.get('note') or ''))
        prep['manufacturer_context']=manufacturer_context
        prep['layout']=layout_info
        prep['layout_mode']=layout_info.get('applied')
        prep['layout_shift_x']=layout_info.get('x_shift_pixels',0)
        if request.form.get('dry_run')=='1':
            warning=('The source is not 2:1, so Smart Import must stretch it to the fixed car UV atlas. '
                     if prep.get('aspect_warning') else '')
            warning+=('Supersampling improves edge filtering but cannot create genuine source detail. '
                      'The saved atlas remains exactly 2048x1024 and the native SD/HD mip installer is unchanged. ')
            warning+=layout_info.get('note','')
            return jsonify(dict(ok=True,dry_run=True,preview_png=_scheme_preview_png(img),image_prep=prep,
                profile=dict(width=2048,height=1024,codec='DXT1',profile='car paint atlas / native SD+HD mip pipeline',
                             payload_size='fixed livery wrappers'),experimental=False,warning=warning))
        png=os.path.join(SCHEMES,name+'.png');lay=os.path.join(SCHEMES,name+'.layer.png')
        img.save(png)
        # An externally supplied 2:1 paint is a complete atlas replacement. The
        # old auto-diff mask could leave donor blocks behind and made SD/HD or
        # adjacent mip levels disagree. Paint Booth edits may still provide an
        # explicit layer through /api/scheme.
        if os.path.exists(lay): os.remove(lay)
        prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
        wrote=None
        install=request.form.get('install')=='1'
        if install:
            if not s:raise ValueError('paint slot was not found in the installed game')
            wrote=install_slot(registry()[1],s,png,None)
        return jsonify(dict(ok=True,installed=bool(wrote),wrote=wrote,image_prep=prep))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scheme/<name>', methods=['POST','GET'])
def scheme(name):
    png=os.path.join(SCHEMES,name+'.png'); lay=os.path.join(SCHEMES,name+'.layer.png')
    if request.method=='GET':
        if os.path.exists(png):
            return send_file(png,mimetype='image/png',
                             download_name=name.replace('.ARC','_scheme.png'))
        return ('none',404)
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(2048,1024),'stretch',preserve_alpha=False)
    img=img.convert('RGB'); img.save(png)
    lf=request.files.get('layer')
    if lf:
        la=Image.open(lf.stream)
        la,_layer_prep=prepare_import_image(la,(2048,1024),'stretch',preserve_alpha=True)
        la.convert('RGBA').save(lay)
    else:
        # Imported from outside the booth: replace the complete atlas. A stale
        # auto-diff layer from v1/RC2 must not survive and reintroduce donor blocks.
        if os.path.exists(lay): os.remove(lay)
        prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
    return jsonify(dict(ok=True, image_prep=prep))

@app.route('/api/layer/<name>')
def layer(name):
    lay=os.path.join(SCHEMES,name+'.layer.png')
    if os.path.exists(lay):
        return send_file(lay,mimetype='image/png',
                         download_name=name.replace('.ARC','_layer.png'))
    return ('none',404)

_LIVE_PAINT_THUMB_CACHE={}
_PAINT_ATLAS_PREVIEW_CACHE={}
_STOCK_THUMB_SUPPORT_CACHE={}
_LIVE_LIVERY_INDEX_CACHE={'sig':None,'script_to_uid':{},'uid_to_driver':{}}
_LIVE_THUMB_LOCATION_CACHE={'sig':None,'by_uid':{},'errors':[]}


def _paint_preview_signature(reg,groups=('0','1','2')):
    parts=[]
    for gid in groups:
        info=reg.get(str(gid)) or {}
        for key in ('ar','cdf'):
            path=info.get(key)
            try:
                st=os.stat(path);parts.append((str(gid),key,st.st_size,st.st_mtime_ns))
            except Exception:parts.append((str(gid),key,0,0))
    try:
        st=os.stat(EXTRA_SCHEME_STATE);parts.append(('state','json',st.st_size,st.st_mtime_ns))
    except Exception:parts.append(('state','json',0,0))
    return tuple(parts)


def _live_livery_index(game,reg):
    sig=_paint_preview_signature(reg,('0','1'))
    cache=_LIVE_LIVERY_INDEX_CACHE
    if cache.get('sig')==sig:return cache
    catalog=extra_scheme_mod().catalog(game,EXTRA_SCHEME_STATE)
    script_to_uid={};uid_to_driver={}
    for driver in catalog.get('drivers',[]):
        try:driver_uid=int(driver.get('uid'))
        except Exception:continue
        for scheme in driver.get('schemes',[]):
            try:uid=int(scheme.get('uid'))
            except Exception:continue
            script=str(scheme.get('script_name') or '').casefold()
            if script:script_to_uid[script]=uid
            uid_to_driver[uid]=driver_uid
    cache.update(sig=sig,script_to_uid=script_to_uid,uid_to_driver=uid_to_driver)
    return cache


def _decode_thumbnail_from_raw(raw, uid):
    """Decode PAINTSCHEME_<uid> from one already-read team container."""
    entries,_=C.parse_multi_arc(raw)
    name=f'PAINTSCHEME_{int(uid)}'
    entry=next((e for e in entries if str(e.get('name'))==name),None)
    if entry is None:
        raise ValueError(f'{name} was not found in this team container')
    if str(entry.get('fmt'))!='DXT5' or int(entry.get('w',0))!=256 or int(entry.get('h',0))!=256:
        raise ValueError(f'{name} is not a readable 256x256 DXT5 thumbnail')
    return C.multi_read_png(raw,entry).convert('RGBA')


def _decode_thumbnail_from_container(game, uid, container_name):
    """Read one exact live PAINTSCHEME resource without applying write guards."""
    tm=extra_thumbnail_mod()
    hit=tm.find_target(game,int(uid),target_container_name=container_name)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {container_name}')
    return _decode_thumbnail_from_raw(hit[2],uid)


def _live_thumbnail_locations(reg):
    """Index every live team-bank copy of every PAINTSCHEME resource once.

    NASCAR 15 can retain the same livery thumbnail in more than one team bank
    after driver moves. The public preview must inspect those live copies rather
    than trusting app-side state or whichever CDF row happens to appear first.
    """
    sig=_paint_preview_signature(reg,('1',))
    cache=_LIVE_THUMB_LOCATION_CACHE
    if cache.get('sig')==sig:
        return cache
    info=need(reg,'1');by_uid=collections.defaultdict(list);errors=[]
    rows=parse_cdfiles(info['cdf'])
    with open(info['ar'],'rb') as fh:
        for off,size,name in rows:
            if not str(name).upper().startswith('2DRIVERSELECTTD_'):
                continue
            try:
                fh.seek(int(off));raw=fh.read(int(size))
                if len(raw)!=int(size):
                    raise ValueError(f'short read ({len(raw)} of {int(size)} bytes)')
                entries,_=C.parse_multi_arc(raw)
                for entry in entries:
                    m=re.fullmatch(r'PAINTSCHEME_(\d+)',str(entry.get('name') or ''),re.I)
                    if not m:
                        continue
                    if str(entry.get('fmt'))!='DXT5' or int(entry.get('w',0))!=256 or int(entry.get('h',0))!=256:
                        continue
                    by_uid[int(m.group(1))].append({
                        'container':str(name),'offset':int(off),'size':int(size),
                    })
            except Exception as ex:
                errors.append(f'{name}: {ex}')
    cache.update(sig=sig,by_uid={int(k):list(v) for k,v in by_uid.items()},errors=errors)
    return cache


def _read_indexed_thumbnail(archive_path, row, uid):
    with open(archive_path,'rb') as fh:
        fh.seek(int(row['offset']));raw=fh.read(int(row['size']))
    if len(raw)!=int(row['size']):
        raise ValueError(f"short read for {row['container']}")
    return _decode_thumbnail_from_raw(raw,uid)


def _pristine_thumbnail_hash(reg, container_name, uid, cache):
    """Return the original backup thumbnail hash for one named team bank."""
    key=(str(container_name).casefold(),int(uid))
    if key in cache:
        return cache[key]
    info=need(reg,'1');bak_ar=backup_path(info['ar']);bak_cdf=backup_path(info['cdf'])
    if not (os.path.exists(bak_ar) and os.path.exists(bak_cdf)):
        cache[key]=None;return None
    try:
        row=next(({'container':n,'offset':int(o),'size':int(s)}
                  for o,s,n in parse_cdfiles(bak_cdf)
                  if str(n).casefold()==str(container_name).casefold()),None)
        if not row:
            cache[key]=None;return None
        image=_read_indexed_thumbnail(bak_ar,row,uid)
        value=hashlib.sha256(image.tobytes()).hexdigest()
    except Exception:
        value=None
    cache[key]=value
    return value


def _live_livery_thumbnail(uid):
    """Return the exact live thumbnail targeted by the proven replacement writer.

    The game-facing replace route uses extra_thumbnail_mod().find_target(game, uid)
    without forcing a team bank. In-game testing proves that changing this exact
    target changes Paint Select. The preview must therefore decode that same target
    first. Ranking duplicate team-bank copies can choose an older stock copy even
    while the game is displaying the writer target.

    Only when the exact writer target cannot be decoded do we fall back to scanning
    every readable duplicate live copy. App-side PNGs remain HTTP-route fallback only.
    """
    game,reg=_extra_game_and_registry();uid=int(uid)
    index=_live_livery_index(game,reg)
    if uid not in index['uid_to_driver']:
        raise ValueError(f'livery UID {uid} is not in the live paint catalog')
    sig=(_paint_preview_signature(reg,('1',)),uid)
    cached=_LIVE_PAINT_THUMB_CACHE.get(sig)
    if cached is not None:
        return Image.open(io.BytesIO(cached)).convert('RGBA')

    errors=[]
    # First choice: the exact resource selected by the in-game-proven writer.
    try:
        writer_hit=extra_thumbnail_mod().find_target(game,uid)
        if writer_hit:
            image=_decode_thumbnail_from_raw(writer_hit[2],uid)
            buf=io.BytesIO();image.save(buf,format='PNG',optimize=False)
            if len(_LIVE_PAINT_THUMB_CACHE)>256:_LIVE_PAINT_THUMB_CACHE.clear()
            _LIVE_PAINT_THUMB_CACHE[sig]=buf.getvalue()
            return image
        errors.append('the proven thumbnail target was not found')
    except Exception as ex:
        errors.append(f'proven target: {ex}')

    # Fallback: inspect every live duplicate and use the newest readable CDF row.
    # This is recovery-only; it must never outrank the same target used by Replace.
    locations=_live_thumbnail_locations(reg)
    rows=list(locations.get('by_uid',{}).get(uid,[]))
    archive_path=need(reg,'1')['ar'];candidates=[]
    for row in rows:
        try:
            image=_read_indexed_thumbnail(archive_path,row,uid)
            candidates.append((int(row.get('offset',0)),image))
        except Exception as ex:
            errors.append(f"{row.get('container')}: {ex}")
    if not candidates:
        detail='; '.join(errors or locations.get('errors',[])[:8])
        raise ValueError(detail or f'no live Paint Select thumbnail was found for livery UID {uid}')
    _off,image=max(candidates,key=lambda x:x[0])
    buf=io.BytesIO();image.save(buf,format='PNG',optimize=False)
    if len(_LIVE_PAINT_THUMB_CACHE)>256:_LIVE_PAINT_THUMB_CACHE.clear()
    _LIVE_PAINT_THUMB_CACHE[sig]=buf.getvalue()
    return image


def _slot_livery_uid(name):
    script = str(name or '')
    if script.upper().startswith('LIVERY_') and script.upper().endswith('.ARC'):
        script = script[7:-4]
    try:
        game,reg=_extra_game_and_registry()
        return _live_livery_index(game,reg)['script_to_uid'].get(script.casefold())
    except Exception:
        pass
    return None


@app.route('/api/paint_thumbnail/<int:uid>')
def paint_thumbnail(uid):
    try:
        image = _live_livery_thumbnail(uid)
        out = io.BytesIO(); image.save(out, format='PNG'); out.seek(0)
        return send_file(out, mimetype='image/png', conditional=True, max_age=300)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),404



@app.route('/api/paint_previews/reload', methods=['POST'])
def paint_previews_reload():
    """Clear only read-only preview caches so failed rows can be retried."""
    _LIVE_PAINT_THUMB_CACHE.clear()
    _PAINT_ATLAS_PREVIEW_CACHE.clear()
    _STOCK_THUMB_SUPPORT_CACHE.clear()
    _LIVE_LIVERY_INDEX_CACHE.update(sig=None,script_to_uid={},uid_to_driver={})
    _LIVE_THUMB_LOCATION_CACHE.update(sig=None,by_uid={},errors=[])
    return jsonify(dict(ok=True,note='Paint preview caches cleared. No game files were changed.'))


def _stock_thumbnail_support(uid):
    """Use the dev26-proven live thumbnail lookup.

    Dev33 scoped every lookup to the driver's current team. That looked safer on
    paper, but in real game files it rejected or targeted the wrong native copy
    for normal stock liveries. The dev26 unscoped helper is the in-game-proven
    replacement path, so keep that behavior and separately replay the saved
    custom pixels when a driver is moved.
    """
    game,reg=_extra_game_and_registry();uid=int(uid)
    sig=(_paint_preview_signature(reg,('1',)),uid)
    cached=_STOCK_THUMB_SUPPORT_CACHE.get(sig)
    if cached is not None:return dict(cached)
    tm=extra_thumbnail_mod()
    try:
        info=tm.inspect_thumbnail_identity(game,uid)
        supported=bool(info.get('exists') and info.get('structural_valid') and info.get('same_bank_valid'))
        reason='' if supported else (info.get('structural_error') or info.get('identity_chain_error') or 'This Paint Select slot does not have a proven same-bank native thumbnail identity.')
        out=dict(ok=True,uid=uid,supported=supported,reason=reason,container=info.get('container'),details=info,
                 target_source='verified native lookup')
    except Exception as ex:
        out=dict(ok=True,uid=uid,supported=False,reason=str(ex),target_source='verified native lookup')
    if len(_STOCK_THUMB_SUPPORT_CACHE)>512:_STOCK_THUMB_SUPPORT_CACHE.clear()
    _STOCK_THUMB_SUPPORT_CACHE[sig]=dict(out)
    return out

@app.route('/api/paint_thumbnail_debug/<int:uid>')
def paint_thumbnail_debug(uid):
    """Read-only report showing every live copy considered for one thumbnail."""
    try:
        game,reg=_extra_game_and_registry();uid=int(uid)
        index=_live_livery_index(game,reg);driver_uid=index['uid_to_driver'].get(uid)
        live_link=_team_fast_driver_links().get(int(driver_uid)) if driver_uid is not None else None
        current_container=(f"2DRIVERSELECTTD_{int(live_link['team_uid'])}.ARC" if live_link else '')
        writer_hit=extra_thumbnail_mod().find_target(game,uid)
        writer_container=str(writer_hit[1].get('name') or '') if writer_hit else ''
        writer_offset=int(writer_hit[1].get('offset',0)) if writer_hit else None
        rows=list(_live_thumbnail_locations(reg).get('by_uid',{}).get(uid,[]))
        archive_path=need(reg,'1')['ar'];pristine_cache={};report=[]
        for row in rows:
            try:
                image=_read_indexed_thumbnail(archive_path,row,uid)
                live_hash=hashlib.sha256(image.tobytes()).hexdigest()
                pristine_hash=_pristine_thumbnail_hash(reg,row['container'],uid,pristine_cache)
                report.append(dict(container=row['container'],offset=int(row['offset']),size=int(row['size']),
                                   current_team=(str(row['container']).casefold()==current_container.casefold()),
                                   writer_target=(str(row['container']).casefold()==writer_container.casefold() and int(row['offset'])==int(writer_offset or -1)),
                                   modified_from_backup=bool(pristine_hash and live_hash!=pristine_hash),
                                   live_hash=live_hash,pristine_hash=pristine_hash))
            except Exception as ex:
                report.append(dict(container=row.get('container'),offset=row.get('offset'),error=str(ex)))
        return jsonify(dict(ok=True,uid=uid,driver_uid=driver_uid,current_container=current_container,
                            writer_container=writer_container,writer_offset=writer_offset,copies=report))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/stock_thumbnail_support/<int:uid>')
def stock_thumbnail_support(uid):
    try:return jsonify(_stock_thumbnail_support(uid))
    except Exception as ex:return jsonify(dict(ok=False,supported=False,error=str(ex))),400


def _stock_thumbnail_preview_path(uid):
    """Stable app-side preview for a replaced stock thumbnail, keyed by livery UID."""
    folder=os.path.join(SCHEMES,'thumbnail_overrides')
    os.makedirs(folder,exist_ok=True)
    return os.path.join(folder,f'{int(uid)}.png')


def _stock_thumbnail_override_record(uid, slot_name, source_container):
    """Remember the exact uploaded thumbnail so a later team move can replay it."""
    state=_team_state_load()
    overrides=state.setdefault('thumbnail_overrides',{})
    saved_name=''
    uid_preview=_stock_thumbnail_preview_path(uid)
    if os.path.exists(uid_preview):
        saved_name=os.path.relpath(uid_preview,SCHEMES).replace(os.sep,'/')
    elif slot_name:
        candidate=os.path.join(SCHEMES,str(slot_name)+'.thumb.png')
        if os.path.exists(candidate):saved_name=os.path.basename(candidate)
    overrides[str(int(uid))]={
        'slot':str(slot_name or ''),
        'saved_thumb':saved_name,
        'uid_preview':saved_name,
        'source_container':str(source_container or ''),
        'updated':int(time.time()),
    }
    _team_state_save(state)


@app.route('/api/stock_thumbnail/<int:uid>', methods=['POST'])
def stock_thumbnail_replace(uid):
    snapshot=None;temp_path=None
    with _EXTRA_CREATE_LOCK:
        try:
            if _extra_game_running():raise RuntimeError('NASCAR15.exe is running. Close the game before changing a Paint Select thumbnail')
            support=_stock_thumbnail_support(uid)
            if not support.get('supported'):raise ValueError(support.get('reason') or 'This thumbnail is not structurally supported')
            upload=request.files.get('file')
            if not upload:raise ValueError('choose an image first')
            prepared,prep=_extra_prepare_thumbnail_source(upload.read(),request.form.get('quality') or 'auto')
            fd,temp_path=tempfile.mkstemp(prefix='n15_stock_thumb_',suffix='.png');os.close(fd);prepared.save(temp_path,'PNG')
            game,reg=_extra_game_and_registry();tm=extra_thumbnail_mod();existing=tm.find_target(game,int(uid))
            if not existing:raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in the live game')
            _extra_backups(reg,('1',));snapshot=_extra_transaction_snapshot(reg,('1',),inplace_thumbnail=existing)
            _extra_persist_snapshot(snapshot,f'Replace in-game thumbnail for livery UID {int(uid)}',operation={'type':'stock_thumbnail','uid':int(uid)})
            report=tm.replace_existing_thumbnail(game,int(uid),temp_path,target_container_name=support.get('container'))
            if 'texconv' not in str(report.get('encoder') or '').lower():
                raise ValueError('the game-safe texconv DXT5 encoder was not used; nothing was kept')
            # Keep the exact installed pixels as the app preview and as the
            # source that follows this livery through future driver moves.
            slot_name=str(request.form.get('slot') or '').strip()
            # Persist the exact installed image under both the slot name and a
            # stable livery-UID key. The UID copy survives driver moves and avoids
            # relying on whichever duplicate team-bank copy the live preview
            # resolver happens to encounter first.
            uid_preview=_stock_thumbnail_preview_path(int(uid))
            prepared.save(uid_preview,'PNG')
            if slot_name:
                os.makedirs(SCHEMES,exist_ok=True)
                preview_path=os.path.join(SCHEMES,slot_name+'.thumb.png')
                prepared.save(preview_path,'PNG')
            _stock_thumbnail_override_record(int(uid),slot_name,report.get('container') or support.get('container'))
            _extra_seal_persisted_snapshot({'type':'stock_thumbnail','uid':int(uid),
                                             'container':report.get('container') or support.get('container')})
            _clear_ui_thumb_cache()
            return jsonify(dict(ok=True,uid=int(uid),container=report.get('container') or support.get('container'),
                                preview=report,preparation=prep,
                                note='Paint Select thumbnail replaced and verified. The installed image was also saved so it can follow the driver during a later team move.'))
        except Exception as ex:
            errors=_extra_transaction_restore(snapshot) if snapshot else []
            detail=str(ex)
            if errors:detail+=' | Rollback warnings: '+'; '.join(errors)
            elif snapshot:_extra_clear_persisted_snapshot()
            return jsonify(dict(ok=False,error=detail,rolled_back=bool(snapshot and not errors))),400
        finally:
            if temp_path and os.path.exists(temp_path):
                try:os.remove(temp_path)
                except OSError:pass

@app.route('/api/slotthumb/<name>', methods=['GET','POST'])
def slotthumb(name):
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    if request.method == 'GET':
        uid = _slot_livery_uid(name)
        # Live game files are the source of truth. This makes a fresh app folder
        # immediately reflect thumbnails installed by any older app version.
        if uid is not None:
            try:
                image=_live_livery_thumbnail(uid)
                out=io.BytesIO();image.save(out,format='PNG');out.seek(0)
                response=send_file(out,mimetype='image/png',conditional=False,max_age=0)
                response.headers['Cache-Control']='no-store, max-age=0'
                response.headers['X-N15-Thumbnail-Source']='live game Paint Select thumbnail'
                return response
            except Exception:
                pass
            uid_path=_stock_thumbnail_preview_path(uid)
            if os.path.exists(uid_path):
                response=send_file(uid_path,mimetype='image/png',conditional=False,max_age=0)
                response.headers['Cache-Control']='no-store, max-age=0'
                response.headers['X-N15-Thumbnail-Source']='fallback saved thumbnail by livery UID'
                return response
        path=os.path.join(SCHEMES,name+'.thumb.png')
        if os.path.exists(path):
            response=send_file(path,mimetype='image/png',conditional=False,max_age=0)
            response.headers['Cache-Control']='no-store, max-age=0'
            response.headers['X-N15-Thumbnail-Source']='fallback app preview'
            return response
        return ('not found',404)
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(256,256),request_resize_mode('fit'),preserve_alpha=True)
    img.save(os.path.join(SCHEMES,name+'.thumb.png'))
    return jsonify(dict(ok=True, image_prep=prep, note='Menu preview saved. Supported game thumbnail targets use it during install.'))

@app.route('/api/slotthumb_export/<name>')
def slotthumb_export(name):
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    uid = _slot_livery_uid(name)
    if uid is not None:
        try:
            image=_live_livery_thumbnail(uid)
            out=io.BytesIO();image.save(out,format='PNG');out.seek(0)
            return send_file(out,mimetype='image/png',as_attachment=True,
                             download_name=f"{name}_menu_thumbnail.png")
        except Exception:
            uid_path=_stock_thumbnail_preview_path(uid)
            if os.path.exists(uid_path):
                return send_file(uid_path,mimetype='image/png',as_attachment=True,
                                 download_name=f"{name}_menu_thumbnail.png")
    path=os.path.join(SCHEMES,name+'.thumb.png')
    if os.path.exists(path):
        return send_file(path,mimetype='image/png',as_attachment=True,
                         download_name=f"{name}_menu_thumbnail.png")
    return ('not found',404)

@app.route('/api/build', methods=['POST'])
def build():
    g,reg=registry(); names=request.json.get('names',[])
    slots={s['name']:s for s in grid_slots()}
    done=[]; errs=[]
    for n in names:
        png=os.path.join(SCHEMES,n+'.png'); lay=os.path.join(SCHEMES,n+'.layer.png')
        if _is_managed_extra_slot(n):
            errs.append(dict(name=n,error=_managed_extra_action_error(n)))
        elif n in slots and os.path.exists(png):
            try: done.append(dict(name=n, wrote=install_slot(reg,slots[n],png,lay)))
            except Exception as e: errs.append(dict(name=n,error=str(e)))
        else: errs.append(dict(name=n,error='no scheme saved'))
    return jsonify(dict(done=done,errors=errs))

@app.route('/api/restore_slot', methods=['POST'])
def restore_slot():
    try:
        g,reg=registry(); name=(request.json or {}).get('name')
        if _is_managed_extra_slot(name):
            return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
        slot=_slot(name)
        if not slot:
            return jsonify(dict(ok=False,error='slot not found')),404
        specs=[dict(kind='SD',preferred=str(slot.get('sd_arc') or slot['arc']),entry=slot.get('name'))]
        if slot.get('hd') and slot.get('hd_size'):
            specs.append(dict(kind='HD',preferred=str(slot.get('hd_arc') or slot['arc']),entry=slot.get('hd')))
        jobs=[]
        for spec in specs:
            live_ref,live_matches=_find_live_resource(reg,spec['entry'],spec['preferred'])
            source_ref,searched,wrong=_find_pristine_resource(reg,spec['entry'],live_ref['size'],g)
            if source_ref is None:
                detail='; '.join(searched) if searched else 'no clean baseline or paired backup was detected'
                if wrong: detail += '; wrong-size matches: ' + ', '.join(wrong)
                raise ValueError(f'no verified stock source contains {spec["entry"]} ({live_ref["size"]} bytes). Searched: {detail}')
            pristine=_read_exact_region(source_ref['ar'],source_ref['offset'],source_ref['size'],f'stock {spec["entry"]}')
            old_live=_read_exact_region(live_ref['ar'],live_ref['offset'],live_ref['size'],f'live {spec["entry"]}')
            jobs.append(dict(**spec,live_ref=live_ref,source_ref=source_ref,size=live_ref['size'],
                             pristine=pristine,old_live=old_live,live_matches=live_matches))
        attempted=[]
        try:
            for job in jobs:
                attempted.append(job)
                with open(job['live_ref']['ar'],'r+b') as live:
                    live.seek(job['live_ref']['offset']);live.write(job['pristine']);live.flush();os.fsync(live.fileno())
                    live.seek(job['live_ref']['offset'])
                    if live.read(job['size'])!=job['pristine']:
                        raise ValueError(f'{job["kind"]} restore readback mismatch')
        except Exception as write_ex:
            rb=[]
            for job in reversed(attempted):
                try:
                    with open(job['live_ref']['ar'],'r+b') as live:
                        live.seek(job['live_ref']['offset']);live.write(job['old_live']);live.flush();os.fsync(live.fileno())
                        live.seek(job['live_ref']['offset'])
                        if live.read(job['size'])!=job['old_live']:
                            raise ValueError('rollback readback mismatch')
                except Exception as ex:
                    rb.append(f'ARCHIVE{job["live_ref"]["arcid"]} {job["kind"]}: {ex}')
            if rb:
                raise RuntimeError(f'{write_ex}; restore rollback also failed: ' + '; '.join(rb))
            raise
        for ext in ('.png','.layer.png','.layer.json','.thumb.png'):
            fp=os.path.join(SCHEMES,name+ext)
            if os.path.exists(fp): os.remove(fp)
        _clear_ui_thumb_cache()
        return jsonify(dict(ok=True,restored=[dict(kind=j['kind'],entry=j['entry'],
                    live_archive=j['live_ref']['arcid'],live_offset=j['live_ref']['offset'],size=j['size'],
                    source=j['source_ref']['label'],source_archive=j['source_ref']['arcid'],
                    source_offset=j['source_ref']['offset']) for j in jobs]))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/paint_forensics/<path:name>')
def paint_forensics(name):
    """Export the exact live/pristine paint wrappers and decoded mip evidence.

    This is intentionally opt-in because it contains the selected game resource
    bytes. It never changes the game and avoids packaging whole archives.
    """
    try:
        g,reg=registry(); slot=_slot(name)
        if not slot:
            return jsonify(dict(ok=False,error='paint slot not found')),404
        jobs=[_paint_forensics_job(reg,slot,'sd',g)]
        if slot.get('hd') and slot.get('hd_size'):
            jobs.append(_paint_forensics_job(reg,slot,'hd',g))
        manifest=dict(app_version=APP_VERSION,release=APP_RELEASE_LABEL,
                      generated=int(time.time()),game_folder=g,slot={k:v for k,v in slot.items() if k not in ('fei',)},
                      jobs=[])
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
            source_path=os.path.join(SCHEMES,name+'.png')
            if os.path.exists(source_path):
                zf.write(source_path,'source/imported_or_saved.png')
            comparison=[]
            source_img=None
            if os.path.exists(source_path):
                try:
                    source_img=Image.open(source_path).convert('RGB'); source_img.load()
                except Exception:
                    source_img=None
            for job in jobs:
                base=job['kind']
                zf.writestr(f'{base}/live_entry.bin',job['live_bytes'])
                if job.get('pristine_bytes') is not None:
                    zf.writestr(f'{base}/pristine_entry.bin',job['pristine_bytes'])
                meta={k:v for k,v in job.items() if not k.endswith('_bytes')}
                meta['live_sha256']=__import__('hashlib').sha256(job['live_bytes']).hexdigest()
                if job.get('pristine_bytes') is not None:
                    meta['pristine_sha256']=__import__('hashlib').sha256(job['pristine_bytes']).hexdigest()
                    meta['changed_bytes']=sum(a!=b for a,b in zip(job['live_bytes'],job['pristine_bytes']))
                else:
                    meta['pristine_sha256']=None
                    meta['changed_bytes']=None
                manifest['jobs'].append(meta)
                max_level=12 if job['hd'] else 11
                for level in range(max_level):
                    try:
                        live_stored=_native_wrapper_mip_image(job['live_bytes'],level,job['hd'],False)
                        live_logical=_native_wrapper_mip_image(job['live_bytes'],level,job['hd'],True)
                        stock_logical=(_native_wrapper_mip_image(job['pristine_bytes'],level,job['hd'],True) if job.get('pristine_bytes') is not None else None)
                        _zip_write_image(zf,f'{base}/mips/L{level:02d}_live_stored.png',live_stored)
                        _zip_write_image(zf,f'{base}/mips/L{level:02d}_live_logical.png',live_logical)
                        if stock_logical is not None:
                            _zip_write_image(zf,f'{base}/mips/L{level:02d}_pristine_logical.png',stock_logical)
                        row=dict(kind=base,level=level,width=live_logical.width,height=live_logical.height)
                        if source_img is not None:
                            box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
                            src=source_img.resize(live_logical.size,box)
                            la=np.asarray(live_logical).astype(np.int16); sa=np.asarray(src).astype(np.int16)
                            row['source_mean_abs_error']=round(float(np.abs(la-sa).mean()),6)
                            _zip_write_image(zf,f'{base}/mips/L{level:02d}_source_resized.png',src)
                        comparison.append(row)
                    except Exception as mip_ex:
                        comparison.append(dict(kind=base,level=level,error=str(mip_ex)))
            zf.writestr('mip_comparison.json',json.dumps(comparison,indent=2))
            zf.writestr('manifest.json',json.dumps(manifest,indent=2,default=str))
            zf.writestr('README.txt',
                'NASCAR 15 paint forensics package\n\n'
                'Contains only the selected paint resource wrappers, the matching pristine backup wrappers,\n'
                'the app-saved source PNG when present, and decoded mip images. No whole archive is included.\n'
                'live_stored shows physical page interpretation; live_logical reverses the currently mapped\n'
                'large-mip compensation for visual comparison. This export never changes the game.\n')
        out.seek(0)
        safe=re.sub(r'[^A-Za-z0-9_.-]+','_',name)
        return send_file(out,mimetype='application/zip',as_attachment=True,
                         download_name=f'PAINT_FORENSICS_{safe}_{time.strftime("%Y%m%d_%H%M%S")}.zip')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/roster')
def api_roster():
    g,reg=registry(); d,t=roster(reg)
    return jsonify(dict(drivers=d, teams=t))

@app.route('/api/name', methods=['POST'])
def api_name():
    g,reg=registry(); j=request.json
    old, new = j['old'], j['new'].strip()
    if not new: return jsonify(dict(ok=False,error='empty name')),400
    exp=bool(j.get('experimental'))
    try:
        n=_apply_display_name(reg, old, new, exp)
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    cfg=load_cfg(); led=cfg.setdefault('renames',{})
    orig=old
    for o,c2 in list(led.items()):
        if c2==old: orig=o; break
    led[orig]=new; save_cfg(cfg)
    return jsonify(dict(ok=True,patched=n,experimental=exp))

@app.route('/api/handle', methods=['POST'])
def api_handle():
    g,reg=registry(); j=request.json
    old,new=j['old'],j['new'].strip()
    if not new: return jsonify(dict(ok=False,error='empty handle')),400
    try: result=_shared_driver_handle_editor().rename_current(old,new)
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    return jsonify(dict(ok=True,patched=result['patched'],applied=result['current'],
                        storage_length=len(result['current'])))

@app.route('/api/names/export')
def names_export():
    try:
        _g,reg=registry();drivers,teams=roster(reg);cfg=load_cfg()
        out=io.StringIO();w=csv.writer(out);w.writerow(['type','original','current','new_value','notes'])
        for d in drivers:
            w.writerow(['driver',d['original'],d['current'],'','full/display name; aliases can be edited in UI Text'])
            if d.get('handle'):w.writerow(['handle',d['handle'],d.get('handle_current',d['handle']),'','driver-card handle'])
        for t in teams:w.writerow(['team',t['original'],t['current'],'','team display name'])
        return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='nascar15_names_roster.csv')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/names/restore_all',methods=['POST'])
def names_restore_all():
    try:
        _g,reg=registry();cfg=load_cfg();renames=dict(cfg.get('renames') or {});handles=dict(cfg.get('handles') or {})
        done=[];errors=[]
        # Reverse the newest display strings back to their original text-table strings.
        for original,current in list(renames.items()):
            if str(original)==str(current):continue
            try:_apply_display_name(reg,str(current),str(original),True);done.append(f'name {current} -> {original}')
            except Exception as ex:errors.append(f'{current}: {ex}')
        handle_editor=_shared_driver_handle_editor()
        mapped={row['original']:row for row in handle_editor.handles()}
        for original,current in list(handles.items()):
            if str(original)==str(current):continue
            try:
                if str(original) not in mapped:raise ValueError('mapped handle was not found')
                handle_editor.restore(mapped[str(original)]['driver_uid']);done.append(f'handle {current} -> {original}')
            except Exception as ex:errors.append(f'@{current}: {ex}')
        if errors:return jsonify(dict(ok=False,error='; '.join(errors),restored=done)),400
        cfg.pop('renames',None);cfg.pop('handles',None);save_cfg(cfg)
        _ui_text_invalidate()
        return jsonify(dict(ok=True,restored=done,count=len(done)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/stats')
def api_stats():
    g,reg=registry()
    return jsonify(dict(stats=read_stats(reg),order=STATS,
                        labels=dict(zip(STATS,STAT_LABELS)),normal_min=0,normal_max=100,
                        experimental_supported=True,experimental_abs_max=STAT_EXPERIMENTAL_ABS_MAX,
                        note='The original scale is 0-100. Custom values outside that range are supported and the app expands the rating data when needed.'))

@app.route('/api/stats/set', methods=['POST'])
def api_stats_set():
    g,reg=registry();j=request.get_json(force=True)
    try: result=write_stat(reg,j['profile_id'],j['stat'],j['value'],bool(j.get('experimental')))
    except Exception as e:return jsonify(dict(ok=False,error=str(e))),400
    return jsonify(dict(ok=True,**result))

@app.route('/api/stats/reset', methods=['POST'])
def api_stats_reset():
    g,reg=registry()
    n=reset_stats(reg, request.json['profile_id'])
    return jsonify(dict(ok=True, restored=n))

@app.route('/api/pack/export')
def pack_export():
    return pack_v2_export()



@app.route('/api/pack/import', methods=['POST'])
def pack_import():
    return pack_v2_import()


@app.route('/api/verify_game_files')
def verify_game_files_api():
    """Check every game file group the app can write to and report problems."""
    try:
        g, _reg = registry()
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400
    try:
        V = bank_verify_mod()
        report = V.verify_game(g, indexes=('1', '2'))
        data = report.to_dict()
        data['ok'] = True
        data['problem'] = not report.ok
        return jsonify(data)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/restore', methods=['POST'])
def restore():
    """Restore all available pristine archive/index pairs as one transaction.

    v1.0.1 copied files one at a time. A locked CDF or interrupted archive copy
    could therefore leave the game half stock and half modified, while stale
    extra/team state still claimed the edits existed. This stages every copy,
    swaps originals aside, verifies the complete set, and rolls the set back if
    any commit step fails.
    """
    try:
        if _extra_game_running():
            raise RuntimeError('The selected NASCAR game is running. Close it before restoring original files')
        result=_shared_backup_manager().restore_all()
        stamp=time.strftime('%Y%m%d_%H%M%S');state_archive=os.path.join(USER_DIR,'restored_app_state',stamp);archived_state=[]
        state_paths=[EXTRA_SCHEME_STATE,TEAM_MANAGER_STATE,globals().get('_RP_HISTORY')]
        for state_path in state_paths:
            if state_path and os.path.isfile(state_path):
                os.makedirs(state_archive,exist_ok=True);dest=os.path.join(state_archive,os.path.basename(state_path));os.replace(state_path,dest);archived_state.append(os.path.basename(state_path))
        if os.path.isdir(TEAM_ASSET_ROLLBACK_DIR):
            os.makedirs(state_archive,exist_ok=True);dest=os.path.join(state_archive,os.path.basename(TEAM_ASSET_ROLLBACK_DIR))
            if os.path.exists(dest):shutil.rmtree(dest)
            os.replace(TEAM_ASSET_ROLLBACK_DIR,dest);archived_state.append(os.path.basename(TEAM_ASSET_ROLLBACK_DIR)+'/')
        cfg=load_cfg();cfg.pop('renames',None);cfg.pop('handles',None);save_cfg(cfg)
        _clear_ui_thumb_cache()
        result.update(archived_state=archived_state,state_archive=(state_archive if archived_state else None),
                      note='All staged files passed readback. Previous app ownership/history state was archived so the restored game is rediscovered from live files.')
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400



# ==================== v0.6 additions ====================

@app.route('/api/import_scheme/<name>', methods=['POST'])
def api_import_scheme(name):
    """One-click import of an externally-edited (GIMP/PS) scheme PNG.
    Saves it as a complete 2048x1024 UV-atlas replacement and optionally
    installs immediately (?install=1)."""
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(2048,1024),'stretch',preserve_alpha=False)
    img=img.convert('RGB')
    png=os.path.join(SCHEMES,name+'.png'); lay=os.path.join(SCHEMES,name+'.layer.png')
    img.save(png)
    g,reg=registry()
    s=_slot(name)
    if os.path.exists(lay): os.remove(lay)
    prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
    wrote=None
    if request.args.get('install') and s:
        try:
            wrote=install_slot(reg,s,png,None)
        except Exception as e:
            return jsonify(dict(ok=False,error=f'install failed: {e}')),500
    return jsonify(dict(ok=True, installed=bool(wrote), wrote=wrote, image_prep=prep))


@app.route('/api/previewedit/<name>', methods=['GET','POST'])
def api_previewedit(name):
    """Load a career/AI 256x256 preview card into the canvas and save it back."""
    editor=_shared_texture_editor();container='BASESCHEMETHUMBNAILS.ARC';arcid='0'
    if request.method=='GET':
        try:img=editor.read_image(arcid,container,name)
        except Exception:return ('not found',404)
        buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
        return send_file(buf,mimetype='image/png')
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    current=editor.read_image(arcid,container,name)
    img,prep=prepare_import_image(img,current.size,request_resize_mode('fit'),preserve_alpha=True)
    editor.replace_image(arcid,container,name,img,resize_mode='stretch',experimental=True)
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True, image_prep=prep))


def import_liv_tmp(path, out_png, raw_offset=0x5, w=2048, h=1024):
    """PROTOTYPE: decode a captured Paint Booth LIV_TMP DXT1 texture to a scheme
    PNG you can then install via the normal import path. Manual use only."""
    data=open(path,'rb').read()
    need_bytes=(w//4)*(h//4)*8
    payload=data[raw_offset:raw_offset+need_bytes]
    if len(payload)<need_bytes:
        payload=payload+b'\0'*(need_bytes-len(payload))
    arr=C._dxt1_decode(payload,w,h)
    Image.fromarray(arr).convert('RGB').save(out_png)
    return out_png

# ==================== end v0.6 additions ====================



# ==================== v0.8 AUDIO LAB ====================
import base64 as _b64

def _shared_audio_tools():
    return AudioToolsManager(USER_DIR)


ffmpeg_path = _shared_audio.ffmpeg_path










@app.route('/api/audio/tools/status')
def audio_tools_status():
    return jsonify(dict(ok=True, **_shared_audio_tools().status()))


@app.route('/api/audio/tools/install', methods=['POST'])
def audio_tools_install():
    try:
        return jsonify(dict(ok=True, **_shared_audio_tools().install()))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/audio/banks')
def audio_banks():
    try:
        rows=_shared_audio_editor().banks()
        banks=[dict(row,arc=row['archive'],n=row['sample_count']) for row in rows]
        return jsonify(dict(
            banks=banks,categories=sorted({row['category'] for row in rows}),
            ffmpeg=bool(_shared_audio.ffmpeg_path()),ffmpeg_details=_ffmpeg_details(),
        ))
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/samples',methods=['POST'])
def audio_samples():
    q=request.get_json(force=True)
    try:
        info=_shared_audio_editor().samples(str(q['arc']),q['bank'])
        samples=[
            dict(row,idx=row['index'],dur=row['duration'],ok=row['editable'])
            for row in info['samples']
        ]
        if info['kind']=='snd':
            note=f"{len(samples)} speech clips found in this container"
        elif info['full_length_candidate']:
            note=(
                'Full-length replacement is available for this music group.'
                if info['full_length_supported'] else
                'This is a full-length music candidate, but FFmpeg is not installed.'
            )
        elif info['editable']:
            note=None
        else:
            note=f"{info['codec']} bank did not pass replacement validation and is read-only"
        return jsonify(dict(
            **info,samples=samples,note=note,
            music=_shared_audio_category(q['bank'])=='Music',
            release_label=APP_RELEASE_LABEL,
        ))
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/preview')
def audio_preview():
    try:
        editor=_shared_audio_editor()
        arc=str(request.args.get('arc'));bank=request.args.get('bank')
        index=int(request.args.get('idx'))
        try:
            item=editor.sample_payload(arc,bank,index,'wav')
        except Exception:
            try:item=editor.sample_payload(arc,bank,index,'mpeg')
            except Exception:item=editor.sample_payload(arc,bank,index,'raw')
        return send_file(io.BytesIO(item['payload']),mimetype=item['mime'])
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/export')
def audio_export():
    try:
        item=_shared_audio_editor().sample_payload(
            str(request.args.get('arc')),request.args.get('bank'),
            int(request.args.get('idx')),(request.args.get('mode') or 'wav').lower(),
        )
        return send_file(
            io.BytesIO(item['payload']),mimetype=item['mime'],as_attachment=True,
            download_name=item['filename'],
        )
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/export_bank')
def audio_export_bank():
    temp_path=None
    try:
        arc=str(request.args.get('arc'));bank=request.args.get('bank')
        modified=request.args.get('modified') in ('1','true','yes')
        fd,temp_path=tempfile.mkstemp(prefix='nascar_audio_export_',suffix='.zip')
        os.close(fd)
        result=_shared_audio_editor().export_bank(arc,bank,temp_path,modified_only=modified)
        @after_this_request
        def cleanup(response):
            try:os.remove(temp_path)
            except OSError:pass
            return response
        suffix='_modified' if modified else ''
        filename=re.sub(r'[^A-Za-z0-9._-]+','_',os.path.splitext(bank)[0]).strip('._')+suffix+'.zip'
        return send_file(result['path'],mimetype='application/zip',as_attachment=True,download_name=filename)
    except Exception as e:
        if temp_path:
            try:os.remove(temp_path)
            except OSError:pass
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/restore_bank',methods=['POST'])
def audio_restore_bank():
    q=request.get_json(force=True)
    try:
        result=_shared_audio_editor().restore_bank(str(q['arc']),q['bank'])
        return jsonify(dict(ok=True,**result))
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/replace_full',methods=['POST'])
def audio_replace_full():
    upload=request.files.get('file')
    if not upload:return jsonify(dict(error='no file uploaded')),400
    try:
        result=_shared_audio_editor().replace_full_song(
            str(request.form.get('arc')),request.form.get('bank'),
            int(request.form.get('idx')),upload.read(),upload.filename or '',
            request.form.get('volume_mode') or 'match_stock',
            request.form.get('volume_gain_db') or 0.0,
        )
        result['note']=(
            f"full song installed at {result['duration']:.2f}s; "
            f"{result['frames']} MPEG frames; bank growth {result['growth']:+,} bytes"
        )
        return jsonify(dict(ok=True,**result))
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/replace',methods=['POST'])
def audio_replace():
    upload=request.files.get('file')
    if not upload:return jsonify(dict(error='no file uploaded')),400
    try:
        result=_shared_audio_editor().replace_sample(
            str(request.form.get('arc')),request.form.get('bank'),
            int(request.form.get('idx')),upload.read(),upload.filename or '',
            request.form.get('volume_mode') or 'match_stock',
            request.form.get('volume_gain_db') or 0.0,
        )
        result['note']='installed through the shared transactional audio editor'
        return jsonify(dict(ok=True,**result))
    except Exception as e:
        return jsonify(dict(error=str(e))),400


@app.route('/api/audio/restore',methods=['POST'])
def audio_restore():
    q=request.get_json(force=True)
    try:
        result=_shared_audio_editor().restore_sample(
            str(q['arc']),q['bank'],int(q['idx'])
        )
        return jsonify(dict(ok=True,**result))
    except Exception as e:
        return jsonify(dict(error=str(e))),400

# ==================== end v0.8 AUDIO LAB ====================



# ==================== v0.9 RACE SETTINGS / AI (mapper-backed) ====================
# The app does NOT re-parse PYC records. It shells out to the proven mapper
# (nascar15_pyc_record_mapper_v5_teams.py + nascar15_v11_probe_patcher.py),
# which resolves real post-setup values and writes same-size patched COPIES.
# The app owns the three-archive model + backup/restore around those calls.
import subprocess as _sp, csv as _csv, tempfile as _tf, hashlib as _hl

MAPPER_NAME  = 'nascar15_pyc_record_mapper_v5_teams.py'
PATCHER_NAME = 'nascar15_v11_probe_patcher.py'
DBFILE = 'DB_GAME_LOCAL_SCRIPT.PYC'
AICFG  = 'DB_AICONFIG_SCRIPT.PYC'
REPOINT_NAME = 'nascar15_const_repoint_v0_2.py'

# Field index of RaceLaps among a RACEDATA_c constructor's LOAD_CONST args.
# Confirmed from the real bytecode: arg[0]=UID, arg[1]=RaceLaps, arg[2]=NumDrivers.
REPOINT_FIELDS = {('RACEDATA_c','RaceLaps'): 1}


# v0.9.15 AI Behavior Lab. These names come from the real DB_AICONFIG_SCRIPT
# constructors. New fields remain experimental until individually verified in-game.
AI_TRACK_FIELDS = list(SHARED_AI_TRACK_FIELDS)
AI_GLOBAL_FIELDS = list(SHARED_AI_GLOBAL_FIELDS)
WORLD_PACE_FIELDS = list(SHARED_WORLD_PACE_FIELDS)

AI_EDITABLE_BY_CLASS = {
    'RACEDATA_c': {'RaceLaps'},
    'AIRACINGTRACKCONFIG_c': set(AI_TRACK_FIELDS),
    'AIRACINGGLOBALCONFIG_c': set(AI_GLOBAL_FIELDS),
    'WORLDSCRIPT_c': set(WORLD_PACE_FIELDS),
}

def _direct_scalar(v):
    """Only direct number/bool constructor fields are writable.
    Nested min/max objects are displayed read-only by the UI and blocked here."""
    s=str(v).strip()
    if s in ('True','False'): return True
    try:
        float(s); return True
    except Exception:
        return False

def repoint_mod():
    """Load the bundled isolated-repoint helper."""
    try:
        return _load_internal_module(REPOINT_NAME, 'n15repoint')
    except Exception:
        return None

def isolated_repoint(pyc_name, uid, old_value, new_value, out_archive):
    """Repoint one record's LOAD_CONST operand instead of mutating a shared constant.
    Writes a same-size patched COPY to out_archive. Never touches the live archive.
    Returns dict(ok, error, available, old_index, new_index)."""
    R=repoint_mod()
    if not R: return dict(ok=False, error='repoint tool not installed')
    live=_live_archive(); cdf=_cdfiles_path()
    data,off,sz = R.extract_from_archive(live, cdf, pyc_name)
    if data is None: return dict(ok=False, error=f'{pyc_name} not found in archive')
    # locate the record + the arg slot holding old_value
    co=args=None
    for c in R.parse(data):
        a=R.find_record_loadconsts(c, int(uid))
        if a: co, args = c, a; break
    if not args: return dict(ok=False, error=f'UID {uid} not found as a constructor arg')
    fidx,_n = R.autodetect_field_index(args, int(old_value))
    if fidx is None:
        return dict(ok=False, error=f'no argument of record {uid} currently holds {old_value}')
    target = args[fidx]
    new_idx = R.find_const_with_value(co, int(new_value))
    if new_idx is None:
        avail=sorted({v for _,v in R.lap_like_consts(co)})
        return dict(ok=False, unavailable_value=True,
                    error=f'{new_value} is not an existing constant, so it cannot be set without resizing the file',
                    available=avail)
    if new_idx > 0xFFFF:
        return dict(ok=False, error='const index requires EXTENDED_ARG; unsupported')
    old_idx = target['const_index']
    buf=bytearray(data)
    aoff = target['arg_off']
    cur = struct.unpack_from('<H', buf, co.code_off + aoff)[0]
    if cur != old_idx:
        return dict(ok=False, error='operand mismatch; refusing to write')
    struct.pack_into('<H', buf, co.code_off + aoff, new_idx)
    if len(buf) != sz:
        return dict(ok=False, error='patched pyc size differs; refused')
    shutil.copyfile(live, out_archive)
    with open(out_archive,'r+b') as f:
        f.seek(off); f.write(bytes(buf))
    return dict(ok=True, old_index=old_idx, new_index=new_idx, field_index=fidx)

def mapper_paths():
    """Return the bundled mapper and patcher paths when both are present."""
    m = component_path(MAPPER_NAME)
    p = component_path(PATCHER_NAME)
    return (m if os.path.exists(m) else None, p if os.path.exists(p) else None)

def mapper_ready():
    m,p = mapper_paths(); return bool(m and p)

def _py():
    # Prefer the same interpreter running the app.
    return sys.executable or 'python'

def _run_mapper(args, timeout=180):
    """Run the mapper with a subcommand + args list. Returns (rc, stdout, stderr).
    Mapper output is UTF-16 on Windows redirects but UTF-8 to a pipe; decode robustly."""
    m,p = mapper_paths()
    if not (m and p): raise RuntimeError('required game-data tools are missing')
    cmd = [_py(), m] + args
    r = _sp.run(cmd, capture_output=True, timeout=timeout)
    def dec(b):
        for enc in ('utf-8','utf-16','latin1'):
            try: return b.decode(enc)
            except Exception: continue
        return b.decode('utf-8','replace')
    return r.returncode, dec(r.stdout), dec(r.stderr)

def _cfg_get(key, default=None):
    c=load_cfg(); return c.get(key, default)
def _cfg_set(key, val):
    c=load_cfg(); c[key]=val; save_cfg(c)

def _sha256(path, limit=None):
    h=_hl.sha256()
    with open(path,'rb') as f:
        while True:
            b=f.read(1<<20)
            if not b: break
            h.update(b)
    return h.hexdigest()

def _live_archive():
    """The game's live data/ARCHIVE0.AR (patch target)."""
    g,reg=registry()
    if not g or '0' not in reg: return None
    return reg['0']['ar']

def _cdfiles_path():
    g,reg=registry()
    if not g or '0' not in reg: return None
    return reg['0']['cdf']

def mapper_records(pyc_file, class_name, fields, archive=None):
    """Enumerate records via `mapper records ... --csv <tmp>`.
    archive defaults to the LIVE archive; pass a temp archive to diff a patch."""
    if archive is None:
        return _shared_pyc_editor().records_for(pyc_file,class_name,fields)
    live=archive or _live_archive(); cdf=_cdfiles_path()
    if not live or not cdf: raise RuntimeError('game archive not found')
    m,p=mapper_paths()
    tmpcsv=os.path.join(_tf.gettempdir(), f'n15mod_records_{os.getpid()}_{abs(hash(live))%99999}.csv')
    args=['records','--archive',live,'--cdfiles',cdf,'--patcher',p,
          '--file',pyc_file,'--class',class_name,
          '--limit','100000','--csv',tmpcsv]
    if fields: args+=['--fields']+fields
    rc,out,err=_run_mapper(args)
    if rc!=0 or not os.path.exists(tmpcsv):
        raise RuntimeError(f'mapper records failed: {err.strip() or out.strip()}')
    rows=[]
    with open(tmpcsv,'r',encoding='utf-8',newline='') as f:
        for row in _csv.DictReader(f): rows.append(row)
    try: os.remove(tmpcsv)
    except OSError: pass
    return rows

def _diff_records(before, after, fields):
    """Compare two record lists (by uid) across `fields`. Returns list of
    (uid, field, old, new) for every changed cell."""
    bi={str(r.get('uid')):r for r in before}
    changes=[]
    for r in after:
        u=str(r.get('uid')); b=bi.get(u)
        if not b: continue
        for f in fields:
            ov=b.get(f); nv=r.get(f)
            if ov is None and nv is None: continue
            if not _num_eq(ov, nv) and str(ov)!=str(nv):
                changes.append((u, f, ov, nv))
    return changes

def _num_eq(a,b):
    try: return abs(float(a)-float(b))<1e-6
    except Exception: return str(a)==str(b)

# Which fields to diff per class (all editable/at-risk fields, so we catch
# collateral changes to sibling records sharing a marshal constant).
_DIFF_FIELDS={
    'RACEDATA_c':['RaceLaps'],
    # Diff every field exposed by the lab, not only the selected one. This catches
    # a shared marshal constant changing a sibling field or another track record.
    'AIRACINGTRACKCONFIG_c':AI_TRACK_FIELDS,
    'AIRACINGGLOBALCONFIG_c':AI_GLOBAL_FIELDS,
    'WORLDSCRIPT_c':WORLD_PACE_FIELDS,
}


_MAPPER_DIRECT_CACHE=None

def _mapper_direct_module():
    global _MAPPER_DIRECT_CACHE
    if _MAPPER_DIRECT_CACHE is not None:return _MAPPER_DIRECT_CACHE
    _MAPPER_DIRECT_CACHE = _load_internal_module(
        MAPPER_NAME,
        'n15mod_mapper_direct',
        load_message='could not load PYC mapper module',
    )
    return _MAPPER_DIRECT_CACHE

def _pyc_live_blob(pyc_file):
    g,reg=registry();v=need(reg,'0')
    _raw,rows,_layout=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,pyc_file)
    with open(v['ar'],'rb') as fh:fh.seek(row['offset']);data=fh.read(row['size'])
    if len(data)!=row['size']:raise ValueError(f'short {pyc_file} read')
    return v,row,data

def _mapped_rows_from_pyc_bytes(pyc,class_name,fields):
    return _shared_mapped_rows_from_pyc_bytes(pyc,class_name,fields)

def _scalar_same_type(a,b):
    return _shared_scalar_same_type(a,b)

def _coerce_scalar_like(old,value):
    return _shared_coerce_scalar_like(old,value)

def _patch_load_const_operand(pyc,operand_abs,new_index,new_value=None,old_value=None):
    return _shared_patch_load_const_operand(pyc,operand_abs,new_index,new_value,old_value)

def _exact_field_variant(pyc,class_name,uid,field,value):
    return _shared_exact_field_variant(pyc,class_name,uid,field,value,_DIFF_FIELDS.get(class_name,[field]))


def _shared_pyc_workflow(pyc_file,class_name):
    mapping={
        (DBFILE,'RACEDATA_c'):'race_laps',
        (AICFG,'AIRACINGTRACKCONFIG_c'):'ai_track',
        (AICFG,'AIRACINGGLOBALCONFIG_c'):'ai_global',
        (DBFILE,'WORLDSCRIPT_c'):'world_pace',
    }
    try:return mapping[(pyc_file,class_name)]
    except KeyError as ex:raise ValueError(f'unsupported PYC class/file: {class_name} / {pyc_file}') from ex

def mapper_set_value(pyc_file, class_name, uid, field, value, dry_run=False):
    """Compatibility seam; shared PycRecordEditor owns every live write."""
    try:
        workflow=_shared_pyc_workflow(pyc_file,class_name)
        change=dict(uid=uid,field=field,value=value)
        editor=_shared_pyc_editor()
        result=editor.preview(workflow,[change]) if dry_run else editor.apply(workflow,[change])
        result.pop('_payload',None)
        return result
    except Exception as ex:
        return dict(ok=False,error=str(ex))




def mapper_set_values_batch(pyc_file, class_name, changes, dry_run=False):
    """Compatibility seam for shared exact-field batch editing."""
    try:
        workflow=_shared_pyc_workflow(pyc_file,class_name)
        editor=_shared_pyc_editor()
        result=editor.preview(workflow,changes) if dry_run else editor.apply(workflow,changes)
        result.pop('_payload',None)
        return result
    except Exception as ex:
        return dict(ok=False,error=str(ex))




# ---- stock baseline management ----
_BASELINE_VERIFY_CACHE={}

def _baseline_live_match(idx,path,size):
    g,reg=registry()
    idx=str(idx)
    if not g or idx not in reg:
        raise ValueError(f'ARCHIVE{idx} is not installed in the selected game folder')
    live=reg[idx]['ar']
    if os.path.normcase(os.path.realpath(path))==os.path.normcase(os.path.realpath(live)):
        raise ValueError(f'ARCHIVE{idx} baseline cannot be the live game archive; choose a separate clean copy')
    live_size=os.path.getsize(live)
    if int(size)!=live_size:
        raise ValueError(f'ARCHIVE{idx} baseline size {int(size)} does not match live archive size {live_size}')
    return live

def _verify_baseline_entry(idx,entry,verify_hash=True):
    idx=str(idx)
    if not entry or not entry.get('path'):
        raise ValueError(f'no baseline registered for ARCHIVE{idx}')
    path=os.path.abspath(entry['path'])
    if not os.path.isfile(path):
        raise ValueError(f'baseline for ARCHIVE{idx} is missing: {path}')
    st=os.stat(path); expected_size=int(entry.get('size',-1))
    if st.st_size!=expected_size:
        raise ValueError(f'baseline ARCHIVE{idx} size changed since registration ({st.st_size} vs {expected_size})')
    _baseline_live_match(idx,path,st.st_size)
    if verify_hash:
        expected_hash=str(entry.get('sha256') or '').lower()
        if len(expected_hash)!=64:
            raise ValueError(f'baseline ARCHIVE{idx} has no valid stored SHA-256')
        key=(path,st.st_size,st.st_mtime_ns,expected_hash)
        ok=_BASELINE_VERIFY_CACHE.get(key)
        if ok is None:
            ok=(_sha256(path).lower()==expected_hash)
            for old_key in list(_BASELINE_VERIFY_CACHE):
                if old_key[0]==path and old_key!=key:
                    _BASELINE_VERIFY_CACHE.pop(old_key,None)
            _BASELINE_VERIFY_CACHE[key]=ok
        if not ok:
            raise ValueError(f'baseline ARCHIVE{idx} hash mismatch - file changed since it was registered')
    return path

def _baseline_public_status(idx,entry,verify_hash=True):
    try:
        path=_verify_baseline_entry(idx,entry,verify_hash=verify_hash)
        return dict(path=path,sha256=str(entry['sha256'])[:16],size=int(entry['size']),ok=True,error=None)
    except Exception as ex:
        return dict(path=(entry or {}).get('path'),sha256=str((entry or {}).get('sha256',''))[:16],
                    size=(entry or {}).get('size'),ok=False,error=str(ex))

def set_stock_baseline(path):
    """Register clean ARCHIVE*.AR copies after filename, size, and hash checks."""
    path=os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
    g,reg=registry()
    if not g:
        raise ValueError('select the NASCAR 15 game folder before setting a baseline')
    candidates=[]
    if os.path.isdir(path):
        for fn in sorted(os.listdir(path)):
            m=re.fullmatch(r'ARCHIVE(\d+)\.AR',fn,re.I)
            if m:
                candidates.append((m.group(1),os.path.join(path,fn)))
        if not candidates:
            raise ValueError('no exactly named ARCHIVE<number>.AR files found in that folder')
    else:
        if not os.path.isfile(path) or os.path.getsize(path)<64:
            raise ValueError('baseline archive not found or too small')
        m=re.fullmatch(r'ARCHIVE(\d+)\.AR',os.path.basename(path),re.I)
        if not m:
            raise ValueError('baseline file must be named exactly ARCHIVE<number>.AR')
        candidates=[(m.group(1),path)]

    entries={}
    for idx,full in candidates:
        size=os.path.getsize(full)
        _baseline_live_match(idx,full,size)
        digest=_sha256(full)
        st=os.stat(full)
        entries[str(idx)]=dict(path=os.path.abspath(full),sha256=digest,size=size,
                               registered_mtime_ns=st.st_mtime_ns)
        _BASELINE_VERIFY_CACHE[(os.path.abspath(full),size,st.st_mtime_ns,digest.lower())]=True
    _cfg_set('stock_baselines',entries)
    _cfg_set('stock_baseline',entries.get('0'))
    return entries

def stock_baselines():
    e=_cfg_get('stock_baselines')
    if e: return e
    legacy=_cfg_get('stock_baseline')
    return {'0':legacy} if legacy else {}

def stock_baseline():
    return stock_baselines().get('0')

def baseline_archive(idx='0'):
    """Return a clean archive only after stored size/hash and live-size checks."""
    e=stock_baselines().get(str(idx))
    if not e: return None
    return _verify_baseline_entry(str(idx),e,verify_hash=True)

def restore_from(source):
    """Restore registered baselines or the app's pristine archive backups."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    restored=[]
    if source=='baseline':
        entries=stock_baselines()
        if not entries: raise RuntimeError('no clean original game copy has been selected')
        verified=[]
        for idx,e in entries.items():
            try: src=_verify_baseline_entry(idx,e,verify_hash=True)
            except Exception as ex: raise RuntimeError(str(ex))
            verified.append((str(idx),src,e))
        prepared=[]; temp_paths=[]
        try:
            # Prepare every copy before replacing any live archive.
            for idx,src,e in verified:
                live=reg[idx]['ar']; tmp=live+'.n15mod.restore.tmp'
                temp_paths.append(tmp)
                if os.path.exists(tmp): os.remove(tmp)
                shutil.copyfile(src,tmp)
                if os.path.getsize(tmp)!=int(e['size']) or _sha256(tmp).lower()!=str(e['sha256']).lower():
                    raise RuntimeError(f'prepared ARCHIVE{idx} restore copy failed verification')
                prepared.append((idx,tmp,live))
            for idx,tmp,live in prepared:
                os.replace(tmp,live); restored.append(f'ARCHIVE{idx}')
        finally:
            for tmp in temp_paths:
                try:
                    if os.path.exists(tmp): os.remove(tmp)
                except OSError: pass
    else:
        for idx,r in reg.items():
            if os.path.exists(r['bak']):
                shutil.copyfile(r['bak'],r['ar']); restored.append(f'ARCHIVE{idx}')
        if not restored: raise RuntimeError('no original backup is available')
    _clear_ui_thumb_cache()
    return dict(ok=True,restored_from=source,restored=restored)

@app.route('/api/pyc/status')
def pyc_status():
    ready=mapper_ready(); entries=stock_baselines(); sb=entries.get('0')
    g,reg=registry(); live=_live_archive()
    # Status pages must stay instant. Full hashes are still verified before restore/apply.
    statuses={k:_baseline_public_status(k,v,verify_hash=False) for k,v in entries.items()}
    return jsonify(dict(
        ready=ready,mapper=os.path.basename(MAPPER_NAME),patcher=os.path.basename(PATCHER_NAME),
        repoint=bool(repoint_mod()),stock_baseline=statuses.get('0'),stock_baselines=statuses,
        has_backup=bool(g and '0' in reg and os.path.exists(reg['0']['bak'])),live=live))

@app.route('/api/pyc/records', methods=['POST'])
def pyc_records():
    if not mapper_ready(): return jsonify(dict(ok=False, error='mapper/patcher not in app folder')),400
    q=request.get_json()
    try:
        arc=None
        if q.get('source')=='baseline':
            arc=baseline_archive('0')
            if not arc: return jsonify(dict(ok=False, error='no clean baseline registered')),400
        rows=mapper_records(q['file'], q['class'], q.get('fields'), archive=arc)
        return jsonify(dict(rows=rows, count=len(rows), source=q.get('source','live')))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/set', methods=['POST'])
def pyc_set():
    if not mapper_ready(): return jsonify(dict(ok=False, error='mapper/patcher not in app folder')),400
    q=request.get_json()
    # Class-specific public guard. NumDrivers and unknown fields stay blocked.
    cls=q.get('class'); field=q.get('field')
    allowed=AI_EDITABLE_BY_CLASS.get(cls,set())
    expected_file={'RACEDATA_c':DBFILE,
                   'AIRACINGTRACKCONFIG_c':AICFG,
                   'AIRACINGGLOBALCONFIG_c':AICFG,
                   'WORLDSCRIPT_c':DBFILE}.get(cls)
    if q.get('file')!=expected_file:
        return jsonify(dict(ok=False,
            error=f'class {cls} must be edited in {expected_file or "an approved file"}')),400
    if field not in allowed:
        return jsonify(dict(ok=False,
            error=f'field {field} is not editable for class {cls} in this app')),400
    try:
        res=mapper_set_value(q['file'], q['class'], q['uid'], q['field'], q['value'],
                             dry_run=bool(q.get('dry_run')))
        return jsonify(res if 'ok' in res else dict(ok=True, **res))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/set_batch', methods=['POST'])
def pyc_set_batch():
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    q=request.get_json(force=True)
    cls=q.get('class'); expected={'AIRACINGTRACKCONFIG_c':AICFG,
                                 'AIRACINGGLOBALCONFIG_c':AICFG}.get(cls)
    if not expected or q.get('file')!=expected:
        return jsonify(dict(ok=False,error='unsupported class/file for AI batch edit')),400
    try:
        result=mapper_set_values_batch(expected,cls,q.get('changes',[]),
                                       dry_run=bool(q.get('dry_run')))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400

@app.route('/api/pyc/baseline', methods=['POST'])
def pyc_baseline():
    q=request.get_json()
    try:
        entries=set_stock_baseline(q['path'])
        return jsonify(dict(ok=True, count=len(entries),
            archives=sorted(entries.keys(), key=lambda x:int(x)),
            details={k:dict(sha256=v['sha256'][:16], size=v['size']) for k,v in entries.items()}))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/restore', methods=['POST'])
def pyc_restore():
    q=request.get_json()
    try:
        return jsonify(restore_from(q.get('source','backup')))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

def _first_int(s):
    m=re.search(r'\((\d+)', s or '')
    return m.group(1) if m else None

def _pretty_world(tok):
    if not tok: return None
    t=re.sub(r'^S_WORLD_(LOC_)?','',tok)
    return t.replace('_',' ').title()

@app.route('/api/pyc/aitrack_crosswalk')
def pyc_aitrack_crosswalk():
    """Return every exposed AIRACINGTRACKCONFIG field with friendly track names,
    per-field shared-value warnings, and clean-baseline values when available."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    try:
        cfg=mapper_records(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS)
        tracks=mapper_records(AICFG,'TRACK_c',['AITrackProfile'])
        aiws=mapper_records(AICFG,'WORLDSCRIPT_c',['WorldID','TrackPointer'])
        gws=mapper_records(DBFILE,'WORLDSCRIPT_c',['WorldID','WorldName','WorldLocation'])
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400

    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            stock_rows=mapper_records(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in stock_rows}
        except Exception:
            stock_by_uid={}

    track_to_cfg={str(t['uid']):_first_int(t.get('AITrackProfile')) for t in tracks}
    aiworld_to_track={str(w.get('WorldID')):_first_int(w.get('TrackPointer')) for w in aiws}
    game_by_world={str(g.get('WorldID')):g for g in gws}
    cfg_to_world={}
    for wid,truid in aiworld_to_track.items():
        cuid=track_to_cfg.get(str(truid))
        if cuid: cfg_to_world.setdefault(str(cuid),[]).append(str(wid))

    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in cfg) for f in AI_TRACK_FIELDS}
    out=[]
    for c in cfg:
        cuid=str(c.get('uid')); label=loc=wtok=None
        for wid in cfg_to_world.get(cuid,[]):
            gw=game_by_world.get(str(wid))
            if gw:
                wtok=gw.get('WorldName'); loc=gw.get('WorldLocation')
                label=_pretty_world(wtok); break
        row=dict(c)
        row.update(track=label or 'Unmapped config',
                   location=_pretty_world(loc) if loc else None,
                   world_token=wtok,
                   stock={f:stock_by_uid.get(cuid,{}).get(f) for f in AI_TRACK_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(c.get(f)),0)>1 for f in AI_TRACK_FIELDS},
                   scalar={f:_direct_scalar(c.get(f)) for f in AI_TRACK_FIELDS})
        out.append(row)
    out.sort(key=lambda r:(r['track']=='Unmapped config',r['track'] or '',int(r.get('uid') or 0)))
    return jsonify(dict(ok=True,rows=out,fields=AI_TRACK_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        shared_note='Values vary by track. Shared-value edits are previewed against every exposed field and are blocked if anything except the selected UID/field would change.'))

@app.route('/api/pyc/aiglobal')
def pyc_aiglobal():
    """Read global AI behavior records. Nested min/max objects remain read-only."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    try:
        rows=mapper_records(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400
    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            sr=mapper_records(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in sr}
        except Exception:
            stock_by_uid={}
    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in rows) for f in AI_GLOBAL_FIELDS}
    out=[]
    for r in rows:
        uid=str(r.get('uid')); row=dict(r)
        row.update(stock={f:stock_by_uid.get(uid,{}).get(f) for f in AI_GLOBAL_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(r.get(f)),0)>1 for f in AI_GLOBAL_FIELDS},
                   scalar={f:_direct_scalar(r.get(f)) for f in AI_GLOBAL_FIELDS})
        out.append(row)
    return jsonify(dict(ok=True,rows=out,fields=AI_GLOBAL_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        note='Direct scalar fields can be previewed/applied. Nested min/max objects are intentionally read-only.'))

@app.route('/api/pyc/worldpace')
def pyc_worldpace():
    """Track-specific practice/qualifying pace targets and environment values."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    fields=['WorldID','WorldName','WorldLocation']+WORLD_PACE_FIELDS
    try:
        rows=mapper_records(DBFILE,'WORLDSCRIPT_c',fields)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400
    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            sr=mapper_records(DBFILE,'WORLDSCRIPT_c',fields,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in sr}
        except Exception:
            stock_by_uid={}
    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in rows) for f in WORLD_PACE_FIELDS}
    out=[]
    for r in rows:
        uid=str(r.get('uid')); token=r.get('WorldName'); loc=r.get('WorldLocation')
        if not any(r.get(f) not in (None,'') for f in WORLD_PACE_FIELDS):
            continue
        row=dict(r)
        row.update(track=_pretty_world(token) or _pretty_world(loc) or f'World UID {uid}',
                   stock={f:stock_by_uid.get(uid,{}).get(f) for f in WORLD_PACE_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(r.get(f)),0)>1 for f in WORLD_PACE_FIELDS},
                   scalar={f:_direct_scalar(r.get(f)) for f in WORLD_PACE_FIELDS})
        out.append(row)
    out.sort(key=lambda r:(r.get('track') or '',int(r.get('uid') or 0)))
    return jsonify(dict(ok=True,rows=out,fields=WORLD_PACE_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        note='Practice best/worst values are track pace targets. Record speeds and temperatures are reference/environment fields. Every edit is previewed against all exposed WORLDSCRIPT fields and blocked if a shared constant would alter another track.'))

# ==================== end v0.9 RACE SETTINGS / AI ====================



# ==================== v0.9.5 SCR DRAFT / AERO ====================
# SCR files store physics as NUL-separated ASCII: KEY \0 VALUE \0
# Confirmed from ARCHIVE0 (48 track SCRs + pace car):
#   AERODYNAMICS { DRAG-CDA 0.0  FRONT-DRAFT-DRAG 0.91 ... }
# Values are edited IN PLACE with the SAME character count (no resizing).
SCR_KEYS = ['FRONT-DRAFT-DRAG','REAR-DRAFT-DRAG','SIDE-DRAFT-DRAG',
            'FRONT-DRAFT-DOWNFORCE','REAR-DRAFT-DOWNFORCE','OVERALL-DOWNFORCE-SCALE']
SCR_NUMRX = re.compile(r'^-?\d+(\.\d+)?$')

def _scr_kv(data, key):
    """Find KEY\0VALUE\0. Returns (value, value_offset, value_len) or (None,None,None).
    Requires the key to be a whole NUL-delimited token."""
    k=key.encode()
    i=data.find(k)
    while i>=0:
        pre_ok = (i==0 or data[i-1]==0)
        end=i+len(k)
        if pre_ok and end<len(data) and data[end]==0:
            e=data.find(b'\0', end+1)
            if e>0:
                return data[end+1:e].decode('latin1'), end+1, e-(end+1)
        i=data.find(k, i+1)
    return None,None,None

def _scr_role(name):
    return _shared_scr_role(name)

def _scr_track(name):
    return _shared_scr_track(name)

def scr_entries(archive_override=None):
    """Enumerate SCR entries that actually contain an AERODYNAMICS block.
    Self-validating: an entry is only accepted if the extracted bytes really
    hold the keys, so reading the wrong archive can never silently succeed."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    out=[]
    for arcid,r in sorted(reg.items(), key=lambda x:int(x[0])):
        try: ents=parse_cdfiles(r['cdf'])
        except Exception: continue
        arpath = archive_override if (archive_override and arcid=='0') else r['ar']
        if not os.path.exists(arpath): continue
        with open(arpath,'rb') as f:
            for off,size,name in ents:
                if not name.upper().endswith('_SCR.ARC'): continue
                role=_scr_role(name)
                if not role: continue
                if size<=0 or size>4*1024*1024: continue
                f.seek(off); data=f.read(size)
                if b'AERODYNAMICS' not in data: continue
                vals={}; offs={}
                for k in SCR_KEYS:
                    v,vo,vl=_scr_kv(data,k)
                    if v is None: continue
                    vals[k]=v; offs[k]=dict(abs_off=off+vo, length=vl)
                if 'FRONT-DRAFT-DRAG' not in vals: continue
                out.append(dict(arc=arcid, name=name, entry_off=off, size=size,
                                track=_scr_track(name), role=role,
                                values=vals, offsets=offs))
    return out

def _scr_snapshot(archive_override=None):
    return {(e['name'],k):v for e in scr_entries(archive_override)
            for k,v in e['values'].items()}

def scr_set(name, key, new_value, dry_run=False):
    """Compatibility wrapper for older Draft/Aero callers.

    All edits now use the same Racing Controls dispatcher: same-width values
    receive surgical writes, while shorter/longer values rebuild and repoint
    the owning SCR container.
    """
    try:
        rows=_scr_numeric_inventory()
        row=next((r for r in rows if r['name'].upper()==str(name).upper()
                  and r['key'].upper()==str(key).upper() and int(r['occurrence'])==0),None)
        if not row:return dict(ok=False,error=f'{name}/{key} not found')
        return scr_key_set(row['arc'],row['name'],row['key'],new_value,
                           dry_run=dry_run,occurrence=0)
    except Exception as ex:
        return dict(ok=False,error=str(ex))

PLATE_TRACKS = {'Daytona','Talladega'}

def _scr_stock_source():
    """Best clean SCR reference as (archive, cdfiles, label).

    Repoint installs move indexed entries, so a pristine archive MUST be read
    with the pristine cdfiles index that was backed up beside it. Older builds
    mixed the stock archive with the live index, which produced blank Stock
    columns after the first repoint.
    """
    try:
        g,reg=registry();v=reg.get('0') if reg else None
        if v:
            cb=backup_path(v['cdf'])
            if os.path.exists(v['bak']) and os.path.exists(cb):
                return v['bak'],cb,'backup'
    except Exception: pass
    try:
        b=baseline_archive('0')
        if b:
            # A registered baseline currently stores the archive only. It is
            # usable with the live index only while no entry offsets differ.
            g,reg=registry();v=reg.get('0') if reg else None
            if v and os.path.getsize(b)==os.path.getsize(v['ar']):
                return b,v['cdf'],'baseline'
    except Exception: pass
    return None,None,None

@app.route('/api/scr/list')
def scr_list():
    try:
        ents=_shared_scr_editor().inventory(include_stock=True)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400
    tracks={}
    for e in ents:
        if int(e.get('occurrence',0)) or e['key'].upper() not in SCR_KEYS:continue
        t=tracks.setdefault(e['track'], dict(track=e['track'], player=None, ai=None,
                                             plate=e['track'] in PLATE_TRACKS))
        side=t[e['role']] or dict(name=e['name'],arc=e['archive'],values={},stock={},lengths={})
        side['values'][e['key']]=e['value'];side['stock'][e['key']]=e.get('stock');side['lengths'][e['key']]=e['length']
        t[e['role']]=side
    rows=sorted(tracks.values(), key=lambda x:(not x['plate'], x['track']))
    return jsonify(dict(ok=True, rows=rows, keys=SCR_KEYS, count=len(ents),
                        stock_source='backup'))

@app.route('/api/scr/set', methods=['POST'])
def scr_set_api():
    q=request.get_json()
    try:
        change=dict(archive=str(q.get('arc','0')),name=q['name'],key=q['key'],
                    value=q['value'],occurrence=int(q.get('occurrence',0)))
        editor=_shared_scr_editor();result=editor.preview([change]) if q.get('dry_run') else editor.apply([change])
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400


# ---- v0.9.17 track-sorted SCR editor + atomic batch apply ----
SCR_KEY_RX=re.compile(r'^[A-Za-z][A-Za-z0-9_-]{1,95}$')

SCR_TESTED_KEYS={'FRONT-DRAFT-DRAG'}
SCR_EXISTING_KEYS=set(SCR_KEYS)
SCR_RECOMMENDED_KEYS={
    # Draft / aero
    'FRONT-DRAFT-DRAG','REAR-DRAFT-DRAG','SIDE-DRAFT-DRAG',
    'FRONT-DRAFT-DOWNFORCE','REAR-DRAFT-DOWNFORCE','OVERALL-DOWNFORCE-SCALE',
    'TAPE','SPLITTER',
    # Grip / tires
    'AI-LAT-GRIP-BOOST','MAXLATFRICTION','MAXLONGFRICTION','OPTSLIPANGLE',
    'OPTSLIPRATIO','TREADWEAR-GRADE','TREADWEAR-DATA','PRESSURE',
    # Suspension / chassis
    'SPRING-STIFFNESS','DAMPING-RATIO','REBOUND-DAMPING-RATIO',
    'ROLL-CENTRE-HEIGHT','STIFFNESS','BUMP-STOP-STRENGTH','CAMBER','TOE',
    'JOUNCE-LIMIT',
    # Brakes
    'BRAKE-BIAS','MAX-TORQUE','FADE','BRAKE-DIAMETER','BRAKE-THICKNESS',
    'BRAKE-MATERIAL',
    # Powertrain
    'FINAL-RATIO','GEARS','EFFICIENCY','RPM-TORQUE','CHANGE-UP-POINT',
    'CHANGE-DOWN-POINT','LIMITER-RANGE','MAX-TORQUE-CAPACITY',
    # Steering
    'MAX-STEERING-ANGLE','ACKERMANN',
}
SCR_DESCRIPTIONS={
    'FRONT-DRAFT-DRAG':'Tested: lower values create a stronger tow and higher drafting speed.',
    'REAR-DRAFT-DRAG':'Rear-car draft drag factor; gameplay effect is still experimental.',
    'SIDE-DRAFT-DRAG':'Side-draft drag factor; gameplay effect is still experimental.',
    'FRONT-DRAFT-DOWNFORCE':'Front downforce retained while drafting.',
    'REAR-DRAFT-DOWNFORCE':'Rear downforce retained while drafting.',
    'OVERALL-DOWNFORCE-SCALE':'Overall aerodynamic downforce scale.',
    'TAPE':'Likely grille tape / cooling-aero setup value.',
    'SPLITTER':'Likely splitter / front-aero setup value.',
    'AI-LAT-GRIP-BOOST':'Likely AI-only lateral-grip assistance or multiplier.',
    'MAXLATFRICTION':'Likely peak lateral tire grip.',
    'MAXLONGFRICTION':'Likely peak acceleration and braking tire grip.',
    'OPTSLIPANGLE':'Likely tire slip angle at peak lateral grip.',
    'OPTSLIPRATIO':'Likely tire slip ratio at peak longitudinal grip.',
    'TREADWEAR-GRADE':'Likely tire durability / wear grade.',
    'TREADWEAR-DATA':'Likely tire-wear curve data.',
    'PRESSURE':'Tire pressure for this wheel context.',
    'SPRING-STIFFNESS':'Suspension spring stiffness.',
    'DAMPING-RATIO':'Suspension damping ratio.',
    'REBOUND-DAMPING-RATIO':'Suspension rebound damping ratio.',
    'ROLL-CENTRE-HEIGHT':'Chassis roll-centre height.',
    'STIFFNESS':'Component stiffness; check the displayed context.',
    'BUMP-STOP-STRENGTH':'Bump-stop stiffness / strength.',
    'CAMBER':'Wheel camber setting.',
    'TOE':'Wheel toe setting.',
    'JOUNCE-LIMIT':'Suspension compression-travel limit.',
    'BRAKE-BIAS':'Front/rear brake balance.',
    'MAX-TORQUE':'Maximum torque; meaning depends on context (brakes, engine, etc.).',
    'FADE':'Brake fade parameter.',
    'FINAL-RATIO':'Final-drive ratio.',
    'GEARS':'Gear-count or gearbox data value; check context.',
    'EFFICIENCY':'Powertrain efficiency value.',
    'RPM-TORQUE':'Engine torque-curve point.',
    'CHANGE-UP-POINT':'Automatic upshift point.',
    'CHANGE-DOWN-POINT':'Automatic downshift point.',
    'LIMITER-RANGE':'Engine rev-limiter range.',
    'MAX-TORQUE-CAPACITY':'Maximum drivetrain torque capacity.',
    'MAX-STEERING-ANGLE':'Maximum steering lock / angle.',
    'ACKERMANN':'Ackermann steering geometry amount.',
}
def _scr_parse_numeric_rows(data):
    return _shared_scr_parse_numeric_rows(data)

def _scr_wheel(path):
    return _shared_scr_wheel(path)

def _scr_context(path,key):
    return _shared_scr_context(path)

def _scr_category(key,path):
    return _shared_scr_category(key,path)

def _scr_status(key):
    k=key.upper()
    if k in SCR_TESTED_KEYS: return 'tested'
    if k in SCR_EXISTING_KEYS: return 'existing'
    if k in SCR_RECOMMENDED_KEYS: return 'candidate'
    return 'raw'

def _scr_numeric_inventory(archive_override=None, archive_id='0', cdf_override=None, track_filter=None,
                           role_filter=None, query=None, recommended_only=False):
    """Inventory numeric Player/AI SCR fields with path and occurrence context."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    archive_id=str(archive_id)
    tf=(track_filter or '').strip().lower(); rf=(role_filter or '').strip().lower()
    q=(query or '').strip().lower(); rows=[]

    for arcid,r in sorted(reg.items(),key=lambda x:int(x[0])):
        cdfpath=cdf_override if (cdf_override and arcid==archive_id) else r['cdf']
        try: ents=parse_cdfiles(cdfpath)
        except Exception: continue
        arpath=archive_override if (archive_override and arcid==archive_id) else r['ar']
        if not os.path.exists(arpath): continue
        with open(arpath,'rb') as fh:
            for off,size,name in ents:
                role=_scr_role(name)
                if not role or not name.upper().endswith('_SCR.ARC'): continue
                track=_scr_track(name)
                if tf and track.lower()!=tf: continue
                if rf and rf!='all' and role!=rf: continue
                if size<=0 or size>4*1024*1024: continue
                fh.seek(off); data=fh.read(size)
                if b'AERODYNAMICS' not in data: continue

                for raw in _scr_parse_numeric_rows(data):
                    key=raw['key']; ku=key.upper(); path=raw['path']
                    category=_scr_category(key,path)
                    recommended=ku in SCR_RECOMMENDED_KEYS
                    context=_scr_context(path,key); wheel=_scr_wheel(path)
                    description=SCR_DESCRIPTIONS.get(ku,'')
                    searchable=' '.join((track,role,key,path,context,category,raw['value'],description)).lower()
                    if recommended_only and not recommended: continue
                    if q and q not in searchable: continue
                    occurrence=raw['occurrence']
                    ident=f'{arcid}|{name.upper()}|{ku}|{occurrence}'
                    rows.append(dict(
                        id=ident,arc=arcid,name=name,track=track,role=role,
                        key=key,value=raw['value'],length=raw['length'],
                        occurrence=occurrence,path=path,context=context,wheel=wheel,
                        category=category,recommended=recommended,status=_scr_status(key),
                        description=description,abs_off=off+raw['value_rel'],
                        entry_off=off,entry_size=size,
                        pair_id=f'{track.lower()}|{ku}|{occurrence}'
                    ))
    return rows

def _scr_row_ident(row):
    return (str(row['arc']),row['name'].upper(),row['key'].upper(),int(row['occurrence']))

def _scr_numeric_snapshot(archive_override=None, archive_id='0'):
    return {_scr_row_ident(r):r['value'] for r in _scr_numeric_inventory(archive_override,archive_id)}

def _scr_public_row(row,stock):
    r=dict(row); ident=_scr_row_ident(row)
    r['stock']=stock.get(ident) if stock else None
    r['modded']=r['stock'] is not None and r['stock']!=r['value']
    for k in ('abs_off','entry_off','entry_size'):
        r.pop(k,None)
    return r



def scr_key_batch(changes,dry_run=False):
    """Compatibility seam; the shared service owns all SCR writes."""
    editor=_shared_scr_editor()
    return editor.preview(changes) if dry_run else editor.apply(changes)

def scr_key_set(arcid,name,key,new_value,dry_run=False,occurrence=0):
    return scr_key_batch([dict(arc=arcid,name=name,key=key,
                               occurrence=occurrence,value=new_value)],dry_run=dry_run)

@app.route('/api/scr/keys')
def scr_keys_api():
    try:
        meta=request.args.get('meta')=='1'
        track=request.args.get('track','').strip()
        role=request.args.get('role','all').strip().lower()
        query=request.args.get('q','').strip()
        recommended=request.args.get('recommended')=='1'
        limit=max(1,min(5000,int(request.args.get('limit','1500'))))

        if meta:
            all_rows=_shared_scr_editor().inventory(include_stock=True)
            tracks=sorted({r['track'] for r in all_rows})
            counts={c:0 for c in SCR_CATEGORY_ORDER}
            for r in all_rows: counts[r['category']]=counts.get(r['category'],0)+1
            has_stock=any(r.get('stock') is not None for r in all_rows)
            return jsonify(dict(ok=True,tracks=tracks,total=len(all_rows),
                unique_keys=len({r['key'].upper() for r in all_rows}),
                categories=SCR_CATEGORY_ORDER,category_counts=counts,
                stock_source=('backup' if has_stock else None),has_stock=has_stock))

        rows=_shared_scr_editor().inventory(track=track,role=role,query=query,
                                            recommended_only=recommended,include_stock=True)
        total=len(rows);rows=rows[:limit];has_stock=any(r.get('stock') is not None for r in rows)
        return jsonify(dict(ok=True,rows=rows,count=total,returned=len(rows),
                            truncated=total>limit,stock_source=('backup' if has_stock else None),
                            categories=SCR_CATEGORY_ORDER,editable=True,
                            note='Same-width edits use surgical writes. Different-width values rebuild each SCR container and install them through atomic append/repoint. Every path performs a full all-key diff.'))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scr/key/set',methods=['POST'])
def scr_key_set_api():
    q=request.get_json(force=True)
    try:
        result=scr_key_set(q.get('arc','0'),q['name'],q['key'],q['value'],
                           dry_run=bool(q.get('dry_run')),
                           occurrence=int(q.get('occurrence',0)))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scr/keys/batch',methods=['POST'])
def scr_keys_batch_api():
    q=request.get_json(force=True)
    try:
        result=scr_key_batch(q.get('changes',[]),dry_run=bool(q.get('dry_run')))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end SCR DRAFT / AERO ====================



# ==================== v0.9.24 IMAGES / TEXTURES / DISCOVERY ====================
# Image index is bundled under data/ui_assets.csv.
# Bytes ALWAYS come from the live archive; the CSV is only an index.
UI_CSV = 'ui_assets.csv'
UI_PACKAGED_MAPPING_CSV = os.path.join(DATA,'ui_asset_map_v2.csv')
DISCOVERED_TEXTURE_CSV = os.path.join(DATA,'discovered_texture_assets.csv')
TEXTURE_DISCOVERY_REPORT = os.path.join(DATA,'texture_discovery_live_report.json')

def _discovered_texture_csv():
    return _profile_scoped_state(DISCOVERED_TEXTURE_CSV,'discovered_texture_assets.csv')

def _texture_discovery_report():
    return _profile_scoped_state(TEXTURE_DISCOVERY_REPORT,'texture_discovery_live_report.json')
TEXTURE_DISCOVERY_TOOL = component_path('nascar15_texture_discovery_v0_1.py')

# Confirmed, user-facing assets. png_replace_safe gates the experimental
# PNG-replace path: a family is only "safe" once export -> replace -> boot/menu
# test has actually passed for it. Everything else is export / copy-from only.
CONFIRMED_ASSETS = [
    dict(id='driver_number_card', label='Driver number card',
         entry_rx=r'^(DRIVER_\d+_3DNUM_|imgcarnumber|l_imgcarnumber)', png_replace_safe=False,
         note='Driver-select or driver-details number artwork.'),
    dict(id='driver_paint_preview', label='Driver paint preview',
         entry_rx=r'^DRIVERPAINT_', fmt='DXT5', png_replace_safe=True,
         note='Driver Select car-card preview.'),
    dict(id='paint_select_preview', label='Paint Select preview',
         entry_rx=r'^PAINTSCHEME_', png_replace_safe=False,
         note='Paint Select / custom-scheme thumbnail.'),
    dict(id='track_select_card', label='Track Select card',
         container_rx=r'^(2TRACKSELECTMENUIMAGE|3TRACKCARDIMAGE)\.ARC$', png_replace_safe=False,
         note='Track Select list/card artwork.'),
    dict(id='track_detail_bg', label='Track detail image',
         container_rx=r'^TRACKDETAILIMAGES\.ARC$', png_replace_safe=False,
         note='Track Select detail background.'),
    dict(id='calendar_track_image', label='Calendar track image',
         container_rx=r'^CALENDAR_TRACK_IMAGES\.ARC$', png_replace_safe=False,
         note='Career / Single Season calendar track tile.'),
    dict(id='lobby_track_image', label='Lobby track image',
         container_rx=r'^(2LOBBYTRACKCARDIMAGE|LOBBYTRACKCARDDETAILIMAGE)\.ARC$', png_replace_safe=False,
         note='Multiplayer lobby track artwork.'),
    dict(id='track_facts_image', label='Track Facts image',
         container_rx=r'^TRACK_FACTS_IMG_', png_replace_safe=False,
         note='Track Facts photo or icon.'),
    dict(id='hud_track_map', label='HUD track map / marker',
         container_rx=r'^MAP_', png_replace_safe=False,
         note='HUD minimap, player marker, leader marker, other-car marker, or arrow.'),
    dict(id='race_hud_candidate', label='Race HUD / gauge candidate',
         container_rx=r'.*(?:HUD|GAUGE|TACH|SPEEDO|DAMAGE|STANDING|LEADER|LAP|POSITION|OVERLAY|METER).*', png_replace_safe=False,
         note='Candidate in-race HUD art such as tachometers, vehicle-status diagrams, timing panels, or standings overlays. Exact consumers require an in-game trace test.'),
    dict(id='driver_team_select', label='Driver / team select artwork',
         container_rx=r'^(2DRIVERSELECTMENUIMAGE|2DRIVERSELECTTD_)', png_replace_safe=False,
         note='Driver Select team logo, 3D number, and paint-preview family.'),
    dict(id='career_scheme_thumb', label='Career / custom scheme thumbnail',
         container_rx=r'^(BASESCHEMETHUMBNAILS|CUSTOMSCHEMETHUMBNAILS)\.ARC$', png_replace_safe=False,
         note='Career base-scheme or custom-scheme thumbnail.'),
    dict(id='team_shop_logo', label='Team Shop logo',
         container_rx=r'^TEAMSHOPLOGO', png_replace_safe=False,
         note='Team Shop branding.'),
    dict(id='global_menu_art', label='Global menu / loading art',
         container_rx=r'^(GLOBALMENUASSETS|3LOADINGTRIVIAQUIZIMAGETEST|RACEMEDIAIMAGES)\.ARC$', png_replace_safe=False,
         note='Global interface atlas, loading trivia, or Race Media presentation art.'),
    dict(id='tire_clean_diffuse', label='Tire sidewall — clean diffuse',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^Tyre02\.dds$', png_replace_safe=False,
         note='Shared clean tire sidewall texture. Smart Import rebuilds every standard mip so branding remains visible at distance; verify globally in game.'),
    dict(id='tire_worn_diffuse', label='Tire sidewall — worn diffuse',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^Tyre02-D\.dds$', png_replace_safe=False,
         note='Shared dirty/worn tire sidewall texture. Update with the clean map; Smart Import rebuilds every standard mip so branding does not revert with wear or distance.'),
    dict(id='tire_texture_set', label='Tire normal/specular & wheel blur maps',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^(Tyre02-(?:DN|DS|N|S)\.dds|wheelblurNEW\.dds)$', png_replace_safe=False,
         note='Advanced tire normal, specular, and spinning-wheel blur resources. Standard mip chains are rebuilt; normal-map mips are re-normalized after filtering.'),
    dict(id='shared_vehicle_textures', label='Shared NASCAR vehicle textures',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', png_replace_safe=False,
         note='Engine, chassis, damage, glass, fuel, dirt, smoke, tire, wheel, and window material resources.'),
]


def ui_csv_path():
    if active_game_profile().get('graphics_mode')=='discovered': return None
    p=component_path(UI_CSV)
    return p if os.path.exists(p) else None

def _ui_recover_dims(w,h,fmt,payload_size):
    """Recover bad DXT metadata from the exact block count.

    A number of track maps/logos store approximate or corrupt pixel dimensions
    while the BC payload size is exact. Search factor pairs of the block count
    and choose the 4-pixel-aligned geometry closest to the metadata. Reject
    implausible giant/sliver candidates rather than inventing an image.
    """
    bpb=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else 0
    if not bpb or payload_size<=0 or payload_size%bpb: return None
    blocks=payload_size//bpb; cand=[]
    lim=int(blocks**0.5)
    meta_ok=1<=w<=8192 and 1<=h<=8192
    target_aspect=(w/h) if meta_ok and h else 1.0
    for a in range(1,lim+1):
        if blocks%a: continue
        b=blocks//a
        for bw,bh in ((a,b),(b,a)):
            pw,ph=bw*4,bh*4
            if pw>4096 or ph>4096: continue
            aspect=pw/ph
            if aspect < (0.01 if meta_ok else 0.08) or aspect > (100.0 if meta_ok else 12.5): continue
            aspect_pen=abs(_math.log(max(aspect,1e-9))- _math.log(max(target_aspect,1e-9)))
            size_pen=(abs(pw-w)/max(4,w)+abs(ph-h)/max(4,h)) if meta_ok else abs(_math.log(aspect))*.15
            common_pen=(0 if pw in (4,8,12,16,32,64,80,96,112,128,160,176,192,256,296,320,384,512,1024,2048,4096) else .08)
            common_pen+=(0 if ph in (4,8,12,16,32,64,80,96,104,108,112,124,128,140,156,160,192,256,320,384,512,1024,2048,4096) else .08)
            cand.append((aspect_pen*2+size_pen+common_pen,pw,ph))
    if not cand: return None
    cand.sort(); score,pw,ph=cand[0]
    if score>3.5: return None
    return pw,ph

_UI_PACKAGED_MAP_CACHE={'signature':None,'rows':{}}

def _ui_truthy(value):
    return value in (1, True, '1', 'True', 'true', 'yes', 'YES')

def _ui_payload_identity(value):
    try:
        return int(value)
    except Exception:
        return -1

def _ui_packaged_map_key(archive, container, entry, payload_abs=None):
    # Duplicate entry names are real in several native containers.  Payload
    # offset is therefore part of the identity; filename-only keys can map the
    # wrong physical resource and can turn bad parser metadata into a preview.
    return (str(archive or ''), str(container or '').upper(), str(entry or ''),
            _ui_payload_identity(payload_abs))

def _ui_bc_required_bytes(w, h, fmt):
    try:
        w=int(w); h=int(h)
    except Exception:
        return 0
    if w<=0 or h<=0:
        return 0
    bpb=8 if str(fmt).upper()=='DXT1' else 16 if str(fmt).upper()=='DXT5' else 0
    if not bpb:
        return 0
    return max(1,(w+3)//4)*max(1,(h+3)//4)*bpb

def _ui_mapping_geometry_plausible(row):
    try:
        w=int(row.get('w')); h=int(row.get('h')); ps=int(row.get('payload_size'))
    except Exception:
        return False
    needed=_ui_bc_required_bytes(w,h,row.get('fmt'))
    return bool(needed and 1<=w<=8192 and 1<=h<=8192 and ps>0 and needed<=ps)

def _ui_packaged_mapping_rank(row, ordinal):
    plausible=_ui_mapping_geometry_plausible(row)
    recovered=(str(row.get('geometry_status') or '').lower()=='recovered')
    decoded=_ui_truthy(row.get('decoded'))
    # Prefer a plausible recovered row, then any plausible decoded row.  File
    # order is only the final deterministic tie-breaker.
    return (1 if recovered and plausible else 0,
            1 if plausible else 0,
            1 if decoded else 0,
            int(ordinal))

def _ui_packaged_mappings():
    """Load the shipped full graphics map by exact physical identity.

    Duplicate names inside one ARC remain separate through payload_abs.  A
    deterministic best-row fallback is also retained for legacy/discovered rows
    that do not carry an offset, but implausible dimensions can never beat sane
    recovered geometry.
    """
    if active_game_profile().get('graphics_mode')=='discovered': return {}
    p=UI_PACKAGED_MAPPING_CSV
    if not os.path.exists(p):
        return {}
    sig=(os.path.getmtime(p),os.path.getsize(p))
    if _UI_PACKAGED_MAP_CACHE.get('signature')==sig:
        return _UI_PACKAGED_MAP_CACHE.get('rows') or {}
    rows={}; fallback={}
    with open(p,'r',encoding='utf-8-sig',newline='') as f:
        for ordinal,r in enumerate(_csv.DictReader(f)):
            exact=_ui_packaged_map_key(r.get('archive'),r.get('container'),r.get('entry'),r.get('payload_abs'))
            if not exact[1] or not exact[2]:
                continue
            rows[exact]=r
            base=exact[:3]
            rank=_ui_packaged_mapping_rank(r,ordinal)
            if base not in fallback or rank>fallback[base][0]:
                fallback[base]=(rank,r)
    # Legacy no-offset lookups use the deterministic safe winner only.
    for base,(_rank,row) in fallback.items():
        rows[base+(-1,)]=row
    _UI_PACKAGED_MAP_CACHE['signature']=sig
    _UI_PACKAGED_MAP_CACHE['rows']=rows
    return rows

_UI_INDEX_CACHE={'signature':None,'rows':None}

def _ui_index_files():
    files=[]
    p=ui_csv_path()
    if p: files.append((p,'built_in'))
    discovered=_discovered_texture_csv()
    if os.path.exists(discovered): files.append((discovered,'discovered'))
    return files

def _ui_index():
    files=_ui_index_files()
    if not files: raise RuntimeError(f'{UI_CSV} is missing from the internal tools folder')
    map_sig=(os.path.getmtime(UI_PACKAGED_MAPPING_CSV),os.path.getsize(UI_PACKAGED_MAPPING_CSV)) if os.path.exists(UI_PACKAGED_MAPPING_CSV) else None
    signature=tuple((p,os.path.getmtime(p),os.path.getsize(p)) for p,_ in files)+(('packaged_map',map_sig),)
    if _UI_INDEX_CACHE.get('rows') is not None and _UI_INDEX_CACHE.get('signature')==signature:
        return _UI_INDEX_CACHE['rows']
    packaged=_ui_packaged_mappings(); rows=[]; seen=set()
    for p,source_index in files:
        with open(p,'r',encoding='utf-8-sig',newline='') as f:
            for r in _csv.DictReader(f):
                try:
                    w=int(r['w']); h=int(r['h']); ps=int(r['payload_size'])
                    if r.get('payload_abs') not in (None,''): r['payload_abs']=int(r['payload_abs'])
                    raw_fmt=str(r.get('fmt') or '').upper()
                    # Native pixel-format 0x19 is NASCAR's quarter-field DXT1
                    # header. The clean v0.2 audit and the primary ARC parser both
                    # confirm that its logical dimensions are four times the two
                    # stored dimension fields. QuickBMS cannot describe this format,
                    # so the app's native parser is authoritative for it.
                    if raw_fmt=='FMT_0X19':
                        nw,nh=max(1,w*4),max(1,h*4)
                        if _ui_bc_required_bytes(nw,nh,'DXT1')<=ps:
                            w,h=nw,nh; r['w']=str(w); r['h']=str(h); r['fmt']='DXT1'; r['decoded']='1'
                            r['geometry_status']='native_format_25'
                    if str(r.get('container') or '').upper()=='SPRINTNUMS2015.ARC' and str(r.get('fmt') or '').upper()=='DXT1' and ps>=4096:
                        w,h=128,64
                        r['w']='128'; r['h']='64'
                except Exception:
                    continue
                identity=_ui_packaged_map_key(r.get('archive'),r.get('container'),r.get('entry'),r.get('payload_abs'))
                if identity in seen: continue
                seen.add(identity)
                seed=packaged.get(identity) or packaged.get(identity[:3]+(-1,))
                if seed and str(seed.get('fmt') or '').upper()=='FMT_0X19':
                    seed=dict(seed)
                    try:
                        sw,sh=int(seed.get('w') or w)*4,int(seed.get('h') or h)*4
                        if _ui_bc_required_bytes(sw,sh,'DXT1')<=int(seed.get('payload_size') or ps):
                            seed.update(w=str(sw),h=str(sh),fmt='DXT1',decoded='True',geometry_status='native_format_25')
                    except Exception:
                        pass
                r['source_index']=source_index
                if seed:
                    r['original_w']=int(seed.get('original_w') or w); r['original_h']=int(seed.get('original_h') or h)
                    seed_candidate=dict(seed,payload_size=seed.get('payload_size') or ps,fmt=seed.get('fmt') or r.get('fmt'))
                    seed_ok=_ui_mapping_geometry_plausible(seed_candidate)
                    if seed_ok:
                        r['w']=int(seed.get('w') or w); r['h']=int(seed.get('h') or h)
                        r['decoded']=_ui_truthy(seed.get('decoded'))
                        r['geometry_status']=str(seed.get('geometry_status') or ('indexed' if r['decoded'] else 'unresolved'))
                    else:
                        # A packaged row may be intentionally short/truncated yet
                        # still previewable by the native parser. Keep sane parser
                        # geometry, but never adopt absurd packaged dimensions.
                        originally_decoded=r.get('decoded') in ('1','True','true',1,True)
                        basic_sane=(1<=w<=8192 and 1<=h<=8192)
                        if originally_decoded and basic_sane:
                            r['w']=w; r['h']=h; r['decoded']=True; r['geometry_status']='indexed'
                        else:
                            dims=_ui_recover_dims(w,h,r.get('fmt',''),ps)
                            if dims:
                                r['w'],r['h']=dims; r['decoded']=True; r['geometry_status']='recovered'
                            else:
                                r['w']=w; r['h']=h; r['decoded']=False; r['geometry_status']='unresolved'
                    r['decode_error']=str(seed.get('decode_error') or r.get('decode_error') or '')
                    if seed.get('family'): r['family']=seed.get('family')
                else:
                    originally_decoded=r.get('decoded') in ('1','True','true',1,True)
                    r['original_w']=w; r['original_h']=h
                    if originally_decoded and _ui_mapping_geometry_plausible(r):
                        r['w']=w; r['h']=h; r['decoded']=True; r['geometry_status']='indexed'
                    else:
                        dims=_ui_recover_dims(w,h,r.get('fmt',''),ps)
                        if dims:
                            r['w'],r['h']=dims; r['decoded']=True; r['geometry_status']='recovered'
                        else:
                            r['w']=w; r['h']=h; r['decoded']=False; r['geometry_status']='unresolved'
                rows.append(r)
    _UI_INDEX_CACHE['signature']=signature; _UI_INDEX_CACHE['rows']=rows
    return rows

UI_MAPPING_FILE = os.path.join(APP_DIR,'data','ui_mapping_overrides.json')

def _ui_mapping_file():
    return _profile_scoped_state(UI_MAPPING_FILE,'ui_mapping_overrides.json')

def _ui_mapping_key(row):
    return f"{row.get('archive','')}|{str(row.get('container','')).upper()}|{row.get('entry','')}|{_ui_payload_identity(row.get('payload_abs'))}"

def _ui_legacy_mapping_key(row):
    return f"{row.get('archive','')}|{str(row.get('container','')).upper()}|{row.get('entry','')}"

def _ui_mapping_overrides():
    try:
        with open(_ui_mapping_file(),'r',encoding='utf-8') as f:
            d=json.load(f)
        return d if isinstance(d,dict) else {}
    except Exception:
        return {}

def _ui_save_mapping_overrides(d):
    atomic_write_json(_ui_mapping_file(), d, indent=2, sort_keys=True)

def _ui_pretty_name(s):
    s=re.sub(r'\.(tga|dds|png)$','',str(s),flags=re.I)
    s=re.sub(r'[_\-]+',' ',s).strip()
    return re.sub(r'\s+',' ',s)

def _ui_auto_mapping(row):
    """Map every indexed texture to a useful screen/family label.

    This is deliberately honest: filenames and container families provide a
    strong likely-use mapping, while user verification can promote an entry to
    confirmed through ui_mapping_overrides.json.
    """
    fam=(row.get('family') or '').lower()
    c=(row.get('container') or '').upper()
    e=(row.get('entry') or '')
    eu=e.upper()
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':
        if fam=='shared_vehicle_textures':
            return dict(category='Shared Vehicle Textures',screen='Shared NASCAR Vehicle / Materials',
                        role='Unsupported shared material resource',label=f'Shared vehicle resource · {e}',
                        confidence='research',note=f'{row.get("fmt") or "Unknown"} resource format is indexed, but pixel decoding is not mapped yet. Raw export only.')
        if fam=='tire_wheel_textures':
            return dict(category='Tires & Wheels',screen='Shared NASCAR Vehicle / On-track',
                        role='Unsupported tire/wheel resource',label=f'Tire / wheel resource · {e}',
                        confidence='research',note=f'{row.get("fmt") or "Unknown"} resource format is indexed, but pixel decoding is not mapped yet. Raw export only.')
        if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
            return dict(category='Driver & Character Textures',screen='Driver / Character Model',
                        role='Unsupported character resource',label=f'Character resource · {c[:-4]} · {e}',
                        confidence='research',note='Indexed character resource, but its geometry/format is not decoded safely. Raw export only.')
        return dict(category='Unresolved Binary Candidates',screen='Research / Not decoded',
                    role='Raw indexed payload',label=f'Unresolved candidate · {c[:-4]} · {row.get("entry","")}',
                    confidence='unknown',note='The scanner found an image-like record, but its geometry could not be recovered safely. Raw export only.')
    category='Unknown / Research'; screen='Unknown'; role='Unknown texture'
    label=_ui_pretty_name(e); confidence='unknown'; note='Indexed and decodable, but its exact screen still needs verification.'

    if fam=='track_select':
        category='Track & Event Art'; confidence='likely'
        if c=='TRACKDETAILIMAGES.ARC': screen='Track Select'; role='Detail background'; label=f'Track detail · {e}'
        elif c=='2TRACKSELECTMENUIMAGE.ARC':
            screen='Track Select'; role='List thumbnail / background'; label=('Track Select background' if 'IMGTRACKSELECTBG' in eu else f'Track thumbnail · {e}')
        elif c=='3TRACKCARDIMAGE.ARC': screen='Track Select'; role='Foreground card'; label=f'Track card · {e}'
        elif c=='CALENDAR_TRACK_IMAGES.ARC': screen='Career / Single Season Calendar'; role='Calendar track tile'; label=f'Calendar track · {e}'
        elif c=='2LOBBYTRACKCARDIMAGE.ARC': screen='Multiplayer Lobby'; role='Track card'; label=f'Lobby track card · {e}'
        elif c=='LOBBYTRACKCARDDETAILIMAGE.ARC': screen='Multiplayer Lobby'; role='Track detail'; label=f'Lobby track detail · {e}'
        elif c.startswith('TRACK_FACTS_IMG_'): screen='Track Facts'; role=('Track icon' if 'ICON' in eu else 'Track photo'); label=f'Track Facts {role.lower()} · {c[16:-4]}'
        elif c=='TRACKTESTING.ARC': screen='Track Testing / Debug'; role='Testing image'; label=f'Track testing · {e}'; confidence='research'
        else: screen='Track Presentation'; role='Track image'; label=f'Track presentation · {e}'
        note='Mapped from a track-presentation container family.'
    elif fam=='track_logos_maps':
        category='HUD Maps & Track Logos'; confidence='likely'
        if c.startswith('MAP_'):
            screen='In-race HUD / Minimap'
            roles={'IMG_MAP':'Track map','IMG_BLIP_PLAYER':'Player marker','IMG_BLIP_FIRST':'Leader marker','IMG_BLIP_OTHERS':'Other-car marker','IMG_ARROW':'Direction arrow'}
            role=roles.get(eu,'HUD map element'); label=f'{role} · {c[4:-4]}'
        else:
            screen='Track Presentation'; role='Track logo'; label=f'Track logo · {c[:-4]}'
        note='Mapped from the HUD map / track-logo container family.'
    elif fam=='driver_number_cards':
        category='Driver & Team Art'
        if c=='SPRINTNUMS2015.ARC':
            screen='Front-end 3D Number Model (consumer unverified)'; role='Wrapped 3D-number UV texture'; label=f'3D number texture atlas · {e}'; confidence='research'
            note=('Not a flat number card. This 128×64 asset is a native UV atlas for a front-end 3D number mesh, so it can look fragmented at gallery scale. '
                  'The exact menu consumer still needs a loud-color in-game trace test. Export and import now use the clean native payload without the old compensating shift.')
        else:
            screen='Driver Details Popup'; role='Car-number image'; label=f'Driver detail number · {e}'; confidence='likely'
            note='Mapped from the known driver-number container family.'
    elif fam=='driver_select':
        category='Driver & Team Art'; screen='Driver Select'; role='3D number graphic'; label=f'Driver Select number · {e}'; confidence='likely'; note='Mapped from DRIVER_*_3DNUM entries.'
    elif fam=='paint_scheme_preview':
        category='Paint Scheme Previews'; confidence='likely'
        if eu.startswith('DRIVERPAINT_'): screen='Driver Select'; role='Car-card preview'; label=f'Driver paint preview · {e}'
        elif eu.startswith('PAINTSCHEME_'): screen='Paint Select / Paint Booth'; role='Scheme thumbnail'; label=f'Paint scheme preview · {e}'
        else: screen='Paint Selection'; role='Scheme preview'; label=f'Paint preview · {e}'
        note='Mapped from known paint-preview naming.'
    elif fam=='team_logos':
        category='Driver & Team Art'; screen='Team Shop'; role='Team logo'; label=f'Team Shop logo · {c[:-4]}'; confidence='likely'; note='Native Team Shop texture. Export and Stock are available; generic Smart Import remains locked until the corrected writer passes an in-game confirmation test.'
    elif fam=='backgrounds_panels':
        category='Menu Backgrounds & Panels'; screen='Race Media'; role='Detail panel / background'; label=f'Race Media panel · {e}'; confidence='likely'; note='Mapped from RACEMEDIAIMAGES.'
    elif fam=='misc_ui':
        confidence='likely'
        if c=='BASESCHEMETHUMBNAILS.ARC': category='Paint Scheme Previews'; screen='Career Paint Select'; role='Base scheme thumbnail'; label=f'Career scheme thumbnail · {e}'
        elif c=='CUSTOMSCHEMETHUMBNAILS.ARC': category='Paint Scheme Previews'; screen='Custom Paint Select'; role='Custom scheme thumbnail'; label=f'Custom scheme thumbnail · {e}'
        elif c=='2DRIVERSELECTMENUIMAGE.ARC': category='Driver & Team Art'; screen='Driver Select'; role='Team tile / logo'; label=f'Driver Select team art · {e}'
        elif c=='3LOADINGTRIVIAQUIZIMAGETEST.ARC': category='Menu Backgrounds & Panels'; screen='Loading / Trivia'; role='Loading image'; label=f'Loading screen art · {e}'
        elif c=='GLOBALMENUASSETS.ARC': category='Menu Backgrounds & Panels'; screen='Global Menus'; role='Interface atlas'; label=f'Global menu atlas · {e}'
        elif c=='RACEMEDIAIMAGES.ARC': category='Menu Backgrounds & Panels'; screen='Race Media'; role='Race Media image'; label=f'Race Media · {e}'
        else: category='Menu Backgrounds & Panels'; screen='Menus'; role='UI texture'; label=f'UI art · {e}'
        note='Mapped from a known UI container family.'
    elif fam=='race_hud_textures':
        category='Race HUD & Gauges';screen='In-race HUD';confidence='research'
        text=(c+' '+eu)
        if any(k in text for k in ('TACH','RPM','REV','GAUGE','DIAL','METER')):role='Tachometer / gauge texture'
        elif any(k in text for k in ('DAMAGE','CARSTATUS','VEHICLESTATUS','TYRE','TIRE','ENGINE')):role='Vehicle damage / status widget'
        elif any(k in text for k in ('STANDING','LEADER','POSITION','INTERVAL','RUNNINGORDER')):role='Running-order / standings panel'
        elif any(k in text for k in ('LAP','TIMING','SPLIT','COUNTER')):role='Lap / timing overlay'
        else:role='Race HUD interface texture'
        label=f'{role} · {c[:-4]} · {e}';note='Discovered by HUD-oriented filename/container matching. Export and use a loud-color trace before treating the consumer as confirmed.'
    elif fam=='tire_wheel_textures':
        category='Tires & Wheels'; screen='Shared NASCAR Vehicle / On-track'; confidence='likely'
        roles={
            'TYRE02.DDS':'Clean tire diffuse / sidewall branding',
            'TYRE02-D.DDS':'Dirty/worn tire diffuse / sidewall branding',
            'TYRE02-N.DDS':'Clean tire normal map',
            'TYRE02-DN.DDS':'Dirty/worn tire normal map',
            'TYRE02-S.DDS':'Clean tire specular map',
            'TYRE02-DS.DDS':'Dirty/worn tire specular map',
            'WHEELBLURNEW.DDS':'Spinning wheel blur texture',
        }
        role=roles.get(eu,'Tire or wheel material texture'); label=f'{role} · {e}'
        note='Shared vehicle texture from NASCAR6_TEXTURES_X.ARC. Diffuse branding is likely global; first replacement still needs an in-game scope test.'
    elif fam=='shared_vehicle_textures':
        category='Shared Vehicle Textures'; screen='Shared NASCAR Vehicle / Materials'; confidence='likely'
        if 'ENGINE' in eu: role='Engine material texture'
        elif 'CHASSIS' in eu: role='Chassis material texture'
        elif 'DAMAGE' in eu: role='Damage overlay / material map'
        elif 'GLASS' in eu or 'WINDOW' in eu: role='Glass / window material texture'
        elif 'OCC' in eu: role='Occlusion map'
        elif 'FUEL' in eu or 'PETROL' in eu: role='Fuel / filler material texture'
        elif 'DIRT' in eu or 'NOISE' in eu: role='Dirt / noise material texture'
        elif 'SMOKE' in eu or 'FIRE' in eu: role='Smoke / fire effect texture'
        elif 'PAINT' in eu or 'ALLOY' in eu: role='Paint / alloy material lookup'
        else: role='Shared vehicle material texture'
        label=f'{role} · {e}'; note='Shared resource mapped from NASCAR6_TEXTURES_X.ARC. Exact consumers may cover multiple cars or visual states.'
    elif fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        category='Driver & Character Textures'; confidence='likely'
        if fam=='driver_face_textures': screen='Driver Character'; role='Face/head diffuse, normal, or specular map'
        elif fam=='driver_suit_glove_textures': screen='Driver Character'; role='Suit or glove material map'
        elif fam=='garage_character_textures': screen='Garage Character'; role='Body or cap material map'
        elif fam=='infield_character_textures': screen='Infield Character'; role='Body or cap material map'
        elif fam=='driver_head_textures': screen='Driver Character'; role='Head/portrait material map'
        else: screen='Champion Character'; role='Champion character material map'
        label=f'{role} · {c[:-4]} · {e}'; note='Previously unindexed 3D character texture discovered in Archive 3.'
    elif fam=='track_environment_textures':
        category='Track & Environment Textures'; screen='Track / Environment'; role='Track material texture'; label=f'Track texture · {c[:-4]} · {e}'; confidence='research'; note='Discovered by the live ARCC texture scanner; exact surface still needs visual verification.'
    elif fam=='discovered_texture':
        category='Discovered Textures'; screen='Unknown / Model Asset'; role='ARCC texture resource'; label=f'Discovered texture · {c[:-4]} · {e}'; confidence='research'; note='Found by the live texture scanner. Export and inspect before replacing.'
    elif fam=='unknown_visual':
        confidence='research'
        if c in ('RACESHOPREPLACETEX.ARC','INFIELDGARAGEREPLACETEX.ARC'):
            category='Character & Pit Crew Textures'; screen=('Race Shop' if c.startswith('RACE') else 'Infield Garage'); role='Pit-crew material map'; label=f'Pit crew {e.lower()}'; note='3D character material texture, not a normal menu image.'
        elif c.startswith(('DTS_','GTS_','ITS_','DF_','PMH_','CHAMP_')):
            category='Character & Pit Crew Textures'; screen='Driver / Champion Character Model'; role='Character material map'; label=f'Character texture · {c[:-4]} · {e}'; note='3D model diffuse/normal/specular or rig-associated texture.'
        elif c.startswith('LIVERY_') or eu=='IMG_LIV':
            category='Vehicle / Livery Textures'; screen='On-track Vehicle'; role='Livery texture'; label=f'Vehicle livery · {c[:-4]}'; note='Car livery atlas. Prefer the dedicated Paint Schemes workflow for normal roster paint changes.'
        elif c in ('2DRIVERDETAILSIMAGE.ARC','2DRIVERDETAILPOPUPIMAGE.ARC'):
            category='Driver & Team Art'; screen='Driver Details'; role='Background / pattern art'; label=f'Driver details art · {e}'; confidence='likely'; note='Presentation art inside the driver-details interface.'
        else:
            category='Unknown / Research'; screen='Unknown / Model Asset'; role='Decoded texture'; label=f'Research texture · {c[:-4]} · {e}'; note='Decoded successfully, but the exact consumer is not mapped yet.'

    return dict(category=category,screen=screen,role=role,label=label,confidence=confidence,note=note)

def _ui_mapping(row, overrides=None):
    m=_ui_auto_mapping(row)
    exact_key=_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))
    seed=_ui_packaged_mappings().get(exact_key) or _ui_packaged_mappings().get(exact_key[:3]+(-1,))
    if isinstance(seed,dict):
        for k in ('category','screen','role','label','note'):
            if seed.get(k): m[k]=str(seed[k])
        if seed.get('confidence'): m['confidence']=str(seed['confidence'])
        m['packaged_mapped']=True
    else:
        m['packaged_mapped']=False
    override_rows=(overrides if overrides is not None else _ui_mapping_overrides())
    ov=override_rows.get(_ui_mapping_key(row),override_rows.get(_ui_legacy_mapping_key(row),{}))
    if isinstance(ov,dict):
        for k in ('category','screen','role','label','note'):
            if ov.get(k): m[k]=str(ov[k])
        if ov.get('verified'):
            m['confidence']='confirmed'
        elif ov.get('confidence'):
            m['confidence']=str(ov['confidence'])
        m['verified']=bool(ov.get('verified'))
        m['user_mapped']=bool(ov)
    else:
        m['verified']=False; m['user_mapped']=False
    return m

def _confirmed_for(row):
    for a in CONFIRMED_ASSETS:
        if a.get('entry_rx') and not re.match(a['entry_rx'], row['entry'], re.I): continue
        if a.get('container_rx') and not re.match(a['container_rx'], row['container'], re.I): continue
        if not a.get('entry_rx') and not a.get('container_rx'): continue
        if a.get('fmt') and row.get('fmt')!=a['fmt']: continue
        return a
    return None

def _ui_category(row):
    return _ui_mapping(row).get('category','Unknown / Research')

def _ui_special_handler(row):
    c=(row.get('container') or '').upper();e=(row.get('entry') or '').upper()
    if c=='SPRINTNUMS2015.ARC':
        return 'number_card_wrap'
    if re.match(r'^(?:L_)?IMGCARNUMBER',e):
        return 'number_card_full_canvas'
    if re.match(r'^TEAMSHOPLOGO(?:2)?\.ARC$',c):
        return 'team_shop_exact'
    if e.startswith('PAINTSCHEME_'):
        return 'paint_scheme_locked'
    if re.match(r'^DRIVER_\d+_3DNUM_',e):
        return 'driver_select_3dnum_dedicated'
    return ''


def _ui_container_type(row):
    """Classify the storage recipe, not merely the artwork's likely screen."""
    c=(row.get('container') or '').upper();fam=(row.get('family') or '').lower();special=_ui_special_handler(row)
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':return 'Unresolved/raw ARCC resource'
    if special=='number_card_wrap':return 'Wrapped number-card multi-texture ARC'
    if special=='team_shop_exact':return 'Team Shop exact DXT5 ARC'
    if c.startswith('LIVERY_') or c.startswith('HDLIVERY_'):return 'Vehicle livery wrapper'
    if c=='NASCAR6_TEXTURES_X.ARC':return 'Shared vehicle ARCC texture bank'
    if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        return 'Character/model ARCC texture bank'
    if c.startswith('MAP_'):return 'HUD map texture ARC'
    return 'Indexed multi-texture ARCC'


def _ui_image_type(row,mapping=None):
    mapping=mapping or _ui_mapping(row);e=(row.get('entry') or '').upper();fam=(row.get('family') or '').lower()
    if row.get('decoded') is False:return 'raw_unknown'
    if _ui_special_handler(row)=='number_card_wrap':return 'wrapped_number_card'
    if _ui_special_handler(row)=='team_shop_exact':return 'team_shop_logo'
    if c:=((row.get('container') or '').upper()):
        if c.startswith('LIVERY_') or c.startswith('HDLIVERY_'):return 'vehicle_livery_atlas'
    if fam=='tire_wheel_textures':
        if '-N.' in e or '-DN.' in e:return 'normal_map'
        if '-S.' in e or '-DS.' in e:return 'specular_map'
        if 'WHEELBLUR' in e:return 'animated_blur_texture'
        return 'tire_diffuse'
    if any(k in e for k in ('NORMAL','_N.','-N.','_NM',' BUMP')):return 'normal_map'
    if any(k in e for k in ('SPEC','_S.','-S.','GLOSS')):return 'specular_map'
    if any(k in e for k in ('OCC','AO.','OCCLUSION')):return 'occlusion_map'
    if any(k in e for k in ('ALPHA','MASK')):return 'mask_texture'
    if mapping.get('category') in ('Menu Backgrounds & Panels','Driver & Team Art','Track & Event Art','HUD Maps & Track Logos','Race HUD & Gauges','Paint Scheme Previews'):
        return 'ui_artwork'
    if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        return 'character_material_texture'
    if fam=='shared_vehicle_textures':return 'shared_vehicle_material'
    return 'decoded_texture'


def _ui_recommended_resize_mode(row, entry=None):
    """Return the mapped default for this exact game texture.

    The public v1 build applied one global "fit and pad" rule to every graphic.
    That is wrong for fixed UV atlases and caused opaque black bars around menu
    numbers. This recommendation is based on the mapped consumer/storage family.
    """
    special=_ui_special_handler(row)
    if special in ('number_card_wrap','number_card_full_canvas'):
        # Every SPRINTNUMS entry consumes the complete 128x64 atlas, including BIG_*.
        # The game consumes the complete atlas; padding a square logo into 128x64
        # becomes visible black side bars. Exact resize is the native behavior.
        return 'stretch'
    image_type=_ui_image_type(row,_ui_mapping(row))
    if image_type in {
        'vehicle_livery_atlas','normal_map','specular_map','occlusion_map',
        'mask_texture','tire_diffuse','animated_blur_texture',
        'shared_vehicle_material','character_material_texture'
    }:
        return 'stretch'
    if _ui_short_dxt1_track_map(row,entry):
        return 'stretch'
    return 'fit'


def _ui_effective_resize_mode(row, requested=None, entry=None):
    requested=str(requested or 'auto').strip().lower()
    if requested not in IMAGE_RESIZE_MODES:
        requested='auto'
    recommended=_ui_recommended_resize_mode(row,entry)
    # Menu-number atlases are a proven special case. Do not allow the old global
    # fit setting to reintroduce black padding; the import preview reports this
    # target-aware override before anything is written.
    if _ui_special_handler(row) in ('number_card_wrap','number_card_full_canvas'):
        return 'stretch',requested,'mapped number canvas; black padding disabled'
    if requested=='auto':
        return recommended,requested,'automatic target-aware sizing'
    return requested,requested,''


def _ui_mapping_status(row,mapping=None):
    mapping=mapping or _ui_mapping(row)
    if mapping.get('verified'):
        return 'verified'
    if mapping.get('packaged_mapped'):
        return 'packaged'
    screen=str(mapping.get('screen') or '').strip().lower()
    category=str(mapping.get('category') or '').strip().lower()
    confidence=str(mapping.get('confidence') or '').strip().lower()
    if confidence not in ('unknown','research') and screen not in ('','unknown','unknown / model asset') and 'unknown' not in category:
        return 'inferred'
    return 'needs_review'


def _ui_short_dxt1_track_map(row, entry=None):
    """Return the logical dimensions for native short DXT1 HUD maps.

    Several MAP_* / IMG_MAP records advertise a non-4-aligned height (for
    example 256x107) while the physical payload omits the final BC block row.
    Treating the payload as a smaller 256x104 image changes the logical row
    layout and produces the horizontal corruption seen in the browser.  The
    game-facing image is the advertised size with the missing final block row
    padded, then cropped back to the logical height.
    """
    if str(row.get('family') or '').lower()!='track_logos_maps': return None
    if str(row.get('entry') or '').upper()!='IMG_MAP': return None
    if str(row.get('fmt') or (entry or {}).get('fmt') or '').upper()!='DXT1': return None
    try:
        ow=int(row.get('original_w') or row.get('w') or (entry or {}).get('w') or 0)
        oh=int(row.get('original_h') or row.get('h') or (entry or {}).get('h') or 0)
        ps=int(row.get('payload_size') or (entry or {}).get('payload_size') or 0)
    except Exception:
        return None
    if ow<=0 or oh<=0 or ps<=0: return None
    pw=((ow+3)//4)*4; ph=((oh+3)//4)*4
    full=(pw//4)*(ph//4)*8
    missing=full-ps
    # All known short track-map rows omit no more than one complete block row.
    if missing<=0 or missing%8 or missing>(pw//4)*8: return None
    return dict(logical_w=ow,logical_h=oh,storage_w=pw,storage_h=ph,
                full_bytes=full,payload_size=ps,missing_bytes=missing)


def _ui_logical_dims(row, entry=None):
    if str(row.get('container') or '').upper()=='SPRINTNUMS2015.ARC':
        return 128,64
    special=_ui_short_dxt1_track_map(row,entry)
    if special:return special['logical_w'],special['logical_h']
    try:return int((entry or {}).get('w') or row.get('w')),int((entry or {}).get('h') or row.get('h'))
    except Exception:return 0,0


def _ui_native_short_layout(row, entry=None):
    """Recognize bounded, block-aligned native short BC payloads.

    This restores the proven v0.9.19.1 behavior for HUD markers and extends it
    to the DXT1 track-map family.  A fresh encode may be truncated only when the
    missing portion is block-aligned and no larger than one physical block row.
    Known fatal/special families remain locked by _ui_replace_reason.
    """
    fmt=str(row.get('fmt') or (entry or {}).get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'):return None
    special=_ui_short_dxt1_track_map(row,entry)
    try:
        ps=int(row.get('payload_size') or (entry or {}).get('payload_size') or 0)
        if special:
            sw,sh=special['storage_w'],special['storage_h']
        else:
            sw=int((entry or {}).get('w') or row.get('w'));sh=int((entry or {}).get('h') or row.get('h'))
    except Exception:return None
    bpb=8 if fmt=='DXT1' else 16
    full=max(1,(sw+3)//4)*max(1,(sh+3)//4)*bpb
    missing=full-ps
    row_bytes=max(1,(sw+3)//4)*bpb
    if missing<=0 or missing%bpb or missing>row_bytes:return None
    return dict(fmt=fmt,storage_w=sw,storage_h=sh,full_bytes=full,
                payload_size=ps,missing_bytes=missing,block_bytes=bpb)


def _ui_extract_padded_level(payload, level):
    chunks=[];off=int(level['offset']);row_bytes=int(level['row_bytes']);stride=int(level['row_stride'])
    for r in range(int(level['rows'])):
        p=off+r*stride
        if p+row_bytes>len(payload):raise ValueError('native padded mip row exceeds payload')
        chunks.append(payload[p:p+row_bytes])
    return b''.join(chunks)


def _ui_decode_image(arc,entry,row,logical=False):
    """Decode one indexed texture through its exact physical storage recipe."""
    special=_ui_short_dxt1_track_map(row,entry) if logical else None
    if special:
        pa=int(entry['payload_abs']);ps=int(entry['payload_size'])
        payload=bytes(arc[pa:pa+ps]);payload+=b'\0'*(special['full_bytes']-len(payload))
        arr=C._dxt1_decode(payload,special['storage_w'],special['storage_h'])
        return Image.fromarray(arr,'RGB').convert('RGBA').crop((0,0,special['logical_w'],special['logical_h']))
    recipe=_ui_layout_recipe(row,entry)
    if recipe.get('kind') in ('bms_padded_mips','row_padded_mips'):
        pa=int(entry['payload_abs']);ps=int(entry['payload_size']);payload=bytes(arc[pa:pa+ps])
        level=recipe['layout'][0];tight=_ui_extract_padded_level(payload,level)
        w,h=int(level['width']),int(level['height']);dw=((w+3)//4)*4;dh=((h+3)//4)*4
        if entry['fmt']=='DXT1':img=Image.fromarray(C._dxt1_decode(tight,dw,dh),'RGB').convert('RGBA')
        else:
            if entry.get('dxt5_swapped'):tight=C.swap_dxt5_halves(tight)
            img=Image.fromarray(C.dxt5_decode(tight,dw,dh),'RGBA')
        return img.crop((0,0,w,h))
    return C.multi_read_png(arc,entry).convert('RGBA')


def _ui_pad_storage_image(img,row,entry):
    special=_ui_short_dxt1_track_map(row,entry)
    if not special:return img
    src=img.convert('RGB')
    canvas=Image.new('RGB',(special['storage_w'],special['storage_h']),(0,0,0))
    canvas.paste(src,(0,0))
    return canvas


def _ui_layout_signature(row, entry=None):
    """Return the exact native-storage compatibility signature for donor copies.

    Dimensions and payload size alone are insufficient: PAINTSCHEME, number-card,
    padded-mip and dedicated resources can share byte counts while requiring
    different consumers or transforms.  A raw donor is compatible only when the
    complete physical recipe and the protected semantic class agree.
    """
    recipe=_ui_layout_recipe(row,entry); special=_ui_special_handler(row)
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'))
        ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:
        w=h=ps=0
    protected=(special or 'generic')
    # Keep semantically distinct but physically similar resources separated.
    if protected=='number_card_full_canvas': protected='number_card_full_canvas'
    elif protected=='number_card_wrap': protected='number_card_wrap'
    elif protected=='paint_scheme_locked': protected='paint_scheme'
    elif protected=='driver_select_3dnum_dedicated': protected='driver_3dnum'
    elif protected=='team_shop_exact': protected='team_shop'
    elif str(row.get('container') or '').upper().startswith('LIVERY_'): protected='livery_sd'
    elif str(row.get('container') or '').upper().startswith('HDLIVERY_'): protected='livery_hd'
    kind=recipe.get('kind') or 'unknown'; levels=int(recipe.get('levels') or 0)
    layout=[]
    for lv in recipe.get('layout') or []:
        if isinstance(lv,dict):
            layout.append((int(lv.get('width') or 0),int(lv.get('height') or 0),
                           int(lv.get('row_bytes') or 0),int(lv.get('rows') or 0),
                           int(lv.get('row_stride') or 0),int(lv.get('span') or 0)))
    if kind=='physical_canvas':
        layout.append((int(recipe.get('storage_width') or 0),int(recipe.get('storage_height') or 0),0,0,0,0))
    return (protected,kind,fmt,w,h,ps,levels,tuple(layout))


def _ui_replacement_route(row,confirmed=None,mapping=None):
    mapping=mapping or _ui_mapping(row)
    safe=_ui_safety(row,confirmed,mapping)
    if bool(row.get('decoded')) and not _ui_replace_reason(row) and row.get('fmt') in ('DXT1','DXT5'):
        return 'specialized_png' if _ui_special_handler(row) else 'smart_png'
    # Every indexed physical payload can still be changed through exact-size raw
    # import. This is intentionally separate from PNG decoding/geometry claims.
    return 'exact_raw'


def _ui_policy_label(safety):
    return {'safe_replace':'Ready to import','guarded_replace':'Import — check in game','copy_only':'Copy / restore only','read_only':'Advanced file'} .get(safety,'Advanced file')


def _ui_needed(w,h,fmt):
    return _ui_bc_required_bytes(w,h,fmt)


def _ui_native_padded_layout(row, entry=None):
    """Recognize the row/mip padding used by the stock ARC texture writer.

    This is the reverse of the NASCAR 13/14 QuickBMS extractor validated against
    the clean NASCAR 15 v0.2 image audit.  Small BC rows are stored at a minimum
    stride of 32 blocks, and many mipmapped resources reserve at least 1024
    blocks per level.  The function returns a byte-exact recipe only when the
    calculated storage size equals the indexed payload exactly.
    """
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'): return None
    try:
        w=int((entry or {}).get('w') or row.get('w')); h=int((entry or {}).get('h') or row.get('h'))
        payload_size=int((entry or {}).get('payload_size') or row.get('payload_size'))
        hint=int((entry or {}).get('mip_count') or row.get('mip_count') or 0)
    except Exception:
        return None
    if w<=0 or h<=0 or payload_size<=0:return None
    bpb=8 if fmt=='DXT1' else 16; row_floor=bpb*32; mip_floor=bpb*1024
    # A real mip chain reaches 1x1 once. Allowing repeated 1x1 levels can make
    # an unrelated larger physical canvas appear to be a padded 17-level chain.
    natural_levels=1
    tw,th=w,h
    while (tw>1 or th>1) and natural_levels<20:
        tw=max(1,tw//2);th=max(1,th//2);natural_levels+=1
    # The native header's mip count is authoritative when it is sane. Only
    # infer the count for legacy index rows that do not carry the header byte.
    levels_to_try=([hint] if 1<=hint<=natural_levels else list(range(1,natural_levels+1)))
    for policy in ('quickbms','row_only'):
        for levels in levels_to_try:
            off=0; layout=[]
            for level in range(levels):
                lw=max(1,w>>level); lh=max(1,h>>level)
                row_bytes=max(1,(lw+3)//4)*bpb; rows=max(1,(lh+3)//4)
                row_stride=max(row_bytes,row_floor)
                occupied=row_stride*rows
                span=max(occupied,mip_floor) if policy=='quickbms' else occupied
                layout.append(dict(level=level,width=lw,height=lh,offset=off,
                                   row_bytes=row_bytes,rows=rows,row_stride=row_stride,
                                   occupied=occupied,span=span))
                off+=span
            if off==payload_size:
                return dict(kind=('bms_padded_mips' if policy=='quickbms' else 'row_padded_mips'),
                            levels=levels,total_bytes=off,fmt=fmt,width=w,height=h,
                            row_floor=row_floor,mip_floor=(mip_floor if policy=='quickbms' else 0),
                            layout=layout,header_mip_count=hint)
    return None


def _ui_physical_canvas_layout(row, entry=None):
    """Recognize a larger tight BC canvas carrying a smaller logical image.

    Five stock DXT5 track maps use this form (for example logical 256x156 in a
    physical 256x256 payload). Only candidates with an exact block count and
    modest aligned padding are accepted.
    """
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'):return None
    c=str(row.get('container') or '').upper();fam=str(row.get('family') or '').lower()
    if not (c.startswith('MAP_') or fam in ('track_logos_maps','track_image')):return None
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'))
        ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:return None
    bpb=8 if fmt=='DXT1' else 16; bw=max(1,(w+3)//4)
    if ps<=0 or ps%bpb or (ps//bpb)%bw:return None
    bh=(ps//bpb)//bw; ph=bh*4; pw=bw*4
    if pw<w or ph<h:return None
    next_pow2=1
    while next_pow2<h:next_pow2*=2
    # The validated MAP_* cases are logical crops in the next power-of-two
    # surface. Do not reinterpret arrays/cubemaps as very tall canvases.
    if ph!=next_pow2 or pw!=((w+3)//4)*4:return None
    return dict(kind='physical_canvas',levels=1,total_bytes=ps,fmt=fmt,
                logical_width=w,logical_height=h,storage_width=pw,storage_height=ph)


def _ui_layout_recipe(row, entry=None):
    special=_ui_special_handler(row);c=str(row.get('container') or '').upper();e=str(row.get('entry') or '').upper()
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if _ui_truthy(row.get('write_blocked')) or _ui_truthy(row.get('overlap_next')) or _ui_truthy(row.get('overlap_previous')):
        return dict(kind='overlapping_payload',writable=False,note='This payload overlaps another indexed resource; PNG writing is blocked.')
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':
        return dict(kind='unresolved',writable=False,note='Geometry is unresolved.')
    if fmt not in ('DXT1','DXT5'):
        return dict(kind='unsupported_format',writable=False,note=f'{fmt or "unknown"} has no validated PNG writer.')
    if special=='paint_scheme_locked':
        return dict(kind='paint_scheme_native_clone',writable=False,note='PAINTSCHEME remains on the dedicated native-clone path.')
    if special=='driver_select_3dnum_dedicated':
        return dict(kind='driver_3dnum_dedicated',writable=False,note='3DNUM uses the dedicated current-team writer.')
    if c.startswith('HDLIVERY_LENOVO'):
        return dict(kind='nonstandard_hd_livery',writable=False,note='Nonstandard SD-sized Lenovo HD wrapper is unproven.')
    if c.startswith('LIVERY_') or c.startswith('HDLIVERY_') or e=='IMG_LIV':
        return dict(kind='dedicated_livery',writable=False,note='Use Paint Schemes so paired SD/HD offsets and native mips stay synchronized.')
    short=_ui_native_short_layout(row,entry)
    if short:return dict(kind='native_short',writable=True,levels=1,layout=short)
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'));ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:
        return dict(kind='invalid_metadata',writable=False,note='Invalid dimensions or payload size.')
    tight=_ui_infer_mips(w,h,fmt,ps)
    try: header_mips=int((entry or {}).get('mip_count') or row.get('mip_count') or 0)
    except Exception: header_mips=0
    if tight and (not header_mips or tight==header_mips):
        return dict(kind=('tight_base' if tight==1 else 'tight_mips'),writable=True,levels=tight)
    padded=_ui_native_padded_layout(row,entry)
    if padded:return dict(padded,writable=True)
    canvas=_ui_physical_canvas_layout(row,entry)
    if canvas:return dict(canvas,writable=True)
    return dict(kind='ambiguous_payload',writable=False,
                note='Payload does not match a tight surface, stock padded-mip recipe, bounded short layout, or validated physical canvas.')


def _ui_replace_reason(row):
    recipe=_ui_layout_recipe(row)
    special=_ui_special_handler(row)
    if recipe.get('writable'):
        return ''
    if special=='paint_scheme_locked':
        return ('PAINTSCHEME thumbnails are structurally mapped, but PNG re-encoding previously caused in-game fatals. '
                'Use the dedicated Paint Schemes thumbnail/native-clone workflow.')
    if special=='driver_select_3dnum_dedicated':
        return ('Use Specialized Import for the driver’s current-team 3D-number resource. Historical/team copies remain copy/raw-only.')
    return recipe.get('note') or 'This native layout is not approved for PNG replacement.'


def _ui_safety(row,confirmed=None,mapping=None):
    mapping=mapping or _ui_mapping(row)
    reason=_ui_replace_reason(row)
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved': return 'read_only'
    if row.get('fmt') not in ('DXT1','DXT5'): return 'read_only'
    if reason: return 'copy_only'
    if _ui_special_handler(row)=='number_card_wrap': return 'safe_replace'
    if _ui_special_handler(row) in ('number_card_full_canvas','team_shop_exact'): return 'guarded_replace'
    if mapping.get('verified') or (confirmed and confirmed.get('png_replace_safe')):
        return 'safe_replace'
    return 'guarded_replace'


def _ui_display_transform(img,row):
    if _ui_special_handler(row)=='number_card_wrap':
        return _numcard_unroll(img.convert('RGBA'))
    return img

def _ui_content_bbox(img, alpha_first=True, threshold=18):
    a=np.asarray(img.convert('RGBA'))
    rgb=a[:,:,:3].max(axis=2)
    alpha=a[:,:,3]
    mask=((alpha>8)&(rgb>threshold)) if alpha_first else (rgb>threshold)
    if int(mask.sum())<4:
        return None
    ys,xs=np.where(mask)
    return (int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1)

def _ui_fit_preview_crop(img, canvas_size, max_content, alpha_first=True, threshold=18, nearest=False):
    src=img.convert('RGBA'); box=_ui_content_bbox(src,alpha_first=alpha_first,threshold=threshold)
    if box:
        x0,y0,x1,y1=box; pad=2
        x0=max(0,x0-pad); y0=max(0,y0-pad); x1=min(src.width,x1+pad); y1=min(src.height,y1+pad)
        src=src.crop((x0,y0,x1,y1))
    mw,mh=max_content
    scale=min(mw/max(1,src.width),mh/max(1,src.height))
    nw=max(1,int(round(src.width*scale))); nh=max(1,int(round(src.height*scale)))
    filt=(Image.Resampling.NEAREST if nearest and hasattr(Image,'Resampling') else
          Image.NEAREST if nearest else
          Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS)
    src=src.resize((nw,nh),filt)
    canvas=Image.new('RGBA',canvas_size,(0,0,0,0))
    canvas.alpha_composite(src,((canvas.width-nw)//2,(canvas.height-nh)//2))
    return canvas

def _ui_hud_sprite_preview(img,row):
    """Center wraparound HUD sprites for browser display only.

    Several 64x64 blip/arrow textures are stored against a repeat seam.  The raw
    surface can show half the marker at each edge even though the game samples it
    correctly.  Roll only the preview when alpha touches both edges; exports and
    installed bytes remain native.
    """
    src=img.convert('RGBA'); a=np.asarray(src)
    alpha=a[:,:,3]; edge=max(2,src.width//8)
    if alpha[:,:edge].max()>8 and alpha[:,-edge:].max()>8:
        a=np.roll(a,src.width//2,axis=1); src=Image.fromarray(a,'RGBA')
    return _ui_fit_preview_crop(src,(112,112),(86,86),alpha_first=True,threshold=8,nearest=True)

# Number-card previews now use the exact native image.  No display roll or
# quarter-turn is applied; that old correction hid the public parser's +40-byte
# offset error and made externally extracted templates disagree with the app.
NUMCARD_PREVIEW_ROTATE=0

def _ui_number_card_preview(img):
    return _ui_fit_preview_crop(img.convert('RGBA'),(224,112),(208,96),
                                alpha_first=False,threshold=20,nearest=True)

def _ui_preview_mode(row):
    if _ui_special_handler(row)=='number_card_wrap': return 'wrapped_number_card'
    if _ui_short_dxt1_track_map(row): return 'short_dxt1_track_map'
    c=str(row.get('container') or '').upper(); e=str(row.get('entry') or '').upper()
    if c.startswith('MAP_') and e in ('IMG_BLIP_PLAYER','IMG_BLIP_FIRST','IMG_BLIP_OTHERS','IMG_ARROW'):
        return 'hud_sprite'
    if row.get('geometry_status')=='recovered': return 'recovered_geometry'
    return 'standard'

def _ui_thumb_transform(img,row):
    mode=_ui_preview_mode(row)
    if mode=='wrapped_number_card': return _ui_number_card_preview(img)
    if mode=='hud_sprite': return _ui_hud_sprite_preview(img,row)
    return _ui_display_transform(img,row)


def _ui_storage_transform(img,row):
    if _ui_special_handler(row)=='number_card_wrap':
        return _numcard_reroll(img.convert('RGBA'))
    return img

def _ui_csv_row(arcid, container, entry_name, payload_abs=None):
    matches=[r for r in _ui_index()
             if r['archive']==str(arcid) and r['container']==container and r['entry']==entry_name]
    if payload_abs is not None:
        wanted=int(payload_abs)
        exact=[r for r in matches if int(r.get('payload_abs',-1))==wanted]
        if len(exact)==1:return exact[0]
        if len(exact)>1:raise ValueError(f'{entry_name}: duplicate indexed rows share payload offset 0x{wanted:X}')
        if matches:raise ValueError(f'{entry_name}: selected payload offset 0x{wanted:X} is not present in the image index')
    return matches[0] if matches else None

def _ui_apply_indexed_geometry(entry,row,arc):
    """Use the packaged/indexed payload geometry when parser metadata is bad.

    Some native HUD maps and track logos contain non-block-aligned metadata such
    as 256x107 even though their exact BC payload is 256x104.  The generic parser
    can expose the bad dimensions again, undoing recovery and causing reshape
    errors.  The index geometry is authoritative after validating the payload
    range against the live container.
    """
    if not row:
        return entry
    try:
        sprintnums=(str(row.get('container') or '').upper()=='SPRINTNUMS2015.ARC')
        pa=int(entry.get('payload_abs') if sprintnums else row.get('payload_abs'))
        ps=int(entry.get('payload_size') if sprintnums else row.get('payload_size'))
        rw,rh=((128,64) if sprintnums else (int(row.get('w')),int(row.get('h'))))
        fmt=('DXT1' if sprintnums else str(row.get('fmt') or entry.get('fmt')))
    except Exception:
        return entry
    if pa<0 or ps<=0 or pa+ps>len(arc):
        return entry
    needed=_ui_bc_required_bytes(rw,rh,fmt)
    # Never let packaged metadata override a parser result unless the claimed
    # BC surface can physically fit inside the exact indexed payload.
    if not needed or needed>ps or rw>8192 or rh>8192:
        return entry
    if (row.get('geometry_status')=='recovered' or int(entry.get('w',0))!=rw or
        int(entry.get('h',0))!=rh or int(entry.get('payload_size',0))!=ps or
        int(entry.get('payload_abs',-1))!=pa):
        fixed=dict(entry); fixed.update(w=rw,h=rh,fmt=fmt,payload_abs=pa,payload_size=ps)
        fixed['needed']=needed
        return fixed
    return entry

def _ui_load_entry(arcid, container, entry_name, w=None, h=None, pristine=False, payload_abs=None):
    """Read one exact texture payload from an indexed ARC container.

    When payload_abs is supplied (bulk mode), both CSV and parsed-container
    resolution must honor that exact offset. This prevents duplicate names or
    aliases from silently resolving to the first entry in a container.
    """
    g,reg=registry()
    off,size=find_entry(reg, str(arcid), container, pristine=pristine)
    path = reg[str(arcid)]['bak'] if pristine else reg[str(arcid)]['ar']
    with open(path,'rb') as f:
        f.seek(off); arc=f.read(size)

    row=_ui_csv_row(arcid, container, entry_name, payload_abs=payload_abs)
    kd=(int(row['w']),int(row['h'])) if row else ((w,h) if (w and h) else None)
    parse_err=None
    try:
        ents,_base=C.parse_multi_arc(arc,known_dims=kd)
        named=[e for e in ents if e['name']==entry_name]
        if payload_abs is not None:
            wanted=int(payload_abs)
            exact=[e for e in named if int(e.get('payload_abs',-1))==wanted]
            if len(exact)==1:return arc,_ui_apply_indexed_geometry(exact[0],row,arc),off,size
            if len(exact)>1:raise ValueError(f'{entry_name}: parsed duplicates share payload offset 0x{wanted:X}')
            # The CSV may know a payload the generic parser does not expose.
            # Fall through to the exact CSV geometry instead of taking named[0].
            parse_err=f'parser did not expose selected payload 0x{wanted:X}'
        elif len(named)==1:
            return arc,_ui_apply_indexed_geometry(named[0],row,arc),off,size
        elif named and row:
            wanted=int(row.get('payload_abs',-1))
            exact=[e for e in named if int(e.get('payload_abs',-2))==wanted]
            if len(exact)==1:return arc,_ui_apply_indexed_geometry(exact[0],row,arc),off,size
            parse_err=f'container has {len(named)} entries by that name'
        else:
            parse_err='container parsed, but no entry by that name'
    except Exception as pe:
        parse_err=f'container parse failed: {pe}'

    if row:
        e=dict(name=entry_name,w=int(row['w']),h=int(row['h']),fmt=row['fmt'],
               payload_abs=int(row['payload_abs']),payload_size=int(row['payload_size']))
        e['needed']=_ui_needed(e['w'],e['h'],e['fmt'])
        if e['payload_abs']<0 or e['payload_abs']+e['payload_size']>len(arc):
            raise ValueError(f'{entry_name}: payload outside container ({len(arc)} bytes) - stale ui_assets.csv?')
        if payload_abs is not None and e['payload_abs']!=int(payload_abs):
            raise ValueError(f'{entry_name}: exact payload offset mismatch')
        return arc,e,off,size
    raise ValueError(f'{entry_name} not found in {container} (no ui_assets.csv row; {parse_err})')

def _ui_only_payload_changed(old, new, pa, ps):
    """Strongest possible guard: every byte outside the target payload window
    must be identical, and the container size must not change."""
    if len(new)!=len(old): return 'container size changed; refused'
    if old[:pa]!=new[:pa]: return 'bytes before the payload changed; refused'
    if old[pa+ps:]!=new[pa+ps:]: return 'bytes after the payload changed; refused'
    return None

def _ui_install(arcid, entry_off, entry_size, new_arc):
    """Transactionally install one same-size container and verify exact readback.

    Image writes are user-facing and happen inside large shared archives. A short
    write, disk error, or antivirus interruption must not leave half of a menu bank
    changed. The original container is captured before the first live write and is
    restored with fsync + readback if anything fails.
    """
    _g,reg=registry(); v=reg[str(arcid)]
    live=v['ar']; bak=v['bak']
    if len(new_arc)!=int(entry_size):
        raise ValueError('container size changed; refused')
    with open(live,'rb') as fh:
        fh.seek(int(entry_off)); old_arc=fh.read(int(entry_size))
    if len(old_arc)!=int(entry_size):
        raise ValueError('could not read the complete live container before writing')
    ensure_backup(live,bak)
    try:
        with open(live,'r+b') as fh:
            fh.seek(int(entry_off)); fh.write(new_arc); fh.flush(); os.fsync(fh.fileno())
        with open(live,'rb') as fh:
            fh.seek(int(entry_off)); check=fh.read(int(entry_size))
        if check!=new_arc:
            raise ValueError('container readback mismatch after image install')
        return True
    except Exception as install_ex:
        try:
            with open(live,'r+b') as fh:
                fh.seek(int(entry_off)); fh.write(old_arc); fh.flush(); os.fsync(fh.fileno())
            with open(live,'rb') as fh:
                fh.seek(int(entry_off)); restored=fh.read(int(entry_size))
            if restored!=old_arc:
                raise ValueError('rollback readback mismatch')
        except Exception as rollback_ex:
            raise RollbackFailed(install_ex,rollback_ex) from install_ex
        raise


def _ui_infer_mips(w,h,fmt,payload_size):
    """Infer how many standard BC mip levels fit the indexed payload.

    Returns None when the payload uses padding or a non-standard layout. This is
    informational only; Smart Import never changes the target wrapper/layout.
    """
    bpb=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else None
    if not bpb: return None
    total=0; levels=0; cw=max(1,int(w)); ch=max(1,int(h))
    while levels<20:
        total+=max(1,(cw+3)//4)*max(1,(ch+3)//4)*bpb
        levels+=1
        if total==int(payload_size): return levels
        if total>int(payload_size) or (cw==1 and ch==1): break
        cw=max(1,cw//2); ch=max(1,ch//2)
    return None


def _ui_full_standard_mip_layout(w,h,fmt):
    """Return (levels,total_bytes) for the complete standard BC chain to 1×1."""
    levels=0; total=0; cw=max(1,int(w)); ch=max(1,int(h))
    while levels<20:
        total+=_ui_bc_level_size(cw,ch,fmt); levels+=1
        if cw==1 and ch==1: break
        cw=max(1,cw//2); ch=max(1,ch//2)
    return levels,total


def _ui_profile(row,e,confirmed=None):
    safe=_ui_safety(row,confirmed);lw,lh=_ui_logical_dims(row,e);short=_ui_native_short_layout(row,e);recipe=_ui_layout_recipe(row,e)
    return dict(
        profile=(confirmed['id'] if confirmed else 'unmapped_ui_texture'),
        safety=safe,
        width=int(lw or e['w']),height=int(lh or e['h']),codec=e['fmt'],
        storage_width=int(e['w']),storage_height=int(e['h']),
        alpha_supported=(e['fmt']=='DXT5'),
        payload_size=int(e['payload_size']),
        base_level_size=(short['full_bytes'] if short else (_ui_needed(e['w'],e['h'],e['fmt']) if e['fmt'] in ('DXT1','DXT5') else None)),
        inferred_mips=(None if short else _ui_infer_mips(e['w'],e['h'],e['fmt'],e['payload_size'])),
        native_short_payload=bool(short),short_payload_bytes=(short['missing_bytes'] if short else 0),
        resize_default=_ui_recommended_resize_mode(row,e),
        wrapper_preserved=True,
        png_replace_safe=(safe=='safe_replace'),
        special_handler=_ui_special_handler(row),
        replace_reason=_ui_replace_reason(row),
        layout_recipe=recipe.get('kind'),native_mip_levels=recipe.get('levels'),
        audit_policy='clean-game image map v1.0.1',
    )


def _ui_bc_level_size(w,h,fmt):
    block_bytes=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else None
    if block_bytes is None: raise ValueError(f'unsupported BC codec {fmt}')
    return max(1,(int(w)+3)//4)*max(1,(int(h)+3)//4)*block_bytes


def _ui_resize_mip_image(base,size,image_type=''):
    """Downsample one texture level.

    Normal maps are re-normalized after filtering so lower tire/material mips do
    not flatten lighting. Diffuse/specular/UI textures use a normal BOX filter.
    """
    box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
    out=base.resize(tuple(map(int,size)),box)
    if image_type!='normal_map': return out
    rgba=np.asarray(out.convert('RGBA')).astype(np.float32)
    vec=rgba[:,:,:3]/127.5-1.0
    length=np.linalg.norm(vec,axis=2,keepdims=True)
    length=np.where(length<1e-6,1.0,length)
    vec=vec/length
    rgb=np.clip((vec+1.0)*127.5,0,255).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb,rgba[:,:,3].astype(np.uint8)]),'RGBA')


def _ui_encode_mip_level(img,fmt,dxt5_swapped=False):
    enc=encode_any(img.convert('RGBA' if fmt=='DXT5' else 'RGB'),fmt)
    if fmt=='DXT5' and dxt5_swapped:
        enc=C.swap_dxt5_halves(enc)
    if fmt not in ('DXT1','DXT5'):
        raise ValueError(f'unsupported BC codec {fmt}')
    return enc


def _ui_encode_standard_mip_chain(img,w,h,fmt,levels,image_type='',dxt5_swapped=False):
    """Encode an exact standard BC mip chain from the imported base image."""
    levels=int(levels); w=int(w); h=int(h)
    if levels<1: raise ValueError('mip level count must be positive')
    base=img.convert('RGBA' if fmt=='DXT5' else 'RGB')
    chunks=[]; dims=[]
    for level in range(levels):
        lw=max(1,w>>level); lh=max(1,h>>level)
        level_img=base if level==0 and base.size==(lw,lh) else _ui_resize_mip_image(base,(lw,lh),image_type)
        encoded=_ui_encode_mip_level(level_img,fmt,dxt5_swapped=dxt5_swapped)
        need=_ui_bc_level_size(lw,lh,fmt)
        if len(encoded)<need:
            raise ValueError(f'{fmt} mip L{level} encoded short: {len(encoded)} < {need}')
        chunks.append(encoded[:need]); dims.append([lw,lh])
    return b''.join(chunks),dims


def _ui_encode_native_padded_chain(img,fmt,recipe,old_payload,image_type='',dxt5_swapped=False):
    """Encode all logical mip bytes into a stock padded payload.

    Padding bytes remain byte-identical to stock/live data. Only actual BC rows
    are replaced, which is stricter than rebuilding the padding with zeros.
    """
    base=img.convert('RGBA' if fmt=='DXT5' else 'RGB'); out=bytearray(old_payload); dims=[];written=0
    for level in recipe['layout']:
        lw,lh=int(level['width']),int(level['height'])
        li=base if level['level']==0 and base.size==(lw,lh) else _ui_resize_mip_image(base,(lw,lh),image_type)
        enc=_ui_encode_mip_level(li,fmt,dxt5_swapped=dxt5_swapped)
        need=_ui_bc_level_size(lw,lh,fmt)
        if len(enc)<need:raise ValueError(f'{fmt} padded mip L{level["level"]} encoded short')
        row_bytes=int(level['row_bytes']);rows=int(level['rows']);stride=int(level['row_stride']);off=int(level['offset'])
        if row_bytes*rows!=need:raise ValueError(f'padded mip L{level["level"]} row geometry mismatch')
        for r in range(rows):
            src=r*row_bytes;dst=off+r*stride
            if dst+row_bytes>len(out):raise ValueError(f'padded mip L{level["level"]} exceeds target payload')
            out[dst:dst+row_bytes]=enc[src:src+row_bytes];written+=row_bytes
        dims.append([lw,lh])
    return bytes(out),dims,written


def _ui_encode_physical_canvas(img,fmt,recipe,old_payload,dxt5_swapped=False):
    sw,sh=int(recipe['storage_width']),int(recipe['storage_height'])
    logical=img.convert('RGBA' if fmt=='DXT5' else 'RGB')
    canvas=Image.new('RGBA' if fmt=='DXT5' else 'RGB',(sw,sh),(0,0,0,0) if fmt=='DXT5' else (0,0,0))
    canvas.paste(logical,(0,0))
    enc=_ui_encode_mip_level(canvas,fmt,dxt5_swapped=dxt5_swapped)
    if len(enc)!=len(old_payload):raise ValueError(f'physical-canvas encoder produced {len(enc)} bytes; expected {len(old_payload)}')
    return enc,[[sw,sh]],len(enc)


def _ui_prepare_encoded(q,arc,e,safe,row):
    """Decode, resize and encode one audit-approved native texture profile."""
    raw=_b64.b64decode(q.get('image') or q.get('png') or '')
    if not raw: raise ValueError('no imported image data')
    img=Image.open(io.BytesIO(raw));source_format=(img.format or 'unknown').upper();img.load()
    preserve_alpha=(e['fmt']=='DXT5')
    logical_w,logical_h=_ui_logical_dims(row,e)
    effective_mode,requested_mode,mode_reason=_ui_effective_resize_mode(row,q.get('resize_mode','auto'),e)
    img,prep=prepare_import_image(img,(logical_w or e['w'],logical_h or e['h']),effective_mode,preserve_alpha=preserve_alpha)
    prep.update(requested_mode=requested_mode,effective_mode=effective_mode,target_aware=bool(mode_reason))
    if mode_reason:prep['resize_reason']=mode_reason
    img=_ui_storage_transform(img,row);prep['target_codec']=e['fmt'];prep['target_payload_size']=int(e['payload_size'])
    if _ui_special_handler(row)=='number_card_wrap':prep['special_transform']='number-card wraparound re-applied for game storage'
    elif _ui_special_handler(row)=='number_card_full_canvas':prep['special_transform']='complete mapped number canvas used; black padding disabled'
    prep['alpha_action']=(('preserved' if prep.get('source_alpha') else 'opaque; target supports alpha') if e['fmt']=='DXT5' else ('flattened to opaque RGB' if prep.get('source_alpha') else 'not present'))
    recipe=_ui_layout_recipe(row,e)
    if not recipe.get('writable'):raise ValueError(recipe.get('note') or 'native layout is not approved for PNG replacement')
    target_size=int(e['payload_size']);pa=int(e['payload_abs']);old_payload=bytes(arc[pa:pa+target_size])
    image_type=_ui_image_type(row,_ui_mapping(row));mip_dims=[];written=0
    kind=recipe['kind']
    if kind in ('tight_base','tight_mips'):
        levels=int(recipe.get('levels') or 1)
        enc,mip_dims=_ui_encode_standard_mip_chain(img,e['w'],e['h'],e['fmt'],levels,image_type,dxt5_swapped=bool(e.get('dxt5_swapped')))
        if len(enc)!=target_size:raise ValueError(f'generated tight mip chain is {len(enc)} bytes; expected {target_size}')
        payload=enc;written=len(enc)
        layout_note=('exact base-level payload' if levels==1 else f'complete {levels}-level tight mip chain regenerated')
    elif kind in ('bms_padded_mips','row_padded_mips'):
        payload,mip_dims,written=_ui_encode_native_padded_chain(img,e['fmt'],recipe,old_payload,image_type,dxt5_swapped=bool(e.get('dxt5_swapped')))
        layout_note=f'complete {recipe["levels"]}-level {kind.replace("_"," ")} regenerated; native row/mip padding preserved byte-for-byte'
    elif kind=='physical_canvas':
        payload,mip_dims,written=_ui_encode_physical_canvas(img,e['fmt'],recipe,old_payload,dxt5_swapped=bool(e.get('dxt5_swapped')))
        layout_note=f'logical {logical_w}x{logical_h} image placed in exact {recipe["storage_width"]}x{recipe["storage_height"]} physical canvas'
    elif kind=='native_short':
        storage=_ui_pad_storage_image(img,row,e)
        enc=_ui_encode_mip_level(storage,e['fmt'],dxt5_swapped=bool(e.get('dxt5_swapped')))
        if len(enc)<target_size:raise ValueError('native-short encoder returned too few bytes')
        payload=enc[:target_size];written=target_size;mip_dims=[[int(e['w']),int(e['h'])]]
        layout_note=f'native short layout preserved; exact {len(enc)-target_size}-byte omitted tail retained outside the payload'
    else:
        raise ValueError('unsupported audit layout recipe '+str(kind))
    if len(payload)!=target_size:raise ValueError(f'encoded payload is {len(payload)} bytes; expected {target_size}')
    new=bytearray(arc);new[pa:pa+target_size]=payload;new=bytes(new)
    err=_ui_only_payload_changed(arc,new,pa,target_size)
    if err:raise ValueError(err)
    decoded=_ui_decode_image(new,e,row,logical=True);expected=_ui_logical_dims(row,e)
    if decoded.size!=expected:raise ValueError(f'decode-back dimensions are {decoded.size}, expected {expected}')
    decoded_display=_ui_thumb_transform(decoded,row);thumb=decoded_display.copy();thumb.thumbnail((420,260));pb=io.BytesIO();thumb.save(pb,'PNG')
    prep.update(dict(source_format=source_format,encoded_size=written,target_payload_size=target_size,padding_bytes=target_size-written,
                     truncated_bytes=0,native_short_payload=(kind=='native_short'),layout_note=layout_note,layout_recipe=kind,
                     mip_levels_written=int(recipe.get('levels') or 1),mip_dimensions=mip_dims,
                     mip_policy=layout_note,decode_back=True,output=[decoded.width,decoded.height]))
    return new,payload,prep,_b64.b64encode(pb.getvalue()).decode()


def _texture_discovery_module():
    return _load_module_from_path(
        TEXTURE_DISCOVERY_TOOL,
        'nascar15_texture_discovery_runtime',
        missing_message='texture discovery helper is missing',
        load_message='could not load texture discovery helper',
    )

def _ui_discovery_family(container, entry):
    """Classify newly discovered texture rows without pretending the exact consumer is verified."""
    c=str(container or '').upper(); e=str(entry or '').upper()
    if c=='NASCAR6_TEXTURES_X.ARC':
        if e.startswith('TYRE') or 'WHEEL' in e:
            return 'tire_wheel_textures'
        return 'shared_vehicle_textures'
    if c=='SPRINTNUMS2015.ARC': return 'driver_number_cards'
    if c.startswith(('2TRACKSELECTMENUIMAGE','3TRACKCARDIMAGE','TRACKDETAILIMAGES','CALENDAR_TRACK_IMAGES','2LOBBYTRACKCARDIMAGE','LOBBYTRACKCARDDETAILIMAGE','TRACK_FACTS_IMG_')):
        return 'track_select'
    if c.startswith('MAP_') or 'TRACKLOGO' in c: return 'track_logos_maps'
    if any(k in (c+' '+e) for k in ('HUD','GAUGE','TACH','SPEEDO','DAMAGE','STANDING','LEADERBOARD','RUNNINGORDER','LAPCOUNTER','POSITION','OVERLAY','TELEMETRY','DIAL','METER')): return 'race_hud_textures'
    if c in ('BASESCHEMETHUMBNAILS.ARC','CUSTOMSCHEMETHUMBNAILS.ARC'): return 'misc_ui'
    if c in ('GLOBALMENUASSETS.ARC','RACEMEDIAIMAGES.ARC','3LOADINGTRIVIAQUIZIMAGETEST.ARC') or 'MENUIMAGE' in c:
        return 'misc_ui'
    if c.startswith('TEAMSHOPLOGO'): return 'team_logos'
    if e.startswith('DRIVERPAINT_') or e.startswith('PAINTSCHEME_'): return 'paint_scheme_preview'
    if e.startswith('DRIVER_') and '3DNUM' in e: return 'driver_select'
    if c.startswith(('LIVERY_','HDLIVERY_')) or e=='IMG_LIV': return 'unknown_visual'
    if any(k in c for k in ('TEXTURE','TEX_','REPLACETEX')): return 'discovered_texture'
    return 'discovered_texture'


def _ui_direct_known_texture_rows(reg,max_mb):
    """Supplement the external scanner by directly enumerating high-value visual containers.

    Older discovery seeds were intentionally narrow and could list only the seven
    known tire/wheel maps from NASCAR6_TEXTURES_X.ARC.  This pass asks the proven
    multi-ARC parser for every entry in likely image containers so the browser can
    expose shared vehicle maps and other presentation assets the seed omitted.
    """
    likely_rx=re.compile(
        r'(?:TEXTURE|IMAGE|LOGO|THUMB|SPRINTNUMS|PAINT|MAP_|REPLACETEX|NASCAR6_TEXTURES_X|HUD|GAUGE|TACH|SPEEDO|DAMAGE|STANDING|LEADER|LAP|POSITION|OVERLAY|METER|TELEMETRY)',re.I)
    out=[];containers=0;errors=[];limit=int(max_mb)*1024*1024
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try: indexed=parse_cdfiles(v['cdf'])
        except Exception as ex:
            errors.append(f'ARCHIVE{arcid} index: {ex}');continue
        for off,size,name in indexed:
            if not str(name).upper().endswith('.ARC') or not likely_rx.search(str(name)):continue
            if int(size)<=0 or int(size)>limit:continue
            try:
                with open(v['ar'],'rb') as f:f.seek(int(off));blob=f.read(int(size))
                if len(blob)!=int(size):raise ValueError('short container read')
                entries,_base=C.parse_multi_arc(blob)
                added=0
                for e in entries:
                    en=str(e.get('name') or '').strip();fmt=str(e.get('fmt') or '').upper()
                    if not en or fmt not in ('DXT1','DXT5'):continue
                    try:
                        w=int(e.get('w'));h=int(e.get('h'));pa=int(e.get('payload_abs'));ps=int(e.get('payload_size'))
                    except Exception:continue
                    if w<=0 or h<=0 or ps<=0 or pa<0 or pa+ps>len(blob):continue
                    out.append(dict(archive=str(arcid),container=str(name),entry=en,w=w,h=h,fmt=fmt,
                                    payload_abs=pa,payload_size=ps,decoded=1,
                                    mip_count=int(e.get('mip_count') or 0),record_type=str(e.get('layout') or 'primary16'),
                                    overlap_previous=0,overlap_next=0,write_blocked=0,
                                    family=_ui_discovery_family(name,en)))
                    added+=1
                if added:containers+=1
            except Exception as ex:
                errors.append(f'ARCHIVE{arcid}/{name}: {ex}')
    return out,dict(direct_containers=containers,direct_found=len(out),direct_errors=errors[:30])


def _ui_write_discovery_csv(rows,path):
    fields=['archive','container','entry','w','h','fmt','payload_abs','payload_size','decoded','mip_count','record_type','mip_layout','overlap_previous','overlap_next','write_blocked','family']
    os.makedirs(os.path.dirname(path),exist_ok=True)
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8',newline='') as f:
        w=_csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for row in sorted(rows,key=lambda r:(int(str(r.get('archive') or '0')),str(r.get('container','')).upper(),str(r.get('entry','')).upper())):
            w.writerow({k:row.get(k,'') for k in fields})
    os.replace(tmp,path)


@app.route('/api/ui/discover', methods=['POST'])
def ui_discover():
    q=request.get_json(silent=True) or {}
    try:
        game,reg=registry()
        if not game or not reg: raise ValueError('NASCAR 15 game data folder is not configured')
        max_mb=max(8,min(1024,int(q.get('max_container_mb',256))))
        helper_error=None
        try:
            mod=_texture_discovery_module()
            live_rows,summary=mod.scan_registry(reg,max_mb)
        except Exception as ex:
            helper_error=str(ex);live_rows=[];summary=dict(containers=0,textures=0)
        direct_rows,direct_summary=_ui_direct_known_texture_rows(reg,max_mb)
        # Direct coverage fills gaps; the external scanner is appended last so its
        # more specific family labels win when both find the same resource.
        live_rows=direct_rows+list(live_rows or [])
        # Merge live discoveries with the packaged seed so a partial/older install
        # cannot erase known Archive 3 character and shared-vehicle mappings.
        merged={}
        discovered_path=_discovered_texture_csv(); report_path=_texture_discovery_report()
        os.makedirs(os.path.dirname(discovered_path) or USER_DIR,exist_ok=True)
        if os.path.exists(discovered_path):
            with open(discovered_path,'r',encoding='utf-8-sig',newline='') as f:
                for row in _csv.DictReader(f):
                    merged[_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))]=row
        before=len(merged)
        for row in live_rows:
            merged[_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))]=row
        _ui_write_discovery_csv(merged.values(),discovered_path)
        summary.update(direct_summary)
        summary.update(dict(game=game,seed_before=before,live_found=len(live_rows),merged_total=len(merged),
                            helper_error=helper_error,created=datetime.datetime.now().isoformat()))
        with open(report_path,'w',encoding='utf-8') as f: json.dump(summary,f,indent=2)
        _UI_INDEX_CACHE['signature']=None; _UI_INDEX_CACHE['rows']=None; _UI_THUMB_CACHE.clear()
        return jsonify(dict(ok=True,**summary))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/status')
def ui_status():
    index_files=_ui_index_files(); rows=[]
    if index_files:
        try: rows=_ui_index()
        except Exception: rows=[]
    overrides=_ui_mapping_overrides(); maps=[_ui_mapping(r,overrides) for r in rows]
    cats=sorted({m['category'] for m in maps}); screens=sorted({m['screen'] for m in maps})
    counts={c:sum(1 for m in maps if m['category']==c) for c in cats}
    screen_counts={c:sum(1 for m in maps if m['screen']==c) for c in screens}
    fmt_counts={f:sum(1 for r in rows if r.get('fmt')==f) for f in sorted({r.get('fmt') for r in rows})}
    verified=sum(1 for m in maps if m.get('verified'))
    packaged_mapped=sum(1 for m in maps if m.get('packaged_mapped'))
    research=sum(1 for m in maps if m.get('confidence') in ('unknown','research'))
    decoded=sum(1 for r in rows if r.get('decoded')); recovered=sum(1 for r in rows if r.get('geometry_status')=='recovered')
    unresolved=sum(1 for r in rows if not r.get('decoded'))
    source_counts={src:sum(1 for r in rows if r.get('source_index')==src) for src in sorted({r.get('source_index','unknown') for r in rows})}
    safety_counts=collections.Counter(_ui_safety(r,_confirmed_for(r),_ui_mapping(r,overrides)) for r in rows)
    mapping_status_counts=collections.Counter(_ui_mapping_status(r,_ui_mapping(r,overrides)) for r in rows)
    replacement_route_counts=collections.Counter(_ui_replacement_route(r,_confirmed_for(r),_ui_mapping(r,overrides)) for r in rows)
    container_type_counts=collections.Counter(_ui_container_type(r) for r in rows)
    image_type_counts=collections.Counter(_ui_image_type(r,_ui_mapping(r,overrides)) for r in rows)
    discovery_report={}
    try:
        report_path=_texture_discovery_report()
        if os.path.exists(report_path): discovery_report=json.load(open(report_path,'r',encoding='utf-8'))
    except Exception: discovery_report={}
    return jsonify(dict(ok=True,csv=bool(index_files),count=len(rows),packaged_count=source_counts.get('built_in',0),discovered_count=source_counts.get('discovered',0),decoded_count=decoded,recovered_count=recovered,unresolved_count=unresolved,
                        categories=cats,screens=screens,category_counts=counts,screen_counts=screen_counts,format_counts=fmt_counts,
                        verified_count=verified,packaged_mapped_count=packaged_mapped,mapping_unmapped_count=max(0,len(rows)-packaged_mapped),research_count=research,source_counts=source_counts,
                        discovery_report=discovery_report,
                        discovery_tool=bool(os.path.exists(TEXTURE_DISCOVERY_TOOL)),
                        safety_counts=dict(safety_counts),mapping_status_counts=dict(mapping_status_counts),replacement_route_counts=dict(replacement_route_counts),container_type_counts=dict(container_type_counts),image_type_counts=dict(image_type_counts),
                        fully_typed_count=sum(image_type_counts.values()),untyped_count=max(0,len(rows)-sum(image_type_counts.values())),
                        structurally_replaceable=sum(1 for r in rows if _ui_safety(r,_confirmed_for(r),_ui_mapping(r,overrides)) in ('safe_replace','guarded_replace')),
                        exact_raw_replaceable=sum(1 for r in rows if int(r.get('payload_size') or 0)>0 and int(r.get('payload_abs') or -1)>=0),
                        confirmed=[dict(id=a['id'],label=a['label'],png_replace_safe=a['png_replace_safe'],
                                        note=a['note']) for a in CONFIRMED_ASSETS]))

def _ui_modified_states(rows,reg):
    """Return exact live-vs-backup payload states without decoding thumbnails."""
    containers={}; states={}
    for r in rows:
        ident=(str(r['archive']),r['container'],r['entry'],_ui_payload_identity(r.get('payload_abs')))
        arcid=str(r['archive'])
        if arcid not in reg or not os.path.exists(reg[arcid]['bak']):
            states[ident]=False; continue
        ckey=(arcid,r['container'])
        pair=containers.get(ckey)
        if pair is None:
            try:
                off,size=find_entry(reg,arcid,r['container'])
                soff,ssize=find_entry(reg,arcid,r['container'],pristine=True)
                with open(reg[arcid]['ar'],'rb') as f:
                    f.seek(off); live=f.read(size)
                with open(reg[arcid]['bak'],'rb') as f:
                    f.seek(soff); stock=f.read(ssize)
                pair=(live,stock) if len(live)==size and len(stock)==ssize and size==ssize else (None,None)
            except Exception:
                pair=(None,None)
            containers[ckey]=pair
        live,stock=pair
        if live is None:
            states[ident]=False; continue
        pa=int(r.get('payload_abs',-1)); ps=int(r.get('payload_size',0))
        if pa<0 or ps<=0 or pa+ps>len(live) or pa+ps>len(stock):
            states[ident]=False; continue
        states[ident]=(live[pa:pa+ps]!=stock[pa:pa+ps])
    return states

@app.route('/api/ui/list', methods=['POST'])
def ui_list():
    q=request.get_json() or {}; mode=q.get('mode','normal'); text=(q.get('q') or '').lower()
    category=q.get('category') or ''; screen=q.get('screen') or ''
    safety=q.get('safety') or 'all'; mapping_filter=q.get('mapping_status') or 'all'; modified_only=bool(q.get('modified_only'))
    try: rows=_ui_index()
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    _g,_reg=registry(); overrides=_ui_mapping_overrides(); candidates=[]
    try: driver_links=_team_fast_driver_links()
    except Exception: driver_links={}
    hidden_normal={'Character & Pit Crew Textures','Driver & Character Textures','Vehicle / Livery Textures','Unknown / Research','Discovered Textures','Unresolved Binary Candidates'}
    tire_categories={'Tires & Wheels'}
    vehicle_categories={'Shared Vehicle Textures','Vehicle / Livery Textures','Character & Pit Crew Textures','Driver & Character Textures'}
    for r in rows:
        a=_confirmed_for(r); m=_ui_mapping(r,overrides); cat=m['category']; safe=_ui_safety(r,a,m)
        special=_ui_special_handler(r); dedicated={}
        if special=='driver_select_3dnum_dedicated':
            match=re.match(r'^DRIVER_(\d+)_3DNUM_',str(r.get('entry') or '').upper())
            driver_uid=int(match.group(1)) if match else None
            link=driver_links.get(driver_uid) if driver_uid is not None else None
            current_team_uid=int(link.get('team_uid')) if link else None
            current_container=(f'2DRIVERSELECTTD_{current_team_uid}.ARC' if current_team_uid is not None else None)
            current_target=bool(current_container and str(r.get('container') or '').upper()==current_container.upper())
            dedicated=dict(driver_uid=driver_uid,config_uid=(int(link.get('config_uid')) if link else None),
                           current_team_uid=current_team_uid,current_container=current_container,
                           dedicated_current_target=current_target)
            safe='safe_replace' if current_target else 'copy_only'
        if mode=='normal' and cat in hidden_normal: continue
        if mode=='tires' and cat not in tire_categories: continue
        if mode=='vehicle' and cat not in vehicle_categories: continue
        if q.get('asset_id') and (not a or a['id']!=q['asset_id']): continue
        if category and category!='all' and cat!=category: continue
        if screen and screen!='all' and m['screen']!=screen: continue
        map_status=_ui_mapping_status(r,m)
        if safety!='all' and safe!=safety: continue
        if mapping_filter!='all' and map_status!=mapping_filter: continue
        hay=' '.join([r['entry'],r['container'],r['archive'],cat,m['screen'],m['role'],m['label'],m['note'],map_status,(a['label'] if a else '')]).lower()
        if text and text not in hay: continue
        candidates.append((r,a,m,safe,dedicated))
    modified_states=_ui_modified_states([x[0] for x in candidates],_reg) if modified_only else {}
    out=[]
    for r,a,m,safe,dedicated in candidates:
        ident=(str(r['archive']),r['container'],r['entry'],_ui_payload_identity(r.get('payload_abs'))); modified=modified_states.get(ident) if modified_only else None
        if modified_only and not modified: continue
        special=_ui_special_handler(r); limited_guard=_limited_editor_profile()
        current_3dnum=bool(dedicated.get('dedicated_current_target')) and not limited_guard
        replacement_route=('exact_raw' if limited_guard else ('specialized_png' if current_3dnum else _ui_replacement_route(r,a,m)))
        replace_reason=(f"{active_game_name()} Smart Import stays locked while its layouts are under validation; use Raw Export/Exact Raw Import only for reviewed ARCHIVE0/1 assets." if limited_guard else ('' if current_3dnum else _ui_replace_reason(r)))
        recipe=_ui_layout_recipe(r)
        out.append(dict(archive=r['archive'],container=r['container'],entry=r['entry'],payload_abs=r.get('payload_abs'),
                        w=r['w'],h=r['h'],fmt=r['fmt'],payload_size=r['payload_size'],mip_count=r.get('mip_count'),
                        family=r.get('family',''),asset_id=(a['id'] if a else None),
                        label=m['label'],category=m['category'],screen=m['screen'],role=m['role'],
                        safety=safe,safety_label=_ui_policy_label(safe),confidence=m['confidence'],verified=bool(m.get('verified')),
                        mapping_status=_ui_mapping_status(r,m),replacement_route=replacement_route,exact_raw_import=True,
                        image_type=_ui_image_type(r,m),container_type=_ui_container_type(r),preview_mode=_ui_preview_mode(r),
                        special_handler=special,replace_reason=replace_reason,packaged_mapped=bool(m.get('packaged_mapped')),
                        driver_uid=dedicated.get('driver_uid'),config_uid=dedicated.get('config_uid'),
                        current_team_uid=dedicated.get('current_team_uid'),current_container=dedicated.get('current_container'),
                        dedicated_current_target=current_3dnum,
                        exact_base_payload=(bool(_ui_native_short_layout(r)) or (int(r.get('payload_size') or 0)>=_ui_needed(r['w'],r['h'],r['fmt']) if r.get('fmt') in ('DXT1','DXT5') else False)),
                        native_short_payload=bool(_ui_native_short_layout(r)),short_payload_bytes=((_ui_native_short_layout(r) or {}).get('missing_bytes',0)),
                        logical_w=_ui_logical_dims(r)[0],logical_h=_ui_logical_dims(r)[1],
                        decoded=bool(r.get('decoded')),geometry_status=r.get('geometry_status','indexed'),
                        original_w=r.get('original_w'),original_h=r.get('original_h'),decode_error=r.get('decode_error',''),
                        user_mapped=bool(m.get('user_mapped')),source_group=r.get('source_index') or r.get('family','ui_assets'),
                        note=m['note'],modified=modified,
                        has_backup=bool(str(r['archive']) in _reg and os.path.exists(_reg[str(r['archive'])]['bak'])),
                        png_replace_safe=(False if limited_guard else (current_3dnum or (bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace') and special!='driver_select_3dnum_dedicated'))),
                        profile=dict(profile=(a['id'] if a else r.get('family') or 'indexed_ui_texture'),
                                     alpha_supported=(r['fmt']=='DXT5'),
                                     inferred_mips=_ui_infer_mips(r['w'],r['h'],r['fmt'],r['payload_size']),
                                     native_mip_levels=recipe.get('levels'),layout_recipe=recipe.get('kind'),
                                     audit_policy='clean-game image map v1.0.1',
                                     wrapper_preserved=True)))
    # Page 1 is the first thing anyone sees, so lead with recognizable car renders,
    # 3D numbers and paint thumbnails. Native number UV atlases can look fragmented
    # at gallery scale, so they are grouped near the back rather than mistaken for
    # broken normal images. Unresolved/read-only resources go last. Python's sort is
    # stable, so ordering inside each band is untouched.
    _LEAD_FAMILIES = ('driver_select', 'paint_scheme_preview')

    def _browse_rank(row):
        if row.get('safety') == 'read_only' or row.get('geometry_status') == 'unresolved':
            return 3
        fam = str(row.get('family') or '')
        if fam == 'driver_number_cards' or row.get('preview_mode') == 'wrapped_number_card':
            return 2
        if fam in _LEAD_FAMILIES:
            return 0
        return 1
    out.sort(key=_browse_rank)
    total=len(out); page=max(0,int(q.get('page',0))); per=max(1,min(240,int(q.get('per',120))))
    return jsonify(dict(ok=True,total=total,page=page,per=per,modified_only=modified_only,
                        rows=out[page*per:(page+1)*per]))

@app.route('/api/ui/audit')
def ui_audit():
    """One pass over the whole graphics index: what is present, what decodes, and
    what can be replaced safely. Read-only - it reads the index and the same
    safety rules the replace routes enforce, and touches no game bytes.

    This exists because the per-item pills only answer the question one asset at a
    time, and there are over a thousand of them.
    """
    try:
        rows = _ui_index()
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

    overrides = _ui_mapping_overrides()
    totals = collections.Counter()
    by_safety = collections.Counter()
    problems = {}
    fmts = collections.Counter()

    for r in rows:
        totals['entries'] += 1
        fmts[str(r.get('fmt') or 'unknown')] += 1
        conf = _confirmed_for(r)
        mapping = _ui_mapping(r, overrides)
        safety = _ui_safety(r, conf, mapping)
        by_safety[safety] += 1
        decoded = r.get('decoded') is not False
        if decoded:
            totals['decodes'] += 1
        reason = _ui_replace_reason(r)
        if reason or not decoded:
            container = str(r.get('container') or '?').upper()
            slot = problems.setdefault(container, dict(
                container=container, count=0, safety=safety,
                reason=(reason or 'Geometry is unresolved; raw export only.'),
                examples=[]))
            slot['count'] += 1
            if len(slot['examples']) < 3:
                slot['examples'].append(str(r.get('entry') or '?'))

    blocked = sorted(problems.values(), key=lambda d: -d['count'])
    return jsonify(dict(ok=True,
        entries=totals['entries'],
        decodes=totals['decodes'],
        undecodable=totals['entries'] - totals['decodes'],
        formats=dict(fmts),
        safety=dict(by_safety),
        replaceable=by_safety.get('safe_replace', 0),
        copy_only=by_safety.get('copy_only', 0),
        read_only=by_safety.get('read_only', 0),
        blocked_containers=blocked,
        note=('Blocked entries are still exportable and restorable; only PNG '
              're-encoding is withheld, because their stored layout is not '
              'proven safe to rebuild.')))

@app.route('/api/ui/tire_family')
def ui_tire_family():
    try:
        scope=(request.args.get('scope') or 'diffuse').lower();rows=_ui_index();_g,reg=registry();out=[]
        names={'diffuse':{'TYRE02.DDS','TYRE02-D.DDS'},
               'maps':{'TYRE02.DDS','TYRE02-D.DDS','TYRE02-N.DDS','TYRE02-DN.DDS','TYRE02-S.DDS','TYRE02-DS.DDS'},
               'all':{'TYRE02.DDS','TYRE02-D.DDS','TYRE02-N.DDS','TYRE02-DN.DDS','TYRE02-S.DDS','TYRE02-DS.DDS','WHEELBLURNEW.DDS'}}.get(scope)
        if names is None:raise ValueError('scope must be diffuse, maps, or all')
        overrides=_ui_mapping_overrides()
        for r in rows:
            if str(r.get('container','')).upper()!='NASCAR6_TEXTURES_X.ARC' or str(r.get('entry','')).upper() not in names:continue
            a=_confirmed_for(r);m=_ui_mapping(r,overrides);safe=_ui_safety(r,a,m)
            out.append(dict(archive=r['archive'],container=r['container'],entry=r['entry'],payload_abs=r.get('payload_abs'),w=r['w'],h=r['h'],fmt=r['fmt'],payload_size=r['payload_size'],family=r.get('family',''),label=m['label'],category=m['category'],screen=m['screen'],role=m['role'],safety=safe,decoded=bool(r.get('decoded')),png_replace_safe=(bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace')),has_backup=bool(str(r['archive']) in reg and os.path.exists(reg[str(r['archive'])]['bak']))))
        order=['Tyre02.dds','Tyre02-D.dds','Tyre02-N.dds','Tyre02-DN.dds','Tyre02-S.dds','Tyre02-DS.dds','wheelblurNEW.dds'];out.sort(key=lambda x:order.index(x['entry']) if x['entry'] in order else 99)
        return jsonify(dict(ok=True,scope=scope,count=len(out),rows=out))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/map', methods=['POST'])
def ui_map():
    q=request.get_json() or {}
    try:
        key=_ui_mapping_key(q); d=_ui_mapping_overrides(); current=d.get(key,{}) if isinstance(d.get(key,{}),dict) else {}
        for k in ('label','category','screen','role','note'):
            if k in q:
                v=str(q.get(k) or '').strip()
                if v: current[k]=v
                else: current.pop(k,None)
        if 'verified' in q: current['verified']=bool(q.get('verified'))
        if current: d[key]=current
        else: d.pop(key,None)
        _ui_save_mapping_overrides(d)
        row=dict(archive=q.get('archive'),container=q.get('container'),entry=q.get('entry'),payload_abs=q.get('payload_abs'),family=q.get('family'),fmt=q.get('fmt'))
        return jsonify(dict(ok=True,mapping=_ui_mapping(row,d)))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

def _ui_full_manifest_rows():
    rows=_ui_index(); overrides=_ui_mapping_overrides(); out=[]
    for r in rows:
        a=_confirmed_for(r); m=_ui_mapping(r,overrides); safe=_ui_safety(r,a,m)
        out.append(dict(
            archive=str(r.get('archive','')),container=r.get('container',''),entry=r.get('entry',''),
            payload_abs=int(r.get('payload_abs',-1)),payload_size=int(r.get('payload_size',0)),
            width=int(r.get('w',0)),height=int(r.get('h',0)),logical_width=_ui_logical_dims(r)[0],logical_height=_ui_logical_dims(r)[1],format=r.get('fmt',''),family=r.get('family',''),
            decoded=bool(r.get('decoded')),geometry_status=r.get('geometry_status',''),native_short_payload=bool(_ui_native_short_layout(r)),short_payload_bytes=((_ui_native_short_layout(r) or {}).get('missing_bytes',0)),
            category=m.get('category',''),screen=m.get('screen',''),role=m.get('role',''),label=m.get('label',''),
            confidence=m.get('confidence',''),mapping_status=_ui_mapping_status(r,m),verified=bool(m.get('verified')),
            image_type=_ui_image_type(r,m),container_type=_ui_container_type(r),preview_mode=_ui_preview_mode(r),
            edit_policy=safe,replacement_route=_ui_replacement_route(r,a,m),png_import=bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace'),
            exact_raw_import=True,note=m.get('note',''),decode_error=r.get('decode_error',''),source_group=r.get('source_index','')
        ))
    return out


@app.route('/api/ui/manifest/export')
def ui_manifest_export():
    try:
        rows=_ui_full_manifest_rows(); fmt=(request.args.get('format') or 'csv').lower()
        if fmt=='json':
            payload=json.dumps(dict(app_version=APP_VERSION,physical_records=len(rows),rows=rows),indent=2).encode('utf-8')
            return send_file(io.BytesIO(payload),mimetype='application/json',as_attachment=True,download_name=f'nascar15_full_graphics_map_v{APP_VERSION}.json')
        fields=list(rows[0].keys()) if rows else []
        text=io.StringIO(); writer=_csv.DictWriter(text,fieldnames=fields,extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
        return send_file(io.BytesIO(text.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name=f'nascar15_full_graphics_map_v{APP_VERSION}.csv')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui/mappings/export')
def ui_mappings_export():
    d=_ui_mapping_overrides(); b=io.BytesIO(json.dumps(d,indent=2,sort_keys=True).encode('utf-8'))
    return send_file(b,mimetype='application/json',as_attachment=True,download_name='nascar15_ui_mapping_overrides.json')

@app.route('/api/ui/export_raw', methods=['POST'])
def ui_export_raw():
    q=request.get_json() or {}
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if not row: raise ValueError('indexed row not found')
        g,reg=registry(); arcid=str(q['archive']); off,size=find_entry(reg,arcid,q['container'])
        with open(reg[arcid]['ar'],'rb') as f:
            f.seek(off); arc=f.read(size)
        pa=int(row.get('payload_abs',-1)); ps=int(row.get('payload_size',0))
        if pa<0 or ps<=0 or pa+ps>len(arc): raise ValueError('payload range is outside the live container')
        payload=arc[pa:pa+ps]
        fn=re.sub(r'[^A-Za-z0-9_.-]+','_',f"ARCHIVE{arcid}_{q['container']}_{q['entry']}_off{pa}_{ps}bytes.bin")
        return jsonify(dict(ok=True,filename=fn,raw=_b64.b64encode(payload).decode(),size=ps))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/export', methods=['POST'])
def ui_export():
    q=request.get_json()
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if row and not row.get('decoded'): raise ValueError('geometry is unresolved; use Raw Export')
        arc,e,_,_=_ui_load_entry(q['archive'], q['container'], q['entry'], q.get('w'), q.get('h'), pristine=bool(q.get('pristine')), payload_abs=q.get('payload_abs'))
        img=_ui_decode_image(arc,e,row or q,logical=True)
        img=_ui_thumb_transform(img,row or q)
        buf=io.BytesIO(); img.save(buf,'PNG')
        return jsonify(dict(ok=True, png=_b64.b64encode(buf.getvalue()).decode(),
                            w=img.width, h=img.height, storage_w=e['w'],storage_h=e['h'],fmt=e['fmt'], pristine=bool(q.get('pristine'))))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400

_UI_THUMB_CACHE={}

@app.route('/api/ui/thumb', methods=['POST'])
def ui_thumb():
    q=request.get_json()
    key=(str(q['archive']),q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs')))
    if key in _UI_THUMB_CACHE:
        return jsonify(dict(ok=True,**_UI_THUMB_CACHE[key],cached=True))
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if row and not row.get('decoded'): raise ValueError('unresolved geometry; raw export and exact raw import only')
        arc,e,_,_=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        native=_ui_decode_image(arc,e,row or q,logical=False)
        logical=_ui_decode_image(arc,e,row or q,logical=True)
        display=_ui_thumb_transform(logical,row or q)
        def enc_thumb(img):
            thumb=img.copy(); thumb.thumbnail((170,150)); b=io.BytesIO(); thumb.save(b,'PNG'); return _b64.b64encode(b.getvalue()).decode()
        native_png=enc_thumb(native); display_png=enc_thumb(display)
        stock_native_png=None; stock_display_png=None; modified=None
        g,reg=registry(); arcid=str(q['archive'])
        if arcid in reg and os.path.exists(reg[arcid]['bak']):
            try:
                barc,be,_,_=_ui_load_entry(arcid,q['container'],q['entry'],q.get('w'),q.get('h'),pristine=True,payload_abs=q.get('payload_abs'))
                live_payload=arc[e['payload_abs']:e['payload_abs']+e['payload_size']]
                stock_payload=barc[be['payload_abs']:be['payload_abs']+be['payload_size']]
                modified=(live_payload!=stock_payload)
                stock_native=_ui_decode_image(barc,be,row or q,logical=False)
                stock_logical=_ui_decode_image(barc,be,row or q,logical=True)
                stock_display=_ui_thumb_transform(stock_logical,row or q)
                stock_native_png=enc_thumb(stock_native); stock_display_png=enc_thumb(stock_display)
            except Exception:
                stock_native_png=None; stock_display_png=None; modified=None
        result=dict(png=native_png,stock_png=stock_native_png,native_png=native_png,display_png=display_png,
                    stock_native_png=stock_native_png,stock_display_png=stock_display_png,
                    modified=modified,w=e['w'],h=e['h'],fmt=e['fmt'],preview_mode=_ui_preview_mode(row or q))
        if len(_UI_THUMB_CACHE)<800: _UI_THUMB_CACHE[key]=result
        return jsonify(dict(ok=True,**result))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/replace_raw', methods=['POST'])
def ui_replace_raw():
    """Install one exact encoded texture payload without interpreting geometry.

    This is the universal fallback for every indexed physical texture. The file
    must match the target payload byte count exactly. A standard 128-byte DDS
    header is accepted and stripped when its remaining payload is the exact size.
    Only the indexed payload window may change; wrapper bytes and container size
    are guarded and the installed payload is read back byte-for-byte.
    """
    try:
        arcid=str(request.form.get('archive','')); container=request.form.get('container',''); entry=request.form.get('entry','')
        payload_abs=request.form.get('payload_abs'); payload_abs=int(payload_abs) if payload_abs not in (None,'') else None
        f=request.files.get('file')
        if not f: raise ValueError('no raw payload file selected')
        payload=f.read()
        row=_ui_csv_row(arcid,container,entry,payload_abs=payload_abs)
        if not row: raise ValueError('indexed physical texture was not found')
        arc,e,off,size=_ui_load_entry(arcid,container,entry,row.get('w'),row.get('h'),payload_abs=payload_abs)
        target_size=int(e['payload_size'])
        stripped_header=0
        if payload[:4]==b'DDS ':
            header=148 if len(payload)>=148 and payload[84:88]==b'DX10' else 128
            if len(payload)-header==target_size:
                payload=payload[header:]; stripped_header=header
        if len(payload)!=target_size:
            raise ValueError(f'raw file is {len(payload)} bytes; this exact target requires {target_size} bytes')
        pa=int(e['payload_abs']); new=bytearray(arc); new[pa:pa+target_size]=payload; new=bytes(new)
        err=_ui_only_payload_changed(arc,new,pa,target_size)
        if err: raise ValueError(err)
        dry=str(request.form.get('dry_run','0')).lower() in ('1','true','yes')
        if dry:
            return jsonify(dict(ok=True,dry_run=True,payload_size=target_size,stripped_dds_header=stripped_header,note='exact payload size and write window verified; nothing written'))
        _ui_install(arcid,off,size,new)
        _UI_THUMB_CACHE.pop((arcid,container,entry,_ui_payload_identity(payload_abs)),None)
        chk,ce,_,_=_ui_load_entry(arcid,container,entry,row.get('w'),row.get('h'),payload_abs=payload_abs)
        readback=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]
        if readback!=payload:
            raise RuntimeError('raw payload readback mismatch after install; restore this graphic immediately')
        return jsonify(dict(ok=True,verified=True,payload_size=target_size,stripped_dds_header=stripped_header))
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),500


@app.route('/api/ui/copy', methods=['POST'])
def ui_copy():
    """Raw donor copy guarded by the complete native layout signature."""
    q=request.get_json()
    try:
        s=q['src']; d=q['dst']
        srow=_ui_csv_row(s['archive'],s['container'],s['entry'],payload_abs=s.get('payload_abs'))
        drow=_ui_csv_row(d['archive'],d['container'],d['entry'],payload_abs=d.get('payload_abs'))
        if not srow or not drow: raise ValueError('source or target is not present in the indexed graphics map')
        sarc,se,_,_ = _ui_load_entry(s['archive'], s['container'], s['entry'], s.get('w'), s.get('h'), payload_abs=s.get('payload_abs'))
        darc,de,doff,dsize = _ui_load_entry(d['archive'], d['container'], d['entry'], d.get('w'), d.get('h'), payload_abs=d.get('payload_abs'))
        ssig=_ui_layout_signature(srow,se); dsig=_ui_layout_signature(drow,de)
        if ssig!=dsig:
            return jsonify(dict(ok=False,error='native layout-family mismatch. Copy From requires the same codec, dimensions, payload, mip/padding recipe, and protected resource class.',
                                source_signature=repr(ssig),target_signature=repr(dsig))),400
        payload=sarc[se['payload_abs']:se['payload_abs']+se['payload_size']]
        new=bytearray(darc); new[de['payload_abs']:de['payload_abs']+de['payload_size']]=payload; new=bytes(new)
        err=_ui_only_payload_changed(darc,new,de['payload_abs'],de['payload_size'])
        if err:return jsonify(dict(ok=False,error=err)),400
        if q.get('dry_run'):
            return jsonify(dict(ok=True,note='dry-run: exact native layout signature matches and only the target payload would change',layout_signature=repr(dsig)))
        _ui_install(d['archive'],doff,dsize,new)
        _UI_THUMB_CACHE.pop((str(d['archive']),d['container'],d['entry'],_ui_payload_identity(d.get('payload_abs'))),None)
        chk,ce,_,_=_ui_load_entry(d['archive'],d['container'],d['entry'],d.get('w'),d.get('h'),payload_abs=d.get('payload_abs'))
        ok=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]==payload
        return jsonify(dict(ok=True,verified=ok,layout_signature=repr(dsig)))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/replace_png', methods=['POST'])
def ui_replace_png():
    """Smart Import for validated UI textures.

    Common source formats are decoded by Pillow, resized to the target geometry,
    encoded to the target DXT codec, placed into an unchanged wrapper, decoded
    back for validation, and finally read back after installation. Unknown
    families remain blocked unless force=True from Advanced mode.
    """
    q=request.get_json() or {}
    try:
        indexed=_ui_csv_row(q.get('archive'),q.get('container'),q.get('entry'),payload_abs=q.get('payload_abs'))
        rowlike=dict(indexed or {},archive=q.get('archive'),entry=q['entry'],container=q['container'],family=q.get('family',''),fmt=q.get('fmt',''),w=q.get('w'),h=q.get('h'),payload_size=q.get('payload_size'))
        a=_confirmed_for(rowlike); mapping=_ui_mapping(rowlike)
        reason=_ui_replace_reason(rowlike)
        if reason:
            return jsonify(dict(ok=False,error=reason,special_handler=_ui_special_handler(rowlike))),400
        safe=(_ui_safety(rowlike,a,mapping)=='safe_replace')
        arc,e,off,size=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        if e.get('fmt') not in ('DXT1','DXT5'):
            return jsonify(dict(ok=False,error=f'Smart Import does not support {e.get("fmt")} targets')),400
        profile=_ui_profile(rowlike,e,a); profile['mapping']=mapping; profile['safety']=('safe_replace' if safe else 'guarded_replace')
        new,payload,prep,preview=_ui_prepare_encoded(q,arc,e,safe,rowlike)
        if q.get('dry_run'):
            return jsonify(dict(ok=True,note='Smart Import conversion preview passed; nothing written',
                                experimental=not safe,image_prep=prep,preview_png=preview,
                                profile=profile,verified_preview=True))
        _ui_install(q['archive'],off,size,new)
        _UI_THUMB_CACHE.pop((str(q['archive']),q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs'))),None)
        chk,ce,_,_=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        readback=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]
        verified=(readback==payload)
        if not verified:
            return jsonify(dict(ok=False,error='readback mismatch after install; restore this image from Stock immediately')),500
        # Decode live bytes once more, not merely the temporary copy.
        decoded=_ui_decode_image(chk,ce,rowlike,logical=True)
        decode_verified=(decoded.size==_ui_logical_dims(rowlike,ce))
        return jsonify(dict(ok=True,verified=verified,decode_verified=decode_verified,
                            experimental=not safe,image_prep=prep,profile=profile))
    except ValueError as ve:
        return jsonify(dict(ok=False,error=str(ve))),400
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/restore', methods=['POST'])
def ui_restore():
    """Restore ONE image from the pristine backup, leaving the rest of the
    container byte-identical (same guard as copy)."""
    q=request.get_json()
    try:
        arcid=str(q['archive'])
        g,reg=registry()
        if not os.path.exists(reg[arcid]['bak']):
            return jsonify(dict(ok=False, error='no original backup is available for that game file')),400
        barc,be,_,_ = _ui_load_entry(arcid, q['container'], q['entry'], q.get('w'), q.get('h'), pristine=True, payload_abs=q.get('payload_abs'))
        larc,le,loff,lsize = _ui_load_entry(arcid, q['container'], q['entry'], q.get('w'), q.get('h'), payload_abs=q.get('payload_abs'))
        if be['payload_size']!=le['payload_size']:
            return jsonify(dict(ok=False, error='payload size differs from backup; refused')),400
        payload=barc[be['payload_abs']:be['payload_abs']+be['payload_size']]
        new=bytearray(larc)
        new[le['payload_abs']:le['payload_abs']+le['payload_size']]=payload
        new=bytes(new)
        err=_ui_only_payload_changed(larc, new, le['payload_abs'], le['payload_size'])
        if err: return jsonify(dict(ok=False, error=err)),400
        _ui_install(arcid, loff, lsize, new)
        _UI_THUMB_CACHE.pop((arcid,q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs'))), None)
        return jsonify(dict(ok=True, restored=q['entry']))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400

# ==================== end UI IMAGES ====================



# ==================== v0.9.26.7 GAMEPLAY-CONFIRMED SCHEDULE EDITOR ====================






























































# ---- v0.9.26.12 atomic bulk Images & Textures actions ----
def _ui_bulk_identity(t):
    # Payload offset is part of the identity so duplicate/aliased entry names in
    # one container cannot collapse into a single selected target.
    pa=t.get('payload_abs')
    try:pa=int(pa)
    except Exception:pa=-1
    return (str(t.get('archive','')),str(t.get('container','')).upper(),str(t.get('entry','')),pa)


def _ui_bulk_group_key(arcid,container):
    return (str(arcid),str(container or '').upper())


def _ui_union_only_changed(old,new,ranges):
    if len(old)!=len(new):return 'container size changed'
    merged=[]
    for a,b in sorted((int(a),int(b)) for a,b in ranges):
        if a<0 or b<a or b>len(old):return 'invalid payload range'
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],b))
        else:merged.append((a,b))
    cur=0
    for a,b in merged:
        if old[cur:a]!=new[cur:a]:return f'bytes outside selected payloads changed before 0x{a:X}'
        cur=b
    if old[cur:]!=new[cur:]:return 'bytes outside selected payloads changed after final payload'
    return None


def _ui_bulk_groups(targets,need_stock=False):
    if not isinstance(targets,list) or not targets:raise ValueError('select at least one image')
    if len(targets)>250:raise ValueError('bulk image operations are limited to 250 selected images')
    seen=set();groups={};resolved=[]
    for t in targets:
        ident=_ui_bulk_identity(t)
        if ident in seen:continue
        seen.add(ident);arcid,container_u,entry,payload_hint=ident
        container=str(t.get('container') or '')
        if not arcid or not container or not entry:raise ValueError('invalid selected image identity')
        indexed=_ui_csv_row(arcid,container,entry,payload_abs=(None if payload_hint<0 else payload_hint))
        rowlike=dict(indexed or {},archive=arcid,container=container,entry=entry,
                     family=t.get('family',''),fmt=t.get('fmt',''),w=t.get('w'),h=t.get('h'),payload_size=t.get('payload_size'))
        live,e,off,size=_ui_load_entry(arcid,container,entry,t.get('w'),t.get('h'),payload_abs=(None if payload_hint<0 else payload_hint))
        if payload_hint>=0 and int(e.get('payload_abs',-1))!=payload_hint:
            raise ValueError(f'{entry}: selected payload 0x{payload_hint:X} resolved to 0x{int(e.get("payload_abs",-1)):X}')
        key=_ui_bulk_group_key(arcid,container)
        g=groups.get(key)
        if g is None:
            g=groups[key]=dict(arcid=arcid,container=container,container_u=container_u,
                               old=live,current=live,off=off,size=size,ranges=[],items=[])
        elif g['off']!=off or g['size']!=size:
            raise ValueError(f'{container}: inconsistent indexed container range')
        stock_e=stock_arc=None
        if need_stock:
            stock_arc,stock_e,soff,ssize=_ui_load_entry(arcid,container,entry,t.get('w'),t.get('h'),pristine=True,payload_abs=(None if payload_hint<0 else payload_hint))
            if stock_e['payload_size']!=e['payload_size']:
                raise ValueError(f'{entry}: stock payload size differs')
        item=dict(target=t,row=rowlike,e=e,stock_e=stock_e,stock_arc=stock_arc,
                  ident=ident,group_key=key)
        g['items'].append(item);resolved.append(item)
    return groups,resolved


def _ui_bulk_commit(groups,expected_payloads,source):
    gpath,reg=registry();written=[];verified=[]
    try:
        # Verify the final in-memory image contains every selected payload before
        # touching disk. This catches accidental "last target wins" behavior.
        for grp in groups.values():
            err=_ui_union_only_changed(grp['old'],grp['current'],grp['ranges'])
            if err:raise ValueError(f'{grp["container"]}: {err}')
            for item in grp['items']:
                payload=expected_payloads.get(item['ident'])
                if payload is None:continue
                e=item['e'];a=e['payload_abs'];b=a+e['payload_size']
                if grp['current'][a:b]!=payload:
                    raise ValueError(f'{item["ident"][2]}: final batch image lost this selected payload before write')
        for grp in groups.values():
            v=need(reg,grp['arcid']);ensure_backup(v['ar'],v['bak'])
            # Mark the container as attempted before the first live write. A
            # disk/fsync failure can happen after bytes changed but before this
            # block returns, and that partially-written container must still be
            # included in rollback.
            written.append(grp)
            with open(v['ar'],'r+b') as fh:
                fh.seek(grp['off']);fh.write(grp['current']);fh.flush();os.fsync(fh.fileno())
        # Read back complete containers and then every selected payload separately.
        for grp in groups.values():
            v=need(reg,grp['arcid'])
            with open(v['ar'],'rb') as fh:fh.seek(grp['off']);chk=fh.read(grp['size'])
            if chk!=grp['current']:raise ValueError(f'{grp["container"]}: container readback mismatch')
        verified_targets=[]
        for grp in groups.values():
            v=need(reg,grp['arcid'])
            with open(v['ar'],'rb') as fh:fh.seek(grp['off']);chk=fh.read(grp['size'])
            for item in grp['items']:
                payload=expected_payloads.get(item['ident'])
                if payload is None:continue
                e=item['e'];a=e['payload_abs'];b=a+e['payload_size']
                if chk[a:b]!=payload:
                    raise ValueError(f'{item["ident"][2]} @ 0x{a:X}: payload readback mismatch')
                verified.append(item['ident'][2])
                verified_targets.append(dict(entry=item['ident'][2],payload_abs=int(a),payload_size=int(e['payload_size']),
                                             archive=str(grp['arcid']),container=grp['container']))
        # Container-name casing made targeted invalidation unreliable in older
        # builds. Clear the whole lightweight thumbnail cache after an atomic batch.
        _clear_ui_thumb_cache()
        return dict(ok=True,verified=True,app_version=APP_VERSION,changed=len(expected_payloads),verified_count=len(verified),
                    verified_entries=verified,verified_targets=verified_targets,containers=len(groups),source=source)
    except Exception as install_ex:
        rollback_errors=[]
        for grp in reversed(written):
            try:
                v=need(reg,grp['arcid'])
                with open(v['ar'],'r+b') as fh:
                    fh.seek(grp['off']);fh.write(grp['old']);fh.flush();os.fsync(fh.fileno())
                    fh.seek(grp['off'])
                    if fh.read(grp['size'])!=grp['old']:
                        raise ValueError('rollback readback mismatch')
            except Exception as rb:
                rollback_errors.append(f'{grp["container"]}: {rb}')
        _clear_ui_thumb_cache()
        if rollback_errors:
            raise RollbackFailed(install_ex,'; '.join(rollback_errors)) from install_ex
        raise

@app.route('/api/ui/bulk_restore',methods=['POST'])
def ui_bulk_restore():
    try:
        q=request.get_json(force=True);groups,items=_ui_bulk_groups(q.get('targets') or [],need_stock=True)
        expected={}
        for item in items:
            g=groups[item['group_key']];e=item['e'];se=item['stock_e'];sa=item['stock_arc']
            payload=sa[se['payload_abs']:se['payload_abs']+se['payload_size']]
            cur=bytearray(g['current']);a=e['payload_abs'];b=a+e['payload_size'];cur[a:b]=payload;g['current']=bytes(cur);g['ranges'].append((a,b));expected[item['ident']]=payload
        if q.get('dry_run'):
            return jsonify(dict(ok=True,dry_run=True,changed=len(expected),containers=len(groups),entries=[i['ident'][2] for i in items]))
        return jsonify(_ui_bulk_commit(groups,expected,'bulk stock restore'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/bulk_replace',methods=['POST'])
def ui_bulk_replace():
    """Apply one image to every selected target, or match a ZIP by entry name.

    Every selected target is independently resized/encoded to its own format.
    ZIP mode accepts names such as Tyre02.png, Tyre02-D.png, or the exported
    container__entry template name. All modified containers are committed as
    one rollback-protected operation.
    """
    try:
        import zipfile
        targets=json.loads(request.form.get('targets') or '[]');groups,items=_ui_bulk_groups(targets,need_stock=False)
        up=request.files.get('file')
        if not up:raise ValueError('choose an image or ZIP package')
        raw=up.read()
        if not raw:raise ValueError('uploaded file is empty')
        resize_mode=request.form.get('resize_mode','fit');force=request.form.get('force')=='1';dry=request.form.get('dry_run')=='1'
        members={};is_zip=(up.filename or '').lower().endswith('.zip') or raw[:4]==b'PK\x03\x04'
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    if n.endswith('/'):continue
                    info=z.getinfo(n)
                    if info.file_size>32*1024*1024:continue
                    base=os.path.basename(n);stem=os.path.splitext(base)[0].casefold()
                    members.setdefault(base.casefold(),z.read(n));members.setdefault(stem,z.read(n))
        matched=[];unmatched=[];expected={};prep=[]
        for item in items:
            t=item['target'];entry=t['entry'];container=t['container'];imgraw=None;member_name=None
            if is_zip:
                eb=os.path.basename(entry);estem=os.path.splitext(eb)[0].casefold();cstem=os.path.splitext(os.path.basename(container))[0].casefold()
                candidates=[eb.casefold(),estem,f'{cstem}__{estem}',re.sub(r'[^a-z0-9_.-]+','_',f'{cstem}__{estem}').casefold()]
                for cand in candidates:
                    if cand in members:imgraw=members[cand];member_name=cand;break
                if imgraw is None:
                    # exported templates include dimensions after the entry name
                    for k,v in members.items():
                        if k.startswith(f'{cstem}__{estem}_') or k.startswith(estem+'_'):
                            imgraw=v;member_name=k;break
            else:
                imgraw=raw;member_name=up.filename or 'shared image'
            if imgraw is None:
                unmatched.append(entry);continue
            row=item['row'];reason=_ui_replace_reason(row)
            if reason:raise ValueError(f'{entry}: {reason}')
            a=_confirmed_for(row);mapping=_ui_mapping(row);safe=(_ui_safety(row,a,mapping)=='safe_replace')
            if not safe and not force and row.get('decoded') is False:
                raise ValueError(f'{entry}: target is not decoded safely')
            g=groups[item['group_key']];e=item['e']
            q=dict(image=_b64.b64encode(imgraw).decode(),resize_mode=resize_mode)
            new,payload,iprep,preview=_ui_prepare_encoded(q,g['current'],e,safe,row)
            g['current']=new;a0=e['payload_abs'];g['ranges'].append((a0,a0+e['payload_size']));expected[item['ident']]=payload
            matched.append(entry);prep.append(dict(entry=entry,source=member_name,target=[e['w'],e['h']],codec=e['fmt'],resized=iprep.get('resized',False),
                                                    mip_levels_written=int(iprep.get('mip_levels_written',1)),mip_dimensions=iprep.get('mip_dimensions') or []))
        if not matched:raise ValueError('no uploaded images matched the selected entries')
        if not is_zip and len(matched)!=len(items):
            raise ValueError(f'same-image batch resolved only {len(matched)} of {len(items)} selected targets')
        if len(expected)!=len(matched):
            raise ValueError('bulk target identity collision: selected entries did not remain unique')
        # Drop groups with no matched target so unchanged containers are not written.
        groups={k:g for k,g in groups.items() if g['ranges']}
        if dry:return jsonify(dict(ok=True,dry_run=True,app_version=APP_VERSION,mode=('zip_match' if is_zip else 'same_image'),matched=matched,unmatched=unmatched,containers=len(groups),resolved_count=len(expected),resolved_targets=[dict(entry=i['ident'][2],payload_abs=int(i['e']['payload_abs']),payload_size=int(i['e']['payload_size'])) for i in items if i['ident'] in expected],preparation=prep))
        result=_ui_bulk_commit(groups,expected,'bulk smart import');result.update(mode=('zip_match' if is_zip else 'same_image'),matched=matched,unmatched=unmatched,preparation=prep);return jsonify(result)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule')
def schedule_get():
    try:
        editor=_shared_schedule_editor()
        rows=editor.rows();profiles=editor.event_lap_profiles()
        return jsonify(dict(
            ok=True,rows=rows,stock_rows=editor.catalog(),
            event_lap_profiles=profiles['profiles'],profile_rows=profiles['rows'],
            profile_source=profiles['source'],season=editor.installation.profile.content_season,
            schedule_sources=1,gameplay_links_verified=True,
            cache_warning='Existing Career and Single Season saves may retain the calendar created when that mode began. Test changes with a brand-new disposable mode.',
        ))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/stock')
def schedule_stock_get():
    try:
        editor=_shared_schedule_editor();profiles=editor.event_lap_profiles()
        return jsonify(dict(ok=True,rows=editor.catalog(),stock_source='pristine backup',
                            event_lap_profiles=profiles['profiles']))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/preview',methods=['POST'])
def schedule_preview():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().preview_custom(q.get('slots') or []))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/apply',methods=['POST'])
def schedule_apply():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().apply_custom(q.get('slots') or []))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/restore',methods=['POST'])
def schedule_restore():
    try:return jsonify(dict(ok=True,**_shared_schedule_editor().restore()))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/stock36_laps')
def schedule_stock36_laps_get():
    try:return jsonify(dict(ok=True,**_shared_schedule_editor().event_lap_profiles()))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_lap/preview',methods=['POST'])
def schedule_event_lap_preview():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().preview_event_laps([q]))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_lap/apply',methods=['POST'])
def schedule_event_lap_apply():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().apply_event_laps([q]))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_laps/batch/preview',methods=['POST'])
def schedule_event_laps_batch_preview():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().preview_event_laps(q.get('entries') or []))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_laps/batch/apply',methods=['POST'])
def schedule_event_laps_batch_apply():
    try:
        q=request.get_json(force=True)
        return jsonify(_shared_schedule_editor().apply_event_laps(q.get('entries') or []))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/stock36_laps/restore',methods=['POST'])
def schedule_stock36_laps_restore():
    try:return jsonify(_shared_schedule_editor().restore_event_laps())
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/tracks/files',methods=['POST'])
def track_files():
    try:
        q=request.get_json(silent=True) or {}
        inventory=_shared_track_inventory()
        summary=inventory.summary()
        rows=inventory.filter(
            track=q.get('track') or 'all',
            category=q.get('category') or 'all',
            query=q.get('q') or '',
        )
        page=max(0,int(q.get('page',0)));per=max(1,min(500,int(q.get('per',200))))
        return jsonify(dict(
            ok=True,total=len(rows),page=page,per=per,
            rows=rows[page*per:(page+1)*per],
            tracks=summary['tracks'],categories=summary['categories'],
            track_counts=summary['track_counts'],read_only=True,cache='shared',
        ))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/tracks/compare',methods=['POST'])
def track_compare():
    try:
        q=request.get_json(force=True)
        return jsonify(dict(ok=True,**_shared_track_inventory().compare(q.get('a'),q.get('b'))))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/tracks/report')
def track_report():
    try:
        track=request.args.get('track','all')
        payload=_shared_track_inventory().report_bytes(track)
        safe=re.sub(r'[^A-Za-z0-9_.-]+','_',track)
        return send_file(io.BytesIO(payload),mimetype='application/zip',as_attachment=True,
                         download_name=f'nascar_track_files_{safe}.zip')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/tracks/export')
def track_export():
    temp_path=None
    try:
        track=request.args.get('track')
        fd,temp_path=tempfile.mkstemp(prefix='nascar_track_export_',suffix='.zip')
        os.close(fd)
        result=_shared_track_inventory().export_track(track,temp_path)
        @after_this_request
        def cleanup(response):
            try:os.remove(temp_path)
            except OSError:pass
            return response
        safe=re.sub(r'[^A-Za-z0-9_.-]+','_',track)
        return send_file(result['path'],mimetype='application/zip',as_attachment=True,
                         download_name=f'nascar_{safe}_track_files.zip')
    except Exception as ex:
        if temp_path:
            try:os.remove(temp_path)
            except OSError:pass
        return jsonify(dict(ok=False,error=str(ex))),400


# ==================== end v0.9.21 SCHEDULE / TRACK FILES ====================




@app.route('/api/pack/v2/export')
def pack_v2_export():
    try:
        payload, _report = _shared_season_pack_editor().export_bytes()
        return send_file(
            io.BytesIO(payload), mimetype='application/zip', as_attachment=True,
            download_name='nascar_shared_season_pack.gridpack',
        )
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400





@app.route('/api/pack/v2/preview', methods=['POST'])
def pack_v2_preview():
    upload = request.files.get('file')
    if not upload:
        return jsonify(dict(ok=False, error='no pack selected')), 400
    try:
        return jsonify(_shared_season_pack_editor().inspect_bytes(upload.read()))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400







@app.route('/api/pack/v2/import', methods=['POST'])
def pack_v2_import():
    upload = request.files.get('file')
    if not upload:
        return jsonify(dict(ok=False, error='no pack selected')), 400
    try:
        try:
            selected = json.loads(request.form.get('categories', '[]'))
        except (TypeError, ValueError):
            selected = []
        return jsonify(_shared_season_pack_editor().import_bytes(
            upload.read(), selected or None,
        ))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


# ---- reusable AI preset library + pit-strategy observation log ----
@app.route('/api/presets')
def presets_list():
    return jsonify(dict(ok=True,presets=_shared_user_library().presets()))


@app.route('/api/presets/save',methods=['POST'])
def presets_save():
    try:return jsonify(dict(ok=True,**_shared_user_library().save_preset(request.get_json(force=True))))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/presets/delete',methods=['POST'])
def presets_delete():
    try:
        q=request.get_json(force=True)
        return jsonify(dict(ok=True,**_shared_user_library().delete_preset(q.get('id'))))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/presets/export')
def presets_export():
    payload=_shared_user_library().export_presets_bytes()
    return send_file(io.BytesIO(payload),mimetype='application/json',as_attachment=True,
                     download_name='nascar_ai_presets.json')


@app.route('/api/presets/import',methods=['POST'])
def presets_import():
    upload=request.files.get('file')
    if not upload:return jsonify(dict(ok=False,error='no file')),400
    try:return jsonify(dict(ok=True,**_shared_user_library().import_presets_bytes(upload.read())))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/pitlog',methods=['GET','POST','DELETE'])
def pit_log_api():
    library=_shared_user_library()
    if request.method=='GET':return jsonify(dict(ok=True,rows=library.pit_entries()))
    try:
        q=request.get_json(force=True)
        result=(library.delete_pit_entry(q.get('id')) if request.method=='DELETE'
                else library.add_pit_entry(q))
        return jsonify(dict(ok=True,**result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


# ==================== v0.9.23 REPOINT / CUSTOM CONTAINERS ====================
# A cdfiles entry can now be safely moved to a new archive offset when a rebuilt
# replacement is larger or smaller than the indexed slot. The original indexed
# bytes remain in the archive; only the cdfiles offset/size pair changes.
_RP_ALIGNMENT = 16
_RP_MAX_SINGLE = 768 * 1024 * 1024
_RP_MAX_PACKAGE = 1536 * 1024 * 1024
_RP_HISTORY = os.path.join(USER_DIR, 'repoint_history.json')
_RP_LOCK = __import__('threading').RLock()


def _rp_sha256_file(path):
    import hashlib
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def _rp_sha256_range(path, offset, size):
    import hashlib
    h=hashlib.sha256();left=int(size)
    with open(path,'rb') as f:
        f.seek(int(offset))
        while left:
            b=f.read(min(8*1024*1024,left))
            if not b: raise ValueError('short archive read during SHA-256 verification')
            h.update(b);left-=len(b)
    return h.hexdigest()


def _rp_load_history():
    try:
        x=json.load(open(_RP_HISTORY,'r',encoding='utf-8'))
        return x if isinstance(x,list) else []
    except Exception:return []


def _rp_save_history(rows):
    atomic_write_json(_RP_HISTORY, rows[-2000:], indent=2)


def _rp_add_history(item):
    rows=_rp_load_history();rows.append(item);_rp_save_history(rows)


def _rp_category(name):
    u=str(name or '').upper()
    if u.endswith('.PYC'):return 'Database / PYC'
    if u.endswith('.LDA') or u.startswith('TEXT'):return 'UI Text / localization'
    if u.endswith(('.FSB','.SND')) or 'SOUND' in u or 'MUSIC' in u:return 'Audio'
    if any(k in u for k in ('BODY','INTERIORASS','WHEEL','BRAKEKIT','SUSPENSION','CHASSIS','PHY.ARC')):return 'Vehicle / model'
    if any(k in u for k in ('TRACK','RACEWAY','SPEEDWAY','REGION','GLOBALPHY')) or re.search(r'000[ANP]\.ARC$',u):return 'Track'
    if any(k in u for k in ('TEXTURE','MENU','HUD','TEAMSHOP','DRIVERSELECT','THUMB','FEI','LOGO')):return 'Images / UI'
    if u.endswith('.ARC'):return 'ARC container'
    return 'Other'


def _rp_magic_name(raw):
    b=bytes(raw[:16])
    if b[:4]==b'ARCC':return 'ARCC'
    if b[:4]==b'filC':return 'filC'
    if b[:3]==b'FSB':return b[:4].decode('ascii','replace')
    if b[:4]==b'PK\x03\x04':return 'ZIP'
    if b[:4]==b'DDS ':return 'DDS'
    return b[:8].hex(' ').upper() or '(empty)'


def _rp_index_rows(cdf_path):
    """Parse cdfiles and retain byte positions for size/offset repointing."""
    raw=bytearray(open(cdf_path,'rb').read())
    entries=_read_cdf_entries(cdf_path)
    if not entries:raise ValueError('cdfiles index contains no payload entries')
    rows=[]
    for entry in entries:
        size_field,offset_field=(2,5) if entry.layout=='A' else (4,7)
        rows.append(dict(index=entry.index,name=entry.name,offset=entry.archive_offset,size=entry.size,
                         record_pos=entry.record_offset,size_pos=entry.record_offset+size_field*4,
                         offset_pos=entry.record_offset+offset_field*4,layout=entry.layout))
    return raw,rows,entries[0].layout


def _rp_find_row(rows,name):
    req=str(name or '').replace('\\','/').casefold();base=req.rsplit('/',1)[-1]
    hits=[r for r in rows if r['name'].replace('\\','/').casefold()==req]
    if not hits:hits=[r for r in rows if r['name'].replace('\\','/').rsplit('/',1)[-1].casefold()==base]
    if not hits:raise ValueError('entry not found: '+str(name))
    if len(hits)>1:raise ValueError(f'entry is ambiguous: {name} ({len(hits)} matches)')
    return hits[0]


def _rp_find_any(reg,name,arc_hint=None):
    hits=[]
    for arcid,v in reg.items():
        if arc_hint is not None and str(arcid)!=str(arc_hint):continue
        try:
            _,rows,_=_rp_index_rows(v['cdf']);r=_rp_find_row(rows,name);hits.append((str(arcid),v,r))
        except ValueError as e:
            if 'ambiguous' in str(e):raise
    if not hits:raise ValueError('no indexed entry matches '+str(name))
    if len(hits)>1:raise ValueError(f'{name} exists in multiple archives; choose an archive')
    return hits[0]


def _rp_backup_pair(v):
    ensure_backup(v['ar'],v['bak'])
    ensure_backup(v['cdf'],backup_path(v['cdf']))


def _rp_validate_upload(entry_name,path,allow_magic=False):
    size=os.path.getsize(path)
    if size<=0:raise ValueError('replacement file is empty')
    if size>_RP_MAX_SINGLE:raise ValueError('replacement exceeds the 768 MB per-file safety limit')
    with open(path,'rb') as f:head=f.read(16)
    ext=os.path.splitext(str(entry_name))[1].upper()
    warnings=[]
    if ext=='.ARC' and head[:4]!=b'ARCC':
        msg=f'ARC target expects ARCC, but upload begins with {_rp_magic_name(head)}'
        if not allow_magic:raise ValueError(msg+'; enable advanced magic override only when this is intentional')
        warnings.append(msg)
    return dict(size=size,sha256=_rp_sha256_file(path),magic=_rp_magic_name(head),warnings=warnings)


def _rp_plan(arcid,v,row,path,allow_magic=False):
    meta=_rp_validate_upload(row['name'],path,allow_magic)
    archive_size=os.path.getsize(v['ar']);new_off=(archive_size+(_RP_ALIGNMENT-1))&~(_RP_ALIGNMENT-1)
    if row['offset']+row['size']>archive_size:raise ValueError('indexed entry exceeds current archive size')
    if new_off+meta['size']>=2**32:raise ValueError('replacement would exceed the 32-bit archive offset limit')
    upload_name=os.path.basename(path)
    return dict(archive=str(arcid),entry=row['name'],category=_rp_category(row['name']),
                old_offset=row['offset'],old_size=row['size'],new_offset=new_off,new_size=meta['size'],
                archive_size=archive_size,projected_archive_size=new_off+meta['size'],growth=new_off+meta['size']-archive_size,
                sha256=meta['sha256'],magic=meta['magic'],warnings=meta['warnings'],source_name=upload_name,
                filename_match=upload_name.casefold()==os.path.basename(row['name']).casefold())


def _rp_install_one(arcid,v,row,path,source_name=None,allow_magic=False,history=True):
    """Append + repoint transaction. On failure, truncate and restore the exact live cdf bytes."""
    if is_process_running('NASCAR15.exe'):raise ValueError('NASCAR15.exe is running; close the game first')
    plan=_rp_plan(arcid,v,row,path,allow_magic)
    _rp_backup_pair(v)
    old_archive_size=os.path.getsize(v['ar']);old_cdf=open(v['cdf'],'rb').read()
    raw,rows,layout=_rp_index_rows(v['cdf']);live_row=_rp_find_row(rows,row['name'])
    try:
        with open(v['ar'],'ab') as dst,open(path,'rb') as src:
            pad=plan['new_offset']-old_archive_size
            if pad:dst.write(b'\0'*pad)
            for b in iter(lambda:src.read(8*1024*1024),b''):dst.write(b)
            dst.flush();os.fsync(dst.fileno())
        if _rp_sha256_range(v['ar'],plan['new_offset'],plan['new_size'])!=plan['sha256']:
            raise ValueError('archive payload SHA-256 readback mismatch')
        struct.pack_into('<I',raw,live_row['size_pos'],plan['new_size'])
        struct.pack_into('<I',raw,live_row['offset_pos'],plan['new_offset'])
        atomic_write_bytes(v['cdf'],bytes(raw),'.repoint.tmp')
        _,check,_=_rp_index_rows(v['cdf']);vr=_rp_find_row(check,row['name'])
        if vr['offset']!=plan['new_offset'] or vr['size']!=plan['new_size']:
            raise ValueError('cdfiles readback did not retain the new offset/size')
    except Exception as install_ex:
        rollback_archive_cdf(v,old_archive_size,old_cdf,'.rollback.tmp',install_ex)
        raise
    item=dict(timestamp=__import__('datetime').datetime.now().isoformat(timespec='seconds'),
              archive=str(arcid),entry=row['name'],old_offset=plan['old_offset'],old_size=plan['old_size'],
              new_offset=plan['new_offset'],new_size=plan['new_size'],growth=plan['growth'],sha256=plan['sha256'],
              source_name=source_name or os.path.basename(path),category=plan['category'],verified=True)
    history_warning=None
    if history:
        try:_rp_add_history(item)
        except Exception as ex:history_warning='install verified, but history could not be saved: '+str(ex)
    _clear_ui_thumb_cache()
    return dict(ok=True,verified=True,plan=plan,history=item,history_warning=history_warning)


def _rp_extract_entry(v,row,out_path,source_archive=None):
    ar=source_archive or v['ar']
    with open(ar,'rb') as src,open(out_path,'wb') as dst:
        src.seek(row['offset']);left=row['size']
        while left:
            b=src.read(min(8*1024*1024,left))
            if not b:raise ValueError('short archive read')
            dst.write(b);left-=len(b)
    return out_path


def _rp_public_entry(arcid,row):
    return dict(archive=str(arcid),name=row['name'],offset=row['offset'],size=row['size'],
                category=_rp_category(row['name']),extension=os.path.splitext(row['name'])[1].upper() or '(none)')


@app.route('/api/repoint/status')
def repoint_status():
    try:
        g,reg=registry()
        if not g:return jsonify(dict(ok=False,error='game folder not selected')),400
        hist=_rp_load_history();archives=[]
        for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
            live=os.path.getsize(v['ar']);bak=os.path.getsize(v['bak']) if os.path.exists(v['bak']) else None
            cdfbak=os.path.exists(backup_path(v['cdf']))
            archives.append(dict(archive=str(arcid),size=live,backup_size=bak,
                                 growth_since_backup=(live-bak if bak is not None else None),
                                 archive_backup=bool(bak is not None),cdfiles_backup=bool(cdfbak),
                                 headroom=max(0,2**32-live),history_count=sum(1 for h in hist if str(h.get('archive'))==str(arcid))))
        return jsonify(dict(ok=True,game=g,archives=archives,history=hist[-100:][::-1],
                            history_count=len(hist),alignment=_RP_ALIGNMENT,max_single=_RP_MAX_SINGLE,
                            facts=['Variable-size installs append a rebuilt file at a 16-byte-aligned offset.',
                                   'The matching cdfiles offset and size are updated atomically.',
                                   'Original indexed bytes remain in the archive until a future compaction pass.',
                                   'Archive and cdfiles receive paired pristine backups before the first repoint.']))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/entries',methods=['POST'])
def repoint_entries():
    try:
        q=request.get_json(silent=True) or {};editor=_shared_resource_editor()
        arc=str(q.get('archive','all'));cat=str(q.get('category','all'));text=str(q.get('q','')).lower()
        page=max(0,int(q.get('page',0)));per=max(1,min(500,int(q.get('per',200))))
        rows=[];categories=set()
        archive_rows=editor.archives()
        for archive in archive_rows:
            arcid=archive['key']
            if arc!='all' and str(arcid)!=arc:continue
            for pub in editor.resources(arcid):
                categories.add(pub['category'])
                if cat!='all' and pub['category']!=cat:continue
                if text and text not in (pub['name']+' '+pub['category']+' ARCHIVE'+str(arcid)).lower():continue
                rows.append(pub)
        rows.sort(key=lambda x:(int(x['archive']),x['category'],x['name']))
        return jsonify(dict(ok=True,total=len(rows),page=page,per=per,rows=rows[page*per:(page+1)*per],
                            archives=sorted((row['key'] for row in archive_rows),key=int),categories=sorted(categories)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/inspect',methods=['POST'])
def repoint_inspect():
    try:
        q=request.get_json(force=True);arcid=str(q.get('archive'));editor=_shared_resource_editor()
        item=editor.inspect(q.get('entry'),arcid);pair=editor.installation.archive_pairs[arcid]
        public={key:item[key] for key in ('archive','name','offset','size','category')}
        return jsonify(dict(ok=True,entry=public,layout=item['layout'],magic=item['magic'],
                            archive_size=pair.archive.stat().st_size,stock=item['stock'],
                            export_current=f'/api/repoint/export?archive={arcid}&entry='+__import__('urllib.parse').parse.quote(item['name']),
                            export_stock=f'/api/repoint/export?stock=1&archive={arcid}&entry='+__import__('urllib.parse').parse.quote(item['name'])))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/preview',methods=['POST'])
def repoint_preview():
    tmp=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a replacement file')
        arcid=str(request.form.get('archive'));entry=request.form.get('entry');allow=request.form.get('allow_magic')=='1'
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_',suffix=os.path.splitext(up.filename or '')[1]);os.close(fd);up.save(tmp)
        editor=_shared_resource_editor();payload=Path(tmp).read_bytes();plan=editor.plan(entry,arcid,payload)
        if plan['warnings'] and not allow:raise ValueError(plan['warnings'][0]+'; enable advanced magic override only when this is intentional')
        plan['source_name']=up.filename or os.path.basename(tmp)
        plan['filename_match']=(os.path.basename(up.filename or '').casefold()==os.path.basename(plan['entry']).casefold())
        return jsonify(dict(ok=True,plan=plan))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


@app.route('/api/repoint/install',methods=['POST'])
def repoint_install():
    tmp=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a replacement file')
        arcid=str(request.form.get('archive'));entry=request.form.get('entry');allow=request.form.get('allow_magic')=='1'
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_',suffix=os.path.splitext(up.filename or '')[1]);os.close(fd);up.save(tmp)
        editor=_shared_resource_editor();payload=Path(tmp).read_bytes();plan=editor.plan(entry,arcid,payload)
        if plan['warnings'] and not allow:raise ValueError(plan['warnings'][0]+'; enable advanced magic override only when this is intentional')
        with _RP_LOCK:r=editor.replace(entry,arcid,payload)
        history=dict(timestamp=datetime.datetime.now().isoformat(timespec='seconds'),archive=arcid,
                     entry=plan['entry'],old_offset=plan['old_offset'],old_size=plan['old_size'],
                     new_offset=r.get('offset',plan['new_offset']),new_size=plan['new_size'],growth=plan['growth'],
                     sha256=plan['sha256'],source_name=up.filename or os.path.basename(tmp),
                     category=plan['category'],verified=True)
        history_warning=None
        try:_rp_add_history(history)
        except Exception as ex:history_warning='install verified, but history could not be saved: '+str(ex)
        r=dict(ok=True,verified=True,plan=plan,history=history,history_warning=history_warning,write=r)
        return jsonify(r)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


@app.route('/api/repoint/export')
def repoint_export():
    tmp=None
    try:
        arcid=str(request.args.get('archive'));entry=request.args.get('entry');stock=request.args.get('stock')=='1'
        editor=_shared_resource_editor();found_key,found_entry=editor.installation.find_entry(entry,arcid)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_export_');os.close(fd)
        editor.export(found_entry.name,found_key,tmp,pristine=stock)
        @after_this_request
        def _cleanup_export(response):
            try:os.remove(tmp)
            except OSError:pass
            return response
        return send_file(tmp,as_attachment=True,download_name=found_entry.name,mimetype='application/octet-stream',max_age=0)
    except Exception as ex:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/restore_entry',methods=['POST'])
def repoint_restore_entry():
    try:
        q=request.get_json(force=True);arcid=str(q.get('archive'));entry=q.get('entry')
        editor=_shared_resource_editor();stock_size=len(editor.read(entry,arcid,pristine=True))
        with _RP_LOCK:r=editor.restore(entry,arcid)
        r.update(ok=True,restored_stock_size=stock_size);return jsonify(r)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


def _rp_package_members(zip_path,reg):
    import zipfile
    plans=[];total=0;ignored=[]
    with zipfile.ZipFile(zip_path) as z:
        members=[i for i in z.infolist() if not i.is_dir() and not i.filename.startswith('__MACOSX/')]
        if len(members)>128:raise ValueError('package is limited to 128 files')
        by_norm={i.filename.replace('\\','/').lstrip('/'):i for i in members}
        manifest_info=next((i for i in members if i.filename.replace('\\','/').rsplit('/',1)[-1].casefold()=='repoint_manifest.json'),None)
        manifest_rows=None
        if manifest_info:
            try:
                manifest=json.loads(z.read(manifest_info).decode('utf-8-sig'))
                manifest_rows=manifest.get('files') if isinstance(manifest,dict) else None
                if not isinstance(manifest_rows,list) or not manifest_rows:raise ValueError('files must be a non-empty list')
            except Exception as ex:raise ValueError('invalid repoint_manifest.json: '+str(ex))
        candidates=[]
        if manifest_rows is not None:
            for item in manifest_rows:
                if not isinstance(item,dict):raise ValueError('manifest file rows must be objects')
                source=str(item.get('source') or '').replace('\\','/').lstrip('/')
                entry=str(item.get('entry') or '')
                arc_hint=item.get('archive')
                if not source or not entry:raise ValueError('each manifest row needs source and entry')
                info=by_norm.get(source)
                if not info:raise ValueError('manifest source not found in ZIP: '+source)
                candidates.append((info,entry,None if arc_hint is None else str(arc_hint)))
            ignored=[i.filename for i in members if i is not manifest_info and i not in {c[0] for c in candidates}]
        else:
            for info in members:
                low=info.filename.lower()
                if low.endswith(('manifest.json','readme.txt','technical_notes.md','.bat','.py','.json','.md')):ignored.append(info.filename);continue
                if info.file_size<=0:continue
                norm=info.filename.replace('\\','/');parts=norm.split('/');arc_hint=None
                for part in parts[:-1]:
                    m=re.fullmatch(r'ARCHIVE(\d*)',part,re.I)
                    if m:arc_hint=m.group(1) or '0';break
                candidates.append((info,parts[-1],arc_hint))
        for info,entry_name,arc_hint in candidates:
            if info.file_size<=0:continue
            if info.file_size>_RP_MAX_SINGLE:raise ValueError(info.filename+' exceeds the per-file limit')
            try:arcid,v,row=_rp_find_any(reg,entry_name,arc_hint)
            except ValueError as ex:
                if manifest_rows is not None or 'multiple archives' in str(ex) or 'ambiguous' in str(ex):raise
                ignored.append(info.filename);continue
            total+=info.file_size
            if total>_RP_MAX_PACKAGE:raise ValueError('matched package payloads exceed the 1.5 GB safety limit')
            plans.append(dict(info=info,archive=arcid,v=v,row=row))
    if not plans:raise ValueError('no package files matched indexed NASCAR 15 entries; use exact filenames or add repoint_manifest.json')
    seen=set()
    for p in plans:
        k=(p['archive'],p['row']['name'].casefold())
        if k in seen:raise ValueError('package targets the same indexed entry more than once: '+p['row']['name'])
        seen.add(k)
    return plans,total,ignored


@app.route('/api/repoint/package_preview',methods=['POST'])
def repoint_package_preview():
    tmp=None;td=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a ZIP package')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_package_',suffix='.zip');os.close(fd);up.save(tmp)
        g,reg=registry();matched,total,ignored=_rp_package_members(tmp,reg);td=tempfile.mkdtemp(prefix='n15mod_rpprev_')
        import zipfile
        out=[]
        with zipfile.ZipFile(tmp) as z:
            for i,p in enumerate(matched):
                fp=os.path.join(td,f'{i:03d}.bin')
                with z.open(p['info']) as src,open(fp,'wb') as dst:shutil.copyfileobj(src,dst,8*1024*1024)
                plan=_rp_plan(p['archive'],p['v'],p['row'],fp,False);plan['source_name']=p['info'].filename;plan['filename_match']=os.path.basename(p['info'].filename).casefold()==os.path.basename(p['row']['name']).casefold();out.append(plan)
        return jsonify(dict(ok=True,count=len(out),total_size=total,plans=out,ignored=ignored,ignored_count=len(ignored),
                            note='Package installs always append and repoint each matched file; unrelated ZIP members are ignored.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        if td:shutil.rmtree(td,ignore_errors=True)


@app.route('/api/repoint/package_install',methods=['POST'])
def repoint_package_install():
    tmp=None;td=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a ZIP package')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_package_',suffix='.zip');os.close(fd);up.save(tmp)
        g,reg=registry();matched,total,ignored=_rp_package_members(tmp,reg);td=tempfile.mkdtemp(prefix='n15mod_rpinstall_')
        import zipfile
        extracted=[]
        with zipfile.ZipFile(tmp) as z:
            for i,p in enumerate(matched):
                fp=os.path.join(td,f'{i:03d}_{os.path.basename(p["row"]["name"])}')
                with z.open(p['info']) as src,open(fp,'wb') as dst:shutil.copyfileobj(src,dst,8*1024*1024)
                _rp_validate_upload(p['row']['name'],fp,False);extracted.append((p,fp))
        # Atomic package transaction: every touched archive can be truncated back
        # to its exact pre-package size, and every cdfiles index can be restored
        # byte-for-byte, because package installs append rather than overwrite.
        touched={}
        for p,fp in extracted:
            _rp_backup_pair(p['v'])
            key=str(p['archive'])
            if key not in touched:
                touched[key]=dict(v=p['v'],archive_size=os.path.getsize(p['v']['ar']),cdf=open(p['v']['cdf'],'rb').read())
        results=[]
        try:
            with _RP_LOCK:
                for p,fp in extracted:
                    results.append(_rp_install_one(p['archive'],p['v'],p['row'],fp,p['info'].filename,False,history=False))
                hist=_rp_load_history();hist.extend(r['history'] for r in results);_rp_save_history(hist)
        except Exception as install_ex:
            # Attempt every archive even if one fails, so a single bad restore
            # cannot strand the remaining archives un-rolled-back. Failures are
            # collected and raised together rather than discarded.
            rollback_errors=[]
            for key,state in touched.items():
                try:
                    rollback_archive_cdf(state['v'],state['archive_size'],state['cdf'],
                                         '.package_rollback.tmp',install_ex)
                except RollbackFailed as rbf:
                    rollback_errors.append(f'ARCHIVE{key}: {rbf.rollback_error}')
            if rollback_errors:
                raise RollbackFailed(install_ex,'; '.join(rollback_errors))
            raise
        return jsonify(dict(ok=True,count=len(results),total_size=total,verified=True,atomic=True,ignored=ignored,ignored_count=len(ignored),
                            results=[r['history'] for r in results]))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        if td:shutil.rmtree(td,ignore_errors=True)

# ==================== end v0.9.23 REPOINT ====================


# ==================== v0.9.26.7 FULL PYC AUDIT ====================
_PYC_AUDIT_CACHE={}
_PYC_AUDIT_HANDLED={
    'DB_GAME_LOCAL_SCRIPT.PYC':'editable database: schedule, race laps, world pace/environment',
    'DB_AICONFIG_SCRIPT.PYC':'editable database: driver ratings, track AI and global AI',
}
_PYC_AUDIT_PARTIAL={
    'GSRACEPOINTS.PYC':'researched points code; not exposed in V1',
    'AIDRIVERPROFILES.PYC':'legacy/secondary AI profile candidate; audited, not edited by current Ratings tab',
    'AILAPTIMES.PYC':'AI lap-time table candidate; not mapped yet',
    'TRACKDATA.PYC':'track-data helper candidate; not mapped yet',
    'GSCAREERCALENDARHELPER.PYC':'Career calendar consumer; read-only audit candidate',
    'GSCAREERRACESPAWN.PYC':'Career race consumer; read-only audit candidate',
    'GSCAREERFUNCTIONSHELPER.PYC':'Career state helper; read-only audit candidate',
    'GSRACESPAWN.PYC':'general race consumer; read-only audit candidate',
    'GSTEAMSHOPRACESETTINGS.PYC':'Race Now/settings consumer; read-only audit candidate',
    'RACEEVENTS.PYC':'race-event constants/helper; read-only audit candidate',
    'EVENTINIT.PYC':'event initialization helper; read-only audit candidate',
    'GSRESULTSINTERFACE.PYC':'results/points UI consumer; read-only audit candidate',
    'GSINFIELDGARAGEHELPER.PYC':'career standings/points consumer; read-only audit candidate',
}
_PYC_AUDIT_KEYWORDS={
    'schedule':['schedule','calendar','numberinseries','racedata','raceevent','raceseries'],
    'career':['career','championship','standings','singleseason','single_season','season'],
    'ai_pace':['ai','laptime','lap time','practiceeasy','practicehard','qualifybasetime','catchup','racingline'],
    'race_physics':['physics','downforce','draft','aero','handling','race laps','racelaps'],
    'points':['points','bonuspoints','championshippoints','raceposition'],
    'livery':['livery','vinyl','paintscheme','manufacturer'],
    'ui_text':['interface','menu','hud','string','textid'],
    'audio':['sound','audio','commentary'],
    'track':['track','worldpointer','worldid','tripwire'],
    'driver':['driver','roster','profile'],
}
_PYC_AUDIT_PRIORITY=[
    'AILAPTIMES.PYC','AIDRIVERPROFILES.PYC','GSCAREERCALENDARHELPER.PYC','GSCAREERRACESPAWN.PYC',
    'GSCAREERFUNCTIONSHELPER.PYC','GSRACESPAWN.PYC','GSTEAMSHOPRACESETTINGS.PYC','RACEEVENTS.PYC',
    'EVENTINIT.PYC','GSRACEPOINTS.PYC','GSRESULTSINTERFACE.PYC','GSINFIELDGARAGEHELPER.PYC','TRACKDATA.PYC'
]

def _pyc_audit_mapper():
    path=component_path(MAPPER_NAME)
    if not os.path.exists(path):raise RuntimeError(MAPPER_NAME+' is missing')
    key=(os.path.realpath(path),os.path.getmtime(path))
    cached=_PYC_AUDIT_CACHE.get('mapper')
    if cached and cached[0]==key:return cached[1]
    mod = _load_module_from_path(path, 'n15_pyc_audit_mapper')
    _PYC_AUDIT_CACHE['mapper']=(key,mod);return mod

def _pyc_ascii(raw,minimum=4):
    # PYC strings are mostly ASCII. Limiting each string prevents huge embedded
    # text resources from making the audit response unwieldy.
    return [m.group().decode('latin1','replace')[:240] for m in re.finditer(rb'[ -~]{%d,}'%minimum,raw)]

def _pyc_audit_category(name,text):
    low=(name+' '+text).lower();scores={cat:sum(low.count(k) for k in keys) for cat,keys in _PYC_AUDIT_KEYWORDS.items()}
    cat=max(scores,key=scores.get) if scores else 'other'
    return (cat if scores.get(cat,0)>0 else 'other'),scores

def _pyc_audit_key(reg):
    parts=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try:
            a=os.stat(v['ar']);c=os.stat(v['cdf']);parts.append((str(arcid),a.st_size,a.st_mtime_ns,c.st_size,c.st_mtime_ns))
        except OSError:pass
    return tuple(parts)

def _pyc_audit_scan(force=False):
    g,reg=registry()
    if not g:raise RuntimeError('game folder not selected')
    key=_pyc_audit_key(reg)
    if not force and _PYC_AUDIT_CACHE.get('result_key')==key:return _PYC_AUDIT_CACHE['result']
    mapper=_pyc_audit_mapper();rows=[];archive_errors=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try:entries=parse_cdfiles(v['cdf'])
        except Exception as ex:archive_errors.append(dict(archive=str(arcid),error=str(ex)));continue
        for off,size,name in entries:
            if not str(name).upper().endswith('.PYC'):continue
            rec=dict(archive=str(arcid),file=str(name),offset=int(off),size=int(size),parse_ok=False,parse_error=None,
                     handled='unmapped',handled_detail=None,schedule_slots=None,sha256=None,category='other',priority=False,keywords=[])
            try:
                with open(v['ar'],'rb') as fh:fh.seek(off);raw=fh.read(size)
                if len(raw)!=size:raise RuntimeError('short archive read')
                rec['sha256']=_hl.sha256(raw).hexdigest()
                root=mapper.parse_pyc(raw);rec['parse_ok']=True
                strings=_pyc_ascii(raw);joined=' '.join(strings)
                cat,scores=_pyc_audit_category(name,joined);rec['category']=cat
                rec['keywords']=[k for k,vv in sorted(scores.items(),key=lambda kv:-kv[1]) if vv>0][:5]
                upper=str(name).upper()
                if upper in _PYC_AUDIT_HANDLED:
                    rec['handled']='editable';rec['handled_detail']=_PYC_AUDIT_HANDLED[upper]
                elif upper in _PYC_AUDIT_PARTIAL:
                    rec['handled']='candidate';rec['handled_detail']=_PYC_AUDIT_PARTIAL[upper]
                elif upper.startswith(('DB_','GS','WID_')) or cat!='other':
                    rec['handled']='research';rec['handled_detail']='discovered and parseable; no V1 editor mapped'
                else:
                    rec['handled']='library';rec['handled_detail']='runtime/library PYC; no data editor expected'
                rec['priority']=upper in _PYC_AUDIT_PRIORITY
                if upper==DBFILE.upper():
                    try:rec['schedule_slots']=len(_shared_schedule_editor().inspect_payload(raw))
                    except Exception as ex:rec['schedule_error']=str(ex)
                # Record/schema totals are useful for generated DB files, but avoid
                # the expensive constructor mapping on every runtime helper.
                if upper.startswith('DB_'):
                    try:
                        schemas=mapper.build_schemas(root);records=mapper.map_records(root,schemas)
                        rec['schemas']=len(schemas);rec['records']=len(records)
                    except Exception as ex:rec['record_map_error']=str(ex)
            except Exception as ex:
                rec['parse_error']=str(ex)
            rows.append(rec)
    by_name={}
    for r in rows:by_name.setdefault(r['file'].upper(),[]).append(r)
    for rs in by_name.values():
        for r in rs:r['duplicate_count']=len(rs);r['duplicate']=len(rs)>1
    rows.sort(key=lambda r:(not r['priority'],r['handled'] in ('library','unmapped'),r['category'],r['file'],int(r['archive'])))
    result=dict(ok=True,game=g,total=len(rows),parseable=sum(1 for r in rows if r['parse_ok']),
                editable=sum(1 for r in rows if r['handled']=='editable'),
                candidates=sum(1 for r in rows if r['handled'] in ('candidate','research')),
                duplicate_names=sum(1 for rs in by_name.values() if len(rs)>1),
                schedule_sources=sum(1 for r in rows if r.get('schedule_slots')==36),
                archives_scanned=len(reg),archive_errors=archive_errors,rows=rows,
                note='Every indexed .PYC in every detected archive is listed. Candidate does not mean safe to edit; it means the file deserves mapping/research.')
    _PYC_AUDIT_CACHE['result_key']=key;_PYC_AUDIT_CACHE['result']=result
    return result

@app.route('/api/pyc/audit')
def pyc_audit():
    try:
        force=request.args.get('force')=='1';return jsonify(_pyc_audit_scan(force))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/pyc/audit/export')
def pyc_audit_export():
    try:
        result=_pyc_audit_scan(False);fmt=request.args.get('format','csv').lower()
        if fmt=='json':
            return Response(json.dumps(result,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=nascar_pyc_audit.json'})
        import io
        out=io.StringIO();fields=['archive','file','offset','size','sha256','parse_ok','parse_error','handled','handled_detail','category','priority','duplicate_count','schedule_slots','schemas','records','keywords']
        w=_csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for row in result['rows']:
            r=dict(row);r['keywords']=';'.join(r.get('keywords') or []);w.writerow(r)
        return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nascar_pyc_audit.csv'})
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end v0.9.26.7 FULL PYC AUDIT ====================

# ---- self-check / support report ----
def _support_checks():
    return _shared_support_reporter().checks()

@app.route('/api/support/check')
def support_check():
    c,s=_support_checks();return jsonify(dict(ok=s['fail_count']==0,checks=c,summary=s,app_name=APP_NAME,version=APP_VERSION,release_label=APP_RELEASE_LABEL))

@app.route('/api/support/report')
def support_report():
    b=io.BytesIO(_shared_support_reporter().report_bytes())
    return send_file(b,mimetype='text/plain',as_attachment=True,download_name=f'nascar15_modding_app_v{APP_VERSION}_support.txt')








@app.route('/api/full_repair/check')
def full_repair_check_api():
    try:
        return jsonify(_shared_full_repair_editor().check())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/full_repair/apply', methods=['POST'])
def full_repair_apply_api():
    try:
        return jsonify(_shared_full_repair_editor().apply())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/full_repair/report')
def full_repair_report_api():
    try:
        payload = json.dumps(
            _shared_full_repair_editor().report(), indent=2,
        ).encode('utf-8')
        return send_file(
            io.BytesIO(payload), mimetype='application/json', as_attachment=True,
            download_name=f'nascar15_whole_mod_repair_v{APP_VERSION}.json',
        )
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 404

# ==================== end v0.9.31.3 FAILURE-FOCUSED WHOLE MOD REPAIR ====================



@app.route('/api/help/request',methods=['POST'])
def help_request_package():
    """Create a small, shareable support ZIP without copying game archives."""
    import zipfile,platform
    q=request.get_json(silent=True) or {}
    area=str(q.get('area') or 'Other').strip()[:120]
    contact=str(q.get('contact') or '').strip()[:240]
    summary=str(q.get('summary') or '').strip()[:180]
    details=str(q.get('details') or '').strip()[:8000]
    if not summary and not details:
        return jsonify(dict(ok=False,error='add a summary or description first')),400
    created=datetime.datetime.now()
    checks,check_summary=_support_checks()
    g,reg=registry()
    cfg=load_cfg()
    request_text='\n'.join([
        f'{APP_NAME} help request',
        f'Created: {created.isoformat(timespec="seconds")}',
        f'App version: {APP_VERSION} {APP_RELEASE_LABEL}',
        f'Area: {area}',
        f'Contact: {contact or "not provided"}',
        '',
        f'Summary: {summary or "not provided"}',
        '',
        'What happened:',
        details or 'not provided',
        '',
        'Please attach this entire ZIP when using the configured help form or email.',
    ])+'\n'
    support_text='\n'.join([
        f'{APP_NAME} v{APP_VERSION} installation check',
        f'Result: {check_summary["pass_count"]} pass, {check_summary["warn_count"]} warning, {check_summary["fail_count"]} fail',
        '',
        *[f'[{c["status"].upper()}] {c["name"]}: {c["detail"]}' for c in checks],
    ])+'\n'
    environment=dict(
        app_name=APP_NAME,app_version=APP_VERSION,release_label=APP_RELEASE_LABEL,
        created=created.isoformat(timespec='seconds'),
        platform=platform.platform(),python=sys.version.split()[0],frozen=bool(getattr(sys,'frozen',False)),
        game_found=bool(g),game_folder=g,archive_groups=sorted(reg.keys()),
        backup_groups=sum(1 for v in reg.values() if os.path.exists(v['bak'])),
        texconv_ready=bool(texconv_path()),ffmpeg_ready=bool(ffmpeg_path()),
        interface_settings={k:v for k,v in _app_settings_payload(cfg).items() if k not in ('help_destination','support_destination')},
    )
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('HELP_REQUEST.txt',request_text.encode('utf-8'))
        z.writestr('INSTALLATION_CHECK.txt',support_text.encode('utf-8'))
        z.writestr('ENVIRONMENT.json',json.dumps(environment,indent=2).encode('utf-8'))
        z.writestr('README.txt',b'This package was created by NASCAR 15 Modding App. It contains no game archives. Send the entire ZIP to the project owner.\n')
    out.seek(0)
    stamp=created.strftime('%Y%m%d_%H%M%S')
    return send_file(out,mimetype='application/zip',as_attachment=True,download_name=f'nascar15_help_request_{stamp}.zip')

# ==================== v0.9.29.7 PROVEN EXTRA-SLOT RUNTIME REPAIR ====================
EXTRA_SCHEME_HELPER = 'nascar15_extra_scheme_manager_v1.py'
EXTRA_THUMBNAIL_HELPER = 'nascar15_thumbnail_native_v25.py'
EXTRA_STOCK_THUMBNAIL_HELPER = 'nascar15_thumbnail_stock_legacy_v25.py'
EXTRA_LEGACY_SCHEME_HELPER = 'nascar15_extra_scheme_manager_rc10.py'
EXTRA_LEGACY_THUMBNAIL_HELPER = 'nascar15_thumbnail_native_legacy_v25.py'
EXTRA_FIXED_TEMPLATE_HELPER = 'nascar15_fixed_template_stock_paint_rc10.py'
EXTRA_SCHEME_STATE = os.path.join(USER_DIR, 'extra_schemes_v1.json')
EXTRA_SCHEME_IMAGES = os.path.join(SCHEMES, 'extra')
EXTRA_SCHEME_ROLLBACK_DIR = os.path.join(USER_DIR, 'extra_scheme_rollback_v1')
EXTRA_SCHEME_LIMIT_PER_DRIVER = 8
_EXTRA_SCHEME_MOD = None
_EXTRA_THUMBNAIL_MOD = None
_EXTRA_STOCK_THUMBNAIL_MOD = None
_EXTRA_LEGACY_SCHEME_MOD = None
_EXTRA_LEGACY_THUMBNAIL_MOD = None
_EXTRA_FIXED_TEMPLATE_MOD = None
_EXTRA_CREATE_LOCK = threading.Lock()
os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)


def extra_scheme_mod():
    global _EXTRA_SCHEME_MOD
    if _EXTRA_SCHEME_MOD is not None:
        # Re-apply each call so a UID verdict takes effect without a restart.
        return ManagedPaintEditor.apply_uid_pool(_EXTRA_SCHEME_MOD, os.path.join(USER_DIR, ManagedPaintEditor.UID_STATE_NAME))
    _EXTRA_SCHEME_MOD = _load_internal_module(
        EXTRA_SCHEME_HELPER, 'n15_extra_scheme_manager'
    )
    return ManagedPaintEditor.apply_uid_pool(_EXTRA_SCHEME_MOD, os.path.join(USER_DIR, ManagedPaintEditor.UID_STATE_NAME))


def extra_thumbnail_mod():
    global _EXTRA_THUMBNAIL_MOD
    if _EXTRA_THUMBNAIL_MOD is not None:
        return _EXTRA_THUMBNAIL_MOD
    _EXTRA_THUMBNAIL_MOD = _load_internal_module(
        EXTRA_THUMBNAIL_HELPER, 'n15_thumbnail_native_v25'
    )
    return _EXTRA_THUMBNAIL_MOD


def extra_stock_thumbnail_mod():
    """Load the untouched v0.9.30.5 stock-team thumbnail backend.

    This module is intentionally separate from the team-aware thumbnail backend.
    Stock paint creation therefore cannot inherit custom-team identity experiments
    through shared helper functions.
    """
    global _EXTRA_STOCK_THUMBNAIL_MOD
    if _EXTRA_STOCK_THUMBNAIL_MOD is not None:
        return _EXTRA_STOCK_THUMBNAIL_MOD
    _EXTRA_STOCK_THUMBNAIL_MOD = _load_internal_module(
        EXTRA_STOCK_THUMBNAIL_HELPER, 'n15_thumbnail_stock_legacy_v25'
    )
    return _EXTRA_STOCK_THUMBNAIL_MOD


def extra_legacy_scheme_mod():
    """Load the byte-for-byte v0.9.29.9 extra-scheme manager for creation only.

    The current manager remains active for catalog, repair, and diagnostics. This
    loader exists so the known-good database/asset append path cannot inherit
    later custom-team experiments.
    """
    global _EXTRA_LEGACY_SCHEME_MOD
    if _EXTRA_LEGACY_SCHEME_MOD is not None:
        return _EXTRA_LEGACY_SCHEME_MOD
    _EXTRA_LEGACY_SCHEME_MOD = _load_internal_module(
        EXTRA_LEGACY_SCHEME_HELPER, 'n15_extra_scheme_manager_legacy_v1'
    )
    return _EXTRA_LEGACY_SCHEME_MOD


def extra_legacy_thumbnail_mod():
    """Load the byte-for-byte v0.9.29.9 native thumbnail writer for creation."""
    global _EXTRA_LEGACY_THUMBNAIL_MOD
    if _EXTRA_LEGACY_THUMBNAIL_MOD is not None:
        return _EXTRA_LEGACY_THUMBNAIL_MOD
    _EXTRA_LEGACY_THUMBNAIL_MOD = _load_internal_module(
        EXTRA_LEGACY_THUMBNAIL_HELPER, 'n15_thumbnail_native_legacy_v25'
    )
    return _EXTRA_LEGACY_THUMBNAIL_MOD


def extra_fixed_template_mod():
    """Load the exact v0.10 fixed-count stock-team Paint Select writer."""
    global _EXTRA_FIXED_TEMPLATE_MOD
    if _EXTRA_FIXED_TEMPLATE_MOD is not None:
        return _EXTRA_FIXED_TEMPLATE_MOD
    _EXTRA_FIXED_TEMPLATE_MOD = _load_internal_module(
        EXTRA_FIXED_TEMPLATE_HELPER, 'n15_fixed_template_stock_paint_v1'
    )
    return _EXTRA_FIXED_TEMPLATE_MOD


def _legacy_stock_creation_guard(driver_uid):
    """Block only a live moved/spare-team link; ignore all historical app state.

    This guard cannot false-lock a restored stock driver because it never reads
    driver_source_teams or prior move history. When no pristine team map exists,
    only explicit spare/custom team UIDs are blocked.
    """
    links = _team_fast_driver_links()
    driver = links.get(int(driver_uid))
    if not driver:
        return {'locked': True, 'reason': 'the driver has no current 2015 Cup team link'}
    team_uid = int(driver['team_uid'])
    config_uid = int(driver['config_uid'])
    if team_uid in SUPPORTED_SPARE_TEAM_UIDS:
        return {'locked': True, 'reason': 'added paint slots remain blocked for spare/custom teams'}
    originals = _team_original_team_map()
    original_uid = int(originals.get(config_uid, team_uid))
    moved = bool(team_uid != original_uid)
    return {'locked': False, 'team_uid': team_uid, 'config_uid': config_uid,
            'moved': moved, 'original_team_uid': original_uid,
            'experimental_moved_driver': moved}


def _extra_transaction_snapshot(reg, groups=('0','1','2'), inplace_thumbnail=None):
    return _shared_paint_transaction().snapshot(groups, inplace_thumbnail)


def _extra_transaction_restore(snapshot):
    return _shared_paint_transaction().restore(snapshot) if snapshot else []


def _extra_clear_persisted_snapshot():
    return _shared_paint_checkpoint().clear()


def _extra_persist_snapshot(snapshot, label, operation=None):
    return _shared_paint_checkpoint().persist(snapshot, label, operation)


def _extra_seal_persisted_snapshot(operation=None):
    return _shared_paint_checkpoint().seal(operation)


def _extra_thumbnail_replace_capability(game, driver, uid, target_container=None):
    """Read-only check for whether an added-scheme thumbnail can be safely rewritten.

    A live thumbnail can already work in game even when there is no same-bank donor
    available for a future repair/rewrite. Surface that honestly in the UI instead
    of warning that a repair is needed when the only safe action is export/use-as-is.
    """
    tm = extra_thumbnail_mod()
    uid = int(uid)
    out = {'supported': False, 'uid': uid, 'container': target_container or ''}
    if target_container is None:
        preview_container, _driver = _team_preview_container_for_driver(int((driver or {}).get('uid', -1)))
        target_container = preview_container
        out['container'] = preview_container
    try:
        identity = tm.inspect_thumbnail_identity(game, uid, target_container_name=target_container) or {}
    except Exception as ex:
        identity = {}
        out['identity_error'] = str(ex)
    out['identity_name'] = identity.get('identity_name') or identity.get('identity_root_name') or ''
    out['live_present'] = bool(identity.get('exists')) if identity else False
    if identity.get('same_bank_valid'):
        out.update({'supported': True, 'mode': 'self', 'reason': ''})
        return out
    try:
        donor = _extra_thumbnail_donor(game, driver, exclude_uid=uid, target_container=target_container)
        out.update({
            'supported': True,
            'mode': 'donor',
            'reason': '',
            'donor_uid': int(donor.get('uid', -1)),
            'donor_container': str(donor.get('container') or ''),
            'identity_name': out.get('identity_name') or str(donor.get('identity_name') or ''),
        })
    except Exception as ex:
        out.update({'supported': False, 'mode': 'unavailable', 'reason': str(ex)})
    return out

def _extra_thumbnail_donor(game, driver, exclude_uid=None, target_container=None):
    tm = extra_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid, target_container_name=target_container)
        if not hit:
            continue
        entry = hit[3]
        identity = tm.inspect_thumbnail_identity(
            game, uid, target_container_name=target_container)
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5' and identity.get('same_bank_valid')):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name'],
                    'identity_name': identity.get('identity_name')}
    # A thumbnail resource is only a same-bank structural donor. It does not
    # need to belong to the same driver. Requiring that made valid clean-game
    # drivers impossible to repair when their own stock tile used an alias or
    # no self-identifying 256x256 anchor. Fall back to any safe native paint
    # identity in the destination team bank; the imported thumbnail pixels are
    # still written afterward.
    if target_container:
        try:
            ta = team_assets_mod()
            team_uid = int(re.search(r'_(\d+)\.ARC$', str(target_container), re.I).group(1))
            for resource in ta.team_container_resource_names(game, team_uid):
                m = re.fullmatch(r'PAINTSCHEME_(\d+)', str(resource), re.I)
                if not m:
                    continue
                uid = int(m.group(1))
                if exclude_uid is not None and uid == int(exclude_uid):
                    continue
                hit = tm.find_target(game, uid, target_container_name=target_container)
                if not hit:
                    continue
                entry = hit[3]
                identity = tm.inspect_thumbnail_identity(
                    game, uid, target_container_name=target_container)
                if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                        and str(entry.get('fmt')) == 'DXT5'
                        and identity.get('same_bank_valid')):
                    return {'uid': uid, 'container': hit[1]['name'],
                            'entry': entry['name'],
                            'identity_name': identity.get('identity_name'),
                            'fallback_scope': 'same_team_bank'}
        except Exception:
            pass
    raise ValueError('No structurally safe 256×256 native thumbnail exists in this team bank')


def _extra_thumbnail_donor_stock_legacy(game, driver, exclude_uid=None, target_container=None):
    """v0.9.30.5 donor selection for the protected native stock-team path."""
    tm = extra_stock_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid, target_container_name=target_container)
        if not hit:
            continue
        entry = hit[3]
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5'):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name']}
    raise ValueError('No compatible 256×256 native thumbnail exists for this driver')


def _extra_thumbnail_donor_legacy299(game, driver, exclude_uid=None):
    """Exact v0.9.29.9 donor selection using the exact legacy v2.5 writer."""
    tm = extra_legacy_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid)
        if not hit:
            continue
        entry = hit[3]
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5'):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name']}
    raise ValueError('No compatible 256×256 native thumbnail exists for this driver')


def _extra_prepare_thumbnail_source(raw, quality='auto'):
    if not raw:
        raise ValueError('choose a thumbnail image')
    image = Image.open(io.BytesIO(raw)); image.load()
    prepared, prep = _extra_prepare_thumbnail(image, (256, 256), quality)
    return prepared, prep


def _extra_read_live_native_thumbnail_preview(game, uid, target_container):
    """Decode a live Paint Select thumbnail without changing game files.

    Upgrade discovery must recover the image the game already uses even when an
    older app build did not preserve its source PNG or when the strict native
    identity audit needs a later repair.  This reader validates the current
    team-bank resource geometry and payload bounds, but deliberately does not
    require the write-path identity guard because it is read-only.
    """
    tm = extra_thumbnail_mod()
    hit = tm.find_target(game, int(uid), target_container_name=target_container)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {target_container}')
    _archive, _row, arc, _legacy_entry = hit
    entries, _ = C.parse_multi_arc(arc)
    name = f'PAINTSCHEME_{int(uid)}'
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None:
        raise ValueError(f'{name} is missing from the native texture table')
    if (str(entry.get('fmt')) != 'DXT5' or int(entry.get('w', 0)) != 256
            or int(entry.get('h', 0)) != 256
            or int(entry.get('payload_size', 0)) < int(entry.get('needed', 0))):
        raise ValueError('live thumbnail is not a complete 256×256 DXT5 resource')
    return C.multi_read_png(arc, entry)


def _extra_read_live_native_thumbnail(game, uid, target_container):
    """Decode the exact live PAINTSCHEME resource from the current team bank."""
    tm = extra_thumbnail_mod()
    identity = tm.inspect_thumbnail_identity(
        game, int(uid), target_container_name=target_container)
    if not identity.get('same_bank_valid'):
        raise ValueError(
            f'PAINTSCHEME_{int(uid)} does not have valid current-team native wiring')
    hit = tm.find_target(game, int(uid), target_container_name=target_container)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {target_container}')
    _archive, _row, arc, _legacy_entry = hit
    entries, _ = C.parse_multi_arc(arc)
    name = f'PAINTSCHEME_{int(uid)}'
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None:
        raise ValueError(f'{name} is missing from the canonical native texture table')
    if entry['fmt'] != 'DXT5' or int(entry['payload_size']) < int(entry['needed']):
        raise ValueError('native thumbnail payload bounds or format are invalid')
    return C.multi_read_png(arc, entry)


def _extra_save_live_native_thumbnail(game, uid, target_container, out_path):
    """Save a browser preview decoded from the exact live native resource."""
    image = _extra_read_live_native_thumbnail(game, uid, target_container)
    image.save(out_path, 'PNG')
    return image


def _extra_update_preview_state(mod, uid, report, source_name):
    state = mod.load_state(EXTRA_SCHEME_STATE)
    item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
    if item is None:
        raise ValueError('the created scheme disappeared from the app state')
    safe_clone = bool(report.get('game_safe_raw_clone'))
    safe_custom = bool(report.get('game_safe_same_bank_custom') or report.get('game_safe_stock_legacy'))
    safe = bool(safe_clone or safe_custom)
    item.update({
        'preview_status': ('safe_clone' if safe_clone else
                           'custom_same_bank' if safe_custom else 'custom_unverified'),
        'preview_container': report.get('container'),
        'preview_entry': f'PAINTSCHEME_{int(uid)}',
        'preview_method': report.get('method', 'native_expand_v25'),
        'preview_encoder': report.get('encoder'),
        'preview_readback_verified': bool(report.get('readback_verified')),
        'preview_game_verified': False,
        'thumbnail_source_png': os.path.basename(source_name),
        'thumbnail_requested': True,
        'thumbnail_installed': int(time.time()),
        'thumbnail_game_safe': safe,
        'thumbnail_same_bank_identity': bool(report.get('game_safe_same_bank_custom') or report.get('game_safe_raw_clone')),
        'thumbnail_stock_legacy_safe': bool(report.get('game_safe_stock_legacy')),
        'thumbnail_identity_name': report.get('identity_name'),
    })
    mod.save_state(EXTRA_SCHEME_STATE, state)
    return item


def _extra_game_and_registry():
    g, reg = registry()
    if not g:
        raise RuntimeError('No game folder is selected. Choose one on the Setup tab.')
    if not reg:
        raise RuntimeError(
            'No game archives were found in ' + os.path.join(g, 'data') + '. '
            'Set the game folder to the folder that contains data\\ARCHIVE0.AR '
            '(the install root, not the data folder itself) on the Setup tab.')
    missing = [k for k in ('0', '1', '2') if k not in reg]
    if missing:
        raise RuntimeError(
            'This game folder is missing ' +
            ', '.join(f'ARCHIVE{k}.AR/cdfiles{k}.dat' for k in missing) +
            '. Paint tools need archives 0, 1 and 2. Verify the game files, or '
            'restore a known-good backup.')
    return g, reg


def _extra_game_running():
    return is_process_running('NASCAR15.exe')


def _extra_backups(reg, groups=('0', '1', '2')):
    for key in groups:
        v = need(reg, key)
        ensure_backup(v['ar'], v['bak'])
        ensure_backup(v['cdf'], backup_path(v['cdf']))


def _extra_ai_backup_paths(reg):
    v = need(reg, '0')
    return backup_path(v['ar']), backup_path(v['cdf'])


def _extra_state_public():
    try:
        mod = extra_scheme_mod()
        try:
            game, _reg = _extra_game_and_registry()
            _shared_managed_paint_editor().reconcile_live_state()
        except Exception:
            # Read-only callers still receive the last valid state when no game
            # is selected; write routes perform their own hard preflight.
            pass
        return mod.load_state(EXTRA_SCHEME_STATE)
    except Exception:
        return {'format': 'nascar15-extra-schemes-v1', 'version': 1, 'schemes': [], 'assignments': {}, 'ai': {}}


def _extra_prepare_thumbnail(image, target_size, quality='auto'):
    """Smart-import a thumbnail while preserving its aspect ratio and alpha."""
    q = str(quality or 'auto').lower().strip()
    if q not in SCHEME_SMART_QUALITIES:
        q = 'auto'
    tw, th = map(int, target_size)
    sw, sh = image.size
    if q in ('direct', '1'):
        factor = 1
    elif q in ('2', '4'):
        factor = int(q)
    else:
        factor = 1 if (sw > tw or sh > th) else 2
    lanczos = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
    if factor > 1:
        stage, prep = prepare_import_image(
            image, (tw * factor, th * factor), 'fit', preserve_alpha=True
        )
        out = stage.resize((tw, th), lanczos)
    else:
        out, prep = prepare_import_image(
            image, (tw, th), 'fit', preserve_alpha=True
        )
    prep.update({
        'quality_requested': q,
        'supersample_factor': factor,
        'quality_policy': ('direct smart fit' if factor == 1 else f'{factor}x smart fit then Lanczos downsample'),
    })
    return out.convert('RGBA'), prep


_EXTRA_DRIVER_SCRIPT_TAILS = {
    'PRIMARY', 'SECONDARY', 'TERTIARY', 'ALT', 'ALTERNATE', 'BEER',
    'THROWBACK', 'TEST', 'DEFAULT', 'SPECIAL', 'NIGHT', 'DAY'
}


def _extra_name_word(word):
    u = str(word or '').upper()
    if u in ('AJ', 'JJ'):
        return u
    if u in ('JR', 'JNR'):
        return 'Jr.'
    if u.startswith('MC') and len(u) > 2:
        return 'Mc' + u[2:].lower().capitalize()
    return u.lower().capitalize()


def _extra_driver_name_from_schemes(driver):
    """Turn a stock 2015 livery script into a full driver name.

    DRIVER_c name tokens usually contain only an initial (for example
    S_DRIVER_A_ALMIROLA). The public schedule should show Aric Almirola instead
    of exposing that internal token-style abbreviation.
    """
    candidates = []
    for scheme in driver.get('schemes', []):
        script = str(scheme.get('script_name') or '')
        m = re.match(r'^15_[0-9]+[A-Z]?_(.+)$', script, re.I)
        if not m:
            continue
        parts = [p for p in m.group(1).split('_') if p]
        while parts and (parts[-1].upper() in _EXTRA_DRIVER_SCRIPT_TAILS or parts[-1].isdigit()):
            parts.pop()
        if len(parts) < 2:
            continue
        label = ' '.join(_extra_name_word(p) for p in parts)
        score = 0
        if script.upper().endswith('_PRIMARY'):
            score += 5
        if scheme.get('year') == 2015:
            score += 3
        if not scheme.get('managed'):
            score += 1
        candidates.append((score, label))
    if not candidates:
        return str(driver.get('label') or f"Driver {driver.get('uid', '')}").strip()
    label = max(candidates, key=lambda x: (x[0], len(x[1])))[1]
    token = str(driver.get('token') or '').upper()
    if token.endswith('_JR') and not label.lower().endswith('jr.'):
        label += ' Jr.'
    return label


def _extra_friendly_catalog(out):
    cfg = load_cfg()
    renames = cfg.get('renames', {}) if isinstance(cfg, dict) else {}
    rename_exact = {str(k).casefold(): str(v) for k, v in renames.items()}
    for driver in out.get('drivers', []):
        stock_name = _extra_driver_name_from_schemes(driver)
        driver['stock_label'] = stock_name
        driver['label'] = rename_exact.get(stock_name.casefold(), stock_name)
    return out


def _extra_recommended_donor(driver):
    eligible = [s for s in (driver or {}).get('schemes', []) if s.get('donor_eligible') and not s.get('managed')]
    if not eligible:
        eligible = [s for s in (driver or {}).get('schemes', []) if s.get('donor_eligible')]
    def score(s):
        script = str(s.get('script_name') or '').upper()
        label = str(s.get('label') or '').casefold()
        return (
            0 if script.endswith('_PRIMARY') or label == 'primary' else 1,
            0 if s.get('year') == 2015 else 1,
            0 if script.startswith('15_') else 1,
            0 if not s.get('world_token') else 1,
            int(s.get('uid', 999999)),
        )
    return min(eligible, key=score) if eligible else None


@app.route('/api/extra_schemes/uid_pool')
def extra_uid_pool():
    try:
        return jsonify(_shared_managed_paints().uid_pool())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/uid_verdict', methods=['POST'])
def extra_uid_verdict():
    q = request.get_json(silent=True) or {}
    try:
        return jsonify(_shared_managed_paints().record_uid_verdict(
            int(q.get('uid')), q.get('verdict'), q.get('note') or '',
        ))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/catalog')
def extra_schemes_catalog():
    """Return the fast paint/driver catalog used by the Paint tab.

    Deep thumbnail identity scans intentionally live in Paint System Check.  The
    older working Paint tab only loaded the database catalog here; performing a
    full team/art scan once per driver made Reload Paint Data look hung and could
    hide an otherwise valid driver list when an unrelated thumbnail check failed.
    """
    try:
        g, _reg = _extra_game_and_registry()
        mod = extra_scheme_mod()
        shared_catalog = _shared_managed_paint_editor().catalog()
        reconciliation = shared_catalog.get('live_reconciliation') or {}
        out = _extra_friendly_catalog(shared_catalog)
        proven = mod.proven_extra_donor(g)
        state = mod.load_state(EXTRA_SCHEME_STATE)
        active = [x for x in state.get('schemes', []) if not x.get('superseded_by')]
        out['needs_runtime_repair'] = any(int(x.get('native_runtime_layout_version', 0)) < 1 for x in active)
        out['runtime_repair_count'] = sum(int(x.get('native_runtime_layout_version', 0)) < 1 for x in active)
        out['proven_donor'] = proven
        out['helper'] = EXTRA_SCHEME_HELPER
        out['state_file'] = os.path.basename(EXTRA_SCHEME_STATE)
        out['paint_rollback'] = _shared_managed_paint_editor().undo_status()
        counts = collections.Counter(
            int(x.get('driver_uid', -1)) for x in active if x.get('driver_uid') is not None
        )

        # Build the team-link inputs once.  The previous route rebuilt the entire
        # Team Editor catalog for every driver, which is the reload regression.
        team_links = _team_fast_driver_links()
        original_links = _team_original_team_map()
        team_state = _team_state_load()
        for driver in out.get('drivers', []):
            uid = int(driver['uid'])
            driver['recommended_donor_uid'] = int(proven['uid'])
            created = int(counts.get(uid, 0))
            driver['created_count'] = created
            driver['created_limit'] = EXTRA_SCHEME_LIMIT_PER_DRIVER
            driver['created_remaining'] = max(0, EXTRA_SCHEME_LIMIT_PER_DRIVER - created)
            guard = _stable_paint_creation_guard(
                uid, driver=team_links.get(uid), originals=original_links,
                state=team_state)
            driver['paint_creation_locked'] = bool(guard.get('locked'))
            driver['paint_creation_lock_reason'] = guard.get('reason') or ''
            driver['paint_creation_guard'] = guard
            driver['can_create'] = bool(driver['created_remaining'] and out.get('next_uid') is not None
                                        and not driver['paint_creation_locked'])
        for driver in out.get('drivers', []):
            managed_rows = [s for s in (driver.get('schemes') or []) if s.get('managed') and not s.get('superseded_by')]
            if not managed_rows:
                continue
            try:
                preview_container, _team_driver = _team_preview_container_for_driver(int(driver.get('uid', -1)))
            except Exception as ex:
                preview_container = None
                preview_error = str(ex)
            else:
                preview_error = ''
            for scheme in managed_rows:
                scheme['thumbnail_replace_supported'] = None
                scheme['thumbnail_replace_reason'] = preview_error
                scheme['thumbnail_replace_mode'] = ''
                scheme['thumbnail_replace_container'] = preview_container or ''
                if preview_container:
                    cap = _extra_thumbnail_replace_capability(g, driver, int(scheme.get('uid', -1)), preview_container)
                    scheme['thumbnail_replace_supported'] = bool(cap.get('supported'))
                    scheme['thumbnail_replace_reason'] = str(cap.get('reason') or '')
                    scheme['thumbnail_replace_mode'] = str(cap.get('mode') or '')
                    if cap.get('identity_name'):
                        scheme['thumbnail_identity_name'] = scheme.get('thumbnail_identity_name') or cap.get('identity_name')

        out['created_limit_per_driver'] = EXTRA_SCHEME_LIMIT_PER_DRIVER
        out['preview_note'] = ('Paint and driver data loads through the proven fast catalog path. '
                               'Use Paint System Check for the deeper live thumbnail identity audit.')
        payload = dict(out)
        payload['ok'] = True
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


# ---------------------------------------------------------------- app data move
# Mods live in the game's own archives (plus our .n15mod.bak beside them), so a
# new app version never touches them. What DOES live in this folder is the app's
# record of what it created: which paint slots exist, team-editor history, saved
# paint art. Extracting a new version into a clean folder loses that record, and
# then created slots are still in the game but invisible here. These two routes
# move that record across. They are reachable before a game is selected, because
# importing into a fresh install is the whole point.
def _shared_appdata_manager():
    return AppDataManager(USER_DIR, APP_VERSION)


def _shared_support_reporter():
    installation = None
    try:
        installation = _shared_installation()
    except Exception:
        pass
    return SupportReporter(installation, APP_DIR, USER_DIR, APP_VERSION, APP_RELEASE_LABEL)


@app.route('/api/appdata/export')
def appdata_export():
    """One zip holding this install's record of your work. No game files."""
    try:
        buf = io.BytesIO(_shared_appdata_manager().export_bytes())
        buf.seek(0)
        stamp = datetime.datetime.now().strftime('%Y%m%d')
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=f'nascar_app_data_{stamp}.zip')
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/appdata/import', methods=['POST'])
def appdata_import():
    """Restore a zip made by /api/appdata/export into this install."""
    f = request.files.get('file')
    if not f:
        return jsonify(dict(ok=False, error='No file was uploaded.')), 400
    try:
        result = _shared_appdata_manager().import_bytes(f.read())
        return jsonify(dict(ok=True, **result, note=(
            f"Restored {result['restored']} file(s). Restart the app so it reloads them."
            + (f" Your previous data was kept in {result['recovery_path']}."
               if result['recovery_path'] else '')
        )))
    except zipfile.BadZipFile:
        return jsonify(dict(ok=False, error='That file is not a readable zip.')), 400
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/paint_system/check')
def paint_system_check_api():
    try:
        return jsonify(_shared_full_repair_editor().paint_system_check())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/create', methods=['POST'])
def extra_schemes_create():
    """Create a native stock-team slot through the proven v0.9 + v0.10 path.

    The database and SD/HD assets use the exact ApplyPatch recipe that produced
    the working 337th livery. Paint Select is rebuilt from the smallest
    compatible game-authored fixed-count stock template; the chosen container's
    count is never expanded, and each DRIVERPAINT/3DNUM pair stays on one donor identity.
    UIDs 25600+ and spare/custom teams are rejected. Transferred drivers on
    authored stock teams are enabled only in this guarded experimental branch.
    """
    try:
        paint_file = request.files.get('file')
        thumbnail_file = request.files.get('thumbnail')
        if not paint_file:
            raise ValueError('choose a paint image')
        if not thumbnail_file:
            raise ValueError('choose a thumbnail image')
        with _EXTRA_CREATE_LOCK, tempfile.TemporaryDirectory(prefix='nascar_paint_create_') as folder:
            paint_path = os.path.join(folder, 'paint.png')
            thumbnail_path = os.path.join(folder, 'thumbnail.png')
            paint_file.save(paint_path)
            thumbnail_file.save(thumbnail_path)
            result = _shared_managed_paint_editor().create(
                int(request.form.get('driver_uid')),
                str(request.form.get('name') or ''), paint_path, thumbnail_path,
                quality=str(request.form.get('quality') or 'auto'),
            )
        try:
            _clear_ui_thumb_cache()
        except Exception:
            pass
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/runtime_repair', methods=['POST'])
def extra_schemes_runtime_repair():
    """Rebuild existing app-created slots to the exact proven DB donor recipe
    and rewrite both SD and HD files through their native page maps.

    EVENTINIT and thumbnail containers are deliberately untouched.
    """
    try:
        with _EXTRA_CREATE_LOCK:
            result = _shared_managed_paint_editor().repair_runtime()
        try: _clear_ui_thumb_cache()
        except Exception: pass
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/thumbnail/<int:uid>', methods=['POST'])
def extra_scheme_thumbnail(uid):
    try:
        upload = request.files.get('file')
        with _EXTRA_CREATE_LOCK, tempfile.TemporaryDirectory(prefix='nascar_thumbnail_') as folder:
            source = None
            if upload:
                source = os.path.join(folder, 'thumbnail.png')
                upload.save(source)
            result = _shared_managed_paint_editor().replace_thumbnail(
                int(uid), source, quality=str(request.form.get('quality') or 'auto'))
        try: _clear_ui_thumb_cache()
        except Exception: pass
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/previews/repair', methods=['POST'])
def extra_scheme_preview_repair():
    return jsonify(dict(
        ok=False,
        error='Use Replace Thumbnail or Repair Thumbnail Identity beside the individual scheme.'
    )), 400


@app.route('/api/extra_schemes/finalize', methods=['POST'])
def extra_schemes_finalize():
    return jsonify(dict(
        ok=False,
        error='The retired registry finalizer is disabled because it never fixed native slot visibility.'
    )), 400


def _extra_scheme_image_path(uid, field):
    state = _extra_state_public()
    item = next((x for x in state.get('schemes', [])
                 if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
    if not item or not item.get(field):
        return None
    path = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(item[field])))
    return path if os.path.exists(path) else None


def _extra_read_live_paint_image(item, game=None, reg=None):
    """Decode mip 0 from the live SD livery when the app PNG is missing.

    Version upgrades can legitimately lose the app-side source file while the
    complete paint remains installed in ARCHIVE2.  The archive is authoritative,
    so previews and Export Paint must still work.
    """
    if game is None or reg is None:
        game, reg = _extra_game_and_registry()
    entry_name = str(item.get('sd_entry') or ('LIVERY_' + str(item.get('script_name') or '') + '.ARC'))
    hit = None
    for arcid, info in reg.items():
        try:
            for off, size, name in parse_cdfiles(info['cdf']):
                if name == entry_name:
                    hit = (arcid, int(off), int(size)); break
        except Exception:
            continue
        if hit:
            break
    if not hit:
        raise ValueError(f'live paint asset was not found: {entry_name}')
    arcid, off, size = hit
    if size < RAW_OFFSET + (2048 // 4) * (1024 // 4) * 8:
        raise ValueError(f'live SD paint wrapper is too short: {size} bytes')
    with open(reg[arcid]['ar'], 'rb') as fh:
        fh.seek(off + RAW_OFFSET)
        payload = fh.read((2048 // 4) * (1024 // 4) * 8)
    if len(payload) != (2048 // 4) * (1024 // 4) * 8:
        raise ValueError('short read while decoding live paint')
    return Image.fromarray(dxt1_decode(payload, 2048, 1024)).convert('RGB')


def _extra_cache_recovered_paint(item, image):
    os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)
    name = f"{int(item['uid'])}__{item.get('script_name') or 'RECOVERED'}.recovered.png"
    path = os.path.join(EXTRA_SCHEME_IMAGES, name)
    image.save(path, 'PNG')
    try:
        mod = extra_scheme_mod()
        state = mod.load_state(EXTRA_SCHEME_STATE)
        row = next((x for x in state.get('schemes', [])
                    if int(x.get('uid', -1)) == int(item['uid']) and not x.get('superseded_by')), None)
        if row is not None:
            row['source_png'] = name
            row['source_recovered_from_live'] = True
            row['source_recovered_at'] = int(time.time())
            mod.save_state(EXTRA_SCHEME_STATE, state)
    except Exception:
        pass
    return path


@app.route('/api/extra_schemes/preview/paint/<int:uid>')
def extra_scheme_paint_preview(uid):
    try:
        path = _extra_scheme_image_path(uid, 'source_png')
        if path:
            return send_file(path, mimetype='image/png', conditional=True, max_age=0)
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', [])
                     if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
        if not item:
            return ('not found', 404)
        image = _extra_read_live_paint_image(item)
        path = _extra_cache_recovered_paint(item, image)
        return send_file(path, mimetype='image/png', conditional=True, max_age=0)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/preview/thumbnail/<int:uid>')
def extra_scheme_thumbnail_preview(uid):
    try:
        path = _extra_scheme_image_path(uid, 'thumbnail_source_png')
        if path:
            return send_file(path, mimetype='image/png', conditional=True, max_age=0)
        # Legacy working custom thumbnails may predate the saved preview PNG.
        # Decode the live current-team resource instead of showing a false blank.
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', [])
                     if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
        if not item:
            return ('not found', 404)
        g, _reg = _extra_game_and_registry()
        container, _driver = _team_preview_container_for_driver(int(item['driver_uid']))
        image = _extra_read_live_native_thumbnail_preview(g, uid, container)
        buf = io.BytesIO(); image.save(buf, 'PNG'); buf.seek(0)
        return send_file(buf, mimetype='image/png', conditional=True, max_age=0)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/source/<int:uid>')
def extra_scheme_source(uid):
    try:
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
        if not item:
            return ('not found', 404)
        path = None
        if item.get('source_png'):
            candidate = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(item['source_png']))
            if os.path.exists(candidate):
                path = candidate
        if not path:
            image = _extra_read_live_paint_image(item)
            path = _extra_cache_recovered_paint(item, image)
        return send_file(path, mimetype='image/png', as_attachment=True,
                         download_name=f"{item.get('name') or item.get('script_name')}.png")
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/source_thumbnail/<int:uid>')
def extra_scheme_thumbnail_source(uid):
    try:
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
        if not item:
            return ('not found', 404)
        path = _extra_scheme_image_path(uid, 'thumbnail_source_png')
        if path:
            return send_file(path, mimetype='image/png', as_attachment=True,
                             download_name=f"{item.get('name') or item.get('script_name')}_thumbnail.png")
        g, _reg = _extra_game_and_registry()
        container, _driver = _team_preview_container_for_driver(int(item['driver_uid']))
        image = _extra_read_live_native_thumbnail_preview(g, uid, container)
        buf = io.BytesIO(); image.save(buf, 'PNG'); buf.seek(0)
        return send_file(buf, mimetype='image/png', as_attachment=True,
                         download_name=f"{item.get('name') or item.get('script_name')}_thumbnail.png")
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/remove/<int:uid>', methods=['POST'])
def extra_scheme_remove(uid):
    """Delete an added scheme from the live game, with exact rollback when possible.

    A matching creation checkpoint can still reverse the newest creation
    byte-for-byte. Fresh app folders do not have that checkpoint, so the public
    fallback removes the target ApplyPatch block directly from the current live
    database, preserving every unrelated record and permanently retiring the
    dormant asset UID.
    """
    try:
        with _EXTRA_CREATE_LOCK:
            result = _shared_managed_paint_editor().remove(int(uid))
        try: _clear_ui_thumb_cache()
        except Exception: pass
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex), rolled_back=False)), 400

@app.route('/api/extra_schemes/undo_status')
def extra_scheme_undo_status():
    return jsonify(dict(ok=True, **_shared_managed_paint_editor().undo_status()))


@app.route('/api/extra_schemes/undo', methods=['POST'])
def extra_scheme_undo():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before undoing a paint change')
        with _EXTRA_CREATE_LOCK:
            result = _shared_managed_paint_editor().undo()
            try:
                _clear_ui_thumb_cache()
            except Exception:
                pass
            return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/repair/<int:uid>', methods=['POST'])
def extra_scheme_repair(uid):
    return jsonify(dict(
        ok=False,
        error=('Legacy identity replacement is disabled. Use Repair App-Created Slots '
               'to rebuild the existing UID without orphaning race assignments.')
    )), 400


def _extra_unsafe_assigned_thumbnail_uids():
    """Return assigned app-created schemes whose live thumbnail wiring is unsafe.

    State flags are not enough after a driver transfer: the destination team bank
    can change while the saved method still says the thumbnail was previously
    valid. Re-read table words 2/6 from the driver's current bank before allowing
    EVENTINIT installation.
    """
    state = _extra_state_public()
    managed = {
        int(x['uid']): x for x in state.get('schemes', [])
        if x.get('uid') is not None and not x.get('superseded_by')
    }
    assigned = set()
    for rows in (state.get('assignments') or {}).values():
        for value in (rows or {}).values():
            try:
                assigned.add(int(value))
            except Exception:
                pass
    relevant = sorted(assigned & set(managed))
    if not relevant:
        return []
    try:
        g, _reg = _extra_game_and_registry()
        tm = extra_thumbnail_mod()
    except Exception:
        return relevant
    unsafe = []
    for uid in relevant:
        item = managed[uid]
        try:
            target_container, _driver = _team_preview_container_for_driver(
                int(item['driver_uid']))
            identity = tm.inspect_thumbnail_identity(
                g, uid, target_container_name=target_container)
            live_safe = bool(identity.get('same_bank_valid'))
        except Exception:
            live_safe = False
        if not live_safe:
            unsafe.append(uid)
    return unsafe


@app.route('/api/ai_paints/assignments', methods=['GET', 'POST'])
def ai_paint_assignments_api():
    try:
        editor = _shared_managed_paint_editor()
        if request.method == 'GET':
            return jsonify(dict(ok=True, assignments=editor.assignments(),
                                state=_extra_state_public().get('ai', {}),
                                base_status=editor.ai_status()))
        q = request.get_json(force=True) or {}
        result = editor.save_assignments(q.get('assignments') or {})
        return jsonify(dict(result,
                            note='Assignments saved in the app. Use Preview, then Apply to write EVENTINIT.PYC.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/preview', methods=['POST'])
def ai_paint_preview_api():
    try:
        # AI selection consumes the live livery/database identity, not the
        # Paint Select thumbnail. A damaged menu tile is reported separately and
        # must never block a valid race assignment.
        return jsonify(_shared_managed_paint_editor().preview_ai())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/apply', methods=['POST'])
def ai_paint_apply_api():
    try:
        # Thumbnail health is presentation-only; validate/apply the live
        # livery and EVENTINIT schedule independently.
        out = _shared_managed_paint_editor().apply_ai()
        payload = dict(out)
        payload['ok'] = True
        payload['note'] = ('AI paint schedule installed. Races and drivers without an assignment '
                           "still use the game's normal paint selection.")
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/restore', methods=['POST'])
def ai_paint_restore_api():
    try:
        out = _shared_managed_paint_editor().restore_ai()
        payload = dict(out)
        payload['ok'] = True
        payload['note'] = ('Original AI paint selection restored. Your saved schedule remains in the app '
                           'and can be installed again later.')
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/export')
def extra_schemes_export():
    """Export app-created scheme metadata, AI assignments, and source PNGs.

    This is intentionally separate from the older mod-pack importer so users can
    preserve/share the new feature before full season-pack installation support
    is expanded in the next packaging pass.
    """
    try:
        out = io.BytesIO(_shared_managed_paint_editor().export_library_bytes())
        out.seek(0)
        return send_file(out, mimetype='application/zip', as_attachment=True,
                         download_name='nascar15_extra_schemes_and_ai_paints.zip')
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

# ==================== end v0.9.29.7 ====================


# ==================== v0.9.27.2 RC3 SETTINGS + HELP/UI POLISH ====================
# Ratings outside 0-100 are supported without experimental wording.
# Settings now cover spacing, text size, page width, previews, startup, and browser behavior.
# Help requests generate a shareable ZIP with no game archives.
# ==================== end v0.9.27.2 ====================

# ==================== v0.9.30.5 DRIVER ART REPAIR HOTFIX ====================
BANK_VERIFY_HELPER = 'nascar15_bank_verify_v1.py'
_BANK_VERIFY_MOD = None


def bank_verify_mod():
    """Verifies game files AFTER a write, by checking the artifact itself.

    Every corruption bug found during the moved-driver investigation was
    invisible to the app because each validator re-derived the numbers that
    produced the output instead of inspecting the output.
    """
    global _BANK_VERIFY_MOD
    if _BANK_VERIFY_MOD is not None:
        return _BANK_VERIFY_MOD
    _BANK_VERIFY_MOD = _load_internal_module(
        BANK_VERIFY_HELPER, 'n15_bank_verify'
    )
    return _BANK_VERIFY_MOD


TEAM_ASSETS_HELPER = 'nascar15_team_assets_v1.py'
TEAM_MANAGER_STATE = os.path.join(USER_DIR, 'team_manager_state.json')
TEAM_ASSET_ROLLBACK_DIR = os.path.join(USER_DIR, 'team_asset_rollback_v1')
_TEAM_ASSETS_MOD = None
_TEAM_MANAGER_LOCK = threading.RLock()

TEAM_DISPLAY_NAMES = {
    1325: 'Richard Petty Motorsports',
    1326: 'JTG Daugherty Racing',
    1327: 'Front Row Motorsports',
    1331: 'Roush Fenway Racing',
    1333: 'Richard Childress Racing',
    1335: 'Joe Gibbs Racing',
    1336: 'Team Penske',
    1340: 'Hendrick Motorsports',
    1341: 'Wood Brothers Racing',
    1347: 'Tommy Baldwin Racing',
    1349: 'Chip Ganassi Racing',
    1351: 'Stewart-Haas Racing',
    1352: 'Germain Racing',
    1354: 'Michael Waltrip Racing',
    1355: 'Furniture Row Racing',
    5762: 'JR Motorsports',
    11444: 'Phil Parsons Racing',
    2403: 'Custom Chevrolet',
    2405: 'Custom Ford',
    2406: 'Custom Toyota',
    8739: 'Leavine Family Racing',
    10518: 'BK Racing',
    23035: 'Premium Motorsports',
    25381: 'HScott Motorsports',
    25430: 'Go FAS Racing',
}
TEAM_MANUFACTURER_NAMES = {
    1015: 'Ford',
    1076: 'Chevrolet',
    1078: 'Toyota',
}
# Three stock Driver Select logos use short/legacy resource names in the clean game.
# Treat them as normal TEAM_<uid> resources everywhere in the public UI.
TEAM_LOGO_RESOURCE_ALIASES = {
    1327: 'ms',       # Front Row Motorsports
    1333: 'mk__r41',  # Richard Childress Racing
    25430: 'mk',      # Go FAS Racing
}

def _team_logo_entry_name(team_uid, entries):
    wanted = f'TEAM_{int(team_uid)}'
    names = {str(e.get('name') if isinstance(e, dict) else getattr(e, 'name', '')) for e in entries}
    if wanted in names:
        return wanted
    alias = TEAM_LOGO_RESOURCE_ALIASES.get(int(team_uid))
    return alias if alias in names else None

SUPPORTED_SPARE_TEAM_UIDS = (2403, 2405, 2406)
# Paint-slot creation for spare/custom teams was locked because appended
# resources corrupted the destination bank: a cross-bank copy transplanted the
# SOURCE bank's directory header into a non-first chunk, and the whole team bank
# then failed to load (fatal at Team Select).
#
# Root cause fixed in nascar15_team_assets_v1.py - _strip_source_directory_header
# plus the _assert_single_directory_header output check. Install that file
# BEFORE enabling this.
#
# Still unproven: whether an app-created paint slot can be SELECTED without
# crashing once the bank loads cleanly. That was never reachable while the bank
# itself was broken. Enable this to find out, on a disposable copy first.
SPARE_TEAM_PAINT_CREATION_ENABLED = False
UNSUPPORTED_TEAM_UIDS = (2404,)
UNSUPPORTED_MANUFACTURER_UIDS = (1074,)
TEAM_DEFAULT_LOGO_DONORS = {2403: 1333, 2405: 1336, 2406: 1335}

# Custom/reserve teams are enabled for the paths that passed the in-game gate:
# driver transfer, native/DLC paint-bank carryover, team logo/name/manufacturer,
# presentation rebuild, and Driver Select art.  The independent app-created
# paint-slot writer remains blocked by _stable_paint_creation_guard for these UIDs.
PUBLIC_CUSTOM_TEAMS_ENABLED = True
PUBLIC_CUSTOM_TEAM_MESSAGE = (
    'Custom teams support driver transfers, existing paint schemes, logos, names, '
    'manufacturer changes and menu artwork. Adding brand new paint slots to a custom team '
    'is not available yet.'
)

def _public_custom_team_guard(team_uid, action='edit this team'):
    if not PUBLIC_CUSTOM_TEAMS_ENABLED and int(team_uid) in SUPPORTED_SPARE_TEAM_UIDS:
        raise ValueError(PUBLIC_CUSTOM_TEAM_MESSAGE + ' Cannot ' + str(action) + '.')

def _public_custom_team_locked(team_uid):
    return bool(not PUBLIC_CUSTOM_TEAMS_ENABLED and int(team_uid) in SUPPORTED_SPARE_TEAM_UIDS)




def team_assets_mod():
    global _TEAM_ASSETS_MOD
    if _TEAM_ASSETS_MOD is not None:
        return _TEAM_ASSETS_MOD
    _TEAM_ASSETS_MOD = _load_internal_module(
        TEAM_ASSETS_HELPER,
        'nascar15_team_assets_runtime',
        'team presentation helper is missing: ' + TEAM_ASSETS_HELPER,
        'could not load the team presentation helper',
    )
    return _TEAM_ASSETS_MOD


def _team_state_load():
    base = {
        'format': 'nascar15-team-manager-v1',
        'version': 1,
        'driver_teams': {},
        'team_manufacturers': {},
        'team_names': {},
        'driver_source_teams': {},
        'team_logo_donors': {},
        'thumbnail_overrides': {},
        'history': [],
    }
    try:
        raw = json.load(open(TEAM_MANAGER_STATE, 'r', encoding='utf-8'))
        if isinstance(raw, dict):
            for key in ('driver_teams', 'team_manufacturers', 'driver_source_teams', 'team_logo_donors'):
                if isinstance(raw.get(key), dict):
                    base[key] = {str(k): int(v) for k, v in raw[key].items()}
            if isinstance(raw.get('thumbnail_overrides'), dict):
                base['thumbnail_overrides'] = {
                    str(k): dict(v) for k, v in raw['thumbnail_overrides'].items()
                    if isinstance(v, dict)
                }
            if isinstance(raw.get('team_names'), dict):
                base['team_names'] = {str(k): str(v) for k, v in raw['team_names'].items() if str(v).strip()}
            if isinstance(raw.get('history'), list):
                base['history'] = raw['history'][-100:]
    except Exception:
        pass
    return base


def _team_state_save(state):
    atomic_write_json(TEAM_MANAGER_STATE, state, indent=2)


def _team_driver_labels():
    labels={}
    try:
        for link in load_driver_links():
            base=str(link.get('base') or '').upper()
            if base:labels[base]=_driver_display_from_link(link)
    except Exception:pass
    return labels


def _team_asset_statuses_direct(game, team_uids):
    """Read-only stock/live presentation status without optional helper imports.

    Team Manager used to convert any helper import error into "Logo needed" and
    "Paint container needed" for every team.  That made valid stock resources
    look missing.  This path uses app.py's already-proven archive/CDF and ARCC
    readers, and reports an explicit diagnostic instead of inventing damage.
    """
    result={}
    for value in team_uids:
        uid=int(value)
        result[uid]=dict(team_uid=uid,logo_ready=False,paint_container_ready=False,
                         paint_resource_count=0,presentation_ready=False,
                         status_source='direct archive scan')
    if not game:
        return result
    _g,reg=registry()
    logo_names=set()
    logo_entries=[]
    try:
        off,size=find_entry(reg,'0','2DRIVERSELECTMENUIMAGE.ARC')
        with open(reg['0']['ar'],'rb') as fh:
            fh.seek(off); raw=fh.read(size)
        logo_entries,_=C.parse_multi_arc(raw)
        logo_names={str(e.get('name') or '') for e in logo_entries}
    except Exception:
        logo_names=set()
    td_rows={}
    try:
        for arcid,name,off,size in td_containers(reg):
            td_rows[str(name).casefold()]=(arcid,off,size)
    except Exception:
        td_rows={}
    for uid,st in result.items():
        st['logo_ready']=bool(_team_logo_entry_name(uid, logo_entries))
        name=f'2DRIVERSELECTTD_{uid}.ARC'
        hit=td_rows.get(name.casefold())
        if hit:
            st['paint_container_ready']=True
            try:
                arcid,off,size=hit
                with open(reg[arcid]['ar'],'rb') as fh:
                    fh.seek(off); raw=fh.read(size)
                entries,_=C.parse_multi_arc(raw)
                st['paint_resource_count']=len(entries)
            except Exception as ex:
                st['status_warning']='container exists but could not be parsed: '+str(ex)
        st['presentation_ready']=bool(st['logo_ready'] and st['paint_container_ready'])
    return result


def _team_friendly_catalog():
    data = _shared_team_editor().catalog()
    cfg = load_cfg()
    renames = cfg.get('renames', {}) if isinstance(cfg.get('renames', {}), dict) else {}
    base_labels = _team_driver_labels()
    manufacturer_labels = dict(TEAM_MANUFACTURER_NAMES)
    for m in data.get('manufacturers', []):
        uid = int(m['uid'])
        m['label'] = manufacturer_labels.get(uid, m.get('label') or m.get('token') or str(uid))
    state = _team_state_load()
    data['manufacturers'] = [m for m in data.get('manufacturers', []) if int(m.get('uid', -1)) not in UNSUPPORTED_MANUFACTURER_UIDS]
    data['teams'] = [t for t in data.get('teams', []) if int(t.get('uid', -1)) not in UNSUPPORTED_TEAM_UIDS]
    team_by_uid = {}
    for team in data.get('teams', []):
        uid = int(team['uid'])
        original = TEAM_DISPLAY_NAMES.get(uid, team.get('label') or team.get('token') or f'Team {uid}')
        team['original_label'] = original
        saved_name = state.get('team_names', {}).get(str(uid))
        team['label'] = saved_name or renames.get(original, original)
        team['manufacturer_label'] = manufacturer_labels.get(team.get('manufacturer_uid'), 'Unknown')
        team['is_spare'] = team.get('category') == 'spare'
        team['public_locked'] = _public_custom_team_locked(uid)
        team['public_lock_reason'] = PUBLIC_CUSTOM_TEAM_MESSAGE if team['public_locked'] else ''
        team_by_uid[uid] = team
    try:
        extra_state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
        created_by_driver = collections.defaultdict(list)
        for item in extra_state.get('schemes', []):
            if item.get('uid') is None or item.get('superseded_by'):
                continue
            created_by_driver[int(item.get('driver_uid', -1))].append(int(item['uid']))
    except Exception:
        created_by_driver = collections.defaultdict(list)
    for d in data.get('drivers', []):
        friendly = base_labels.get(str(d.get('base_arc') or '').upper())
        if friendly:
            # Respect a current full-name rename when the original roster name is known.
            d['label'] = renames.get(friendly, friendly)
        d['car_label'] = (('#' + str(d.get('number'))) if d.get('number') else 'No number') + ' ' + str(d.get('label') or '')
        team = team_by_uid.get(int(d.get('team_uid') or -1))
        d['team_label'] = team.get('label') if team else f"Team UID {d.get('team_uid')}"
        created_uids = sorted(created_by_driver.get(int(d.get('driver_uid', -1)), []))
        d['created_scheme_uids'] = created_uids
        d['created_scheme_count'] = len(created_uids)
        d['team_move_locked'] = bool(created_uids)
        d['team_move_lock_reason'] = (
            'This driver has extra paint slots you added: ' + ', '.join(map(str, created_uids)) +
            '. Moving a driver with added paint slots is not supported yet, because their thumbnails '
            'do not survive the move. Remove the extra slots first, or move the driver before adding any.' if created_uids else '')
        d['current_team_public_locked'] = _public_custom_team_locked(int(d.get('team_uid') or -1))
        d['recovery_move_available'] = bool(d['current_team_public_locked'] and not created_uids)
    members = {}
    driver_by_cfg = {int(d['config_uid']): d for d in data.get('drivers', [])}
    for d in data.get('drivers', []):
        members.setdefault(int(d['team_uid']), []).append(d)
    for team in data.get('teams', []):
        team['drivers'] = members.get(int(team['uid']), [])
        team['driver_count'] = len(team['drivers'])
    g = None
    team_status_warning = None
    try:
        g, _reg = registry()
        statuses = team_assets_mod().team_asset_statuses(g, [int(t['uid']) for t in data.get('teams', [])]) if g else {}
    except Exception as ex:
        team_status_warning = 'Team presentation helper fallback used: ' + str(ex)
        statuses = _team_asset_statuses_direct(g, [int(t['uid']) for t in data.get('teams', [])]) if g else {}
    for team in data.get('teams', []):
        team.update(statuses.get(int(team['uid']), {
            'logo_ready': False, 'paint_container_ready': False,
            'paint_resource_count': 0, 'presentation_ready': False
        }))
    try:
        art_locations = team_assets_mod().driver_art_location_map(g) if g else {}
    except Exception:
        art_locations = {}
    for d in data.get('drivers', []):
        preferred = f"2DRIVERSELECTTD_{int(d.get('team_uid', -1))}.ARC"
        locations = list(art_locations.get(int(d.get('driver_uid', -1)), []))
        chosen = preferred if preferred in locations else (locations[0] if locations else None)
        d['art_container'] = chosen
        d['art_locations'] = locations
        d['art_uses_fallback'] = bool(chosen and chosen != preferred)
        d['driver_art_ready'] = bool(chosen)
    data['state'] = state
    data['history'] = state.get('history', [])[-20:]
    data['asset_rollback'] = _team_asset_rollback_info()
    data['supported_spare_team_uids'] = list(SUPPORTED_SPARE_TEAM_UIDS)
    data['custom_team_capacity'] = len(SUPPORTED_SPARE_TEAM_UIDS)
    data['custom_teams_in_use'] = sum(1 for t in data.get('teams', []) if int(t.get('uid',-1)) in SUPPORTED_SPARE_TEAM_UIDS and int(t.get('driver_count',0))>0)
    data['team_status_warning'] = team_status_warning
    data['public_custom_teams_enabled'] = bool(PUBLIC_CUSTOM_TEAMS_ENABLED)
    data['public_custom_team_message'] = PUBLIC_CUSTOM_TEAM_MESSAGE
    data['warnings'] = [
        'A transfer copies the driver\u2019s artwork into the new team first, then makes the move. Drivers with paint slots you have added cannot be moved yet.',
        'Manufacturer switching updates the team association used by the game, including the matching Chevrolet, Ford, or Toyota body package.',
        PUBLIC_CUSTOM_TEAM_MESSAGE,
    ]
    data['driver_by_config'] = driver_by_cfg
    return data


def _team_install_changes(changes, source_name='Teams editor'):
    if _extra_game_running():
        raise RuntimeError('NASCAR15.exe is running. Close the game before changing teams')
    result = _shared_team_editor().apply(changes)
    return dict(ok=True, changed=result['changed'], verified=True,
                patch={'changes': result['changes'], 'recovery': result.get('recovery')},
                install=result.get('write'), source=source_name)


def _team_reapply_saved_links():
    """Reapply safe saved links after a clean-base DB rebuild.

    Reapply saved authored-team and supported custom-team links after a clean-base
    database rebuild. Drivers with app-created paint slots remain protected from
    cross-team reapplication by the existing moved-paint guard.
    """
    state = _team_state_load()
    changes = []
    skipped_public_custom = []
    for uid, target in state.get('driver_teams', {}).items():
        if _public_custom_team_locked(int(target)):
            skipped_public_custom.append(dict(kind='driver_team', uid=int(uid), target_uid=int(target)))
            continue
        driver = _team_driver_by_config_uid(int(uid))
        if driver is not None:
            current_team = int(driver.get('team_uid', -1))
            created_uids = [int(x) for x in (driver.get('created_scheme_uids') or [])]
            if current_team != int(target) and created_uids:
                raise ValueError(
                    'Saved team-link repair is blocked for ' + str(driver.get('car_label') or uid) +
                    ' because app-created paint slot(s) ' + ', '.join(map(str, created_uids)) +
                    ' would cross teams through the unresolved thumbnail path.')
        changes.append(dict(class_name='DRIVERCONFIG_c', uid=int(uid), field='TEAM', target_uid=int(target)))
    for uid, target in state.get('team_manufacturers', {}).items():
        if _public_custom_team_locked(int(uid)):
            skipped_public_custom.append(dict(kind='team_manufacturer', uid=int(uid), target_uid=int(target)))
            continue
        changes.append(dict(class_name='RACETEAM_c', uid=int(uid), field='MANUFACTURER', target_uid=int(target)))
    result = _team_install_changes(changes, 'Repair and reapply saved team and manufacturer links')
    result['skipped_public_custom_team_links'] = skipped_public_custom
    result['public_custom_teams_enabled'] = bool(PUBLIC_CUSTOM_TEAMS_ENABLED)
    return result


def _team_asset_snapshot(reg):
    return _shared_team_asset_transaction().snapshot(('0', '1'))


def _team_asset_restore(snap):
    return _shared_team_asset_transaction().restore(snap) if snap else []


def _team_asset_clear_persisted_snapshot():
    return _shared_team_asset_checkpoint().clear()


def _team_snapshot_created_epoch(meta):
    raw = str((meta or {}).get('created') or '').strip()
    if not raw:
        return 0.0
    try:
        return float(datetime.datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp())
    except Exception:
        return 0.0


def _team_snapshot_restore_block(meta):
    return _shared_team_asset_checkpoint().restore_block(meta)


def _team_asset_persist_snapshot(snap, label):
    return _shared_team_asset_checkpoint().persist(snap, label)


def _team_asset_rollback_info():
    return _shared_team_asset_checkpoint().info()


def _team_asset_load_persisted_snapshot(reg):
    return _shared_team_asset_checkpoint().load()


def _team_original_team_map():
    try:
        _g, reg = registry(); v = need(reg, '0')
        archive = v.get('bak') if os.path.exists(v.get('bak', '')) else None
        cdf = backup_path(v['cdf']) if os.path.exists(backup_path(v['cdf'])) else None
        if not archive or not cdf:
            return {}
        _raw, rows, _layout = _rp_index_rows(cdf)
        row = _rp_find_row(rows, DBFILE)
        with open(archive, 'rb') as fh:
            fh.seek(row['offset']); pyc = fh.read(row['size'])
        cat = _shared_team_editor().catalog_payload(pyc)
        return {int(d['config_uid']): int(d['team_uid']) for d in cat.get('drivers', [])}
    except Exception:
        return {}


def _team_original_manufacturer_map():
    """Stock RACETEAM UID -> manufacturer UID from the oldest valid backup."""
    try:
        _g, reg = registry(); v = need(reg, '0')
        archive = v.get('bak') if os.path.exists(v.get('bak', '')) else None
        cdf = backup_path(v['cdf']) if os.path.exists(backup_path(v['cdf'])) else None
        if not archive or not cdf:
            return {}
        _raw, rows, _layout = _rp_index_rows(cdf)
        row = _rp_find_row(rows, DBFILE)
        with open(archive, 'rb') as fh:
            fh.seek(row['offset']); pyc = fh.read(row['size'])
        cat = _shared_team_editor().catalog_payload(pyc)
        return {int(t['uid']): int(t['manufacturer_uid']) for t in cat.get('teams', [])}
    except Exception:
        return {}


def _slot_manufacturer_context(slot_name):
    """Return whether a stock paint slot's live team changed body family."""
    link = next((x for x in load_driver_links()
                 if str(x.get('slot') or '').casefold() == str(slot_name or '').casefold()), None)
    if not link or link.get('driver_uid') is None:
        return {'known': False, 'mismatch': False}
    driver_uid = int(link['driver_uid'])
    live_driver = _team_fast_driver_links().get(driver_uid)
    if not live_driver:
        return {'known': False, 'mismatch': False, 'driver_uid': driver_uid}
    config_uid = int(live_driver['config_uid'])
    current_team_uid = int(live_driver['team_uid'])
    original_team_uid = int(_team_original_team_map().get(config_uid, current_team_uid))
    originals = _team_original_manufacturer_map()
    catalog = _team_friendly_catalog()
    teams = {int(t['uid']): t for t in catalog.get('teams', [])}
    current = teams.get(current_team_uid, {})
    current_mfr = current.get('manufacturer_uid')
    original_mfr = originals.get(original_team_uid)
    return {
        'known': current_mfr is not None and original_mfr is not None,
        'mismatch': (current_mfr is not None and original_mfr is not None
                     and int(current_mfr) != int(original_mfr)),
        'driver_uid': driver_uid,
        'config_uid': config_uid,
        'current_team_uid': current_team_uid,
        'original_team_uid': original_team_uid,
        'current_manufacturer_uid': current_mfr,
        'original_manufacturer_uid': original_mfr,
        'current_manufacturer': current.get('manufacturer_label'),
    }


def _team_driver_livery_uids(game, driver_uid):
    cat = extra_scheme_mod().catalog(game, EXTRA_SCHEME_STATE)
    driver = next((d for d in cat.get('drivers', []) if int(d.get('uid', -1)) == int(driver_uid)), None)
    if not driver:
        return []
    return sorted({int(x['uid']) for x in driver.get('schemes', []) if x.get('uid') is not None})


def _team_driver_by_driver_uid(driver_uid):
    catalog = _team_friendly_catalog()
    return next((d for d in catalog.get('drivers', [])
                 if int(d.get('driver_uid', -1)) == int(driver_uid)), None)


def _team_driver_by_config_uid(config_uid):
    catalog = _team_friendly_catalog()
    return next((d for d in catalog.get('drivers', [])
                 if int(d.get('config_uid', -1)) == int(config_uid)), None)


def _team_driver_by_art_key(key):
    """Prefer the stable DRIVERCONFIG UID; retain old driver-UID URLs."""
    return _team_driver_by_config_uid(key) or _team_driver_by_driver_uid(key)


def _team_resolved_driver_art(game, driver):
    return team_assets_mod().resolve_driver_art_container(
        game, int(driver['team_uid']), int(driver['driver_uid']))


def _team_preview_container_for_driver(driver_uid):
    driver = _team_driver_by_driver_uid(driver_uid)
    if not driver:
        raise ValueError('the driver is not linked to a current 2015 Cup team')
    return f"2DRIVERSELECTTD_{int(driver['team_uid'])}.ARC", driver


def _team_original_source_uid_for_driver(driver):
    state = _team_state_load()
    originals = _team_original_team_map()
    config_uid = int(driver['config_uid'])
    return int(state.get('driver_source_teams', {}).get(
        str(config_uid), originals.get(config_uid, driver['team_uid'])))


def _team_fast_driver_links():
    """Read only the live DRIVERCONFIG team links, without presentation scans."""
    raw = _shared_team_editor().catalog()
    return {
        int(d['driver_uid']): {
            'driver_uid': int(d['driver_uid']),
            'config_uid': int(d['config_uid']),
            'team_uid': int(d['team_uid']),
        }
        for d in raw.get('drivers', [])
        if d.get('driver_uid') is not None and d.get('config_uid') is not None
        and d.get('team_uid') is not None
    }


def _stable_paint_creation_guard(driver_uid, driver=None, originals=None, state=None):
    """Classify whether the proven stock-team paint writer is safe for one driver.

    Optional cached inputs keep Paint Data reload fast.  The live team link is authoritative. Saved source-team metadata is
    trusted only while its saved destination still matches the live link, so a
    restored stock install cannot remain falsely locked by stale drop-in state.
    """
    driver = driver or _team_driver_by_driver_uid(int(driver_uid))
    if not driver:
        return {
            'locked': True, 'reason': 'the driver has no current 2015 Cup team link',
            'driver_uid': int(driver_uid), 'team_uid': None, 'moved': False,
            'spare_team': False,
        }
    config_uid = int(driver['config_uid'])
    team_uid = int(driver['team_uid'])
    originals = _team_original_team_map() if originals is None else originals
    state = _team_state_load() if state is None else state
    stock_original_uid = int(originals.get(config_uid, team_uid))
    source_raw = state.get('driver_source_teams', {}).get(str(config_uid))
    target_raw = state.get('driver_teams', {}).get(str(config_uid))
    saved_source_uid = int(source_raw) if source_raw is not None else stock_original_uid
    saved_target_uid = int(target_raw) if target_raw is not None else None

    # A source-team record alone is historical metadata, not proof that the
    # driver is currently moved.  Drop-in upgrades can retain that metadata after
    # the game archives were restored, which previously false-locked native
    # drivers.  Treat the saved move as active only when its target still matches
    # the live DRIVERCONFIG link.
    active_saved_move = bool(
        saved_target_uid is not None and saved_target_uid == team_uid
        and team_uid != saved_source_uid
    )
    moved = bool(team_uid != stock_original_uid or active_saved_move)
    original_uid = saved_source_uid if active_saved_move else stock_original_uid
    spare = bool(team_uid in SUPPORTED_SPARE_TEAM_UIDS)
    # rc9 experimental branch: transferred drivers on authored stock teams use
    # rc8's proven paired fixed-template writer. Spare/custom teams remain locked.
    locked = bool(spare)
    # The lock existed because appended resources corrupted the destination bank
    # (foreign directory header). With that fixed in nascar15_team_assets_v1.py,
    # this flag re-opens the path without deleting the guard.
    if locked and SPARE_TEAM_PAINT_CREATION_ENABLED:
        locked = False
    if locked:
        reason = ('Paint-slot creation is safety-locked for spare/custom teams in '
                  'the stable baseline. Team moves, logos, carousel art, 3D numbers, '
                  'and native paint previews remain available.')
    else:
        reason = ''
    return {
        'locked': locked, 'reason': reason, 'driver_uid': int(driver_uid),
        'config_uid': config_uid, 'team_uid': team_uid,
        'original_team_uid': original_uid, 'moved': moved, 'spare_team': spare,
        'experimental_moved_driver': bool(moved and not spare),
    }


def _team_active_created_uids():
    try:
        state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
        return {int(x.get('uid')) for x in state.get('schemes', [])
                if x.get('uid') is not None and not x.get('superseded_by')}
    except Exception:
        return set()


def _team_driver_native_livery_uids(game, driver_uid):
    """Return only native/non-app-created previews for stable team rebuilds."""
    created = _team_active_created_uids()
    return [uid for uid in _team_driver_livery_uids(game, driver_uid)
            if int(uid) not in created]


def _team_rebuild_created_thumbnails(game, driver_uid, target_team_uid):
    """Rebuild every app-created thumbnail through the proven append/repoint route.

    Team-bank resource transfer preserves exact native resources, but old custom-team
    builds may already contain an invalid copied alias.  Recreating each managed
    PAINTSCHEME from a valid same-team donor removes that legacy state without ever
    overwriting the currently indexed bank in place.
    """
    mod = extra_scheme_mod()
    state = mod.load_state(EXTRA_SCHEME_STATE)
    items = [x for x in state.get('schemes', [])
             if int(x.get('driver_uid', -1)) == int(driver_uid)
             and not x.get('superseded_by')]
    if not items:
        return []
    catalog = mod.catalog(game, EXTRA_SCHEME_STATE)
    driver = next((d for d in catalog.get('drivers', [])
                   if int(d.get('uid', -1)) == int(driver_uid)), None)
    if driver is None:
        raise ValueError(f'driver UID {int(driver_uid)} is missing from the livery catalog')
    target_container = f"2DRIVERSELECTTD_{int(target_team_uid)}.ARC"
    tm = extra_thumbnail_mod()
    reports = []
    for item in sorted(items, key=lambda x: int(x.get('uid', 0))):
        uid = int(item['uid'])
        donor = _extra_thumbnail_donor(
            game, driver, exclude_uid=uid, target_container=target_container)
        image_path = None
        saved = item.get('thumbnail_source_png')
        if saved:
            candidate = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(saved)))
            if os.path.exists(candidate):
                image_path = candidate
        report = tm.install_or_replace_thumbnail(
            game, uid, int(donor['uid']), image_path,
            target_container_name=target_container)
        reports.append({
            'uid': uid, 'name': item.get('name'),
            'used_saved_custom_image': bool(image_path),
            'donor_uid': int(donor['uid']),
            'container': target_container,
            'method': report.get('method'),
            'archive_offset': report.get('archive_offset'),
            'readback_verified': bool(report.get('readback_verified')),
        })
    return reports


def _team_replay_saved_stock_thumbnails(game, driver, target_team_uid, livery_uids):
    """Replay proven stock-thumbnail replacements into the destination team bank.

    The native transfer writer copies the driver's presentation resources first.
    If a user replaced a stock Paint Select thumbnail, replay the exact saved PNG
    into the newly built destination bank before the DRIVERCONFIG team link moves.
    This avoids depending on which duplicate bank the old unscoped native lookup
    originally selected.
    """
    state=_team_state_load()
    overrides=state.get('thumbnail_overrides') or {}
    target_container=f"2DRIVERSELECTTD_{int(target_team_uid)}.ARC"
    tm=extra_thumbnail_mod()
    reports=[]
    for raw_uid in livery_uids:
        uid=int(raw_uid)
        meta=overrides.get(str(uid)) or {}
        slot=str(meta.get('slot') or '')
        saved=str(meta.get('saved_thumb') or '')
        candidates=[_stock_thumbnail_preview_path(uid)]
        if saved:
            candidates.append(os.path.join(SCHEMES,*saved.replace('\\','/').split('/')))
            candidates.append(os.path.join(SCHEMES,os.path.basename(saved)))
        if slot:candidates.append(os.path.join(SCHEMES,slot+'.thumb.png'))
        path=next((x for x in candidates if os.path.isfile(x)),None)
        if not path:
            continue
        hit=tm.find_target(game,uid,target_container_name=target_container)
        if not hit:
            raise ValueError(f'PAINTSCHEME_{uid} was not copied into {target_container}')
        identity=tm.inspect_thumbnail_identity(game,uid,target_container_name=target_container) or {}
        if not (identity.get('exists') and identity.get('structural_valid') and identity.get('same_bank_valid')):
            raise ValueError(f'PAINTSCHEME_{uid} in {target_container} is not safe for thumbnail replay')
        report=tm.replace_existing_thumbnail(game,uid,path,target_container_name=target_container)
        if 'texconv' not in str(report.get('encoder') or '').lower():
            raise ValueError(f'texconv DXT5 was not used while replaying PAINTSCHEME_{uid}')
        _extra_read_live_native_thumbnail_preview(game,uid,target_container)
        reports.append({'uid':uid,'container':target_container,'source':os.path.basename(path),
                        'method':report.get('method'),'readback_verified':bool(report.get('readback_verified'))})
    return reports

def _team_prepare_transfer_assets(game, driver, old_team_uid, new_team_uid):
    """Prepare a transfer through the exact public-v1 writer.

    Do not pre-classify a real team bank through the experimental footer model
    and do not fall back to a synthetic complete-bank rebuild. The working v1.0
    path edits the indexed destination revision directly, then appends and
    repoints it transactionally. The helper still resolves short physical aliases
    for driver art before writing the long logical identity.
    """
    assets = team_assets_mod()
    source_uid = int(old_team_uid)
    try:
        source_status = assets.team_asset_status(game, source_uid)
    except Exception:
        source_status = {}
    if not source_status.get('paint_container_ready'):
        state = _team_state_load()
        source_uid = int(state.get('driver_source_teams', {}).get(
            str(driver['config_uid']),
            _team_original_team_map().get(int(driver['config_uid']), old_team_uid)))
    livery_uids = _team_driver_native_livery_uids(
        game, int(driver['driver_uid']))
    paint = assets.ensure_driver_assets(
        game, int(new_team_uid), source_uid, int(driver['driver_uid']),
        livery_uids)
    paint['transfer_strategy'] = 'public_v1_direct_revision'

    stock_thumbnail_replays = _team_replay_saved_stock_thumbnails(
        game, driver, int(new_team_uid), livery_uids)

    thumbnails = []
    if SPARE_TEAM_PAINT_CREATION_ENABLED:
        thumbnails = _team_rebuild_created_thumbnails(
            game, int(driver['driver_uid']), int(new_team_uid))
        thumbnail_guard = {
            'skipped': False,
            'reason': 'spare-team paint creation enabled',
        }
    else:
        thumbnail_guard = {
            'skipped': True,
            'reason': ('stable baseline keeps app-created thumbnails out of '
                       'moved/custom team banks'),
        }
    logo = None
    status = assets.team_asset_status(game, int(new_team_uid))
    if not status.get('logo_ready'):
        logo = assets.ensure_team_logo(game, int(new_team_uid), source_uid)
    return {
        'paint': paint, 'thumbnails': thumbnails,
        'stock_thumbnail_replays': stock_thumbnail_replays,
        'thumbnail_guard': thumbnail_guard, 'logo': logo,
        'livery_uids': livery_uids, 'source_team_uid': source_uid,
    }


def _team_history_label(kind, catalog, uid, old_uid, new_uid):
    if kind == 'driver_team':
        d = next((x for x in catalog.get('drivers', []) if int(x['config_uid']) == int(uid)), None)
        old = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(old_uid)), None)
        new = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(new_uid)), None)
        return f"{(d or {}).get('car_label', 'Driver')} · {(old or {}).get('label', old_uid)} → {(new or {}).get('label', new_uid)}"
    team = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(uid)), None)
    manu = {int(x['uid']): x.get('label') for x in catalog.get('manufacturers', [])}
    return f"{(team or {}).get('label', 'Team')} · {manu.get(int(old_uid), old_uid)} → {manu.get(int(new_uid), new_uid)}"


@app.route('/api/teams/catalog')
def teams_catalog_api():
    try:
        return jsonify(dict(ok=True, **_shared_team_presentation_editor().catalog()))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/move_driver', methods=['POST'])
def teams_move_driver_api():
    try:
        q = request.get_json(force=True) or {}
        result = _shared_team_presentation_editor().move_driver(
            int(q.get('config_uid')), int(q.get('team_uid')),
            dry_run=bool(q.get('dry_run')),
        )
        if not result.get('dry_run'):
            _clear_ui_thumb_cache()
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/set_manufacturer', methods=['POST'])
def teams_set_manufacturer_api():
    try:
        q = request.get_json(force=True) or {}
        return jsonify(_shared_team_presentation_editor().set_manufacturer(
            int(q.get('team_uid')), int(q.get('manufacturer_uid')),
            dry_run=bool(q.get('dry_run')),
        ))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/prepare', methods=['POST'])
def teams_prepare_assets_api():
    try:
        q = request.get_json(force=True) or {}
        return jsonify(_shared_team_presentation_editor().prepare_team(
            int(q.get('team_uid')),
        ))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/rename', methods=['POST'])
def teams_rename_api():
    try:
        q = request.get_json(force=True) or {}
        return jsonify(_shared_team_presentation_editor().rename_team(
            int(q.get('team_uid')), str(q.get('name') or ''),
        ))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


def _prepare_team_logo_auto(image, target_size):
    """Prepare a logo for the wide Team Select tile without visible stretching.

    Most TEAM textures are stored on a square native canvas but rendered by the
    menu on an approximately 2:1 card.  Fitting directly to the native canvas
    therefore makes normal wordmarks look twice as wide in-game.  Build the
    artwork in a virtual 2:1 display canvas first, then compress that complete
    canvas back to the native texture.  The game stretches it back to the
    intended proportions.
    """
    tw, th = map(int, target_size)
    src = image.convert('RGBA')
    alpha = src.getchannel('A')
    bbox = alpha.getbbox()
    cropped = False
    if bbox and bbox != (0, 0, src.width, src.height):
        src = src.crop(bbox)
        cropped = True

    display_aspect = 2.0
    display_w = max(1, int(round(th * display_aspect)))
    display_h = th
    # Generous safe area: the actual red card masks a little more than the
    # raw texture preview suggests, especially at the right edge.
    safe_w = max(1, int(round(display_w * 0.72)))
    safe_h = max(1, int(round(display_h * 0.64)))
    fitted, info = prepare_import_image(src, (safe_w, safe_h), 'fit',
                                        preserve_alpha=True,
                                        background=(0, 0, 0, 0))
    virtual = Image.new('RGBA', (display_w, display_h), (0, 0, 0, 0))
    virtual.alpha_composite(fitted, ((display_w - safe_w) // 2,
                                     (display_h - safe_h) // 2))
    lanczos = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
    canvas = virtual.resize((tw, th), lanczos)
    info.update({
        'native_target': [tw, th],
        'virtual_display_canvas': [display_w, display_h],
        'safe_display_area': [safe_w, safe_h],
        'display_aspect_compensation': display_aspect,
        'transparent_border_trimmed': bool(cropped),
        'mode': 'team-select-display-fit',
    })
    return canvas, info


@app.route('/api/teams/logo/<int:team_uid>')
def teams_logo_png(team_uid):
    try:
        image = _shared_team_presentation_editor().read_logo(team_uid)
        if request.args.get('display'):
            image = _ui_fit_preview_crop(
                image, (320, 180), (286, 156), alpha_first=True, threshold=8,
            )
        out = io.BytesIO()
        image.save(out, format='PNG')
        out.seek(0)
        return send_file(
            out, mimetype='image/png',
            as_attachment=bool(request.args.get('download')),
            download_name=f'TEAM_{int(team_uid)}.png', max_age=0,
        )
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 404

@app.route('/api/teams/logo', methods=['POST'])
def teams_logo_install_api():
    try:
        upload = request.files.get('file')
        if not upload:
            raise ValueError('choose a logo image')
        with tempfile.TemporaryDirectory(prefix='n15_team_logo_upload_') as folder:
            source = os.path.join(folder, os.path.basename(upload.filename or 'logo.png'))
            upload.save(source)
            result = _shared_team_presentation_editor().replace_logo(
                int(request.form.get('team_uid')), source,
            )
        _clear_ui_thumb_cache()
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/driver_art/<int:driver_key>/<kind>')
def teams_driver_art_png(driver_key, kind):
    try:
        image = _shared_team_presentation_editor().read_driver_art(driver_key, kind)
        if request.args.get('display'):
            alpha_first = str(kind).lower() not in ('number', '3dnum', 'card')
            image = _ui_fit_preview_crop(
                image, (360, 180), (326, 154),
                alpha_first=alpha_first, threshold=8,
            )
        out = io.BytesIO()
        image.save(out, format='PNG')
        out.seek(0)
        response = send_file(
            out, mimetype='image/png',
            as_attachment=bool(request.args.get('download')),
            download_name=f'DRIVER_{int(driver_key)}_{kind}.png', max_age=0,
        )
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 404

@app.route('/api/teams/driver_art', methods=['POST'])
def teams_driver_art_install_api():
    try:
        raw_key = request.form.get('config_uid') or request.form.get('driver_uid')
        if raw_key is None:
            raise ValueError('driver key is missing')
        upload = request.files.get('file')
        if not upload:
            raise ValueError('choose an image')
        with tempfile.TemporaryDirectory(prefix='n15_driver_art_upload_') as folder:
            source = os.path.join(folder, os.path.basename(upload.filename or 'art.png'))
            upload.save(source)
            result = _shared_team_presentation_editor().replace_driver_art(
                int(raw_key), str(request.form.get('kind') or ''), source,
                resize_mode=str(request.form.get('resize_mode') or 'fit'),
            )
        _clear_ui_thumb_cache()
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/driver_art/repair', methods=['POST'])
def teams_driver_art_repair_api():
    try:
        q = request.get_json(force=True) or {}
        raw_key = q.get('config_uid', q.get('driver_uid'))
        if raw_key is None:
            raise ValueError('driver key is missing')
        result = _shared_team_presentation_editor().repair_driver_art(int(raw_key))
        _clear_ui_thumb_cache()
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/teams/restore_assets', methods=['POST'])
def teams_restore_assets_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before restoring team assets')
        with _TEAM_MANAGER_LOCK:
            result = _shared_team_presentation_recovery().restore()
            try: _clear_ui_thumb_cache()
            except Exception: pass
            return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/repair_legacy', methods=['POST'])
def teams_repair_legacy_api():
    try:
        return jsonify(_shared_team_presentation_editor().repair_saved_links())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/undo', methods=['POST'])
def teams_undo_api():
    try:
        result = _shared_team_presentation_editor().undo()
        _clear_ui_thumb_cache()
        return jsonify(result)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


# ==================== end v0.9.30.5 ====================


def _port_busy(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        try:
            return s.connect_ex(('127.0.0.1', port)) == 0
        except OSError:
            return True


def _is_our_app(port):
    """True only when the listener exposes this app's status signature."""
    import urllib.request, json as _j
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=1.2) as r:
            data = _j.loads(r.read().decode('utf-8', 'replace'))
        if not isinstance(data, dict):
            return False
        # Reuse only the exact same build.  Treating any older NASCAR Modding App
        # listener as this build caused a newly launched RC to open the old server
        # and old cached UI instead of starting its own process.
        return (data.get('app_name') == APP_NAME
                and data.get('version') == APP_VERSION
                and data.get('release_label') == APP_RELEASE_LABEL)
    except Exception:
        return False


def _choose_port(first=8151, tries=12):
    """Return (port, already_running).

    Scan the complete app range before choosing a free port. This also finds an
    existing copy that had to start on 8152-8162 because 8151 was occupied.
    """
    first_free = None
    for p in range(first, first + tries):
        if not _port_busy(p):
            if first_free is None:
                first_free = p
            continue
        if _is_our_app(p):
            return p, True
    return first_free, False


if __name__=='__main__':
    port, already = _choose_port()
    if already:
        print(f'The app is already running. Opening http://127.0.0.1:{port} ...')
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/?build={APP_VERSION.replace('.', '_').replace('-', '_')}")
        except Exception:
            pass
        sys.exit(0)
    if port is None:
        print('Could not find a free port between 8151 and 8162.')
        print('Close other copies of this app (or whatever is using those ports) and try again.')
        sys.exit(1)
    if port != 8151:
        print(f'Port 8151 was busy, using {port} instead.')
    print(f'NASCAR Modding App v{APP_VERSION} - http://127.0.0.1:{port}')
    print('Leave this window open while you use the app. Close it to stop the app.')
    try:
        if _app_settings_payload().get('auto_open_browser',True):
            webbrowser.open(f"http://127.0.0.1:{port}/?build={APP_VERSION.replace('.', '_').replace('-', '_')}")
    except Exception: pass
    try:
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    except OSError as ex:
        print()
        print(f'Could not start the web server on port {port}: {ex}')
        print('This is usually another copy of the app, or security software blocking')
        print('local connections. Close other copies and try again.')
        sys.exit(1)
