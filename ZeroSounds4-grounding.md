# Zero Sounds 4 JSX (PlayStation 2 style SFX synthesis): grounding

This document is the design reference for Zero Sounds 4. It explains why PlayStation 2 era sound effects sound the way they do, defines every model and formula the engine uses, and describes how to build it in Python so that a JavaScript port produces the same bytes.

It is written to be read without the code. `ZeroSounds4.py` is the reference implementation of everything here, and `ZeroSounds4.jsx` is the port.

Two words are used throughout. A **patch** is the full set of derived parameters for one sound. A **voice** is one playback channel of the sound chip; a sound is three or four voices.

---

## 1. From PlayStation to PlayStation 2: what changes

Zero Sounds 2 modelled the first PlayStation's sound chip, the SPU. The PlayStation 2 (released in 2000) carries its successor, the SPU2. The SPU2 is the same idea made bigger rather than a new design, and that is exactly why its sound is recognisable: the grain of the old chip, with far more room.

| | PlayStation (SPU) | PlayStation 2 (SPU2) | Effect on the sound |
|---|---|---|---|
| Voices | 24 | 48, as two cores of 24 | Sounds are layered more deeply: a body, a sub, a tail, an ambience |
| Sample RAM | 512 KB | 2 MB | Samples are longer and brighter; whole filter sweeps are stored as samples |
| Native rate | 44.1 kHz | 48 kHz | A little more air; pitch step `0x1000` now means 48 kHz |
| Sample format | 4-bit ADPCM, 28 samples in 16 bytes | The same | The grain survives |
| Interpolation | 4-point Gaussian | The same | The soft top end survives |
| Envelope | ADSR registers | The same layout | The exponential tails survive |
| Effects | One reverb unit | One effect unit per core | Reverb and a second effect (here, echo) at once |
| Output | Stereo | Stereo, plus digital out | Games encode matrix surround into the stereo pair |

What changed more than the chip was how it was used. Four things define the era's aesthetic, and the engine models each:

1. **Deep layering.** With 48 voices, one event could spend four or more of them. An explosion is a crack, a body, a sub-bass drop and a debris tail, each its own voice.
2. **Baked sweeps.** The SPU2 has no filter. Sound designers swept filters in their studio tools and stored the result, because 2 MB could hold it. A PS2 whoosh is a recording of a filter moving, played back, not a filter running on the console.
3. **Workstation timbres.** Interface and pickup sounds came from the sample-based keyboards of the day: glassy FM bells, wide detuned-saw ensembles, breathy digital pads.
4. **Surround by phase.** SPU2 voice volumes are signed. A voice played with opposite sign on left and right is out of phase between the speakers, and a matrix decoder (Dolby Pro Logic II was the common one) steers out-of-phase content to the rear speakers. On plain stereo the same voice sounds wide and placeless.

Zero Sounds 4 keeps the Zero Sounds 2 voice path and changes what surrounds it: 48 kHz, cleaner and longer source samples, new source types, three or four voices per sound, a second effect core and phase-inverted rear voices.

**Accuracy note.** The ADPCM format, the pitch counter, the ADSR formula, signed volumes and the reverb's structure follow the hardware. These parts are approximations chosen for this project: the Gaussian table is an analytic fit, not the console's ROM table; the reverb delay lengths and gains are not Sony's preset values; the echo is a plain cross-feedback delay standing in for the second core's effect unit; and the rear placement is simple phase inversion, without the 90-degree phase shifts a real Pro Logic II encoder applies. This is a style synthesiser, not an emulator.

---

## 2. The signal chain

```
worldSeed
   |  hash
category seed -> sound seed -> layer seed -> parameter slots          (section 3)
   |
   v
PATCH: 3 or 4 layers, reverb, echo                                      (section 4.3)
   |
   |  per layer
   v
source sample   FM tone | wavetable | ensemble loop | filtered noise | pitch-drop sine   (4.4)
   v
SPU-ADPCM encode, then decode                                           (4.5)
   v
pitch counter (4.12 fixed point, 0x1000 = 48 kHz) + 4-point Gaussian    (4.6)
   v
ADSR envelope                                                           (4.7)
   v
signed volume L, R   (negative R = rear)                                (4.8)
   |
   +--> dry bus
   +--> core 0 send: reverb at 24 kHz                                   (4.9)
   +--> core 1 send: cross-feedback echo                                (4.10)
   v
sum, master volume, round, clamp to 16 bits, trim silence               (4.11)
   v
48 kHz stereo PCM
```

The sound is rendered offline in one pass and then played from a buffer. Nothing runs in real time, so the cost of a render does not affect playback.

---

## 3. The ZeroBytes contract

Zero Sounds 4 follows the same rules as the rest of the ZeroBytes family.

1. **Position is seed.** Every parameter is `hash(position, parentSeed)`. Nothing is stored except names.
2. **No random numbers.** There is no `Math.random()` and no `random` module. Asking for the same sound twice gives the same sound.
3. **O(1) access.** Any sound can be derived without deriving any other.
4. **Hierarchy.** `worldSeed -> category -> sound -> layer -> slot`. Changing the world seed changes everything; sounds in one category stay related because they share a category seed and category ranges.
5. **Byte-identical audio.** The same seed gives the same 16-bit PCM in every implementation. Section 5 explains how.

### Hash core

Unchanged from Zero Sounds 1 and 2, so seeds mean the same thing across the family.

```
zbHash(key, seed):                       # u32 arithmetic throughout
    h = seed XOR 0x9e3779b9
    numeric key:
        h ^= key
        h = h * 0xcc9e2d51
        h = rotate_left(h, 15)
        h = h * 0x1b873593
        h ^= h >> 16;  h = h * 0x85ebca6b
        h ^= h >> 13;  h = h * 0xc2b2ae35
        h ^= h >> 16
    string key, per character c:
        h ^= c;  h = h * 0x5bd1e995;  h ^= h >> 15

zbFloat(h)         = (h AND 0xFFFFFF) / 0x1000000        # [0, 1)
zbRange(h, lo, hi) = lo + zbFloat(h) * (hi - lo)
zbInt(h, lo, hi)   = lo + h mod (hi - lo + 1)
zbPick(h, list)    = list[h mod len(list)]
```

Seeds are chained like this:

```
catSeed   = zbHash(category.index, worldSeed)
soundSeed = zbHash(styleIndex, catSeed)
hs(k)     = zbHash(100 + k, soundSeed)        # sound-level slots
layerSeed = zbHash(layerNumber + 1, soundSeed)
h(k)      = zbHash(k, layerSeed)              # layer-level slots
```

---

## 4. Models

### 4.1 Taxonomy (the only stored data)

48 named sounds, six in each of eight categories. A name maps to four fields: `category`, `arch` (archetype), `styleIndex` and `dir` (pitch direction: `1` rising, `-1` falling, `0` hashed). No sound parameters are stored.

| Category | Sounds | Root pitch | Hold | Reverb depth | Echo depth | Rooms |
|---|---|---|---|---|---|---|
| ui | cursor, confirm, cancel, pause, error, save | 500 to 1400 Hz | 0.04 to 0.16 s | 0.18 to 0.45 | 0.10 to 0.30 | room, studio, hall |
| pickup | orb, item, key, health, ammo, gem | 700 to 2000 Hz | 0.08 to 0.30 s | 0.25 to 0.60 | 0.15 to 0.40 | studio, hall |
| movement | jump, land, dash, footstep, grind, glide | 90 to 420 Hz | 0.06 to 0.24 s | 0.06 to 0.25 | 0 to 0.12 | room, studio |
| combat | slash, gunshot, punch, laser, magic, block | 110 to 900 Hz | 0.06 to 0.35 s | 0.12 to 0.40 | 0 to 0.20 | room, studio, hall |
| impact | explosion, crash, thud, glassBreak, metalHit, quake | 45 to 200 Hz | 0.18 to 0.70 s | 0.22 to 0.55 | 0 to 0.15 | studio, hall |
| power | powerUp, levelUp, heal, charge, checkpoint, shield | 260 to 1100 Hz | 0.35 to 1.00 s | 0.30 to 0.65 | 0.20 to 0.45 | hall, space |
| damage | hurt, death, alarm, poison, stun, lowHealth | 120 to 520 Hz | 0.10 to 0.60 s | 0.12 to 0.40 | 0.05 to 0.25 | room, studio |
| environment | door, chest, splash, warp, switch, wind | 90 to 700 Hz | 0.10 to 0.60 s | 0.25 to 0.60 | 0.10 to 0.35 | hall, space, studio |

"Hold" is the time from key-on to key-off. The sound continues after it through the release and the effect tails.

### 4.2 Archetypes: how voices are layered

Seven archetypes. "Rear" marks the voice that is phase-inverted (section 4.8). "Echo" marks voices sent to core 1.

| Archetype | Voice 1 | Voice 2 | Voice 3 | Voice 4 |
|---|---|---|---|---|
| `glass` | FM body (echo) | FM shimmer, detuned harmonic (echo) | High-pass noise "air" (rear) | |
| `pad` | Ensemble body, slow attack (echo) | Bright wavetable an octave up, percussive (rear) | Wavetable an octave down | |
| `arp` | Body, re-keyed 2 to 5 notes (echo) | Octave voice, other source type (echo) | Ensemble bed under the notes (rear) | |
| `whoosh` | Band-pass noise sweep | Second noise sweep, slower playback (rear) | Ensemble tone gliding with it (echo) | |
| `impact` | Low-pass noise crack | Pitch-drop body | Sub-bass drop, kept dry | Looped debris noise (rear) |
| `sweep` | Silent modulator | Pitch-modulated carrier (echo) | Chorus copy | Ensemble an octave down (rear) |
| `metal` | FM partial 1 | FM partial 2, inharmonic | FM partial 3, inharmonic (rear) | High-pass noise strike |

Three ideas recur:

- **The fake filter.** The `pad` has no filter. Its bright voice uses a percussive envelope and its dark voices sustain, so the tone darkens over time as it would with a closing filter. This is how sample-based instruments on filterless hardware did it.
- **The baked sweep.** In `whoosh`, `impact`, `glass` and `metal`, the filter movement is inside the noise sample (section 4.4).
- **The dry sub.** The `impact` sub voice bypasses the reverb, which keeps the low end tight. An impact whose hold is 0.3 s or longer is "big": its sub is louder and longer, and its body is pitched down.

`metal` builds a struck-metal tone from three FM voices at fixed inharmonic frequency ratios, one of `[1, 2.76, 5.4]`, `[1, 2.32, 4.25]`, `[1, 1.51, 2.66]`, `[1, 3.01, 5.2]`, `[1, 2.0, 4.9]`.

### 4.3 The patch (output of derivation)

```
{ name, category, archetype, direction, worldSeed, soundSeed, baseFreq, hold,
  reverb: { preset, depth },
  echo:   { time, feedback, depth },
  layers: [{
     role, source, freq, step0,
     sweepOct, sweepTime, vibDepth, vibRate,
     pmon, mute, pmDepth,
     adsr: { ar, am, dr, sl, sm, sd, sr, rm, rr },
     hold, volL, volR, reverb, echo, surround,
     notes: [{ t, ratio }] }] }
```

Sound-level slots, `hs(k)`:

| Slot | Meaning | Range |
|---|---|---|
| 0 | `baseFreq` | category range |
| 1 | `hold` | category range |
| 2, 3 | reverb preset, depth | category rooms, range |
| 4, 5, 6 | arpeggio note count, pattern, gap | 2 to 5 notes, 8 patterns, 45 to 110 ms |
| 7 | direction when `dir` is 0 | +1 or -1 |
| 8 | arpeggio body source | FM or wavetable |
| 9 | pan | -0.5 to 0.5 |
| 10 to 19 | archetype-specific | see the code |
| 20, 21, 22 | echo time, feedback, depth | 90 to 300 ms, 0.2 to 0.5, category range |

Layer-level slots, `h(k)`: 10 to 16 describe the source, 20 to 26 the ADSR registers, 30 the vibrato rate.

Rules applied during derivation:

- **Pitch ceiling.** A voice's highest pitch is `freq * maxNoteRatio * 2^(max(0, sweepOct) + vibDepth)`. `freq` is lowered if needed so that the pitch step never exceeds 3.5 with the shortest cycle (28 samples).
- **Cycle choice.** A tonal source picks its samples-per-cycle from `[56, 112, 224]`, keeping only the values whose step stays at or below 3.5. These are twice the Zero Sounds 2 set, which halves the playback step for the same note and so reduces aliasing and grain.
- **Step.** `step0 = freq * cycle / 48000`. Noise and pitch-drop voices set `step0` directly.
- **Echo off.** If no layer is sent to the echo, `echo.depth` is set to 0.

Patches are plain data and can be edited before rendering.

### 4.4 Source samples: the virtual sample RAM

Each layer computes a short PCM sample. Every sample length is a multiple of 28 so it fills whole ADPCM blocks. A sample either loops from `loopStart` to its end or is a one-shot.

**FM tone** (`fm`). A two-operator FM tone with a second, inharmonic modulator that exists only during the attack. `P` is the cycle length, `nAtt = attackLoops * 2P`, total `N = nAtt + 2P`, loop over the last `2P`.

```
t(n)    = 1 - n / nAtt   for n < nAtt, else 0
idx(n)  = indexLoop + (index0 - indexLoop) * t^2
idx2(n) = index2 * t
ph(n)   = 2 pi n / P
s(n)    = round(26000 * sin(ph + idx * sin(ratio * ph) + idx2 * sin(ratio2 * ph)))
```

`ratio` comes from `[1, 2, 3, 3.5, 4, 5, 5.5, 7, 0.5, 1.5]`, all of which repeat exactly over two cycles, so the loop is seamless. `ratio2` comes from `[1.41, 2.76, 3.3, 4.07, 5.4, 7.13]`; because `idx2` reaches zero before the loop, it colours the strike without breaking the loop. `index2` ranges up to 0.8 by default, 1.2 for `glass` and 4.0 for `metal`.

**Wavetable** (`wave`). Two cycles of an additive waveform, looped.

```
y(n) = sum over k = 1..hmax of  a(k) * sin(k * 2 pi n / P)
hmax = min(harmonics, P/2 - 1)          harmonics from [8, 16, 32, 48]
s(n) = round(26000 * y(n) / max|y|)
```

Each amplitude is multiplied by a hashed jitter of 0.85 to 1.15. Shapes:

| Shape | a(k) |
|---|---|
| saw | 1/k |
| pulse | sin(pi k duty) / k |
| tri | odd k only, alternating sign, 1/k^2 |
| organ | hashed 0.2 to 1.0 for k in {1, 2, 3, 4, 6, 8} |
| glass | 1/k for k in {1, 3, 4, 7, 9, 13} |
| digi | (0.15 + r^2) / k with r hashed per harmonic |

**Ensemble loop** (`super`). The detuned-saw ensemble of the era, stored as one long loop. This is the source type that most needs the 2 MB: at 3584 or 7168 samples it would not have been spent on the first PlayStation.

The loop is `N = C * P` samples with `C = 64`. It holds three band-limited saws that complete `C - 1`, `C` and `C + 1` cycles. All three therefore repeat exactly at the loop point, while being detuned from each other by 1/64 (about 27 cents).

```
for j in 0..2:   amp_j = 0.7 + 0.3 * hash,  phi_j = 2 pi * hash,  w_j = 2 pi (C + j - 1) / N
y(n) = sum over j, k = 1..hmax of  (amp_j / k) * sin(k * (w_j * n + phi_j))
s(n) = round(26000 * y(n) / max|y|)
```

`P` comes from `[56, 112]` and harmonics from `[8, 12, 16]`. The beat rate is the played frequency divided by 64.

**Filtered noise** (`noise`). White noise through a state-variable filter whose cutoff moves along the sample: the baked sweep. White noise is `w(n) = 2 * zbFloat(zbHash(n, seed)) - 1`, so each value is a function of its index.

```
fc(n) = clamp(f0 * 2^(oct * n / N), 40, 7500)       Hz
f(n)  = 2 * sin(pi * fc / 48000)
low   = low + f * band
high  = w - low - q * band
band  = band + f * high
y(n)  = low | band | high        by mode lp | bp | hp
```

This is the Chamberlin form. `q` is damping (lower rings more), and the 7500 Hz ceiling keeps it stable. The output is normalised to a peak of 24000.

A one-shot is `floor(dur * 48000 / 28) + 1` blocks (40 to 2400) and is faded linearly to zero over its last 15% so that it ends in silence. A looped noise sample is 584 blocks with a fixed cutoff.

**Pitch-drop sine** (`thump`). A sine falling from `f1` to `f2` with a cubic curve, one-shot. With `c = f / 48000`, `t = 1 - n/N`:

```
freq(n)  = f2 + (f1 - f2) * t^3
phase(n) = 2 pi * (c2 * n + (c1 - c2) * N/4 * (1 - t^4))      # the exact integral
s(n)     = round(28000 * sin(phase) * t^2)
```

The body voice uses 150 to 340 Hz falling to 40 to 75 Hz over 80 to 200 blocks. The sub voice uses 70 to 120 Hz falling to 26 to 40 Hz over up to 800 blocks.

### 4.5 SPU-ADPCM

Each sample is encoded to the console's ADPCM and decoded again before playback. The round trip adds the quantisation grain that the hardware added. The format is the one the first PlayStation used.

A block is 28 samples in 16 bytes: a header holding a 4-bit shift and a filter number, then 28 signed 4-bit values. Decoding one sample:

```
pred = (prev1 * K0[f] + prev2 * K1[f] + 32) >> 6
d    = clamp16(pred + nibble * 2^(12 - shift))
K0 = [0, 60, 115, 98, 122]      K1 = [0, 0, -52, -55, -60]
```

Encoding one block:

1. **Filter, open loop.** For each of the five filters, predict every sample from the two *original* samples before it and record the largest absolute error. Keep the filter with the smallest (ties go to the lower number).
2. **Shift.** Let `q` be the smallest value in 0..12 with `7 * 2^q >=` that error. Candidates are `q` and `q + 1`.
3. **Closed loop.** For each candidate, encode the block predicting from *decoded* history, with `nibble = clamp(floor((2 * (x - pred) + step) / (2 * step)), -8, 7)` and `step = 2^q`. Keep the candidate with the smaller squared error.

Zero Sounds 2 searched all five filters closed-loop. The open-loop choice here is the approach of the classic encoders for this format and is several times cheaper, which matters now that samples are longer. Measured per sound at the default seed, the signal-to-noise ratio of the round trip runs from about 19 dB for noise-heavy sounds to 42 dB for tonal ones.

Sample RAM used is 16 bytes per block. The inspector reports the total per sound.

### 4.6 Pitch counter and Gaussian interpolation

Each voice has a pitch counter in 4.12 fixed point. A step of `0x1000` advances one source sample per output sample, which on the SPU2 means 48 kHz. The step is recomputed every output sample:

```
u    = min(1, i / sweepSamples);  u = u * (2 - u)             # ease-out
ex   = sweepOct * u + vibDepth * sin(2 pi vibRate i / 48000)
step = step0 * noteRatio * 2^ex
step = step * (32768 + mod[i]) / 32768                        # only if pitch-modulated
si   = clamp(round(step * 4096), 1, 0x3FFF)
```

Pitch modulation is the hardware's PMON feature: the previous voice's output, scaled by `pmDepth`, bends this voice's pitch. The `sweep` archetype uses a muted modulator voice for this.

The output sample is a 4-point Gaussian blend of the source around the counter position. With `idx = pos >> 12` and `fr = (pos >> 4) & 255`:

```
s = (G[255 - fr] * x[idx - 1] + G[511 - fr] * x[idx]
   + G[256 + fr] * x[idx + 1] + G[fr]       * x[idx + 2]) >> 15
```

`G` is a 512-entry table. The console's table is in ROM; this project uses the analytic fit

```
G[j] = round(32768 * 0.7008 * exp(-x^2 / (2 * 0.569^2))),   x = (511 - j) / 256
```

Gaussian interpolation never overshoots and rolls off the top octave. That soft top end is a large part of the family sound of both consoles.

Reading past the end of a looped sample wraps to `loopStart`; reading past the end of a one-shot returns zero.

### 4.7 ADSR

The envelope is the SPU register model, stepped once per output sample at 48 kHz. The level runs from 0 to `0x7FFF`.

| Register | Range | Meaning |
|---|---|---|
| `ar` | 0 to 127 | attack rate: shift `ar >> 2`, step `7 - (ar & 3)` |
| `am` | 0, 1 | attack mode: linear or exponential |
| `dr` | 0 to 15 | decay shift, always exponential, step -8 |
| `sl` | 0 to 15 | sustain level `(sl + 1) * 0x800` |
| `sr` | 0 to 127 | sustain rate: shift `sr >> 2`, step `-8 + (sr & 3)` |
| `sm`, `sd` | fixed 1, 1 | sustain is an exponential decrease |
| `rm` | 0, 1 | release mode: linear or exponential |
| `rr` | 0 to 31 | release shift, step -8 |

Each tick, for the current phase's `shift`, `step` and mode:

```
cycles = 2^max(0, shift - 11)
st     = step * 2^max(0, 11 - shift)
if exponential and increasing and level > 0x6000:  cycles *= 4
if exponential and decreasing:                     st = floor(st * level / 32768)
every `cycles` ticks:  level = clamp(level + st, 0, 0x7FFF)
```

Attack ends at `0x7FFF`, decay ends at the sustain level, key-off starts release, and the voice ends when the release reaches zero (capped at three seconds).

As a guide at 48 kHz: an exponential decay with shift 11 has a time constant of about 85 ms, and each step up in shift doubles it. A linear attack with shift 11 takes about 0.1 s.

Five register families are used:

| Family | ar | dr | sl | sr | rr | Used for |
|---|---|---|---|---|---|---|
| `perc` | 0 to 12 | 11 to 14 | 0 to 6 | 44 to 58 | 11 to 14 | bells, arpeggio notes, thumps |
| `sus` | 8 to 36 | 9 to 12 | 8 to 15 | 52 to 66 | 8 to 12 | sweep carriers |
| `pad` | 36 to 50 | 11 to 13 | 9 to 15 | 56 to 70 | 12 to 14 | ensembles, slow attack and long release |
| `noise` | 0 to 30 | 9 to 13 | 2 to 12 | 42 to 56 | 10 to 13 | whooshes, air, debris |
| `hit` | 0 to 4 | 8 to 11 | 0 to 3 | 40 to 50 | 9 to 12 | cracks and strikes |

### 4.8 Volume and rear placement

A layer has a volume `v = round(vol * 0x4000)` and is placed in one of two ways.

```
front:  volL = round(v * min(1, 1 - pan)),   volR = round(v * min(1, 1 + pan))
rear:   volL = round(v * 0.75),              volR = -round(v * 0.75)
```

Mixing multiplies the voice by each volume: `l = floor(x * volL / 16384)`, `r = floor(x * volR / 16384)`. A rear voice is therefore inverted in the right channel.

What that does depends on the listener's equipment:

- **Matrix surround decoder:** the voice comes from the rear speakers.
- **Stereo speakers or headphones:** the voice sounds wide and hard to place.
- **Mono:** the voice cancels. Only supporting layers are placed in the rear for this reason, so every sound keeps its body in mono.

The stereo correlation of a render measures how much rear content it has: +1 is mono, 0 is unrelated channels, -1 is fully inverted. Named sounds at the default seed range from about +0.3 to +0.99.

### 4.9 Core 0: reverb

The SPU reverb structure, run at half the output rate (24 kHz) as the hardware does. The send is averaged in pairs on the way in and linearly interpolated on the way out.

```
input x (gain 1 - wall)
four wall buffers: same-side L, same-side R, cross L, cross R
   z = z + (x + buffer_out * wall - z) * iir          # reflection with a one-pole low-pass
   cross buffers read from the opposite channel's buffer
output = 0.40 * tap1 + 0.35 * tap2   (same-side buffer, at 0.93 and 0.71 of its length)
       + 0.35 * tap3 + 0.30 * tap4   (cross buffer, at 0.83 and 0.59 of its length)
then two all-pass filters in series, gains 0.6 and 0.5
```

Base buffer lengths at 24 kHz are 3847, 4253, 3163 and 3613 samples for the walls and 667 and 389 for the all-pass filters, each multiplied by the preset's scale.

| Preset | Scale | Wall | IIR | Tail rendered |
|---|---|---|---|---|
| room | 0.30 | 0.55 | 0.65 | 0.5 s |
| studio | 0.50 | 0.62 | 0.60 | 0.8 s |
| hall | 1.00 | 0.72 | 0.50 | 1.6 s |
| space | 1.70 | 0.80 | 0.40 | 2.4 s |

These are parametric values chosen for this project, not Sony's preset tables.

### 4.10 Core 1: echo

A cross-feedback delay, the "ping-pong" echo heard on menu and pickup sounds of the era. With `D = floor(time * 48000)`:

```
xL[i] = inL[i] + feedback * xR[i - D]
xR[i] = inR[i] + feedback * xL[i - D]
outL[i] = xL[i - D],   outR[i] = xR[i - D]
```

Each repeat swaps sides. Only the voices marked "echo" in section 4.2 are sent to it. The rendered tail is `min(2.5 s, 6 * time)`.

### 4.11 Mix and output

```
out = round((dry + reverb * reverb.depth + echo * echo.depth) * 0.80)
```

The 0.80 is the master volume. It leaves headroom for four-voice impacts; no sound clips at the seeds tested. The result is clamped to 16 bits. Trailing samples are removed after the last one louder than 24 (about -63 dB), keeping 256 more.

---

## 5. Deterministic math

Rule 5 of the contract is the hard one. JavaScript and Python both use IEEE 754 doubles, and `+ - * /` and `floor` give identical results on every conforming machine. `sin`, `exp` and `pow` do not: the standard lets implementations round them differently. So the signal path never calls them.

```
dsin(x):
    r = x - floor(x / 2pi + 0.5) * 2pi           # to [-pi, pi]
    if r >  pi/2:  r =  pi - r                   # fold to [-pi/2, pi/2]
    if r < -pi/2:  r = -pi - r
    return r * (1 + r^2 * P(r^2))                # Horner, 7 terms: -1/3!, 1/5!, ... -1/15!

dexp2(x):                                        # 2^x
    n = floor(x);  y = (x - n) * ln 2
    e = 1 + y * (1 + y * Q(y))                   # Horner, 12 terms: 1/2!, ... 1/13!
    return e * 2^n                               # exact doubling or halving, n times
```

Three more rules complete it:

- **Fixed order.** Floating-point addition is not associative, so every formula in this document is evaluated in one written order, and both engines use that order. Sums over arrays are plain left-to-right loops.
- **Integers where the hardware used integers.** The codec, the Gaussian mix and the ADSR are integer arithmetic. Intermediate products stay below 2^31, so JavaScript's 32-bit shift operators and Python's unbounded integers agree.
- **Rounding.** `round(x)` always means `floor(x + 0.5)`.

Functions that only report (stereo correlation, the signal-to-noise figure) may use the platform's `sqrt` and `log`, because they never feed the audio.

---

## 6. Python implementation blueprint

`ZeroSounds4.py` is one file with NumPy as its only dependency. Its sections follow this document:

| Section | Functions |
|---|---|
| Hash core | `zb_hash`, `zb_hash_vec`, `zb_float`, `zb_range`, `zb_int`, `zb_pick` |
| Deterministic math | `dsin`, `dexp2` (scalar), `fdsin`, `fdexp2` (array), `rnd`, `frnd` |
| Taxonomy | `ZB_CATEGORIES`, `ZB_SOUNDS`, `ENV`, `REVERBS` |
| Derivation | `zb_derive_params`, `zb_randomize` |
| Sources | `gen_source` |
| Codec | `adpcm_codec` |
| Voice | `adsr_run`, `render_voice` |
| Effects | `spu_reverb`, `spu_echo` |
| Render | `zs_render`, `zs_checksum`, `zs_correlation`, `zs_write_wav`, `zb_verify_all` |
| CLI | `main` |

How NumPy is used without breaking rule 5:

- **Element-wise array arithmetic is safe.** `a * b + c` on float64 arrays performs the same IEEE operation on each element that a scalar loop would. `fdsin` and `fdexp2` are the scalar functions written with array operations in the same order. `np.ldexp` replaces the repeated doubling in `dexp2`, which is exact.
- **Reductions are not safe.** `np.sum` adds in a different order from a loop. It is used only for reporting. `np.max` and `np.cumsum` on integers are exact and are used.
- **`np.sin`, `np.exp`, `np.power` are never called.**

What is vectorised and what is not:

| Stage | Method |
|---|---|
| Source synthesis, noise hash | Array operations over the sample index |
| Noise filter | Python loop (each sample depends on the last) |
| Codec, filter choice | Array operations over the whole sample, then a maximum per block |
| Codec, closed loop | Python loop, two candidates of 28 samples per block |
| ADSR | Python loop, cached by registers and key-off time |
| Pitch counter and Gaussian mix | Array operations; positions are an integer cumulative sum |
| Reverb | Python loop over 24 kHz samples, on lists |
| Echo | Array operations in blocks of `D` samples, since a block only reads the one before it |

The integer cumulative sum is the step that makes the voice vectorisable: the position at sample `i` is the sum of all earlier integer steps, which has no rounding to disagree about.

Command line:

```
python ZeroSounds4.py --list
python ZeroSounds4.py --sound confirm --out out/
python ZeroSounds4.py --all --seed 0xDEADBEEF --out out/
python ZeroSounds4.py --random 7 --out out/
python ZeroSounds4.py --params confirm
python ZeroSounds4.py --verify
python ZeroSounds4.py --checksums
python ZeroSounds4.py --checksums-random 8
```

`--checksums` prints the CRC-32 of each sound's interleaved little-endian PCM. `build-demo.py` writes that table into the JavaScript engine as `ZS_ORACLE`, and the demo's check button compares against it.

---

## 7. Tuning guide

No listening tests have been done on this version. The ranges below are where to start when tuning by ear.

| To change | Edit | Notes |
|---|---|---|
| Overall brightness | `CYCLES`, wavetable `harmonics` list | Longer cycles and more harmonics are cleaner and brighter |
| Amount of console grain | `CYCLES` (shorter is grainier), `MAX_STEP` | A step above about 2 aliases audibly |
| Ensemble width | `SUPER_LOOPS` | Detune is 1/loops: 64 gives about 27 cents, 128 about 13 |
| Whoosh character | `whoosh` slots 10, 11, 14 | Sweep range in octaves, start cutoff, damping |
| Impact weight | `big` threshold, sub `vol`, sub `f1`/`f2` | The sub is the main PS2-versus-PS1 difference in impacts |
| Bell hardness | `strike` values, FM `index0` range | Higher index is brighter and harsher |
| Tail lengths | `ENV` `rr` ranges | Each step doubles the release time |
| Room size | `REVERBS`, category `rooms` | `scale` sets size, `wall` sets decay, `iir` sets damping |
| Echo | slots 20 to 22, category `echoLow`/`echoHigh` | Feedback above 0.6 runs past the rendered tail |
| Rear content | which layers pass `sur` | Keep the main body in front for mono compatibility |
| Loudness | layer `vol` values, `MASTER_VOL` | Re-run `--verify` at several seeds and check `clip=0` |

Any change must be made in both engines, followed by `python build-demo.py`.

---

## 8. Verification

The claim to test is: same seed, same bytes, in both engines.

1. **Self-consistency.** `--verify` renders every named sound twice, compares CRC-32s, and checks that each sound is audible (peak above 500) and does not clip.
2. **Cross-engine.** Render the same sounds in Python and in the JavaScript engine under Node.js and compare CRC-32s. Include unnamed patches and more than one seed, so that every archetype and source type is exercised.
3. **In the browser.** The demo's check button renders all 48 sounds twice and, at seed `0x1337BEEF`, compares them against the Python table.

What these tests do not show: that the sounds are good, or that they match real hardware. Both need ears and, for the second, an SPU2 emulator or a console.

---

## 9. Integration notes

- **Render once, play many.** A render takes tens of milliseconds in JavaScript. Cache renders by `(worldSeed, name)` or pre-render at load.
- **Sample rate.** Renders are 48 kHz. Web Audio resamples on playback if the device runs at another rate; the stored bytes do not change.
- **Surround.** The output is ordinary stereo. Rear placement only takes effect if the player's system applies matrix decoding. Avoid summing the output to mono.
- **Seed tricks.** Because the whole library follows the seed, `zbHash(depth, baseSeed)` gives each level its own palette, and `zbRandomize(item.id, worldSeed)` gives every item its own sound on every client.
- **Porting.** The engine block in `ZeroSounds4.jsx` has no React or DOM dependency and is the boundary for a TypeScript module. Any port must reproduce the Python checksums.
