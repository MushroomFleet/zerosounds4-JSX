#!/usr/bin/env python3
"""
ZeroSounds4.py - Zero Sounds 4 (PlayStation 2 style SFX synthesis)

Reference engine and command-line tool. Deterministic sound effects in the
style of the PlayStation 2 sound chip (SPU2). Every sound is three or four
SPU2 "voices"; every voice is

    procedural source sample -> SPU-ADPCM encode/decode -> 4.12 pitch counter
    with 4-point Gaussian interpolation -> SPU ADSR -> signed stereo volume

mixed at 48 kHz into a dry bus and two effect cores: a half-rate (24 kHz)
SPU-topology reverb and a cross-feedback echo. Voices with a negative right
volume are phase-inverted, which a matrix-surround decoder sends to the rear.

ZeroBytes rules: every parameter comes from hash(soundName, worldSeed). There
is no random module, no stored presets and no math.sin/exp/pow in the signal
path. The JavaScript engine in ZeroSounds4.jsx must reproduce this file's
16-bit PCM byte for byte; `--checksums` prints the CRC-32 table it is checked
against.

    python ZeroSounds4.py --list
    python ZeroSounds4.py --sound confirm --out out/
    python ZeroSounds4.py --all --seed 0xDEADBEEF --out out/
    python ZeroSounds4.py --verify

The design is described in ZeroSounds4-grounding.md.
"""
import argparse
import json
import math
import os
import sys
import wave
import zlib

import numpy as np

SR = 48000
DEFAULT_SEED = 0x1337BEEF
M32 = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# 1. ZB HASH CORE (same as ZeroSounds 1 and 2)
# ---------------------------------------------------------------------------
def _imul(a, b):
    return (a * b) & M32


def zb_hash(key, seed=0):
    """u32 hash of a numeric or string key under a u32 seed."""
    h = (seed ^ 0x9e3779b9) & M32
    if isinstance(key, str):
        for ch in key:
            h ^= ord(ch) & M32
            h = _imul(h, 0x5bd1e995)
            h ^= h >> 15
        return h & M32
    h ^= int(key) & M32
    h = _imul(h, 0xcc9e2d51)
    h = ((h << 15) | (h >> 17)) & M32
    h = _imul(h, 0x1b873593)
    h ^= h >> 16
    h = _imul(h, 0x85ebca6b)
    h ^= h >> 13
    h = _imul(h, 0xc2b2ae35)
    h ^= h >> 16
    return h & M32


def zb_hash_vec(n, seed):
    """zb_hash(k, seed) for k = 0..n-1, as a uint64 array."""
    m = np.uint64(M32)
    h = np.arange(n, dtype=np.uint64) ^ np.uint64((seed ^ 0x9e3779b9) & M32)
    h = (h * np.uint64(0xcc9e2d51)) & m
    h = ((h << np.uint64(15)) | (h >> np.uint64(17))) & m
    h = (h * np.uint64(0x1b873593)) & m
    h ^= h >> np.uint64(16)
    h = (h * np.uint64(0x85ebca6b)) & m
    h ^= h >> np.uint64(13)
    h = (h * np.uint64(0xc2b2ae35)) & m
    h ^= h >> np.uint64(16)
    return h


def zb_float(h):
    return (h & 0xFFFFFF) / 0x1000000


def zb_range(h, lo, hi):
    return lo + zb_float(h) * (hi - lo)


def zb_int(h, lo, hi):
    return lo + (h % (hi - lo + 1))


def zb_pick(h, arr):
    return arr[h % len(arr)]


# ---------------------------------------------------------------------------
# 2. DETERMINISTIC MATH - only + - * / and floor, in a fixed order
# ---------------------------------------------------------------------------
PI = 3.141592653589793
TWO_PI = 6.283185307179586
HALF_PI = 1.5707963267948966
LN2 = 0.6931471805599453
_S = [-1.0 / 6.0, 1.0 / 120.0, -1.0 / 5040.0, 1.0 / 362880.0,
      -1.0 / 39916800.0, 1.0 / 6227020800.0, -1.0 / 1307674368000.0]
_E = [1.0 / 2.0, 1.0 / 6.0, 1.0 / 24.0, 1.0 / 120.0, 1.0 / 720.0, 1.0 / 5040.0,
      1.0 / 40320.0, 1.0 / 362880.0, 1.0 / 3628800.0, 1.0 / 39916800.0,
      1.0 / 479001600.0, 1.0 / 6227020800.0]


def dsin(x):
    """Scalar sine: range reduction then a degree-15 odd polynomial."""
    r = x - math.floor(x / TWO_PI + 0.5) * TWO_PI
    if r > HALF_PI:
        r = PI - r
    if r < -HALF_PI:
        r = -PI - r
    r2 = r * r
    p = _S[6]
    for k in range(5, -1, -1):
        p = p * r2 + _S[k]
    return r * (1.0 + r2 * p)


def dexp2(x):
    """Scalar 2**x: polynomial on the fraction, exact doubling on the integer."""
    n = math.floor(x)
    y = (x - n) * LN2
    p = _E[11]
    for k in range(10, -1, -1):
        p = p * y + _E[k]
    e = 1.0 + y * (1.0 + y * p)
    return math.ldexp(e, n)


def fdsin(x):
    """dsin over a float64 array, same operations in the same order."""
    r = x - np.floor(x / TWO_PI + 0.5) * TWO_PI
    r = np.where(r > HALF_PI, PI - r, r)
    r = np.where(r < -HALF_PI, -PI - r, r)
    r2 = r * r
    p = np.full_like(r, _S[6])
    for k in range(5, -1, -1):
        p = p * r2 + _S[k]
    return r * (1.0 + r2 * p)


def fdexp2(x):
    """dexp2 over a float64 array."""
    n = np.floor(x)
    y = (x - n) * LN2
    p = np.full_like(y, _E[11])
    for k in range(10, -1, -1):
        p = p * y + _E[k]
    e = 1.0 + y * (1.0 + y * p)
    return np.ldexp(e, n.astype(np.int64))


def rnd(x):
    return math.floor(x + 0.5)


def frnd(x):
    return np.floor(x + 0.5)


# ---------------------------------------------------------------------------
# 3. SOUND TAXONOMY (anchors only - no parameters are stored)
# ---------------------------------------------------------------------------
ZB_CATEGORIES = {
    'ui':          dict(index=0, freqLow=500, freqHigh=1400, durLow=0.04, durHigh=0.16, revLow=0.18, revHigh=0.45, echoLow=0.10, echoHigh=0.30, rooms=['room', 'studio', 'hall']),
    'pickup':      dict(index=1, freqLow=700, freqHigh=2000, durLow=0.08, durHigh=0.30, revLow=0.25, revHigh=0.60, echoLow=0.15, echoHigh=0.40, rooms=['studio', 'hall']),
    'movement':    dict(index=2, freqLow=90,  freqHigh=420,  durLow=0.06, durHigh=0.24, revLow=0.06, revHigh=0.25, echoLow=0.00, echoHigh=0.12, rooms=['room', 'studio']),
    'combat':      dict(index=3, freqLow=110, freqHigh=900,  durLow=0.06, durHigh=0.35, revLow=0.12, revHigh=0.40, echoLow=0.00, echoHigh=0.20, rooms=['room', 'studio', 'hall']),
    'impact':      dict(index=4, freqLow=45,  freqHigh=200,  durLow=0.18, durHigh=0.70, revLow=0.22, revHigh=0.55, echoLow=0.00, echoHigh=0.15, rooms=['studio', 'hall']),
    'power':       dict(index=5, freqLow=260, freqHigh=1100, durLow=0.35, durHigh=1.00, revLow=0.30, revHigh=0.65, echoLow=0.20, echoHigh=0.45, rooms=['hall', 'space']),
    'damage':      dict(index=6, freqLow=120, freqHigh=520,  durLow=0.10, durHigh=0.60, revLow=0.12, revHigh=0.40, echoLow=0.05, echoHigh=0.25, rooms=['room', 'studio']),
    'environment': dict(index=7, freqLow=90,  freqHigh=700,  durLow=0.10, durHigh=0.60, revLow=0.25, revHigh=0.60, echoLow=0.10, echoHigh=0.35, rooms=['hall', 'space', 'studio']),
}
CATEGORY_ORDER = ['ui', 'pickup', 'movement', 'combat', 'impact', 'power', 'damage', 'environment']


def _s(category, arch, style_index, direction, label):
    return dict(category=category, arch=arch, styleIndex=style_index, dir=direction, label=label)


# arch: glass | pad | arp | whoosh | impact | sweep | metal.  dir: +1 rising, -1 falling, 0 hashed.
ZB_SOUNDS = {
    'cursor':     _s('ui', 'glass', 0, 0, 'Cursor'),
    'confirm':    _s('ui', 'arp', 1, 1, 'Confirm'),
    'cancel':     _s('ui', 'arp', 2, -1, 'Cancel'),
    'pause':      _s('ui', 'pad', 3, -1, 'Pause'),
    'error':      _s('ui', 'sweep', 4, -1, 'Error'),
    'save':       _s('ui', 'glass', 5, 0, 'Save'),
    'orb':        _s('pickup', 'arp', 0, 1, 'Orb'),
    'item':       _s('pickup', 'glass', 1, 0, 'Item'),
    'key':        _s('pickup', 'metal', 2, 0, 'Key'),
    'health':     _s('pickup', 'arp', 3, 1, 'Health'),
    'ammo':       _s('pickup', 'metal', 4, 0, 'Ammo'),
    'gem':        _s('pickup', 'glass', 5, 0, 'Gem'),
    'jump':       _s('movement', 'sweep', 0, 1, 'Jump'),
    'land':       _s('movement', 'impact', 1, 0, 'Land'),
    'dash':       _s('movement', 'whoosh', 2, 1, 'Dash'),
    'footstep':   _s('movement', 'impact', 3, 0, 'Footstep'),
    'grind':      _s('movement', 'whoosh', 4, -1, 'Grind'),
    'glide':      _s('movement', 'pad', 5, 1, 'Glide'),
    'slash':      _s('combat', 'whoosh', 0, -1, 'Slash'),
    'gunshot':    _s('combat', 'impact', 1, 0, 'Gunshot'),
    'punch':      _s('combat', 'impact', 2, 0, 'Punch'),
    'laser':      _s('combat', 'sweep', 3, -1, 'Laser'),
    'magic':      _s('combat', 'sweep', 4, 1, 'Magic'),
    'block':      _s('combat', 'metal', 5, 0, 'Block'),
    'explosion':  _s('impact', 'impact', 0, 0, 'Explosion'),
    'crash':      _s('impact', 'metal', 1, 0, 'Crash'),
    'thud':       _s('impact', 'impact', 2, 0, 'Thud'),
    'glassBreak': _s('impact', 'glass', 3, 0, 'Glass break'),
    'metalHit':   _s('impact', 'metal', 4, 0, 'Metal hit'),
    'quake':      _s('impact', 'impact', 5, 0, 'Quake'),
    'powerUp':    _s('power', 'arp', 0, 1, 'Power up'),
    'levelUp':    _s('power', 'arp', 1, 1, 'Level up'),
    'heal':       _s('power', 'pad', 2, 1, 'Heal'),
    'charge':     _s('power', 'sweep', 3, 1, 'Charge'),
    'checkpoint': _s('power', 'glass', 4, 0, 'Checkpoint'),
    'shield':     _s('power', 'pad', 5, 0, 'Shield'),
    'hurt':       _s('damage', 'sweep', 0, -1, 'Hurt'),
    'death':      _s('damage', 'arp', 1, -1, 'Death'),
    'alarm':      _s('damage', 'arp', 2, 1, 'Alarm'),
    'poison':     _s('damage', 'sweep', 3, -1, 'Poison'),
    'stun':       _s('damage', 'metal', 4, 0, 'Stun'),
    'lowHealth':  _s('damage', 'pad', 5, -1, 'Low health'),
    'door':       _s('environment', 'whoosh', 0, -1, 'Door'),
    'chest':      _s('environment', 'arp', 1, 1, 'Chest'),
    'splash':     _s('environment', 'whoosh', 2, -1, 'Splash'),
    'warp':       _s('environment', 'sweep', 3, 1, 'Warp'),
    'switch':     _s('environment', 'impact', 4, 0, 'Switch'),
    'wind':       _s('environment', 'whoosh', 5, 0, 'Wind'),
}
ARCHETYPES = ['glass', 'pad', 'arp', 'whoosh', 'impact', 'sweep', 'metal']
ARP_PATTERNS = [[0, 4, 7, 12, 16], [0, 7, 12, 19, 24], [0, 5, 7, 12, 17],
                [0, 3, 7, 10, 15], [0, 12, 7, 12, 19], [0, 6, 0, 6, 0], [0, 2, 4, 7, 9], [0, 12, 0, 12, 0]]
FM_RATIOS = [1.0, 2.0, 3.0, 3.5, 4.0, 5.0, 5.5, 7.0, 0.5, 1.5]
FM_STRIKE_RATIOS = [1.41, 2.76, 3.3, 4.07, 5.4, 7.13]
WAVE_SHAPES = ['pulse', 'saw', 'tri', 'organ', 'glass', 'digi']
METAL_PARTIALS = [[1.0, 2.76, 5.4], [1.0, 2.32, 4.25], [1.0, 1.51, 2.66], [1.0, 3.01, 5.2], [1.0, 2.0, 4.9]]
CYCLES = [56, 112, 224]          # samples per cycle; twice the PlayStation 1 set
SUPER_CYCLES = [56, 112]
SUPER_LOOPS = 64                 # centre cycles in an ensemble loop
MAX_STEP = 3.5
MASTER_VOL = 0.80                # SPU2 master volume (MVOL), as a fraction of full scale

# SPU ADSR register ranges per envelope family
ENV = {
    'perc':  dict(ar=[0, 12],  dr=[11, 14], sl=[0, 6],  sr=[44, 58], rr=[11, 14]),
    'sus':   dict(ar=[8, 36],  dr=[9, 12],  sl=[8, 15], sr=[52, 66], rr=[8, 12]),
    'pad':   dict(ar=[36, 50], dr=[11, 13], sl=[9, 15], sr=[56, 70], rr=[12, 14]),
    'noise': dict(ar=[0, 30],  dr=[9, 13],  sl=[2, 12], sr=[42, 56], rr=[10, 13]),
    'hit':   dict(ar=[0, 4],   dr=[8, 11],  sl=[0, 3],  sr=[40, 50], rr=[9, 12]),
}
# Parametric defaults in the SPU reverb topology - not the Sony preset register tables.
# Delay lengths are in samples at the 24 kHz reverb rate.
REVERBS = {
    'room':   dict(scale=0.30, wall=0.55, iir=0.65, tail=0.5),
    'studio': dict(scale=0.50, wall=0.62, iir=0.60, tail=0.8),
    'hall':   dict(scale=1.00, wall=0.72, iir=0.50, tail=1.6),
    'space':  dict(scale=1.70, wall=0.80, iir=0.40, tail=2.4),
}
REV_LENGTHS = [3847, 4253, 3163, 3613, 667, 389]


# ---------------------------------------------------------------------------
# 4. PARAMETER DERIVATION  (world -> category -> sound -> layer -> slot)
# ---------------------------------------------------------------------------
def _derive_adsr(h, family):
    e = ENV[family]
    lin = family in ('sus', 'pad')
    return dict(
        ar=zb_int(h(20), e['ar'][0], e['ar'][1]),      # attack rate 0..127
        am=(h(21) & 1) if family == 'sus' else 0,      # attack mode 0 lin / 1 exp
        dr=zb_int(h(22), e['dr'][0], e['dr'][1]),      # decay shift 0..15
        sl=zb_int(h(23), e['sl'][0], e['sl'][1]),      # sustain level 0..15
        sm=1,                                          # sustain mode exp
        sd=1,                                          # sustain direction decrease
        sr=zb_int(h(24), e['sr'][0], e['sr'][1]),      # sustain rate 0..127
        rm=(h(25) & 1) if lin else 1,                  # release mode 0 lin / 1 exp
        rr=zb_int(h(26), e['rr'][0], e['rr'][1]),      # release shift 0..31
    )


def _derive_source(kind, h, seed, fmax, hint):
    cycle = 0
    if kind in ('fm', 'wave', 'super'):
        pool = SUPER_CYCLES if kind == 'super' else CYCLES
        cands = [c for c in pool if fmax * c / SR <= MAX_STEP]
        if not cands:
            cands = [28]
        cycle = zb_pick(h(10), cands)
    if kind == 'fm':
        strike = hint.get('strike', 0.8)
        return dict(kind='fm', cycle=cycle, ratio=zb_pick(h(11), FM_RATIOS),
                    index0=zb_range(h(12), 1.0, 6.0), indexLoop=zb_range(h(13), 0.1, 1.0),
                    attackLoops=zb_int(h(14), 4, 40), ratio2=zb_pick(h(15), FM_STRIKE_RATIOS),
                    index2=zb_range(h(16), 0.0, strike), seed=seed)
    if kind == 'wave':
        return dict(kind='wave', cycle=cycle, shape=zb_pick(h(11), WAVE_SHAPES),
                    duty=zb_range(h(12), 0.125, 0.5), harmonics=zb_pick(h(13), [8, 16, 32, 48]), seed=seed)
    if kind == 'super':
        return dict(kind='super', cycle=cycle, loops=SUPER_LOOPS,
                    harmonics=zb_pick(h(11), [8, 12, 16]), seed=seed)
    if kind == 'noise':
        loop = bool(hint.get('loop', False))
        blocks = 584 if loop else min(2400, max(40, math.floor(hint['dur'] * SR / 28.0) + 1))
        return dict(kind='noise', blocks=blocks, loop=loop, mode=hint['mode'], f0=hint['f0'],
                    oct=0.0 if loop else hint['oct'], q=hint['q'], seed=seed)
    f1, f2, bl = hint.get('f1', [150.0, 340.0]), hint.get('f2', [40.0, 75.0]), hint.get('blocks', [80, 200])
    return dict(kind='thump', blocks=zb_int(h(11), bl[0], bl[1]), f1=zb_range(h(12), f1[0], f1[1]),
                f2=zb_range(h(13), f2[0], f2[1]), seed=seed)


def _derive(name, category, arch, style_index, dir_in, world_seed):
    cat = ZB_CATEGORIES[category]
    cat_seed = zb_hash(cat['index'], world_seed)
    sound_seed = zb_hash(style_index, cat_seed)
    hs = lambda k: zb_hash(100 + k, sound_seed)

    base_freq = zb_range(hs(0), cat['freqLow'], cat['freqHigh'])
    hold = zb_range(hs(1), cat['durLow'], cat['durHigh'])
    reverb = dict(preset=zb_pick(hs(2), cat['rooms']), depth=zb_range(hs(3), cat['revLow'], cat['revHigh']))
    echo = dict(time=zb_range(hs(20), 0.09, 0.30), feedback=zb_range(hs(21), 0.20, 0.50),
                depth=zb_range(hs(22), cat['echoLow'], cat['echoHigh']))
    direction = dir_in
    if direction == 0:
        direction = 1 if (hs(7) & 1) else -1

    notes = [dict(t=0.0, ratio=1.0)]
    if arch == 'arp':
        pat = zb_pick(hs(5), ARP_PATTERNS)
        count = zb_int(hs(4), 2, len(pat))
        gap = zb_range(hs(6), 0.045, 0.11)
        notes = [dict(t=i * gap, ratio=dexp2(direction * pat[i] / 12.0)) for i in range(count)]
    pan = zb_range(hs(9), -0.5, 0.5)

    def layer(i, kind, role, family, **o):
        seed = zb_hash(i + 1, sound_seed)
        h = lambda k: zb_hash(k, seed)
        nts = o.get('notes') or [dict(t=0.0, ratio=1.0)]
        sweep = o.get('sweep', 0.0)
        vib = o.get('vib', 0.0)
        has_freq = 'freq' in o
        freq = o['freq'] if has_freq else None
        fmax = 0.0
        if has_freq:
            top = 0.0
            for n in nts:
                if n['ratio'] > top:
                    top = n['ratio']
            top = top * dexp2(max(0.0, sweep) + vib)
            freq = min(freq, MAX_STEP * SR / 28.0 / top)
            fmax = freq * top
        source = _derive_source(kind, h, seed, fmax, o.get('src', {}))
        step0 = freq * source['cycle'] / SR if has_freq else o['step0']
        hd = o.get('hold', hold)
        sweep_frac = o.get('sweepFrac', 1.0)
        pan_l = o.get('pan', 0.0)
        sur = bool(o.get('sur', False))
        v = rnd(o.get('vol', 0.5) * 0x4000)
        if sur:                                   # phase-inverted pair: decodes to the rear
            vol_l, vol_r = rnd(v * 0.75), -rnd(v * 0.75)
        else:
            vol_l, vol_r = rnd(v * min(1.0, 1.0 - pan_l)), rnd(v * min(1.0, 1.0 + pan_l))
        return dict(
            role=role, source=source, freq=freq, step0=step0,
            sweepOct=sweep, sweepTime=max(0.005, hd * sweep_frac),
            vibDepth=vib, vibRate=zb_range(h(30), 4.0, 9.0),
            pmon=bool(o.get('pmon', False)), mute=bool(o.get('mute', False)), pmDepth=o.get('pmDepth', 0.0),
            adsr=_derive_adsr(h, family), hold=hd, volL=vol_l, volR=vol_r,
            reverb=bool(o.get('send', True)), echo=bool(o.get('echo', False)), surround=sur, notes=nts,
        )

    L = []
    if arch == 'glass':
        L.append(layer(0, 'fm', 'body', 'perc', freq=base_freq, vol=0.46, pan=pan,
                       vib=zb_range(hs(10), 0.0, 0.006), echo=True, src=dict(strike=1.2)))
        L.append(layer(1, 'fm', 'shimmer', 'perc',
                       freq=base_freq * zb_pick(hs(11), [2.0, 2.0, 3.0, 1.5, 4.0]) * (1.0 + zb_range(hs(12), 0.002, 0.008)),
                       vol=0.20, pan=-pan, echo=True, src=dict(strike=1.2)))
        L.append(layer(2, 'noise', 'air', 'noise', step0=1.0, vol=0.10, sur=True,
                       src=dict(mode='hp', f0=zb_range(hs(13), 2500.0, 6000.0), oct=-zb_range(hs(14), 0.2, 1.5),
                                q=zb_range(hs(15), 0.5, 1.2), dur=hold + 0.3)))
    elif arch == 'pad':
        sw = direction * zb_range(hs(10), 0.0, 0.25)
        hd = hold * 1.5 + 0.12
        L.append(layer(0, 'super', 'body', 'pad', freq=base_freq, vol=0.50, pan=pan, sweep=sw, hold=hd, echo=True))
        L.append(layer(1, 'wave', 'bright', 'perc', freq=base_freq * 2.0 * (1.0 + zb_range(hs(11), 0.003, 0.010)),
                       vol=0.18, sur=True, sweep=sw, hold=hd))
        L.append(layer(2, 'wave', 'under', 'pad', freq=base_freq * 0.5, vol=0.30, pan=-pan, sweep=sw, hold=hd))
    elif arch == 'arp':
        kind = 'fm' if (hs(8) & 1) else 'wave'
        note_hold = min(hold, notes[1]['t'] * 1.5) if len(notes) > 1 else hold
        L.append(layer(0, kind, 'body', 'perc', freq=base_freq, vol=0.34, pan=pan, hold=note_hold, notes=notes, echo=True))
        L.append(layer(1, 'wave' if kind == 'fm' else 'fm', 'octave', 'perc', freq=base_freq * 2.0,
                       vol=0.14, pan=-pan, hold=note_hold, notes=notes, echo=True))
        L.append(layer(2, 'super', 'bed', 'pad', freq=base_freq, vol=0.13, sur=True,
                       hold=notes[len(notes) - 1]['t'] + note_hold))
    elif arch == 'whoosh':
        dur = hold * zb_range(hs(12), 1.0, 1.8) + 0.15
        oc = direction * zb_range(hs(10), 1.0, 3.0)
        f0 = zb_range(hs(11), 300.0, 1200.0) if direction > 0 else zb_range(hs(11), 2500.0, 7000.0)
        sw = direction * zb_range(hs(13), 0.1, 0.6)
        hd = dur - 0.15
        L.append(layer(0, 'noise', 'body', 'noise', step0=1.0, vol=0.70, pan=pan, sweep=sw, hold=hd,
                       src=dict(mode='bp', f0=f0, oct=oc, q=zb_range(hs(14), 0.35, 0.9), dur=dur)))
        L.append(layer(1, 'noise', 'width', 'noise', step0=zb_range(hs(15), 0.7, 0.95), vol=0.40, sur=True, sweep=sw, hold=hd,
                       src=dict(mode=zb_pick(hs(16), ['lp', 'hp']), f0=f0 * zb_range(hs(17), 0.6, 1.4), oct=oc * 0.8,
                                q=0.8, dur=dur)))
        L.append(layer(2, 'super', 'tone', 'pad', freq=base_freq, vol=0.16, pan=-pan,
                       sweep=direction * zb_range(hs(18), 0.5, 1.5), hold=hd, echo=True))
    elif arch == 'impact':
        big = hold >= 0.3                              # long holds become heavy, sub-driven impacts
        L.append(layer(0, 'noise', 'crack', 'hit', step0=1.0, vol=0.40, pan=pan, hold=min(hold, 0.12),
                       src=dict(mode='lp', f0=zb_range(hs(10), 4500.0, 7400.0), oct=-zb_range(hs(11), 2.5, 5.0),
                                q=zb_range(hs(12), 0.6, 1.2), dur=zb_range(hs(13), 0.10, 0.30) * (1.6 if big else 1.0))))
        L.append(layer(1, 'thump', 'body', 'perc', step0=zb_range(hs(14), 0.5, 1.0) * (0.7 if big else 1.0), vol=0.50,
                       src=dict(f1=[150.0, 340.0], f2=[40.0, 75.0], blocks=[80, 200])))
        L.append(layer(2, 'thump', 'sub', 'perc', step0=zb_range(hs(15), 0.6, 0.9), vol=0.50 if big else 0.25, send=False,
                       src=dict(f1=[70.0, 120.0], f2=[26.0, 40.0], blocks=[400, 800] if big else [150, 300])))
        L.append(layer(3, 'noise', 'debris', 'noise', step0=zb_range(hs(16), 0.25, 0.6), vol=0.20 if big else 0.08,
                       sur=True, sweep=-zb_range(hs(17), 0.3, 1.2), hold=hold if big else min(hold, 0.10),
                       src=dict(loop=True, mode='bp', f0=zb_range(hs(18), 500.0, 2500.0), oct=0.0,
                                q=zb_range(hs(19), 0.4, 0.9))))
    elif arch == 'sweep':
        lfo = (hs(10) & 1) == 1
        mod_freq = zb_range(hs(11), 6.0, 40.0) if lfo else base_freq * zb_pick(hs(11), [0.5, 1.0, 2.0, 3.0, 0.25])
        sw = direction * zb_range(hs(12), 0.7, 2.5)
        frac = zb_range(hs(13), 0.6, 1.2)
        kind = 'fm' if (hs(14) & 1) else 'wave'
        L.append(layer(0, 'wave', 'modulator', 'sus', freq=mod_freq, vol=0.5, mute=True, send=False,
                       pmDepth=zb_range(hs(15), 0.05, 0.6)))
        L.append(layer(1, kind, 'carrier', 'sus', freq=base_freq, vol=0.46, pan=pan, sweep=sw, sweepFrac=frac,
                       pmon=True, echo=True))
        L.append(layer(2, kind, 'chorus', 'sus', freq=base_freq * (1.0 + zb_range(hs(16), 0.004, 0.012)),
                       vol=0.20, pan=-pan, sweep=sw, sweepFrac=frac))
        L.append(layer(3, 'super', 'under', 'pad', freq=base_freq * 0.5, vol=0.14, sur=True, sweep=sw, sweepFrac=frac))
    else:  # metal
        part = zb_pick(hs(10), METAL_PARTIALS)
        L.append(layer(0, 'fm', 'partial 1', 'perc', freq=base_freq * part[0], vol=0.40, pan=pan, src=dict(strike=4.0)))
        L.append(layer(1, 'fm', 'partial 2', 'perc', freq=base_freq * part[1] * (1.0 + zb_range(hs(14), 0.001, 0.006)),
                       vol=0.24, pan=-pan, src=dict(strike=4.0)))
        L.append(layer(2, 'fm', 'partial 3', 'perc', freq=base_freq * part[2], vol=0.14, sur=True, src=dict(strike=4.0)))
        L.append(layer(3, 'noise', 'strike', 'hit', step0=1.0, vol=0.20, pan=pan, hold=min(hold, 0.06),
                       src=dict(mode='hp', f0=zb_range(hs(11), 2000.0, 5500.0), oct=-zb_range(hs(12), 0.0, 1.5),
                                q=0.7, dur=zb_range(hs(13), 0.05, 0.14))))

    if not any(l['echo'] for l in L):
        echo['depth'] = 0.0
    return dict(name=name, category=category, archetype=arch, direction=direction,
                worldSeed=world_seed & M32, soundSeed=sound_seed, baseFreq=base_freq, hold=hold,
                reverb=reverb, echo=echo, layers=L)


def zb_derive_params(name, world_seed=DEFAULT_SEED):
    """Named sound -> patch. O(1), pure."""
    if name not in ZB_SOUNDS:
        raise KeyError('Unknown sound: %s' % name)
    s = ZB_SOUNDS[name]
    return _derive(name, s['category'], s['arch'], s['styleIndex'], s['dir'], world_seed & M32)


def zb_randomize(n, world_seed=DEFAULT_SEED):
    """Unnamed sound at position n of the infinite patch space."""
    v = zb_hash(n, (world_seed ^ 0x5A5A5A5A) & M32)
    category = CATEGORY_ORDER[zb_int(zb_hash(0, v), 0, 7)]
    arch = ARCHETYPES[zb_int(zb_hash(1, v), 0, 6)]
    return _derive('patch#%d' % n, category, arch, 1000 + (v & 0xFFFF), 0, world_seed & M32)


# ---------------------------------------------------------------------------
# 5. PROCEDURAL SOURCE SAMPLES - the virtual sample RAM
# ---------------------------------------------------------------------------
def _wave_amp(src, k):
    jit = 0.85 + 0.3 * zb_float(zb_hash(300 + k, src['seed']))
    shape = src['shape']
    if shape == 'saw':
        a = 1.0 / k
    elif shape == 'pulse':
        a = dsin(PI * k * src['duty']) / k
    elif shape == 'tri':
        a = 0.0 if k % 2 == 0 else (1.0 if (k >> 1) % 2 == 0 else -1.0) / (k * k)
    elif shape == 'organ':
        a = zb_range(zb_hash(200 + k, src['seed']), 0.2, 1.0) if k in (1, 2, 3, 4, 6, 8) else 0.0
    elif shape == 'glass':
        a = 1.0 / k if k in (1, 3, 4, 7, 9, 13) else 0.0
    else:  # digi: a hashed spectrum with a 1/k tilt
        r = zb_float(zb_hash(200 + k, src['seed']))
        a = (0.15 + r * r) / k
    return a * jit


def gen_source(src):
    """-> (pcm int64 array, loop_start). loop_start -1 means one-shot. len(pcm) % 28 == 0."""
    kind = src['kind']
    if kind == 'fm':
        P = src['cycle']
        n_att = src['attackLoops'] * 2 * P
        N = n_att + 2 * P
        n = np.arange(N, dtype=np.float64)
        t = np.where(n < n_att, 1.0 - n / n_att, 0.0)
        idx = src['indexLoop'] + (src['index0'] - src['indexLoop']) * (t * t)
        idx2 = src['index2'] * t
        ph = TWO_PI * n / P
        y = fdsin(ph + idx * fdsin(src['ratio'] * ph) + idx2 * fdsin(src['ratio2'] * ph))
        return frnd(26000.0 * y).astype(np.int64), n_att
    if kind == 'wave':
        P = src['cycle']
        N = 2 * P
        hmax = min(src['harmonics'], (P >> 1) - 1)
        n = np.arange(N, dtype=np.float64)
        y = np.zeros(N)
        for k in range(1, hmax + 1):
            a = _wave_amp(src, k)
            if a != 0.0:
                y = y + a * fdsin(k * (TWO_PI * n / P))
        peak = float(np.max(np.abs(y)))
        return frnd(26000.0 * y / peak).astype(np.int64), 0
    if kind == 'super':
        P = src['cycle']
        C = src['loops']
        N = C * P
        hmax = min(src['harmonics'], (P >> 1) - 1)
        n = np.arange(N, dtype=np.float64)
        y = np.zeros(N)
        for j in range(3):                               # C-1, C and C+1 cycles in the same loop
            amp = 0.7 + 0.3 * zb_float(zb_hash(400 + j, src['seed']))
            phi = TWO_PI * zb_float(zb_hash(410 + j, src['seed']))
            w = TWO_PI * (C + j - 1) / N
            for k in range(1, hmax + 1):
                y = y + (amp / k) * fdsin(k * (w * n + phi))
        peak = float(np.max(np.abs(y)))
        return frnd(26000.0 * y / peak).astype(np.int64), 0
    if kind == 'noise':
        N = 28 * src['blocks']
        h = zb_hash_vec(N, src['seed'])
        w = ((h & np.uint64(0xFFFFFF)).astype(np.float64) / 0x1000000 * 2.0 - 1.0).tolist()
        n = np.arange(N, dtype=np.float64)
        fc = src['f0'] * fdexp2(src['oct'] * (n / N))
        fc = np.minimum(7500.0, np.maximum(40.0, fc))
        f = (2.0 * fdsin(PI * fc / SR)).tolist()
        q = src['q']
        mode = {'lp': 0, 'bp': 1, 'hp': 2}[src['mode']]
        y = [0.0] * N
        low = band = 0.0
        for i in range(N):                               # Chamberlin state-variable filter
            low = low + f[i] * band
            high = w[i] - low - q * band
            band = band + f[i] * high
            y[i] = low if mode == 0 else (band if mode == 1 else high)
        y = np.array(y)
        peak = float(np.max(np.abs(y)))
        if src['loop']:
            g = np.ones(N)
        else:                                            # one-shots end in silence
            g = np.minimum(1.0, (N - n) / (math.floor(N * 0.15) + 1))
        return frnd(24000.0 * y / peak * g).astype(np.int64), (0 if src['loop'] else -1)
    # thump: sine whose pitch falls f1 -> f2, phase integrated in closed form
    N = 28 * src['blocks']
    c1, c2 = src['f1'] / SR, src['f2'] / SR
    n = np.arange(N, dtype=np.float64)
    t = 1.0 - n / N
    t2 = t * t
    ph = TWO_PI * (c2 * n + (c1 - c2) * N * 0.25 * (1.0 - t2 * t2))
    return frnd(28000.0 * (fdsin(ph) * t2)).astype(np.int64), -1


# ---------------------------------------------------------------------------
# 6. SPU-ADPCM (28 samples -> 16 bytes: shift/filter header + 4-bit nibbles)
# ---------------------------------------------------------------------------
K0 = [0, 60, 115, 98, 122]
K1 = [0, 0, -52, -55, -60]


def adpcm_codec(s):
    """Encode then decode. -> (decoded int64 array, [(shift, filter)] per block).

    Filter choice is open-loop (smallest peak residual against the original
    samples); the shift is then chosen closed-loop between two candidates.
    """
    n = len(s)
    nb = n // 28
    p1 = np.concatenate(([0], s[:-1]))
    p2 = np.concatenate(([0, 0], s[:-2]))
    m = np.empty((5, nb), dtype=np.int64)
    for f in range(5):
        e = np.abs(s - ((p1 * K0[f] + p2 * K1[f] + 32) >> 6))
        m[f] = e.reshape(nb, 28).max(axis=1)
    bf = np.argmin(m, axis=0).tolist()
    bm = m.min(axis=0).tolist()
    sl = s.tolist()
    out = [0] * n
    blocks = []
    h1 = h2 = 0
    for b in range(nb):
        f, mm = bf[b], bm[b]
        k0, k1 = K0[f], K1[f]
        q = 0
        while q < 12 and 7 * (1 << q) < mm:
            q += 1
        best_err, best_dec, best_shift = -1, None, 0
        for qq in ((q, q + 1) if q < 12 else (q,)):
            step = 1 << qq
            a1, a2, err = h1, h2, 0
            dec = [0] * 28
            for k in range(28):
                x = sl[b * 28 + k]
                pred = (a1 * k0 + a2 * k1 + 32) >> 6
                nib = (2 * (x - pred) + step) // (2 * step)
                if nib > 7:
                    nib = 7
                elif nib < -8:
                    nib = -8
                d = pred + nib * step
                if d > 32767:
                    d = 32767
                elif d < -32768:
                    d = -32768
                err += (x - d) * (x - d)
                dec[k] = d
                a2 = a1
                a1 = d
            if best_err < 0 or err < best_err:
                best_err, best_dec, best_shift = err, dec, 12 - qq
        out[b * 28:b * 28 + 28] = best_dec
        blocks.append((best_shift, f))
        h1, h2 = best_dec[27], best_dec[26]
    return np.array(out, dtype=np.int64), blocks


# ---------------------------------------------------------------------------
# 7. SPU2 VOICE: Gaussian interpolation + ADSR
# ---------------------------------------------------------------------------
def _gauss_table():
    """512-entry kernel. Analytic fit (peak 0.7008, sigma 0.569), not the console ROM table."""
    j = np.arange(512, dtype=np.float64)
    x = (511.0 - j) / 256.0
    return frnd(32768.0 * 0.7008 * fdexp2(-(x * x) / (2.0 * 0.569 * 0.569) / LN2)).astype(np.int64)


GAUSS = _gauss_table()
_adsr_cache = {}


def adsr_run(a, keyoff, cap):
    """SPU ADSR state machine -> per-sample level (0..0x7FFF) until release hits zero."""
    key = (a['ar'], a['am'], a['dr'], a['sl'], a['sm'], a['sd'], a['sr'], a['rm'], a['rr'], keyoff, cap)
    if key in _adsr_cache:
        return _adsr_cache[key]
    end = keyoff + cap
    out = []
    sus_level = (a['sl'] + 1) * 0x800
    level = counter = phase = 0
    i = 0
    while i < end:
        if i == keyoff:
            phase = 3
        if phase == 0:
            exp, inc, shift, step = a['am'], True, a['ar'] >> 2, 7 - (a['ar'] & 3)
        elif phase == 1:
            exp, inc, shift, step = 1, False, a['dr'], -8
        elif phase == 2:
            inc = a['sd'] == 0
            exp, shift = a['sm'], a['sr'] >> 2
            step = 7 - (a['sr'] & 3) if inc else -8 + (a['sr'] & 3)
        else:
            exp, inc, shift, step = a['rm'], False, a['rr'], -8
        cycles = 1 << (shift - 11 if shift > 11 else 0)
        st = step * (1 << (11 - shift if shift < 11 else 0))
        if exp and inc and level > 0x6000:
            cycles *= 4
        if exp and not inc:
            st = (st * level) // 32768
        counter += 1
        if counter >= cycles:
            counter = 0
            level += st
            if level < 0:
                level = 0
            elif level > 0x7FFF:
                level = 0x7FFF
        if phase == 0 and level >= 0x7FFF:
            phase = 1
        elif phase == 1 and level <= sus_level:
            phase = 2
        out.append(level)
        i += 1
        if phase == 3 and level == 0:
            break
    env = np.array(out, dtype=np.int64)
    if len(_adsr_cache) > 256:
        _adsr_cache.clear()
    _adsr_cache[key] = env
    return env


def render_voice(pcm, loop_start, layer, ratio, mod=None, mod_off=0):
    """One key-on of one SPU2 voice -> (float64 array of integers, envelope)."""
    env = adsr_run(layer['adsr'], math.floor(layer['hold'] * SR), 3 * SR)
    n = len(env)
    L = len(pcm)
    loop_len = L - loop_start
    sweep_n = max(1, math.floor(layer['sweepTime'] * SR))
    i = np.arange(n, dtype=np.float64)
    u = np.minimum(1.0, i / sweep_n)
    u = u * (2.0 - u)
    ex = layer['sweepOct'] * u
    if layer['vibDepth'] > 0.0:
        ex = ex + layer['vibDepth'] * fdsin(TWO_PI * layer['vibRate'] * i / SR)
    step = (layer['step0'] * ratio) * fdexp2(ex)
    if mod is not None:                                  # SPU pitch modulation (PMON)
        m = np.zeros(n)
        avail = max(0, min(n, len(mod) - mod_off))
        m[:avail] = mod[mod_off:mod_off + avail]
        step = step * ((32768.0 + m) / 32768.0)
    si = frnd(step * 4096.0).astype(np.int64)            # 4.12 fixed-point pitch, 0x1000 = 48 kHz
    si = np.clip(si, 1, 0x3FFF)
    pos = np.concatenate(([0], np.cumsum(si)[:-1]))
    idx = pos // 4096
    fr = (pos // 16) % 256

    def tap(k):
        over = k >= L
        if loop_start < 0:
            kk = np.where(over | (k < 0), 0, k)
            return np.where(over | (k < 0), 0, pcm[kk])
        kk = np.where(over, loop_start + (k - loop_start) % loop_len, k)
        return np.where(kk < 0, 0, pcm[np.maximum(kk, 0)])

    G = GAUSS
    s = (G[255 - fr] * tap(idx - 1) + G[511 - fr] * tap(idx)
         + G[256 + fr] * tap(idx + 1) + G[fr] * tap(idx + 2)) >> 15
    return ((s * env) >> 15).astype(np.float64), env


# ---------------------------------------------------------------------------
# 8. EFFECT CORES
# ---------------------------------------------------------------------------
def spu_reverb(in_l, in_r, preset):
    """Core 0: SPU-topology reverb at 24 kHz (wall reflections -> 4 comb taps -> 2 all-pass)."""
    p = REVERBS[preset]
    wall, iir = p['wall'], p['iir']
    d = lambda b: max(2, math.floor(b * p['scale']))
    nSL, nSR, nDL, nDR, nA1, nA2 = [d(b) for b in REV_LENGTHS]
    t1l, t2l, t3l, t4l = math.floor(nSL * 0.93), math.floor(nSL * 0.71), math.floor(nDL * 0.83), math.floor(nDL * 0.59)
    t1r, t2r, t3r, t4r = math.floor(nSR * 0.93), math.floor(nSR * 0.71), math.floor(nDR * 0.83), math.floor(nDR * 0.59)
    SL, SRb, DL, DR = [0.0] * nSL, [0.0] * nSR, [0.0] * nDL, [0.0] * nDR
    A1L, A1R, A2L, A2R = [0.0] * nA1, [0.0] * nA1, [0.0] * nA2, [0.0] * nA2
    wSL = wSR = wDL = wDR = wA1 = wA2 = 0
    zl1 = zr1 = zl2 = zr2 = pl = pr = 0.0
    g_in, g1, g2 = 1.0 - wall, 0.6, 0.5
    half = len(in_l) >> 1
    il, ir = in_l.tolist(), in_r.tolist()
    out_l, out_r = [0.0] * (half * 2), [0.0] * (half * 2)
    for j in range(half):
        xl = (il[2 * j] + il[2 * j + 1]) * 0.5 * g_in
        xr = (ir[2 * j] + ir[2 * j + 1]) * 0.5 * g_in
        a, b, c, e = SL[wSL], SRb[wSR], DR[wDR], DL[wDL]
        zl1 = zl1 + (xl + a * wall - zl1) * iir
        zr1 = zr1 + (xr + b * wall - zr1) * iir
        zl2 = zl2 + (xl + c * wall - zl2) * iir
        zr2 = zr2 + (xr + e * wall - zr2) * iir
        SL[wSL] = zl1
        SRb[wSR] = zr1
        DL[wDL] = zl2
        DR[wDR] = zr2
        lo = (0.40 * SL[(wSL - t1l + nSL) % nSL] + 0.35 * SL[(wSL - t2l + nSL) % nSL]
              + 0.35 * DL[(wDL - t3l + nDL) % nDL] + 0.30 * DL[(wDL - t4l + nDL) % nDL])
        ro = (0.40 * SRb[(wSR - t1r + nSR) % nSR] + 0.35 * SRb[(wSR - t2r + nSR) % nSR]
              + 0.35 * DR[(wDR - t3r + nDR) % nDR] + 0.30 * DR[(wDR - t4r + nDR) % nDR])
        z = A1L[wA1]; v = lo - g1 * z; A1L[wA1] = v; lo = v * g1 + z
        z = A1R[wA1]; v = ro - g1 * z; A1R[wA1] = v; ro = v * g1 + z
        z = A2L[wA2]; v = lo - g2 * z; A2L[wA2] = v; lo = v * g2 + z
        z = A2R[wA2]; v = ro - g2 * z; A2R[wA2] = v; ro = v * g2 + z
        wSL = (wSL + 1) % nSL; wSR = (wSR + 1) % nSR; wDL = (wDL + 1) % nDL; wDR = (wDR + 1) % nDR
        wA1 = (wA1 + 1) % nA1; wA2 = (wA2 + 1) % nA2
        out_l[2 * j] = (pl + lo) * 0.5; out_l[2 * j + 1] = lo
        out_r[2 * j] = (pr + ro) * 0.5; out_r[2 * j + 1] = ro
        pl, pr = lo, ro
    return np.array(out_l), np.array(out_r)


def spu_echo(in_l, in_r, time, feedback):
    """Core 1: cross-feedback (ping-pong) echo. out[i] = x[i - D], x = in + fb * other[i - D]."""
    n = len(in_l)
    D = max(1, math.floor(time * SR))
    xl, xr = in_l.copy(), in_r.copy()
    for a in range(D, n, D):                             # a block of D samples only reads the block before it
        b = min(a + D, n)
        xl[a:b] = in_l[a:b] + feedback * xr[a - D:b - D]
        xr[a:b] = in_r[a:b] + feedback * xl[a - D:b - D]
    out_l, out_r = np.zeros(n), np.zeros(n)
    if D < n:
        out_l[D:] = xl[:n - D]
        out_r[D:] = xr[:n - D]
    return out_l, out_r


# ---------------------------------------------------------------------------
# 9. RENDER
# ---------------------------------------------------------------------------
def zs_render(patch, detail=False):
    """patch -> dict(left, right: int16 arrays, sampleRate, clipped, spuRamBytes, layerEnvs)."""
    voices, mods, layer_envs = [], [], []
    spu_ram = 0
    snr_num = snr_den = 0.0
    for layer in patch['layers']:
        raw, loop_start = gen_source(layer['source'])
        pcm, blocks = adpcm_codec(raw)
        spu_ram += len(blocks) * 16
        if detail:
            snr_num += float(np.sum(raw.astype(np.float64) ** 2))
            snr_den += float(np.sum((raw - pcm).astype(np.float64) ** 2))
        prev = mods[-1] if (layer['pmon'] and mods) else None
        parts = []
        n = 0
        for note in layer['notes']:
            off = math.floor(note['t'] * SR)
            out, env = render_voice(pcm, loop_start, layer, note['ratio'], prev, off)
            parts.append((off, out, env))
            n = max(n, off + len(out))
        mono = np.zeros(n)
        for off, out, _ in parts:
            mono[off:off + len(out)] += out
        mod = np.floor(mono * layer['pmDepth']) if layer['mute'] else mono
        mods.append(mod)
        voices.append((mono, layer))
        layer_envs.append(parts[0][2])

    rv = REVERBS[patch['reverb']['preset']]
    ec = patch['echo']
    tail = rv['tail']
    if ec['depth'] > 0.0:
        tail = max(tail, min(2.5, ec['time'] * 6.0))
    n = max(len(m) for m, _ in voices) + math.floor(tail * SR)
    n += n & 1
    dry_l, dry_r = np.zeros(n), np.zeros(n)
    rev_l, rev_r = np.zeros(n), np.zeros(n)
    ech_l, ech_r = np.zeros(n), np.zeros(n)
    for mono, layer in voices:
        if layer['mute']:
            continue
        k = len(mono)
        l = np.floor(mono * layer['volL'] / 16384)
        r = np.floor(mono * layer['volR'] / 16384)       # a negative volume inverts phase
        dry_l[:k] += l
        dry_r[:k] += r
        if layer['reverb']:
            rev_l[:k] += l
            rev_r[:k] += r
        if layer['echo']:
            ech_l[:k] += l
            ech_r[:k] += r
    wet_l, wet_r = spu_reverb(rev_l, rev_r, patch['reverb']['preset'])
    if ec['depth'] > 0.0:
        eo_l, eo_r = spu_echo(ech_l, ech_r, ec['time'], ec['feedback'])
    else:
        eo_l, eo_r = np.zeros(n), np.zeros(n)
    depth, edepth = patch['reverb']['depth'], ec['depth']
    l = frnd((dry_l + wet_l * depth + eo_l * edepth) * MASTER_VOL)
    r = frnd((dry_r + wet_r * depth + eo_r * edepth) * MASTER_VOL)
    clipped = int(np.count_nonzero((l > 32767) | (l < -32768) | (r > 32767) | (r < -32768)))
    l = np.clip(l, -32768, 32767)
    r = np.clip(r, -32768, 32767)
    loud = np.nonzero((np.abs(l) > 24) | (np.abs(r) > 24))[0]
    end = 1 if len(loud) == 0 else min(n, int(loud[-1]) + 257)
    res = dict(left=l[:end].astype(np.int16), right=r[:end].astype(np.int16), sampleRate=SR,
               clipped=clipped, spuRamBytes=spu_ram, layerEnvs=layer_envs)
    if detail:
        res['adpcmSnrDb'] = 10.0 * math.log10(snr_num / snr_den) if snr_den > 0 else 99.0
    return res


def zs_interleave(r):
    out = np.empty(len(r['left']) * 2, dtype='<i2')
    out[0::2] = r['left']
    out[1::2] = r['right']
    return out


def zs_checksum(r):
    """CRC-32 of the interleaved little-endian int16 stream."""
    return zlib.crc32(zs_interleave(r).tobytes()) & M32


def zs_correlation(r):
    """Stereo correlation, +1 mono .. -1 fully phase-inverted. Reporting only."""
    a, b = r['left'].astype(np.float64), r['right'].astype(np.float64)
    den = math.sqrt(float(np.sum(a * a)) * float(np.sum(b * b)))
    return float(np.sum(a * b)) / den if den > 0 else 1.0


def zs_write_wav(path, r):
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(r['sampleRate'])
        w.writeframes(zs_interleave(r).tobytes())


def zb_verify_all(world_seed=DEFAULT_SEED):
    """Render every named sound twice; report determinism and levels."""
    res = {}
    for name in ZB_SOUNDS:
        a = zs_render(zb_derive_params(name, world_seed), detail=True)
        _adsr_cache.clear()
        b = zs_render(zb_derive_params(name, world_seed))
        ca, cb = zs_checksum(a), zs_checksum(b)
        peak = int(max(np.max(np.abs(a['left'].astype(np.int32))), np.max(np.abs(a['right'].astype(np.int32)))))
        res[name] = dict(crc='%08X' % ca, deterministic=ca == cb, seconds=len(a['left']) / SR, peak=peak,
                         clipped=a['clipped'], spuRamBytes=a['spuRamBytes'], adpcmSnrDb=a['adpcmSnrDb'],
                         correlation=zs_correlation(a), voices=len(a['layerEnvs']))
    return res


# ---------------------------------------------------------------------------
# 10. COMMAND LINE
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='Zero Sounds 4 - PlayStation 2 style deterministic SFX synthesis')
    ap.add_argument('--seed', default=hex(DEFAULT_SEED), help='world seed (hex or decimal), default 0x1337BEEF')
    ap.add_argument('--out', default='out', help='output directory for WAV files')
    ap.add_argument('--list', action='store_true', help='list named sounds')
    ap.add_argument('--sound', action='append', default=[], help='render a named sound (repeatable)')
    ap.add_argument('--all', action='store_true', help='render every named sound')
    ap.add_argument('--random', type=int, action='append', default=[], metavar='N', help='render unnamed patch N (repeatable)')
    ap.add_argument('--params', metavar='NAME', help='print the derived patch as JSON')
    ap.add_argument('--verify', action='store_true', help='render all sounds twice and report determinism and levels')
    ap.add_argument('--checksums', action='store_true', help='print {sound: CRC32} JSON for the seed (compare with the JS engine)')
    ap.add_argument('--checksums-random', type=int, metavar='COUNT', help='print CRC32 JSON for unnamed patches 0..COUNT-1')
    args = ap.parse_args(argv)
    seed = int(args.seed, 0) & M32

    if args.list:
        for c in CATEGORY_ORDER:
            print('%-12s %s' % (c, ', '.join(k for k, v in ZB_SOUNDS.items() if v['category'] == c)))
        return 0
    if args.params:
        print(json.dumps(zb_derive_params(args.params, seed), indent=2))
        return 0
    if args.checksums:
        print(json.dumps({k: '%08X' % zs_checksum(zs_render(zb_derive_params(k, seed))) for k in ZB_SOUNDS}, indent=1))
        return 0
    if args.checksums_random is not None:
        print(json.dumps({'patch#%d' % k: '%08X' % zs_checksum(zs_render(zb_randomize(k, seed)))
                          for k in range(args.checksums_random)}, indent=1))
        return 0
    if args.verify:
        res = zb_verify_all(seed)
        ok = True
        for k, v in res.items():
            good = v['deterministic'] and v['peak'] > 500 and v['clipped'] == 0
            ok = ok and good
            print('%-11s %s crc=%s %5.2fs peak=%5d clip=%d voices=%d ram=%6dB adpcm-snr=%5.1fdB corr=%+.2f' % (
                k, 'ok  ' if good else 'FAIL', v['crc'], v['seconds'], v['peak'], v['clipped'], v['voices'],
                v['spuRamBytes'], v['adpcmSnrDb'], v['correlation']))
        print('seed 0x%08X: %d sounds, %s' % (seed, len(res), 'ALL PASS' if ok else 'FAILURES'))
        return 0 if ok else 1

    jobs = [(n, zb_derive_params(n, seed)) for n in (list(ZB_SOUNDS) if args.all else args.sound)]
    jobs += [('patch%d' % n, zb_randomize(n, seed)) for n in args.random]
    if not jobs:
        ap.print_help()
        return 0
    os.makedirs(args.out, exist_ok=True)
    for name, patch in jobs:
        r = zs_render(patch)
        path = os.path.join(args.out, '%s_%08X.wav' % (name, seed))
        zs_write_wav(path, r)
        print('%-28s %5.2fs crc=%08X' % (path, len(r['left']) / SR, zs_checksum(r)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
