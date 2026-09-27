#!/usr/bin/env python3
"""
Read `git-info.txt` out of a published .charm without downloading it.

A .charm is a zip. Its central directory sits at the end, so a seekable file
over HTTP range requests lets zipfile pull just the directory and the one entry
we want — about 450 KB instead of the 15-150 MB package.

Used by provenance/sources.py (the release collector) and, as public API, by
other tools that read the same file, such as ceph-qa's
scripts/charm-build-info.py. Stdlib only.
"""

import io
import tempfile
import urllib.request
import zipfile
from pathlib import Path

GIT_INFO_FILE = "git-info.txt"


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over a URL using HTTP Range requests."""

    def __init__(self, url):
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.url = resp.geturl()  # resolve the 302 to storage once
            self.size = int(resp.headers["Content-Length"])
        self.pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}
        self.pos = base[whence] + offset
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, buf):
        end = min(self.pos + len(buf), self.size) - 1
        if end < self.pos:
            return 0
        headers = {"Range": f"bytes={self.pos}-{end}"}
        req = urllib.request.Request(self.url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            partial = resp.status == 206
        if not partial:
            # A proxy or CDN may ignore Range and return the whole object with
            # a 200. Slice out what was asked for rather than overflowing buf.
            data = data[self.pos:end + 1]
        data = data[:len(buf)]
        buf[: len(data)] = data
        self.pos += len(data)
        return len(data)


def parse_git_info(raw):
    """Parse 'key: value' lines from git-info.txt into a dict."""
    info = {}
    for line in raw.decode(errors="replace").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def read_git_info_from_zip(zf):
    try:
        return parse_git_info(zf.read(GIT_INFO_FILE))
    except KeyError:
        return None  # older build without git-info.txt


def fetch_git_info(url, allow_full_download=False):
    """Parsed git-info dict, or None when the charm has no git-info.txt.

    Raises on network failure. `allow_full_download` retries by pulling the
    whole package when the range read fails — the CLI wants that, the daily
    collector does not (a 150 MB download per revision would dominate the run).
    """
    try:
        ranged = io.BufferedReader(HttpRangeFile(url), buffer_size=128 * 1024)
        with zipfile.ZipFile(ranged) as zf:
            return read_git_info_from_zip(zf)
    except KeyboardInterrupt:
        raise
    except Exception:
        if not allow_full_download:
            raise

    with tempfile.TemporaryDirectory(prefix="charmzip-") as tmpdir:
        charm_path = Path(tmpdir) / "download.charm"
        urllib.request.urlretrieve(url, charm_path)
        with zipfile.ZipFile(charm_path) as zf:
            return read_git_info_from_zip(zf)
