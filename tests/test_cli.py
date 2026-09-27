"""The `ci-dashboard` command, and the features the port from ceph-qa added:
`site.description`, `version_pattern`, and the public zip reader."""

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ci_dashboard import cli, simulate
from ci_dashboard.collect_releases import collect_source
from ci_dashboard.config import ConfigError, load_config
from ci_dashboard.provenance import (VERSION_STRING, Channel, SnapStoreSource,
                                     build_sources)
from ci_dashboard.render import render_site

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
EXAMPLES = os.path.join(ROOT, "examples")

MINIMAL = """\
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
"""


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


def run_cli(*argv, env=None):
    """(exit code, stdout, stderr) of cli.main(argv), in-process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            patch.dict(os.environ, env or {}, clear=env is not None):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TopLevelTest(unittest.TestCase):

    def test_help_lists_every_command(self):
        code, out, _ = run_cli("--help")
        self.assertEqual(code, 0)
        for name in cli.COMMANDS:
            self.assertIn(name, out)

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(run_cli()[0], 2)

    def test_unknown_command_is_a_usage_error(self):
        code, _, err = run_cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)

    def test_version(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^ci-dashboard \S+")

    def test_version_matches_pyproject(self):
        """The action runs from source, so __version__ is what it reports."""
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            declared = re.search(r'^version = "([^"]+)"$', f.read(), re.M).group(1)
        from ci_dashboard import __version__
        self.assertEqual(__version__, declared)

    def test_subcommand_help_exits_zero(self):
        for name in cli.COMMANDS:
            with self.subTest(name):
                self.assertEqual(run_cli(name, "--help")[0], 0)

    def test_python_dash_m_is_the_same_command(self):
        proc = subprocess.run([sys.executable, "-m", "ci_dashboard", "--version"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("ci-dashboard "))


class ValidateTest(unittest.TestCase):

    def test_every_example_config_validates(self):
        configs = [f for f in os.listdir(EXAMPLES) if f.endswith(".yml")
                   and not f.startswith("workflow")]
        self.assertGreaterEqual(len(configs), 2)
        for name in configs:
            with self.subTest(name):
                code, out, err = run_cli("validate", "--config", os.path.join(EXAMPLES, name))
                self.assertEqual(code, 0, err)
                self.assertIn("OK", out)

    def test_summary_names_repos_and_assets(self):
        code, out, _ = run_cli("validate", "--config", os.path.join(EXAMPLES, "ceph-qa.yml"))
        self.assertEqual(code, 0)
        self.assertIn("canonical/microceph: main, squid", out)
        self.assertIn("snap-microceph", out)

    def test_invalid_config_fails_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "c.yml"), "groups: []\n")
            code, _, err = run_cli("validate", "--config", path)
        self.assertEqual(code, 1)
        self.assertIn("groups must be a non-empty list", err)

    def test_missing_config_fails_cleanly(self):
        code, _, err = run_cli("validate", "--config", "/nonexistent/c.yml")
        self.assertEqual(code, 1)
        self.assertIn("cannot read /nonexistent/c.yml", err)


class RenderAndBuildTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site = os.path.join(self._tmp.name, "site")
        self.config = write(os.path.join(self._tmp.name, "c.yml"), """\
site:
  title: Test site
  description: Dashboards for the test team.
groups:
  - name: Examples
    repos:
      - repo: example/alpha
        branches: [main, squid]
      - repo: example/bravo
        branches: [main, stable/squid]
""")
        simulate.write_history(os.path.join(self.site, "data"))

    def tearDown(self):
        self._tmp.cleanup()

    def read(self, *rel):
        with open(os.path.join(self.site, *rel)) as f:
            return f.read()

    def test_render_defaults_the_data_dir_under_the_output(self):
        code, out, err = run_cli("render", "--config", self.config, "--output-dir", self.site)
        self.assertEqual(code, 0, err)
        self.assertIn("example/alpha", self.read("ci-health", "index.html"))
        self.assertTrue(os.path.isdir(os.path.join(self.site, "ci-health", "example--alpha",
                                                   "jobs")))

    def test_landing_page_carries_the_description(self):
        run_cli("render", "--config", self.config, "--output-dir", self.site)
        landing = self.read("index.html")
        self.assertIn("Dashboards for the test team.", landing)
        self.assertIn("<title>Test site</title>", landing)

    def test_build_without_collect_renders_existing_data(self):
        code, _, err = run_cli("build", "--config", self.config, "--output-dir", self.site,
                               "--no-collect", env={"PATH": os.environ["PATH"]})
        self.assertEqual(code, 0, err)
        self.assertIn("example/alpha", self.read("ci-health", "index.html"))

    def test_build_renders_even_when_collection_fails(self):
        """No token: collection fails offline, the site still renders, exit 1."""
        code, _, err = run_cli("build", "--config", self.config, "--output-dir", self.site,
                               env={"PATH": os.environ["PATH"]})
        self.assertEqual(code, 1)
        self.assertIn("GH_TOKEN must be set", err)
        self.assertIn("collection failed for CI metrics", err)
        self.assertIn("example/alpha", self.read("ci-health", "index.html"))

    def test_render_of_an_unsupported_snapshot_fails_cleanly(self):
        path = os.path.join(self.site, "data", "example--alpha", "2099-01-01.json")
        write(path, '{"schema_version": 99}')
        code, _, err = run_cli("render", "--config", self.config, "--output-dir", self.site)
        self.assertEqual(code, 1)
        self.assertIn("unsupported snapshot schema_version 99", err)

    def test_simulate_writes_a_site(self):
        out = os.path.join(self._tmp.name, "sim")
        code, _, err = run_cli("simulate", out)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(out, "releases", "index.html")))


class SiteDescriptionConfigTest(unittest.TestCase):

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            return load_config(write(os.path.join(tmp, "c.yml"), text))

    def test_defaults(self):
        site = self.load(MINIMAL)["site"]
        self.assertEqual(site, {"title": "CI Dashboard", "description": None})

    def test_description_is_stripped(self):
        site = self.load("site:\n  description: '  Ours.  '\n" + MINIMAL)["site"]
        self.assertEqual(site["description"], "Ours.")

    def test_blank_description_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "site.description"):
            self.load("site:\n  description: ''\n" + MINIMAL)

    def test_default_description_reaches_the_landing_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.load(MINIMAL)
            render_site(config, os.path.join(tmp, "data"), tmp)
            with open(os.path.join(tmp, "index.html")) as f:
                self.assertIn("Quality dashboards for the tracked repositories.", f.read())


def releases_yaml(group_extra):
    return MINIMAL + f"""\
releases:
  groups:
    - repo: octo/snap
      provenance: version-string
      assets: [thing]
{group_extra}"""


class VersionPatternConfigTest(unittest.TestCase):

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            return load_config(write(os.path.join(tmp, "c.yml"), text))

    def test_absent_means_none(self):
        group = self.load(releases_yaml(""))["releases"]["groups"][0]
        self.assertIsNone(group["version_pattern"])

    def test_pattern_reaches_the_source(self):
        config = self.load(releases_yaml(r"      version_pattern: '\+git([0-9a-f]{7,40})\Z'" + "\n"))
        source, = build_sources(config["releases"])
        self.assertEqual(source.version_pattern, r"\+git([0-9a-f]{7,40})\Z")

    def test_must_have_exactly_one_group(self):
        for pattern in (r"\+git[0-9a-f]+", r"(\d+)\+git([0-9a-f]+)"):
            with self.subTest(pattern), self.assertRaisesRegex(ConfigError, "exactly one"):
                self.load(releases_yaml(f"      version_pattern: '{pattern}'\n"))

    def test_must_compile(self):
        with self.assertRaisesRegex(ConfigError, "not a valid regular expression"):
            self.load(releases_yaml("      version_pattern: '(unclosed'\n"))

    def test_only_valid_on_version_string(self):
        text = MINIMAL + """\
releases:
  groups:
    - repo: octo/charm
      provenance: git-tag
      assets: [thing]
      version_pattern: '(x)'
"""
        with self.assertRaisesRegex(ConfigError, "only applies to provenance: version-string"):
            self.load(text)


class VersionPatternSourceTest(unittest.TestCase):

    def resolve(self, version, pattern=None):
        source = SnapStoreSource("thing", repo="octo/snap", version_pattern=pattern)
        return source.resolve(Channel(track="latest", risk="stable", revision=1,
                                      version=version))

    def test_default_pattern_is_unchanged(self):
        self.assertEqual(self.resolve("19.2.3+snapcf306793a4").commit, "cf306793a4")
        self.assertIsNone(self.resolve("1.2.3+git.cf30679").commit)

    def test_custom_pattern(self):
        got = self.resolve("1.2.3+git.cf30679", r"\+git\.([0-9a-f]{7,40})\Z")
        self.assertEqual(got.commit, "cf30679")
        self.assertEqual(got.provenance, VERSION_STRING)

    def test_miss_names_the_pattern(self):
        got = self.resolve("1.2.3", r"\+git\.([0-9a-f]{7,40})\Z")
        self.assertIsNone(got.commit)
        self.assertIn(r"\+git\.", got.note)

    def test_capture_must_look_like_a_commit(self):
        """A loose pattern must not turn a version number into a commit link."""
        got = self.resolve("1.2.3-45", r"-(\d+)\Z")
        self.assertIsNone(got.commit)
        self.assertIn("not a commit hash", got.note)

    def test_uppercase_hex_capture_is_a_commit(self):
        got = self.resolve("1.2.3-ABCDEF1", r"-([0-9A-Fa-f]{7,40})\Z")
        self.assertEqual(got.commit, "abcdef1")

    def test_optional_group_that_did_not_participate_is_unknown(self):
        got = self.resolve("1.2.3", r"1\.2\.3(\+[0-9a-f]+)?\Z")
        self.assertIsNone(got.commit)
        self.assertIn("not a commit hash", got.note)

    def test_catastrophic_pattern_times_out_instead_of_hanging(self):
        """A nested-quantifier pattern that backtracks for minutes on a near
        miss must cost at most the timeout, once, not hang the collector."""
        from ci_dashboard.provenance import sources
        pattern = r"^(?:[a-z]+-)*([0-9a-f]+)+$"
        source = SnapStoreSource("thing", repo="octo/snap", version_pattern=pattern)
        near_miss = "myapp-" + "a" * 40 + "!"
        with patch.object(sources, "PATTERN_TIMEOUT", 0.5):
            import time
            started = time.monotonic()
            first = source.resolve(Channel(track="t", risk="stable", revision=1,
                                           version=near_miss))
            second = source.resolve(Channel(track="t", risk="edge", revision=2,
                                            version="myapp-abcdef1"))
            elapsed = time.monotonic() - started
        self.assertIsNone(first.commit)
        self.assertIn("backtracks catastrophically", first.note)
        self.assertIsNone(second.commit, "a timed-out pattern is not retried per revision")
        self.assertLess(elapsed, 5)

    def test_snaps_re_resolve_every_run(self):
        """A corrected pattern must reach revisions resolved under the old one."""
        channels = [Channel(track="latest", risk="stable", revision=5,
                            version="1.0+git.abcdef1")]

        class Snap(SnapStoreSource):
            def channels(self):
                return channels

        stale = {"5": {"commit": None, "branch": None, "provenance": "unknown",
                       "note": "old pattern", "retryable": False}}
        with contextlib.redirect_stdout(io.StringIO()):
            entry = collect_source(
                Snap("thing", repo="octo/snap", version_pattern=r"\+git\.([0-9a-f]+)\Z"),
                stale, now=__import__("datetime").datetime(2026, 1, 1))
        self.assertEqual(entry["revisions"]["5"]["commit"], "abcdef1")


class ReleasesFooterTest(unittest.TestCase):
    """The footer names only the services the configured sources read."""

    def sources(self, *groups):
        return build_sources({"groups": [
            {"repo": "o/r", "provenance": p, "assets": ["a"], "collapsed": False}
            for p in groups]})

    def test_names_only_what_is_used(self):
        from ci_dashboard.render.releases import data_sources
        self.assertEqual(data_sources(self.sources("version-string")), "the Snap Store")
        self.assertEqual(data_sources(self.sources("git-info")), "Charmhub")
        self.assertEqual(data_sources(self.sources("git-tag")), "Charmhub and GitHub")
        self.assertEqual(data_sources(self.sources("version-string", "git-tag", "git-info")),
                         "the Snap Store, Charmhub, and GitHub")


class PublicApiTest(unittest.TestCase):
    """What other repositories import from this package; do not break it.

    ceph-qa's scripts/charm-build-info.py reads git-info.txt out of published
    charms with the same reader the collector uses.
    """

    def test_charmzip_reader(self):
        from ci_dashboard.provenance.charmzip import GIT_INFO_FILE, fetch_git_info
        self.assertEqual(GIT_INFO_FILE, "git-info.txt")
        self.assertTrue(callable(fetch_git_info))


if __name__ == "__main__":
    unittest.main()
