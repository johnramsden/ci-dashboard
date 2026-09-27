#!/usr/bin/env python3
"""
The Releases page: each published snap/charm revision, and the commit behind it.

Reads the ledger collect_releases.py writes, plus the config's `releases:`
block. The ledger holds only data — channels for every architecture, and
resolved revisions. Which risks to show and in what order, which architecture a
table opens on, how old is too old, and which assets exist at all come from the
config and are applied here, so editing the config reshapes the page without
re-fetching anything. That mirrors how the health pages apply job excludes at
render time rather than at collection.

Risks become the table columns, so a track's promotion pipeline reads left to
right and an edge revision trailing its stable one stands out. A row key is
(track) for snaps and (track, base) for charms: one (track, risk) genuinely
spans several bases at different revisions — ceph-mon squid/stable is revision
491 on 22.04 and 498 on 24.04, built from different branches.

Architecture is the third axis, and it is a table rather than a column:
revisions are numbered per architecture and their commits can differ, so a
section carries one table per architecture and a <select> switches between them.

Nothing in this module knows about flaky rates.
"""

import json
from datetime import datetime, timedelta, timezone

from .. import collect_releases
from .. import provenance

from . import fmt_timestamp, plural

# Provenance -> Vanilla status-label modifier. A reader has to be able to tell
# a commit the build system recorded from one parsed out of a version string.
PROVENANCE_LABEL = {
    provenance.GIT_TAG: "positive",
    provenance.GIT_INFO: "positive",
    provenance.VERSION_STRING: "information",
    provenance.UNKNOWN: "caution",
}

# Commits arrive at three widths: 10 chars from a snap version string, 12 from
# git-info.txt, 40 from a git tag. Display one width.
COMMIT_ABBREV = 10


def parse_iso(value):
    """An aware UTC datetime from any timestamp the stores or the ledger emit.

    They do not agree: the ledger writes `2026-07-09T12:00:00Z`, while both
    api.snapcraft.io and api.charmhub.io write
    `2026-07-06T14:41:16.990050+00:00`. Comparing those as strings is wrong —
    `.` sorts below `Z` — so everything is parsed. Returns None for anything
    unparseable, which callers must treat as "age unknown", never as "old".
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"   # fromisoformat rejects Z before 3.11
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def active_cutoff(generated_at, max_age_days):
    """The oldest release instant a channel may carry and still be shown.

    Derived from the ledger's own timestamp, never the wall clock, so
    re-rendering unchanged data stays byte-identical.
    """
    stamp = parse_iso(generated_at)
    if stamp is None:
        raise RuntimeError(
            f"release ledger: unparseable generated_at {generated_at!r}")
    return stamp - timedelta(days=max_age_days)


def _is_active(channel, cutoff):
    """False for a channel nothing has been released to since `cutoff`.

    Charmhub and the Snap Store never retire a channel, so long-dead tracks
    (luminous, mimic) and EOL interim bases linger forever. A channel whose
    released_at is missing or unparseable is kept: unknown age is not evidence
    of staleness, and silently hiding a row is worse than showing a stale one.
    """
    released_at = parse_iso(channel.get("released_at"))
    return released_at is None or released_at >= cutoff


def _row_is_active(channels, cutoff):
    """True when any risk in this row has seen a release since `cutoff`.

    The cutoff has to apply to the whole row, not to each cell. A row is
    (track, base); its risks are the columns. Dropping one stale cell out of a
    live row would render it as `·` — which the page's legend defines as
    "nothing is published on that risk" — turning a merely old revision into a
    claim that none exists. ceph-dashboard quincy/20.04 is exactly that: a
    stable revision from 2022 beneath a candidate and edge from last year.
    """
    return any(_is_active(channel, cutoff) for channel in channels)


def _release_cell(source, entry, channel):
    """One (row, risk) cell: the revision on that risk, and its commit."""
    if channel is None:
        return None
    resolution = entry["revisions"].get(str(channel["revision"]), {})
    commit = resolution.get("commit")
    prov = resolution.get("provenance", provenance.UNKNOWN)

    # The tooltip carries everything the pivot has no column for: the snap's
    # version string, the charm's build branch, and why a cell is unresolved.
    detail = [f"revision {channel['revision']}"]
    if channel.get("version"):
        detail.append(channel["version"])
    if commit:
        detail.append(prov)
        if resolution.get("branch"):
            detail.append(f"branch {resolution['branch']}")
    elif resolution.get("note"):
        detail.append(resolution["note"])

    return {
        "revision": channel["revision"],
        "commit": commit[:COMMIT_ABBREV] if commit else None,
        "commit_url": (f"https://github.com/{source.repo}/commit/{commit}"
                       if commit else None),
        "provenance": prov,
        "title": " · ".join(detail),
    }


def release_pivot(source, entry, arch, risks, cutoff):
    """Pivot one architecture's channels into (row key) x (risk) cells.

    One table is one architecture. Revisions are numbered per architecture, and
    the commits behind them can differ too — ceph-mon squid/stable on 24.04 is
    e67ab37a on amd64 but a9b45088 on arm64, because arm64 was rebuilt hours
    later from a newer commit. Mixing them in one table would put contradictory
    rows side by side, so `arch` selects before anything else happens.

    `risks` is the config's list, in display order. Grouping and the activity
    check then run over *every* risk the asset publishes to, not just the
    configured ones — filtering first would make a row's liveness depend on
    which columns the config happens to show, so a row whose only recent release
    is on an unshown risk would be hidden as "nothing released since <cutoff>",
    which is simply false. Configured risks are selected last, when cells are
    built.

    A row is hidden only when *every* one of its risks is past `cutoff`; a live
    row keeps its older cells, so `·` always means "not published" and never
    "published, but too old to show".

    Nothing vanishes untold. Channels dropped for age are counted in `hidden`;
    channels dropped for their risk are counted in `omitted_channels`, their
    risks named in `omitted_risks`, and any revision reachable *only* through
    them is counted in `omitted_revisions`.
    """
    wanted = set(risks)

    everything = {}
    for channel in entry["channels"]:
        if channel.get("arch") != arch:
            continue
        key = tuple(channel.get(attr) for _, attr in source.row_keys)
        everything.setdefault(key, {})[channel["risk"]] = channel

    grid, hidden = {}, 0
    omitted_risks, omitted_channels = set(), []
    for key, cells in everything.items():
        if not _row_is_active(cells.values(), cutoff):
            hidden += len(cells)
            continue
        shown = {}
        for risk, channel in cells.items():
            if risk in wanted:
                shown[risk] = channel
            else:
                omitted_risks.add(risk)
                omitted_channels.append(channel)
        # A row whose every risk is unshown contributes no row, only counts.
        if shown:
            grid[key] = shown

    seen = {risk for cells in grid.values() for risk in cells}
    columns = [risk for risk in risks if risk in seen]

    rows = []
    for key in sorted(grid, key=lambda k: tuple(part or "" for part in k)):
        rows.append({
            "key": [part or "—" for part in key],
            "cells": [_release_cell(source, entry, grid[key].get(risk))
                      for risk in columns],
        })

    visible = {channel["revision"] for cells in grid.values()
               for channel in cells.values()}
    stranded = {channel["revision"] for channel in omitted_channels} - visible
    return {
        "rows": rows,
        "columns": columns,
        "hidden": hidden,
        "omitted_risks": sorted(omitted_risks),
        "omitted_channels": len(omitted_channels),
        "omitted_revisions": len(stranded),
    }


def _empty_entry():
    return {"collected_at": None, "channels": [], "revisions": {}}


def _arch_order(entry, default_arch):
    """Architectures this asset publishes to, the default one first.

    The rest are alphabetical, so the selector reads the same on every table
    and the rendered HTML does not depend on ledger iteration order.
    """
    found = {channel.get("arch") for channel in entry["channels"]}
    found.discard(None)
    rest = sorted(found - {default_arch})
    return ([default_arch] if default_arch in found else []) + rest


def _arch_table(source, entry, arch, risks, cutoff):
    """One architecture's table, or (None, channels hidden) when it is dead.

    An architecture is dead only when *every* channel it has is past `cutoff` —
    `armhf` and `i386` have had no ceph-charms release since 2022. It gets no
    table and no selector option, and its channel count comes back so the
    section can name it rather than drop it in silence.

    An architecture that is alive but whose live channels all sit on risks the
    config omits still gets a table: an empty one, carrying the counts that say
    so. Folding it in with the dead ones would label it "nothing released
    since <cutoff>", which would be false — something was released, just not
    onto a risk this page shows.
    """
    pivot = release_pivot(source, entry, arch, risks, cutoff)
    if not pivot["rows"] and not pivot["omitted_channels"]:
        return None, pivot["hidden"]

    # Count revisions, not cells: one revision commonly sits on several
    # risks and bases, and counting cells would inflate both halves.
    cells = [cell for row in pivot["rows"] for cell in row["cells"] if cell]
    published = {cell["revision"] for cell in cells}
    resolved = {cell["revision"] for cell in cells if cell["commit"]}
    provenances = sorted({cell["provenance"] for cell in cells if cell["commit"]})

    table = {
        "arch": arch,
        "risks": pivot["columns"],
        "rows": pivot["rows"],
        "resolved": len(resolved),
        "total": len(published),
        "provenances": [{"name": name,
                         "label": PROVENANCE_LABEL.get(name, "caution")}
                        for name in provenances],
        "hidden": pivot["hidden"],
        "hidden_text": plural(pivot["hidden"], "channel"),
        "omitted_risks": pivot["omitted_risks"],
        "omitted_channels_text": plural(pivot["omitted_channels"], "channel"),
        # Revisions the config's `risks:` puts out of reach entirely. Not
        # zero for ceph-nfs, whose revision 1 lives only on latest/beta.
        "omitted_revisions": pivot["omitted_revisions"],
        "omitted_revisions_text": plural(pivot["omitted_revisions"], "revision"),
        "cutoff": cutoff.strftime("%Y-%m"),
    }
    return table, 0


def release_sections(ledger, generated_at, sources, risks, max_age_days,
                     default_arch):
    """One section per configured asset, in config order, each pivoted.

    A section carries one table per architecture the asset publishes to, the
    default first. They are all rendered; a per-table selector picks which is
    visible. An architecture whose rows are all past the cutoff gets no table
    and no option — its channels are still counted in the tables that remain.

    Sections come from the config, not from the ledger's key order: an asset
    added to the config but not yet collected still gets a section saying so,
    and one removed from the config disappears even if the ledger lags.

    An asset whose collected_at predates the ledger's generated_at did not
    refresh on the last collection — its store failed, or a `--source` run
    skipped it. Either way its rows are older than the page, so say so on the
    section rather than letting the footer date every table from the last run.
    The cause is not knowable from the ledger, so the notice does not guess.
    """
    cutoff = active_cutoff(generated_at, max_age_days)
    sections = []
    for source in sources:
        entry = ledger.get(source.slug) or _empty_entry()
        collected_at = entry.get("collected_at")

        tables, inactive, inactive_channels = [], [], 0
        for arch in _arch_order(entry, default_arch):
            table, dropped = _arch_table(source, entry, arch, risks, cutoff)
            if table is not None:
                tables.append(table)
            else:
                inactive.append(arch)
                inactive_channels += dropped

        sections.append({
            "slug": source.slug,
            "title": source.name,
            "kind": source.kind,
            "repo": source.repo,
            "collapsed": source.collapsed,
            "key_headings": [heading for heading, _ in source.row_keys],
            "risks_shown": list(risks),
            "tables": tables,
            "arches": [table["arch"] for table in tables],
            # Architectures the asset publishes to but that have nothing active.
            # Named, with their channel count, rather than quietly dropped.
            "inactive_arches": inactive,
            "inactive_channels_text": plural(inactive_channels, "channel"),
            "cutoff": cutoff.strftime("%Y-%m"),
            # The collector never reached this asset's store, not even once.
            "never_collected": collected_at is None,
            # None when the ledger predates collected_at, or on a fresh source.
            "stale_since": (fmt_timestamp(collected_at)
                            if collected_at and collected_at != generated_at
                            else None),
        })
    return sections


def unpublished_default_arch(sections, default_arch):
    """`default_arch` when no asset publishes to it, else None.

    config.py cannot catch this: which architectures exist is a property of the
    ledger, not of the config file. So the check lands here, where the data is.
    A section that simply lacks the default — charm-microceph publishes amd64
    only — is fine and opens on its own first architecture. A default that *no*
    asset publishes is a typo, and every table silently opens somewhere else.
    """
    if not sections:
        return None
    if any(default_arch in section["arches"] for section in sections):
        return None
    return default_arch


def _join(items):
    """a / a and b / a, b, and c"""
    if len(items) < 3:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def data_sources(sources):
    """The services the configured sources read, for the page footer.

    Snaps come from the Snap Store and charms from Charmhub (a `git-info`
    charm's commit too: it is read out of the package there); only `git-tag`
    asks GitHub.
    """
    used = []
    if any(s.kind == "snap" for s in sources):
        used.append("the Snap Store")
    if any(s.kind == "charm" for s in sources):
        used.append("Charmhub")
    if any(s.provenance_type == "git-tag" for s in sources):
        used.append("GitHub")
    return _join(used) or "the configured stores"


def collapsed_groups(sections):
    """Collapsed sections gathered under one heading per source repository.

    The nine ceph-charms all come from canonical/ceph-charms; grouping keeps
    their <details> together instead of repeating the repo link nine times.

    Grouped by repository identity, not by adjacency: the config may list one
    repo in two non-adjacent `releases.groups` entries, and rendering its
    heading twice would read as two different repositories. Each repo appears
    once, where it first appears in config order.
    """
    groups, by_repo = [], {}
    for section in sections:
        if not section["collapsed"]:
            continue
        group = by_repo.get(section["repo"])
        if group is None:
            group = by_repo[section["repo"]] = {"repo": section["repo"],
                                                "sections": []}
            groups.append(group)
        group["sections"].append(section)
    for group in groups:
        # "9 charms" for the ceph-charms monorepo; "assets" once kinds mix.
        kinds = {section["kind"] for section in group["sections"]}
        noun = kinds.pop() if len(kinds) == 1 else "asset"
        group["count_text"] = plural(len(group["sections"]), noun)
    return groups


class ReleasePages:
    """The releases half of the site: one page, many tables.

    `site` is the SiteRenderer, which owns the Jinja environment, the output
    directory, and write_page().
    """

    def __init__(self, site):
        self.site = site
        self.config = site.config["releases"]
        self.output_dir = site.output_dir
        self.site_title = site.site_title
        self.sources = provenance.build_sources(self.config)

    def _load_ledger(self):
        """The ledger, or None when the collector has never run."""
        try:
            with open(collect_releases.ledger_path(self.output_dir)) as f:
                ledger = json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"release ledger is unreadable: {exc}") from exc
        if not isinstance(ledger, dict):
            raise RuntimeError("release ledger is not a JSON object")
        version = ledger.get("schema_version")
        if version != collect_releases.SCHEMA_VERSION:
            raise RuntimeError(
                f"release ledger: unsupported schema_version {version!r} "
                f"(this renderer understands {collect_releases.SCHEMA_VERSION})")
        return ledger

    def render(self):
        """Render releases/index.html.

        A broken ledger degrades this page and nothing else. It is an auxiliary
        view over an auxiliary cache; letting a bad timestamp in it abort the
        run would stop the CI-health metrics — the site's actual purpose — from
        being republished. The error is printed and shown on the page.
        """
        crumbs = [(self.site_title, "../index.html"), ("Releases", None)]
        error, sections, bad_default_arch = None, [], None
        footer = "No release data collected yet."
        try:
            ledger = self._load_ledger()
            if ledger is not None:
                sections = release_sections(
                    ledger["sources"], ledger["generated_at"], self.sources,
                    self.config["risks"], self.config["max_age_days"],
                    self.config["default_arch"])
                footer = (f"Data collected {fmt_timestamp(ledger['generated_at'])} "
                          f"from {data_sources(self.sources)}.")
                bad_default_arch = unpublished_default_arch(
                    sections, self.config["default_arch"])
                if bad_default_arch:
                    self.site.warn(
                        f"releases page: the config's default_arch "
                        f"{bad_default_arch!r} is published by no asset; every "
                        "table opens on its own first architecture instead")
        except (RuntimeError, KeyError, TypeError, AttributeError) as exc:
            error = str(exc)
            self.site.warn(f"releases page: {exc}")

        self.site.write_page(
            "releases/index.html", "releases/index.html",
            title=f"Releases · {self.site_title}", root="../", footer=footer,
            crumbs=crumbs, sections=sections, error=error,
            collapsed_groups=collapsed_groups(sections),
            max_age_days=self.config["max_age_days"],
            default_arch=self.config["default_arch"],
            bad_default_arch=bad_default_arch,
            risks=self.config["risks"],
        )
