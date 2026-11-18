#!/usr/bin/env python3
"""Static file server like `python -m http.server`, plus HTTP Range support
and optional slowness: a fixed time to first byte and a transfer rate per
request. Also useful on its own, to watch a throttled video in a normal
Firefox:

    python range_server.py 8000 -d media --rate 200 --delay 0.5"""

import argparse
import os
import re
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
CHUNK_SIZE = 16 * 1024


class RangeRequestHandler(SimpleHTTPRequestHandler):
    # Seconds before each response, and bytes per second after it (0: no limit).
    delay = 0.0
    rate = 0
    # Server misbehaviours: ignore Range and always answer 200, and leave out
    # the length so each response ends by closing the connection.
    accept_ranges = True
    send_length = True

    def send_head(self):
        self.remaining = None
        time.sleep(self.delay)

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        stat = os.fstat(f.fileno())
        size = stat.st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK

        match = RANGE_RE.match(self.headers.get("Range", "").strip())
        if self.accept_ranges and match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                if match[2]:
                    end = min(int(match[2]), size - 1)
            else:
                start = max(size - int(match[2]), 0)
            if start >= size or start > end:
                f.close()
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            status = HTTPStatus.PARTIAL_CONTENT

        self.send_response(status)
        self.send_header("Content-Type", self.guess_type(path))
        if self.accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if self.send_length:
            self.send_header("Content-Length", str(end - start + 1))
        else:
            self.close_connection = True
        if status == HTTPStatus.PARTIAL_CONTENT:
            total = size if self.send_length else "*"
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.end_headers()

        f.seek(start)
        self.start = start
        self.remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        if self.remaining is None:
            return super().copyfile(source, outputfile)
        total = self.remaining
        sent = 0
        began = time.monotonic()
        try:
            while sent < total:
                if self.rate:
                    wait = began + sent / self.rate - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                chunk = source.read(min(CHUNK_SIZE, total - sent))
                if not chunk:
                    return
                outputfile.write(chunk)
                outputfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("client closed after %d of %d bytes", sent, total)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", type=int, default=8000)
    parser.add_argument("-d", "--directory", default=os.getcwd())
    parser.add_argument("-b", "--bind", default="127.0.0.1")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds to wait before answering each request")
    parser.add_argument("--rate", type=float, default=0,
                        help="transfer rate per request in KiB/s (0 = unlimited)")
    parser.add_argument("--no-ranges", action="store_true",
                        help="ignore Range headers and always answer 200")
    parser.add_argument("--no-length", action="store_true",
                        help="leave out Content-Length and end each response "
                        "by closing the connection")
    args = parser.parse_args()

    RangeRequestHandler.delay = args.delay
    RangeRequestHandler.rate = int(args.rate * 1024)
    RangeRequestHandler.accept_ranges = not args.no_ranges
    RangeRequestHandler.send_length = not args.no_length
    handler = partial(RangeRequestHandler, directory=args.directory)
    with ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        print(f"Serving {args.directory} on http://{args.bind}:{args.port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
