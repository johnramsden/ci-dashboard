import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from unittest.mock import patch

from ci_dashboard import render
import ci_dashboard.render.health
from ci_dashboard import simulate as simulate_site
from ci_dashboard.config import job_slug, repo_slug
from ci_dashboard.render import render_site
from ci_dashboard.render.health import branch_rates, chart_payload, top_failing_entries

VANILLA_URL = "https://assets.ubuntu.com/v1/vanilla_framework_version_4.55.0.min.css"

# Elements that never take a closing tag (SVG shapes are written
# self-closing, which html.parser reports separately).
VOID_ELEMENTS = {"meta", "link", "br", "hr", "img", "input"}


class TagBalanceChecker(HTMLParser):
    """Structural sanity check: every opened tag is closed, in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing (<meta />, <polyline ... />): nothing to balance

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"closing </{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"closing </{tag}> but <{self.stack[-1]}> is open")
            self.stack.pop()
        else:
            self.stack.pop()


class RenderedSiteTest(unittest.TestCase):
    """Render the simulated site once and assert on the output tree."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.site = cls._tmp.name
        data_dir = os.path.join(cls.site, "data")
        cls.config = simulate_site.write_history(data_dir)
        # Seed a release ledger too, so the whole-site sweeps below (tag
        # balance, determinism) actually parse a populated Releases table
        # rather than its empty state.
        simulate_site.write_releases(cls.site)
        render_site(cls.config, data_dir, cls.site)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def read(self, *rel):
        with open(os.path.join(self.site, *rel)) as f:
            return f.read()

    def test_output_tree(self):
        for rel in (
            "index.html",
            ".nojekyll",
            os.path.join("static", "ci-health.css"),
            os.path.join("static", "ci-health-chart.js"),
            os.path.join("static", "vendor", "uplot", "uPlot.iife.min.js"),
            os.path.join("static", "vendor", "uplot", "uPlot.min.css"),
            os.path.join("ci-health", "index.html"),
            os.path.join("ci-health", "example--alpha", "index.html"),
            os.path.join("ci-health", "example--bravo", "index.html"),
            os.path.join("ci-health", "example--alpha", "jobs",
                         job_slug("Tests", "lint"), "index.html"),
        ):
            self.assertTrue(os.path.exists(os.path.join(self.site, rel)),
                            f"missing {rel}")

    def test_vanilla_hotlink_present(self):
        self.assertIn(VANILLA_URL, self.read("index.html"))

    def test_landing_lists_ci_health_section(self):
        landing = self.read("index.html")
        self.assertIn('href="ci-health/index.html"', landing)
        self.assertIn("CI Health", landing)

    def test_overview_links_repos_and_shows_no_data_branch(self):
        overview = self.read("ci-health", "index.html")
        self.assertIn('href="example--alpha/index.html"', overview)
        self.assertIn("<code>squid</code>: no data", overview)
        self.assertNotIn("<code>squid</code>: 0.0%", overview)

    def test_repo_page_links_job_pages(self):
        page = self.read("ci-health", "example--alpha", "index.html")
        slug = job_slug("Tests", "lint")
        self.assertIn(f'href="jobs/{slug}/index.html"', page)

    def test_repo_page_zero_run_branch_notification(self):
        page = self.read("ci-health", "example--alpha", "index.html")
        self.assertIn("No push/schedule/workflow_dispatch runs on "
                      "<code>squid</code>", page)

    def test_repo_page_has_trend_chart(self):
        page = self.read("ci-health", "example--alpha", "index.html")
        self.assertIn('class="ci-chart__canvas"', page)
        self.assertIn('class="ci-chart__data"', page)

    def test_repo_page_shows_exclude_note(self):
        page = self.read("ci-health", "example--alpha", "index.html")
        self.assertIn("Vuln scan*", page)

    def test_job_page_links_failing_runs(self):
        page = self.read("ci-health", "example--alpha", "jobs",
                         job_slug("Tests", "lint"), "index.html")
        self.assertIn("https://github.com/example/alpha/actions/runs/", page)
        self.assertIn("Recent failures", page)
        self.assertIn('class="ci-chart__canvas"', page)

    def test_sparse_job_trend_suppressed(self):
        page = self.read("ci-health", "example--alpha", "jobs",
                         job_slug("Nightly", "deploy"), "index.html")
        self.assertIn("<td data-heading=\"Trend\">—</td>", page)

    def test_all_pages_are_balanced_html(self):
        for dirpath, _, filenames in os.walk(self.site):
            for name in filenames:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(dirpath, name)
                checker = TagBalanceChecker()
                with open(path) as f:
                    checker.feed(f.read())
                checker.close()
                self.assertEqual(checker.errors, [], f"unbalanced HTML in {path}")
                self.assertEqual(checker.stack, [], f"unclosed tags in {path}: "
                                                    f"{checker.stack}")

    def test_rendering_is_deterministic(self):
        with tempfile.TemporaryDirectory() as second:
            data_dir = os.path.join(second, "data")
            simulate_site.write_history(data_dir)
            simulate_site.write_releases(second)
            render_site(self.config, data_dir, second)
            for dirpath, _, filenames in os.walk(self.site):
                rel_dir = os.path.relpath(dirpath, self.site)
                if rel_dir.startswith("data"):
                    continue
                for name in filenames:
                    first_path = os.path.join(dirpath, name)
                    second_path = os.path.join(second, rel_dir, name)
                    with open(first_path, "rb") as a, open(second_path, "rb") as b:
                        self.assertEqual(a.read(), b.read(),
                                         f"nondeterministic output: {rel_dir}/{name}")


class FailSafeCleanupTest(unittest.TestCase):
    """A bad input must not cost the site that is already published."""

    def _site_with_previous_render(self, tmp):
        for rel in ("index.html", os.path.join("ci-health", "index.html"),
                    os.path.join("releases", "index.html")):
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("PREVIOUSLY PUBLISHED")

    def test_unreadable_snapshot_leaves_the_published_site_intact(self):
        config = {"site": {"title": "Ceph QA"},
                  "groups": [{"name": "G", "repos": [{
                      "repo": "octo/repo", "branches": ["main"], "exclude": [], "preflight": [],
                      "lookback_days": 30, "top_jobs": 5,
                      "flaky_rate_threshold": 3.0}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data", repo_slug("octo/repo"))
            os.makedirs(data_dir)
            with open(os.path.join(data_dir, "2026-07-01.json"), "w") as f:
                json.dump({"schema_version": 99, "repo": "octo/repo",
                           "generated_at": "2026-07-01T00:00:00Z",
                           "window": {"since": "2026-06-01T00:00:00Z",
                                      "until": "2026-07-01T00:00:00Z",
                                      "lookback_days": 30},
                           "branches": {"main": {"jobs": []}}}, f)
            self._site_with_previous_render(tmp)

            with self.assertRaises(RuntimeError):
                render_site(config, os.path.join(tmp, "data"), tmp)

            for rel in ("index.html", os.path.join("ci-health", "index.html"),
                        os.path.join("releases", "index.html")):
                self.assertTrue(os.path.exists(os.path.join(tmp, rel)),
                                f"{rel} was deleted before the render failed")


class DownsampleTest(unittest.TestCase):
    """render_trend_chart's point cap - unit-tested directly since a
    rendered PNG has no per-point markup to count."""

    def test_below_cap_returns_unchanged(self):
        points = [(f"2026-01-{d:02d}", float(d)) for d in range(1, 21)]
        self.assertEqual(render.health._downsample(points, max_points=150), points)

    def test_above_cap_keeps_endpoints_and_bounds_count(self):
        points = [(str(i), float(i)) for i in range(2000)]
        result = render.health._downsample(points, max_points=150)
        self.assertLessEqual(len(result), 150)
        self.assertEqual(result[0], points[0])
        self.assertEqual(result[-1], points[-1])


class ChartPayloadTest(unittest.TestCase):

    def test_none_when_single_point(self):
        payload = chart_payload([("main", [("2026-01-01", 1.0)])], threshold=3.0)
        self.assertIsNone(payload)

    def test_none_when_single_date(self):
        payload = chart_payload(
            [("main", [("2026-01-01", 1.0)]), ("squid", [("2026-01-01", 2.0)])],
            threshold=3.0)
        self.assertIsNone(payload)

    def test_shape_for_normal_series(self):
        payload = chart_payload(
            [("main", [("2026-01-01", 1.0), ("2026-01-02", 2.0)]),
             ("squid", [("2026-01-01", 0.5), ("2026-01-02", None)])],
            threshold=3.0)
        self.assertEqual(payload["dates"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(payload["threshold"], 3.0)
        main, squid = payload["series"]
        self.assertEqual(main["label"], "main")
        self.assertEqual(main["values"], [1.0, 2.0])
        self.assertEqual(squid["values"], [0.5, None])
        self.assertNotEqual(main["color"], squid["color"])

    def test_gaps_preserved_as_null(self):
        payload = chart_payload([("main", [
            ("2026-01-01", 1.0),
            ("2026-01-02", 2.0),
            ("2026-01-03", None),   # no runs that day: the line must gap
            ("2026-01-04", 1.5),
        ])], threshold=3.0)
        self.assertEqual(payload["series"][0]["values"], [1.0, 2.0, None, 1.5])

    def test_all_none_series_included(self):
        """an all-None series still appears (as all-null values) rather
        than being dropped - dropping it would shift later series' colors,
        see ChartColorConsistencyTest."""
        payload = chart_payload([
            ("main", [("2026-01-01", 1.0), ("2026-01-02", 2.0)]),
            ("squid", [("2026-01-01", None), ("2026-01-02", None)]),
        ], threshold=3.0)
        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(payload["series"][1]["values"], [None, None])

    def test_threshold_none_is_json_null(self):
        payload = chart_payload(
            [("main", [("2026-01-01", 1.0), ("2026-01-02", 2.0)])], threshold=None)
        self.assertIsNone(payload["threshold"])

    def test_long_history_stays_bounded(self):
        """years of daily snapshots must not grow the payload without
        bound: both series exceed MAX_CHART_POINTS, so both get
        downsampled to the same point count regardless of raw history."""
        start = date(2020, 1, 1)
        short_history = [((start + timedelta(days=i)).isoformat(), float(i % 10))
                         for i in range(400)]
        long_history = [((start + timedelta(days=i)).isoformat(), float(i % 10))
                        for i in range(4000)]
        short_payload = chart_payload([("main", short_history)], threshold=3.0)
        long_payload = chart_payload([("main", long_history)], threshold=3.0)
        self.assertLessEqual(len(long_payload["dates"]), 150)
        self.assertEqual(len(long_payload["dates"]), len(short_payload["dates"]))


class TopFailingOrderTest(unittest.TestCase):

    def test_ranks_by_rate_not_raw_count(self):
        """top_failing_entries must agree with job_table_rows' worst-by-rate
        ranking, not rank a high-volume/low-rate job above a
        low-volume/high-rate one."""
        jobs = [
            {"workflow": "Tests", "job": "high-volume", "runs": 1000, "failures": 50},
            {"workflow": "Tests", "job": "high-rate", "runs": 5, "failures": 4},
        ]
        entries = top_failing_entries(jobs, top_n=5)
        self.assertEqual([e["job"] for e in entries], ["high-rate", "high-volume"])
        self.assertEqual(entries[0]["rate"], "80.0%")


def _snapshot_dict(repo, date_str, branches):
    """Snapshot JSON skeleton shared by the fixtures below; only `branches`
    varies. Raw counts only - render/health.py derives rates/totals and applies the
    live config's excludes (no `config`/`totals`/`excluded_*` stored)."""
    return {
        "schema_version": 1,
        "repo": repo,
        "generated_at": f"{date_str}T00:00:00Z",
        "window": {"since": f"{date_str}T00:00:00Z", "until": f"{date_str}T00:00:00Z",
                   "lookback_days": 30},
        "branches": branches,
    }


class ChartColorConsistencyTest(unittest.TestCase):
    """A branch's chart-series order (and therefore its assigned color, by
    list position - see CHART_COLORS) must match between the repo overview
    chart and any job page, even when a job has no data on an
    earlier-listed branch."""

    def _write_snapshot(self, data_dir, repo, date_str, squid_failures):
        dest = os.path.join(data_dir, repo_slug(repo))
        os.makedirs(dest, exist_ok=True)
        squid_job = {
            "workflow": "Tests", "job": "only-on-squid",
            "runs": 10, "failures": squid_failures,
            "buckets": [{"runs": 10, "failures": squid_failures}],
            "recent_failures": [],
        }
        snapshot = _snapshot_dict(repo, date_str, {
            # main has zero jobs; only squid runs this job. The bug this
            # guards against: filtering an all-None branch out of a job
            # page's series shifted every later branch's list-index color.
            "main": {"totals": {"runs": 0, "failures": 0}, "jobs": []},
            "squid": {"totals": {"runs": 10, "failures": squid_failures},
                      "jobs": [squid_job]},
        })
        with open(os.path.join(dest, f"{date_str}.json"), "w") as f:
            json.dump(snapshot, f)

    def test_branch_order_matches_between_repo_and_job_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/colortest"
            self._write_snapshot(data_dir, repo, "2026-01-01", squid_failures=1)
            self._write_snapshot(data_dir, repo, "2026-01-02", squid_failures=2)
            config = {
                "site": {"title": "Ceph QA"},
                "groups": [{"name": "G", "repos": [{
                    "repo": repo, "branches": ["main", "squid"], "exclude": [], "preflight": [],
                    "lookback_days": 30, "top_jobs": 5, "flaky_rate_threshold": 3.0,
                }]}],
            }
            with patch.object(render.health, "chart_payload",
                              wraps=render.health.chart_payload) as spy:
                render_site(config, data_dir, tmp)

            # Exactly two chart calls for this single-repo, single-job
            # fixture: the repo-overview chart, then this job's own chart.
            self.assertEqual(len(spy.call_args_list), 2)
            branch_orders = [[label for label, _ in call.args[0]]
                             for call in spy.call_args_list]
            self.assertEqual(branch_orders[0], ["main", "squid"])
            self.assertEqual(branch_orders[0], branch_orders[1])


class HistoryEdgeCasesTest(unittest.TestCase):
    """Coverage for snapshot-history shapes the main simulated fixture never
    produces: a repo with no snapshots yet, and a job whose history stops
    before the latest snapshot."""

    def _config(self, repo, branches=("main",)):
        return {
            "site": {"title": "Ceph QA"},
            "groups": [{"name": "G", "repos": [{
                "repo": repo, "branches": list(branches), "exclude": [], "preflight": [],
                "lookback_days": 30, "top_jobs": 5, "flaky_rate_threshold": 3.0,
            }]}],
        }

    def test_repo_with_no_snapshots_renders_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir)
            config = self._config("example/nodata")
            render_site(config, data_dir, tmp)  # must not raise
            slug = repo_slug("example/nodata")
            with open(os.path.join(tmp, "ci-health", slug, "index.html")) as f:
                self.assertIn("No data collected yet", f.read())
            with open(os.path.join(tmp, "ci-health", "index.html")) as f:
                self.assertIn("No data yet", f.read())

    def test_job_missing_from_latest_snapshot_does_not_crash(self):
        """a job present in an earlier snapshot but gone from the latest one
        (renamed/removed workflow) must not raise, and gets no stale page."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/vanishing"
            dest = os.path.join(data_dir, repo_slug(repo))
            os.makedirs(dest)

            def snapshot(date_str, jobs):
                return _snapshot_dict(repo, date_str, {"main": {
                    "totals": {"runs": sum(j["runs"] for j in jobs),
                               "failures": sum(j["failures"] for j in jobs)},
                    "jobs": jobs,
                }})

            old_job = {"workflow": "Tests", "job": "retired", "runs": 5, "failures": 1,
                       "buckets": [{"runs": 5, "failures": 1}], "recent_failures": []}
            with open(os.path.join(dest, "2026-01-01.json"), "w") as f:
                json.dump(snapshot("2026-01-01", [old_job]), f)
            with open(os.path.join(dest, "2026-01-02.json"), "w") as f:
                json.dump(snapshot("2026-01-02", []), f)  # "retired" is gone

            config = self._config(repo)
            render_site(config, data_dir, tmp)  # must not raise
            slug = repo_slug(repo)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "ci-health", slug, "jobs", job_slug("Tests", "retired"))))


class ExcludeRetroactivityTest(unittest.TestCase):
    """The bug this guards against: excludes used to be baked into each
    snapshot at collect time, so adding one only affected new days and made
    the trend chart show a spurious cliff. Excludes now apply at render time
    against raw per-job counts, so editing them recomputes the whole history."""

    def _job(self, workflow, job, runs, failures):
        return {"workflow": workflow, "job": job, "runs": runs, "failures": failures,
                "buckets": [{"runs": runs, "failures": failures}], "recent_failures": []}

    def _write(self, data_dir, repo, date_str, jobs):
        dest = os.path.join(data_dir, repo_slug(repo))
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, f"{date_str}.json"), "w") as f:
            json.dump(_snapshot_dict(repo, date_str, {"main": {"jobs": jobs}}), f)

    def _config(self, repo, excludes):
        return {"site": {"title": "Ceph QA"},
                "groups": [{"name": "G", "repos": [{
                    "repo": repo, "branches": ["main"], "exclude": excludes, "preflight": [],
                    "lookback_days": 30, "top_jobs": 5, "flaky_rate_threshold": 3.0}]}]}

    def test_excluding_a_workflow_lowers_every_historical_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/retro"
            # A noisy "Bots" workflow present in BOTH days' raw data.
            self._write(data_dir, repo, "2026-01-01",
                        [self._job("Tests", "unit", 10, 1),
                         self._job("Bots", "vuln", 10, 9)])
            self._write(data_dir, repo, "2026-01-02",
                        [self._job("Tests", "unit", 10, 1),
                         self._job("Bots", "vuln", 10, 8)])

            def series_for(excludes):
                with patch.object(render.health, "chart_payload",
                                  wraps=render.health.chart_payload) as spy:
                    render_site(self._config(repo, excludes), data_dir, tmp)
                # First chart call is the repo-overview chart.
                main_label, points = spy.call_args_list[0].args[0][0]
                self.assertEqual(main_label, "main")
                return [v for _, v in points]

            # Without the exclude: (1+9)/20=50%, (1+8)/20=45% - the noisy job dominates.
            self.assertEqual(series_for([]), [50.0, 45.0])
            # With it: only Tests/unit counts, 1/10 both days - and crucially the
            # OLDER point (2026-01-01) recomputes too, not just the newest.
            self.assertEqual(series_for([{"workflow": "Bots"}]), [10.0, 10.0])

    def test_excluded_job_has_no_page_and_is_summarized(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/retro2"
            for date_str in ("2026-01-01", "2026-01-02"):
                self._write(data_dir, repo, date_str,
                            [self._job("Tests", "unit", 10, 1),
                             self._job("Bots", "vuln", 10, 9)])
            render_site(self._config(repo, [{"workflow": "Bots"}]), data_dir, tmp)

            slug = repo_slug(repo)
            # No page for the excluded job; the real one still has a page.
            self.assertFalse(os.path.exists(os.path.join(
                tmp, "ci-health", slug, "jobs", job_slug("Bots", "vuln"))))
            self.assertTrue(os.path.exists(os.path.join(
                tmp, "ci-health", slug, "jobs", job_slug("Tests", "unit"), "index.html")))
            # The repo page names what the current exclude drops - the glob is
            # resolved to the real workflow/job, not just echoed at the reader.
            with open(os.path.join(tmp, "ci-health", slug, "index.html")) as f:
                page = f.read()
            self.assertIn("workflow=&#34;Bots&#34;", page)  # autoescaped quotes
            self.assertIn("<td data-heading=\"Job\">vuln</td>", page)
            self.assertIn("1 job type, 10 job runs skipped", page)
            # The overview card flags that its headline rate omits something.
            with open(os.path.join(tmp, "ci-health", "index.html")) as f:
                self.assertIn("1 job type excluded", f.read())

    def test_excluded_summary_dedups_job_types_across_branches(self):
        """the same excluded (workflow, job) running on N monitored branches is
        one job TYPE listing both branches, but every occurrence's runs are
        summed - matching the shipped config (microceph excludes a workflow
        across main + squid)."""
        vuln = lambda runs: {
            "workflow": "Vuln scan (scheduled)", "job": "scan",
            "runs": runs, "failures": runs,
            "buckets": [{"runs": runs, "failures": runs}], "recent_failures": []}
        snapshot = {"branches": {"main": {"jobs": [vuln(5)]},
                                 "squid": {"jobs": [vuln(3)]}}}
        summary = render.health.excluded_summary(
            snapshot, ["main", "squid"], [{"workflow": "Vuln scan (scheduled)"}])
        self.assertEqual(summary["job_types"], 1)  # one distinct (workflow, job)
        self.assertEqual(summary["runs"], 8)       # 5 + 3 genuinely skipped runs
        self.assertEqual(summary["jobs"], [{
            "workflow": "Vuln scan (scheduled)", "job": "scan",
            "branches": ["main", "squid"], "runs": 8,
            "pattern": 'workflow="Vuln scan (scheduled)"',
        }])

    def test_excluded_summary_resolves_globs_to_full_names(self):
        """a glob is expanded against the jobs actually collected: each dropped
        job is named in full and attributed to the rule that dropped it."""
        snapshot = {"branches": {"main": {"jobs": [
            self._job("Nightly", "deploy", 4, 0),
            self._job("Vuln scan (scheduled)", "govulncheck", 6, 2),
            self._job("Vuln scan (weekly)", "trivy", 3, 1),
        ]}}}
        summary = render.health.excluded_summary(
            snapshot, ["main"], [{"workflow": "Vuln scan*"}])
        self.assertEqual([(j["workflow"], j["job"], j["runs"]) for j in summary["jobs"]],
                         [("Vuln scan (scheduled)", "govulncheck", 6),
                          ("Vuln scan (weekly)", "trivy", 3)])
        self.assertEqual({j["pattern"] for j in summary["jobs"]}, {'workflow="Vuln scan*"'})
        self.assertEqual(summary["runs"], 9)  # Nightly/deploy is untouched
        self.assertEqual(summary["unmatched"], [])

    def test_excluded_summary_reports_patterns_matching_nothing(self):
        """a pattern no collected job matches is surfaced as stale config -
        but one merely shadowed by an earlier rule is not, since it does match."""
        snapshot = {"branches": {"main": {"jobs": [self._job("Bots", "vuln", 10, 1)]}}}
        summary = render.health.excluded_summary(snapshot, ["main"], [
            {"workflow": "Bots"},                    # wins the first-match race
            {"workflow": "Bots", "job": "vuln"},     # shadowed, but does match
            {"job": "deploy-preview"},               # genuinely matches nothing
        ])
        self.assertEqual(summary["job_types"], 1)
        self.assertEqual(summary["jobs"][0]["pattern"], 'workflow="Bots"')
        self.assertEqual(summary["unmatched"], ['job="deploy-preview"'])

    def test_excluded_summary_is_none_without_excludes(self):
        snapshot = {"branches": {"main": {"jobs": [self._job("Tests", "unit", 5, 0)]}}}
        self.assertIsNone(render.health.excluded_summary(snapshot, ["main"], []))

    def test_branch_with_every_job_excluded_reads_as_no_data(self):
        """if the only jobs on a branch are all excluded, it must degrade to a
        'no data' branch (runs 0), not render 0.0% or crash."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/allexcluded"
            for date_str in ("2026-01-01", "2026-01-02"):
                self._write(data_dir, repo, date_str, [self._job("Bots", "vuln", 10, 3)])
            render_site(self._config(repo, [{"workflow": "Bots"}]), data_dir, tmp)

            slug = repo_slug(repo)
            with open(os.path.join(tmp, "ci-health", slug, "index.html")) as f:
                repo_page = f.read()
            self.assertIn("No push/schedule/workflow_dispatch runs on "
                          "<code>main</code>", repo_page)
            self.assertNotIn("0.0% flaky rate", repo_page)
            with open(os.path.join(tmp, "ci-health", "index.html")) as f:
                self.assertIn("<code>main</code>: no data", f.read())


class OverviewCardTest(unittest.TestCase):
    """The overview card shows each branch's own rate + status, not one pooled
    cross-branch number that matched neither branch's repo page."""

    def _write(self, data_dir, repo, date_str, branches):
        dest = os.path.join(data_dir, repo_slug(repo))
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, f"{date_str}.json"), "w") as f:
            json.dump(_snapshot_dict(repo, date_str, branches), f)

    def test_card_shows_per_branch_rate_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            repo = "example/cards"
            job = lambda runs, fails: {
                "workflow": "Tests", "job": "unit", "runs": runs, "failures": fails,
                "buckets": [{"runs": runs, "failures": fails}], "recent_failures": []}
            # main 1% (OK), squid 10% (above 3% threshold).
            self._write(data_dir, repo, "2026-01-01", {
                "main": {"jobs": [job(100, 1)]},
                "squid": {"jobs": [job(100, 10)]}})
            config = {"site": {"title": "Ceph QA"},
                      "groups": [{"name": "G", "repos": [{
                          "repo": repo, "branches": ["main", "squid"], "exclude": [], "preflight": [],
                          "lookback_days": 30, "top_jobs": 5,
                          "flaky_rate_threshold": 3.0}]}]}
            render_site(config, data_dir, tmp)

            with open(os.path.join(tmp, "ci-health", "index.html")) as f:
                overview = f.read()
            # Each branch's own rate, matching its repo page.
            self.assertIn("<code>main</code>: 1.0% (1 / 100)", overview)
            self.assertIn("<code>squid</code>: 10.0% (10 / 100)", overview)
            # Per-branch status: one OK, one above threshold.
            self.assertIn("p-status-label--positive", overview)
            self.assertIn("p-status-label--negative", overview)
            # No pooled headline: 11/200 = 5.5% must appear nowhere.
            self.assertNotIn("5.5%", overview)


class InfraFailuresTest(unittest.TestCase):
    """Preflight gate jobs and `Infra`-annotated failures leave the flaky
    rate; they feed each branch's infrastructure block and the incl.-infra rate."""

    REPO = "example/infra"
    GATE = [{"workflow": "Tests", "job": "Preflight infra gate"}]

    def _failure(self, date_str, run_id, kind=None, reason=""):
        f = {"date": date_str, "run_id": run_id,
             "url": f"https://github.com/example/infra/actions/runs/{run_id}/job/{run_id}0"}
        if kind:
            f["infra_kind"] = kind
            f["infra_reason"] = reason
        return f

    def _jobs(self):
        gate = {"workflow": "Tests", "job": "Preflight infra gate", "runs": 10, "failures": 2,
                "infra_failures": 0, "infra_kinds": {},
                "buckets": [{"runs": 10, "failures": 2, "infra_failures": 0}],
                "recent_failures": [self._failure("2026-01-03", 30, "preflight", "Snap Store down"),
                                    self._failure("2026-01-01", 10)]}
        unit = {"workflow": "Tests", "job": "unit", "runs": 20, "failures": 5,
                "infra_failures": 3, "infra_kinds": {"apt": 1, "snap-store": 2},
                "buckets": [{"runs": 20, "failures": 5, "infra_failures": 3}],
                "recent_failures": [self._failure("2026-01-04", 40, "snap-store",
                                                  "snap install failed: <nonce> 503"),
                                    self._failure("2026-01-02", 21),
                                    self._failure("2026-01-02", 20, "apt"),
                                    self._failure("2026-01-01", 11, "snap-store")]}
        return [gate, unit]

    def _render(self, tmp, preflight, days=("2026-01-05",)):
        data_dir = os.path.join(tmp, "data")
        dest = os.path.join(data_dir, repo_slug(self.REPO))
        os.makedirs(dest, exist_ok=True)
        for day in days:
            with open(os.path.join(dest, f"{day}.json"), "w") as f:
                json.dump(_snapshot_dict(self.REPO, day, {"main": {"jobs": self._jobs()}}), f)
        config = {"site": {"title": "Ceph QA"},
                  "groups": [{"name": "G", "repos": [{
                      "repo": self.REPO, "branches": ["main"], "exclude": [],
                      "preflight": preflight, "lookback_days": 30, "top_jobs": 5,
                      "flaky_rate_threshold": 3.0}]}]}
        with patch.object(render.health, "chart_payload",
                          wraps=render.health.chart_payload) as spy:
            render_site(config, data_dir, tmp)
        with open(os.path.join(tmp, "ci-health", repo_slug(self.REPO), "index.html")) as f:
            repo_page = f.read()
        with open(os.path.join(tmp, "ci-health", "index.html")) as f:
            overview = f.read()
        return repo_page, overview, spy.call_args_list[0].args[0][0][1]

    def test_branch_rates(self):
        rates = branch_rates(self._jobs(), self.GATE)
        # flaky: unit only, 3 infra runs dropped: (5-3)/(20-3).
        self.assertEqual((rates["failures"], rates["runs"]), (2, 17))
        self.assertAlmostEqual(rates["flaky"], 2 / 17 * 100)
        # incl. infra: every job, raw: (2+5)/(10+20).
        self.assertAlmostEqual(rates["incl_infra"], 7 / 30 * 100)
        # infra: gate failures + annotated failures over every run: (2+3)/30.
        self.assertEqual(rates["infra_failures"], 5)
        self.assertAlmostEqual(rates["infra"], 5 / 30 * 100)

    def test_repo_page_separates_infra(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _, _ = self._render(tmp, self.GATE)
        self.assertIn("11.8% flaky rate", page)
        self.assertIn("2 failures / 17 job runs", page)
        self.assertIn("Incl. infrastructure failures: 23.3% · 16.7% infrastructure failure rate "
                      "(5 infrastructure failures / 30 job runs", page)
        # One table: the gate first, marked, its failures under Infra only.
        table = page.partition('<table class="ci-jobs')[2]
        gate_at = table.index("Preflight infra gate</a> "
                              '<span class="p-status-label--information">Preflight gate</span>')
        self.assertLess(gate_at, table.index(">unit</a>"))
        gate_row = table[gate_at:].partition("</tr>")[0]
        self.assertIn('<td data-heading="Failures" class="u-align--right">—</td>'
                      '<td data-heading="Infra" class="u-align--right">2</td>'
                      '<td data-heading="Rate" class="u-align--right">20.0%</td>', gate_row)
        unit_row = table[table.index(">unit</a>"):].partition("</tr>")[0]
        self.assertIn('<td data-heading="Failures" class="u-align--right">2</td>'
                      '<td data-heading="Infra" class="u-align--right">3</td>', unit_row)

    def test_job_pages_mark_infra_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render(tmp, self.GATE)
            jobs_dir = os.path.join(tmp, "ci-health", repo_slug(self.REPO), "jobs")

            def recent(job):
                with open(os.path.join(jobs_dir, job_slug("Tests", job), "index.html")) as f:
                    return f.read().partition("Recent failures")[2]
            unit, gate = recent("unit"), recent("Preflight infra gate")
        marker = '<span class="p-status-label--caution">Infrastructure</span>'
        items = {line.partition("run ")[2].partition("<")[0]: line
                 for line in unit.splitlines() if "p-list__item" in line}
        # The reason from the annotation follows the tag, HTML-escaped.
        self.assertIn(f"{marker} <code>snap-store</code> snap install failed: &lt;nonce&gt; 503",
                      items["40"])
        self.assertIn(f"{marker} <code>apt</code>", items["20"])
        self.assertNotIn(marker, items["21"])  # a real failure
        self.assertEqual(gate.count(f"{marker} <code>preflight gate</code>"), 2)
        self.assertIn(f"{marker} <code>preflight gate</code> Snap Store down", gate)

    def test_overview_card_matches_repo_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, overview, _ = self._render(tmp, self.GATE)
        self.assertIn("<code>main</code>: 11.8% (2 / 17)", overview)
        self.assertIn("incl. infra: 23.3%", overview)

    def test_preflight_config_is_retroactive(self):
        days = ("2026-01-04", "2026-01-05")
        with tempfile.TemporaryDirectory() as tmp:
            _, _, without = self._render(tmp, [], days)
        with tempfile.TemporaryDirectory() as tmp:
            _, _, with_gate = self._render(tmp, self.GATE, days)
        # Annotated failures always leave the rate: (2+2)/(10+17) without a
        # gate; with one, the gate leaves too: 2/17 - on every historical point.
        self.assertEqual([round(v, 1) for _, v in without], [14.8, 14.8])
        self.assertEqual([round(v, 1) for _, v in with_gate], [11.8, 11.8])

    def test_snapshot_without_infra_fields_has_no_infra_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            dest = os.path.join(data_dir, repo_slug(self.REPO))
            os.makedirs(dest)
            job = {"workflow": "Tests", "job": "unit", "runs": 10, "failures": 1,
                   "buckets": [{"runs": 10, "failures": 1}], "recent_failures": []}
            with open(os.path.join(dest, "2026-01-05.json"), "w") as f:
                json.dump(_snapshot_dict(self.REPO, "2026-01-05", {"main": {"jobs": [job]}}), f)
            config = {"site": {"title": "Ceph QA"},
                      "groups": [{"name": "G", "repos": [{
                          "repo": self.REPO, "branches": ["main"], "exclude": [],
                          "preflight": [], "lookback_days": 30, "top_jobs": 5,
                          "flaky_rate_threshold": 3.0}]}]}
            render_site(config, data_dir, tmp)
            with open(os.path.join(tmp, "ci-health", repo_slug(self.REPO), "index.html")) as f:
                page = f.read()
        self.assertIn("10.0% flaky rate", page)
        self.assertNotIn("Incl. infrastructure", page)
        self.assertNotIn("Preflight gate", page)


if __name__ == "__main__":
    unittest.main()
