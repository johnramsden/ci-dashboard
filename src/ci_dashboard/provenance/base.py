#!/usr/bin/env python3
"""
Interface every release source implements, and the records they exchange.

A source answers two questions about one published asset (a snap or a charm):

  channels()          what is currently published, and where — cheap, refetched
                      every run because promotions move revisions between
                      channels.
  resolve(channel)    which commit built this revision — potentially expensive
                      (an HTTP range read into a .charm), so collect_releases.py
                      caches the answer forever. A revision's commit is
                      immutable, so a cached Resolution is never stale.

Splitting the two is what keeps a full ceph-charms sweep at seconds instead of
minutes: only revisions never seen before are resolved.

Sources differ in what identifies a table row (a snap has only a track; a charm
also has a base), so each declares its own `row_keys`. Sources collect every
architecture the store publishes; which one a table shows, and which risks
become its columns, are render-time choices driven by config.yaml.
"""

import abc
from dataclasses import dataclass, field

# How a Resolution's commit was established, worst to best. Rendered verbatim
# on the page: a reader must be able to tell an authoritative record from a
# parsed convention.
UNKNOWN = "unknown"                 # no commit could be established
VERSION_STRING = "version string"   # inferred from the snap's version field
GIT_TAG = "git tag"                 # the publish workflow tagged the commit
GIT_INFO = "git-info.txt"           # the build wrote the commit into the artifact

# Store risk levels, most to least stable. The page pivots these into columns,
# so a track's promotion pipeline reads left to right. Not every asset
# publishes to every risk; the renderer emits only the ones in use.
RISKS = ("stable", "candidate", "beta", "edge")


@dataclass(frozen=True)
class Channel:
    """One published revision on one channel, as the store reports it."""

    track: str
    risk: str
    revision: int
    version: str | None = None
    arch: str | None = None
    base: str | None = None
    released_at: str | None = None
    # Where the artifact can be fetched. Needed by sources that read provenance
    # out of the package itself; never serialized (it carries a signed CDN
    # token that expires, so persisting it would be misleading).
    download_url: str | None = field(default=None, compare=False, repr=False)

    def to_dict(self):
        # `arch` is what lets one collection serve every per-architecture table:
        # the renderer filters on it, so a channel must carry the arch it was
        # published for.
        return {
            "track": self.track,
            "risk": self.risk,
            "revision": self.revision,
            "version": self.version,
            "arch": self.arch,
            "base": self.base,
            "released_at": self.released_at,
        }


@dataclass(frozen=True)
class Resolution:
    """The commit a revision was built from, and how we know."""

    commit: str | None = None       # abbreviated or full SHA; None when unknown
    branch: str | None = None       # only sources that record it set this
    provenance: str = UNKNOWN
    note: str = ""                  # why it is unknown, when it is
    # True when the lookup failed for a reason that might not recur — a timeout,
    # a 5xx from the CDN. The collector re-attempts these on the next run
    # instead of caching the failure forever. A definitive answer ("this charm
    # has no git-info.txt") is not retryable: it will never change.
    retryable: bool = False

    @property
    def known(self):
        return self.commit is not None

    def to_dict(self):
        return {
            "commit": self.commit,
            "branch": self.branch,
            "provenance": self.provenance,
            "note": self.note,
            "retryable": self.retryable,
        }


def unknown(note, retryable=False):
    """A Resolution for a revision whose commit could not be established.

    Set retryable when the cause was transient, so the answer is not cached.
    """
    return Resolution(provenance=UNKNOWN, note=note, retryable=retryable)


class ReleaseSource(abc.ABC):
    """One published asset, and the mechanism that maps its revisions to commits.

    Swapping how an asset is resolved — say, moving the snap off version-string
    parsing and onto Launchpad build records — means writing a subclass with a
    new `provenance_type` and naming it in the config. Nothing else moves.
    """

    #: "snap" or "charm" — groups assets on the page.
    kind: str = ""

    #: (heading, Channel attribute) pairs identifying one table row. Risks
    #: become the columns, so these are what remains: a track, and — for
    #: charms, whose revisions differ per base — a base.
    row_keys: tuple[tuple[str, str], ...] = ()

    #: The config's name for this mechanism, e.g. "git-tag". Set by subclasses
    #: and used as the key in sources.PROVENANCE_TYPES.
    provenance_type: str = ""

    #: Whether collect_releases.py may reuse a stored Resolution forever. True
    #: for anything that costs a network round trip. A source whose resolve() is
    #: a pure, free function of the Channel sets False, so a config change to
    #: how it resolves (say, a new version_pattern) reaches every published
    #: revision on the next run instead of only revisions never seen before.
    cache_resolutions: bool = True

    def __init__(self, name, repo, collapsed=False):
        self.name = name        # the asset's name in its store, e.g. "ceph-mon"
        self.repo = repo        # owner/name on GitHub, for commit links
        # Render this asset inside a closed <details>. Set on the nine
        # ceph-charms, which would otherwise bury the two headline tables.
        self.collapsed = collapsed

    @property
    def slug(self):
        """Unique, filesystem- and JSON-key-safe id. The snap and the charm are
        both called "microceph", so the kind has to be part of it."""
        return f"{self.kind}-{self.name}"

    @abc.abstractmethod
    def channels(self):
        """Every currently-published (channel, revision) pair, as Channels."""

    @abc.abstractmethod
    def resolve(self, channel):
        """The Resolution for one Channel's revision. Called once per revision."""

    def __repr__(self):
        return f"<{type(self).__name__} {self.slug}>"
