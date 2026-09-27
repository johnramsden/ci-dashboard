#!/usr/bin/env python3
"""
Release provenance collector: which commit built each published revision.

Builds the assets named in the config's `releases:` block, asks each what it
currently publishes, and resolves the commit behind every revision it has not
seen before.

Storage is one ledger, not a dated snapshot per day like collect.py's CI
metrics. A flaky rate genuinely changes daily; a revision's commit does not —
revision 1735 was built from cf306793a4 and always will be. So `revisions` is
append-only and consulted as a cache, while `channels` (which revision each
track/risk points at) is recomputed every run, because promotions move it.

That cache is what makes a daily run affordable: resolving a ceph-charms
revision costs an HTTP range read into the .charm, so a cold run takes minutes
and every subsequent one takes seconds.

The ledger stores data, never presentation. Every architecture the stores
publish is collected. Which one a table opens on, which risks become its
columns, how old is too old, and which assets exist at all live in the config
and are applied at render time — so editing the config reshapes the whole page
without re-fetching anything, the same way render/health.py applies job
excludes.

Environment:
  GH_TOKEN - GitHub token, for reading `git-tag` repositories' tags (falls
             back to GITHUB_TOKEN). Anonymous works but shares a 60/hour
             budget.

Usage:
  ci-dashboard collect-releases --config FILE --output-dir SITE
                      [--source SLUG] [--now ISO8601]

The ledger lands at SITE/releases-data/releases.json. It is deliberately not
under SITE/data/, whose loader treats every *.json as a dated CI snapshot and
raises on anything whose schema_version is not its own.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .config import ConfigError, load_config
from .provenance import build_sources, unknown

# 2: risks pivoted into columns; a source stored `row_keys` and `collapsed`.
# 3: presentation moved to config.yaml, so a source stores data only —
#    collected_at, channels, revisions.
# 4: channels carry every architecture the store publishes, not just one.
#    Which one a table shows is chosen at render time, per table.
SCHEMA_VERSION = 4

LEDGER_DIR = "releases-data"
LEDGER_NAME = "releases.json"


def ledger_path(output_dir):
    return os.path.join(output_dir, LEDGER_DIR, LEDGER_NAME)


def load_ledger(output_dir):
    """(cached sources, discarded) for the existing ledger.

    Everything in it is a cache that can be rebuilt from the stores, so a
    ledger that is corrupt, truncated, or written by another schema version is
    discarded rather than half-migrated or allowed to crash the run.

    `discarded` distinguishes "a ledger was there and we threw it away" from
    "there was never one". They look identical to a full run, which rebuilds
    everything either way — but a `--source` run must not turn the first case
    into a ledger holding one asset and nothing else.
    """
    path = ledger_path(output_dir)
    try:
        with open(path) as f:
            ledger = json.load(f)
    except FileNotFoundError:
        return {}, False
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"{path}: unreadable ({exc}); rebuilding from scratch.",
              file=sys.stderr)
        return {}, True
    if not isinstance(ledger, dict) or ledger.get("schema_version") != SCHEMA_VERSION:
        found = ledger.get("schema_version") if isinstance(ledger, dict) else None
        print(f"{path}: schema_version {found!r} is not {SCHEMA_VERSION}; "
              "rebuilding from scratch.", file=sys.stderr)
        return {}, True
    sources = ledger.get("sources")
    return (sources, False) if isinstance(sources, dict) else ({}, True)


def _numeric_keys(source, cached_revisions):
    """Cached (revision, resolution) pairs whose key is a revision number.

    Revision keys are ints stringified on the way into JSON. A key that is not
    would make `int(r)` below throw, and since a throwing source keeps its
    cached entry verbatim, the asset would be wedged on every future run with
    no way to self-heal. Drop the bad key instead; it will be re-resolved.
    """
    for revision, resolution in cached_revisions.items():
        try:
            int(revision)
        except (TypeError, ValueError):
            print(f"{source.slug}: dropping non-numeric revision key {revision!r}",
                  file=sys.stderr)
            continue
        if not isinstance(resolution, dict):
            print(f"{source.slug}: dropping revision {revision} — its cached "
                  f"resolution is {type(resolution).__name__}, not a mapping",
                  file=sys.stderr)
            continue
        yield revision, resolution


def collect_source(source, cached_revisions, now):
    """One asset's channels, plus a resolution for every revision it publishes.

    cached_revisions maps a revision number (as a string, JSON's only key type)
    to a previously resolved dict. Revisions already there are not re-fetched —
    except ones whose last lookup failed transiently, which are retried.

    `collected_at` records when this asset last talked to its store. A source
    carried forward after a failure keeps its old stamp, so the page can say
    which tables are stale rather than dating them all from the current run.
    """
    channels = source.channels()
    revisions = {rev: res for rev, res in _numeric_keys(source, cached_revisions)
                 if not res.get("retryable")}

    # A source whose resolve() is free re-resolves everything it publishes, so
    # a change to how it resolves is never masked by an older cached answer.
    cached = ({int(r) for r in revisions} if source.cache_resolutions else set())
    todo = sorted({c.revision for c in channels} - cached)
    if todo and not source.cache_resolutions:
        print(f"{source.slug}: re-resolving {len(todo)} published revision(s)")
    elif todo:
        print(f"{source.slug}: resolving {len(todo)} new revision(s): "
              f"{', '.join(str(r) for r in todo)}")
    else:
        print(f"{source.slug}: {len(channels)} channel(s), nothing new to resolve")

    # One Channel per revision is enough: a revision's commit does not depend
    # on which channel it happens to sit on.
    by_revision = {}
    for channel in channels:
        by_revision.setdefault(channel.revision, channel)

    for revision in todo:
        try:
            resolution = source.resolve(by_revision[revision])
        except Exception as exc:
            # Checkpoint per revision. A ceph-charm resolves 20 revisions with
            # 20 range reads; letting the 15th failure discard the first 14
            # would mean a flaky CDN never lets the asset finish. Retryable, so
            # the next run picks it up.
            print(f"  rev {revision}: error ({exc})", file=sys.stderr)
            resolution = unknown(f"resolving raised: {exc}", retryable=True)
        resolved = resolution.to_dict()
        # A re-resolved revision that is still unknown for the same reason
        # was reported the first time; do not repeat it every day.
        if not resolution.known and revisions.get(str(revision)) != resolved:
            print(f"  rev {revision}: unknown — {resolution.note}")
        revisions[str(revision)] = resolved

    return {
        "collected_at": fmt_time(now),
        "channels": [c.to_dict() for c in channels],
        "revisions": revisions,
    }


def fmt_time(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _placeholder(source):
    """An asset that has never been collected successfully.

    collected_at is None, which the renderer reads as "never collected" and
    says so, rather than dropping the asset off the page.
    """
    return {"collected_at": None, "channels": [], "revisions": {}}


def build_ledger(sources, cached, now, keep_uncollected=False):
    """Collect `sources` into a ledger dict, plus the slugs that failed.

    keep_uncollected carries forward assets that were not collected this run.
    A full run leaves it False, so an asset removed from the config's
    `releases.groups` disappears from the site; a `--source` debugging run sets
    it, so poking at one asset does not wipe the others.

    A source that raises keeps its previous rows *and its previous
    collected_at*: the ledger's own generated_at moves every run, so reusing it
    for a carried-forward asset would date stale rows as fresh. A source that
    raises before it ever succeeded has no previous rows, so it gets an empty
    placeholder — otherwise it would be missing from the ledger entirely, and
    the page would render nothing at all for it, indistinguishable from an
    asset nobody ever configured.
    """
    out = dict(cached) if keep_uncollected else {}
    failed = []
    for source in sources:
        previous = cached.get(source.slug, {}).get("revisions", {})
        try:
            out[source.slug] = collect_source(source, previous, now)
        except Exception as exc:  # one asset failing must not lose the others
            print(f"Error collecting {source.slug}: {exc}", file=sys.stderr)
            failed.append(source.slug)
            out[source.slug] = cached.get(source.slug) or _placeholder(source)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": fmt_time(now),
        "sources": out,
    }, failed


def write_ledger(ledger, output_dir):
    dest = ledger_path(output_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(ledger, f, indent=1, sort_keys=True)
            f.write("\n")
        os.rename(tmp, dest)
    except BaseException:
        # A full disk or a non-serializable value would otherwise leave a
        # partial .tmp next to a good ledger. The rename is what publishes.
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    return dest


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description="Collect release provenance.")
    parser.add_argument("--config", required=True, help="Path to the dashboard config file")
    parser.add_argument("--output-dir", required=True,
                        help=f"Site root; the ledger goes to "
                             f"<output-dir>/{LEDGER_DIR}/{LEDGER_NAME}")
    parser.add_argument("--source", help="Collect only this slug (debugging)")
    parser.add_argument("--now", help="ISO-8601 override of the clock (tests)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    all_sources = build_sources(config["releases"])

    sources = all_sources
    if args.source:
        # Checked before the "nothing configured" exit below: an explicit
        # --source that cannot be satisfied is an error, never a quiet no-op.
        sources = [s for s in all_sources if s.slug == args.source]
        if not sources:
            known = ", ".join(s.slug for s in all_sources) or "none configured"
            print(f"Error: no source {args.source!r}. Known: {known}",
                  file=sys.stderr)
            sys.exit(1)

    if not all_sources:
        print(f"{args.config} configures no releases; nothing to collect.")
        return

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(timezone.utc))

    cached, discarded = load_ledger(args.output_dir)
    if args.source and discarded:
        # There is nothing to carry forward, so writing now would replace a
        # whole ledger with one asset. A full run rebuilds everyone instead.
        print(f"Error: {ledger_path(args.output_dir)} was discarded (see above), "
              f"so a --source run would leave {args.source!r} as the only asset "
              "in it. Run without --source to rebuild first.", file=sys.stderr)
        sys.exit(1)

    ledger, failed = build_ledger(sources, cached, now,
                                  keep_uncollected=bool(args.source))
    print(f"Wrote {write_ledger(ledger, args.output_dir)}")

    if failed:
        print(f"Error: collection failed for: {', '.join(failed)}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
