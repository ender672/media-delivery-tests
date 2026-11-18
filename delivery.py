#!/usr/bin/env python3
"""Work out how a case's video was really delivered.

The server records every request for the video and when each byte went out.
From that, the file's packet layout and the page's samples of currentTime,
this computes for every 100 ms of the run how much media had arrived ahead of
the playhead (the lead), by the server's account and by Firefox's own
(`video.buffered`).

    python delivery.py CASE_DIR      print the requests and the lead over time"""

import argparse
import bisect
import json
import os

import schedule

# A lead is not counted once the download has reached this close to the end
# of the file, where it shrinks to nothing however good the delivery is.
END_MARGIN = 0.1
# The lowest lead is taken from this much playback onward.
SETTLED_SECONDS = 1.0
# A `lead` delivery that fell this far behind its schedule (Firefox not
# reading, or waiting on a new request) did not test what it was meant to.
OFF_PLAN_SECONDS = 0.5


class MediaMap:
    """Byte offsets to media time and back, from a file's packets."""

    def __init__(self, packets, duration):
        self.duration = duration
        self.ends = [end for _, end, _ in packets]
        # until[i]: the earliest media time among packets i onward, so the
        # time up to which everything is present once packets before i are.
        self.until = [duration] * (len(packets) + 1)
        for i in range(len(packets) - 1, -1, -1):
            self.until[i] = min(self.until[i + 1], packets[i][2])
        by_time = sorted((dts, pos) for pos, _, dts in packets)
        self.times = [dts for dts, _ in by_time]
        # first[i]: the lowest offset among packets at times[i] or later.
        self.first = [0] * (len(by_time) + 1)
        self.first[len(by_time)] = self.ends[-1] if packets else 0
        for i in range(len(by_time) - 1, -1, -1):
            self.first[i] = min(self.first[i + 1], by_time[i][1])

    def media_at(self, offset):
        """Media time up to which every packet lies below `offset`."""
        return self.until[bisect.bisect_right(self.ends, offset)]

    def offset_of(self, seconds):
        """Offset of the first byte needed to play on from `seconds`."""
        return self.first[bisect.bisect_left(self.times, seconds - 0.001)]


class Ranges:
    def __init__(self):
        self.starts = []
        self.ends = []

    def add(self, start, end):
        i = bisect.bisect_left(self.ends, start)
        j = bisect.bisect_right(self.starts, end)
        if i < j:
            start = min(start, self.starts[i])
            end = max(end, self.ends[j - 1])
        self.starts[i:j] = [start]
        self.ends[i:j] = [end]

    def end_from(self, offset):
        """End of the range holding `offset`, or `offset` if none does."""
        i = bisect.bisect_right(self.starts, offset) - 1
        if i >= 0 and self.ends[i] >= offset:
            return self.ends[i]
        return offset


def arrivals(requests, page_start):
    """[(page ms, start, end)] for every piece sent, in time order."""
    pieces = []
    for request in requests:
        before = 0
        for when, sent in request["sent"]:
            pieces.append((round((when - page_start) * 1000),
                           request["start"] + before, request["start"] + sent))
            before = sent
    return sorted(pieces)


def analyse(video_path, video, requests, page_start, record, lead=None, play_time=None):
    """Return {"requests", "series", "summary"}. Each series row is [page ms,
    currentTime, media delivered from the start of the file, media available
    at the playhead by the server's account, the same by Firefox's, and for a
    `lead` delivery the media the schedule had made available]."""
    if page_start is None or not requests or "samples" not in record:
        return {"requests": requests or [], "series": [], "summary": {}}
    media = MediaMap(schedule.media_packets(video_path), video["duration"])
    to_ms = lambda seconds: round((seconds - page_start) * 1000)
    listed = [{
        "asked_ms": to_ms(r["asked"]),
        "first_byte_ms": to_ms(r["sent"][0][0]) if r["sent"] else None,
        "ended_ms": to_ms(r["ended"]) if "ended" in r else None,
        "range": r["range"],
        "start": r["start"],
        "length": r["length"],
        "sent": r["sent"][-1][1] if r["sent"] else 0,
        "complete": r.get("complete", False),
    } for r in requests]

    pieces = arrivals(requests, page_start)
    have = Ranges()
    piece = 0
    series = []
    for sample in record["samples"]:
        when, current = sample[0], sample[1]
        while piece < len(pieces) and pieces[piece][0] <= when:
            have.add(pieces[piece][1], pieces[piece][2])
            piece += 1
        at = media.offset_of(current)
        designed = None
        if lead:
            since = None if play_time is None else when / 1000 - (play_time - page_start)
            designed = round(min(media.duration, lead.get("preload", 0) if since is None
                                 or since < 0 else schedule.available(lead, since)), 3)
        series.append([
            when,
            current,
            round(max(0, media.media_at(have.end_from(0))), 3),
            round(max(current, media.media_at(have.end_from(at))), 3),
            sample[6] if len(sample) > 6 else None,
            designed,
        ])

    play_at = record.get("playAt")
    ended = next((e[0] for e in record.get("events", []) if e[1] == "ended"), None)
    playing = [row for row in series if play_at is not None and row[0] >= play_at
               and (ended is None or row[0] <= ended)]

    def lowest(column):
        rows = [row for row in playing
                if row[column] is not None and row[1] >= SETTLED_SECONDS
                and row[column] < media.duration - END_MARGIN]
        if not rows:
            return None
        row = min(rows, key=lambda r: r[column] - r[1])
        return {"lead": round(row[column] - row[1], 3), "at": row[1], "ms": row[0]}

    at_play = playing[0] if playing else None
    summary = {
        "lead_at_play": round((at_play[4] or at_play[3]) - at_play[1], 3) if at_play else None,
        "requests": len(listed),
        "incomplete_requests": sum(not r["complete"] for r in listed),
        "bytes_sent": sum(r["sent"] for r in listed),
        "min_lead_server": lowest(3),
        "min_lead_client": lowest(4),
    }
    if lead:
        # The server never sends early, so the only way to miss the design is
        # to fall behind it.
        late = [(row[5] - row[3], row) for row in playing if row[5] > row[1]]
        worst = max(late, key=lambda item: item[0], default=None)
        summary["behind_design"] = round(max(0, worst[0]), 3) if worst else 0
        summary["off_plan"] = summary["behind_design"] > OFF_PLAN_SECONDS
    return {"requests": listed, "series": series, "summary": summary}


def show(directory):
    with open(os.path.join(directory, "delivery.json")) as f:
        delivered = json.load(f)
    with open(os.path.join(directory, "result.json")) as f:
        record = json.load(f)
    print(f"play() at {record.get('playAt')} ms")
    print("requests for the video:")
    for r in delivered["requests"]:
        print(f"  asked {r['asked_ms']:>6} ms  first byte {r['first_byte_ms']} ms  "
              f"ended {r['ended_ms']} ms  {r['range'] or 'no Range'}  "
              f"sent {r['sent']} of {r['length']}"
              f"{'' if r['complete'] else '  (closed early)'}")
    print("summary:", json.dumps(delivered["summary"]))
    print("\n   wall s  currentTime  delivered  at playhead: server  Firefox   lead  designed")
    show_at = None
    for row in delivered["series"]:
        if show_at is not None and row[0] < show_at:
            continue
        show_at = row[0] + 500
        fmt = lambda v: "      -" if v is None else f"{v:7.2f}"
        lead = (row[4] if row[4] else row[3]) - row[1]
        print(f"  {row[0] / 1000:7.1f}  {row[1]:11.2f}  {fmt(row[2])}"
              f"  {fmt(row[3]):>19}  {fmt(row[4])}  {lead:5.2f}  {fmt(row[5])}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    show(parser.parse_args().directory)


if __name__ == "__main__":
    main()
