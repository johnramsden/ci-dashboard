import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ci_dashboard import collect_releases
from ci_dashboard.collect_releases import build_ledger, collect_source, load_ledger, write_ledger
from ci_dashboard.provenance import (GIT_INFO, GIT_TAG, UNKNOWN, VERSION_STRING, Channel,
                        CharmGitInfoSource, CharmTagSource, ReleaseSource,
                        Resolution, SnapStoreSource, build_sources, unknown)
from ci_dashboard.render import releases as render_releases

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                              "examples", "ceph-qa.yml")


def snap_channel_map(*rows):
    """rows: (track, risk, arch, revision, version)"""
    return {"channel-map": [
        {"channel": {"track": t, "risk": r, "architecture": a,
                     "released-at": "2026-07-01T00:00:00Z"},
         "revision": rev, "version": ver}
        for t, r, a, rev, ver in rows
    ]}


def charm_channel_map(*rows):
    """rows: (track, risk, arch, base, revision)"""
    return {"channel-map": [
        {"channel": {"track": t, "risk": r, "released-at": "2026-07-01T00:00:00Z",
                     "base": {"architecture": a, "channel": b, "name": "ubuntu"}},
         "revision": {"revision": rev, "version": str(rev),
                      "download": {"url": f"https://cdn.example/{rev}.charm"}}}
        for t, r, a, b, rev in rows
    ]}


class SnapVersionParsingTest(unittest.TestCase):
    """The version string is the only commit signal the Snap Store exposes."""

    def setUp(self):
        self.source = SnapStoreSource("microceph", repo="canonical/microceph")

    def resolve(self, version):
        return self.source.resolve(
            Channel(track="squid", risk="stable", revision=1, version=version))

    def test_current_scheme_yields_the_commit(self):
        got = self.resolve("19.2.3+snapcf306793a4")
        self.assertEqual(got.commit, "cf306793a4")
        self.assertEqual(got.provenance, VERSION_STRING)

    def test_short_abbrev_still_parses(self):
        self.assertEqual(self.resolve("20.2.1+snap73293b5").commit, "73293b5")

    def test_legacy_git_scheme_is_unknown(self):
        """The quincy track's 0+git.<sha> scheme is deliberately unsupported."""
        got = self.resolve("0+git.4a608fc")
        self.assertIsNone(got.commit)
        self.assertEqual(got.provenance, UNKNOWN)
        self.assertIn("0+git.4a608fc", got.note)

    def test_dirty_build_is_unknown(self):
        """A dirty tree means the commit does not describe the artifact."""
        self.assertIsNone(self.resolve("19.2.3+snapcf306793a4-dirty").commit)

    def test_describe_with_reachable_tag_is_unknown(self):
        """If a tag ever becomes reachable, describe emits <tag>-<n>-g<sha>."""
        self.assertIsNone(self.resolve("19.2.3+snapv19.2.0+squid-12-gcf30679").commit)

    def test_missing_version_is_unknown(self):
        self.assertIsNone(self.resolve(None).commit)

    def test_non_hex_suffix_is_unknown(self):
        self.assertIsNone(self.resolve("19.2.3+snapzzzzzzzzzz").commit)

    def test_channels_collects_every_architecture(self):
        """The renderer picks one; the collector must not choose for it.

        arm64 is a different revision, and sometimes a different commit.
        """
        payload = snap_channel_map(
            ("squid", "stable", "amd64", 1735, "19.2.3+snapcf306793a4"),
            ("squid", "stable", "arm64", 1736, "19.2.3+snapcf306793a4"),
        )
        with patch("ci_dashboard.provenance.sources.snap_info", return_value=payload):
            channels = self.source.channels()
        self.assertEqual([(c.arch, c.revision) for c in channels],
                         [("amd64", 1735), ("arm64", 1736)])


class CharmTagResolutionTest(unittest.TestCase):
    """charm-microceph's publish workflow tags the commit it built."""

    def setUp(self):
        self.source = CharmTagSource("microceph", repo="canonical/charm-microceph")

    def _with_tags(self, *names_and_shas):
        tags = [{"name": n, "commit": {"sha": s}} for n, s in names_and_shas]
        return patch("ci_dashboard.provenance.sources.gh_paginate", return_value=tags)

    def resolve(self, revision):
        return self.source.resolve(
            Channel(track="squid", risk="stable", revision=revision))

    def test_bare_rev_tag(self):
        with self._with_tags(("rev255", "a" * 40)):
            got = self.resolve(255)
        self.assertEqual(got.commit, "a" * 40)
        self.assertEqual(got.provenance, GIT_TAG)

    def test_track_prefixed_tag(self):
        with self._with_tags(("squid-rev296", "b" * 40)):
            self.assertEqual(self.resolve(296).commit, "b" * 40)

    def test_untagged_revision_is_unknown(self):
        """Revisions 260 and 293 were published without a tag; they stay unknown."""
        with self._with_tags(("squid-rev296", "b" * 40)):
            got = self.resolve(293)
        self.assertIsNone(got.commit)
        self.assertIn("no rev293 tag", got.note)

    def test_unrelated_tags_ignored(self):
        with self._with_tags(("v1.2.3", "c" * 40), ("rev-not-a-number", "d" * 40)):
            self.assertIsNone(self.resolve(1).commit)

    def test_tags_fetched_once_across_revisions(self):
        with self._with_tags(("rev1", "a" * 40), ("rev2", "b" * 40)) as paginate:
            self.resolve(1)
            self.resolve(2)
        self.assertEqual(paginate.call_count, 1)

    def test_duplicate_tags_agreeing_on_the_commit_resolve(self):
        """rev300 and tentacle-rev300 may coexist; if they agree, that is fine."""
        with self._with_tags(("rev300", "a" * 40), ("tentacle-rev300", "a" * 40)):
            self.assertEqual(self.resolve(300).commit, "a" * 40)

    def test_conflicting_tags_refuse_to_guess(self):
        """Two tags, two commits, one revision: pick neither, and say why."""
        with self._with_tags(("rev300", "a" * 40), ("tentacle-rev300", "b" * 40)):
            got = self.resolve(300)
        self.assertIsNone(got.commit)
        self.assertIn("disagree", got.note)
        self.assertIn("rev300", got.note)
        self.assertIn("tentacle-rev300", got.note)

    def test_conflict_note_is_order_independent(self):
        """Whichever order the API lists them, the note reads the same."""
        with self._with_tags(("rev300", "a" * 40), ("tentacle-rev300", "b" * 40)):
            first = self.resolve(300).note
        self.source._tag_cache = None
        with self._with_tags(("tentacle-rev300", "b" * 40), ("rev300", "a" * 40)):
            second = self.resolve(300).note
        self.assertEqual(first, second)


class CharmGitInfoResolutionTest(unittest.TestCase):
    """ceph-charms embeds git-info.txt in the .charm."""

    def setUp(self):
        self.source = CharmGitInfoSource("ceph-mon", repo="canonical/ceph-charms")
        self.channel = Channel(track="squid", risk="stable", revision=498,
                               download_url="https://cdn.example/498.charm")

    def test_git_info_present(self):
        info = {"commit": "e67ab37ad305", "branch": "main",
                "commit_date": "2026-04-20T15:57:44-07:00"}
        with patch("ci_dashboard.provenance.sources.fetch_git_info", return_value=info):
            got = self.source.resolve(self.channel)
        self.assertEqual(got.commit, "e67ab37ad305")
        self.assertEqual(got.branch, "main")
        self.assertEqual(got.provenance, GIT_INFO)
        self.assertFalse(got.retryable)

    def test_absent_git_info_is_permanently_unknown(self):
        with patch("ci_dashboard.provenance.sources.fetch_git_info", return_value=None):
            got = self.source.resolve(self.channel)
        self.assertIsNone(got.commit)
        self.assertFalse(got.retryable, "absence is definitive, not transient")

    def test_network_failure_is_retryable(self):
        with patch("ci_dashboard.provenance.sources.fetch_git_info", side_effect=OSError("boom")):
            got = self.source.resolve(self.channel)
        self.assertIsNone(got.commit)
        self.assertTrue(got.retryable, "a flaky CDN must not poison the ledger")

    def test_missing_download_url_is_retryable(self):
        """Charmhub always sends one; its absence is a bad response, not a fact."""
        got = self.source.resolve(Channel(track="t", risk="stable", revision=1))
        self.assertIsNone(got.commit)
        self.assertTrue(got.retryable)


class FakeSource(ReleaseSource):
    """A source with no network, for exercising the collector."""

    kind = "snap"
    columns = (("Channel", "channel"), ("Rev", "revision"))

    def __init__(self, channels, resolutions):
        super().__init__("fake", repo="octo/repo")
        self._channels = channels
        self._resolutions = resolutions
        self.resolved = []

    def channels(self):
        return self._channels

    def resolve(self, channel):
        self.resolved.append(channel.revision)
        return self._resolutions[channel.revision]


class CollectSourceTest(unittest.TestCase):

    def make(self, *revisions, **resolutions):
        channels = [Channel(track="squid", risk="stable", revision=r)
                    for r in revisions]
        return FakeSource(channels, {int(k[1:]): v for k, v in resolutions.items()})

    def test_new_revisions_are_resolved(self):
        source = self.make(1, 2, r1=Resolution(commit="a" * 40, provenance=GIT_TAG),
                           r2=Resolution(commit="b" * 40, provenance=GIT_TAG))
        out = collect_source(source, {}, NOW)
        self.assertEqual(sorted(source.resolved), [1, 2])
        self.assertEqual(out["revisions"]["1"]["commit"], "a" * 40)

    def test_cached_revisions_are_not_refetched(self):
        source = self.make(1, 2, r1=Resolution(commit="a" * 40),
                           r2=Resolution(commit="b" * 40))
        cached = {"1": Resolution(commit="a" * 40, provenance=GIT_TAG).to_dict()}
        collect_source(source, cached, NOW)
        self.assertEqual(source.resolved, [2], "revision 1 came from the cache")

    def test_retryable_failures_are_reattempted(self):
        source = self.make(1, r1=Resolution(commit="a" * 40, provenance=GIT_TAG))
        cached = {"1": unknown("cdn timed out", retryable=True).to_dict()}
        out = collect_source(source, cached, NOW)
        self.assertEqual(source.resolved, [1])
        self.assertEqual(out["revisions"]["1"]["commit"], "a" * 40)

    def test_permanent_unknowns_are_not_reattempted(self):
        source = self.make(1, r1=Resolution(commit="a" * 40))
        cached = {"1": unknown("charm has no git-info.txt").to_dict()}
        collect_source(source, cached, NOW)
        self.assertEqual(source.resolved, [], "a definitive answer is cached")

    def test_a_raising_resolve_does_not_discard_earlier_work(self):
        """20 range reads; the 15th failing must not throw away the first 14."""
        source = self.make(1, 2, 3, r1=Resolution(commit="a" * 40, provenance=GIT_TAG),
                           r2=Resolution(commit="b" * 40, provenance=GIT_TAG),
                           r3=Resolution(commit="c" * 40, provenance=GIT_TAG))
        real_resolve = source.resolve

        def resolve(channel):
            if channel.revision == 2:
                raise OSError("cdn exploded")
            return real_resolve(channel)

        source.resolve = resolve
        out = collect_source(source, {}, NOW)
        self.assertEqual(out["revisions"]["1"]["commit"], "a" * 40)
        self.assertEqual(out["revisions"]["3"]["commit"], "c" * 40)
        self.assertIsNone(out["revisions"]["2"]["commit"])
        self.assertTrue(out["revisions"]["2"]["retryable"],
                        "the failed one is retried, the others are kept")

    def test_non_dict_cached_resolution_is_dropped_not_fatal(self):
        source = self.make(1, r1=Resolution(commit="a" * 40, provenance=GIT_TAG))
        out = collect_source(source, {"1": "not-a-mapping"}, NOW)
        self.assertEqual(out["revisions"]["1"]["commit"], "a" * 40,
                         "the junk entry is discarded and the revision re-resolved")

    def test_non_numeric_cached_key_is_dropped_not_fatal(self):
        """A corrupt key would otherwise wedge the source on every future run."""
        source = self.make(1, r1=Resolution(commit="a" * 40, provenance=GIT_TAG))
        out = collect_source(source, {"oops": {"commit": "x"}}, NOW)
        self.assertNotIn("oops", out["revisions"])
        self.assertEqual(out["revisions"]["1"]["commit"], "a" * 40)

    def test_one_channel_per_revision_is_resolved(self):
        """A revision on three channels is resolved once, not three times."""
        channels = [Channel(track="squid", risk=r, revision=7)
                    for r in ("stable", "candidate", "edge")]
        source = FakeSource(channels, {7: Resolution(commit="a" * 40)})
        out = collect_source(source, {}, NOW)
        self.assertEqual(source.resolved, [7])
        self.assertEqual(len(out["channels"]), 3)

    def test_history_is_retained_for_delisted_revisions(self):
        source = self.make(2, r2=Resolution(commit="b" * 40))
        cached = {"1": Resolution(commit="a" * 40, provenance=GIT_TAG).to_dict()}
        out = collect_source(source, cached, NOW)
        self.assertIn("1", out["revisions"], "a revision that left every channel "
                                             "keeps its resolved commit")


class BuildLedgerTest(unittest.TestCase):

    def source(self, name):
        src = FakeSource([Channel(track="t", risk="stable", revision=1)],
                         {1: Resolution(commit="a" * 40, provenance=GIT_TAG)})
        src.name = name
        return src

    def test_failing_source_keeps_previous_rows(self):
        good = self.source("good")
        bad = self.source("bad")
        bad.channels = lambda: (_ for _ in ()).throw(OSError("store down"))
        cached = {"snap-bad": {"title": "bad", "revisions": {"9": {"commit": "old"}}}}
        ledger, failed = build_ledger([good, bad], cached, NOW)
        self.assertEqual(failed, ["snap-bad"])
        self.assertEqual(ledger["sources"]["snap-bad"]["revisions"]["9"]["commit"],
                         "old", "yesterday's data survives a store outage")
        self.assertIn("snap-good", ledger["sources"])

    def test_collected_at_stamps_a_successful_source(self):
        ledger, _ = build_ledger([self.source("good")], {}, NOW)
        self.assertEqual(ledger["sources"]["snap-good"]["collected_at"],
                         ledger["generated_at"])

    def test_failing_source_keeps_its_old_collected_at(self):
        """A carried-forward table must not be dated from this run's clock."""
        bad = self.source("bad")
        bad.channels = lambda: (_ for _ in ()).throw(OSError("store down"))
        cached = {"snap-bad": {"title": "bad", "revisions": {},
                               "collected_at": "2026-01-01T00:00:00Z"}}
        ledger, _ = build_ledger([bad], cached, NOW)
        self.assertEqual(ledger["generated_at"], "2026-07-09T12:00:00Z")
        self.assertEqual(ledger["sources"]["snap-bad"]["collected_at"],
                         "2026-01-01T00:00:00Z")

    def test_first_ever_run_failing_still_renders_the_asset(self):
        """No cache to fall back on: the asset must not vanish from the page."""
        bad = self.source("bad")
        bad.channels = lambda: (_ for _ in ()).throw(OSError("store down"))
        ledger, failed = build_ledger([bad], {}, NOW)
        self.assertEqual(failed, ["snap-bad"])
        self.assertIn("snap-bad", ledger["sources"],
                      "a never-collected asset gets a placeholder, not omission")
        entry = ledger["sources"]["snap-bad"]
        self.assertIsNone(entry["collected_at"])
        self.assertEqual(entry["channels"], [])

        sections = render_releases.release_sections(
            ledger["sources"], ledger["generated_at"], [bad], FIXTURE_RISKS,
            MAX_AGE, DEFAULT_ARCH)
        self.assertEqual([s["slug"] for s in sections], ["snap-bad"])
        self.assertTrue(sections[0]["never_collected"])

    def test_full_run_prunes_removed_assets(self):
        cached = {"snap-gone": {"title": "gone", "revisions": {}}}
        ledger, _ = build_ledger([self.source("kept")], cached, NOW)
        self.assertNotIn("snap-gone", ledger["sources"])

    def test_filtered_run_preserves_other_assets(self):
        cached = {"snap-other": {"title": "other", "revisions": {}}}
        ledger, _ = build_ledger([self.source("kept")], cached, NOW,
                                 keep_uncollected=True)
        self.assertIn("snap-other", ledger["sources"],
                      "--source must not wipe the assets it did not collect")
        self.assertIn("snap-kept", ledger["sources"])

    def test_generated_at_comes_from_the_clock_argument(self):
        ledger, _ = build_ledger([], {}, NOW)
        self.assertEqual(ledger["generated_at"], "2026-07-09T12:00:00Z")


class LedgerRoundTripTest(unittest.TestCase):

    def test_write_then_load(self):
        with tempfile.TemporaryDirectory() as site:
            ledger, _ = build_ledger([], {}, NOW)
            ledger["sources"] = {"snap-x": {"title": "x", "revisions": {}}}
            write_ledger(ledger, site)
            self.assertEqual(load_ledger(site), (ledger["sources"], False))

    def test_missing_ledger_is_empty_and_not_discarded(self):
        """"never existed" must be distinguishable from "thrown away"."""
        with tempfile.TemporaryDirectory() as site:
            self.assertEqual(load_ledger(site), ({}, False))

    def test_foreign_schema_version_is_discarded(self):
        with tempfile.TemporaryDirectory() as site:
            path = collect_releases.ledger_path(site)
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump({"schema_version": 99, "sources": {"a": {}}}, f)
            self.assertEqual(load_ledger(site), ({}, True),
                             "an unreadable cache is rebuilt, never half-migrated")

    def test_corrupt_json_is_discarded_not_fatal(self):
        """A truncated write must not stop the collector from rebuilding."""
        with tempfile.TemporaryDirectory() as site:
            path = collect_releases.ledger_path(site)
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write('{"schema_version": 2, "sourc')
            self.assertEqual(load_ledger(site), ({}, True))

    def test_failed_write_leaves_no_tmp_behind(self):
        class Unserializable:
            pass
        with tempfile.TemporaryDirectory() as site:
            ledger, _ = build_ledger([], {}, NOW)
            ledger["sources"] = {"x": Unserializable()}
            with self.assertRaises(TypeError):
                write_ledger(ledger, site)
            leftovers = os.listdir(os.path.join(site, "releases-data"))
            self.assertEqual(leftovers, [], "a partial .tmp must not survive")

    def test_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as site:
            ledger, _ = build_ledger([], {}, NOW)
            write_ledger(ledger, site)
            leftovers = [n for n in os.listdir(os.path.join(site, "releases-data"))
                         if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])


class SourceRunGuardTest(unittest.TestCase):
    """`--source` must never turn a discarded ledger into a one-asset ledger."""

    def run_main(self, site, argv):
        from ci_dashboard import collect_releases as cr
        with patch.object(sys, "argv", ["collect_releases.py"] + argv):
            return cr.main()

    def test_source_run_refuses_when_the_ledger_was_discarded(self):
        with tempfile.TemporaryDirectory() as site:
            path = collect_releases.ledger_path(site)
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:                      # a schema-2 ledger
                json.dump({"schema_version": 2, "sources": {
                    "snap-microceph": {}, "charm-microceph": {}}}, f)
            with self.assertRaises(SystemExit) as caught:
                self.run_main(site, ["--config", EXAMPLE_CONFIG,
                                     "--output-dir", site,
                                     "--source", "snap-microceph"])
            self.assertEqual(caught.exception.code, 1)
            # and it did not overwrite the ledger on its way out
            with open(path) as f:
                self.assertEqual(json.load(f)["schema_version"], 2)

    def test_unsatisfiable_source_errors_even_with_no_releases_configured(self):
        """An explicit --source must never be a quiet no-op."""
        with tempfile.TemporaryDirectory() as site:
            cfg = os.path.join(site, "config.yaml")
            with open(cfg, "w") as f:
                f.write("groups:\n  - name: G\n    repos:\n"
                        "      - repo: octo/repo\n        branches: [main]\n")
            with self.assertRaises(SystemExit) as caught:
                self.run_main(site, ["--config", cfg, "--output-dir", site,
                                     "--source", "snap-microceph"])
            self.assertEqual(caught.exception.code, 1)

    def test_no_releases_configured_without_source_is_a_clean_no_op(self):
        with tempfile.TemporaryDirectory() as site:
            cfg = os.path.join(site, "config.yaml")
            with open(cfg, "w") as f:
                f.write("groups:\n  - name: G\n    repos:\n"
                        "      - repo: octo/repo\n        branches: [main]\n")
            self.run_main(site, ["--config", cfg, "--output-dir", site])
            self.assertFalse(os.path.exists(collect_releases.ledger_path(site)),
                             "an unconfigured run must not write a ledger")

    def test_source_run_is_fine_when_there_is_no_ledger_at_all(self):
        """A fresh checkout has nothing to lose; only a *discard* is dangerous."""
        cached, discarded = load_ledger(tempfile.mkdtemp())
        self.assertEqual((cached, discarded), ({}, False))


class ChannelTest(unittest.TestCase):

    def test_download_url_is_not_serialized(self):
        """The CDN url carries an expiring token; persisting it would mislead."""
        channel = Channel(track="squid", risk="stable", revision=1,
                          download_url="https://cdn.example/x?token=secret")
        self.assertNotIn("download_url", channel.to_dict())


class ResolutionTest(unittest.TestCase):

    def test_unknown_has_no_commit(self):
        self.assertFalse(unknown("nope").known)

    def test_retryable_survives_serialization(self):
        self.assertTrue(unknown("cdn timed out", retryable=True)
                        .to_dict()["retryable"])


FRESH = "2026-07-09T12:00:00Z"
DEAD = "2022-03-16T00:00:00Z"     # older than RELEASE_MAX_AGE_DAYS


def _chan(track, risk, revision, **kw):
    channel = {"track": track, "risk": risk, "revision": revision,
               "version": None, "arch": "amd64", "base": None,
               "released_at": FRESH}
    channel.update(kw)
    return channel


def _res(commit=None, provenance=UNKNOWN, branch=None, note=""):
    return {"commit": commit, "branch": branch, "provenance": provenance,
            "note": note, "retryable": False}


def ledger_fixture():
    """A ledger exercising every provenance tier, a revision on two risks,
    a multi-base charm, a beta risk, and a channel past the active cutoff."""
    return {
        "schema_version": collect_releases.SCHEMA_VERSION,
        "generated_at": FRESH,
        "sources": {
            "snap-microceph": {
                "collected_at": FRESH,
                "channels": [
                    _chan("squid", "stable", 1735,
                          version="19.2.3+snapcf306793a4"),
                    _chan("squid", "candidate", 1735,
                          version="19.2.3+snapcf306793a4"),
                    _chan("squid", "edge", 1701,
                          version="19.2.3+snapfd93b718e2"),
                    _chan("quincy", "stable", 793, version="0+git.4a608fc"),
                    # Long dead: hidden by the active-channel cutoff.
                    _chan("luminous", "stable", 3, released_at=DEAD),
                ],
                "revisions": {
                    "1735": _res("cf306793a4", VERSION_STRING),
                    "1701": _res("fd93b718e2", VERSION_STRING),
                    "793": _res(note="legacy scheme"),
                    "3": _res("aaaaaaaaaa", VERSION_STRING),
                },
            },
            "charm-microceph": {
                "collected_at": FRESH,
                "channels": [
                    _chan("squid", "stable", 293, base="22.04"),
                    _chan("squid", "stable", 296, base="24.04"),
                    _chan("squid", "beta", 296, base="24.04"),
                ],
                "revisions": {
                    "293": _res(note="no rev293 tag"),
                    "296": _res("b" * 40, GIT_TAG),
                },
            },
            "charm-ceph-mon": {
                "collected_at": FRESH,
                "channels": [
                    # A live row with a stale stable: the cutoff must not drop
                    # that cell, or `·` would claim nothing is on stable.
                    _chan("squid", "stable", 498, base="24.04", released_at=DEAD),
                    _chan("squid", "edge", 572, base="24.04"),
                    # Every risk stale: the whole row goes.
                    _chan("luminous", "edge", 73, base="16.04", released_at=DEAD),
                ],
                "revisions": {
                    "498": _res("e67ab37ad305", GIT_INFO, branch="main"),
                    "572": _res("240008eb1f93", GIT_INFO, branch="main"),
                    "73": _res(note="charm has no git-info.txt"),
                },
            },
        },
    }


class ParseIsoTest(unittest.TestCase):
    """The ledger writes Z; both stores write microseconds and +00:00."""

    def test_ledger_format(self):
        self.assertEqual(render_releases.parse_iso("2026-07-09T12:00:00Z"),
                         datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc))

    def test_real_store_format(self):
        """Exactly what api.snapcraft.io and api.charmhub.io return."""
        self.assertEqual(render_releases.parse_iso("2026-07-06T14:41:16.990050+00:00"),
                         datetime(2026, 7, 6, 14, 41, 16, 990050, tzinfo=timezone.utc))

    def test_non_utc_offset_is_normalized(self):
        """String comparison would put this hours off; parsing does not."""
        self.assertEqual(render_releases.parse_iso("2026-07-06T09:41:16-05:00"),
                         datetime(2026, 7, 6, 14, 41, 16, tzinfo=timezone.utc))

    def test_naive_is_assumed_utc(self):
        self.assertEqual(render_releases.parse_iso("2026-07-06T14:41:16"),
                         datetime(2026, 7, 6, 14, 41, 16, tzinfo=timezone.utc))

    def test_garbage_and_empty_are_none(self):
        for value in (None, "", "   ", "not-a-date", "2026-13-45T99:99:99Z"):
            self.assertIsNone(render_releases.parse_iso(value), value)


class ActiveCutoffTest(unittest.TestCase):
    """Stores never retire a channel, so dead ones must be hidden."""

    def cutoff(self):
        return render_releases.active_cutoff("2026-07-09T12:00:00Z", 730)

    def test_cutoff_is_derived_from_the_ledger_not_the_clock(self):
        """730 days before 2026-07-09; 2024 is a leap year, so the day matches."""
        self.assertEqual(self.cutoff(),
                         datetime(2024, 7, 9, 12, 0, tzinfo=timezone.utc))

    def test_unparseable_generated_at_raises_rather_than_hiding_everything(self):
        with self.assertRaises(RuntimeError):
            render_releases.active_cutoff("yesterday", 730)

    def test_channel_released_before_the_cutoff_is_inactive(self):
        self.assertFalse(render_releases._is_active(
            _chan("luminous", "stable", 1, released_at=DEAD), self.cutoff()))

    def test_channel_released_exactly_at_the_cutoff_is_kept(self):
        self.assertTrue(render_releases._is_active(
            _chan("x", "stable", 1, released_at="2024-07-09T12:00:00Z"), self.cutoff()))

    def test_store_format_at_the_boundary_is_not_mis_sorted(self):
        """'.' < 'Z' — a naive string compare would hide this row."""
        self.assertTrue(render_releases._is_active(
            _chan("x", "stable", 1, released_at="2024-07-09T12:00:00.500000+00:00"),
            self.cutoff()))

    def test_channel_without_a_released_at_is_kept(self):
        """Unknown age is not evidence of staleness."""
        self.assertTrue(render_releases._is_active(
            _chan("x", "stable", 1, released_at=None), self.cutoff()))

    def test_channel_with_an_unparseable_released_at_is_kept(self):
        self.assertTrue(render_releases._is_active(
            _chan("x", "stable", 1, released_at="???"), self.cutoff()))


#: Presentation lives in the config, so the tests supply it explicitly.
FIXTURE_RISKS = ["stable", "candidate", "beta", "edge"]
MAX_AGE = 730


def fixture_sources():
    """The three assets ledger_fixture() describes, in display order."""
    return [
        SnapStoreSource("microceph", repo="canonical/microceph"),
        CharmTagSource("microceph", repo="canonical/charm-microceph"),
        CharmGitInfoSource("ceph-mon", repo="canonical/ceph-charms",
                           collapsed=True),
    ]


DEFAULT_ARCH = "amd64"


def sections_from(ledger, risks=None, max_age_days=MAX_AGE, sources=None,
                  default_arch=DEFAULT_ARCH):
    return render_releases.release_sections(
        ledger["sources"], ledger["generated_at"], sources or fixture_sources(),
        risks or FIXTURE_RISKS, max_age_days, default_arch)


def merge_default_table(section):
    """A section fused with the table it opens on.

    Most assertions here predate the per-architecture selector and care about
    the default table's rows, risks and counts. Fusing keeps them readable;
    the arch-specific tests below index `section["tables"]` directly.
    """
    empty = {"rows": [], "risks": [], "resolved": 0, "total": 0, "hidden": 0,
             "hidden_text": "0 channels", "omitted_risks": [],
             "omitted_channels_text": "0 channels", "omitted_revisions": 0,
             "omitted_revisions_text": "0 revisions", "provenances": []}
    table = section["tables"][0] if section["tables"] else empty
    return {**section, **table}


class ReleasePivotTest(unittest.TestCase):

    def sections(self, **kw):
        """Sections with their default table merged in — see merge_default_table."""
        return [merge_default_table(s) for s in self.raw_sections(**kw)]

    def raw_sections(self, **kw):
        ledger = kw.pop("ledger", None) or ledger_fixture()
        return sections_from(ledger, **kw)

    def test_risks_become_columns_in_stability_order(self):
        snap = self.sections()[0]
        self.assertEqual(snap["risks"], ["stable", "candidate", "edge"])

    def test_risks_absent_from_an_asset_get_no_column(self):
        """The snap publishes no beta, so no beta column."""
        self.assertNotIn("beta", self.sections()[0]["risks"])
        self.assertIn("beta", self.sections()[1]["risks"])

    def test_one_row_per_track_for_a_snap(self):
        snap = self.sections()[0]
        self.assertEqual(snap["key_headings"], ["Track"])
        self.assertEqual([r["key"] for r in snap["rows"]], [["quincy"], ["squid"]])

    def test_charm_rows_keep_base_because_revisions_differ(self):
        """squid/stable is rev 293 on 22.04 and 296 on 24.04 — different commits."""
        charm = self.sections()[1]
        self.assertEqual(charm["key_headings"], ["Track", "Base"])
        self.assertEqual([r["key"] for r in charm["rows"]],
                         [["squid", "22.04"], ["squid", "24.04"]])
        stable = [r["cells"][charm["risks"].index("stable")] for r in charm["rows"]]
        self.assertEqual([c["revision"] for c in stable], [293, 296])

    def test_promotion_pipeline_reads_left_to_right(self):
        snap = self.sections()[0]
        squid = next(r for r in snap["rows"] if r["key"] == ["squid"])
        self.assertEqual([c["revision"] for c in squid["cells"]], [1735, 1735, 1701])

    def test_missing_risk_renders_as_an_empty_cell(self):
        snap = self.sections()[0]
        quincy = next(r for r in snap["rows"] if r["key"] == ["quincy"])
        self.assertEqual(quincy["cells"][0]["revision"], 793)
        self.assertIsNone(quincy["cells"][1], "quincy has no candidate")
        self.assertIsNone(quincy["cells"][2], "quincy has no edge")

    def test_dead_track_is_hidden_and_counted(self):
        snap = self.sections()[0]
        self.assertNotIn(["luminous"], [r["key"] for r in snap["rows"]])
        self.assertEqual(snap["hidden"], 1)
        self.assertEqual(snap["hidden_text"], "1 channel")

    def test_live_row_keeps_its_stale_cells(self):
        """The cutoff is per row, not per cell.

        ceph-mon squid/24.04 has a 2022 stable beneath a current edge. Dropping
        the stable cell would render `·`, which the legend defines as "nothing
        is published on that risk" — a lie.
        """
        mon = self.sections()[2]
        row = next(r for r in mon["rows"] if r["key"] == ["squid", "24.04"])
        stable = row["cells"][mon["risks"].index("stable")]
        edge = row["cells"][mon["risks"].index("edge")]
        self.assertIsNotNone(stable, "a stale stable in a live row must survive")
        self.assertEqual(stable["revision"], 498)
        self.assertEqual(edge["revision"], 572)

    def test_row_with_every_risk_stale_is_hidden(self):
        mon = self.sections()[2]
        self.assertNotIn(["luminous", "16.04"], [r["key"] for r in mon["rows"]])
        self.assertEqual(mon["hidden"], 1)

    def test_a_stale_cell_in_a_live_row_counts_toward_the_ratio(self):
        mon = self.sections()[2]
        self.assertEqual((mon["resolved"], mon["total"]), (2, 2))

    def test_hidden_channels_are_excluded_from_the_ratio(self):
        """luminous rev 3 resolves, but it is not an active revision."""
        snap = self.sections()[0]
        self.assertEqual((snap["resolved"], snap["total"]), (2, 3))

    def test_revisions_counted_once_across_risks(self):
        """rev 1735 fills two cells but is one revision."""
        snap = self.sections()[0]
        cells = [c for r in snap["rows"] for c in r["cells"] if c]
        self.assertEqual(len(cells), 4)
        self.assertEqual(snap["total"], 3)

    def test_snap_precedes_charms_regardless_of_alphabetical_order(self):
        self.assertEqual([(s["title"], s["kind"]) for s in self.sections()],
                         [("microceph", "snap"), ("microceph", "charm"),
                          ("ceph-mon", "charm")])

    def test_resolved_cell_links_the_full_sha_and_shows_a_short_one(self):
        charm = self.sections()[1]
        cell = next(c for r in charm["rows"] for c in r["cells"]
                    if c and c["commit"])
        self.assertEqual(cell["commit"], "b" * 10)
        self.assertTrue(cell["commit_url"].endswith("b" * 40))

    def test_unknown_cell_has_no_link_and_explains_itself(self):
        charm = self.sections()[1]
        cell = next(c for r in charm["rows"] for c in r["cells"]
                    if c and not c["commit"])
        self.assertIsNone(cell["commit_url"])
        self.assertIn("no rev293 tag", cell["title"])

    def test_cell_title_carries_version_provenance_and_branch(self):
        snap_cell = self.sections()[0]["rows"][1]["cells"][0]
        self.assertIn("revision 1735", snap_cell["title"])
        self.assertIn("19.2.3+snapcf306793a4", snap_cell["title"])
        self.assertIn(VERSION_STRING, snap_cell["title"])

        mon_cell = self.sections()[2]["rows"][0]["cells"][0]
        self.assertIn("branch main", mon_cell["title"])
        self.assertIn(GIT_INFO, mon_cell["title"])

    def test_section_lists_the_provenances_actually_present(self):
        self.assertEqual([p["name"] for p in self.sections()[0]["provenances"]],
                         [VERSION_STRING])
        self.assertEqual([p["name"] for p in self.sections()[1]["provenances"]],
                         [GIT_TAG])

    def test_ceph_charms_are_collapsed_and_grouped(self):
        sections = self.sections()
        self.assertEqual([s["collapsed"] for s in sections], [False, False, True])
        groups = render_releases.collapsed_groups(sections)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["repo"], "canonical/ceph-charms")
        self.assertEqual([s["title"] for s in groups[0]["sections"]], ["ceph-mon"])

    def _multiarch_ledger(self):
        """ceph-mon squid/stable 24.04: amd64 and arm64 are different commits."""
        ledger = ledger_fixture()
        src = ledger["sources"]["charm-ceph-mon"]
        src["channels"] = [
            _chan("squid", "stable", 498, base="24.04", arch="amd64"),
            _chan("squid", "stable", 502, base="24.04", arch="arm64"),
            _chan("squid", "stable", 499, base="24.04", arch="s390x"),
            # dead everywhere: no table, no option, but counted
            _chan("luminous", "stable", 73, base="16.04", arch="armhf",
                  released_at=DEAD),
        ]
        src["revisions"] = {
            "498": _res("e67ab37ad305", GIT_INFO, branch="main"),
            "502": _res("a9b45088bcf7", GIT_INFO, branch="main"),
            "499": _res("e67ab37ad305", GIT_INFO, branch="main"),
            "73": _res(note="charm has no git-info.txt"),
        }
        return ledger

    def test_one_table_per_architecture_default_first(self):
        mon = self.raw_sections(ledger=self._multiarch_ledger())[2]
        self.assertEqual(mon["arches"], ["amd64", "arm64", "s390x"],
                         "default first, then alphabetical")
        self.assertEqual([t["arch"] for t in mon["tables"]],
                         ["amd64", "arm64", "s390x"])

    def test_each_architecture_shows_its_own_revision_and_commit(self):
        mon = self.raw_sections(ledger=self._multiarch_ledger())[2]
        by_arch = {t["arch"]: t["rows"][0]["cells"][0] for t in mon["tables"]}
        self.assertEqual(by_arch["amd64"]["revision"], 498)
        self.assertEqual(by_arch["arm64"]["revision"], 502)
        self.assertEqual(by_arch["amd64"]["commit"], "e67ab37ad3")
        self.assertEqual(by_arch["arm64"]["commit"], "a9b45088bc",
                         "arm64 was rebuilt from a newer commit")

    def test_dead_architecture_gets_no_option_but_is_named(self):
        mon = self.raw_sections(ledger=self._multiarch_ledger())[2]
        self.assertNotIn("armhf", mon["arches"])
        self.assertEqual(mon["inactive_arches"], ["armhf"])
        self.assertEqual(mon["inactive_channels_text"], "1 channel")

    def test_default_arch_absent_falls_back_to_the_first_published(self):
        ledger = self._multiarch_ledger()
        mon = self.raw_sections(ledger=ledger, default_arch="riscv64")[2]
        self.assertEqual(mon["arches"], ["amd64", "arm64", "s390x"],
                         "no default match: alphabetical, nothing invented")

    def test_default_arch_nobody_publishes_is_reported(self):
        sections = self.raw_sections(ledger=self._multiarch_ledger(),
                                     default_arch="loongarch64")
        self.assertEqual(
            render_releases.unpublished_default_arch(sections, "loongarch64"),
            "loongarch64")

    def test_default_arch_published_by_any_asset_is_fine(self):
        """charm-microceph publishes amd64 only; that is not a config error."""
        sections = self.raw_sections(ledger=self._multiarch_ledger(),
                                     default_arch="arm64")
        self.assertIsNone(
            render_releases.unpublished_default_arch(sections, "arm64"),
            "one asset publishing it is enough")

    def test_no_sections_means_nothing_to_report(self):
        self.assertIsNone(render_releases.unpublished_default_arch([], "amd64"))

    def test_a_single_architecture_asset_still_has_one_table(self):
        snap = self.raw_sections()[0]
        self.assertEqual(snap["arches"], ["amd64"])
        self.assertEqual(len(snap["tables"]), 1)

    def test_collapsed_groups_merge_a_repo_listed_twice(self):
        """The config may name one repo in two non-adjacent groups."""
        sections = [
            {"collapsed": True, "repo": "o/a", "title": "a1", "kind": "charm"},
            {"collapsed": True, "repo": "o/b", "title": "b1", "kind": "charm"},
            {"collapsed": True, "repo": "o/a", "title": "a2", "kind": "charm"},
        ]
        groups = render_releases.collapsed_groups(sections)
        self.assertEqual([g["repo"] for g in groups], ["o/a", "o/b"],
                         "each repo once, in first-appearance order")
        self.assertEqual([s["title"] for s in groups[0]["sections"]], ["a1", "a2"])
        self.assertEqual([g["count_text"] for g in groups], ["2 charms", "1 charm"])

    def test_collapsed_group_of_mixed_kinds_counts_assets(self):
        """A repo publishing a snap and a charm is neither "2 snaps" nor "2 charms"."""
        sections = [
            {"collapsed": True, "repo": "o/a", "title": "a", "kind": "snap"},
            {"collapsed": True, "repo": "o/a", "title": "a", "kind": "charm"},
        ]
        groups = render_releases.collapsed_groups(sections)
        self.assertEqual(groups[0]["count_text"], "2 assets")

    def test_risk_not_in_the_config_is_named_never_silently_dropped(self):
        """A store inventing a risk gets no column, but the page says so."""
        ledger = ledger_fixture()
        ledger["sources"]["snap-microceph"]["channels"].append(
            _chan("squid", "experimental", 1999))
        ledger["sources"]["snap-microceph"]["revisions"]["1999"] = _res()
        snap = self.sections(ledger=ledger)[0]
        self.assertNotIn("experimental", snap["risks"])
        self.assertEqual(snap["omitted_risks"], ["experimental"])

    def test_row_entirely_on_an_omitted_risk_is_counted_not_vanished(self):
        """Its revision is unreachable, so the page must at least say how many."""
        ledger = ledger_fixture()
        src = ledger["sources"]["snap-microceph"]
        src["channels"] = [_chan("squid", "beta", 7)]
        src["revisions"] = {"7": _res("c" * 40, GIT_TAG)}
        snap = self.sections(ledger=ledger, risks=["stable", "candidate", "edge"])[0]
        self.assertEqual(snap["rows"], [])
        self.assertEqual(snap["omitted_risks"], ["beta"])
        self.assertEqual(snap["omitted_channels_text"], "1 channel")
        self.assertEqual(snap["omitted_revisions"], 1,
                         "revision 7 exists only on beta — say so, do not drop it")

    def test_row_activity_ignores_which_risks_are_shown(self):
        """A fresh beta keeps its row alive even when beta has no column.

        Otherwise the row is hidden under "nothing released since <cutoff>",
        which is false — something shipped today.
        """
        ledger = ledger_fixture()
        src = ledger["sources"]["snap-microceph"]
        src["channels"] = [_chan("squid", "stable", 1, released_at=DEAD),
                           _chan("squid", "beta", 2)]
        src["revisions"] = {"1": _res("a" * 40, GIT_TAG), "2": _res("b" * 40, GIT_TAG)}
        snap = self.sections(ledger=ledger, risks=["stable", "candidate", "edge"])[0]
        self.assertEqual([r["key"] for r in snap["rows"]], [["squid"]])
        self.assertEqual(snap["hidden"], 0, "the row is live; nothing is age-hidden")
        self.assertEqual(snap["rows"][0]["cells"][0]["revision"], 1,
                         "its stale stable cell still renders")

    def test_revision_visible_elsewhere_is_not_counted_as_stranded(self):
        """charm-microceph's beta cells duplicate its stable/candidate ones."""
        charm = self.sections(risks=["stable", "candidate", "edge"])[1]
        self.assertEqual(charm["omitted_risks"], ["beta"])
        self.assertEqual(charm["omitted_revisions"], 0)

    def test_architecture_with_only_dead_channels_is_named_not_dropped(self):
        """No table, no selector option — but the channels are still counted."""
        ledger = ledger_fixture()
        src = ledger["sources"]["snap-microceph"]
        src["channels"] = [_chan("luminous", "stable", 1, released_at=DEAD),
                           _chan("luminous", "beta", 1, released_at=DEAD)]
        src["revisions"] = {"1": _res()}
        snap = self.raw_sections(ledger=ledger,
                                 risks=["stable", "candidate", "edge"])[0]
        self.assertEqual(snap["tables"], [])
        self.assertEqual(snap["arches"], [])
        self.assertEqual(snap["inactive_arches"], ["amd64"])
        self.assertEqual(snap["inactive_channels_text"], "2 channels")

    def test_live_architecture_with_only_omitted_risks_keeps_its_table(self):
        """It IS publishing; saying "nothing released since <cutoff>" would lie."""
        ledger = ledger_fixture()
        src = ledger["sources"]["snap-microceph"]
        src["channels"] = [_chan("squid", "beta", 7)]
        src["revisions"] = {"7": _res("c" * 40, GIT_TAG)}
        snap = self.raw_sections(ledger=ledger,
                                 risks=["stable", "candidate", "edge"])[0]
        self.assertEqual(snap["inactive_arches"], [])
        self.assertEqual(snap["arches"], ["amd64"])
        table = snap["tables"][0]
        self.assertEqual(table["rows"], [])
        self.assertEqual(table["omitted_risks"], ["beta"])
        self.assertEqual(table["omitted_revisions"], 1)

    def test_config_chooses_which_risks_are_columns(self):
        """The whole point of the config: drop `beta`, and be told you did."""
        charm = self.sections(risks=["stable", "candidate", "edge"])[1]
        self.assertEqual(charm["risks"], ["stable"])
        self.assertEqual(charm["omitted_risks"], ["beta"])

    def test_config_chooses_the_column_order(self):
        snap = self.sections(risks=["edge", "candidate", "stable"])[0]
        self.assertEqual(snap["risks"], ["edge", "candidate", "stable"])

    def test_config_chooses_the_active_window(self):
        """Widen the window past the dead track and it comes back."""
        default = self.sections()[0]
        self.assertNotIn(["luminous"], [r["key"] for r in default["rows"]])
        self.assertEqual(default["hidden"], 1)

        widened = self.sections(max_age_days=3000)[0]
        self.assertIn(["luminous"], [r["key"] for r in widened["rows"]])
        self.assertEqual(widened["hidden"], 0)

    def test_fresh_sections_carry_no_staleness_warning(self):
        self.assertEqual([s["stale_since"] for s in self.sections()],
                         [None, None, None])

    def test_stale_section_is_flagged_with_its_own_timestamp(self):
        ledger = ledger_fixture()
        ledger["sources"]["snap-microceph"]["collected_at"] = "2026-01-01T00:00:00Z"
        sections = self.sections(ledger=ledger)
        self.assertEqual(sections[0]["stale_since"], "2026-01-01 00:00 UTC")
        self.assertIsNone(sections[1]["stale_since"])


class ReleasePageTest(unittest.TestCase):
    """render() must produce the page, and must not eat the ledger."""

    @staticmethod
    def fixture_config(data_dir):
        """simulate_site's health config, plus a releases block naming exactly
        the three assets ledger_fixture() describes."""
        from ci_dashboard import simulate as simulate_site
        config = simulate_site.write_history(data_dir)
        config["releases"] = {
            "default_arch": "amd64", "risks": FIXTURE_RISKS,
            "max_age_days": MAX_AGE,
            "groups": [
                {"repo": "canonical/microceph", "provenance": "version-string",
                 "assets": ["microceph"], "collapsed": False},
                {"repo": "canonical/charm-microceph", "provenance": "git-tag",
                 "assets": ["microceph"], "collapsed": False},
                {"repo": "canonical/ceph-charms", "provenance": "git-info",
                 "assets": ["ceph-mon"], "collapsed": True},
            ],
        }
        return config

    def render_site(self, site, with_ledger=True):
        from ci_dashboard import render
        data_dir = os.path.join(site, "data")
        config = self.fixture_config(data_dir)
        if with_ledger:
            write_ledger(ledger_fixture(), site)
        render.render_site(config, data_dir, site)
        return config

    def test_page_is_written_and_ledger_survives(self):
        with tempfile.TemporaryDirectory() as site:
            self.render_site(site)
            self.assertTrue(os.path.exists(
                os.path.join(site, "releases", "index.html")))
            self.assertTrue(os.path.exists(collect_releases.ledger_path(site)),
                            "render() must not rmtree releases-data/")

    def test_page_shows_commits_and_unknowns(self):
        with tempfile.TemporaryDirectory() as site:
            self.render_site(site)
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()
        self.assertIn("canonical/microceph/commit/cf306793a4", page)
        self.assertIn("no rev293 tag", page)
        self.assertIn("p-status-label--information", page)

    def test_landing_page_links_the_releases_card(self):
        with tempfile.TemporaryDirectory() as site:
            self.render_site(site)
            with open(os.path.join(site, "index.html")) as f:
                landing = f.read()
        self.assertIn('href="releases/index.html"', landing)

    def test_renders_without_a_ledger(self):
        """The day-one state: the collector has never run."""
        with tempfile.TemporaryDirectory() as site:
            self.render_site(site, with_ledger=False)
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()
        self.assertIn("No release data collected yet", page)

    def test_selector_and_panels_reach_the_page(self):
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            src = ledger["sources"]["charm-ceph-mon"]
            src["channels"] = [
                _chan("squid", "stable", 498, base="24.04", arch="amd64"),
                _chan("squid", "stable", 502, base="24.04", arch="arm64"),
            ]
            src["revisions"] = {"498": _res("e67ab37ad305", GIT_INFO),
                                "502": _res("a9b45088bcf7", GIT_INFO)}
            write_ledger(ledger, site)
            self.render_site(site, with_ledger=False)
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()

        self.assertIn('id="arch-charm-ceph-mon"', page)
        self.assertIn('<option value="amd64">', page)
        self.assertIn('<option value="arm64">', page)
        # Both architectures' tables are real HTML; only the default is visible.
        self.assertIn('data-arch="amd64"', page)
        self.assertIn('data-arch="arm64"', page)
        self.assertIn("e67ab37ad3", page)
        self.assertIn("a9b45088bc", page)
        self.assertIn('data-arch="arm64" hidden', page)
        # The control is inert until JS reveals it, so no-JS readers see no
        # broken dropdown — just the default architecture's table.
        self.assertIn('data-section="charm-ceph-mon" hidden>', page)

    def test_unpublished_default_arch_reaches_the_page_and_stderr(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as site:
            write_ledger(ledger_fixture(), site)
            data_dir = os.path.join(site, "data")
            config = self.fixture_config(data_dir)
            config["releases"]["default_arch"] = "loongarch64"
            from ci_dashboard import render
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                render.render_site(config, data_dir, site)
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()
        self.assertIn("default_arch: loongarch64", page)
        self.assertIn("no asset publishes", page)
        self.assertIn("loongarch64", stderr.getvalue())
        # and the tables still render, on whatever they do publish
        self.assertIn('data-arch="amd64"', page)

    def test_single_architecture_asset_gets_no_selector(self):
        with tempfile.TemporaryDirectory() as site:
            self.render_site(site)
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()
        self.assertNotIn('id="arch-snap-microceph"', page,
                         "a choice of one is not a choice")

    def test_stale_warning_reaches_the_page(self):
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            ledger["sources"]["snap-microceph"]["collected_at"] = "2026-01-01T00:00:00Z"
            write_ledger(ledger, site)
            self.render_site(site, with_ledger=False)  # ledger already written
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()
        self.assertIn("Not refreshed on the last collection", page)
        self.assertIn("2026-01-01 00:00 UTC", page)

    def test_hostile_note_cannot_escape_the_title_attribute(self):
        """Notes are built from exception text, so they are untrusted input.

        `unknown(f"could not read the charm: {exc}")` puts an arbitrary
        exception message into title="...". If autoescape were ever bypassed
        (a stray |safe), this payload would close the attribute, the cell, the
        table, and inject a script — which TagBalanceChecker would catch.
        """
        try:
            from test_render import TagBalanceChecker        # discover -s tests
        except ImportError:
            from tests.test_render import TagBalanceChecker  # discover from the root
        payload = '"onmouseover=alert(1) x="</td></tr></table><script>bad()</script>'
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            ledger["sources"]["charm-microceph"]["revisions"]["293"]["note"] = payload
            write_ledger(ledger, site)
            self.render_site(site, with_ledger=False)  # ledger already written
            with open(os.path.join(site, "releases", "index.html")) as f:
                page = f.read()

        self.assertNotIn("<script>bad()</script>", page)
        self.assertIn("&lt;script&gt;", page, "the payload is escaped, not dropped")
        checker = TagBalanceChecker()
        checker.feed(page)
        checker.close()
        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])

    def _render_with_broken_ledger(self, site, write):
        """Render a site whose release ledger is broken in some way."""
        from ci_dashboard import render
        data_dir = os.path.join(site, "data")
        config = self.fixture_config(data_dir)
        path = collect_releases.ledger_path(site)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write(path)
        render.render_site(config, data_dir, site)   # must NOT raise
        with open(os.path.join(site, "releases", "index.html")) as f:
            return f.read()

    def _write(self, ledger):
        return lambda path: open(path, "w").write(json.dumps(ledger))

    def test_a_broken_ledger_does_not_sink_the_rest_of_the_site(self):
        """The CI-health metrics are the site's purpose; an auxiliary cache
        must never stop them being republished."""
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            ledger["schema_version"] = 99
            page = self._render_with_broken_ledger(site, self._write(ledger))
            self.assertTrue(os.path.exists(
                os.path.join(site, "ci-health", "index.html")))
        self.assertIn("could not be read", page)
        self.assertIn("schema_version", page)

    def test_corrupt_ledger_json_renders_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as site:
            page = self._render_with_broken_ledger(
                site, lambda p: open(p, "w").write("{not json"))
        self.assertIn("could not be read", page)

    def test_unparseable_generated_at_renders_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            ledger["generated_at"] = "garbage-not-a-date"
            page = self._render_with_broken_ledger(site, self._write(ledger))
        self.assertIn("could not be read", page)
        self.assertIn("generated_at", page)

    def test_asset_with_no_active_channels_explains_itself(self):
        """Not a headless two-column table."""
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            for channel in ledger["sources"]["snap-microceph"]["channels"]:
                channel["released_at"] = DEAD
            page = self._render_with_broken_ledger(site, self._write(ledger))
        self.assertIn("No channel has been released to since", page)

    def test_never_collected_asset_says_so(self):
        with tempfile.TemporaryDirectory() as site:
            ledger = ledger_fixture()
            source = ledger["sources"]["snap-microceph"]
            source["collected_at"] = None
            source["channels"], source["revisions"] = [], {}
            page = self._render_with_broken_ledger(site, self._write(ledger))
        self.assertIn("Never collected", page)


class SourceRegistryTest(unittest.TestCase):

    def test_slugs_are_unique(self):
        from ci_dashboard import config
        sources = build_sources(config.load_config(EXAMPLE_CONFIG)["releases"])
        slugs = [s.slug for s in sources]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(slugs), 11)

    def test_snap_and_charm_named_microceph_do_not_collide(self):
        self.assertNotEqual(
            SnapStoreSource("microceph", repo="a/b").slug,
            CharmTagSource("microceph", repo="a/c").slug)


if __name__ == "__main__":
    unittest.main()
