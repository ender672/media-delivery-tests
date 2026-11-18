# Firefox media playback under bad networks

Plays short test videos in a real headless Firefox while a local server
controls how the bytes arrive, then checks what was actually presented:
skipped or frozen video, stalls, wrong frames, bad seeks, slow startup.

Needs Python 3 (no packages), `ffmpeg` and `ffprobe` with libx264, libvpx-vp9
and libopus, and a Firefox binary. Linux only so far.

## Run it

```sh
python suite.py list                                         # the cases
python suite.py run --firefox /usr/bin/firefox --name base   # ~4 min
python suite.py run --firefox ~/obj/dist/bin/firefox --name patch --against base
```

Each case plays a 10 s generated MP4 (H.264 + AAC) and WebM (VP9 + Opus)
clip. `--only dip seek` runs only matching cases, `--clips mp4 webm bbb`
adds a Big Buck Bunny clip, and `--pref NAME=VALUE` sets Firefox prefs.
Cases with findings are rerun once. `2/2` means a finding appeared both times, and
`flaky` means it appeared only once. `--against` shows the other run's row
under each case as `was`. Compare runs made on the same machine.

Each case's files go in `results/NAME/CASE.CLIP/`, such as
`results/base/dip.mp4/`, and a rerun's in the same name with `-r1` added.
The cases, and what each allows, are the `CASES` table in `suite.py`.

## Findings

| Finding | Meaning |
|---|---|
| `skip` | Presented video jumped forward by more than 0.5 s |
| `frozen_video` | Picture unchanged for 1 s while playback advanced 0.5 s or more |
| `silent_stall` | `currentTime` stopped for 1 s or more with no `waiting` event |
| `missing_frames` | 10% fewer distinct frames shown than at full speed |
| `wrong_frame` | A frame's barcode didn't match its reported time |
| `seek_*` | A seek landed in the wrong place, or never completed |
| `not_ended` | Error, timeout, or the first frame never loaded |
| `crash` | Minidumps were left in the profile |
| others | Over a budget the case sets (waits, startup, seek time, dropped frames) |

## Look at one case

A case's results folder holds its spec, the verdict with metrics, the page's
raw record, and the server and Firefox logs.

```sh
python delivery.py results/base/dip.mp4    # what the server sent, and the lead over time
python verdict.py results/base/dip.mp4     # judge the saved record again
python run_case.py --firefox ... --out runs/a --case results/base/dip.mp4/spec.json
python run_case.py --firefox ... --out runs/b --case results/base/dip.mp4/spec.json \
    --moz-log MediaDecoder:4,MediaFormatReader:4
```

`run_case.py` reruns the case alone, with its output in `--out`.
`python run_case.py --help` lists the options for building a case by hand.

## Files

| File | Does |
|---|---|
| `suite.py` | The cases, a parallel runner and the report |
| `run_case.py` | One case: serve, launch Firefox on a fresh profile, judge |
| `oracle.html` | The page: plays, runs actions, records every presented frame |
| `verdict.py` | Findings and metrics from that record |
| `range_server.py`, `schedule.py` | The shaped HTTP server and deliveries timed from `play()` |
| `delivery.py` | What was really sent |
| `make_clip.py` | Clips with the frame number as a barcode, cached in `clips/` |

The code is under the Mozilla Public License 2.0 (`LICENSE`).
`media/bbb-stutter.mp4` is the first 20 s of Big Buck Bunny, (c) 2008 Blender
Foundation, CC BY 3.0 (see `media/README.md`).
