#!/usr/bin/env python3
"""
Thin HTTP helpers for the release sources. Stdlib only, no shared session.

The three stores need three different sets of headers, and only GitHub needs a
token. Each helper below encodes one store's requirements so the sources never
have to.
"""

import json
import os
import urllib.error
import urllib.request

USER_AGENT = "ci-dashboard"

# The Snap Store rejects requests without a device series and without a
# non-empty User-Agent; both omissions return an opaque 400.
SNAP_HEADERS = {"Snap-Device-Series": "16"}

SNAP_INFO_URL = "https://api.snapcraft.io/v2/snaps/info/{name}?fields={fields}"
CHARM_INFO_URL = "https://api.charmhub.io/v2/charms/info/{name}?fields={fields}"


def get_json(url, headers=None):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def snap_info(name, fields):
    return get_json(SNAP_INFO_URL.format(name=name, fields=",".join(fields)),
                    SNAP_HEADERS)


def charm_info(name, fields):
    return get_json(CHARM_INFO_URL.format(name=name, fields=",".join(fields)))


def gh_json(path):
    """One GitHub API page. Authenticates when GH_TOKEN (or GITHUB_TOKEN) is set.

    Anonymous requests work against public repos but share a 60/hour budget,
    which a full ceph-charms sweep can exhaust; the workflow always sets a
    token.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return get_json(f"https://api.github.com{path}", headers)


#: Guards against endpoints that ignore `page` and return the same body
#: forever. /git/refs/tags is one; paging it never terminates.
MAX_PAGES = 50


def gh_paginate(path, per_page=100):
    """Every item from a GitHub list endpoint. `path` must already carry a `?`."""
    items = []
    for page in range(1, MAX_PAGES + 1):
        batch = gh_json(f"{path}&per_page={per_page}&page={page}")
        if not batch:
            return items
        if len(batch) < per_page:
            return items + batch      # short page: the last one, by definition
        items.extend(batch)
    raise RuntimeError(
        f"{path} still returning full pages after {MAX_PAGES}; it likely "
        "ignores `page` and cannot be paginated")
