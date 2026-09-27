#!/usr/bin/env bash
# Pages-branch plumbing for action.yml.
#
#   pages.sh prepare   check the pages branch out into the site directory: a
#                      worktree of the workspace's repository, so the collectors
#                      append to the data history already published there.
#   pages.sh publish   commit the rebuilt site and push it back.
#
# Run from inside the checkout (the workspace). Every network operation runs
# against the main repository, never the worktree, so it authenticates with
# whatever actions/checkout persisted there — including credentials it scopes
# to the main repository's git directory. No token passes through this script.
#
# Environment:
#   PAGES_BRANCH    branch holding the site and its data (default gh-pages)
#   SITE_DIR        prepare: where to put the worktree (default
#                   $RUNNER_TEMP/ci-dashboard-site); publish: the site to push
#   COMMIT_MESSAGE  publish: commit message; the UTC date is appended
#   GITHUB_OUTPUT   set by Actions; receives `dir=` (prepare), `changed=` (publish)
set -euo pipefail

branch="${PAGES_BRANCH:-gh-pages}"

die() {
    echo "::error::$*" >&2
    exit 1
}

output() {
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "$1=$2" >>"$GITHUB_OUTPUT"
    fi
}

summary() {
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        echo "$*" >>"$GITHUB_STEP_SUMMARY"
    fi
}

prepare() {
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
        die "the workspace is not a git checkout; run actions/checkout before this action"
    git remote get-url origin >/dev/null 2>&1 ||
        die "the checkout has no 'origin' remote to read '$branch' from"
    git check-ref-format "refs/heads/$branch" ||
        die "'$branch' is not a valid branch name"

    local site="${SITE_DIR:-}"
    if [ -z "$site" ]; then
        site="${RUNNER_TEMP:?set SITE_DIR or RUNNER_TEMP}/ci-dashboard-site"
    fi
    mkdir -p "$site"
    site="$(cd "$site" && pwd)"
    if [ -n "$(ls -A "$site")" ]; then
        die "site directory $site is not empty"
    fi

    local rc=0
    git ls-remote --exit-code origin "refs/heads/$branch" >/dev/null || rc=$?
    if [ "$rc" -eq 0 ]; then
        git fetch --quiet --depth=1 origin "refs/heads/$branch"
        git worktree add --quiet -B "$branch" "$site" FETCH_HEAD
    elif [ "$rc" -eq 2 ]; then
        # --exit-code exits 2 for "no matching refs": the first-ever run.
        # Anything else (a network failure) must NOT take this path, or the
        # publish would push a fresh history over the existing one... and be
        # rejected as a non-fast-forward, after collecting everything.
        echo "No '$branch' branch on origin yet; starting it empty."
        git worktree add --quiet --detach "$site"
        git -C "$site" checkout --quiet --orphan "$branch"
        git -C "$site" rm -r --quiet --cached . >/dev/null 2>&1 || true
        git -C "$site" clean -fdxq
    else
        die "git ls-remote origin failed with exit code $rc"
    fi

    echo "Site directory: $site ($branch)"
    output dir "$site"
}

publish() {
    local site="${SITE_DIR:?SITE_DIR must name the prepared site directory}"
    [ "$(git -C "$site" symbolic-ref --quiet --short HEAD || true)" = "$branch" ] ||
        die "$site is not on '$branch'; was it made by 'pages.sh prepare'?"
    # The main repository's git directory, for fetch and push (see top).
    local repo
    repo="$(git -C "$site" rev-parse --path-format=absolute --git-common-dir)"

    git -C "$site" add -A
    if git -C "$site" diff --cached --quiet; then
        echo "No changes to publish."
        summary "Dashboard unchanged; nothing pushed to \`$branch\`."
        output changed false
        return
    fi

    git -C "$site" \
        -c user.name="github-actions[bot]" \
        -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        -c commit.gpgsign=false \
        commit --quiet -m "${COMMIT_MESSAGE:-chore(dashboard): update site} $(date -u +%F)"

    # Collection can run long enough (rate-limit backoff) for a transient push
    # failure to be worth retrying. A branch that genuinely moved underneath
    # us (a second writer) fails --ff-only instead of being overwritten.
    local attempt
    for attempt in 1 2 3; do
        if git --git-dir="$repo" push --quiet origin "refs/heads/$branch:refs/heads/$branch"; then
            local sha
            sha="$(git -C "$site" rev-parse --short HEAD)"
            echo "Published $sha to $branch."
            summary "Dashboard published to \`$branch\` as $sha."
            output changed true
            return
        fi
        echo "Push rejected (attempt $attempt/3); re-fetching $branch." >&2
        if git --git-dir="$repo" fetch --quiet origin "refs/heads/$branch" 2>/dev/null; then
            # FETCH_HEAD is per-worktree: resolve it where the fetch wrote it.
            git -C "$site" merge --quiet --ff-only "$(git --git-dir="$repo" rev-parse FETCH_HEAD)" ||
                die "$branch moved in a way that is not a fast-forward (two writers?); it needs manual reconciliation"
        fi
    done
    die "pushing $branch failed after 3 attempts; the job needs 'permissions: contents: write' and actions/checkout's default persist-credentials"
}

case "${1:-}" in
prepare) prepare ;;
publish) publish ;;
*)
    echo "usage: $0 prepare|publish" >&2
    exit 2
    ;;
esac
