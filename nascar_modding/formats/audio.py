"""Eutechnyx FSB5/SND parsing and verified audio transformation primitives."""

from __future__ import annotations

import base64 as _b64
import io
import math as _math
import os
from pathlib import Path
import shutil
import sys
import struct
import subprocess
import tempfile

import numpy as np


def ffmpeg_path():
    root = Path(__file__).resolve().parents[2]
    candidates = [
        root / 'audio_tools' / 'bin' / 'ffmpeg.exe',
        root / 'ffmpeg.exe',
    ]
    if getattr(sys, 'frozen', False):
        executable_root = Path(sys.executable).resolve().parent
        candidates[:0] = [
            executable_root / 'audio_tools' / 'bin' / 'ffmpeg.exe',
            executable_root / 'ffmpeg.exe',
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')


_SIL={ (True): _b64.b64decode("//2UxIZnZ3ZmbbbRkAAAqqqqqqvvvvvvvvvvvvvvvvvvvn+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbQ"), (False): _b64.b64decode("//2UBFUzM0MiRDMRIiIiSSSbSAAAAAAAAACqqqqqqqqqqvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvn333333d3d3d3d1sbb58ti2Nttta+fPnz58+fPnz58222+fPvvvvvu7u7u7u7rY23z5bFsbbba18+fPnz58+fPnz5ttt8+ffffffd3d3d3d3Wxtvny2LY2221r58+fPnz58+fPnzbbb58++++++7u7u7u7utjbfPlsWxtttrXz58+fPnz58+fPm223z59999993d3d3d3dbG2+fLYtjbbbWvnz58+fPnz58+fNttvnz777777u7u7u7u62Nt8+WxbG222tfPnz58+fPnz58+bbbfPn333333d3d3d3d1sbb58ti2Nttta+fPnz58+fPnz58222+fPvvvvvu7u7u7u7rY23z5bFsbbba18+fPnz58+fPnz5ttt8+ffffffd3d3d3d3Wxtvny2LY2221r58+fPnz58+fPnzbbb58++++++7u7u7u7utjbfPlsWxtttrXz58+fPnz58+fPm223z59999993d3d3d3dbG2+fLYtjbbbWvnz58+fPnz58+fNttvnz777777u7u7u7u62Nt8+WxbG222tfPnz58+fPnz58+bbbfPg") }

_BR={(1,1):{1:32,2:64,3:96,4:128,5:160,6:192,7:224,8:256,9:288,10:320,11:352,12:384,13:416,14:448},
     (1,2):{1:32,2:48,3:56,4:64,5:80,6:96,7:112,8:128,9:160,10:192,11:224,12:256,13:320,14:384},
     (1,3):{1:32,2:40,3:48,4:56,5:64,6:80,7:96,8:112,9:128,10:160,11:192,12:224,13:256,14:320},
     (2,1):{1:32,2:48,3:56,4:64,5:80,6:96,7:112,8:128,9:144,10:160,11:176,12:192,13:224,14:256},
     (2,2):{1:8,2:16,3:24,4:32,5:40,6:48,7:56,8:64,9:80,10:96,11:112,12:128,13:144,14:160}}
_BR[(2,3)]=_BR[(2,2)]
_SRT={3:{0:44100,1:48000,2:32000},2:{0:22050,1:24000,2:16000},0:{0:11025,1:12000,2:8000}}

def frame_info(d,i=0):
    if i+4>len(d) or d[i]!=0xFF or (d[i+1]&0xE0)!=0xE0: return None
    vf=(d[i+1]>>3)&3
    if vf==1: return None
    lf=(d[i+1]>>1)&3
    if lf==0: return None
    layer=4-lf; ver=1 if vf==3 else 2
    br=(d[i+2]>>4)&0xF; sr=(d[i+2]>>2)&3; pad=(d[i+2]>>1)&1
    if br in (0,15) or sr==3: return None
    kbps=_BR[(ver,layer)][br]; hz=_SRT[vf][sr]
    if layer==1: flen=(12*kbps*1000//hz+pad)*4
    elif layer==3 and ver==2: flen=72*kbps*1000//hz+pad
    else: flen=144*kbps*1000//hz+pad
    return (layer,kbps,hz,((d[i+3]>>6)&3)==3,flen)

def walk_frames(d,limit=100000):
    out=[];i=0
    while i<len(d)-4 and len(out)<limit:
        fi=frame_info(d,i)
        if not fi: break
        out.append((i,fi)); i+=fi[4]
    return out

def _mpeg_frame_topology(d, limit=200000, max_gap=96):
    """Map the exact MPEG frame starts used by an FSB5 sample.

    NASCAR 15 music banks are not safe to rebuild as a generic contiguous or
    16-byte-padded stream.  FMOD banks may align each frame differently (32-byte
    alignment is common), and the sample header/loop chunks continue to describe
    the original decoded sample window.  This mapper preserves the stock frame
    start offsets instead of guessing a padding rule.
    """
    frames=[]
    if not d: return dict(frames=[], starts=[], cells=[], padded=False, spec=None)
    i=0
    # A valid FSB sample should start on a frame. Tolerate a tiny leading pad for
    # raw user streams, but never silently skip a large unknown prefix.
    if not frame_info(d,0):
        first=next((j for j in range(1,min(max_gap+1,max(1,len(d)-3))) if frame_info(d,j)),None)
        if first is None: return dict(frames=[], starts=[], cells=[], padded=False, spec=None)
        i=first
    spec=None
    while i<len(d)-4 and len(frames)<limit:
        fi=frame_info(d,i)
        if not fi: break
        if spec is None: spec=fi
        # A slot is one codec configuration. A different layer/rate/channel mode
        # is treated as the end rather than accepted as a false sync in padding.
        if (fi[0],fi[1],fi[2],fi[3]) != (spec[0],spec[1],spec[2],spec[3]): break
        end=i+fi[4]
        if end>len(d): break
        frames.append(dict(start=i, length=fi[4], data=d[i:end], info=fi))
        nxt=None
        for j in range(end,min(len(d)-3,end+max_gap+1)):
            nfi=frame_info(d,j)
            if nfi and (nfi[0],nfi[1],nfi[2],nfi[3]) == (spec[0],spec[1],spec[2],spec[3]):
                nxt=j; break
        if nxt is None: break
        i=nxt
    starts=[x['start'] for x in frames]
    cells=[]
    for n,x in enumerate(frames):
        # Every non-final cell ends at the next stock frame start.  The final
        # cell is limited to the original frame itself; bytes after it are kept
        # byte-identical, which avoids turning unknown tail data into audio.
        end=starts[n+1] if n+1<len(starts) else x['start']+x['length']
        cells.append(dict(start=x['start'], end=end, capacity=end-x['start'],
                          frame_length=x['length']))
    padded=any(c['capacity']!=frames[i]['length'] for i,c in enumerate(cells))
    return dict(frames=frames, starts=starts, cells=cells, padded=padded, spec=spec)

def fmod_walk(d,limit=200000):
    """Return de-padded frames using the measured FSB frame topology."""
    t=_mpeg_frame_topology(d,limit=limit)
    return [x['data'] for x in t['frames']],t['padded'],t['spec']

def _mpeg_silent_frame_that_fits(spec, capacity):
    sil=_sil_for(spec)
    if sil:
        t=_mpeg_frame_topology(sil,limit=4)
        for x in t['frames']:
            if len(x['data'])<=capacity:
                return x['data']
        fi=frame_info(sil,0)
        if fi and fi[4]<=capacity:
            return sil[:fi[4]]
    return None

def _fit_mpeg_to_stock_topology(stream, original, meta):
    """Fit replacement audio into the stock sample's exact MPEG frame cells.

    The old writer filled the byte slot using a guessed 16-byte padding rule and
    as many silent frames as would fit.  Music then retained stock sample-count
    and loop metadata but no longer retained stock frame boundaries.  This writer
    keeps the original number of frames, every original frame start offset, all
    bytes outside those cells, and therefore all header/loop metadata.
    """
    stock=_mpeg_frame_topology(original)
    source=_mpeg_frame_topology(stream)
    if not stock['frames'] or not stock['spec']:
        raise ValueError('stock MPEG frame topology could not be mapped')
    if not source['frames'] or not source['spec']:
        raise ValueError('replacement contains no valid MPEG frames')
    ss=stock['spec']; rs=source['spec']
    if (ss[0],ss[1],ss[2],ss[3]) != (rs[0],rs[1],rs[2],rs[3]):
        raise ValueError('replacement MPEG format does not exactly match the stock song')
    out=bytearray(original)
    src=[x['data'] for x in source['frames']]
    used=0; silence=0; preserved_tail=len(original)-(stock['cells'][-1]['end'] if stock['cells'] else 0)
    for i,cell in enumerate(stock['cells']):
        cap=cell['capacity']
        frame=src[i] if i<len(src) else None
        if frame is not None and len(frame)>cap:
            # A different encoder padding phase can make an occasional CBR frame
            # one byte larger.  Do not split it; use a valid matching silent frame
            # for that final fraction of a second instead.
            frame=None
        if frame is None:
            frame=_mpeg_silent_frame_that_fits(ss,cap)
            if frame is None:
                raise ValueError(f'no valid matching MPEG frame fits stock cell {i} ({cap} bytes)')
            silence+=1
        else:
            used+=1
        out[cell['start']:cell['end']]=frame+b'\0'*(cap-len(frame))
    # Re-scan the result and demand the exact original start map.  This is the
    # important game-safety check; ordinary FSB parsing does not verify it.
    check=_mpeg_frame_topology(bytes(out))
    if check['starts']!=stock['starts'] or len(check['frames'])!=len(stock['frames']):
        raise ValueError('rebuilt MPEG frame topology differs from stock; write refused')
    if check['spec'] and (check['spec'][0],check['spec'][1],check['spec'][2],check['spec'][3]) != (ss[0],ss[1],ss[2],ss[3]):
        raise ValueError('rebuilt MPEG codec specification changed')
    samples=int((meta or {}).get('samples') or 0)
    hz=int((meta or {}).get('hz') or ss[2] or 0)
    return bytes(out),dict(stock_frames=len(stock['frames']),source_frames=len(src),
        audio_frames=used,silent_frames=silence,padded=stock['padded'],
        preserved_tail=preserved_tail,samples=samples,hz=hz,starts=stock['starts'])

def _verify_mpeg_decode(payload):
    """Ask FFmpeg to decode the rebuilt elementary stream before game write."""
    ff=ffmpeg_path()
    if not ff: return True,None
    frames,_,fi=fmod_walk(payload)
    if not frames or not fi: return False,'no frames after rebuild'
    ext='.mp2' if fi[0]==2 else '.mp3'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'verify'+ext)
        open(src,'wb').write(b''.join(frames))
        r=subprocess.run([ff,'-v','error','-i',src,'-f','null','-'],capture_output=True,text=True)
        if r.returncode!=0:
            return False,(r.stderr or 'FFmpeg decode failed')[-240:]
    return True,None


_AUDIO_VOLUME_MODES={'match_stock','custom','source'}
_AUDIO_CUSTOM_GAIN_MIN_DB=-24.0
_AUDIO_CUSTOM_GAIN_MAX_DB=24.0

def _audio_volume_mode(value):
    mode=str(value or 'match_stock').strip().lower()
    return mode if mode in _AUDIO_VOLUME_MODES else 'match_stock'

def _audio_custom_gain_db(value, mode='custom'):
    if _audio_volume_mode(mode)!='custom':
        return 0.0
    try:
        gain=float(value)
    except (TypeError,ValueError):
        raise ValueError('Custom volume must be a number between -24 and +24 dB')
    if not _math.isfinite(gain):
        raise ValueError('Custom volume must be a finite number')
    if gain<_AUDIO_CUSTOM_GAIN_MIN_DB or gain>_AUDIO_CUSTOM_GAIN_MAX_DB:
        raise ValueError('Custom volume must be between -24 and +24 dB')
    return gain

def _audio_volume_label(mode,gain_db):
    mode=_audio_volume_mode(mode)
    if mode=='match_stock': return f'matched to stock ({float(gain_db):+.1f} dB applied)'
    if mode=='source': return 'kept at source level (0.0 dB applied)'
    return f'custom gain {float(gain_db):+.1f} dB'

def _audio_is_loop_sample(name):
    n=str(name or '').lower()
    return any(token in n for token in ('eng_l','eng_r','exh_l','exh_r','engine','exhaust'))

def _active_pcm_stats(values):
    arr=np.asarray(values,dtype=np.float64).reshape(-1)
    if not arr.size: return dict(rms=0.0,peak=0.0,rms_db=-120.0,peak_db=-120.0)
    peak=float(np.max(np.abs(arr)))
    if peak<=1e-9: return dict(rms=0.0,peak=0.0,rms_db=-120.0,peak_db=-120.0)
    floor=max(1e-5,peak*0.001)
    active=arr[np.abs(arr)>=floor]
    if active.size<32: active=arr
    rms=float(np.sqrt(np.mean(active*active))) if active.size else 0.0
    def db(v): return 20.0*np.log10(max(v,1e-9))
    return dict(rms=rms,peak=peak,rms_db=float(db(rms)),peak_db=float(db(peak)))

def _safe_gain_db(mode, source_stats=None, stock_stats=None, custom_gain_db=0.0):
    mode=_audio_volume_mode(mode)
    if mode=='source': return 0.0
    if mode=='custom': return _audio_custom_gain_db(custom_gain_db,mode)
    if not source_stats or not stock_stats or source_stats.get('rms',0)<=0 or stock_stats.get('rms',0)<=0:
        return 0.0
    wanted=float(stock_stats['rms_db'])-float(source_stats['rms_db'])
    # Leave headroom before the limiter. Match-stock is intentionally bounded so
    # a nearly silent upload cannot turn into a destructive +40 dB surprise.
    peak_room=-0.5-float(source_stats.get('peak_db',-120.0))
    return max(-12.0,min(18.0,wanted,peak_room+3.0))

def _apply_pcm_gain_i16(stream,gain_db):
    if not stream: return stream
    usable=len(stream)-(len(stream)%2)
    src=np.frombuffer(stream[:usable],dtype='<i2').astype(np.float64)/32768.0
    scaled=src*(10.0**(float(gain_db)/20.0))
    # Leave already-safe audio untouched. Engage the smooth limiter only when
    # the selected gain would exceed the headroom.
    limited=(scaled if (not scaled.size or float(np.max(np.abs(scaled)))<=0.97)
             else np.tanh(scaled/0.97)*0.97)
    out=np.clip(np.rint(limited*32767.0),-32768,32767).astype('<i2').tobytes()
    return out+stream[usable:]

def _loop_fill_pcm16(stream,target_bytes,channels,hz,crossfade_ms=12):
    frame_bytes=max(1,int(channels)*2)
    target_bytes=int(target_bytes)-(int(target_bytes)%frame_bytes)
    usable=len(stream)-(len(stream)%frame_bytes)
    if usable<=0 or target_bytes<=0: return b''
    if usable>=target_bytes: return stream[:target_bytes]
    src=np.frombuffer(stream[:usable],dtype='<i2').reshape(-1,int(channels)).astype(np.float64)
    target_frames=target_bytes//frame_bytes
    repeats=(target_frames+len(src)-1)//len(src)
    out=np.tile(src,(repeats,1))[:target_frames].copy()
    fade=max(1,min(len(src)//4,int(int(hz)*crossfade_ms/1000)))
    # Blend the first samples of each repeat with the previous repeat's tail.
    # The blend is in-place, so the output remains exactly the stock duration.
    if fade>0:
        alpha=np.linspace(0.0,1.0,fade,endpoint=False)[:,None]
        for boundary in range(len(src),target_frames,len(src)):
            n=min(fade,target_frames-boundary)
            if n<=0: break
            prev=src[-n:]
            nxt=src[:n]
            out[boundary:boundary+n]=prev*(1.0-alpha[:n])+nxt*alpha[:n]
    return np.clip(np.rint(out),-32768,32767).astype('<i2').tobytes()

def _ffmpeg_pcm_stats(raw,filename,hz,channels):
    ff=ffmpeg_path()
    if not ff: return None
    ext=os.path.splitext(filename or '')[1] or '.bin'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'measure'+ext); out=os.path.join(td,'measure.f32')
        open(src,'wb').write(raw)
        r=subprocess.run([ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
                          '-ar',str(int(hz)),'-ac',str(int(channels)),
                          '-c:a','pcm_f32le','-f','f32le',out,'-y'],
                         capture_output=True,text=True)
        if r.returncode!=0 or not os.path.exists(out): return None
        data=np.frombuffer(open(out,'rb').read(),dtype='<f4')
    return _active_pcm_stats(data)

def _mpeg_gain_db(mode,upload_raw,upload_name,stock_payload,spec,custom_gain_db=0.0):
    mode=_audio_volume_mode(mode)
    if mode!='match_stock': return _safe_gain_db(mode,custom_gain_db=custom_gain_db)
    frames,_,_fi=fmod_walk(stock_payload)
    if not frames: return 0.0
    channels=1 if spec[3] else 2
    stock_ext='.mp2' if spec[0]==2 else '.mp3'
    source_stats=_ffmpeg_pcm_stats(upload_raw,upload_name,spec[2],channels)
    stock_stats=_ffmpeg_pcm_stats(b''.join(frames),'stock'+stock_ext,spec[2],channels)
    return _safe_gain_db(mode,source_stats,stock_stats)

FSB_MODES={0:'NONE',1:'PCM8',2:'PCM16',3:'PCM24',4:'PCM32',5:'PCMFLOAT',6:'GCADPCM',
 7:'IMAADPCM',8:'VAG',9:'HEVAG',10:'XMA',11:'MPEG',12:'CELT',13:'AT9',14:'XWMA',15:'VORBIS'}
FSB_FREQ={1:8000,2:11000,3:11025,4:16000,5:22050,6:24000,7:32000,8:44100,9:48000,10:96000}

def parse_fsb5(bank):
    if bank[:4]!=b'FSB5': raise ValueError('not FSB5')
    ver,num,shdr,ntab,dsz,mode=struct.unpack_from('<6I',bank,4)
    codec=FSB_MODES.get(mode,f'mode{mode}')
    data_start=len(bank)-dsz; names_start=data_start-ntab; hdrs_start=names_start-shdr
    raws=[]; chunks=[]; raw_positions=[]; chunk_records=[]; pos=hdrs_start
    for _ in range(num):
        raw_pos=pos
        raw,=struct.unpack_from('<Q',bank,pos); pos+=8
        raws.append(raw); raw_positions.append(raw_pos); cl=[]; cr=[]; nxt=raw&1
        while nxt:
            chunk_header_pos=pos
            ch,=struct.unpack_from('<I',bank,pos); pos+=4
            nxt=ch&1; size=(ch>>1)&0xFFFFFF; ctype=ch>>25
            payload_pos=pos; payload=bank[pos:pos+size]
            cl.append((ctype,payload))
            cr.append(dict(type=ctype,header_pos=chunk_header_pos,payload_pos=payload_pos,size=size,data=payload))
            pos+=size
        chunks.append(cl); chunk_records.append(cr)
    walk_delta=pos-names_start
    names=['']*num
    if ntab>=4*num:
        noffs=[struct.unpack_from('<I',bank,names_start+4*i)[0] for i in range(num)]
        for i,o in enumerate(noffs):
            p=names_start+o
            if 0<=p<data_start:
                e=bank.find(b'\0',p,data_start)
                names[i]=bank[p:e].decode('ascii','replace') if e!=-1 else ''
    # per-sample meta: frequency index bits 1-4, channel bit 5 (standard v1 layout);
    # chunk type 1 = channel count, type 2 = explicit frequency (override)
    metas=[]
    for i,r in enumerate(raws):
        hz=FSB_FREQ.get((r>>1)&0xF); chn=((r>>5)&1)+1; scount=(r>>34)&0x3FFFFFFF
        for ct,pay in chunks[i]:
            if ct==1 and len(pay)>=1: chn=pay[0]
            elif ct==2 and len(pay)>=4: hz=struct.unpack_from('<I',pay)[0]
        metas.append(dict(hz=hz, ch=chn, samples=scount))
    # candidate offset layouts, scored by a codec-appropriate validator
    def score(offs):
        s=0
        for i,o in enumerate(offs):
            end=offs[i+1] if i+1<len(offs) else dsz
            if mode==11:
                s+=1 if (o+1<dsz and bank[data_start+o]==0xFF and (bank[data_start+o+1]&0xE0)==0xE0) else 0
            elif mode==2:
                exp=metas[i]['samples']*2*metas[i]['ch']
                s+=1 if 0<=(end-o)-exp<128 else 0
            else:
                s+=1 if end>o else 0
        return s
    cands=[]
    for bits,mult,sh in ((28,16,6),(27,32,7),(28,16,7)):
        offs=[((r>>sh)&((1<<bits)-1))*mult for r in raws]
        if not offs or offs!=sorted(offs) or offs[-1]>=dsz or offs[0]>=64: continue
        cands.append((score(offs),offs,f'sh{sh}x{mult}'))
    pick=max(cands,key=lambda c:c[0]) if cands else None
    if not pick or walk_delta!=0:
        r0=f'{raws[0]:016x}' if raws else '-'
        d0=bank[data_start:data_start+16].hex() if dsz>=16 else '-'
        raise ValueError(f'sample offset decode failed [codec={codec} n={num} shdr={shdr} '
            f'ntab={ntab} dsz={dsz} walk_delta={walk_delta} raw0={r0} data0={d0} '
            f'cands={[(c[0],c[2]) for c in cands]}] - paste this to the dev')
    sync,offs,layout=pick
    validated = sync==num                      # every sample passed its validator
    slices=[]
    for i,o in enumerate(offs):
        end=offs[i+1] if i+1<len(offs) else dsz
        slices.append((names[i] or f'sample_{i:03d}', data_start+o, end-o, metas[i]))
    return dict(num=num, mode=mode, codec=codec, slices=slices, layout=layout,
                validated=validated,
                editable=(mode==11 and validated) or (mode==2 and validated),
                version=ver, sample_headers_size=shdr, name_table_size=ntab,
                data_size=dsz, data_start=data_start, names_start=names_start,
                headers_start=hdrs_start, raws=raws, raw_positions=raw_positions,
                chunks=chunks, chunk_records=chunk_records, offsets=offs,
                metas=metas, names=names, header_walk_end=pos)


def _align_up(value, alignment):
    alignment=max(1,int(alignment))
    return (int(value)+(alignment-1)) & ~(alignment-1)


def _infer_stock_mpeg_frame_alignment(sample_bytes):
    """Infer the actual FMOD MPEG frame alignment used by one stock sample.

    Full-length replacement cannot preserve the old finite frame-start map, so it
    must reproduce the rule that generated that map. We only accept a power-of-two
    alignment when it predicts every measured stock frame start exactly.
    """
    topo=_mpeg_frame_topology(sample_bytes)
    frames=topo.get('frames') or []
    if len(frames)<3:
        raise ValueError('stock song has too few MPEG frames to infer its alignment')
    for alignment in (16,32,64,8,4,128,256):
        ok=True
        for i in range(len(frames)-1):
            expected=_align_up(frames[i]['start']+frames[i]['length'],alignment)
            if expected!=frames[i+1]['start']:
                ok=False;break
        if ok:
            return alignment,topo
    gaps=sorted({frames[i+1]['start']-(frames[i]['start']+frames[i]['length'])
                 for i in range(min(len(frames)-1,64))})
    raise ValueError('stock MPEG frame alignment is not a consistent supported power-of-two rule; '
                     'measured gaps '+str(gaps[:12]))


def _pack_mpeg_frames_with_alignment(stream, alignment):
    source=_mpeg_frame_topology(stream)
    frames=source.get('frames') or []
    if not frames or not source.get('spec'):
        raise ValueError('encoded replacement contains no valid MPEG frames')
    out=bytearray(); starts=[]
    for frame in frames:
        if out:
            target=_align_up(len(out),alignment)
            if target>len(out): out.extend(b'\0'*(target-len(out)))
        starts.append(len(out)); out.extend(frame['data'])
    check=_mpeg_frame_topology(bytes(out))
    if check.get('starts')!=starts or len(check.get('frames') or [])!=len(frames):
        raise ValueError('aligned MPEG rebuild did not retain every encoded frame')
    return bytes(out),dict(frames=len(frames),starts=starts,spec=source['spec'],alignment=alignment)


def _encode_full_length_mpeg(raw, filename, spec, gain_db=0.0):
    """Encode the complete upload and measure its intended decoded sample count."""
    ff=ffmpeg_path()
    if not ff:
        raise ValueError('full-length music replacement needs the installed LGPL audio tools')
    fmt='mp2' if spec[0]==2 else 'mp3'
    channels=1 if spec[3] else 2
    ext=os.path.splitext(filename or '')[1] or '.bin'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'input'+ext); enc_path=os.path.join(td,'encoded.'+fmt); pcm_path=os.path.join(td,'measure.pcm')
        open(src,'wb').write(raw)
        cmd=[ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
             '-ar',str(spec[2]),'-ac',str(channels)]
        if abs(float(gain_db))>0.01:
            cmd += ['-af',f'volume={float(gain_db):.3f}dB,alimiter=limit=0.97']
        cmd += ['-c:a','mp2' if spec[0]==2 else 'libmp3lame','-b:a',f'{spec[1]}k']
        if spec[0]!=2:
            cmd += ['-write_xing','0','-id3v2_version','0']
        cmd += ['-f',fmt,enc_path,'-y']
        r=subprocess.run(cmd,capture_output=True,text=True)
        if r.returncode!=0 or not os.path.exists(enc_path):
            raise ValueError('FFmpeg could not encode the full song: '+(r.stderr or '')[-240:])
        m=subprocess.run([ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
                          '-ar',str(spec[2]),'-ac',str(channels),'-c:a','pcm_s16le',
                          '-f','s16le',pcm_path,'-y'],capture_output=True,text=True)
        if m.returncode!=0 or not os.path.exists(pcm_path):
            raise ValueError('FFmpeg could not measure the full song duration: '+(m.stderr or '')[-240:])
        encoded=open(enc_path,'rb').read(); pcm_size=os.path.getsize(pcm_path)
    first=next((i for i in range(len(encoded)) if frame_info(encoded,i)),None)
    if first is None: raise ValueError('full-song encode produced no MPEG frames')
    stream=encoded[first:]
    got=frame_info(stream,0)
    if not got or got[:4]!=spec[:4]:
        raise ValueError('full-song encode did not match the stock MPEG format')
    sample_count=pcm_size//(channels*2)
    if sample_count<=0 or sample_count>=2**30:
        raise ValueError('full song decoded sample count is outside the FSB5 header range')
    return stream,int(sample_count),channels


def _scaled_loop_points(old_start, old_end, old_samples, new_samples):
    if new_samples<=0: return 0,0
    if old_samples<=1:
        return 0,max(0,new_samples-1)
    # Map the inclusive [0, old_samples-1] timeline onto the new inclusive
    # timeline so a stock loop ending on the final sample still ends on the
    # final sample after a duration change.
    scale=float(max(0,new_samples-1))/float(old_samples-1)
    ns=max(0,min(new_samples-1,int(round(old_start*scale))))
    ne=max(ns,min(new_samples-1,int(round(old_end*scale))))
    return ns,ne


def _rebuild_fsb5_full_mpeg_sample(bank, sample_index, stream, sample_count):
    """Rebuild one MPEG sample at arbitrary duration while preserving the bank.

    The FSB5 header, names, unknown chunks, GUID and every untouched sample are
    retained. Only the target sample count/loop points, all sample data offsets,
    the bank data-size field, and the target MPEG bytes change.
    """
    fsb=parse_fsb5(bank)
    if fsb['mode']!=11 or not fsb['validated']:
        raise ValueError('full-length rebuild is limited to validated MPEG FSB5 banks')
    if fsb['layout']!='sh6x16':
        raise ValueError('full-length rebuild requires the standard FSB5 16-byte data-offset layout; found '+fsb['layout'])
    if not (0<=int(sample_index)<fsb['num']):
        raise ValueError('sample index is outside the FSB5 bank')
    idx=int(sample_index); target=fsb['slices'][idx]
    original_target=bank[target[1]:target[1]+target[2]]
    alignment,stock_topology=_infer_stock_mpeg_frame_alignment(original_target)
    packed,pack_info=_pack_mpeg_frames_with_alignment(stream,alignment)
    if pack_info['spec'][:4] != stock_topology['spec'][:4]:
        raise ValueError('replacement MPEG format does not exactly match the stock song')

    data=bytearray(); new_offsets=[]; original_blobs=[]
    for i,(_name,rel,length,_meta) in enumerate(fsb['slices']):
        aligned=_align_up(len(data),16)
        if aligned>len(data): data.extend(b'\0'*(aligned-len(data)))
        new_offsets.append(len(data))
        original_blob=bytes(bank[rel:rel+length]); original_blobs.append(original_blob)
        data.extend(packed if i==idx else original_blob)
    final=_align_up(len(data),16)
    if final>len(data): data.extend(b'\0'*(final-len(data)))
    if len(data)>=2**32:
        raise ValueError('rebuilt FSB5 data chunk exceeds the 32-bit size field')

    prefix=bytearray(bank[:fsb['data_start']])
    struct.pack_into('<I',prefix,20,len(data))
    for i,raw_pos in enumerate(fsb['raw_positions']):
        off=new_offsets[i]
        if off%16 or off//16 >= 2**28:
            raise ValueError('rebuilt sample offset is outside the FSB5 28-bit offset field')
        count=int(sample_count if i==idx else fsb['metas'][i]['samples'])
        if count<0 or count>=2**30:
            raise ValueError('rebuilt sample count is outside the FSB5 30-bit field')
        old=fsb['raws'][i]
        rebuilt=(old & 0x3F) | ((off//16)<<6) | (count<<34)
        struct.pack_into('<Q',prefix,raw_pos,rebuilt)

    loop_update=None
    for rec in fsb['chunk_records'][idx]:
        if rec['type']==3 and rec['size']>=8:
            old_start,old_end=struct.unpack_from('<II',rec['data'],0)
            new_start,new_end=_scaled_loop_points(old_start,old_end,fsb['metas'][idx]['samples'],sample_count)
            struct.pack_into('<II',prefix,rec['payload_pos'],new_start,new_end)
            loop_update=dict(old=[old_start,old_end],new=[new_start,new_end])

    rebuilt=bytes(prefix)+bytes(data)
    check=parse_fsb5(rebuilt)
    if check['num']!=fsb['num'] or check['mode']!=fsb['mode'] or check['layout']!='sh6x16':
        raise ValueError('rebuilt FSB5 bank structure did not validate')
    if check['metas'][idx]['samples']!=int(sample_count):
        raise ValueError('rebuilt FSB5 sample count readback mismatch')
    for i,(_name,rel,length,_meta) in enumerate(check['slices']):
        if i==idx: continue
        if rebuilt[rel:rel+len(original_blobs[i])] != original_blobs[i]:
            raise ValueError('untouched FSB5 sample '+str(i)+' changed during rebuild')
    new_name,new_rel,new_len,new_meta=check['slices'][idx]
    new_target=rebuilt[new_rel:new_rel+new_len]
    topo=_mpeg_frame_topology(new_target)
    if len(topo.get('frames') or [])!=pack_info['frames'] or topo.get('starts')!=pack_info['starts']:
        raise ValueError('rebuilt target MPEG topology readback mismatch')
    ok,err=_verify_mpeg_decode(new_target)
    if not ok: raise ValueError('rebuilt full song did not decode cleanly: '+str(err))
    return rebuilt,dict(old_bank_size=len(bank),new_bank_size=len(rebuilt),growth=len(rebuilt)-len(bank),
                        old_slot_size=target[2],new_slot_size=new_len,alignment=alignment,
                        frames=pack_info['frames'],samples=int(sample_count),hz=int(new_meta['hz'] or 0),
                        channels=int(new_meta['ch'] or 0),loop=loop_update)

def fit_payload(stream, slice_len, sil, fmod_pad=False):
    def cell(b): return b+b'\0'*((16-len(b)%16)%16) if fmod_pad else b
    out=b''; used=0
    for i,f in walk_frames(stream):
        fb=cell(stream[i:i+f[4]])
        if len(out)+len(fb)>slice_len: break
        out+=fb; used+=1
    if used==0: raise ValueError('no valid MPEG frames in replacement')
    sc=cell(sil) if sil else b''
    while sc and len(out)+len(sc)<=slice_len: out+=sc
    return out+b'\0'*(slice_len-len(out)), used

def _pcm16_from_wav(data, channels, hz):
    """Convert an uncompressed WAV to raw little-endian PCM16 without ffmpeg.

    Covers the ordinary case of dropping a .wav onto a PCM16 game slot: reads
    8/16/24/32-bit integer PCM via the stdlib, then matches the slot's channel
    count and sample rate with numpy. Raises ValueError for anything it cannot
    handle (float WAV, ADPCM, other compressed forms) so the caller can fall back
    to ffmpeg or report a clear message.

    Resampling is linear interpolation, which is coarser than ffmpeg's
    swresample. ffmpeg is preferred whenever it is available; this is the
    no-ffmpeg path.
    """
    import wave as _wave
    try:
        with _wave.open(io.BytesIO(data), 'rb') as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except Exception as ex:
        raise ValueError('not an uncompressed PCM WAV (' + str(ex) + ')') from ex
    if ch < 1 or fr < 1:
        raise ValueError('WAV header reports no channels or no sample rate')
    if not raw:
        raise ValueError('WAV contains no audio frames')

    if sw == 1:            # unsigned 8-bit
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        a = (a - 128.0) * 256.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype='<i2').astype(np.float32)
    elif sw == 3:          # 24-bit packed, sign-extend via the high byte
        b = np.frombuffer(raw[:len(raw) - (len(raw) % 3)], dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
        a = (v / 256.0).astype(np.float32)
    elif sw == 4:
        a = np.frombuffer(raw, dtype='<i4').astype(np.float32) / 65536.0
    else:
        raise ValueError('unsupported WAV sample width: ' + str(sw * 8) + '-bit')

    usable = (a.size // ch) * ch
    a = a[:usable].reshape(-1, ch)
    if a.shape[0] == 0:
        raise ValueError('WAV contains no complete audio frames')

    want = 1 if channels <= 1 else 2
    if a.shape[1] == want:
        pass
    elif want == 1:
        a = a.mean(axis=1, keepdims=True)
    elif a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    else:
        a = a[:, :2]

    if fr != hz:
        m = int(round(a.shape[0] * float(hz) / float(fr)))
        if m < 1:
            raise ValueError('resampling to ' + str(hz) + ' Hz leaves no audio')
        src_x = np.arange(a.shape[0], dtype=np.float64)
        dst_x = np.linspace(0.0, a.shape[0] - 1, m)
        a = np.stack([np.interp(dst_x, src_x, a[:, c]) for c in range(a.shape[1])], axis=1)

    return np.clip(np.rint(a), -32768, 32767).astype('<i2').ravel().tobytes()


# --- verified silent MPEG frames, so tail padding needs no ffmpeg -------------
# One frame per (layer, kbps, Hz, mono) for layers 2 and 3 at 128/160/192 kbps and
# 32000/44100/48000 Hz, mono and stereo. Each was generated with ffmpeg anullsrc and
# then decoded back and checked byte-by-byte: all 36 decode to pure silence.
# zlib+base64 keeps 36 frames (21 KB raw) in about 3 KB of source.
_SIL_TABLE_B64 = (
    'eNrtWm9sG0kVt0p6OrXqtZZKJaiO5nIVfElPsTdxgxC5SrXQRfABQV2J+I5A0/pOZ+9FQiRGtdP4w4FaldBQVVClSKmgh6KrrJyP'
    '8xpq2bGgaRAoCgjOtrKynQ+QfKjOK2GBa1nO8t7M7NrebJyaPxe73TdtEo9n3rx57/fevjezTqe122Lt7+asPT093T3dfSdtr3XX'
    '9Vnq+np7LWRcr+Wkps9S39dP+XH9vZo+S7XP1qOue9Lao+mz1PUp6/ZZrZo+S30fWxd+a/os1b7PW9V1+229mj5LXZ+yrs1q0/RZ'
    '6vv6NfpT+xT9cTp65nT0zOnomdPRM6ejZ05Hz5yOnjkdPXM6euZ09Mzp6JnT0TOno2dOR8+cjp45HT1zOnrmtuj5NZNcudzhsnPQ'
    '7F0cx/HC3zzTJkLvMpqPlyX9f5cq5aKULcYkr9frPPJA5D3OI4uhwrrIC9V/5IugMfQJHQr4uf/T770x7p2IJDtNpj1bEVOSJ3NF'
    'SlIuJgFvKV6MFXMT+C8m+bLFBRkbrDYWKmzKCxKl3IIvC3LkJiQfY+VDkSZlbL5s/uHIRgVYU1q4VIyBzPFyljHOEtbyJrZiLJNc'
    'WikDa0qXKpIPZMHVKWMiRUyuYJN80eC12SKwJgTiZmEsrk4Z54gUPrmMLVv0Oo+9LJU3KYG4MZAbV2ebpxuUSYvl88IA6KJCCcT1'
    'gdy4OmU8QTcok+bLZDx+0EWZEpoD5MbVmWKJFDkZW7YYvZduRzUHTBB/Ah12jrOaT3eZzeYX7DeGjpvq48920Qd5ZQgJpaDz6Ohs'
    'X9A9VJqfScbdfGEI5YoSgg+CLTfgHkqGC8N9QX9SXA+jIF6kcfjg4eP+ZDjoXo/D/GBk2Z1FUyF9FFl+My36S8A4tQxjCs7RmVQM'
    'bYMkjs58GBJKBWA8PwNj1oVc3zwwpsvCh7sjfGEdGA/3FWA+H3d/k7oUEHw4+0DcWAap4u51mC/6k6geui58CE5FVmZAKn9qGebD'
    'Dndpu38CA90fO3v2goMfD5mYg9f7N2CZQiEaVVgRcch+AD6ASOo3eUnRDhlHBoGvAa4o+qOKBui4LPOYyUmKYTYDiIwjgwD3T/Xy'
    'j9CD3u6wW7u6zNwL4EHm49e/1pQHYSgHHPICmh1xAYhkWPwI4jwAWBD9tB+hPEBBPA7TAPki4JhNCs/PMvQDQ8AQDw6gcEytMLcB'
    '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'
)
_SIL_TABLE_CACHE = None

def _sil_table():
    """Lazily unpack the baked silent-frame table. Returns {} if anything is off."""
    global _SIL_TABLE_CACHE
    if _SIL_TABLE_CACHE is None:
        try:
            import zlib, base64 as _b, json as _j
            raw = zlib.decompress(_b.b64decode(_SIL_TABLE_B64))
            cut = raw.index(b'\x00')
            index = _j.loads(raw[:cut])
            body = raw[cut + 1:]
            out, pos = {}, 0
            for layer, kbps, hz, mono, n in index:
                out[(layer, kbps, hz, bool(mono))] = body[pos:pos + n]
                pos += n
            _SIL_TABLE_CACHE = out
        except Exception:
            _SIL_TABLE_CACHE = {}
    return _SIL_TABLE_CACHE


def _sil_for(spec):
    if (spec[0],spec[1],spec[2])==(2,160,48000): return _SIL[spec[3]]
    baked=_sil_table().get((spec[0],spec[1],spec[2],bool(spec[3])))
    if baked: return baked
    ff=ffmpeg_path()
    if not ff: return b''
    fmt='mp2' if spec[0]==2 else 'mp3'
    with tempfile.TemporaryDirectory() as td:
        o=os.path.join(td,'s.bin')
        subprocess.run([ff,'-v','error','-f','lavfi','-i',
            f'anullsrc=r={spec[2]}:cl={"mono" if spec[3] else "stereo"}','-t','0.06',
            '-c:a','mp2' if spec[0]==2 else 'libmp3lame','-b:a',f'{spec[1]}k','-f',fmt,o,'-y'],check=True)
        sd=open(o,'rb').read()
    fr=walk_frames(sd[next((i for i in range(len(sd)) if frame_info(sd,i)),0):],limit=2)
    return sd[fr[0][0]:fr[0][0]+fr[0][1][4]] if fr else b''

def parse_snd(c):
    """A .SND speech container = concatenated single-sample FSB5s.
    Returns flat sample list across all sub-FSBs, chained + validated."""
    flat=[]; pos=0; subs=0
    while pos+28<=len(c):
        if c[pos:pos+4]!=b'FSB5':
            j=c.find(b'FSB5',pos)
            if j==-1: break
            pos=j
        ver,num,shdr,ntab,dsz=struct.unpack_from('<5I',c,pos+4)
        total=60+shdr+ntab+dsz
        if num==0 or num>4096 or pos+total>len(c): break
        try: fsb=parse_fsb5(c[pos:pos+total])
        except Exception: break
        for n,rel,sl,m in fsb['slices']:
            flat.append(dict(name=n, rel=pos+rel, len=sl, meta=m,
                             mode=fsb['mode'], ok=fsb['editable']))
        subs+=1; pos+=total
    return flat, subs
