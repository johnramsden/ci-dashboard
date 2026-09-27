# ci-dashboard

A static dashboard of post-merge CI health for a set of GitHub repositories, published to GitHub Pages and configured by one YAML file. Optionally, it also maps every published snap and charm revision to the commit that built it.

It ships two ways from this one repository:

- a **GitHub Action** (`uses: johnramsden/ci-dashboard@v1`) that collects, renders, and publishes the site on a schedule, so a team adds a config file and a short workflow and copies no code;
- a **Python CLI** (`ci-dashboard`) for running the same pipeline locally, or on any other CI system.

## Quick start

1. Add `.github/ci-dashboard.yml` to the repository that will host the dashboard:

   ```yaml
   site:
     title: "My team CI"
   groups:
     - name: Services
       repos:
         - repo: my-org/service-a
           branches: [main]
         - repo: my-org/service-b
           branches: [main, stable/1.x]
   ```

2. Add `.github/workflows/ci-dashboard.yml`:

   ```yaml
   name: CI dashboard
   on:
     schedule:
       - cron: "17 6 * * *"
     workflow_dispatch:
   jobs:
     publish:
       runs-on: ubuntu-latest
       permissions:
         contents: write   # push gh-pages
         actions: read     # only needed to monitor this repository itself
         checks: read      # likewise
       concurrency:
         group: ci-dashboard-publish
         cancel-in-progress: false
       steps:
         - uses: actions/checkout@v4
         - uses: johnramsden/ci-dashboard@v1
   ```

   [`examples/workflow.yml`](examples/workflow.yml) is the fuller version: it also re-renders when the config changes and checks config changes in pull requests.

3. Run the workflow once (Actions → CI dashboard → Run workflow). It creates the `gh-pages` branch. Then enable Pages: Settings → Pages → Deploy from a branch → `gh-pages`, `/ (root)`.

The monitored repositories can be anywhere; the hosting repository only holds the config, the workflow, and the `gh-pages` branch. One team can host its own dashboard, or one repository can watch a whole organization.

## What it measures

### CI health: the flaky rate

Only `push`, `schedule` and `workflow_dispatch` runs on the monitored branches count. Code merged there already passed its pre-merge checks, so a failure there is a flake or a regression:

```
flaky rate = (failed jobs on monitored branches) / (all jobs on monitored branches)
```

`pull_request` runs are excluded: pre-merge failures include genuine code bugs, and excluding them stops a same-named branch in a fork from leaking into the numbers. Every attempt of a re-run job counts, so a job that failed and was re-run to green still shows its failure.

The site has an overview card per repository, a page per repository (per-branch rate against its threshold, a trend chart, a job table, the top failing jobs), and a page per job (history chart, and links to each recent failing run).

Collection stores *raw* per-job counts, one snapshot per repository per day. Everything else is computed when the site is rendered, so editing `exclude`, `preflight` or a threshold recomputes the whole history, charts included, rather than only days collected afterwards.

A branch with no qualifying runs in the window shows "no data". A low-traffic branch needs a scheduled workflow running on it to produce any signal.

### Infrastructure failures

Failures caused by infrastructure (a store outage, a runner problem) rather than the code under test are kept out of the flaky rate and reported as a separate infrastructure failure rate. There are two ways to mark them:

- **Preflight gates**: jobs listed under a repository's `preflight:` that exist to check the infrastructure before the real tests. All their failures are infrastructure failures.
- **Annotations**: any job that fails with an error annotation titled `Infra` is an infrastructure failure. Emit it from the job itself, with an optional `kind=` token first:

  ```bash
  echo "::error title=Infra::kind=snap-store snap download failed after 3 attempts"
  ```

  The kind and message appear on the job's page next to that failure. This costs one extra API call per failed job and needs no dashboard configuration, only a token that can read check-run annotations (see [Permissions](#permissions-and-private-repositories)).

### Releases: which commit built each revision

With a `releases:` block, a second page shows every actively published snap or charm revision per track, risk, base and architecture, and the commit it was built from. A revision's commit is established by one of three mechanisms, set per group of assets:

| `provenance` | How the commit is found | Strength |
|---|---|---|
| `version-string` | parsed from the snap's version string; by default a `+snap<sha>` suffix, or your own `version_pattern` | inferred: a build convention, not metadata |
| `git-tag` | the revision's `rev<N>` (or `<track>-rev<N>`) tag in the repository, as created by `canonical/charming-actions/upload-charm` | authoritative |
| `git-info` | a `git-info.txt` file (`commit:` and `branch:` lines) inside the published `.charm`, read with HTTP range requests (about 0.5 MB per revision instead of the whole charm) | authoritative |

Anything the mechanism cannot resolve is shown as unknown, with the reason on hover, never guessed. Resolutions are cached in the ledger forever (a revision's commit never changes), so only the first collection is slow; `version-string` is free to compute and is re-resolved on every run, so a corrected `version_pattern` applies to every revision.

## Configuration reference

The config file is YAML. Unknown keys are errors, so a typo fails loudly instead of being ignored. `ci-dashboard validate --config FILE` checks a file and summarizes it.

### `site`

| Key | Default | Meaning |
|---|---|---|
| `title` | `CI Dashboard` | Site name in the header and page titles. |
| `description` | `Quality dashboards for the tracked repositories.` | Tagline on the landing page. |

### `defaults`

Apply to every repository unless it overrides them.

| Key | Default | Meaning |
|---|---|---|
| `lookback_days` | `30` | Width of the rolling window each daily snapshot covers. |
| `top_jobs` | `5` | How many jobs the "top failing jobs" list shows. |
| `flaky_rate_threshold` | `3.0` | Percent. A branch at or above it is flagged "Above threshold". |

### `groups`

Required, non-empty. Each group is a heading on the overview, with a `name` and a non-empty `repos` list. Each repository entry:

| Key | Required | Meaning |
|---|---|---|
| `repo` | yes | `owner/name`. |
| `branches` | yes | Branches whose `push`/`schedule`/`workflow_dispatch` runs count. Each is queried separately and jobs are attributed to the branch they ran on. |
| `exclude` | no | Jobs to leave out entirely (bots, release jobs, scanners). A list of entries with a `workflow` and/or `job` glob (`fnmatch`, case-sensitive); a job is excluded when every key in an entry matches. The repository page lists every job each pattern dropped, and flags patterns that matched nothing. |
| `preflight` | no | Infrastructure gate jobs, in the same format as `exclude`. Their failures count toward the infrastructure failure rate, not the flaky rate. |
| `lookback_days`, `top_jobs`, `flaky_rate_threshold` | no | Per-repository overrides of `defaults`. |

Jobs are keyed by workflow name and job name, so renaming either starts a new series; an `exclude` glob can cover both names during a transition.

### `releases`

Optional. Without it there is no Releases page.

| Key | Default | Meaning |
|---|---|---|
| `groups` | required | One entry per repository and provenance mechanism, see below. |
| `risks` | `[stable, candidate, beta, edge]` | Risk columns, in display order. A risk an asset publishes to but that is not listed is named on the page, never silently dropped. |
| `default_arch` | `amd64` | The architecture each table opens on; a selector switches between all published ones. |
| `max_age_days` | `730` | Hide rows whose every risk is older than this; the stores never retire a channel, so dead tracks linger forever otherwise. |

Each entry of `releases.groups`:

| Key | Required | Meaning |
|---|---|---|
| `repo` | yes | `owner/name` the assets are built from; commit links point there. |
| `provenance` | yes | `version-string` (snaps), `git-tag` or `git-info` (charms); see the table above. |
| `assets` | yes | Store names of the snaps or charms. |
| `collapsed` | no | `true` renders each asset inside a closed `<details>`, grouped under its repository. Useful for a monorepo of many charms. |
| `version_pattern` | no | `version-string` only. A regular expression searched in the version, with exactly one capturing group for the commit, e.g. `'\+git\.([0-9a-f]{7,40})\Z'`. Anchor it; whatever it captures must look like a commit hash or the revision is reported unknown. A pattern that takes over 2 seconds on one version (catastrophic backtracking) is abandoned for that run and its revisions reported unknown. Default: `\+snap([0-9a-f]{7,10})\Z`. |

[`examples/`](examples/) has a [minimal config](examples/minimal.yml) and ceph-qa's [full config](examples/ceph-qa.yml), releases included.

## Action reference

The action runs: install (from this repository's `uv.lock`), validate the config, check the pages branch out into a worktree, collect CI metrics, collect release provenance, render, commit and push, and finally fail if any collection failed. A repository or asset that fails to collect does not stop the others: what succeeded is rendered and published first, and the previous day's data for the failed one stays in place.

It requires `actions/checkout` to have run first (for the config, and because the pages branch is a branch of the checked-out repository) and a Linux or macOS runner.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `config` | `.github/ci-dashboard.yml` | Path to the config, relative to the workspace. |
| `token` | `${{ github.token }}` | Token for the GitHub API reads (see [Permissions](#permissions-and-private-repositories)). Not used for pushing. |
| `collect` | `true` | `false` skips all API calls and re-renders the data already on the pages branch, which is how to apply or check a config change. |
| `publish` | `true` | `false` builds the site into `site-dir` without pushing. |
| `pages-branch` | `gh-pages` | Branch holding the site and its data history; created on the first publish. |
| `site-dir` | a directory under `$RUNNER_TEMP` | Where to build the site. Must be empty or absent. |
| `fail-on-collect-error` | `true` | Fail the step when any collection failed, after publishing what succeeded. |
| `commit-message` | `chore(dashboard): update site` | Pages-branch commit message; the UTC date is appended. |

### Outputs

| Output | Meaning |
|---|---|
| `site-dir` | Absolute path of the built site (for `actions/upload-artifact`, or a Pages deployment of your own). |
| `changed` | `true` when a commit was pushed, `false` when the rendered site was identical, empty when not publishing. Rendering is a pure function of the data, so an unchanged day pushes nothing. |
| `collect-failed` | `true` when any collection failed. |

### Permissions and private repositories

Publishing needs `permissions: contents: write` on the job; the push uses the credentials `actions/checkout` stored (keep its default `persist-credentials: true`).

Reading the monitored repositories uses `token`, which needs, on each of them:

- `actions: read`, for workflow runs and jobs;
- `checks: read`, for the check-run annotations that mark [infrastructure failures](#infrastructure-failures). Without it the dashboard still collects, but prints a warning and counts those failures as ordinary ones;
- `contents: read`, only for `git-tag` release provenance.

The default job token can read public repositories. For the repository the workflow itself runs in, it only has the scopes the job's `permissions:` grants, so a dashboard that monitors its own hosting repository must add `actions: read` and `checks: read` there (the examples do). To monitor private or internal repositories, pass a token with those permissions on each of them; a GitHub App is the simplest way to get all three:

```yaml
      - uses: actions/create-github-app-token@v1
        id: app-token
        with:
          app-id: ${{ vars.DASHBOARD_APP_ID }}
          private-key: ${{ secrets.DASHBOARD_APP_KEY }}
          owner: my-org
      - uses: johnramsden/ci-dashboard@v1
        with:
          token: ${{ steps.app-token.outputs.token }}
```

Collection costs one API call per page of runs, one per counted run (its jobs), and one per failed job (its annotations): a few hundred to a few thousand calls a day for a handful of busy repositories, against the 1,000 per hour of a job token. A real rate limit is waited out and retried once; a repository whose window holds more than 1,000 runs gets a warning.

Pages on a private repository needs a GitHub plan that supports it; the site is otherwise public, including the collected data under `data/`.

## Running locally

The CLI is the same code the action runs. Run it without installing anything permanent:

```bash
uvx --from git+https://github.com/johnramsden/ci-dashboard@v1 ci-dashboard --help
```

or install it with `pip install git+https://github.com/johnramsden/ci-dashboard@v1`.

| Command | Does |
|---|---|
| `ci-dashboard validate --config FILE` | Check a config and summarize what it tracks. |
| `ci-dashboard build --config FILE --output-dir SITE` | Collect, then render: the whole pipeline. `--no-collect` re-renders only. |
| `ci-dashboard collect --config FILE --output-dir SITE` | Write today's CI snapshots to `SITE/data/`. `--repo owner/name` collects one repository; `--now ISO8601` pins the clock. |
| `ci-dashboard collect-releases --config FILE --output-dir SITE` | Update the release ledger at `SITE/releases-data/releases.json`. `--source SLUG` (e.g. `snap-microceph`) collects one asset and keeps the rest. |
| `ci-dashboard render --config FILE --output-dir SITE` | Render the site from the data in `SITE`. |
| `ci-dashboard simulate [DIR]` | Render a site from fabricated data, offline; for trying out styling and templates. |

Collection reads `GH_TOKEN` (or `GITHUB_TOKEN`):

```bash
export GH_TOKEN=$(gh auth token)
ci-dashboard build --config .github/ci-dashboard.yml --output-dir /tmp/site
xdg-open /tmp/site/index.html
```

To render the published history locally, render into a checkout of the pages branch: `git worktree add /tmp/site gh-pages`, then `ci-dashboard render --config .github/ci-dashboard.yml --output-dir /tmp/site`.

## Where the data lives

Everything is on the pages branch, next to the pages rendered from it:

```
index.html                              landing page
ci-health/index.html                    overview
ci-health/<owner>--<repo>/index.html    repository page
ci-health/<owner>--<repo>/jobs/<slug>/  job pages
releases/index.html                     releases page
static/                                 CSS, JS, vendored uPlot
data/<owner>--<repo>/<YYYY-MM-DD>.json  daily raw snapshots (schema_version 1)
releases-data/releases.json             release ledger (schema_version 4)
```

Rendering rebuilds `index.html`, `ci-health/`, `releases/` and `static/` from scratch; `data/` and `releases-data/` are inputs and are never deleted. A snapshot is 10 to 50 KB per repository per day and nothing is pruned; charts are downsampled to 150 points however long the history grows.

A repository removed from the config stops being rendered, but its snapshots stay on the branch; delete `data/<owner>--<repo>/` to drop them. The ledger is a cache that can always be rebuilt: delete `releases-data/` to start it over.

## Versioning

Releases are tagged `vX.Y.Z`, with a moving `vX` tag on the latest release of each major version: reference `@v1` to receive fixes, or a full commit SHA to pin exactly. A change to the snapshot or ledger schema, or an incompatible change to the config or the action's inputs, is a new major version.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests       # includes action.yml, run against a local bare repo
uv run ci-dashboard simulate /tmp/sim && xdg-open /tmp/sim/index.html
```

```
action.yml                     the composite action
scripts/pages.sh               pages-branch worktree and push, used by action.yml
src/ci_dashboard/
  cli.py                       the ci-dashboard command
  config.py                    config loading and validation
  collect.py                   CI snapshots from the GitHub Actions API
  collect_releases.py          the release ledger
  provenance/                  one ReleaseSource per mechanism; stores and zip reading
  render/                      health.py and releases.py never import each other
  templates/, static/          Jinja templates (autoescaped) and the page assets
  simulate.py                  fabricated data for offline rendering and tests
tests/
examples/
```

`tests/test_action.py` executes `action.yml` step by step with a small runner that understands exactly the expressions the action uses, so an expression it does not know fails the tests rather than shipping untested. The `CI` workflow additionally runs the real action against this repository's own history.

The page styling is Canonical's [Vanilla Framework](https://vanillaframework.io/), loaded from `assets.ubuntu.com`; charts are drawn client-side by [uPlot](https://github.com/leeoniya/uPlot) (MIT, vendored in `static/vendor/uplot/`).
