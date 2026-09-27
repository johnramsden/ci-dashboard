#!/usr/bin/env python3
"""
CI health pages: the overview, one page per repository, one per (workflow, job).

Everything here derives from the dated snapshots collect.py writes. Nothing in
this module knows about releases, and nothing in render/releases.py knows about
flaky rates; render/__init__.py owns only what they share — the Jinja
environment, the landing page, and the output tree.

Rendering stays a pure function of (config, snapshots): all displayed
timestamps come from the snapshots, never the wall clock, so re-rendering
unchanged data is byte-identical and the publish workflow can skip empty
commits.

Infrastructure failures are kept out of the flaky rate. A job matching the
repo's `preflight` config is an infrastructure gate: all its failures are
infrastructure failures. Any other job's failures that collect.py found an
`Infra` annotation on (`infra_failures`) are too. Both are dropped from the
flaky rate, runs included, and reported as each branch's infrastructure
failure rate; the "incl. infrastructure" rate is the plain failures/runs over
every job. Snapshots collected before annotation checking carry no
`infra_failures` and read as zero.

The trend chart is the one exception to "templates hold the HTML":
chart_payload() returns plain data (dates/series/threshold), which a template
embeds as JSON via the `tojson` filter — safe by construction, no |safe needed
— for static/ci-health-chart.js to hand to uPlot client-side.
"""

import json
import os

from ..collect import SCHEMA_VERSION, trend_indicator
from ..config import is_excluded, iter_repos, job_slug, matching_exclude, repo_slug

from . import fmt_timestamp
from . import plural as _plural

# Accessible line colors for the trend charts (series are additionally
# distinguished by an end-of-line text label, not color alone).
CHART_COLORS = ["#0066cc", "#e95420", "#775da8", "#0e8420", "#c7162b"]


def rate_pct(failures, runs):
    return failures / runs * 100 if runs else None


def fmt_rate(failures, runs):
    value = rate_pct(failures, runs)
    return "no data" if value is None else f"{value:.1f}%"


def _failure_rate_key(job):
    """Sort key: worst failure rate first, tied jobs ordered by workflow then job name."""
    rate = job["failures"] / job["runs"] if job["runs"] else 0
    return (-rate, job["workflow"], job["job"])


def _included_jobs(snapshot, branch, excludes):
    """A branch's raw jobs from one snapshot, minus those the current excludes
    drop.

    This is where exclusion happens - at render time, against the config's
    live excludes - so editing an exclude recomputes every historical point
    consistently. Snapshots store raw per-job counts and never a pre-excluded
    total, so old points get re-filtered by the same rule as new ones.
    """
    jobs = snapshot["branches"].get(branch, {}).get("jobs", [])
    return [j for j in jobs if not is_excluded(j["workflow"], j["job"], excludes)]


def _totals(jobs):
    """(runs, failures) summed over the given jobs."""
    return sum(j["runs"] for j in jobs), sum(j["failures"] for j in jobs)


def _net_of_infra(entry):
    """A copy of a job entry with its infrastructure failures removed from
    runs, failures and every bucket, and counted in `infra` instead.

    An infra-failed run never tested the code, so it counts as neither a
    failure nor a run. Every rate and trend helper then works unchanged on
    the result.
    """
    infra = entry.get("infra_failures", 0)
    return {
        **entry,
        "runs": entry["runs"] - infra,
        "failures": entry["failures"] - infra,
        "infra": infra,
        "buckets": [
            {"runs": b["runs"] - b.get("infra_failures", 0),
             "failures": b["failures"] - b.get("infra_failures", 0)}
            for b in entry["buckets"]
        ],
    }


def _split_preflight(jobs, preflight):
    """(gate_jobs, suite_jobs): jobs matching the repo's `preflight` config,
    and the rest with their infrastructure failures netted out."""
    gate = [j for j in jobs if is_excluded(j["workflow"], j["job"], preflight)]
    suite = [_net_of_infra(j) for j in jobs
             if not is_excluded(j["workflow"], j["job"], preflight)]
    return gate, suite


def branch_rates(jobs, preflight):
    """A branch's three rates from its included (post-exclude) raw jobs.

    flaky: suite jobs, infrastructure failures dropped - the headline.
    incl_infra: plain failures/runs over every job, gates included.
    infra: (gate failures + suite infra failures) / every job's runs.
    """
    gate, suite = _split_preflight(jobs, preflight)
    runs, failures = _totals(suite)
    all_runs, all_failures = _totals(jobs)
    gate_failures = sum(j["failures"] for j in gate)
    infra = gate_failures + sum(j["infra"] for j in suite)
    return {
        "runs": runs, "failures": failures, "flaky": rate_pct(failures, runs),
        "all_runs": all_runs, "all_failures": all_failures,
        "incl_infra": rate_pct(all_failures, all_runs),
        "infra_failures": infra, "infra": rate_pct(infra, all_runs),
    }


MAX_CHART_POINTS = 150  # bounds chart payload size regardless of how much


def _downsample(points, max_points=MAX_CHART_POINTS):
    """Even-stride sample down to at most max_points, keeping the first and last.

    One point accumulates per collection day, forever; past a few hundred
    days the markers overlap into an unreadable smear and the SVG grows
    without bound. Raw history stays in data/ regardless - this only thins
    what gets drawn.
    """
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    keep = sorted({round(i * step) for i in range(max_points)})
    return [points[i] for i in keep]


def chart_payload(series, threshold=None):
    """JSON-ready data for the client-side trend chart (see
    static/ci-health-chart.js, which hands this to uPlot), or None when
    there are fewer than two plottable points or fewer than two distinct
    dates (the caller shows a notification instead).

    series: [(label, [(date_str, value_or_None), ...])] — a None value gaps
    the line (that day had no runs). Each series is independently
    downsampled to MAX_CHART_POINTS first, so payload size stays bounded
    no matter how much history has accumulated.
    """
    all_dates = sorted({d for _, points in series for d, _ in points})
    plottable = [v for _, points in series for _, v in points if v is not None]
    if len(plottable) < 2 or len(all_dates) < 2:
        return None

    downsampled = [(label, dict(_downsample(points))) for label, points in series]
    dates = sorted({d for _, points in downsampled for d in points})
    return {
        "dates": dates,
        "series": [
            {
                "label": label,
                "color": CHART_COLORS[i % len(CHART_COLORS)],
                "values": [points.get(d) for d in dates],
            }
            for i, (label, points) in enumerate(downsampled)
        ],
        "threshold": threshold,
    }


def has_enough_trend_data(entry):
    """True when a job averaged >=2 runs per trend bucket.

    Below that, the early/late split trend_indicator() compares is mostly
    noise, so callers show "—" instead of a trend arrow.
    """
    return entry["runs"] >= len(entry["buckets"]) * 2


GATE_KIND = "preflight gate"  # infra kind shown for a preflight gate's own failures


def job_table_rows(jobs, gate=()):
    """Template-ready rows for a branch's failure-rate table: preflight gate
    jobs first (marked `gate`), then suite jobs worst-by-rate.

    Suite jobs come from _split_preflight(): runs/failures/rate exclude
    infrastructure failures, which get their own `infra` column. A gate's
    failures are all infrastructure, so its row shows them under `infra`
    and its rate is the gate's own failure rate.
    """
    rows = []
    for entry in sorted(gate, key=_failure_rate_key):
        rows.append({
            "workflow": entry["workflow"],
            "job": entry["job"],
            "slug": job_slug(entry["workflow"], entry["job"]),
            "gate": True,
            "runs": entry["runs"],
            "failures": "—",
            "infra": entry["failures"],
            "rate": fmt_rate(entry["failures"], entry["runs"]),
            "trend": trend_indicator(entry["buckets"]) if has_enough_trend_data(entry) else "—",
        })
    for entry in sorted(jobs, key=_failure_rate_key):
        trend = trend_indicator(entry["buckets"]) if has_enough_trend_data(entry) else "—"
        rows.append({
            "workflow": entry["workflow"],
            "job": entry["job"],
            "slug": job_slug(entry["workflow"], entry["job"]),
            "gate": False,
            "runs": entry["runs"],
            "failures": entry["failures"],
            "infra": entry["infra"],
            "rate": fmt_rate(entry["failures"], entry["runs"]),
            "trend": trend,
        })
    return rows


def top_failing_entries(jobs, top_n):
    """Template-ready worst-by-rate jobs with >=1 failure - same ranking as job_table_rows()."""
    failing = [j for j in jobs if j["failures"] > 0]
    failing.sort(key=_failure_rate_key)
    return [
        {"workflow": j["workflow"], "job": j["job"], "failures": j["failures"],
         "rate": fmt_rate(j["failures"], j["runs"])}
        for j in failing[:top_n]
    ]


def exclude_label(entry):
    """One exclude entry as it reads in the config: `workflow="X" and job="Y"`."""
    return " and ".join(f'{key}="{entry[key]}"' for key in ("workflow", "job") if key in entry)


def excluded_summary(snapshot, branches, excludes):
    """Render-time detail of what `excludes` drop from a snapshot's raw jobs,
    or None when nothing is configured.

    Each glob is resolved against the jobs actually collected, so the page
    can name every dropped workflow and job in full instead of leaving the
    reader to expand the pattern themselves. Derived here (not stored by
    collect.py) so the listing always reflects the current config.

    Returns {"jobs": rows, "job_types": n, "runs": n, "unmatched": [labels]}.
    A label in `unmatched` matched no job in this window — a stale pattern,
    or one guarding against a job that has not run lately.
    """
    if not excludes:
        return None

    # The same (workflow, job) recurs once per branch it ran on: collapse to
    # one row listing its branches, and sum every occurrence's runs (each is
    # a genuine skipped job result).
    rows = {}
    seen = set()
    for branch in branches:
        for job in snapshot["branches"].get(branch, {}).get("jobs", []):
            key = (job["workflow"], job["job"])
            seen.add(key)
            rule = matching_exclude(job["workflow"], job["job"], excludes)
            if rule is None:
                continue
            row = rows.setdefault(key, {
                "workflow": job["workflow"], "job": job["job"],
                "branches": [], "runs": 0, "pattern": exclude_label(rule),
            })
            row["branches"].append(branch)
            row["runs"] += job["runs"]

    ordered = [rows[key] for key in sorted(rows)]
    total_runs = sum(row["runs"] for row in ordered)
    return {
        "jobs": ordered,
        "job_types": len(ordered),
        "runs": total_runs,
        "jobs_text": _plural(len(ordered), "job type"),
        "runs_text": _plural(total_runs, "job run"),
        # Tested per entry against every job seen, not against `rows`: an
        # entry shadowed by an earlier one still matches, it just never wins
        # matching_exclude()'s first-match race and so owns no row.
        "unmatched": [exclude_label(entry) for entry in excludes
                      if not any(is_excluded(w, j, [entry]) for w, j in seen)],
    }


class HealthPages:
    """The CI-health half of the site: overview, repo pages, job pages.

    Holds the snapshots it loads. `site` is the SiteRenderer, which owns the
    Jinja environment, the output directory, and write_page().
    """

    def __init__(self, site):
        self.site = site
        self.config = site.config
        self.data_dir = site.data_dir
        self.site_title = site.site_title
        # repo -> [(date_str, snapshot)], oldest -> newest
        self.snapshots = {
            repo_cfg["repo"]: self._load_snapshots(repo_cfg["repo"])
            for repo_cfg in iter_repos(site.config)
        }

    def write_page(self, *args, **kwargs):
        return self.site.write_page(*args, **kwargs)

    def footer(self):
        """Landing-page footer: when the newest snapshot was collected."""
        return self._footer()

    def render(self):
        self.render_overview()
        for repo_cfg in iter_repos(self.config):
            self.render_repo(repo_cfg)

    def _load_snapshots(self, repo):
        snap_dir = os.path.join(self.data_dir, repo_slug(repo))
        if not os.path.isdir(snap_dir):
            return []
        loaded = []
        for name in sorted(os.listdir(snap_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(snap_dir, name)
            with open(path) as f:
                snap = json.load(f)
            version = snap.get("schema_version")
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{path}: unsupported snapshot schema_version {version!r} "
                    f"(this renderer understands {SCHEMA_VERSION})")
            loaded.append((name[:-len(".json")], snap))
        return loaded

    def latest(self, repo):
        snaps = self.snapshots.get(repo, [])
        return snaps[-1][1] if snaps else None

    def _footer(self, snapshot=None):
        if snapshot is not None:
            return (f"Data collected {fmt_timestamp(snapshot['generated_at'])} "
                    "from the GitHub Actions API.")
        newest = [snaps[-1][1]["generated_at"]
                  for snaps in self.snapshots.values() if snaps]
        if not newest:
            return "No CI health data collected yet."
        return (f"Data collected {fmt_timestamp(max(newest))} "
                "from the GitHub Actions API.")

    def render_overview(self):
        groups = []
        for group in self.config["groups"]:
            cards = [self._overview_card(repo_cfg) for repo_cfg in group["repos"]]
            groups.append({"name": group["name"], "cards": cards})
        crumbs = [(self.site_title, "../index.html"), ("CI Health", None)]
        self.write_page("ci-health/index.html", "health/overview.html",
                        title=f"CI Health · {self.site_title}", root="../",
                        footer=self._footer(), crumbs=crumbs, groups=groups)

    def _overview_card(self, repo_cfg):
        repo = repo_cfg["repo"]
        slug = repo_slug(repo)
        snapshot = self.latest(repo)
        if snapshot is None:
            return {"repo": repo, "slug": slug, "has_snapshot": False}

        # Each branch shows its own rate + OK/above-threshold status. A single
        # pooled cross-branch rate would match neither branch's repo page.
        excludes = repo_cfg["exclude"]
        branch_rows = []
        for branch in repo_cfg["branches"]:
            rates = branch_rates(_included_jobs(snapshot, branch, excludes),
                                 repo_cfg["preflight"])
            branch_rows.append({
                "branch": branch,
                "runs": rates["runs"],
                "failures": rates["failures"],
                "rate": rates["flaky"],  # None when the branch has no tested runs
                "incl_infra": rates["incl_infra"],
                "infra_failures": rates["infra_failures"],
            })

        # Flag that the headline rates omit something; the repo page names what.
        excluded = excluded_summary(snapshot, repo_cfg["branches"], excludes)
        note = f"{excluded['jobs_text']} excluded" if excluded and excluded["jobs"] else None
        return {
            "repo": repo, "slug": slug, "has_snapshot": True,
            "threshold": repo_cfg["flaky_rate_threshold"],
            "branch_rows": branch_rows,
            "updated": snapshot["generated_at"][:10],
            "excluded_note": note,
        }

    def render_repo(self, repo_cfg):
        repo = repo_cfg["repo"]
        slug = repo_slug(repo)
        snaps = self.snapshots.get(repo, [])
        crumbs = [
            (self.site_title, "../../index.html"),
            ("CI Health", "../index.html"),
            (repo, None),
        ]

        if not snaps:
            self.write_page(f"ci-health/{slug}/index.html", "health/repo.html",
                            title=f"{repo} · CI Health", root="../../",
                            footer=self._footer(), crumbs=crumbs,
                            repo=repo, has_snapshots=False)
            return

        snapshot = snaps[-1][1]
        excludes = repo_cfg["exclude"]
        preflight = repo_cfg["preflight"]
        threshold = repo_cfg["flaky_rate_threshold"]
        top_jobs = repo_cfg["top_jobs"]
        lookback = snapshot["window"]["lookback_days"]

        # Every point is a snapshot's post-exclude flaky rate, recomputed from
        # its raw jobs - so changing an exclude or preflight reshapes the whole line.
        series = []
        for branch in repo_cfg["branches"]:
            points = []
            for date_str, snap in snaps:
                rates = branch_rates(_included_jobs(snap, branch, excludes), preflight)
                points.append((date_str, rates["flaky"]))
            series.append((branch, points))
        chart_data = chart_payload(series, threshold)
        chart_caption = f"Flaky rate per branch; every point is a {lookback}-day rolling window."

        branches = []
        for branch in repo_cfg["branches"]:
            jobs = _included_jobs(snapshot, branch, excludes)
            rates = branch_rates(jobs, preflight)
            if rates["all_runs"] == 0:
                branches.append({"branch": branch, "has_data": False})
                continue
            gate, suite = _split_preflight(jobs, preflight)
            runs, failures = rates["runs"], rates["failures"]
            branches.append({
                "branch": branch, "has_data": True, "overall": rates["flaky"],
                "failures": failures, "failures_word": "failure" if failures == 1 else "failures",
                "runs": runs, "runs_word": "job run" if runs == 1 else "job runs",
                "incl_infra": rates["incl_infra"],
                "infra_rate": rates["infra"],
                "infra_failures_text": _plural(rates["infra_failures"], "infrastructure failure"),
                "all_runs_text": _plural(rates["all_runs"], "job run"),
                "has_infra": bool(gate) or rates["infra_failures"] > 0,
                "job_rows": job_table_rows(suite, gate),
                "top_n": top_jobs,
                "top_entries": top_failing_entries(suite, top_jobs),
            })

        self.write_page(
            f"ci-health/{slug}/index.html", "health/repo.html",
            title=f"{repo} · CI Health", root="../../", footer=self._footer(snapshot),
            crumbs=crumbs, repo=repo, has_snapshots=True, lookback=lookback,
            generated=fmt_timestamp(snapshot["generated_at"]), threshold=threshold,
            chart_payload=chart_data, chart_caption=chart_caption,
            branches=branches,
            excluded=excluded_summary(snapshot, repo_cfg["branches"], excludes),
        )

        self._render_job_pages(repo_cfg, snaps)

    def _render_job_pages(self, repo_cfg, snaps):
        repo = repo_cfg["repo"]
        slug = repo_slug(repo)
        snapshot = snaps[-1][1]
        excludes = repo_cfg["exclude"]
        preflight = repo_cfg["preflight"]
        threshold = repo_cfg["flaky_rate_threshold"]
        lookback = snapshot["window"]["lookback_days"]

        # indexed: [(date_str, {branch: {(workflow, job): job_entry}})], one
        # entry per historical snapshot, oldest -> newest.
        indexed = []
        for date_str, snap in snaps:
            by_branch = {
                branch: {(e["workflow"], e["job"]): e for e in data.get("jobs", [])}
                for branch, data in snap["branches"].items()
            }
            indexed.append((date_str, by_branch))

        # Job pages exist for every non-excluded (workflow, job) in the latest
        # snapshot; excluded jobs are absent from every stat, page included.
        latest_by_branch = indexed[-1][1]
        keys = sorted({key for jobs in latest_by_branch.values() for key in jobs
                       if not is_excluded(key[0], key[1], excludes)})

        for workflow, job in keys:
            slug_j = job_slug(workflow, job)
            # A gate's failures are all infrastructure by definition, so its
            # page shows its own raw failure rate; any other job's page nets
            # out its annotated infrastructure failures, as the repo page does.
            is_gate = is_excluded(workflow, job, preflight)

            def view(entry):
                return {**entry, "infra": entry["failures"]} if is_gate else _net_of_infra(entry)

            crumbs = [
                (self.site_title, "../../../../index.html"),
                ("CI Health", "../../../index.html"),
                (repo, "../../index.html"),
                (f"{workflow} / {job}", None),
            ]

            rows = []
            failures = []
            for branch in repo_cfg["branches"]:
                raw = latest_by_branch.get(branch, {}).get((workflow, job))
                if raw is None:
                    continue
                entry = view(raw)
                trend = (trend_indicator(entry["buckets"])
                         if has_enough_trend_data(entry) else "—")
                rows.append({
                    "branch": branch, "runs": entry["runs"], "failures": entry["failures"],
                    "infra": entry["infra"],
                    "rate": fmt_rate(entry["failures"], entry["runs"]), "trend": trend,
                })
                failures.extend(
                    {"date": f["date"], "branch": branch, "run_id": f["run_id"], "url": f["url"],
                     "kind": GATE_KIND if is_gate else f.get("infra_kind"),
                     "reason": f.get("infra_reason")}
                    for f in raw["recent_failures"])

            series = []
            for branch in repo_cfg["branches"]:
                points = []
                for date_str, by_branch in indexed:
                    entry = by_branch.get(branch, {}).get((workflow, job))
                    if entry is not None:
                        entry = view(entry)
                    # None gaps the line: a snapshot without this job must
                    # not be bridged over (same rule as the repo chart).
                    value = (rate_pct(entry["failures"], entry["runs"])
                             if entry is not None else None)
                    points.append((date_str, value))
                # Always keep every configured branch (even all-None ones)
                # so each branch's chart color (assigned by list position in
                # chart_payload) matches its color on the repo overview
                # chart, which also lists every configured branch.
                series.append((branch, points))
            chart_data = chart_payload(series, threshold)
            chart_caption = f"Failure rate; every point is a {lookback}-day rolling window."

            failures.sort(key=lambda f: (f["date"], f["run_id"]), reverse=True)

            self.write_page(
                f"ci-health/{slug}/jobs/{slug_j}/index.html", "health/job.html",
                title=f"{workflow} / {job} · {repo} · CI Health", root="../../../../",
                footer=self._footer(snapshot), crumbs=crumbs, workflow=workflow, job=job,
                repo=repo, lookback=lookback, generated=fmt_timestamp(snapshot["generated_at"]),
                rows=rows, chart_payload=chart_data, chart_caption=chart_caption, failures=failures,
                is_gate=is_gate,
            )

