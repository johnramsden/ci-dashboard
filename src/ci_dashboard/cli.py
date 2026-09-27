#!/usr/bin/env python3
"""
The `ci-dashboard` command: one entry point over the collectors and renderer.

  ci-dashboard validate          --config FILE
  ci-dashboard collect           --config FILE --output-dir SITE [--repo OWNER/NAME] [--now ISO8601]
  ci-dashboard collect-releases  --config FILE --output-dir SITE [--source SLUG] [--now ISO8601]
  ci-dashboard render            --config FILE --output-dir SITE [--data-dir DIR]
  ci-dashboard build             --config FILE --output-dir SITE [--no-collect] [--now ISO8601]
  ci-dashboard simulate          [OUTPUT_DIR]

`build` is the whole pipeline in one command, for running locally. The GitHub
Action runs the same subcommands as separate steps instead, so each gets its
own collapsible log and a collection failure can still be published.

Every subcommand exits 0 on success and 1 on failure. `build` exits 1 when any
collection failed, but only after rendering (and so keeping) whatever did
succeed — the same contract the Action has.
"""

import argparse
import os
import sys

from . import __version__, collect, collect_releases, simulate
from .config import ConfigError, iter_repos, load_config
from .provenance import build_sources
from .render import render_site

PROG = "ci-dashboard"


def _load(path):
    """The validated config, or exit 1 with the reason."""
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: cannot read {path}: {e.strerror}", file=sys.stderr)
        sys.exit(1)


def cmd_validate(argv, prog):
    parser = argparse.ArgumentParser(
        prog=prog, description="Check a dashboard config file and summarize what it tracks.")
    parser.add_argument("--config", required=True, help="Path to the dashboard config file")
    args = parser.parse_args(argv)

    config = _load(args.config)
    repos = list(iter_repos(config))
    print(f"{args.config}: OK — {config['site']['title']!r}, "
          f"{len(repos)} repositor{'y' if len(repos) == 1 else 'ies'} "
          f"in {len(config['groups'])} group(s)")
    for group in config["groups"]:
        print(f"  {group['name']}")
        for repo in group["repos"]:
            extras = []
            if repo["exclude"]:
                extras.append(f"{len(repo['exclude'])} exclude")
            if repo["preflight"]:
                extras.append(f"{len(repo['preflight'])} preflight")
            print(f"    {repo['repo']}: {', '.join(repo['branches'])} "
                  f"({repo['lookback_days']}d window, threshold "
                  f"{repo['flaky_rate_threshold']:g}%"
                  f"{''.join(', ' + e for e in extras)})")
    sources = build_sources(config["releases"])
    if sources:
        print(f"  Releases: {len(sources)} asset(s): "
              f"{', '.join(s.slug for s in sources)}")
    else:
        print("  Releases: not configured (no Releases page)")
    return 0


def cmd_render(argv, prog):
    parser = argparse.ArgumentParser(prog=prog, description="Render the static site.")
    parser.add_argument("--config", required=True, help="Path to the dashboard config file")
    parser.add_argument("--output-dir", required=True, help="Site root to write into")
    parser.add_argument("--data-dir",
                        help="Directory holding the CI snapshots (default: <output-dir>/data)")
    args = parser.parse_args(argv)

    config = _load(args.config)
    data_dir = args.data_dir or os.path.join(args.output_dir, "data")
    try:
        render_site(config, data_dir, args.output_dir)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Site rendered to {args.output_dir}")
    return 0


def _exit_code(fn, argv, prog):
    """Run a main() that reports failure with sys.exit, as an exit code."""
    try:
        return fn(argv, prog) or 0
    except SystemExit as e:
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1


def cmd_build(argv, prog):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Collect, then render: the whole pipeline. A collection "
                    "failure still renders what succeeded, then exits 1.")
    parser.add_argument("--config", required=True, help="Path to the dashboard config file")
    parser.add_argument("--output-dir", required=True, help="Site root: data and pages")
    parser.add_argument("--no-collect", action="store_true",
                        help="Re-render the data already in --output-dir; no network")
    parser.add_argument("--now", help="ISO-8601 override of the clock (tests/reproducibility)")
    args = parser.parse_args(argv)

    config = _load(args.config)  # fail fast, before any network traffic
    common = ["--config", args.config, "--output-dir", args.output_dir]
    if args.now:
        common += ["--now", args.now]

    failed = []
    if not args.no_collect:
        if _exit_code(collect.main, common, f"{PROG} collect"):
            failed.append("CI metrics")
        if config["releases"] and _exit_code(collect_releases.main, common,
                                             f"{PROG} collect-releases"):
            failed.append("release provenance")

    rc = cmd_render(["--config", args.config, "--output-dir", args.output_dir],
                    f"{PROG} render")
    if rc:
        return rc
    if failed:
        print(f"Error: collection failed for {' and '.join(failed)}; the site "
              "was rendered from what did succeed.", file=sys.stderr)
        return 1
    return 0


COMMANDS = {
    "validate": (cmd_validate, "check a config file and summarize it"),
    "collect": (collect.main, "collect CI health snapshots from the GitHub Actions API"),
    "collect-releases": (collect_releases.main, "collect published snap/charm revisions and their commits"),
    "render": (cmd_render, "render the static site from collected data"),
    "build": (cmd_build, "collect, then render (the whole pipeline)"),
    "simulate": (simulate.main, "render a site from fabricated data, offline"),
}


def _usage():
    width = max(len(name) for name in COMMANDS)
    lines = [f"usage: {PROG} <command> [options]", "", "commands:"]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in COMMANDS.items()]
    lines += ["", f"Run '{PROG} <command> --help' for a command's options."]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0 if argv else 2
    if argv[0] == "--version":
        print(f"{PROG} {__version__}")
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"{PROG}: unknown command {name!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    fn, _ = COMMANDS[name]
    return _exit_code(fn, rest, f"{PROG} {name}")


if __name__ == "__main__":
    sys.exit(main())
