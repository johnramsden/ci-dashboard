#!/usr/bin/env python3
"""
One ReleaseSource per published asset; the config says which and how.

Three mechanisms, keyed in the config by `provenance:`

  version-string    the snap's version carries the commit — by default a
                    `+snap<sha>` suffix, as snapcraft.yaml sets it from
                    `git describe --always`; `version_pattern` overrides it.
                    Inferred, not metadata — see SnapStoreSource.
  git-tag           the publish workflow tags the commit it built.
  git-info          the build writes git-info.txt into the .charm.

Teaching an asset a new mechanism means adding a subclass with a
`provenance_type` and naming it in the config. Adding an asset means adding
its name to a `releases.groups[].assets` list.
"""

import json
import re
import subprocess
import sys

from .base import (GIT_INFO, GIT_TAG, VERSION_STRING, Channel, ReleaseSource,
                   Resolution, unknown)
from .charmzip import fetch_git_info
from .fetch import charm_info, gh_paginate, snap_info

#: Seconds a user-supplied version_pattern may spend on one version string.
PATTERN_TIMEOUT = 2.0

# Runs in a child process: prints [matched, group 1] for (pattern, text) on stdin.
_SEARCH = ("import json,re,sys;p,t=json.load(sys.stdin);m=re.search(p,t);"
           "print(json.dumps([m is not None, m and m.group(1)]))")


def bounded_search(pattern, text, timeout=PATTERN_TIMEOUT):
    """(matched, group 1) of re.search(pattern, text), in a child process
    killed after `timeout` seconds; raises TimeoutError past it.

    A pattern from the config can backtrack catastrophically — nested
    quantifiers like `([0-9a-f]+)+` are an easy mistake — and CPython's `re`
    holds the GIL for the whole match, so neither a thread nor a signal
    handler can interrupt it. A process can always be killed.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _SEARCH],
            input=json.dumps([pattern, text]), capture_output=True, text=True,
            timeout=timeout, check=True)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"version_pattern took over {timeout:g}s") from exc
    matched, group = json.loads(proc.stdout)
    return matched, group


# The architecture a table opens on when the config names none. Every table is
# one arch at a time: revisions, and sometimes commits, genuinely diverge across
# them — ceph-mon squid/stable on 24.04 is e67ab37a on amd64 but a9b45088 on
# arm64 — so mixing would put contradictory rows next to each other. Sources
# collect them all and the renderer picks.
DEFAULT_ARCH = "amd64"


class SnapStoreSource(ReleaseSource):
    """A snap: parse the commit out of the version string.

    The default pattern fits microceph, whose snap/snapcraft.yaml builds the
    version as

        craftctl set version=${pkg_version}+snap$(git describe --always --dirty --abbrev=10)

    and because no tag is reachable from the release branches, `git describe`
    falls through to `--always` and emits a bare 10-char SHA. The Snap Store
    exposes no commit field of its own, so this is all there is.

    It is a convention, not metadata: a reachable annotated tag would turn the
    suffix into `<tag>-<n>-g<sha>`, and a dirty tree appends `-dirty`, in which
    case the commit does not identify the artifact anyway. Both fail the regex
    and report unknown rather than guessing. The quincy track's older
    `0+git.<sha>` scheme is deliberately not supported.

    Another snap names its own convention with the config's `version_pattern`:
    a regex searched in the version, whose one group captures the commit.
    Whatever it captures must still look like a commit hash, or the revision
    is unknown — a loose pattern must not turn a version number into a link.

    Resolving is a regex over a string the store already returned, so nothing
    is cached: a corrected pattern re-resolves every published revision. A
    configured pattern runs under bounded_search(), so one that backtracks
    catastrophically reports its revisions unknown instead of hanging the run.
    """

    kind = "snap"
    provenance_type = "version-string"
    # A snap has no base, so within one architecture a track is the whole key.
    row_keys = (("Track", "track"),)
    cache_resolutions = False

    #: Anchored: a suffix match would happily accept the legacy `0+git.<sha>`
    #: scheme's trailing hex too.
    VERSION_RE = re.compile(r"\+snap([0-9a-f]{7,10})\Z")

    #: What a captured commit must look like: an abbreviated or full SHA-1
    #: (compared lowercased; GitHub resolves either case).
    COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")

    def __init__(self, *args, version_pattern=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.version_pattern = version_pattern
        # Set once a configured pattern times out: every later revision would
        # time out too, so they are reported without paying for it again.
        self._pattern_timed_out = False

    def _search(self, version):
        """(matched, captured commit or None) for one version string."""
        if not self.version_pattern:
            match = self.VERSION_RE.search(version)
            return match is not None, match and match.group(1)
        return bounded_search(self.version_pattern, version)

    def channels(self):
        # `channel` is not a selectable field — the store always nests one in
        # every channel-map entry, and rejects the name outright with a 400.
        data = snap_info(self.name, ["revision", "version"])
        found = []
        for entry in data.get("channel-map", []):
            channel = entry["channel"]
            found.append(Channel(
                track=channel["track"],
                risk=channel["risk"],
                revision=entry["revision"],
                version=entry["version"],
                arch=channel["architecture"],
                released_at=channel.get("released-at"),
            ))
        return found

    def resolve(self, channel):
        slow = (f"version_pattern {self.version_pattern} takes over "
                f"{PATTERN_TIMEOUT:g}s to match; it likely backtracks catastrophically")
        if self._pattern_timed_out:
            return unknown(slow)
        try:
            matched, commit = self._search(channel.version or "")
        except TimeoutError:
            self._pattern_timed_out = True
            return unknown(slow)
        if not matched:
            if self.version_pattern:
                return unknown(f"version {channel.version!r} does not match "
                               f"version_pattern {self.version_pattern}")
            return unknown(f"version {channel.version!r} is not <pkg>+snap<sha>")
        if not commit or not self.COMMIT_RE.match(commit.lower()):
            return unknown(f"version {channel.version!r}: captured {commit!r}, "
                           "which is not a commit hash")
        return Resolution(commit=commit.lower(), provenance=VERSION_STRING)


class CharmTagSource(ReleaseSource):
    """charm-microceph: the publish workflow's git tag names the revision.

    canonical/charming-actions/upload-charm tags the commit it built as
    `rev<N>`, optionally prefixed with the track (`squid-rev295`). Both forms
    are live: 141 bare, 21 prefixed at the time of writing.

    Authoritative where it exists, but not universal — a publish that ran
    without `contents: write` uploaded a revision and tagged nothing. Those
    revisions resolve to unknown and stay that way; the charm carries no
    git-info.txt to fall back on.
    """

    kind = "charm"
    provenance_type = "git-tag"
    row_keys = (("Track", "track"), ("Base", "base"))

    TAG_RE = re.compile(r"\A(?:[a-z]+-)?rev(\d+)\Z")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tag_cache = None

    def channels(self):
        return _charm_channels(self.name)

    def _tags(self):
        """revision number -> commit SHA, for every rev tag in the repo.

        /repos/:repo/tags, not /git/refs/tags: the latter ignores `page`
        entirely and returns the whole ref list every time, so paging it never
        terminates. This one pages properly, and its `commit.sha` is already
        dereferenced for annotated tags.
        """
        if self._tag_cache is not None:
            return self._tag_cache
        # revision -> {sha: [tag names]}. Both `rev300` and `tentacle-rev300`
        # are legal names for revision 300; if they ever disagree about the
        # commit, resolve() must not silently pick whichever the API listed last.
        tags = {}
        for tag in gh_paginate(f"/repos/{self.repo}/tags?"):
            match = self.TAG_RE.match(tag["name"])
            if match:
                by_sha = tags.setdefault(int(match.group(1)), {})
                by_sha.setdefault(tag["commit"]["sha"], []).append(tag["name"])
        self._tag_cache = tags
        return tags

    def resolve(self, channel):
        by_sha = self._tags().get(channel.revision)
        if not by_sha:
            return unknown(f"no rev{channel.revision} tag in {self.repo}")
        if len(by_sha) > 1:
            names = sorted(n for names in by_sha.values() for n in names)
            return unknown(f"tags {', '.join(names)} disagree about which "
                           f"commit built revision {channel.revision}")
        commit, = by_sha
        return Resolution(commit=commit, provenance=GIT_TAG)


class CharmGitInfoSource(ReleaseSource):
    """ceph-charms: the build writes git-info.txt into the .charm.

    Each charm's charmcraft.yaml has a `git-info` part recording commit,
    branch, and commit_date. Read with HTTP range requests so only the zip
    central directory and that one entry cross the wire.

    Authoritative, but absent from many revisions — coverage tracks the build
    pipeline, not the calendar, so a recent revision may lack what an older one
    has.
    """

    kind = "charm"
    provenance_type = "git-info"
    row_keys = (("Track", "track"), ("Base", "base"))

    def channels(self):
        return _charm_channels(self.name)

    def resolve(self, channel):
        if not channel.download_url:
            # Charmhub always carries one; its absence is a bad response, not a
            # fact about the revision. Cache it and the revision can never
            # resolve again, so retry instead.
            return unknown("charmhub reported no download url", retryable=True)
        try:
            info = fetch_git_info(channel.download_url)
        except Exception as exc:                      # network, CDN, bad zip
            # Might be a flaky CDN rather than a real absence — retry tomorrow
            # rather than caching a wrong "unknown" for this revision forever.
            return unknown(f"could not read the charm: {exc}", retryable=True)
        if info is None:
            return unknown("charm has no git-info.txt")
        commit = info.get("commit")
        if not commit:
            return unknown("git-info.txt has no commit line")
        return Resolution(commit=commit, branch=info.get("branch"),
                          provenance=GIT_INFO)


def _charm_channels(name):
    """Charmhub's channel map for one charm, every architecture."""
    data = charm_info(name, ["channel-map"])
    found = []
    for entry in data.get("channel-map", []):
        channel, revision = entry["channel"], entry["revision"]
        found.append(Channel(
            track=channel["track"],
            risk=channel["risk"],
            revision=revision["revision"],
            arch=channel["base"]["architecture"],
            base=channel["base"]["channel"],
            released_at=channel.get("released-at"),
            download_url=revision.get("download", {}).get("url"),
        ))
    return found


#: The config's `provenance:` values -> the class that implements them.
#: config.py validates against these keys, so a typo is caught at load.
PROVENANCE_TYPES = {
    cls.provenance_type: cls
    for cls in (SnapStoreSource, CharmTagSource, CharmGitInfoSource)
}


def build_sources(releases_config):
    """Every asset the Releases page shows, in config order.

    `releases_config` is the config's validated `releases:` block, or None
    when the file configures no releases at all — in which case there is no
    Releases page and this returns nothing.
    """
    if not releases_config:
        return []
    sources = []
    for group in releases_config["groups"]:
        cls = PROVENANCE_TYPES[group["provenance"]]
        # Only version-string takes a pattern; config.py rejects it elsewhere.
        extra = ({"version_pattern": group["version_pattern"]}
                 if group.get("version_pattern") else {})
        for name in group["assets"]:
            sources.append(cls(name, repo=group["repo"],
                               collapsed=group["collapsed"], **extra))
    return sources
