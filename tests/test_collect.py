import http.client
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import urllib.error
import urllib.request

from ci_dashboard import collect
from ci_dashboard.collect import (
    _urlopen,
    aggregate_jobs,
    bucket_count,
    collect_repo,
    infra_failure,
    trend_indicator,
)


# collect_repo() fetches annotations for every failed job. Default every test
# to "no annotations" so none reaches the network; TestInfraAnnotations
# patches get_annotations itself to exercise classification.
_NO_ANNOTATIONS = patch("ci_dashboard.collect.get_annotations", return_value=[])


def setUpModule():
    _NO_ANNOTATIONS.start()


def tearDownModule():
    _NO_ANNOTATIONS.stop()


def _branch_totals(snapshot, branch):
    """Sum a snapshot branch's raw jobs into {runs, failures} - collect.py no
    longer stores a pre-aggregated total, so tests derive it the way render/health.py
    does."""
    jobs = snapshot["branches"][branch]["jobs"]
    return {"runs": sum(j["runs"] for j in jobs),
            "failures": sum(j["failures"] for j in jobs)}

NOW = datetime(2026, 1, 20, 0, 0, 0, tzinfo=timezone.utc)


def repo_cfg(**overrides):
    cfg = {
        "repo": "octo/repo",
        "branches": ["main"],
        "exclude": [],
        "lookback_days": 30,
        "top_jobs": 5,
        "flaky_rate_threshold": 3.0,
    }
    cfg.update(overrides)
    return cfg


def run_entry(run_id=1, name="Tests", event="push", created="2026-01-15T00:00:00Z"):
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "created_at": created,
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}",
    }


def job_entry(name="build", conclusion="success", run_id=1, job_id=11):
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}/job/{job_id}",
    }


def _fake_response(body):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(status, reason, headers=None):
    msg = http.client.HTTPMessage()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/test", status, reason, msg, None)


class TestRateLimitRetries(unittest.TestCase):
    """_urlopen's GitHub rate-limit / permission-error handling."""

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_rate_limit_retries_with_wait(self, mock_urlopen, mock_sleep):
        """_urlopen sleeps Retry-After + 5s on 429 then retries successfully."""
        mock_urlopen.side_effect = [
            _http_error(429, "Too Many Requests", {"Retry-After": "10"}),
            _fake_response(b'{"ok": true}'),
        ]
        result = _urlopen(urllib.request.Request("https://api.github.com/test"))
        mock_sleep.assert_called_once_with(15)  # Retry-After(10) + 5
        self.assertEqual(result, b'{"ok": true}')

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_rate_limit_gives_up_with_runtime_error(self, mock_urlopen, mock_sleep):
        """a persistent rate limit raises (not SystemExit), so main() can
        isolate the failure to the repo being collected."""
        def limited(*args, **kwargs):
            raise _http_error(429, "Too Many Requests", {"Retry-After": "1"})
        mock_urlopen.side_effect = limited
        with self.assertRaises(RuntimeError):
            _urlopen(urllib.request.Request("https://api.github.com/test"))

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_permission_403_raises_immediately(self, mock_urlopen, mock_sleep):
        """a plain 403 (no rate-limit headers) is a permission/auth error,
        not a rate limit - it must not sleep and retry the same request."""
        mock_urlopen.side_effect = _http_error(403, "Forbidden")
        with self.assertRaises(RuntimeError):
            _urlopen(urllib.request.Request("https://api.github.com/test"))
        mock_sleep.assert_not_called()
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_403_with_remaining_zero_retries_like_rate_limit(self, mock_urlopen, mock_sleep):
        """a 403 that does carry X-RateLimit-Remaining: 0 is a real primary
        rate limit and must still get the wait-and-retry treatment."""
        mock_urlopen.side_effect = [
            _http_error(403, "Forbidden", {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(time.time()) + 10),
            }),
            _fake_response(b'{"ok": true}'),
        ]
        result = _urlopen(urllib.request.Request("https://api.github.com/test"))
        mock_sleep.assert_called_once()
        self.assertEqual(result, b'{"ok": true}')


class TestBucketAndTrendMath(unittest.TestCase):

    def test_bucket_count(self):
        """bucket_count targets ~TARGET_BUCKETS regardless of window size."""
        self.assertEqual(bucket_count(1), 1)
        self.assertEqual(bucket_count(7), 7)
        self.assertEqual(bucket_count(10), 10)
        self.assertEqual(bucket_count(30), 10)
        self.assertEqual(bucket_count(90), 10)

    def test_bucket_count_has_no_large_jumps(self):
        """adjacent lookback_days values must never swing bucket_count by a
        large factor - regression test for the old fixed daily/weekly/
        monthly unit switch, which dropped 14 buckets to 2 for a lookback
        change from 14 to 15 days (and 12 to 3 for 90 to 91 days)."""
        for days in range(2, 400):
            lo, hi = bucket_count(days), bucket_count(days + 1)
            self.assertLessEqual(hi, lo * 2 + 1, f"days={days}->{days + 1}")
            self.assertLessEqual(lo, hi * 2 + 1, f"days={days}->{days + 1}")

    def test_trend_indicator_increasing(self):
        buckets = [
            {"runs": 10, "failures": 1},
            {"runs": 10, "failures": 1},
            {"runs": 10, "failures": 5},
            {"runs": 10, "failures": 5},
        ]
        self.assertTrue(trend_indicator(buckets).startswith("↑"))

    def test_trend_indicator_decreasing(self):
        buckets = [
            {"runs": 10, "failures": 5},
            {"runs": 10, "failures": 5},
            {"runs": 10, "failures": 1},
            {"runs": 10, "failures": 1},
        ]
        self.assertTrue(trend_indicator(buckets).startswith("↓"))


class TestExcludesDeferredToRender(unittest.TestCase):
    """collect.py records every job raw; excludes are applied by render/health.py so
    they recompute retroactively. These pin that collect no longer drops or
    counts excluded jobs (matching is unit-tested in test_config.py)."""

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_would_be_excluded_workflow_still_collected(self, mock_runs, mock_jobs):
        """a config exclude no longer skips the jobs API call or drops the job:
        the raw data must be present for render/health.py to filter (and re-filter)."""
        mock_runs.return_value = [
            run_entry(run_id=1, name="Vuln scan (scheduled)"),
            run_entry(run_id=2, name="Tests"),
        ]
        mock_jobs.side_effect = lambda token, repo, run_id: [job_entry(run_id=run_id)]
        snapshot = collect_repo(
            "tok", repo_cfg(exclude=[{"workflow": "Vuln scan (scheduled)"}]), NOW)
        # Both runs' jobs are fetched now - no fully-excluded-workflow skip.
        fetched = sorted(call.args[2] for call in mock_jobs.call_args_list)
        self.assertEqual(fetched, [1, 2])
        workflows = sorted(j["workflow"] for j in snapshot["branches"]["main"]["jobs"])
        self.assertEqual(workflows, ["Tests", "Vuln scan (scheduled)"])
        # No exclude bookkeeping is baked into the snapshot anymore.
        self.assertNotIn("excluded_run_count", snapshot)
        self.assertNotIn("excluded_job_count", snapshot)

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_would_be_excluded_job_still_collected(self, mock_runs, mock_jobs):
        """a job-level exclude no longer removes the job at collect time."""
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [
            job_entry(name="build", conclusion="failure"),
            job_entry(name="flaky-suite", conclusion="failure"),
        ]
        snapshot = collect_repo(
            "tok", repo_cfg(exclude=[{"workflow": "Tests", "job": "flaky*"}]), NOW)
        names = sorted(j["job"] for j in snapshot["branches"]["main"]["jobs"])
        self.assertEqual(names, ["build", "flaky-suite"])
        self.assertEqual(_branch_totals(snapshot, "main"), {"runs": 2, "failures": 2})


class TestRecentFailures(unittest.TestCase):

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_failure_links_captured(self, mock_runs, mock_jobs):
        """failing jobs record the job's html_url; successes record nothing."""
        mock_runs.return_value = [run_entry(run_id=7)]
        mock_jobs.return_value = [
            job_entry(name="build", conclusion="failure", run_id=7, job_id=70),
            job_entry(name="lint", conclusion="success", run_id=7, job_id=71),
        ]
        snapshot = collect_repo("tok", repo_cfg(), NOW)
        jobs = {j["job"]: j for j in snapshot["branches"]["main"]["jobs"]}
        self.assertEqual(jobs["build"]["recent_failures"], [{
            "date": "2026-01-15",
            "run_id": 7,
            "url": "https://github.com/octo/repo/actions/runs/7/job/70",
        }])
        self.assertEqual(jobs["lint"]["recent_failures"], [])

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_failure_links_capped_newest_first(self, mock_runs, mock_jobs):
        mock_runs.return_value = [
            run_entry(run_id=i, created=f"2026-01-{i:02d}T00:00:00Z")
            for i in range(1, 20)  # 19 failing runs in the window
        ]
        mock_jobs.side_effect = lambda token, repo, run_id: [
            job_entry(conclusion="failure", run_id=run_id, job_id=run_id * 10)]
        with patch.object(collect, "MAX_RECENT_FAILURES", 5):
            snapshot = collect_repo("tok", repo_cfg(), NOW)
        failures = snapshot["branches"]["main"]["jobs"][0]["recent_failures"]
        self.assertEqual(len(failures), 5)
        self.assertEqual([f["date"] for f in failures],
                         ["2026-01-19", "2026-01-18", "2026-01-17",
                          "2026-01-16", "2026-01-15"])


class TestCollectRepoFiltering(unittest.TestCase):

    @patch("ci_dashboard.collect.gh_get")
    def test_get_runs_filters_by_branch(self, mock_gh_get):
        """get_runs passes the branch as a query parameter."""
        mock_gh_get.return_value = {"workflow_runs": []}
        collect.get_runs("tok", "o/r", "2026-01-01T00:00:00Z", "squid")
        path = mock_gh_get.call_args[0][1]
        self.assertIn("branch=squid", path)

    @patch("ci_dashboard.collect.gh_get")
    def test_get_runs_urlencodes_branch(self, mock_gh_get):
        """stable/squid must survive as one branch name in the query string."""
        mock_gh_get.return_value = {"workflow_runs": []}
        collect.get_runs("tok", "o/r", "2026-01-01T00:00:00Z", "stable/squid")
        path = mock_gh_get.call_args[0][1]
        self.assertIn("branch=stable%2Fsquid", path)

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_skipped_and_cancelled_not_counted(self, mock_runs, mock_jobs):
        """skipped and cancelled conclusions are excluded from run and failure counts."""
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [
            job_entry(conclusion="success"),
            job_entry(conclusion="failure"),
            job_entry(conclusion="skipped"),
            job_entry(conclusion="cancelled"),
        ]
        snapshot = collect_repo("tok", repo_cfg(), NOW)
        job = snapshot["branches"]["main"]["jobs"][0]
        self.assertEqual(job["runs"], 2)
        self.assertEqual(job["failures"], 1)

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_pull_request_runs_not_counted(self, mock_runs, mock_jobs):
        """runs from events other than push/schedule/workflow_dispatch are excluded."""
        mock_runs.return_value = [
            run_entry(run_id=1, event="push"),
            run_entry(run_id=2, event="pull_request"),
        ]
        mock_jobs.return_value = [job_entry(conclusion="failure")]
        snapshot = collect_repo("tok", repo_cfg(), NOW)
        job = snapshot["branches"]["main"]["jobs"][0]
        self.assertEqual(job["runs"], 1)
        self.assertEqual(job["failures"], 1)
        # The excluded run's jobs were never fetched.
        self.assertEqual(mock_jobs.call_count, 1)

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_runs_fetched_per_monitored_branch(self, mock_runs, mock_jobs):
        """each configured branch is queried separately."""
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [job_entry()]
        collect_repo("tok", repo_cfg(branches=["main", "squid"]), NOW)
        branches_queried = [call.args[3] for call in mock_runs.call_args_list]
        self.assertEqual(branches_queried, ["main", "squid"])

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_failures_attributed_to_correct_branch(self, mock_runs, mock_jobs):
        """per-branch totals reflect the branch each run was fetched from."""
        def runs_for(token, repo, since, branch):
            run_id = {"main": 1, "squid": 2}[branch]
            return [run_entry(run_id=run_id)]

        def jobs_for(token, repo, run_id):
            return {
                1: [job_entry(conclusion="success", run_id=1)],
                2: [job_entry(conclusion="failure", run_id=2)],
            }[run_id]

        mock_runs.side_effect = runs_for
        mock_jobs.side_effect = jobs_for
        snapshot = collect_repo("tok", repo_cfg(branches=["main", "squid"]), NOW)
        self.assertEqual(_branch_totals(snapshot, "main"), {"runs": 1, "failures": 0})
        self.assertEqual(_branch_totals(snapshot, "squid"), {"runs": 1, "failures": 1})

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_zero_run_branch_present_in_snapshot(self, mock_runs, mock_jobs):
        """a monitored branch with no runs still appears, with an empty job list."""
        mock_runs.side_effect = lambda token, repo, since, branch: (
            [run_entry()] if branch == "main" else [])
        mock_jobs.return_value = [job_entry()]
        snapshot = collect_repo("tok", repo_cfg(branches=["main", "squid"]), NOW)
        self.assertEqual(snapshot["branches"]["squid"], {"jobs": []})


class TestAggregateBuckets(unittest.TestCase):

    def test_runs_land_in_time_buckets(self):
        """oldest runs go to the first bucket, newest to the last."""
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 1, 29, tzinfo=timezone.utc)  # 28-day window, 4 buckets
        runs = [
            ("main", run_entry(run_id=1, created="2026-01-01T12:00:00Z")),
            ("main", run_entry(run_id=2, created="2026-01-28T12:00:00Z")),
        ]
        jobs = lambda run_id: [job_entry(conclusion="failure", run_id=run_id)]
        stats = aggregate_jobs(runs, jobs, since, now, 4)
        entry = stats["main"][("Tests", "build")]
        self.assertEqual([b["runs"] for b in entry["buckets"]], [1, 0, 0, 1])
        self.assertEqual((entry["runs"], entry["failures"]), (2, 2))

    def test_clock_skew_clamps_to_first_bucket(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 1, 29, tzinfo=timezone.utc)
        runs = [("main", run_entry(created="2025-12-31T23:59:00Z"))]
        jobs = lambda run_id: [job_entry()]
        stats = aggregate_jobs(runs, jobs, since, now, 4)
        buckets = stats["main"][("Tests", "build")]["buckets"]
        self.assertEqual([b["runs"] for b in buckets], [1, 0, 0, 0])


def infra_annotation(message, title="Infra", level="failure"):
    return {"title": title, "annotation_level": level, "message": message}


# Annotations a real microceph suite failure carries (run 35989785169).
NODE20_WARNING = infra_annotation(
    "Node.js 20 actions are deprecated.", title="", level="warning")
EXIT_CODE = infra_annotation("Process completed with exit code 7.", title="")
SNAP_STORE = infra_annotation(
    "kind=snap-store snap install failed after 3 attempts: nonce error")


class TestInfraAnnotations(unittest.TestCase):

    def test_infra_failure_parsed(self):
        self.assertEqual(infra_failure([NODE20_WARNING, EXIT_CODE, SNAP_STORE]),
                         ("snap-store", "snap install failed after 3 attempts: nonce error"))
        self.assertEqual(infra_failure([infra_annotation(
            "kind=preflight PREFLIGHT: endpoint checks failed\n  from vm")]),
            ("preflight", "PREFLIGHT: endpoint checks failed from vm"))

    def test_infra_failure_unknown_without_kind_token(self):
        self.assertEqual(infra_failure([infra_annotation("Snap Store down")]),
                         ("unknown", "Snap Store down"))
        self.assertEqual(infra_failure([infra_annotation("kind= oops")]),
                         ("unknown", "kind= oops"))

    def test_infra_reason_truncated(self):
        kind, reason = infra_failure([infra_annotation("kind=apt " + "x" * 1000)])
        self.assertEqual(len(reason), collect.MAX_INFRA_REASON)
        self.assertTrue(reason.endswith("…"))

    def test_non_infra_annotations_ignored(self):
        self.assertIsNone(infra_failure([]))
        self.assertIsNone(infra_failure([NODE20_WARNING, EXIT_CODE]))
        # Right title at the wrong level is not a failure classification.
        self.assertIsNone(infra_failure([infra_annotation(
            "kind=apt stall", level="warning")]))

    @patch("ci_dashboard.collect.get_annotations")
    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_annotations_fetched_only_for_failures(self, mock_runs, mock_jobs, mock_ann):
        mock_runs.return_value = [run_entry(run_id=7)]
        mock_jobs.return_value = [
            job_entry(name="build", conclusion="failure", run_id=7, job_id=70),
            job_entry(name="lint", conclusion="success", run_id=7, job_id=71),
            job_entry(name="docs", conclusion="skipped", run_id=7, job_id=72),
        ]
        mock_ann.return_value = []
        collect_repo("tok", repo_cfg(), NOW)
        mock_ann.assert_called_once_with("tok", "octo/repo", 70)

    @patch("ci_dashboard.collect.get_annotations")
    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_infra_failures_recorded(self, mock_runs, mock_jobs, mock_ann):
        mock_runs.return_value = [
            run_entry(run_id=i, created=f"2026-01-{i:02d}T00:00:00Z") for i in (1, 2, 3)]
        mock_jobs.side_effect = lambda token, repo, run_id: [
            job_entry(conclusion="failure", run_id=run_id, job_id=run_id * 10)]
        # Run 1: snap-store flake; run 2: a real failure; run 3: snap-store again.
        mock_ann.side_effect = lambda token, repo, job_id: {
            10: [EXIT_CODE, SNAP_STORE], 20: [EXIT_CODE], 30: [SNAP_STORE],
        }[job_id]
        snapshot = collect_repo("tok", repo_cfg(), NOW)
        job = snapshot["branches"]["main"]["jobs"][0]
        self.assertEqual((job["runs"], job["failures"], job["infra_failures"]), (3, 3, 2))
        self.assertEqual(job["infra_kinds"], {"snap-store": 2})
        self.assertEqual(sum(b["infra_failures"] for b in job["buckets"]), 2)
        self.assertEqual(
            [(f["run_id"], f.get("infra_kind")) for f in job["recent_failures"]],
            [(3, "snap-store"), (2, None), (1, "snap-store")])
        self.assertEqual(job["recent_failures"][0]["infra_reason"],
                         "snap install failed after 3 attempts: nonce error")
        # Only infra failures carry the keys at all.
        self.assertNotIn("infra_kind", job["recent_failures"][1])
        self.assertNotIn("infra_reason", job["recent_failures"][1])

    @staticmethod
    def _http_error(code):
        err = urllib.error.HTTPError("https://api.github.com/x", code, "Forbidden", {}, None)
        try:
            raise RuntimeError(f"GitHub API error {code}") from err
        except RuntimeError as e:
            return e

    @patch("ci_dashboard.collect.get_annotations")
    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_forbidden_annotations_degrade_instead_of_dropping_the_repo(
            self, mock_runs, mock_jobs, mock_ann):
        """A token without `checks: read` still collects the repo: its failures
        count as plain failures, and annotations are not asked for again."""
        mock_runs.return_value = [
            run_entry(run_id=i, created=f"2026-01-{i:02d}T00:00:00Z") for i in (1, 2)]
        mock_jobs.side_effect = lambda token, repo, run_id: [
            job_entry(conclusion="failure", run_id=run_id, job_id=run_id * 10)]
        mock_ann.side_effect = self._http_error(403)
        with patch("sys.stderr", new=__import__("io").StringIO()) as err:
            snapshot = collect_repo("tok", repo_cfg(), NOW)
        job = snapshot["branches"]["main"]["jobs"][0]
        self.assertEqual((job["runs"], job["failures"], job["infra_failures"]), (2, 2, 0))
        self.assertEqual(mock_ann.call_count, 1)
        self.assertIn("checks: read", err.getvalue())

    @patch("ci_dashboard.collect.get_annotations")
    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_a_persisting_rate_limit_is_not_mistaken_for_a_missing_scope(
            self, mock_runs, mock_jobs, mock_ann):
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [job_entry(conclusion="failure")]
        err = urllib.error.HTTPError("https://api.github.com/x", 403, "rate limited",
                                     {"X-RateLimit-Remaining": "0"}, None)
        try:
            raise RuntimeError("rate limit persists after retry") from err
        except RuntimeError as e:
            mock_ann.side_effect = e
        with self.assertRaises(RuntimeError):
            collect_repo("tok", repo_cfg(), NOW)

    @patch("ci_dashboard.collect.get_annotations")
    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_other_annotation_errors_still_fail_the_repo(self, mock_runs, mock_jobs, mock_ann):
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [job_entry(conclusion="failure")]
        mock_ann.side_effect = self._http_error(500)
        with self.assertRaises(RuntimeError):
            collect_repo("tok", repo_cfg(), NOW)

    @patch("ci_dashboard.collect.get_jobs")
    @patch("ci_dashboard.collect.get_runs")
    def test_no_infra_fields_zero(self, mock_runs, mock_jobs):
        mock_runs.return_value = [run_entry()]
        mock_jobs.return_value = [job_entry(conclusion="failure")]
        job = collect_repo("tok", repo_cfg(), NOW)["branches"]["main"]["jobs"][0]
        self.assertEqual((job["infra_failures"], job["infra_kinds"]), (0, {}))


class TestSnapshotWriter(unittest.TestCase):

    def _run_main(self, tmp, extra_argv=None, conclusion="success"):
        config_path = os.path.join(tmp, "config.yaml")
        with open(config_path, "w") as f:
            f.write("""
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
""")
        argv = ["collect.py", "--config", config_path, "--output-dir", tmp,
                "--now", "2026-01-20T00:00:00Z"] + (extra_argv or [])
        with patch.object(collect, "get_runs", return_value=[run_entry()]), \
             patch.object(collect, "get_jobs",
                          return_value=[job_entry(conclusion=conclusion)]), \
             patch.dict(os.environ, {"GH_TOKEN": "tok"}), \
             patch("sys.argv", argv):
            collect.main()
        return os.path.join(tmp, "data", "octo--repo", "2026-01-20.json")

    def test_snapshot_written_with_utc_date_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._run_main(tmp)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                snapshot = json.load(f)
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertEqual(snapshot["repo"], "octo/repo")
            self.assertEqual(snapshot["generated_at"], "2026-01-20T00:00:00Z")
            self.assertEqual(snapshot["window"]["lookback_days"], 30)

    def test_same_day_rerun_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._run_main(tmp, conclusion="success")
            with open(path) as f:
                self.assertEqual(_branch_totals(json.load(f), "main"),
                                 {"runs": 1, "failures": 0})
            path = self._run_main(tmp, conclusion="failure")
            # The rerun replaced (not duplicated, not kept) the snapshot.
            with open(path) as f:
                self.assertEqual(_branch_totals(json.load(f), "main"),
                                 {"runs": 1, "failures": 1})
            self.assertEqual(len(os.listdir(os.path.dirname(path))), 1)

    def test_unknown_repo_filter_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                self._run_main(tmp, ["--repo", "not/there"])
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
