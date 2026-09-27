#!/usr/bin/env python3
"""
Static site renderer. Owns only what the two halves of the site share.

  render/health.py    CI flakiness: overview, repo pages, job pages
  render/releases.py  published snap/charm revisions -> the commits behind them

Neither imports the other. This module holds the Jinja environment, the output
tree, the landing page that links them, and the handful of formatting helpers
both need.

Rendering is a pure function of (config, data): every displayed timestamp comes
from a snapshot or the release ledger, never the wall clock, so re-rendering
unchanged data is byte-identical and the publish workflow can skip empty
commits.

HTML lives in templates/ (Jinja2, autoescaped), split the same way:

  templates/base.html         page chrome, shared
  templates/_macros.html      breadcrumbs, notification — shared
  templates/landing.html      the landing page
  templates/health/           overview.html, repo.html, job.html, _macros.html
  templates/releases/         index.html, _macros.html

Usage:
  ci-dashboard render --config FILE --output-dir SITE [--data-dir SITE/data]
"""

import os
import shutil
import sys

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, "templates")
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.pardir, "static")

# Directories the renderer owns and rebuilds wholesale. `data/` (CI snapshots)
# and `releases-data/` (the release ledger) are NOT here: they are inputs.
_GENERATED_DIRS = ("ci-health", "releases", "static")
_GENERATED_FILES = ("index.html",)

# Landing-page cards. "Releases" only appears when the config configures it.
_HEALTH_SECTION = ("CI Health", "ci-health/index.html",
                   "Automated CI flakiness metrics for the tracked repositories.")
_RELEASES_SECTION = ("Releases", "releases/index.html",
                     "Published snap and charm revisions, mapped to the "
                     "commits that built them.")

# The landing page's tagline when the config's `site.description` is unset.
DEFAULT_DESCRIPTION = "Quality dashboards for the tracked repositories."


def fmt_timestamp(iso):
    """2026-07-03T06:17:00Z -> 2026-07-03 06:17 UTC"""
    return iso.replace("T", " ")[:16] + " UTC"


def plural(count, word):
    """Count with its noun pluralized: 1 job type, 3 job types."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


class SiteRenderer:
    """The output tree, the Jinja environment, and the landing page."""

    def __init__(self, config, data_dir, output_dir):
        self.config = config
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.site_title = config["site"]["title"]
        self.site_description = (config["site"].get("description")
                                 or DEFAULT_DESCRIPTION)
        self.env = Environment(
            loader=FileSystemLoader(_TEMPLATES_DIR),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def warn(self, message):
        print(f"Warning: {message}", file=sys.stderr)

    def write_page(self, rel_path, template_name, *, title, root, footer, **context):
        page = self.env.get_template(template_name).render(
            title=title, site_title=self.site_title, root=root, footer=footer,
            **context)
        dest = os.path.join(self.output_dir, rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            f.write(page)

    def render_landing(self, sections, footer):
        cards = [{"title": t, "href": h, "description": d} for t, h, d in sections]
        self.write_page("index.html", "landing.html", title=self.site_title,
                        root="", footer=footer, sections=cards,
                        site_description=self.site_description)

    def _clean(self):
        """Regenerate the HTML tree wholesale; the data directories survive.

        `releases/` (pages) and `releases-data/` (the ledger) are siblings on
        purpose: deleting the former must never reach the latter.
        """
        for name in _GENERATED_DIRS:
            shutil.rmtree(os.path.join(self.output_dir, name), ignore_errors=True)
        for name in _GENERATED_FILES:
            try:
                os.remove(os.path.join(self.output_dir, name))
            except FileNotFoundError:
                pass
        os.makedirs(self.output_dir, exist_ok=True)
        shutil.copytree(_STATIC_DIR, os.path.join(self.output_dir, "static"))
        with open(os.path.join(self.output_dir, ".nojekyll"), "w") as f:
            f.write("")

    def render(self):
        # Imported here, not at module scope: each half imports this module for
        # its shared helpers, so importing them at the top would be circular.
        from . import health, releases

        # Load and validate every input *before* _clean() deletes the site that
        # is already there. HealthPages.__init__ reads the snapshots and raises
        # on a schema it does not understand; doing that after the rmtree would
        # trade a published site for a stack trace.
        health_pages = health.HealthPages(self)
        sections = [_HEALTH_SECTION]

        release_pages = None
        if self.config.get("releases"):
            release_pages = releases.ReleasePages(self)
            sections.append(_RELEASES_SECTION)

        self._clean()

        self.render_landing(sections, health_pages.footer())
        health_pages.render()
        if release_pages is not None:
            release_pages.render()


def render_site(config, data_dir, output_dir):
    SiteRenderer(config, data_dir, output_dir).render()
