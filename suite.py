#!/usr/bin/env python3
"""A fixed suite of network-delivery edge cases and player-performance checks.

    python suite.py run    --firefox /usr/bin/firefox --name base
    python suite.py run    --firefox ~/obj/dist/bin/firefox --name patch --only seek
    python suite.py report --name patch --against base
    python suite.py list

Every case in CASES runs on every clip in --clips (10 s generated MP4 and
WebM by default). The `full` case of each clip runs first and is the
reference the others' frame coverage is compared with. Cases with findings
are run again (--confirm), so a timing fluke shows as 1/2 rather than a
failure. Results go to results/NAME/."""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import make_clip
import run_case
import schedule

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
# Browsers that start at the same moment slow each other's first seconds,
# which shows up as startup time and dropped frames. Space them out.
STAGGER_S = 1.5


def generated(container, **settings):
    return {"source": "generate", "settings": {
        **make_clip.DEFAULTS, "container": container, "duration": 10.0, "gop": 48,
        **settings}}


# name -> video spec, and how much must be there before play(): Firefox does
# not fire loadeddata on a WebM file until the download has passed its
# second keyframe (2 s here).
CLIPS = {
    "mp4": (generated("mp4"), 0.3),
    "webm": (generated("webm"), 2.5),
    "mp4-noaudio": (generated("mp4", audio=False), 0.3),
    "bbb": ({"source": "media", "name": "bbb-stutter.mp4", "trim": 10}, 0.3),
}
DEFAULT_CLIPS = ["mp4", "webm"]


# A delivery is a function of the clip's preload: how much must be downloaded
# before play() (see CLIPS).
def full(preload):
    return {"mode": "full"}


def rate(x, ttfb=0.05, **extra):
    """Every request gets `x` times the clip's average bitrate."""
    return lambda preload: {"mode": "rate", "x": x, "ttfb": ttfb, **extra}


def timed(fn, **settings):
    return lambda preload: fn(preload=preload, **settings)


def seek(media, to):
    return {"at": {"media": media}, "do": "seek", "to": to}


def at_wall(seconds, do, **extra):
    return {"at": {"wall": seconds}, "do": do, **extra}


def case(about, delivery, actions=(), expect=None, **extra):
    return about, delivery, list(actions), expect or {}, extra


# Every case is also judged for skips, frozen video, silent stalls, wrong
# frames, bad seeks and not playing to the end; `expect` adds budgets.
CASES = {
    "full": case(
        "full speed: the reference, no buffering allowed",
        full, expect={"max_waits": 0}),
    "fast": case(
        "4x the bitrate: no buffering allowed",
        rate(4), expect={"max_waits": 0}),
    "near-rate": case(
        "1.1x the bitrate: the lead grows, so at most two waits",
        rate(1.1), expect={"max_waits": 2}),
    "under-rate": case(
        "0.8x the bitrate: must buffer, never skip or freeze",
        rate(0.8)),
    "high-ttfb": case(
        "2x the bitrate but 1 s to first byte on every request",
        rate(2, ttfb=1.0)),
    "thin-lead": case(
        "the download stays only 0.5 s ahead of the playhead",
        timed(schedule.steady, lead=0.5)),
    "dip": case(
        "4 s ahead, then the lead falls to 0.2 s at 5 s",
        timed(schedule.dip, start=4, low=0.2, at=5)),
    "dip-slow": case(
        "the lead falls to 1.2 s at 5 s, then delivery runs at 0.85x",
        timed(schedule.dip, start=4, low=1.2, at=5, rate=0.85)),
    "outage": case(
        "2 s ahead, then nothing for 4 s from 4 s, then 1.5x",
        timed(schedule.outage, at=4, length=4, lead=2, rate=1.5)),
    "bursty": case(
        "2 s ahead, data released in 2 s lumps",
        timed(schedule.steady, lead=2, burst=2)),
    "no-ranges": case(
        "a server that ignores Range, and a seek back",
        rate(2, no_ranges=True), actions=[seek(6, 2)]),
    "live-like": case(
        "no length and no Range, like a live stream, at 1.2x",
        rate(1.2, no_ranges=True, no_length=True)),
    "seek-ahead": case(
        "at 1.5x, seek past the end of the download",
        rate(1.5), actions=[seek(2, 8)]),
    "seek-back": case(
        "at 1.2x, seek back to the start",
        rate(1.2), actions=[seek(6, 1)]),
    "pause-in-dip": case(
        "pause while the lead is falling, then resume",
        timed(schedule.dip, start=3, low=0.5, at=4),
        actions=[at_wall(3, "pause"), at_wall(5, "play")]),
    "fast-rate": case(
        "playbackRate 1.5 on a 1.25x delivery: playback outruns it",
        rate(1.25), actions=[at_wall(0, "rate", value=1.5)]),
    "busy-page": case(
        "full speed, the page's main thread blocked for 1 s",
        full, actions=[at_wall(3, "busy", value=1)], expect={"max_waits": 0}),
    "quick-start": case(
        "play() as soon as the first frame loads, 1 s ahead",
        timed(schedule.steady, lead=1), play_delay=0),
}

COLUMNS = [("startup_ms", "start ms", "{:.0f}"), ("rebuffers", "waits", "{:d}"),
           ("rebuffer_ms", "wait ms", "{:.0f}"), ("seek_ms", "seek ms", "{:.0f}"),
           ("dropped_pct", "drop %", "{:.1f}"), ("coverage", "cover", "{:.2f}")]


def build(clips, only=None):
    """[(case id, spec)], the `full` cases first."""
    specs = []
    for name in sorted(CASES, key=lambda n: n != "full"):
        if only and name != "full" and not any(o in name for o in only):
            continue
        about, shape, actions, expect, extra = CASES[name]
        for clip in clips:
            video, preload = CLIPS[clip]
            specs.append((f"{name}.{clip}", {
                "video": video,
                "delivery": shape(preload),
                "play_delay": 1.0,
                "actions": actions,
                "expect": expect,
                **extra,
                "tags": {"case": name, "clip": clip},
            }))
    return specs


class Runner:
    def __init__(self, args):
        self.args = args
        self.root = os.path.join(RESULTS, args.name)
        self.lock = threading.Lock()
        self.last_start = 0.0
        self.references = {}
        self.out = open(os.path.join(self.root, "results.jsonl"), "w")

    def run(self, case_id, spec, attempt):
        with self.lock:
            wait = self.last_start + STAGGER_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_start = time.monotonic()
        out = os.path.join(self.root, case_id if attempt == 0 else f"{case_id}-r{attempt}")
        spec = {**spec, "prefs": {**spec.get("prefs", {}), **self.args.prefs}}
        reference = self.references.get(spec["tags"]["clip"])
        try:
            result = run_case.run_spec(spec, out, self.args.firefox, reference=reference)
        except Exception as e:
            result = {"findings": [{"kind": "harness_error", "error": repr(e)}],
                      "metrics": {}, "delivery": {}}
        row = {"id": case_id, "attempt": attempt, **spec["tags"],
               "findings": sorted({f["kind"] for f in result["findings"]}),
               "metrics": result["metrics"], "delivery": result.get("delivery", {}),
               "userAgent": result.get("userAgent")}
        with self.lock:
            self.out.write(json.dumps(row) + "\n")
            self.out.flush()
            mark = "ok  " if not row["findings"] else "FAIL"
            again = f" (rerun {attempt})" if attempt else ""
            print(f"{mark} {case_id}{again} {' '.join(row['findings'])}", flush=True)
        if spec["tags"]["case"] == "full" and attempt == 0 and not row["findings"]:
            self.references[spec["tags"]["clip"]] = result
        return row

    def run_all(self, specs, attempt=0):
        firsts = [s for s in specs if s[1]["tags"]["case"] == "full"]
        rest = [s for s in specs if s[1]["tags"]["case"] != "full"]
        rows = []
        with ThreadPoolExecutor(self.args.jobs) as pool:
            for batch in (firsts, rest):
                rows += list(pool.map(lambda s: self.run(s[0], s[1], attempt), batch))
        return rows


def run(args):
    args.prefs = run_case.parse_pairs(args.pref)
    specs = build(args.clips, args.only)
    if not specs:
        sys.exit("no cases match --only")
    root = os.path.join(RESULTS, args.name)
    os.makedirs(root, exist_ok=True)
    # Make every clip once, before cases running at once all want it.
    for clip in args.clips:
        run_case.resolve_video({"video": CLIPS[clip][0]})
    with open(os.path.join(root, "meta.json"), "w") as f:
        json.dump({"firefox": args.firefox, "clips": args.clips, "only": args.only,
                   "jobs": args.jobs, "prefs": args.prefs,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=1)
    print(f"{len(specs)} cases, {args.jobs} at a time, into {root}")
    runner = Runner(args)
    rows = runner.run_all(specs)
    for attempt in range(1, args.confirm + 1):
        failed = {r["id"] for r in rows if r["findings"]}
        again = [s for s in specs if s[0] in failed]
        if not again:
            break
        print(f"rerunning {len(again)} cases with findings")
        rows += runner.run_all(again, attempt)
    runner.out.close()
    sys.exit(report(args))


def load(name):
    path = os.path.join(RESULTS, name, "results.jsonl")
    if not os.path.exists(path):
        sys.exit(f"no results at {path}")
    cases = {}
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            cases.setdefault(row["id"], []).append(row)
    return cases


def outcome(runs):
    """'ok', 'FAIL' (findings in every run) or 'flaky', and the kinds seen."""
    bad = [r for r in runs if r["findings"]]
    kinds = sorted({k for r in bad for k in r["findings"]})
    if not bad:
        return "ok", kinds
    return ("FAIL" if len(bad) == len(runs) else "flaky"), kinds


def cell(value, fmt):
    return "-" if value is None else fmt.format(value)


def report(args):
    """Print a table of the run and return 1 if any case failed in every run."""
    cases = load(args.name)
    before = load(args.against) if getattr(args, "against", None) else {}
    ua = next((r["userAgent"] for runs in cases.values() for r in runs if r.get("userAgent")),
              None)
    print(f"\n{args.name}: {ua or 'unknown browser'}")
    header = f"{'case':<22} {'result':<6}" + "".join(f" {h:>8}" for _, h, _ in COLUMNS)
    print(header + "  findings")
    failed = flaky = 0
    for case_id, runs in cases.items():
        state, kinds = outcome(runs)
        failed += state == "FAIL"
        flaky += state == "flaky"
        metrics = runs[0]["metrics"]
        line = f"{case_id:<22} {state:<6}"
        for key, _, fmt in COLUMNS:
            line += f" {cell(metrics.get(key), fmt):>8}"
        tally = f"{sum(bool(r['findings']) for r in runs)}/{len(runs)} " if len(runs) > 1 else ""
        print(line + ("  " + tally + ",".join(kinds) if kinds else ""))
        if case_id in before:
            old_state, old_kinds = outcome(before[case_id])
            old = before[case_id][0]["metrics"]
            line = f"{'  was':<22} {old_state:<6}"
            for key, _, fmt in COLUMNS:
                line += f" {cell(old.get(key), fmt):>8}"
            print(line + ("  " + ",".join(old_kinds) if old_kinds else ""))
    print(f"\n{len(cases)} cases: {len(cases) - failed - flaky} ok, {failed} failed, "
          f"{flaky} flaky")
    if before:
        common = [c for c in cases if c in before]
        fixed = [c for c in common if outcome(before[c])[0] != "ok" and outcome(cases[c])[0] == "ok"]
        broke = [c for c in common if outcome(before[c])[0] == "ok" and outcome(cases[c])[0] != "ok"]
        print(f"against {args.against}: {len(fixed)} now pass ({' '.join(fixed) or '-'}), "
              f"{len(broke)} now fail ({' '.join(broke) or '-'})")
    return 1 if failed else 0


def list_cases(args):
    for name, (about, *_rest) in CASES.items():
        print(f"{name:<14} {about}")
    print(f"\nclips: {', '.join(CLIPS)} (default {', '.join(DEFAULT_CLIPS)})")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("run", help="run the suite")
    p.add_argument("--firefox", required=True, help="Firefox binary to test")
    p.add_argument("--name", required=True, help="results go to results/NAME")
    p.add_argument("--jobs", type=int, default=4,
                   help="cases at once (default %(default)s); compare runs made "
                        "with the same number, as timing depends on it")
    p.add_argument("--clips", nargs="+", default=DEFAULT_CLIPS, choices=list(CLIPS))
    p.add_argument("--only", nargs="+", metavar="TEXT",
                   help="run only cases whose name contains one of these")
    p.add_argument("--confirm", type=int, default=1,
                   help="reruns of cases with findings (default %(default)s)")
    p.add_argument("--pref", nargs="*", metavar="NAME=VALUE",
                   help="Firefox prefs for every case")
    p.add_argument("--against", metavar="NAME", help="an earlier run to compare with")
    p.set_defaults(function=run)
    p = commands.add_parser("report", help="show a finished run")
    p.add_argument("--name", required=True)
    p.add_argument("--against", metavar="NAME", help="an earlier run to compare with")
    p.set_defaults(function=lambda a: sys.exit(report(a)))
    p = commands.add_parser("list", help="list the cases")
    p.set_defaults(function=list_cases)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
