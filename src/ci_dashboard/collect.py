#!/usr/bin/env python3
"""
CI health metrics collector.

Queries completed GitHub Actions workflow runs on each configured
repository's monitored branches over a lookback window, aggregates
per-job failure counts, and writes one dated JSON snapshot per repository
for render/health.py to turn into the static dashboard.

Only runs on the monitored branches are counted. Code merged there has
already passed the test suite, so any post-merge failure is a false positive
(or a regression) - the "flaky rate":

  flaky rate = (jobs failing on monitored branches) / (total jobs)

Snapshots store *raw* per-job counts: no rates, no per-branch totals, and no
exclude filtering. render/health.py derives rates and applies the config's excludes,
so editing a threshold or an exclude recomputes the whole history instead of
only affecting days collected afterwards.

Failed jobs are also checked for an infrastructure annotation
(`::error title=Infra::kind=<kind> ...`), recorded as `infra_failures` so
render/health.py can separate infrastructure flakes from real test failures.

Environment:
  GH_TOKEN - GitHub token able to read the target repos' Actions API
             (any authenticated token works for public repositories);
             GITHUB_TOKEN is used when GH_TOKEN is unset

Usage:
  ci-dashboard collect --config FILE --output-dir SITE [--repo owner/name] [--now ISO8601]

Snapshots land in SITE/data/<owner>--<repo>/<YYYY-MM-DD>.json (UTC date);
re-running on the same day overwrites the same file.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.error
import urllib.parse

from .config import ConfigError, iter_repos, load_config, repo_slug

SCHEMA_VERSION = 1

COUNTED_CONCLUSIONS = {"success", "failure"}

# Events that represent the branch's own code being tested. Excludes
# pull_request so a fork PR whose head branch shares a monitored branch
# name cannot leak into the stats.
COUNTED_EVENTS = {"push", "schedule", "workflow_dispatch"}

# Failing-run links kept per (workflow, job) in each snapshot.
MAX_RECENT_FAILURES = 20

# Each counted run costs at least one jobs API call (plus one annotations
# call per failed job); warn when a repo's
# window approaches the GITHUB_TOKEN rate budget (~1000 requests/hour).
MAX_RUNS_WARN = 1000


# Annotation title marking a failure as caused by infrastructure (Snap Store,
# archive, LXD...) rather than the code under test. Emitted by microceph's
# preflight gate and test harness as `::error title=Infra::kind=<kind> ...`.
INFRA_TITLE = "Infra"


TARGET_BUCKETS = 10  # trend buckets to aim for, regardless of window size


def bucket_count(lookback_days):
    """Return the number of trend buckets for a given lookback window.

    Picks a bucket width (in days) that keeps the count near
    TARGET_BUCKETS
    """
    bucket_days = max(1, round(lookback_days / TARGET_BUCKETS))
    return max(1, round(lookback_days / bucket_days))


def trend_indicator(buckets):
    """Compare first-half vs second-half failure rate and return an arrow + delta string."""
    mid = len(buckets) // 2
    early, recent = buckets[:mid], buckets[mid:]
    e_runs  = sum(b["runs"]     for b in early)
    e_fails = sum(b["failures"] for b in early)
    r_runs  = sum(b["runs"]     for b in recent)
    r_fails = sum(b["failures"] for b in recent)
    if e_runs == 0 or r_runs == 0:
        return "—"
    delta = (r_fails / r_runs - e_fails / e_runs) * 100
    if abs(delta) < 1.0:
        return f"→ {delta:+.1f}%"
    return f"{'↑' if delta > 0 else '↓'} {delta:+.1f}%"


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_rate_limit(e):
    """True when an HTTPError is an actual rate limit, not a permission/auth 403.

    429 is unambiguous. A plain 403 also covers private-repo/SSO/Actions-
    disabled errors that GitHub Actions bots hit constantly and that a retry
    can never fix - only trust it as a rate limit when GitHub's own signals
    (Retry-After for the secondary/abuse limit, X-RateLimit-Remaining: 0 for
    the primary one) are present.
    """
    if e.code == 429:
        return True
    if e.code != 403:
        return False
    return bool(e.headers.get("Retry-After")) or e.headers.get("X-RateLimit-Remaining") == "0"


def _urlopen(req):
    """Execute a request with one automatic retry on an actual rate limit."""
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if not _is_rate_limit(e):
            raise RuntimeError(f"GitHub API error {e.code} for {req.full_url}") from e
        retry_after = e.headers.get("Retry-After")
        reset = e.headers.get("X-RateLimit-Reset")
        if retry_after:
            wait = int(retry_after) + 5
        elif reset:
            # +5: small buffer so the retry doesn't land exactly on the reset boundary.
            wait = max(0, int(reset) - int(time.time())) + 5
        else:
            wait = 60
        print(f"Rate limited (HTTP {e.code}). Waiting {wait}s before retry...", file=sys.stderr)
        time.sleep(wait)
    # Retry once outside the except block so a second failure is handled cleanly.
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as retry_e:
        raise RuntimeError(
            f"rate limit persists after retry (HTTP {retry_e.code}) for {req.full_url}"
        ) from retry_e


def gh_get(token, path):
    """Fetch a single page from the GitHub API and return parsed JSON."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers=_headers(token))
    return json.loads(_urlopen(req))


def paginate(token, path, key):
    """Page through a GitHub list endpoint, collecting `key` from each page until empty."""
    items = []
    page = 1
    while True:
        data = gh_get(token, f"{path}&page={page}")
        batch = data.get(key, [])
        if not batch:
            break
        items.extend(batch)
        page += 1
    return items


def get_runs(token, repo, since, branch):
    """Return all completed workflow runs on `branch` created on or after `since`."""
    branch_q = urllib.parse.quote(branch, safe="")
    path = (
        f"/repos/{repo}/actions/runs"
        f"?status=completed&created=>={since}&branch={branch_q}&per_page=100"
    )
    return paginate(token, path, "workflow_runs")


def get_jobs(token, repo, run_id):
    """Return all jobs for a workflow run, including earlier re-run attempts.

    `filter=all` matters for the flaky rate: the API default (`latest`) only
    returns the newest attempt, so a flaky job that failed and was re-run to
    green would disappear from the stats entirely.
    """
    path = f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
    return paginate(token, path, "jobs")


def get_annotations(token, repo, job_id):
    """Return the check-run annotations for a job (a job id is its check-run id)."""
    path = f"/repos/{repo}/check-runs/{job_id}/annotations?per_page=100"
    return gh_get(token, path)


# Longest infra reason kept per failure; annotation messages can embed stderr.
MAX_INFRA_REASON = 300


def infra_failure(annotations):
    """Return (kind, reason) from a job's infra annotation, or None.

    Looks for a failure-level annotation titled INFRA_TITLE. Its message is
    `kind=<kind> <reason>`: kind is "unknown" when the token is missing,
    and the reason is the rest of the message, truncated to MAX_INFRA_REASON.
    """
    for a in annotations:
        if a.get("title") != INFRA_TITLE or a.get("annotation_level") != "failure":
            continue
        message = " ".join((a.get("message") or "").split())
        first, _, rest = message.partition(" ")
        if first.startswith("kind=") and len(first) > len("kind="):
            kind, reason = first[len("kind="):], rest
        else:
            kind, reason = "unknown", message
        if len(reason) > MAX_INFRA_REASON:
            reason = reason[:MAX_INFRA_REASON - 1] + "…"
        return kind, reason
    return None


def aggregate_jobs(tagged_runs, jobs_fetcher, since_dt, now, num_buckets,
                   annotations_fetcher=None):
    """Aggregate per-branch, per-(workflow, job) raw stats from (branch, run) pairs.

    tagged_runs: [(branch, run_dict), ...] - the branch each run was fetched
    from, per collect_repo(). jobs_fetcher(run_id) does the actual network
    call (one per counted run) and this function prints per-run progress.

    since_dt/now: the lookback window's start/end, used to place each run
    into one of num_buckets equal time-slices for trend tracking.

    num_buckets: how many time-slices to divide the window into (see
    bucket_count()).

    annotations_fetcher(job_id), if given, is called for each failed job to
    classify it as an infrastructure failure (see infra_failure()).

    Every counted job is recorded; excludes are NOT applied here. render/health.py
    applies the config's current excludes so they recompute retroactively.

    Returns branch -> (workflow, job) ->
    {runs, failures, infra_failures, infra_kinds, buckets, recent_failures}.
    """
    stats = {}
    window_secs = (now - since_dt).total_seconds()

    for i, (branch, run) in enumerate(tagged_runs, start=1):
        stats.setdefault(branch, {})
        print(f"  Fetching jobs for run {i}/{len(tagged_runs)} (id={run['id']})...")
        # Each run has its own created_at timestamp. To tell whether a job is
        # trending better or worse (not just its overall rate), we need to
        # know roughly *when* within the lookback window each run happened -
        # so the window gets sliced into num_buckets equal time-ranges, and
        # every run is sorted into whichever slice its timestamp falls in.
        run_dt = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        elapsed = (run_dt - since_dt).total_seconds()  # seconds from window start to this run
        # elapsed/window_secs is this run's position in the window as a 0..1
        # fraction; scaling by num_buckets and flooring (via int()) picks
        # which equal-width bucket it falls into.
        # clamp: elapsed==window_secs would produce index num_buckets
        bucket_idx = min(int(elapsed / window_secs * num_buckets), num_buckets - 1)
        bucket_idx = max(0, bucket_idx)  # clamp: clock skew can make elapsed slightly negative

        for job in jobs_fetcher(run["id"]):
            conclusion = job.get("conclusion")
            if conclusion not in COUNTED_CONCLUSIONS:
                continue
            key = (run["name"], job["name"])
            s = stats[branch].setdefault(key, {
                "runs": 0,
                "failures": 0,
                "infra_failures": 0,
                "infra_kinds": {},
                "buckets": [
                    {"runs": 0, "failures": 0, "infra_failures": 0}
                    for _ in range(num_buckets)
                ],
                "recent_failures": [],
            })
            bucket = s["buckets"][bucket_idx]
            s["runs"] += 1
            bucket["runs"] += 1
            if conclusion == "failure":
                s["failures"] += 1
                bucket["failures"] += 1
                infra = None
                if annotations_fetcher is not None:
                    infra = infra_failure(annotations_fetcher(job["id"]))
                kind, reason = infra or (None, None)
                if kind is not None:
                    s["infra_failures"] += 1
                    bucket["infra_failures"] += 1
                    s["infra_kinds"][kind] = s["infra_kinds"].get(kind, 0) + 1
                s["recent_failures"].append({
                    "created_at": run["created_at"],
                    "run_id": run["id"],
                    "url": job.get("html_url") or run.get("html_url", ""),
                    "infra_kind": kind,
                    "infra_reason": reason,
                })

    # Keep only the newest failures per job.
    for branch_stats in stats.values():
        for s in branch_stats.values():
            s["recent_failures"].sort(key=lambda f: (f["created_at"], f["run_id"]), reverse=True)
            del s["recent_failures"][MAX_RECENT_FAILURES:]

    return stats


def _recent_failure(f):
    """Serialize one recent failure; `infra_kind`/`infra_reason` only for infra failures."""
    out = {"date": f["created_at"][:10], "run_id": f["run_id"], "url": f["url"]}
    if f.get("infra_kind"):
        out["infra_kind"] = f["infra_kind"]
        out["infra_reason"] = f["infra_reason"]
    return out


def build_snapshot(repo_cfg, stats, since_dt, now):
    """Serialize raw per-job stats into the versioned snapshot format.

    Stores only raw counts - no rates, no per-branch totals, no exclude
    filtering. render/health.py derives everything from the config (see module
    docstring), which is what makes excludes and thresholds recompute across
    the whole history rather than only from the day they changed.
    """
    branches_out = {}
    for branch in repo_cfg["branches"]:
        jobs = []
        for (workflow, job), s in sorted(stats.get(branch, {}).items()):
            jobs.append({
                "workflow": workflow,
                "job": job,
                "runs": s["runs"],
                "failures": s["failures"],
                "infra_failures": s["infra_failures"],
                "infra_kinds": dict(sorted(s["infra_kinds"].items())),
                "buckets": s["buckets"],
                "recent_failures": [_recent_failure(f) for f in s["recent_failures"]],
            })
        branches_out[branch] = {"jobs": jobs}

    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo_cfg["repo"],
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lookback_days": repo_cfg["lookback_days"],
        },
        "branches": branches_out,
    }


def collect_repo(token, repo_cfg, now):
    """Collect one repository's snapshot dict."""
    repo = repo_cfg["repo"]
    lookback_days = repo_cfg["lookback_days"]

    since_dt = now - timedelta(days=lookback_days)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    num_buckets = bucket_count(lookback_days)

    # (branch, run) pairs across all monitored branches
    tagged_runs = []
    for branch in repo_cfg["branches"]:
        print(f"Fetching workflow runs on {repo}@{branch} since {since}...")
        runs = get_runs(token, repo, since, branch)
        runs = [r for r in runs if r.get("event") in COUNTED_EVENTS]
        print(f"Found {len(runs)} completed runs on {branch}.")
        if not runs:
            print(
                f"Warning: no countable runs on {repo}@{branch} — "
                "check the branch name in the config (and whether the branch "
                "gets any push/schedule/workflow_dispatch runs at all).",
                file=sys.stderr,
            )
        tagged_runs.extend((branch, run) for run in runs)

    if len(tagged_runs) > MAX_RUNS_WARN:
        print(
            f"Warning: {len(tagged_runs)} runs in {repo}'s window — each costs a "
            "jobs API call; this may exhaust the hourly rate budget.",
            file=sys.stderr,
        )

    def jobs_fetcher(run_id):
        return get_jobs(token, repo, run_id)

    # Annotations come from the Checks API, which a token can be denied while
    # it still reads Actions (`checks: read` is a separate permission). Losing
    # them costs only the infrastructure classification, so warn once and
    # count every failure as a plain failure rather than drop the repo's day.
    annotations_denied = False

    def annotations_fetcher(job_id):
        nonlocal annotations_denied
        if annotations_denied:
            return []
        try:
            return get_annotations(token, repo, job_id)
        except RuntimeError as e:
            cause = e.__cause__
            if not (isinstance(cause, urllib.error.HTTPError) and cause.code in (403, 404)):
                raise
            if _is_rate_limit(cause):
                raise  # a rate limit outlasting the retry, not a missing scope
            annotations_denied = True
            print(f"Warning: cannot read check-run annotations on {repo} (HTTP "
                  f"{cause.code}); the token needs `checks: read`. Infrastructure "
                  "failures on it are counted as plain failures this run.",
                  file=sys.stderr)
            return []

    stats = aggregate_jobs(
        tagged_runs, jobs_fetcher, since_dt, now, num_buckets, annotations_fetcher
    )

    return build_snapshot(repo_cfg, stats, since_dt, now)


def write_snapshot(snapshot, output_dir, date_str):
    """Write a snapshot under output_dir/data/<repo slug>/, atomically."""
    dest_dir = os.path.join(output_dir, "data", repo_slug(snapshot["repo"]))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{date_str}.json")
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=1, sort_keys=True)
        f.write("\n")
    os.rename(tmp, dest)
    return dest


def parse_now(value):
    """Parse an ISO-8601 --now override; naive values are taken as UTC."""
    now = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def github_token():
    """The token for GitHub API reads: GH_TOKEN, else GITHUB_TOKEN, else ""."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description="Collect CI health snapshots.")
    parser.add_argument("--config", required=True, help="Path to the dashboard config file")
    parser.add_argument("--output-dir", required=True,
                        help="Site root; snapshots go to <output-dir>/data/<repo>/<date>.json")
    parser.add_argument("--repo", help="Collect only this owner/name (debugging)")
    parser.add_argument("--now", help="ISO-8601 override of the clock (tests/reproducibility)")
    args = parser.parse_args(argv)

    token = github_token()
    if not token:
        print("Error: GH_TOKEN must be set, e.g. export GH_TOKEN=$(gh auth token)",
              file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    repos = list(iter_repos(config))
    if args.repo:
        repos = [r for r in repos if r["repo"] == args.repo]
        if not repos:
            print(f"Error: {args.repo} is not in the configuration.", file=sys.stderr)
            sys.exit(1)

    now = parse_now(args.now) if args.now else datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    failed = []
    for repo_cfg in repos:
        try:
            snapshot = collect_repo(token, repo_cfg, now)
        except Exception as e:  # one repo failing must not lose the others
            print(f"Error collecting {repo_cfg['repo']}: {e}", file=sys.stderr)
            failed.append(repo_cfg["repo"])
            continue
        dest = write_snapshot(snapshot, args.output_dir, date_str)
        print(f"Wrote {dest}")

    if failed:
        print(f"Error: collection failed for: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
