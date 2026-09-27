#!/usr/bin/env python3
"""
Renders the dashboard from synthetic snapshots for offline development.

Usage: ci-dashboard simulate [OUTPUT_DIR]

Writes several days of fabricated history for two example repositories to
<output_dir>/data — covering worsening/improving/stable/spiky/sparse trends,
a zero-run branch, and recent-failure links — then runs the renderer over
it. Open <output_dir>/index.html in a browser to inspect styling without
touching the GitHub API.
"""

import argparse
import json
import os
from datetime import date, timedelta

from .collect import SCHEMA_VERSION
from .collect_releases import SCHEMA_VERSION as RELEASES_SCHEMA_VERSION
from .collect_releases import write_ledger
from .config import repo_slug
from .provenance import GIT_INFO, GIT_TAG, UNKNOWN, VERSION_STRING
from .render import render_site

BASE_DATE = date(2026, 4, 1)
DAYS = 14

# Each entry: (workflow, job, base buckets oldest -> newest, day trend).
# The buckets shape drives the in-window trend arrow; the day trend scales
# failures across simulated days so the over-time charts show movement.
SCENARIOS = [
    # Clearly getting worse — failure rate climbing week over week
    ("Tests", "lint", [(20, 1), (20, 3), (20, 8), (20, 14)], "worsening"),
    # Clearly improving — failure rate falling
    ("Tests", "unit", [(20, 12), (20, 8), (20, 3), (20, 1)], "improving"),
    # Flat / stable low failure rate
    ("Tests", "build", [(20, 2), (20, 2), (20, 2), (20, 2)], "stable"),
    # Flat / stable high failure rate
    ("Integration", "smoke", [(20, 14), (20, 15), (20, 13), (20, 14)], "stable"),
    # Spike in the middle, now recovering
    ("Integration", "full", [(20, 2), (20, 18), (20, 18), (20, 3)], "spike"),
    # Sparse — only 2 runs total; too little data for an arrow, so the
    # trend cell should render as an em dash (—).
    ("Nightly", "deploy", [(1, 1), (0, 0), (0, 0), (1, 0)], "stable"),
    # Non-test workflow the config excludes. Stored raw in every snapshot (as
    # collect.py now does), with a high failure rate, so render/health.py's
    # retroactive exclusion visibly has to drop it from every rate, chart, and
    # job page — if it leaked in it would dominate the flaky rate.
    ("Vuln scan (scheduled)", "scan", [(5, 4), (5, 5), (5, 4), (5, 5)], "stable"),
    # Preflight infrastructure gate (config `preflight:`): its failures feed
    # the infrastructure failure rate, never the flaky rate or job table.
    ("Tests", "Preflight infra gate", [(20, 1), (20, 0), (20, 2), (20, 1)], "stable"),
]

# (workflow, job) -> infra kind: that job's failures are half infrastructure
# (as if collect.py found an `Infra` annotation on them). Only alpha's jobs
# carry infra fields at all; bravo's snapshots stay in the pre-annotation
# format, exercising the "missing infra fields read as zero" path.
INFRA_KINDS = {("Integration", "smoke"): "snap-store"}

BRAVO_SCENARIOS = [
    ("Build/Test", "build-and-test / Functional test", [(10, 1), (10, 2), (10, 1), (10, 2)], "stable"),
    ("Build/Test", "build-and-test / Juju cluster test", [(10, 3), (10, 2), (10, 4), (10, 3)], "worsening"),
    ("Push (main)", "Lint", [(15, 0), (15, 0), (15, 1), (15, 0)], "stable"),
    # Bot workflow the config excludes, running on both monitored branches:
    # the repo page's excluded-jobs table must collapse it to a single row
    # listing both branches, not one row per branch.
    ("Close inactive issues", "stale", [(2, 1), (2, 0), (2, 1), (2, 0)], "stable"),
]

# stable/squid runs only the functional test — plus the excluded bot workflow.
BRAVO_SQUID_SCENARIOS = [BRAVO_SCENARIOS[0], BRAVO_SCENARIOS[-1]]


def _day_scale(trend, day):
    """Deterministic per-day multiplier for a scenario's failure counts."""
    progress = day / (DAYS - 1)
    if trend == "worsening":
        # ramps failure rate from 40% to 100% of the base bucket across the window
        return 0.4 + 0.6 * progress
    if trend == "improving":
        # mirror of worsening: falls from 100% to 40%
        return 1.0 - 0.6 * progress
    if trend == "spike":
        # full rate for the 5 days centered on the midpoint, half rate elsewhere
        return 1.0 if abs(day - DAYS // 2) <= 2 else 0.5
    return 1.0


def _job_entry(repo, workflow, job, buckets, trend, day):
    scale = _day_scale(trend, day)
    kind = INFRA_KINDS.get((workflow, job))
    day_buckets = [
        {"runs": runs, "failures": min(runs, round(failures * scale))}
        for runs, failures in buckets
    ]
    if kind:
        for b in day_buckets:
            b["infra_failures"] = b["failures"] // 2
    total_runs = sum(b["runs"] for b in day_buckets)
    total_failures = sum(b["failures"] for b in day_buckets)
    recent = [
        {
            "date": (BASE_DATE + timedelta(days=day - offset)).isoformat(),
            "run_id": 100000 * (day + 1) + offset,
            "url": (f"https://github.com/{repo}/actions/runs/"
                    f"{100000 * (day + 1) + offset}/job/{offset + 1}"),
        }
        for offset in range(min(total_failures, 3))
    ]
    entry = {
        "workflow": workflow,
        "job": job,
        "runs": total_runs,
        "failures": total_failures,
        "buckets": day_buckets,
        "recent_failures": recent,
    }
    if kind:
        infra = sum(b["infra_failures"] for b in day_buckets)
        entry["infra_failures"] = infra
        entry["infra_kinds"] = {kind: infra} if infra else {}
        if infra:
            recent[0]["infra_kind"] = kind
            recent[0]["infra_reason"] = "snap download failed after 3 attempts: nonce error (503)"
    return entry


def _branch_block(repo, scenarios, day):
    """A branch's raw per-job block - no totals, matching collect.py's schema
    (render/health.py derives totals and applies excludes)."""
    jobs = [_job_entry(repo, wf, job, buckets, trend, day)
            for wf, job, buckets, trend in scenarios]
    jobs.sort(key=lambda j: (j["workflow"], j["job"]))
    return {"jobs": jobs}


def _snapshot(repo_cfg, branches, day):
    day_date = BASE_DATE + timedelta(days=day)
    generated = f"{day_date.isoformat()}T06:17:00Z"
    since = f"{(day_date - timedelta(days=30)).isoformat()}T06:17:00Z"
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo_cfg["repo"],
        "generated_at": generated,
        "window": {"since": since, "until": generated, "lookback_days": 30},
        "branches": branches,
    }


def make_config():
    """Config dict shaped exactly like config.load_config() output."""
    defaults = {"lookback_days": 30, "top_jobs": 5, "flaky_rate_threshold": 3.0}
    alpha = dict(defaults, repo="example/alpha", branches=["main", "squid"],
                 # The second entry matches nothing: exercises the repo page's
                 # "matched no job in this window" note for a stale pattern.
                 exclude=[{"workflow": "Vuln scan*"}, {"job": "deploy-preview"}],
                 preflight=[{"workflow": "Tests", "job": "Preflight infra gate"}])
    bravo = dict(defaults, repo="example/bravo",
                 branches=["main", "stable/squid"],
                 exclude=[{"workflow": "Close inactive issues"}], preflight=[])
    return {
        "site": {"title": "CI Dashboard", "description": None},
        "groups": [{"name": "Examples", "repos": [alpha, bravo]}],
        # `beta` is deliberately absent from `risks` while charm-example
        # publishes to it: exercises the "risk not shown" note.
        "releases": {
            "default_arch": "amd64",
            "risks": ["stable", "candidate", "edge"],
            "max_age_days": 730,
            "groups": [
                {"repo": "example/alpha", "provenance": "version-string",
                 "assets": ["example"], "collapsed": False},
                {"repo": "example/bravo", "provenance": "git-info",
                 "assets": ["example"], "collapsed": True},
            ],
        },
    }


def write_history(data_dir, days=DAYS):
    """Write `days` daily snapshots for both example repos."""
    config = make_config()
    alpha_cfg, bravo_cfg = config["groups"][0]["repos"]
    for day in range(days):
        day_date = (BASE_DATE + timedelta(days=day)).isoformat()

        alpha = _snapshot(alpha_cfg, {
            "main": _branch_block(alpha_cfg["repo"], SCENARIOS, day),
            # squid never runs: exercises the "no data" paths.
            "squid": {"jobs": []},
        }, day)

        bravo = _snapshot(bravo_cfg, {
            "main": _branch_block(bravo_cfg["repo"], BRAVO_SCENARIOS, day),
            "stable/squid": _branch_block(
                bravo_cfg["repo"], BRAVO_SQUID_SCENARIOS, day),
        }, day)

        for snap in (alpha, bravo):
            dest_dir = os.path.join(data_dir, repo_slug(snap["repo"]))
            os.makedirs(dest_dir, exist_ok=True)
            with open(os.path.join(dest_dir, f"{day_date}.json"), "w") as f:
                json.dump(snap, f, indent=1, sort_keys=True)
                f.write("\n")
    return config


def _release_ledger():
    """A fabricated ledger covering all four provenance tiers."""
    def rev(commit=None, provenance=UNKNOWN, branch=None, note=""):
        return {"commit": commit, "branch": branch, "provenance": provenance,
                "note": note, "retryable": False}

    collected = f"{BASE_DATE + timedelta(days=DAYS)}T06:17:00Z"
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "generated_at": collected,
        "sources": {
            "snap-example": {
                "collected_at": collected,
                "channels": [
                    {"track": "squid", "risk": "stable", "revision": 1735,
                     "version": "19.2.3+snapcf306793a4", "arch": "amd64",
                     "base": None, "released_at": collected},
                    {"track": "squid", "risk": "edge", "revision": 1848,
                     "version": "20.2.1+snap73293b5d48", "arch": "amd64",
                     "base": None, "released_at": collected},
                    {"track": "quincy", "risk": "stable", "revision": 793,
                     "version": "0+git.4a608fc", "arch": "amd64",
                     "base": None, "released_at": collected},
                ],
                "revisions": {
                    "1735": rev("cf306793a4983f0821e502973de5ae6f52c4a20b",
                                VERSION_STRING),
                    "1848": rev("73293b5d48c0da0cd6f97dadb212be18b35e996e",
                                VERSION_STRING),
                    "793": rev(note="version '0+git.4a608fc' is not <pkg>+snap<sha>"),
                },
            },
            "charm-example": {
                "collected_at": collected,
                "channels": [
                    {"track": "squid", "risk": "stable", "revision": 296,
                     "version": None, "arch": "amd64", "base": "24.04",
                     "released_at": collected},
                    # Same channel, different arch, different commit — the
                    # ceph-mon squid/stable case the selector exists for.
                    {"track": "squid", "risk": "stable", "revision": 297,
                     "version": None, "arch": "arm64", "base": "24.04",
                     "released_at": collected},
                    {"track": "tentacle", "risk": "edge", "revision": 499,
                     "version": None, "arch": "arm64", "base": "24.04",
                     "released_at": collected},
                    {"track": "squid", "risk": "edge", "revision": 293,
                     "version": None, "arch": "amd64", "base": "22.04",
                     "released_at": collected},
                    {"track": "tentacle", "risk": "edge", "revision": 498,
                     "version": None, "arch": "amd64", "base": "24.04",
                     "released_at": collected},
                    {"track": "squid", "risk": "beta", "revision": 296,
                     "version": None, "arch": "amd64", "base": "24.04",
                     "released_at": collected},
                    # Long-dead track: exercises the active-channel cutoff.
                    {"track": "luminous", "risk": "stable", "revision": 12,
                     "version": None, "arch": "amd64", "base": "16.04",
                     "released_at": "2022-03-16T00:00:00Z"},
                ],
                "revisions": {
                    "296": rev("b3db3c3e2857a0e2b1a9d7f8c6e5d4c3b2a19087", GIT_TAG),
                    "293": rev(note="no rev293 tag in example/bravo"),
                    "498": rev("e67ab37ad305f082eb1c8865e055090143131cb0",
                               GIT_INFO, branch="main"),
                    "12": rev(note="charm has no git-info.txt"),
                    "297": rev("a9b45088bcf7c1d2e3f405162738495a6b7c8d9e",
                               GIT_INFO, branch="main"),
                    "499": rev("3e890ad332f8aabbccddeeff00112233445566aa",
                               GIT_INFO, branch="main"),
                },
            },
        },
    }


def write_releases(output_dir):
    """Write a fabricated release ledger next to the CI snapshots."""
    write_ledger(_release_ledger(), output_dir)


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(
        prog=prog, description="Render the dashboard from fabricated data (no network).")
    parser.add_argument("output_dir", nargs="?", default="sim-site",
                        help="Where to write the site (default: sim-site)")
    output = parser.parse_args(argv).output_dir
    data_dir = os.path.join(output, "data")
    config = write_history(data_dir)
    write_releases(output)
    render_site(config, data_dir, output)
    print(f"Simulated site rendered to {output}/index.html")


if __name__ == "__main__":
    main()
