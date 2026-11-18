#!/usr/bin/env python3
"""Play one video in one headless Firefox under one network delivery, and
judge what was presented.

The video is either one from the media directory or a synthetic clip made by
make_clip.py. The server (range_server.py's handler, in-process) shapes the
delivery; Firefox runs on a fresh profile at oracle.html, which posts its
record back. Everything needed to rerun the case is left in the output
directory, including the spec as spec.json (rerun it with --case).

    python run_case.py --firefox /usr/bin/firefox --out runs/a --generate
    python run_case.py --firefox ... --out runs/b --generate --rate x=0.8
    python run_case.py --firefox ... --out runs/c --generate container=webm \\
        --dip start=4 low=0.2 at=5"""

import argparse
import bisect
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer

import delivery
import make_clip
import schedule
import verdict
from range_server import RangeRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(HERE, "media")
CLIP_DIR = os.path.join(HERE, "clips")
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ogv")

PREFS = {
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # Headless Firefox composites at 25 Hz by default, which hides frames of
    # a 24 fps video.
    "layout.frame_rate": 60,
    "app.update.disabledForTesting": True,
    "app.update.auto": False,
    "app.update.staging.enabled": False,
    "app.update.background.scheduling.enabled": False,
    "app.update.checkInstallTime": False,
    "browser.aboutwelcome.enabled": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "extensions.update.enabled": False,
    "network.captive-portal-service.enabled": False,
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    # Nothing below is fetched or uploaded in the background: a fresh profile
    # otherwise makes hundreds of requests while the case is loading.
    "services.settings.server": "data:,#remote-settings-dummy/v1",
    "extensions.systemAddon.update.enabled": False,
    "extensions.systemAddon.update.url": "",
    "media.gmp-manager.updateEnabled": False,
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False,
    "browser.safebrowsing.blockedURIs.enabled": False,
    "browser.safebrowsing.provider.mozilla.updateURL": "",
    "browser.safebrowsing.provider.google4.updateURL": "",
    "browser.safebrowsing.provider.google5.updateURL": "",
    "browser.safebrowsing.provider.google.updateURL": "",
    "network.connectivity-service.enabled": False,
    "browser.region.network.url": "",
    "browser.region.update.enabled": False,
    "app.normandy.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.server": "",
    "telemetry.fog.test.localhost_port": -1,
    "browser.newtabpage.activity-stream.feeds.system.topstories": False,
    "browser.newtabpage.activity-stream.feeds.topsites": False,
    "browser.topsites.contile.enabled": False,
    "extensions.getAddons.cache.enabled": False,
    "extensions.blocklist.enabled": False,
    "dom.push.connection.enabled": False,
    "browser.search.update": False,
    "browser.ml.enable": False,
    "browser.translations.enable": False,
    "network.prefetch-next": False,
    "network.dns.disablePrefetch": True,
    "network.predictor.enabled": False,
}


class SentRecorder:
    """Passes writes through to a socket file and notes when each returned.
    A write blocks while the client is not reading, so the times are those at
    which the bytes left the server."""

    def __init__(self, raw, entry, epoch):
        self.raw = raw
        self.entry = entry
        self.epoch = epoch
        self.sent = 0

    def write(self, data):
        self.raw.write(data)
        self.sent += len(data)
        when = round(time.monotonic() - self.epoch, 3)
        log = self.entry["sent"]
        if log and when - log[-1][0] < 0.005:
            log[-1][1] = self.sent
        else:
            log.append([when, self.sent])

    def flush(self):
        self.raw.flush()


class CaseHandler(RangeRequestHandler):
    """RangeRequestHandler that also accepts the page's record, keeps its
    request log in a file, and records what it sent of the video and when.
    Subclassed per case to carry that case's state."""

    record = None
    done = None
    log = None
    epoch = None
    media = None
    # One entry per request for the video: the range asked for and
    # [seconds since `epoch`, bytes sent so far] as they went out.
    requests = None
    # When the page asked for case.json, which is when its own clock starts.
    page_start = None
    # For a `lead` delivery: schedule.plan()'s pieces, and when the page said
    # it called play().
    steps = None
    played = None
    play_time = None

    def send_head(self):
        asked = time.monotonic() - self.epoch
        name = os.path.basename(self.path.split("?")[0])
        if name == "case.json" and type(self).page_start is None:
            type(self).page_start = asked
        self.entry = None
        source = super().send_head()
        if source is not None and name == self.media and self.command == "GET":
            self.entry = {
                "asked": round(asked, 3),
                "headers": round(time.monotonic() - self.epoch, 3),
                "range": self.headers.get("Range"),
                "start": self.start,
                "length": self.remaining,
                "sent": [],
            }
            self.requests.append(self.entry)
        return source

    def copyfile(self, source, outputfile):
        if self.entry is None:
            return super().copyfile(source, outputfile)
        recorder = SentRecorder(outputfile, self.entry, self.epoch)
        try:
            if self.steps is None:
                super().copyfile(source, recorder)
            else:
                self.send_scheduled(source, recorder)
        finally:
            self.entry["ended"] = round(time.monotonic() - self.epoch, 3)
            self.entry["complete"] = recorder.sent == self.entry["length"]

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/result":
            type(self).record = json.loads(body)
            self.done.set()
        elif self.path == "/mark?play" and not self.played.is_set():
            type(self).play_time = time.monotonic()
            self.played.set()
        self.send_response(204)
        self.end_headers()

    def send_scheduled(self, source, out):
        position = self.start
        end = self.start + self.remaining
        ends = [step[0] for step in self.steps]
        try:
            for step_end, when in self.steps[bisect.bisect_right(ends, position):]:
                if position >= end:
                    break
                if when is not None:
                    while not self.played.wait(0.5):
                        if self.done.is_set():
                            return
                    wait = self.play_time + when - time.monotonic()
                    if wait == float("inf"):
                        self.done.wait()
                        return
                    if wait > 0 and self.done.wait(wait):
                        return
                size = min(step_end, end) - position
                out.write(source.read(size))
                out.flush()
                position += size
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("client closed after %d of %d bytes",
                             position - self.start, self.remaining)

    def log_message(self, format, *args):
        self.log.write(f"{self.log_date_time_string()} {format % args}\n")
        self.log.flush()


def parse_value(text):
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def parse_pairs(pairs):
    return {k: parse_value(v) for k, v in (pair.split("=", 1) for pair in pairs or [])}


def list_videos(directory):
    return sorted(name for name in os.listdir(directory)
                  if name.lower().endswith(VIDEO_SUFFIXES))


def describe_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,nb_frames:format=duration", "-of", "json", path],
        check=True, capture_output=True, text=True).stdout
    probe = json.loads(out)
    num, den = probe["streams"][0]["r_frame_rate"].split("/")
    fps = int(num) / int(den)
    duration = float(probe["format"]["duration"])
    frames = int(probe["streams"][0].get("nb_frames") or round(duration * fps))
    return {"file": os.path.basename(path), "fps": fps, "frames": frames,
            "duration": duration, "size": os.path.getsize(path), "barcode": None}


def trimmed(path, seconds, clip_dir):
    """A copy of a media-directory video cut to its first `seconds`, streams
    copied, kept in the clip cache."""
    stem, suffix = os.path.splitext(os.path.basename(path))
    out = os.path.join(clip_dir, f"{stem}-first-{seconds:g}s{suffix}")
    if not os.path.exists(out):
        os.makedirs(clip_dir, exist_ok=True)
        partial_out = f"{out}.part{os.getpid()}-{threading.get_ident()}{suffix}"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path, "-t", str(seconds), "-c", "copy",
             "-movflags", "+faststart", partial_out], check=True)
        os.replace(partial_out, out)
    return out


def resolve_video(spec, media_dir=MEDIA_DIR, clip_dir=CLIP_DIR):
    """(path, description) of the video a spec names: a video in the media
    directory, or a generated clip."""
    video = spec["video"]
    if video["source"] == "generate":
        description = make_clip.make_clip(clip_dir, **video.get("settings", {}))
        return os.path.join(clip_dir, description["file"]), description
    videos = list_videos(media_dir)
    name = video.get("name") or (videos[0] if len(videos) == 1 else None)
    if name not in videos:
        raise ValueError(f"choose a video from {media_dir}: {', '.join(videos) or 'none'}")
    path = os.path.join(media_dir, name)
    if video.get("trim"):
        path = trimmed(path, video["trim"], clip_dir)
    return path, describe_video(path)


def bitrate_kbps(video):
    return video["size"] * 8 / video["duration"] / 1000


def run_spec(spec, out, firefox, media_dir=MEDIA_DIR, clip_dir=CLIP_DIR,
             reference=None, keep_profile=False):
    video_path, video = resolve_video(spec, media_dir, clip_dir)
    shape = spec.get("delivery", {"mode": "full"})
    mode = shape.get("mode", "full")
    if mode not in ("full", "rate", "lead"):
        raise ValueError(f"unknown delivery mode {mode}")
    lead = shape if mode == "lead" else None
    steps = lead and schedule.plan(schedule.media_packets(video_path),
                                   os.path.getsize(video_path), lead)
    kbps = 0
    if mode == "rate":
        kbps = shape["kbps"] if "kbps" in shape else shape["x"] * bitrate_kbps(video)
    play_delay = spec.get("play_delay", 3.0)
    moz_log = spec.get("moz_log")

    os.makedirs(out, exist_ok=True)
    for stale in os.listdir(out):
        if stale.startswith("moz.log"):
            os.remove(os.path.join(out, stale))
    with open(os.path.join(out, "spec.json"), "w") as f:
        json.dump(spec, f, indent=1)
    for source in (video_path, os.path.join(HERE, "oracle.html")):
        link = os.path.join(out, os.path.basename(source))
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(os.path.abspath(source), link)

    max_play = (video["duration"] * 4 + 30) * 1000
    with open(os.path.join(out, "case.json"), "w") as f:
        json.dump({
            "src": video["file"],
            "video": video,
            "playDelayMs": round(play_delay * 1000),
            "playOn": spec.get("play_on", "loadeddata"),
            "actions": spec.get("actions", []),
            "maxPlayMs": round(max_play),
            "maxLoadMs": round(spec.get("max_load", 60) * 1000),
        }, f)

    log = open(os.path.join(out, "server.log"), "w")
    handler = type("Handler", (CaseHandler,), {
        "delay": shape.get("ttfb", 0.0),
        "rate": round(kbps * 125),
        "accept_ranges": not shape.get("no_ranges", False),
        "send_length": not shape.get("no_length", False),
        "done": threading.Event(),
        "steps": steps,
        "played": threading.Event(),
        "log": log,
        "epoch": time.monotonic(),
        "media": video["file"],
        "requests": [],
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=out))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/oracle.html"

    profile = tempfile.mkdtemp(prefix="profile-", dir=out)
    with open(os.path.join(profile, "user.js"), "w") as f:
        for name, value in {**PREFS, **spec.get("prefs", {})}.items():
            f.write(f"user_pref({json.dumps(name)}, {json.dumps(value)});\n")
    # Firefox leaves its downloads in TMPDIR when it is killed; the profile
    # directory is removed with the case.
    env = dict(os.environ, MOZ_HEADLESS="1", TMPDIR=os.path.abspath(profile),
               MOZ_CRASHREPORTER="1", MOZ_CRASHREPORTER_NO_REPORT="1",
               **spec.get("env", {}))
    if moz_log:
        env["MOZ_LOG"] = "timestamp,sync," + moz_log
        env["MOZ_LOG_FILE"] = os.path.join(os.path.abspath(out), "moz.log")
    command = [firefox, "--headless", "--no-remote", "--profile", profile, url]
    with open(os.path.join(out, "firefox.log"), "w") as browser_log:
        browser = subprocess.Popen(command, env=env, stdout=browser_log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            finished = handler.done.wait(play_delay + max_play / 1000 + 90)
        finally:
            try:
                os.killpg(browser.pid, signal.SIGTERM)
                browser.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(browser.pid, signal.SIGKILL)
                browser.wait()
            except ProcessLookupError:
                pass
            server.shutdown()
            server.server_close()
            log.close()
            dumps = sorted(name for _, _, names in os.walk(profile)
                           for name in names if name.endswith(".dmp"))
            if not keep_profile:
                shutil.rmtree(profile, ignore_errors=True)

    record = handler.record if finished else {"finished": "harness-timeout"}
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(record, f)
    result = verdict.evaluate(record, video, reference)
    if dumps:
        result["findings"].append({"kind": "crash", "dumps": len(dumps)})
    played = handler.play_time and handler.play_time - handler.epoch
    delivered = delivery.analyse(video_path, video, handler.requests, handler.page_start,
                                 record, lead, played)
    delivered["raw"] = {"requests": handler.requests, "page_start": handler.page_start,
                        "play_time": played}
    with open(os.path.join(out, "delivery.json"), "w") as f:
        json.dump(delivered, f)
    result["delivery"] = delivered["summary"]
    if finished:
        result["findings"] += verdict.unexpected(spec.get("expect", {}), result)
    result["userAgent"] = record.get("userAgent")
    with open(os.path.join(out, "verdict.json"), "w") as f:
        json.dump(result, f, indent=1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--firefox", required=True, help="Firefox binary to test")
    parser.add_argument("--out", required=True, help="directory for this case")
    parser.add_argument("--case", help="run this spec.json; the flags that make a "
                        "spec are then ignored")
    parser.add_argument("--media-dir", default=MEDIA_DIR,
                        help="directory holding the test videos (default: %(default)s)")
    parser.add_argument("--clip-dir", default=CLIP_DIR,
                        help="where generated clips are cached")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--video", help="name of the video in --media-dir to play "
                        "(default: the only video there)")
    source.add_argument("--generate", nargs="*", metavar="NAME=VALUE",
                        help="play a synthetic clip made with these make_clip.py "
                        "settings, e.g. container=webm gop=48 duration=10")
    shaped = parser.add_mutually_exclusive_group()
    shaped.add_argument("--rate", nargs="+", metavar="NAME=VALUE",
                        help="each request gets `kbps` kilobits a second, or `x` times "
                        "the clip's average bitrate, after `ttfb` seconds")
    shaped.add_argument("--steady", nargs="+", metavar="NAME=VALUE",
                        help="timed from play(): keep the download `lead` seconds "
                        "ahead, gaining `rate` - 1 s of lead a second")
    shaped.add_argument("--dip", nargs="+", metavar="NAME=VALUE",
                        help="timed from play(): lead `start` s, falling to `low` when "
                        "the playhead reaches `at`, then at `rate`")
    shaped.add_argument("--outage", nargs="+", metavar="NAME=VALUE",
                        help="timed from play(): `lead` s ahead until `at` s, nothing "
                        "for `length` s, then at `rate`")
    parser.add_argument("--no-ranges", action="store_true",
                        help="the server ignores Range and always answers 200")
    parser.add_argument("--no-length", action="store_true",
                        help="the server sends no length, like a live stream")
    parser.add_argument("--actions", metavar="JSON", default="[]",
                        help='page actions, e.g. \'[{"at": {"media": 5}, "do": "seek", '
                        '"to": 12}]\'; also pause, play, rate (value), busy (value)')
    parser.add_argument("--play-delay", type=float, default=3.0,
                        help="seconds between the first frame loading and play()")
    parser.add_argument("--play-on", default="loadeddata",
                        choices=("loadeddata", "loadedmetadata"))
    parser.add_argument("--expect", nargs="*", metavar="NAME=VALUE",
                        help="budgets, e.g. max_waits=0 max_startup_ms=500 "
                        "(see EXPECT in verdict.py)")
    parser.add_argument("--pref", nargs="*", metavar="NAME=VALUE")
    parser.add_argument("--env", nargs="*", metavar="NAME=VALUE",
                        help="extra environment variables for Firefox")
    parser.add_argument("--moz-log", metavar="MODULES",
                        help="record MOZ_LOG, e.g. MediaDecoder:4,MediaFormatReader:4")
    parser.add_argument("--reference", help="output directory of a full-speed run, "
                        "to compare frame coverage with")
    parser.add_argument("--keep-profile", action="store_true")
    args = parser.parse_args()

    try:
        if args.case:
            with open(args.case) as f:
                spec = json.load(f)
        else:
            if args.generate is not None:
                video = {"source": "generate", "settings": parse_pairs(args.generate)}
            else:
                video = {"source": "media", "name": args.video}
            if args.rate:
                shape = {"mode": "rate", "ttfb": 0.05, **parse_pairs(args.rate)}
            elif args.steady:
                shape = schedule.steady(**parse_pairs(args.steady))
            elif args.dip:
                shape = schedule.dip(**parse_pairs(args.dip))
            elif args.outage:
                shape = schedule.outage(**parse_pairs(args.outage))
            else:
                shape = {"mode": "full"}
            if args.no_ranges:
                shape["no_ranges"] = True
            if args.no_length:
                shape["no_length"] = True
            spec = {
                "video": video,
                "delivery": shape,
                "play_delay": args.play_delay,
                "play_on": args.play_on,
                "actions": json.loads(args.actions),
                "expect": parse_pairs(args.expect),
                "prefs": parse_pairs(args.pref),
                "env": {k: str(v) for k, v in parse_pairs(args.env).items()},
                "moz_log": args.moz_log,
            }
    except (ValueError, TypeError) as e:  # bad NAME=VALUE settings
        parser.error(str(e))
    reference = None
    if args.reference:
        with open(os.path.join(args.reference, "verdict.json")) as f:
            reference = json.load(f)
    try:
        result = run_spec(spec, args.out, args.firefox, args.media_dir, args.clip_dir,
                          reference, args.keep_profile)
    except ValueError as e:  # no such video
        parser.error(str(e))

    print(result.get("userAgent"))
    print(json.dumps(result["metrics"]))
    print(json.dumps(result["delivery"]))
    for finding in result["findings"]:
        print(verdict.describe(finding))
    if not result["findings"]:
        print("no findings")
    if any(f["kind"] == "not_ended" for f in result["findings"]):
        sys.exit(2)
    sys.exit(1 if result["findings"] else 0)


if __name__ == "__main__":
    main()
