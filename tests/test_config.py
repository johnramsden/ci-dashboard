import os
import tempfile
import unittest

from ci_dashboard.config import ConfigError, is_excluded, job_slug, load_config, matching_exclude, repo_slug

EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                              "examples", "ceph-qa.yml")

VALID = """
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
"""


def load_text(text):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return load_config(path)
    finally:
        os.unlink(path)


class TestRealConfig(unittest.TestCase):

    def test_shipped_config_parses(self):
        """The ceph-qa example stays loadable (guards branch spellings)."""
        config = load_config(EXAMPLE_CONFIG)
        repos = {r["repo"]: r for g in config["groups"] for r in g["repos"]}
        self.assertIn("canonical/microceph", repos)
        self.assertIn("canonical/charm-microceph", repos)
        self.assertEqual(repos["canonical/microceph"]["branches"], ["main", "squid"])
        # charm-microceph's squid branch is stable/squid, NOT squid.
        self.assertEqual(repos["canonical/charm-microceph"]["branches"],
                         ["main", "stable/squid"])
        self.assertEqual(repos["canonical/microceph"]["exclude"],
                         [{"workflow": "Vuln scan (scheduled)"}])
        self.assertEqual(repos["canonical/microceph"]["preflight"],
                         [{"workflow": "Tests", "job": "Preflight infra gate"}])
        self.assertEqual(repos["canonical/charm-microceph"]["preflight"], [])
        self.assertEqual(config["site"]["title"], "Ceph QA")


class TestValidation(unittest.TestCase):

    def test_defaults_merged_into_repos(self):
        config = load_text(VALID)
        repo = config["groups"][0]["repos"][0]
        self.assertEqual(repo["lookback_days"], 30)
        self.assertEqual(repo["top_jobs"], 5)
        self.assertEqual(repo["flaky_rate_threshold"], 3.0)
        self.assertEqual(repo["exclude"], [])

    def test_per_repo_override(self):
        config = load_text("""
defaults:
  lookback_days: 60
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
        lookback_days: 7
      - repo: octo/other
        branches: [main]
""")
        repos = config["groups"][0]["repos"]
        self.assertEqual(repos[0]["lookback_days"], 7)
        self.assertEqual(repos[1]["lookback_days"], 60)

    def test_per_repo_top_jobs_and_threshold_override(self):
        """top_jobs and flaky_rate_threshold, like lookback_days, are
        overridable per repo, not just as file-wide defaults."""
        config = load_text("""
defaults:
  top_jobs: 5
  flaky_rate_threshold: 3.0
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
        top_jobs: 10
        flaky_rate_threshold: 1.5
      - repo: octo/other
        branches: [main]
""")
        repos = config["groups"][0]["repos"]
        self.assertEqual(repos[0]["top_jobs"], 10)
        self.assertEqual(repos[0]["flaky_rate_threshold"], 1.5)
        self.assertEqual(repos[1]["top_jobs"], 5)
        self.assertEqual(repos[1]["flaky_rate_threshold"], 3.0)

    def test_negative_top_jobs_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID.replace("branches: [main]", "branches: [main]\n        top_jobs: 0"))

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID + "\nrepositories: []\n")

    def test_empty_branches_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID.replace("branches: [main]", "branches: []"))

    def test_whitespace_branch_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID.replace("branches: [main]", 'branches: [" "]'))

    def test_malformed_repo_rejected(self):
        for bad in ("octo", "octo/repo/extra", "", "octo/repo\\n"):
            with self.assertRaises(ConfigError):
                load_text(f"""
groups:
  - name: G
    repos:
      - repo: "{bad}"
        branches: [main]
""")

    def test_duplicate_branches_rejected(self):
        """duplicates — including whitespace variants — would double-count."""
        with self.assertRaises(ConfigError):
            load_text(VALID.replace("branches: [main]", 'branches: ["main", " main"]'))

    def test_null_site_title_rejected(self):
        with self.assertRaises(ConfigError):
            load_text("site:\n  title:\n" + VALID)

    def test_non_string_unknown_keys_report_config_error(self):
        """YAML parses bare `on:` as a boolean; the unknown-key report must
        not crash sorting mixed-type keys."""
        with self.assertRaises(ConfigError):
            load_text(VALID + "\non: x\nextra: y\n")

    def test_exclude_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID + '        exclude:\n          - workflows: "Tests"\n')

    def test_exclude_empty_entry_rejected(self):
        with self.assertRaises(ConfigError):
            load_text(VALID + "        exclude:\n          - {}\n")

    def test_preflight_accepted(self):
        config = load_text(VALID + '        preflight:\n          - job: "Preflight*"\n')
        repo = config["groups"][0]["repos"][0]
        self.assertEqual(repo["preflight"], [{"job": "Preflight*"}])

    def test_preflight_validated_like_exclude(self):
        for bad in ('        preflight: "Preflight infra gate"\n',
                    '        preflight:\n          - jobs: "Preflight"\n',
                    "        preflight:\n          - {}\n",
                    '        preflight:\n          - job: ""\n'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, "preflight"):
                load_text(VALID + bad)

    def test_empty_groups_rejected(self):
        with self.assertRaises(ConfigError):
            load_text("groups: []\n")

    def test_duplicate_repo_rejected(self):
        with self.assertRaises(ConfigError):
            load_text("""
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
      - repo: octo/repo
        branches: [dev]
""")

    def test_slug_colliding_repos_rejected(self):
        """distinct repos whose slugs collide would share a data directory."""
        with self.assertRaises(ConfigError):
            load_text("""
groups:
  - name: G
    repos:
      - repo: a/b--c
        branches: [main]
      - repo: a--b/c
        branches: [main]
""")

    def test_invalid_yaml_reports_config_error(self):
        """a syntax error must surface as ConfigError, not a raw traceback."""
        with self.assertRaises(ConfigError):
            load_text("groups: [\n")

    def test_negative_lookback_rejected(self):
        with self.assertRaises(ConfigError):
            load_text("""
defaults:
  lookback_days: -1
""" + VALID)


RELEASES = """
groups:
  - name: G
    repos:
      - repo: octo/repo
        branches: [main]
releases:
  default_arch: amd64
  risks: [stable, candidate, edge]
  max_age_days: 730
  groups:
    - repo: canonical/microceph
      provenance: version-string
      assets: [microceph]
    - repo: canonical/ceph-charms
      provenance: git-info
      collapsed: true
      assets: [ceph-mon, ceph-osd]
"""


def releases_yaml(**overrides):
    """RELEASES with one `releases:` key replaced, as raw YAML text."""
    import yaml
    doc = yaml.safe_load(RELEASES)
    doc["releases"].update(overrides)
    return yaml.safe_dump(doc)


class TestReleasesConfig(unittest.TestCase):
    """The Releases page is entirely config-driven; a typo must not reach render."""

    def test_absent_block_means_no_releases_page(self):
        self.assertIsNone(load_text(VALID)["releases"])

    def test_valid_block_normalizes(self):
        releases = load_text(RELEASES)["releases"]
        self.assertEqual(releases["default_arch"], "amd64")
        self.assertEqual(releases["risks"], ["stable", "candidate", "edge"])
        self.assertEqual(releases["max_age_days"], 730)
        self.assertEqual(len(releases["groups"]), 2)
        self.assertFalse(releases["groups"][0]["collapsed"])
        self.assertTrue(releases["groups"][1]["collapsed"])

    def test_defaults_fill_in(self):
        import yaml
        doc = yaml.safe_load(RELEASES)
        for key in ("default_arch", "risks", "max_age_days"):
            del doc["releases"][key]
        releases = load_text(yaml.safe_dump(doc))["releases"]
        self.assertEqual(releases["default_arch"], "amd64")
        self.assertEqual(releases["risks"], ["stable", "candidate", "beta", "edge"])
        self.assertEqual(releases["max_age_days"], 730)

    def test_risk_order_is_preserved(self):
        """`risks:` is display order, so the config controls the columns."""
        releases = load_text(releases_yaml(risks=["edge", "stable"]))["releases"]
        self.assertEqual(releases["risks"], ["edge", "stable"])

    def _rejects(self, message_fragment, **overrides):
        with self.assertRaises(ConfigError) as caught:
            load_text(releases_yaml(**overrides))
        self.assertIn(message_fragment, str(caught.exception))

    def test_unknown_risk_rejected(self):
        self._rejects("unknown risk", risks=["stable", "alpha"])

    def test_duplicate_risk_rejected(self):
        self._rejects("duplicate", risks=["edge", "edge"])

    def test_empty_risks_rejected(self):
        self._rejects("non-empty list", risks=[])

    def test_non_positive_max_age_rejected(self):
        self._rejects("positive integer", max_age_days=0)

    def test_unknown_key_rejected(self):
        self._rejects("unknown key", colour="blue")

    def test_unknown_provenance_rejected(self):
        self._rejects("provenance must be one of", groups=[
            {"repo": "octo/repo", "provenance": "launchpad", "assets": ["x"]}])

    def test_non_string_provenance_is_a_config_error_not_a_crash(self):
        """`provenance: [git-tag]` is an easy YAML slip; a list is unhashable."""
        for bad in ([("git-tag")], {"a": 1}, 3, None):
            with self.subTest(provenance=bad):
                self._rejects("provenance must be one of", groups=[
                    {"repo": "octo/repo", "provenance": bad, "assets": ["x"]}])

    def test_bad_repo_rejected(self):
        self._rejects("owner/name", groups=[
            {"repo": "nope", "provenance": "git-tag", "assets": ["x"]}])

    def test_empty_assets_rejected(self):
        self._rejects("non-empty list", groups=[
            {"repo": "octo/repo", "provenance": "git-tag", "assets": []}])

    def test_non_bool_collapsed_rejected(self):
        self._rejects("true or false", groups=[
            {"repo": "octo/repo", "provenance": "git-tag", "assets": ["x"],
             "collapsed": "yes"}])

    def test_duplicate_asset_slug_rejected(self):
        """The snap and the charm may share a name; two charms may not."""
        self._rejects("duplicates another asset", groups=[
            {"repo": "octo/a", "provenance": "git-tag", "assets": ["ceph-mon"]},
            {"repo": "octo/b", "provenance": "git-info", "assets": ["ceph-mon"]}])

    def test_whitespace_duplicate_is_caught(self):
        """The dedup key must be the stripped name — it becomes the ledger key,
        and two sources sharing one would silently overwrite each other."""
        self._rejects("duplicates another asset", groups=[
            {"repo": "octo/a", "provenance": "git-tag",
             "assets": ["ceph-mon", " ceph-mon "]}])

    def test_assets_are_stripped(self):
        releases = load_text(releases_yaml(groups=[
            {"repo": "octo/a", "provenance": "git-tag",
             "assets": [" ceph-mon "]}]))["releases"]
        self.assertEqual(releases["groups"][0]["assets"], ["ceph-mon"])

    def test_snap_and_charm_may_share_a_name(self):
        releases = load_text(releases_yaml(groups=[
            {"repo": "canonical/microceph", "provenance": "version-string",
             "assets": ["microceph"]},
            {"repo": "canonical/charm-microceph", "provenance": "git-tag",
             "assets": ["microceph"]}]))["releases"]
        from ci_dashboard.provenance import build_sources
        self.assertEqual([s.slug for s in build_sources(releases)],
                         ["snap-microceph", "charm-microceph"])

    def test_shipped_config_builds_every_source(self):
        from ci_dashboard.provenance import build_sources
        sources = build_sources(load_config(EXAMPLE_CONFIG)["releases"])
        self.assertEqual(len(sources), 11)
        self.assertEqual(sources[0].slug, "snap-microceph")
        self.assertEqual(sum(s.collapsed for s in sources), 9)


class TestSlugs(unittest.TestCase):

    def test_repo_slug(self):
        self.assertEqual(repo_slug("canonical/microceph"), "canonical--microceph")

    def test_job_slug_deterministic(self):
        self.assertEqual(job_slug("Tests", "Multi node testing"),
                         job_slug("Tests", "Multi node testing"))

    def test_job_slug_separator_prevents_collisions(self):
        self.assertNotEqual(job_slug("A B", "C"), job_slug("A", "B C"))

    def test_job_slug_disambiguates_identical_sanitizations(self):
        # Both sanitize to the same prefix; the hash must differ.
        self.assertNotEqual(job_slug("Tests", "build/lint"),
                            job_slug("Tests", "build lint"))

    def test_job_slug_is_url_safe_and_bounded(self):
        slug = job_slug(
            "Main Documentation Checks",
            "documentation-checks / Link check / Check links in the documentation")
        self.assertRegex(slug, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(slug), 69)  # 60-char base + "-" + 8-char digest


class TestIsExcluded(unittest.TestCase):
    """render/health.py applies these against each snapshot's raw jobs."""

    def test_workflow_only_glob(self):
        excludes = [{"workflow": "Vuln scan*"}]
        self.assertTrue(is_excluded("Vuln scan (scheduled)", "anything", excludes))
        self.assertFalse(is_excluded("Tests", "anything", excludes))

    def test_job_only_glob(self):
        excludes = [{"job": "*govulncheck*"}]
        self.assertTrue(is_excluded("Tests", "Full govulncheck scan", excludes))
        self.assertFalse(is_excluded("Tests", "build", excludes))

    def test_workflow_and_job_must_both_match(self):
        excludes = [{"workflow": "Tests", "job": "flaky*"}]
        self.assertTrue(is_excluded("Tests", "flaky-suite", excludes))
        self.assertFalse(is_excluded("Tests", "build", excludes))
        self.assertFalse(is_excluded("Other", "flaky-suite", excludes))

    def test_case_sensitive(self):
        excludes = [{"workflow": "vuln*"}]
        self.assertFalse(is_excluded("Vuln scan (scheduled)", "x", excludes))

    def test_empty_excludes_never_match(self):
        self.assertFalse(is_excluded("Tests", "build", []))

    def test_matching_exclude_returns_the_first_matching_entry(self):
        """render/health.py names the rule that dropped a job, so which entry wins a
        multi-match matters: the earliest in config order."""
        broad = {"workflow": "Vuln scan*"}
        narrow = {"workflow": "Vuln scan (scheduled)", "job": "scan"}
        self.assertEqual(matching_exclude("Vuln scan (scheduled)", "scan", [broad, narrow]), broad)
        self.assertEqual(matching_exclude("Vuln scan (scheduled)", "scan", [narrow, broad]), narrow)
        self.assertIsNone(matching_exclude("Tests", "unit", [broad, narrow]))


if __name__ == "__main__":
    unittest.main()
