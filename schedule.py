"""Deliveries defined by how much media is available at each moment after
play(), so that a planned wait cannot be skipped by asking again.

A `lead` delivery is

    {"mode": "lead", "ttfb": 0.05, "preload": 0.3,
     "points": [[0.25, 4.5], [2.0, 4.5]], "rate": 1.0, "burst": 0.25}

- Media up to `preload` seconds is available as soon as it is asked for.
- Nothing more is available until the page calls play(). From then, the media
  time available `t` seconds later rises in straight lines from
  (0, preload) through `points`, and after the last point at `rate` seconds
  of media per second.
- With `burst`, what becomes available during each `burst` seconds is held
  back and released together at the end of it.
- `ttfb` delays the first byte of every request.

If playback never stalls, the download is `available(t) - t` seconds ahead of
the playhead at time t. `steady`, `dip` and `outage` build common shapes."""

import bisect
import math
import subprocess

CHUNK = 4096


def media_packets(path):
    """[(byte offset, end byte offset, dts seconds)] for every packet, in file
    order."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "packet=dts_time,pos,size",
         "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True).stdout
    packets = []
    for line in out.splitlines():
        dts, size, pos = line.split(",")[:3]
        if dts == "N/A" or pos == "N/A":
            continue
        packets.append((int(pos), int(pos) + int(size), float(dts)))
    packets.sort()
    return packets


def curve(delivery):
    """[(seconds after play, media seconds available)], starting at zero."""
    points = [(0.0, float(delivery.get("preload", 0)))]
    for when, media in delivery.get("points", []):
        if when < points[-1][0] or media < points[-1][1]:
            raise ValueError("lead points must not go backwards")
        points.append((float(when), float(media)))
    return points


def available(delivery, seconds):
    points = curve(delivery)
    burst = delivery.get("burst")
    if burst:
        seconds = math.floor(seconds / burst + 1e-9) * burst
    if seconds >= points[-1][0]:
        return points[-1][1] + (seconds - points[-1][0]) * delivery.get("rate", 1.0)
    i = bisect.bisect_right([p[0] for p in points], seconds) - 1
    (t0, m0), (t1, m1) = points[i], points[i + 1]
    return m0 + (m1 - m0) * (seconds - t0) / (t1 - t0)


def when_available(delivery, media):
    """Seconds after play() at which `media` becomes available: None if it is
    there before play, inf if it never is."""
    points = curve(delivery)
    if media <= points[0][1]:
        return None
    when = math.inf
    for (t0, m0), (t1, m1) in zip(points, points[1:]):
        if media <= m1:
            when = t0 + (t1 - t0) * (media - m0) / (m1 - m0)
            break
    else:
        rate = delivery.get("rate", 1.0)
        if rate > 0:
            when = points[-1][0] + (media - points[-1][1]) / rate
    burst = delivery.get("burst")
    if burst and when != math.inf:
        when = math.ceil(when / burst - 1e-9) * burst
    return when


def plan(packets, size, delivery):
    """[(end offset, seconds after play or None)]: the file cut into pieces,
    each with the moment its last byte may be sent."""
    # needed[i]: the media time by which the bytes below ends[i] must all be
    # there, taking packets in time order.
    by_time = sorted((dts, end) for _, end, dts in packets)
    ends, needed = [], []
    for dts, end in by_time:
        if not ends or end > ends[-1]:
            ends.append(end)
            needed.append(dts)
    steps = []
    offset = 0
    while offset < size:
        offset = min(size, offset + CHUNK)
        i = bisect.bisect_left(ends, offset)
        # Bytes after the last packet (an index at the end of the file, say)
        # come with the last of the media.
        media = needed[min(i, len(needed) - 1)] if needed else 0.0
        steps.append((offset, when_available(delivery, media)))
    return steps


def _lead(points, rate, preload, ttfb, burst):
    # Nothing is taken back: the delivery waits at `preload` until the
    # pacing line passes it.
    points = [[when, max(media, preload)] for when, media in points]
    return {"mode": "lead", "ttfb": ttfb, "preload": preload, "points": points,
            "rate": rate, "burst": burst}


def steady(lead, rate=1.0, preload=0.3, ttfb=0.05, burst=0.25):
    """The download reaches `lead` seconds ahead soon after play, and from
    there gains `rate - 1` seconds of lead each second."""
    first = ttfb + 0.2
    if preload > first + lead:
        return _lead([[preload - lead, preload]], rate, preload, ttfb, burst)
    return _lead([[ttfb, preload], [first, first + lead]], rate, preload, ttfb, burst)


def dip(start, low, at, rate=1.0, preload=0.3, ttfb=0.05, burst=0.25):
    """The lead is `start` seconds soon after play, falls to `low` when the
    playhead reaches `at` seconds, and from there changes by `rate - 1` each
    second."""
    first = ttfb + 0.2
    fall = at - (start - low)
    if fall < first:
        raise ValueError("the lead cannot fall that far that early")
    if preload > at + low:
        raise ValueError("more is preloaded than the dip allows")
    return _lead([[ttfb, preload], [first, first + start], [fall, fall + start],
                  [at, at + low]], rate, preload, ttfb, burst)


def outage(at, length, lead=3.0, rate=1.0, preload=0.3, ttfb=0.05, burst=0.25):
    """The download keeps `lead` seconds ahead until `at` seconds after play,
    sends nothing for `length` seconds, then resumes at `rate`."""
    first = ttfb + 0.2
    if at < first:
        raise ValueError("an outage cannot start before the download does")
    return _lead([[ttfb, preload], [first, first + lead], [at, at + lead],
                  [at + length, at + lead]], rate, preload, ttfb, burst)
