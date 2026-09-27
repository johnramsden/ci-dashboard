#!/usr/bin/env python3
"""
Configuration loading and slug helpers for the CI health dashboard.

The config file maps groups of repositories to their measurement settings; see
README.md and examples/ for the schema. load_config() validates the file and
returns it with the defaults merged into every repo entry, so consumers never
have to look at two places for a setting.
"""

import fnmatch
import hashlib
import re

import yaml

from .provenance import PROVENANCE_TYPES, RISKS, DEFAULT_ARCH

DEFAULT_TITLE = "CI Dashboard"

DEFAULTS = {
    "lookback_days": 30,
    "top_jobs": 5,
    "flaky_rate_threshold": 3.0,
}

_TOP_LEVEL_KEYS = {"defaults", "site", "groups", "releases"}
_SITE_KEYS = {"title", "description"}
_GROUP_KEYS = {"name", "repos"}
_REPO_KEYS = {
    "repo", "branches", "exclude", "preflight",
    "lookback_days", "top_jobs", "flaky_rate_threshold",
}
_EXCLUDE_KEYS = {"workflow", "job"}  # also the keys of a preflight entry

_RELEASES_KEYS = {"default_arch", "risks", "max_age_days", "groups"}
_RELEASE_GROUP_KEYS = {"repo", "provenance", "assets", "collapsed", "version_pattern"}

RELEASE_DEFAULTS = {
    "default_arch": DEFAULT_ARCH,
    "risks": list(RISKS),
    # Two years. Tightening below ~688 days starts hiding rows whose revisions
    # do resolve to a commit (measured 2026-07 against the live stores), so this
    # keeps about six weeks of headroom as those releases age.
    "max_age_days": 730,
}

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")  # \Z: reject trailing newline


class ConfigError(Exception):
    """Raised when the configuration file is invalid."""


def repo_slug(repo):
    """Filesystem/URL slug for an owner/name repository string."""
    return repo.replace("/", "--")


def job_slug(workflow, job):
    """Deterministic, collision-free slug for a (workflow, job) pair.

    Job names contain spaces, slashes, dots, and parentheses. The sanitized
    prefix keeps URLs readable; the hash suffix guarantees uniqueness (the
    NUL separator makes ("A B", "C") and ("A", "B C") hash differently).
    """
    base = re.sub(r"[^a-z0-9]+", "-", f"{workflow} {job}".lower()).strip("-")[:60]
    digest = hashlib.sha256(f"{workflow}\x00{job}".encode()).hexdigest()[:8]
    if not base:
        return digest
    return f"{base}-{digest}"


def matching_exclude(workflow, job, excludes):
    """The first exclude entry that fully matches this workflow/job pair, else None.

    An entry matches when every key it carries (workflow and/or job) matches
    its fnmatch glob, case-sensitively. Applied by render/health.py, not collect.py:
    snapshots keep raw per-job counts, so editing excludes recomputes the
    whole history rather than only affecting newly collected days.

    The entry itself is returned, not just a yes/no, so the repo page can
    show which rule dropped a given job.
    """
    for entry in excludes:
        workflow_pat = entry.get("workflow")
        job_pat = entry.get("job")
        if workflow_pat is not None and not fnmatch.fnmatchcase(workflow, workflow_pat):
            continue
        if job_pat is not None and not fnmatch.fnmatchcase(job, job_pat):
            continue
        return entry
    return None


def is_excluded(workflow, job, excludes):
    """True when any exclude entry matches this workflow/job pair — see matching_exclude()."""
    return matching_exclude(workflow, job, excludes) is not None


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _check_keys(mapping, allowed, context):
    unknown = set(mapping) - allowed
    # str() the keys: YAML can produce non-string keys (`on:` parses as a
    # boolean) and sorted() over mixed types would raise TypeError.
    _require(not unknown, f"{context}: unknown key(s) {sorted(str(k) for k in unknown)}")


def _check_positive_int(value, context):
    _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
             f"{context} must be a positive integer, got {value!r}")


def _check_positive_number(value, context):
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0,
             f"{context} must be a positive number, got {value!r}")


def _check_nonempty_list(value, context):
    _require(isinstance(value, list) and value, f"{context} must be a non-empty list")


def _validate_excludes(excludes, context, key="exclude"):
    """Validate a list of workflow/job glob entries (`exclude` or `preflight`)."""
    _require(isinstance(excludes, list), f"{context}: {key} must be a list")
    for i, entry in enumerate(excludes):
        entry_ctx = f"{context}: {key}[{i}]"
        _require(isinstance(entry, dict), f"{entry_ctx} must be a mapping")
        _check_keys(entry, _EXCLUDE_KEYS, entry_ctx)
        _require(entry, f"{entry_ctx} must have a workflow and/or job key")
        for key, value in entry.items():
            _require(isinstance(value, str) and value.strip(),
                     f"{entry_ctx}: {key} must be a non-empty string")


def _validate_repo(repo_entry, defaults, context):
    _require(isinstance(repo_entry, dict), f"{context} must be a mapping")
    _check_keys(repo_entry, _REPO_KEYS, context)

    repo = repo_entry.get("repo")
    _require(isinstance(repo, str) and _REPO_RE.match(repo or ""),
             f"{context}: repo must be an owner/name string, got {repo!r}")

    branches = repo_entry.get("branches")
    _check_nonempty_list(branches, f"{context} ({repo}): branches")
    for branch in branches:
        _require(isinstance(branch, str) and branch.strip(),
                 f"{context} ({repo}): branches entries must be non-empty strings")
    stripped = [b.strip() for b in branches]
    _require(len(set(stripped)) == len(stripped),
             f"{context} ({repo}): duplicate branch entries {stripped} would "
             "double-count every run")

    _validate_excludes(repo_entry.get("exclude", []), f"{context} ({repo})")
    _validate_excludes(repo_entry.get("preflight", []), f"{context} ({repo})", "preflight")
    if "lookback_days" in repo_entry:
        _check_positive_int(repo_entry["lookback_days"], f"{context} ({repo}): lookback_days")
    if "top_jobs" in repo_entry:
        _check_positive_int(repo_entry["top_jobs"], f"{context} ({repo}): top_jobs")
    if "flaky_rate_threshold" in repo_entry:
        _check_positive_number(repo_entry["flaky_rate_threshold"],
                               f"{context} ({repo}): flaky_rate_threshold")

    # All validation passed - now merge in the defaults.
    merged = dict(defaults)
    merged.update(repo_entry)
    merged["branches"] = stripped
    merged.setdefault("exclude", [])
    merged.setdefault("preflight", [])
    return merged


def _validate_version_pattern(pattern, provenance, context):
    """`version_pattern`: a regex whose one group captures the commit.

    Only `version-string` provenance reads a version, so on any other mechanism
    the key would be silently ignored — reject it instead.
    """
    _require(provenance == "version-string",
             f"{context}: version_pattern only applies to provenance: "
             f"version-string, not {provenance}")
    _require(isinstance(pattern, str) and pattern,
             f"{context}: version_pattern must be a non-empty string")
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"{context}: version_pattern is not a valid regular "
                          f"expression: {e}") from e
    _require(compiled.groups == 1,
             f"{context}: version_pattern must have exactly one capturing group "
             f"(the commit), found {compiled.groups}")


def _validate_releases(raw):
    """Normalize config.yaml's `releases:` block, or None when it is absent.

    Absent means the site has no Releases page at all — not that it has one
    with nothing on it — so the landing page drops the card too.
    """
    if raw is None:
        return None
    _require(isinstance(raw, dict), "releases must be a mapping")
    _check_keys(raw, _RELEASES_KEYS, "releases")

    merged = dict(RELEASE_DEFAULTS)
    merged.update(raw)

    _require(isinstance(merged["default_arch"], str) and merged["default_arch"].strip(),
             "releases.default_arch must be a non-empty string, got "
             f"{merged['default_arch']!r}")
    _check_positive_int(merged["max_age_days"], "releases.max_age_days")

    risks = merged["risks"]
    _check_nonempty_list(risks, "releases.risks")
    unknown_risks = [r for r in risks if r not in RISKS]
    _require(not unknown_risks,
             f"releases.risks: unknown risk(s) {unknown_risks}; "
             f"valid risks are {list(RISKS)}")
    _require(len(set(risks)) == len(risks),
             f"releases.risks: duplicate entries in {risks}")

    groups_raw = merged.get("groups")
    _check_nonempty_list(groups_raw, "releases.groups")
    groups, seen = [], set()
    for gi, group in enumerate(groups_raw):
        context = f"releases.groups[{gi}]"
        _require(isinstance(group, dict), f"{context} must be a mapping")
        _check_keys(group, _RELEASE_GROUP_KEYS, context)

        repo = group.get("repo")
        _require(isinstance(repo, str) and _REPO_RE.match(repo or ""),
                 f"{context}: repo must be an owner/name string, got {repo!r}")

        # isinstance first: `provenance: [git-tag]` is an easy YAML slip, and a
        # list is unhashable, so the membership test would raise TypeError
        # rather than the ConfigError the CLIs know how to report.
        provenance = group.get("provenance")
        _require(isinstance(provenance, str) and provenance in PROVENANCE_TYPES,
                 f"{context}: provenance must be one of "
                 f"{sorted(PROVENANCE_TYPES)}, got {provenance!r}")

        assets = group.get("assets")
        _check_nonempty_list(assets, f"{context}: assets")
        stripped = []
        for asset in assets:
            _require(isinstance(asset, str) and asset.strip(),
                     f"{context}: assets entries must be non-empty strings")
            asset = asset.strip()
            # Dedupe on the stripped name, which is what build_sources() and
            # the ledger key actually use: "microceph" and "microceph " must
            # collide, or the second source silently overwrites the first.
            # The snap and the charm are both called "microceph"; the kind
            # disambiguates them.
            slug = f"{PROVENANCE_TYPES[provenance].kind}-{asset}"
            _require(slug not in seen,
                     f"{context}: {asset} duplicates another asset (same "
                     f"ledger key {slug!r})")
            seen.add(slug)
            stripped.append(asset)

        collapsed = group.get("collapsed", False)
        _require(isinstance(collapsed, bool),
                 f"{context}: collapsed must be true or false, got {collapsed!r}")

        version_pattern = group.get("version_pattern")
        if version_pattern is not None:
            _validate_version_pattern(version_pattern, provenance, context)

        groups.append({"repo": repo, "provenance": provenance,
                       "assets": stripped, "collapsed": collapsed,
                       "version_pattern": version_pattern})

    return {"default_arch": merged["default_arch"].strip(), "risks": list(risks),
            "max_age_days": merged["max_age_days"], "groups": groups}


def load_config(path):
    """Load, validate, and normalize a dashboard config file.

    Returns {"site": {...}, "groups": [...], "releases": {...} or None}.
    Every repo dict carries its effective lookback_days, top_jobs,
    flaky_rate_threshold, branches, and exclude list.
    """
    with open(path) as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"{path} is not valid YAML: {e}") from e

    _require(isinstance(raw, dict), "configuration must be a YAML mapping")
    _check_keys(raw, _TOP_LEVEL_KEYS, "configuration")

    defaults = dict(DEFAULTS)
    user_defaults = raw.get("defaults", {})
    _require(isinstance(user_defaults, dict), "defaults must be a mapping")
    _check_keys(user_defaults, set(DEFAULTS), "defaults")
    defaults.update(user_defaults)
    _check_positive_int(defaults["lookback_days"], "defaults.lookback_days")
    _check_positive_int(defaults["top_jobs"], "defaults.top_jobs")
    _check_positive_number(defaults["flaky_rate_threshold"], "defaults.flaky_rate_threshold")

    site = raw.get("site", {})
    _require(isinstance(site, dict), "site must be a mapping")
    _check_keys(site, _SITE_KEYS, "site")
    title = site.get("title", DEFAULT_TITLE)
    _require(isinstance(title, str) and title.strip(),
             "site.title must be a non-empty string")
    description = site.get("description")
    _require(description is None or (isinstance(description, str) and description.strip()),
             "site.description must be a non-empty string")
    site = {"title": title.strip(),
            "description": description.strip() if description else None}

    groups_raw = raw.get("groups")
    _check_nonempty_list(groups_raw, "groups")

    groups = []
    seen_repos = set()
    for gi, group in enumerate(groups_raw):
        context = f"groups[{gi}]"
        _require(isinstance(group, dict), f"{context} must be a mapping")
        _check_keys(group, _GROUP_KEYS, context)
        name = group.get("name")
        _require(isinstance(name, str) and name.strip(),
                 f"{context}: name must be a non-empty string")
        repos_raw = group.get("repos")
        _check_nonempty_list(repos_raw, f"{context} ({name}): repos")
        repos = []
        for ri, repo_entry in enumerate(repos_raw):
            merged = _validate_repo(repo_entry, defaults, f"{context}.repos[{ri}]")
            # Dedupe on the slug, not the raw name: distinct repos whose
            # slugs collide would share one data directory.
            slug = repo_slug(merged["repo"])
            _require(slug not in seen_repos,
                     f"{context}: {merged['repo']} duplicates another repo "
                     f"(same data directory {slug!r})")
            seen_repos.add(slug)
            repos.append(merged)
        groups.append({"name": name.strip(), "repos": repos})

    return {"site": site, "groups": groups,
            "releases": _validate_releases(raw.get("releases"))}


def iter_repos(config):
    """Yield every merged repo dict across all groups, in config order."""
    for group in config["groups"]:
        yield from group["repos"]
