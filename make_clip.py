#!/usr/bin/env python3
"""Generate a synthetic test clip whose frames carry their own frame number.

Nothing in the clip comes from an existing video: the picture is ffmpeg's
test pattern with noise, so the encoder has to spend its bitrate, and the
audio is a tone with a beep every second.

The top strip of every frame is a barcode: BITS blocks across, the frame
number's bits least significant first, white for 1. The lower half of the
strip repeats the row inverted, so a reader can reject a damaged strip.

`video_duration` (not a default) ends the picture that many seconds in while
the sound goes on to `duration`.

    python make_clip.py --container webm --gop 24 --duration 10"""

import argparse
import hashlib
import json
import os
import subprocess
import threading

BITS = 16
STRIP_FRACTION = 1 / 8

DEFAULTS = {
    "container": "mp4",
    "width": 854,
    "height": 480,
    "fps": 24,
    "duration": 20.0,
    "gop": 96,
    "video_kbps": 1200,
    "audio": True,
    "noise": 30,
}

CODECS = {
    "mp4": {
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
                  "-bf", "2", "-sc_threshold", "0"],
        "audio": ["-c:a", "aac", "-b:a", "96k", "-ac", "2"],
        "mux": ["-movflags", "+faststart"],
    },
    "webm": {
        "video": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                  "-row-mt", "1"],
        "audio": ["-c:a", "libopus", "-b:a", "64k", "-ac", "2"],
        "mux": ["-cues_to_front", "1", "-reserve_index_space", "20000"],
    },
}


def strip_height(height):
    return max(16, int(height * STRIP_FRACTION) // 4 * 4)


def video_duration(p):
    return p.get("video_duration") or p["duration"]


def filter_graph(p):
    strip = strip_height(p["height"])
    size = f"r={p['fps']}:d={video_duration(p)}"
    bit = f"mod(floor(N/pow(2,floor(X*{BITS}/W))),2)"
    lum = f"255*if(lt(Y,H/2),{bit},1-{bit})"
    return (
        f"color=c=black:s={p['width']}x{strip}:{size},format=gray,"
        f"geq=lum='{lum}',format=yuv420p[strip];"
        f"testsrc2=s={p['width']}x{p['height'] - strip}:{size},"
        f"noise=alls={p['noise']}:allf=t,format=yuv420p[body];"
        "[strip][body]vstack=inputs=2[v]"
    )


def make_clip(directory, **overrides):
    """Create the clip described by DEFAULTS plus `overrides` in `directory`,
    unless it is already there. Returns its description, which is also saved
    next to it as JSON."""
    p = {**DEFAULTS, **overrides}
    if p["width"] % 2 or p["height"] % 4:
        raise ValueError("width must be even and height a multiple of 4")
    key = hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10]
    name = f"clip-{key}.{p['container']}"
    path = os.path.join(directory, name)
    info_path = path + ".json"
    if os.path.exists(path) and os.path.exists(info_path):
        with open(info_path) as f:
            return json.load(f)

    os.makedirs(directory, exist_ok=True)
    # Cases running at once may want the same clip: each writes its own file
    # and moves it into place whole.
    partial = os.path.join(directory, f"partial-{os.getpid()}-{threading.get_ident()}-{name}")
    codec = CODECS[p["container"]]
    kbps = p["video_kbps"]
    command = ["ffmpeg", "-v", "error", "-y"]
    if p["audio"]:
        command += ["-f", "lavfi", "-i",
                    f"sine=frequency=440:beep_factor=4:duration={p['duration']}"]
    command += ["-filter_complex", filter_graph(p), "-map", "[v]"]
    if p["audio"]:
        command += ["-map", "0:a"] + codec["audio"]
    command += codec["video"] + [
        "-pix_fmt", "yuv420p", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 5 // 4}k",
        "-bufsize", f"{kbps * 5 // 2}k", "-g", str(p["gop"]),
        "-keyint_min", str(p["gop"]), "-map_metadata", "-1",
    ] + codec["mux"] + [partial]
    subprocess.run(command, check=True)

    info = {
        "file": name,
        "params": p,
        "fps": p["fps"],
        "frames": round(video_duration(p) * p["fps"]),
        "duration": p["duration"],
        "video_duration": video_duration(p),
        "size": os.path.getsize(partial),
        "barcode": {"bits": BITS, "strip": strip_height(p["height"]) / p["height"]},
    }
    with open(partial + ".json", "w") as f:
        json.dump(info, f, indent=1)
    os.replace(partial, path)
    os.replace(partial + ".json", info_path)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-d", "--directory", default="clips")
    parser.add_argument("--container", choices=sorted(CODECS))
    for name in ("width", "height", "fps", "gop", "video_kbps", "noise"):
        parser.add_argument("--" + name.replace("_", "-"), type=int)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--video-duration", type=float)
    parser.add_argument("--no-audio", dest="audio", action="store_false", default=None)
    args = vars(parser.parse_args())
    directory = args.pop("directory")
    info = make_clip(directory, **{k: v for k, v in args.items() if v is not None})
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
