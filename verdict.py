#!/usr/bin/env python3
"""Judge a playback record made by oracle.html.

A finding is something a correct player should not do however the bytes
arrive: skip video, show a frozen picture while playback carries on, stall
without saying it is waiting, show a frame other than the one it reports,
land a seek on the wrong frame, or fail to play to the end. A case can also
set budgets in `expect` (see EXPECT) for things that are only too slow, not
wrong in themselves.

    python verdict.py CASE_DIR [--reference REF_DIR]    rejudge a saved case"""

import argparse
import json
import math
import os
from collections import Counter

SKIP_SECONDS = 0.5
FROZEN_WALL_MS = 1000
FROZEN_ADVANCE_SECONDS = 0.5
STALL_MS = 1000
COVERAGE_TOLERANCE = 0.1
SEEK_SETTLE_MS = 300
# The page samples currentTime every 100 ms; a longer silence from it means
# its main thread was held up.
PAGE_STALL_MS = 250
SEEK_SLOW_MS = 20000


def waiting_windows(events, end_ms):
    """[(start ms, end ms)] during which the element had said it was waiting."""
    windows = []
    start = None
    for when, kind, _ in events:
        if kind == "waiting" and start is None:
            start = when
        elif kind in ("playing", "ended", "error") and start is not None:
            windows.append((start, when))
            start = None
    if start is not None:
        windows.append((start, end_ms))
    return windows


def overlap(start, end, windows):
    return sum(max(0, min(end, b) - max(start, a)) for a, b in windows)


def find_seeks(record, end_ms):
    """One dict per seek action: when it was asked for, when `seeked` fired
    (None if it never did, or a later seek replaced it first) and where
    playback then was."""
    asked = [a for a in record.get("actions", []) if a[1] == "seek"]
    seeks = []
    for action, following in zip(asked, asked[1:] + [None]):
        when, target, fast = action[0], action[3], action[4]
        until = following[0] if following else end_ms
        done = next((e for e in record["events"]
                     if e[1] == "seeked" and when <= e[0] < until), None)
        seeks.append({
            "at": when,
            "to": target,
            "fast": fast,
            "seekable": action[5] if len(action) > 5 else None,
            "frames_before": action[6] if len(action) > 6 else 0,
            "done": done[0] if done else None,
            "landed": done[2] if done else None,
            "until": until,
        })
    return seeks


def judge_seek(seek, frames, fps):
    """Yield findings for one seek. `frames` are those presented from shortly
    before `seeked` until the next seek. A seek to a time outside the
    element's seekable ranges has to go to the nearest time inside them, and
    one made when nothing is seekable does nothing."""
    wanted = seek["to"]
    if seek["seekable"] is not None:
        if not seek["seekable"]:
            return
        if not any(start <= wanted <= end for start, end in seek["seekable"]):
            wanted = min((edge for span in seek["seekable"] for edge in span),
                         key=lambda edge: abs(edge - wanted))
    if seek["done"] is None:
        if seek["until"] - seek["at"] >= SEEK_SLOW_MS:
            yield {"kind": "seek_never_completed", "to": seek["to"],
                   "waited_ms": seek["until"] - seek["at"]}
        return
    if not seek["fast"] and abs(seek["landed"] - wanted) > 0.05:
        yield {"kind": "seek_wrong_position", "to": seek["to"], "landed": seek["landed"],
               **({"nearest_seekable": wanted} if wanted != seek["to"] else {})}
    if frames:
        first = frames[0]
        ahead = first[1] - seek["landed"]
        played = max(0, first[0] - seek["done"]) / 1000
        if ahead < -1.5 / fps or ahead > 3 / fps + played:
            yield {"kind": "seek_wrong_frame", "to": seek["to"], "landed": seek["landed"],
                   "first_frame": first[1], "after_ms": first[0] - seek["done"]}


def page_stalls(samples, render_gaps=()):
    """Spans in which the page's own timer did not run, or in which the page
    was given no animation frame, and so no frame callback: nothing the page
    reports about them says what was shown."""
    spans = sorted([(before[0], after[0]) for before, after in zip(samples, samples[1:])]
                   + [tuple(gap) for gap in render_gaps])
    merged = []
    for start, end in spans:
        if end - start < PAGE_STALL_MS:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def longest_unpainted(paints):
    """The longest span, in ms, that begins with a frame handed to the
    compositor, ends with the next one it put up, and has two or more handed
    over in it: a picture that stood still with frames to show. `paints` is
    [wall ms, frames put up, frames handed over], one row per change."""
    longest = 0
    start = None
    for before, row in zip(paints, paints[1:]):
        if row[2] > before[2] and start is None:
            start = (row[0], before[2])
        if row[1] > before[1] and start is not None:
            if row[2] - start[1] >= 2:
                longest = max(longest, row[0] - start[0])
            start = None
    return longest


def unseen(previous, frame, stalls):
    """Whether the time between two presented frames is one the page was
    stalled for, with playback going on no faster than it can behind it."""
    wall = frame[0] - previous[0]
    return (overlap(previous[0], frame[0], stalls) >= max(wall - 100, PAGE_STALL_MS / 2)
            and frame[1] - previous[1] <= wall / 1000 * 2 + SKIP_SECONDS / 2)


def find_skips(frames, fps, stalls=()):
    """A step of one frame is not a skip, however long the frame."""
    least = max(SKIP_SECONDS, 1.5 / fps)
    for previous, frame in zip(frames, frames[1:]):
        jump = frame[1] - previous[1]
        if jump > least and not unseen(previous, frame, stalls):
            yield {
                "kind": "skip",
                "from": previous[1],
                "to": frame[1],
                "skipped_seconds": round(jump, 3),
                "wall_ms": frame[0] - previous[0],
            }


def find_frozen_video(frames, fps, duration, ended_ms, stalls=()):
    """A picture is not frozen in less than two frames' time, however long
    the frames."""
    least_ms = max(FROZEN_WALL_MS, 2000 / fps)
    least = max(FROZEN_ADVANCE_SECONDS, 2 / fps)
    for previous, frame in zip(frames, frames[1:]):
        wall = frame[0] - previous[0]
        advance = frame[2] - previous[2]
        if wall >= least_ms and advance >= least and not unseen(previous, frame, stalls):
            yield {
                "kind": "frozen_video",
                "at": previous[1],
                "wall_ms": wall,
                "playback_advanced": round(advance, 3),
            }
    if frames and ended_ms is not None:
        last = frames[-1]
        if duration - last[1] >= least and ended_ms - last[0] >= least_ms:
            yield {
                "kind": "frozen_video",
                "at": last[1],
                "wall_ms": ended_ms - last[0],
                "playback_advanced": round(duration - last[2], 3),
                "until_end": True,
            }


def find_silent_stalls(samples, windows, start_ms, end_ms, stalls=()):
    """`currentTime` standing still while playing, outside `waiting` windows
    and outside spans the page itself was held up (it reads a stale
    `currentTime` straight after one)."""
    run_start = None
    previous = None
    for sample in samples + [None]:
        same = (
            sample is not None
            and previous is not None
            and start_ms <= sample[0] <= end_ms
            and not sample[4]
            and abs(sample[1] - previous[1]) < 0.001
        )
        if same:
            if run_start is None:
                run_start = previous
        elif run_start is not None:
            length = previous[0] - run_start[0]
            silent = (length - overlap(run_start[0], previous[0], windows)
                      - overlap(run_start[0], previous[0], stalls))
            if silent >= STALL_MS:
                yield {
                    "kind": "silent_stall",
                    "at": run_start[1],
                    "stalled_ms": length,
                    "without_waiting_ms": silent,
                    "ready_state": run_start[2],
                }
            run_start = None
        previous = sample


def wrong_frames(frames, fps, actions):
    """Generated clips only: frames whose barcode is too far from their
    reported time. The picture drawn in a frame callback is often one frame
    away from the frame the callback reports, and now and then two, so only
    a larger difference counts. A page that was late reading the picture
    reads a later frame, by up to the frames shown meanwhile."""
    coded = [f for f in frames if f[3] is not None]
    if not coded:
        return []
    usual = Counter(f[3] - round(f[1] * fps) for f in coded).most_common(1)[0][0]
    fastest = max([1] + [a[3] for a in actions if a[1] == "rate"])

    def wrongly_shown(f):
        away = f[3] - round(f[1] * fps) - usual
        late = f[4] if len(f) > 4 else 0
        return not -2 <= away <= 2 + math.ceil(late * fps * fastest / 1000)

    wrong = [f for f in coded if wrongly_shown(f)]
    if not wrong:
        return []
    return [{"kind": "wrong_frame", "count": len(wrong),
             "examples": [{"reported": f[1], "shown_frame": f[3]} for f in wrong[:5]]}]


def evaluate(record, video, reference=None):
    """Return {"findings": [...], "metrics": {...}} for one playback record of
    `video`, a description from run_case.describe_video or make_clip."""
    findings = []
    events = record.get("events", [])
    play_at = record.get("playAt")
    if record.get("finished") != "ended" or play_at is None:
        findings.append({
            "kind": "not_ended",
            "finished": record.get("finished"),
            "error": record.get("error") or record.get("playError"),
        })
    if play_at is None:
        return {"findings": findings, "metrics": {}}

    fps = video["fps"]
    frames = [f for f in record["frames"] if f[0] >= play_at]
    ended_ms = next((e[0] for e in events if e[1] == "ended"), None)
    end_ms = ended_ms if ended_ms is not None else (
        record["samples"][-1][0] if record.get("samples") else play_at)
    windows = [w for w in waiting_windows(events, end_ms) if w[1] > play_at]
    # Startup is play() to the first frame presented past where play() was
    # called. Waiting before then is part of startup, not a rebuffer.
    at_play = next((e[2] for e in events if e[1] == "play" and e[0] >= play_at), 0)
    first = next((f for f in frames if f[1] > at_play + 0.001), None)
    started = first[0] if first else end_ms
    rebuffers = [w for w in windows if w[0] >= started]
    stalls = page_stalls(record.get("samples", []), record.get("renderGaps", []))

    # Each seek starts a new stretch of playback. A stretch after a seek
    # begins where the seek landed, so a picture that never follows the seek
    # shows up as frozen video.
    seeks = find_seeks(record, end_ms)
    stretches = [[f for f in frames if not seeks or f[0] < seeks[0]["at"]]]
    for seek in seeks:
        start = seek["at"] if seek["done"] is None else max(
            seek["at"], seek["done"] - SEEK_SETTLE_MS)
        # A frame recorded before the seek was asked for is not one of its
        # frames, even when both fall in the same millisecond.
        after = [f for f in record["frames"][seek["frames_before"]:]
                 if max(start, play_at) <= f[0] < seek["until"]]
        # Nor is the frame that follows the last one of those and is reported
        # before `seeked`: it was on its way to the screen when the seek was
        # asked for, and its callback ran a refresh later.
        if after and seek["done"] is not None and seek["frames_before"]:
            last = record["frames"][seek["frames_before"] - 1]
            if after[0][0] < seek["done"] and 0 < after[0][1] - last[1] < 1.5 / fps:
                after = after[1:]
        findings += judge_seek(seek, after, fps)
        if seek["done"] is not None:
            windows.append((seek["at"], seek["done"]))
            after = [[seek["done"], seek["landed"], seek["landed"], None]] + [
                f for f in after if f[0] >= seek["done"]]
        else:
            windows.append((seek["at"], seek["until"]))
            after = []
        stretches.append(after)
    for index, stretch in enumerate(stretches):
        last = index == len(stretches) - 1
        findings += find_skips(stretch, fps, stalls)
        findings += find_frozen_video(stretch, fps,
                                      video.get("video_duration", video["duration"]),
                                      ended_ms if last else None, stalls)
    findings += find_silent_stalls(record["samples"], windows, play_at, end_ms, stalls)
    findings += wrong_frames(frames, fps, record.get("actions", []))
    if video.get("barcode") and record.get("unreadable", 0) > 2:
        findings.append({"kind": "unreadable_frames", "count": record["unreadable"]})

    shown = {round(f[1] * fps) for f in frames}
    coverage = len(shown) / video["frames"]
    # Frames presented while the page was held up are never seen by it.
    unseen_share = sum(min(b, end_ms) - max(a, play_at) for a, b in stalls
                       if b > play_at and a < end_ms) / 1000 * fps / video["frames"]
    if (reference and not seeks and reference["metrics"].get("coverage")
            and coverage < reference["metrics"]["coverage"] - COVERAGE_TOLERANCE
            - unseen_share):
        findings.append({
            "kind": "missing_frames",
            "coverage": round(coverage, 3),
            "reference_coverage": reference["metrics"]["coverage"],
        })

    loaded = next((e[0] for e in events if e[1] == "loadeddata"), None)
    quality = record.get("quality") or {}
    wall_ms = end_ms - play_at
    rebuffer_ms = sum(b - max(a, play_at) for a, b in rebuffers)
    seek_times = [s["done"] - s["at"] for s in seeks if s["done"] is not None]
    metrics = {
        "load_ms": loaded,
        "startup_ms": first[0] - play_at if first else None,
        "rebuffers": len(rebuffers),
        "rebuffer_ms": rebuffer_ms,
        "stall_pct": round(100 * rebuffer_ms / wall_ms, 1) if wall_ms > 0 else None,
        "seek_ms": max(seek_times) if seek_times else None,
        "dropped": quality.get("dropped"),
        "dropped_pct": round(100 * quality["dropped"] / quality["total"], 2)
        if quality.get("total") else None,
        "coverage": round(coverage, 3),
        "frames_shown": len(shown),
        "max_video_lag": round(max((f[2] - f[1] for f in frames), default=0), 3),
        "wall_ms": wall_ms,
    }
    lateness = [f[4] for f in frames if len(f) > 4]
    if lateness:
        metrics["page_late_ms"] = max(lateness)
    if record.get("paints"):
        metrics["unpainted_ms"] = longest_unpainted(record["paints"])
    return {"findings": findings, "metrics": metrics}


# What a case's `expect` can limit: its name there, where the number is in
# the verdict, and the finding when the number is over the limit.
EXPECT = (("max_wait_ms", "metrics", "rebuffer_ms", "needless_wait"),
          ("max_waits", "metrics", "rebuffers", "wait_churn"),
          ("max_startup_ms", "metrics", "startup_ms", "slow_startup"),
          ("max_seek_ms", "metrics", "seek_ms", "slow_seek"),
          ("max_dropped_pct", "metrics", "dropped_pct", "dropped_frames"),
          ("max_requests", "delivery", "requests", "request_churn"))


def unexpected(expect, result):
    """Findings for the limits in a case's `expect` that the run is over."""
    findings = []
    for name, section, measure, kind in EXPECT:
        seen = (result.get(section) or {}).get(measure)
        if name in expect and seen is not None and seen > expect[name]:
            findings.append({"kind": kind, "seen": seen, "limit": expect[name]})
    return findings


def describe(finding):
    kind = finding["kind"]
    if kind in {e[3] for e in EXPECT}:
        return f"{kind}: {finding['seen']}, where the case allows {finding['limit']}"
    if kind == "skip":
        return (f"skip: video jumped {finding['from']:.2f} s -> {finding['to']:.2f} s "
                f"({finding['skipped_seconds']:.2f} s never shown)")
    if kind == "frozen_video":
        tail = " until the end" if finding.get("until_end") else ""
        return (f"frozen video: picture stuck at {finding['at']:.2f} s for "
                f"{finding['wall_ms'] / 1000:.1f} s while playback advanced "
                f"{finding['playback_advanced']:.2f} s{tail}")
    if kind == "silent_stall":
        return (f"silent stall: playback stopped at {finding['at']:.2f} s for "
                f"{finding['stalled_ms'] / 1000:.1f} s, "
                f"{finding['without_waiting_ms'] / 1000:.1f} s of it without 'waiting' "
                f"(readyState {finding['ready_state']})")
    if kind == "seek_wrong_frame":
        return (f"seek wrong frame: seek to {finding['to']:.2f} s landed at "
                f"{finding['landed']:.2f} s but first showed {finding['first_frame']:.2f} s")
    if kind == "seek_wrong_position":
        return (f"seek wrong position: seek to {finding['to']:.2f} s landed at "
                f"{finding['landed']:.2f} s")
    if kind == "seek_never_completed":
        return (f"seek never completed: seek to {finding['to']:.2f} s had no 'seeked' "
                f"after {finding['waited_ms'] / 1000:.0f} s")
    if kind == "not_ended":
        return f"not ended: {finding['finished']}" + (
            f" ({finding['error']})" if finding.get("error") else "")
    if kind == "crash":
        return f"crash: {finding['dumps']} minidumps were left in the profile"
    if kind == "wrong_frame":
        return f"wrong frame: {finding['count']} presented frames did not match their reported time"
    if kind == "unreadable_frames":
        return f"unreadable frames: {finding['count']} frames had no readable barcode"
    if kind == "missing_frames":
        return (f"missing frames: {finding['coverage']:.0%} of frames shown, "
                f"{finding['reference_coverage']:.0%} in the reference run")
    return f"{kind}: {json.dumps({k: v for k, v in finding.items() if k != 'kind'})}"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_dir", help="output directory of run_case.py")
    parser.add_argument("--reference", help="output directory of a full-speed run")
    args = parser.parse_args()
    with open(os.path.join(args.case_dir, "result.json")) as f:
        record = json.load(f)
    with open(os.path.join(args.case_dir, "case.json")) as f:
        video = json.load(f)["video"]
    reference = None
    if args.reference:
        with open(os.path.join(args.reference, "verdict.json")) as f:
            reference = json.load(f)
    result = evaluate(record, video, reference)
    print(json.dumps(result["metrics"]))
    for finding in result["findings"]:
        print(describe(finding))


if __name__ == "__main__":
    main()
