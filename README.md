# ZeroSounds4-JSX

**Deterministic PlayStation 2 style sound effect synthesis for games and applications.**

Zero Sounds 4 is the next version of [ZeroSounds2-JSX](https://github.com/MushroomFleet/zerosounds2-JSX). That project modelled the first PlayStation's sound chip. This one models the PlayStation 2's chip (the SPU2) and the way games of the early 2000s used it, for a more modern sound: 48 kHz output, three or four layered voices per sound, sub-bass under the impacts, detuned-saw ensembles, filter sweeps baked into the samples, reverb and echo together, and voices placed behind the listener by phase.

It keeps the ZeroBytes method, *position-is-seed, zero bytes store infinity*. Every sound is derived from a hash of `(soundName, worldSeed)`. There is no `Math.random()`, no stored presets and no sample files. One 32-bit integer defines a library of 48 named sounds and an unlimited space of unnamed ones.

As in Zero Sounds 2, the guarantee covers the audio itself. The same seed produces the same 16-bit PCM, byte for byte, in the browser and in Python.

## What is new since Zero Sounds 2

| | Zero Sounds 2 (PlayStation) | Zero Sounds 4 (PlayStation 2) |
|---|---|---|
| Output | 44.1 kHz stereo | 48 kHz stereo, matrix-surround compatible |
| Voices per sound | 2 or 3 | 3 or 4 |
| Named sounds | 40 | 48, six per category |
| Archetypes | bell, synth, arp, noise, hit, sweep | glass, pad, arp, whoosh, impact, sweep, metal |
| Source samples | FM, wavetable, noise loop, pitch-drop sine | The same, plus ensemble loops and filtered noise sweeps; cycles twice as long |
| Effects | Reverb | Reverb (core 0) and ping-pong echo (core 1) |
| Placement | Pan | Pan, plus phase-inverted rear voices |

## How it works

Each sound is three or four hardware-style voices. For every voice the engine:

1. Hashes its way down `worldSeed -> category -> sound -> voice -> parameter`.
2. Computes a source sample directly from the sample index: an FM tone, a wavetable, a loop of three detuned saws, a noise sweep through a filter, or a pitch-drop sine. The SPU2 has no filter, so filter movement is stored in the sample, as it was on the console.
3. Encodes that sample to SPU-ADPCM (28 samples in 16 bytes) and decodes it again, which adds the console's grain.
4. Plays it through a 4.12 fixed-point pitch counter with 4-point Gaussian interpolation and an SPU ADSR envelope.
5. Applies a signed left and right volume. A voice with a negative right volume is out of phase between the speakers; a matrix surround decoder sends it to the rear, and on plain stereo it sounds wide.

The voices are mixed with two effect units, a reverb running at 24 kHz and a cross-feedback echo. The result is rendered offline to a 48 kHz stereo buffer and then played.

To make the bytes reproducible, the signal path avoids the platform's `sin`, `exp` and `pow`, which are allowed to round differently between machines. It uses its own versions built from plain arithmetic. The full design, with the formulas, is in [`ZeroSounds4-grounding.md`](ZeroSounds4-grounding.md).

**Accuracy note.** The ADPCM format, pitch counter, ADSR formula, signed volumes and reverb structure follow the real hardware. These parts are approximations chosen for this project: the Gaussian interpolation table is an analytic fit, not the console's ROM table; the reverb delay lengths and gains are not Sony's preset values; the echo is a simple delay standing in for the second core's effect unit; and rear placement is plain phase inversion, simpler than a real Pro Logic II encoder. This is a style synthesiser, not an emulator.

## Contents

| File | What it is |
|---|---|
| [`ZeroSounds4-grounding.md`](ZeroSounds4-grounding.md) | Design document: why PlayStation 2 effects sound as they do, every model and formula, the Python blueprint, tuning guide, verification method |
| [`ZeroSounds4.py`](ZeroSounds4.py) | Reference engine and command-line tool. Renders WAV files and acts as the oracle the JavaScript engine is checked against |
| [`ZeroSounds4.jsx`](ZeroSounds4.jsx) | React component, plus the same engine in JavaScript and a small Web Audio player |
| [`ZeroSounds4-demo.html`](ZeroSounds4-demo.html) | Standalone interactive demo with the component inlined. No build step |
| [`build-demo.py`](build-demo.py) | Refreshes the checksum table in the `.jsx` from the Python engine and re-inlines the `.jsx` into the demo |
| `README.md` | This file |

## Using the demo

Open `ZeroSounds4-demo.html` in a browser. It works straight from disk; it needs an internet connection to load React, Babel and the fonts from their CDNs.

- **Press a sound** to render and play it. The inspector shows the stereo waveform, a vectorscope, length, sample RAM used, reverb, echo, stereo correlation and a CRC-32 of the audio. Below that, each voice lists its source, pitch, ADSR registers and effect sends, with its envelope drawn.
- **Read the vectorscope** as front against rear: a vertical line is centred sound, and sideways spread is out-of-phase content that a surround decoder would send behind you. Voices tagged `REAR` produce it.
- **Change the world seed** by typing hex, dragging the slider or pressing **Next seed**. All 48 sounds are re-derived.
- **Download WAV** saves exactly what you heard.
- **Unnamed patches** steps through the sounds that have numbers instead of names.
- **Check all 48 sounds** renders everything twice and compares checksums. At seed `0x1337BEEF` it also compares against checksums produced by `ZeroSounds4.py`, so a pass means the browser and Python engines agree bit for bit.

## Using the React component

Copy `ZeroSounds4.jsx` into your project. It has no dependencies beyond React.

```jsx
import ZeroSounds4 from './components/ZeroSounds4';

export default function App() {
  return <ZeroSounds4 initialSeed={0xDEADBEEF} onPlay={(name) => console.log(name)} />;
}
```

| Prop | Type | Default | Meaning |
|---|---|---|---|
| `initialSeed` | number | `0x1337BEEF` | World seed on mount (unsigned 32-bit) |
| `initialVolume` | number | `0.7` | Master volume, 0 to 1 |
| `onPlay` | function | `null` | Called as `(name, patch, render)` after a sound starts |
| `className` | string | `''` | Extra class on the root element |

The component injects its own styles under the `zs4` class prefix. It uses the Oxanium and IBM Plex Mono fonts if the page provides them and falls back to system fonts otherwise.

## Using the engine in a game

The engine functions are exported from the same file and do not need React or the DOM.

```js
import { zbDeriveParams, zbRandomize, zbHash, zsRender, zsEncodeWav, ZS4Engine } from './ZeroSounds4';

const player = new ZS4Engine();          // create after a user gesture
const cache = new Map();

function playSound(name, worldSeed) {
  const key = `${worldSeed}:${name}`;
  if (!cache.has(key)) cache.set(key, zsRender(zbDeriveParams(name, worldSeed)));
  player.play(cache.get(key));
}

playSound('confirm', game.worldSeed);
```

A render took about 35 ms on average under Node.js on the machine used for testing, and longer for long sounds, so cache renders or pre-render at load. Do not render per frame.

The seed tricks from earlier versions still work, because the whole library follows the seed:

```js
// The palette shifts as the player descends
playSound('footstep', zbHash(player.depth, baseSeed));

// Every item has its own sound, identical on every client
player.play(zsRender(zbRandomize(item.id, worldSeed)));
```

Two things to know about the output:

- It is ordinary stereo. Rear placement only takes effect if the player's system applies matrix surround decoding.
- Do not sum it to mono. Rear voices cancel in mono. They are always supporting layers, so the body of each sound survives, but the sound gets thinner.

### API

```js
zbHash(key, seed)                 // u32; numeric or string key (same as ZeroSounds 1 and 2)
zbFloat(h)  zbRange(h, lo, hi)  zbInt(h, lo, hi)

zbDeriveParams(name, worldSeed)   // named sound -> patch
zbRandomize(n, worldSeed)         // unnamed sound number n -> patch
zsRender(patch)                   // -> { left, right: Int16Array, sampleRate: 48000,
                                  //      clipped, spuRamBytes, layerEnvs }
zsChecksum(render)                // CRC-32 of the interleaved PCM
zsCorrelation(render)             // stereo correlation: +1 mono .. -1 fully inverted
zsEncodeWav(render)               // -> Uint8Array, a 16-bit stereo WAV file
zbVerifyOne(name, worldSeed)      // -> { crc, deterministic, oracle, ok }
zbVerifyAll(worldSeed)            // -> { [name]: result }

const player = new ZS4Engine();
player.play(render);              // creates the AudioContext on first use
player.setVolume(0.5);
```

A patch looks like this (see the grounding document for every field):

```js
{ name, category, archetype, direction, worldSeed, soundSeed, baseFreq, hold,
  reverb: { preset, depth },
  echo:   { time, feedback, depth },
  layers: [{ role, source, freq, step0, sweepOct, sweepTime, vibDepth, vibRate,
             pmon, mute, pmDepth, adsr: { ar, am, dr, sl, sm, sd, sr, rm, rr },
             hold, volL, volR, reverb, echo, surround, notes: [{ t, ratio }] }] }
```

Patches are plain data. You can edit one before rendering it, for example to change `reverb.preset` per room or set `echo.depth` to 0.

## Using the Python engine

```bash
pip install numpy

python ZeroSounds4.py --list                              # sound names by category
python ZeroSounds4.py --sound confirm --out out/          # one WAV
python ZeroSounds4.py --sound jump --sound laser --seed 0xDEADBEEF --out out/
python ZeroSounds4.py --all --out out/                    # all 48
python ZeroSounds4.py --random 7 --out out/               # unnamed patch 7
python ZeroSounds4.py --params confirm                    # the derived patch as JSON
python ZeroSounds4.py --verify                            # determinism and level check
python ZeroSounds4.py --checksums                         # CRC-32 per sound, as JSON
python ZeroSounds4.py --checksums-random 8                # CRC-32 for unnamed patches 0 to 7
```

Files are written as `<name>_<SEED>.wav`, 16-bit stereo at 48 kHz, and match the browser's output for the same seed byte for byte.

From Python:

```python
import ZeroSounds4 as zs

patch = zs.zb_derive_params('explosion', 0xDEADBEEF)
render = zs.zs_render(patch)           # dict with 'left' and 'right' int16 arrays
zs.zs_write_wav('explosion.wav', render)
```

`--verify` over all 48 sounds took about 20 seconds on the test machine; the envelope, codec and reverb loops are plain Python.

## Sound library

48 named sounds in 8 categories. Each is a four-field anchor, not a preset.

| Category | Sounds |
|---|---|
| **ui** | cursor, confirm, cancel, pause, error, save |
| **pickup** | orb, item, key, health, ammo, gem |
| **movement** | jump, land, dash, footstep, grind, glide |
| **combat** | slash, gunshot, punch, laser, magic, block |
| **impact** | explosion, crash, thud, glassBreak, metalHit, quake |
| **power** | powerUp, levelUp, heal, charge, checkpoint, shield |
| **damage** | hurt, death, alarm, poison, stun, lowHealth |
| **environment** | door, chest, splash, warp, switch, wind |

Seven archetypes decide how a sound's voices are layered:

| Archetype | Voices |
|---|---|
| `glass` | FM body, detuned FM shimmer, and a breath of high-pass noise in the rear |
| `pad` | Ensemble body with a slow attack, a bright voice that fades (in the rear), and a voice an octave down |
| `arp` | Body and octave voice re-keyed for 2 to 5 notes, over an ensemble bed in the rear |
| `whoosh` | Two filtered noise sweeps, one in the rear, and a gliding ensemble tone |
| `impact` | Noise crack, pitch-drop body, dry sub-bass drop, and looped debris in the rear |
| `sweep` | A silent voice pitch-modulating a swept carrier, a chorus voice, and an ensemble an octave down in the rear |
| `metal` | Three FM voices at inharmonic ratios, one in the rear, and a noise strike |

### Adding a sound

Add one line to `ZB_SOUNDS` in both `ZeroSounds4.py` and `ZeroSounds4.jsx`, using a `styleIndex` not already taken in that category, then run `python build-demo.py`.

```js
stab: { category: 'combat', arch: 'whoosh', styleIndex: 6, dir: -1, label: 'Stab' },
```

`dir` is `1` for rising, `-1` for falling, `0` to let the hash choose.

## Parameters

Both engines use the same names and ranges.

| Parameter | Range | Meaning |
|---|---|---|
| `worldSeed` | 0 to `0xFFFFFFFF` | Defines the whole library |
| `baseFreq` | per category, 45 to 2000 Hz | Root pitch |
| `hold` | per category, 0.04 to 1.0 s | Time until key-off |
| `reverb.preset` | room, studio, hall, space | Reverb size |
| `reverb.depth` | 0.06 to 0.65 | Reverb level |
| `echo.time` | 0.09 to 0.30 s | Delay between repeats |
| `echo.feedback` | 0.20 to 0.50 | How much each repeat feeds the next |
| `echo.depth` | 0 to 0.45 | Echo level; 0 when no voice is sent to it |
| `source.kind` | fm, wave, super, noise, thump | Source sample type |
| `source.cycle` | 56, 112, 224 | Samples per cycle; lower is grainier |
| `step0` | up to 3.5 | Pitch counter step (1.0 plays at 48 kHz) |
| `sweepOct` | -2.5 to 2.5 | Pitch glide in octaves |
| `adsr.ar` / `sr` | 0 to 127 | SPU attack and sustain rate registers |
| `adsr.dr` / `sl` | 0 to 15 | SPU decay shift and sustain level |
| `adsr.rr` | 0 to 31 | SPU release shift |
| `volL`, `volR` | `-0x4000` to `0x4000` | Voice volume per channel; a negative `volR` marks a rear voice |
| `surround` | true, false | Whether the voice is placed in the rear |

## Requirements

| Interface | Needs |
|---|---|
| Demo | A current browser; internet access for the React, Babel and font CDNs |
| React component | React 17 or later (hooks). No other packages |
| Python engine | Python 3.8 or later, NumPy |
| `build-demo.py` | Same as the Python engine |

## Verification

What has been checked:

- 48 named sounds and 8 unnamed patches at three seeds (168 renders) gave identical CRC-32s from the Python engine and from the JavaScript engine under Node.js.
- `--verify` passed at five seeds: every sound reproducible, non-silent and free of clipping.
- The demo was mounted in headless Chrome with local copies of React and Babel: 48 pads rendered, sounds and unnamed patches were pressed without errors, and the in-page check reported all 48 matching the Python checksums.

What has not been checked: no listening tests and no comparison against real hardware or an SPU2 emulator. Nobody has heard these sounds yet. Treat the parameter ranges as a starting point for tuning by ear; the grounding document has a tuning guide.

## Continuing development

The repository is meant to be cloned and developed further, for example with Claude Code. `ZeroSounds4-grounding.md` is the context document, `ZeroSounds4.py` is the behavioural oracle (any port must reproduce its checksums), and the engine block in `ZeroSounds4.jsx` is the natural boundary for a TypeScript module. There is no `ZeroSounds4-TINS.md` implementation plan yet.

## License

No licence file has been added to this repository yet. Add the one you intend before publishing.

## 📚 Citation

### Academic Citation

If you use this codebase in your research or project, please cite:

```bibtex
@software{zerosounds4_jsx,
  title = {ZeroSounds4-JSX: ZeroBytes-Aligned Deterministic PlayStation 2 Style Audio Synthesis},
  author = {Drift Johnson},
  year = {2026},
  url = {https://github.com/MushroomFleet/zerosounds4-JSX},
  version = {1.0.0}
}
```

### Donate

[![Ko-Fi](https://cdn.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/driftjohnson)
