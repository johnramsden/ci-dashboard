"""action.yml, checked statically and then run step by step against a local
bare repository standing in for GitHub.

The runner below is deliberately small: it understands exactly the expression
forms action.yml uses and fails loudly on anything else, so a new expression
in action.yml has to be taught here — and so tested — before it can ship.
It skips the two steps that need the network (setting up uv, installing the
package) and hands the rest the `ci-dashboard` this test suite runs under.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from ci_dashboard import simulate

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
ACTION = os.path.join(ROOT, "action.yml")
PAGES_SH = os.path.join(ROOT, "scripts", "pages.sh")

# Steps that need the network; the runner fakes their outputs instead.
NETWORK_STEPS = {"Set up uv", "Install ci-dashboard"}

EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

# The config the fake workspace carries: the two repositories simulate.py
# fabricates history for, so a seeded pages branch renders real pages.
CONFIG = """\
site:
  title: Action test
groups:
  - name: Examples
    repos:
      - repo: example/alpha
        branches: [main, squid]
        exclude:
          - workflow: "Vuln scan*"
        preflight:
          - workflow: Tests
            job: Preflight infra gate
      - repo: example/bravo
        branches: [main, stable/squid]
"""


def load_action():
    with open(ACTION) as f:
        return yaml.safe_load(f)


def ci_dashboard_bin():
    """The console script installed next to the interpreter running the tests."""
    path = os.path.join(os.path.dirname(sys.executable), "ci-dashboard")
    if not os.path.exists(path):
        raise unittest.SkipTest(f"{path} not installed; run the tests via `uv run`")
    return path


class ActionMetadataTest(unittest.TestCase):
    """Static checks: the mistakes a composite action only reveals at run time."""

    @classmethod
    def setUpClass(cls):
        cls.action = load_action()
        cls.steps = cls.action["runs"]["steps"]
        cls.inputs = set(cls.action["inputs"])
        cls.step_ids = {s["id"] for s in cls.steps if "id" in s}

    def test_is_a_composite_action(self):
        self.assertEqual(self.action["runs"]["using"], "composite")

    def test_every_run_step_names_its_shell(self):
        """Composite actions have no default shell; omitting it fails at run time."""
        for step in self.steps:
            if "run" in step:
                self.assertEqual(step.get("shell"), "bash", step["name"])

    def test_no_expression_is_interpolated_into_a_script(self):
        """Inputs reach scripts through env, never `${{ }}` inside `run:`,
        where a crafted input value would become shell code."""
        for step in self.steps:
            if "run" in step:
                self.assertNotIn("${{", step["run"], step["name"])

    def _expressions(self):
        for step in self.steps:
            for key in ("if",):
                if key in step:
                    yield step["name"], str(step[key])
            for mapping in (step.get("env", {}), step.get("with", {})):
                for value in mapping.values():
                    yield step["name"], str(value)
        for name, out in self.action["outputs"].items():
            yield f"outputs.{name}", out["value"]

    def test_every_input_referenced_is_declared(self):
        for where, text in self._expressions():
            for name in re.findall(r"inputs\.([A-Za-z0-9_-]+)", text):
                self.assertIn(name, self.inputs, f"{where} references inputs.{name}")

    def test_every_step_referenced_exists_earlier(self):
        seen = set()
        for step in self.steps:
            for mapping in (step.get("env", {}), step.get("with", {})):
                for value in mapping.values():
                    for ref in re.findall(r"steps\.([A-Za-z0-9_-]+)\.", str(value)):
                        self.assertIn(ref, seen, f"{step['name']} reads steps.{ref} "
                                                 "before it has run")
            for ref in re.findall(r"steps\.([A-Za-z0-9_-]+)\.", str(step.get("if", ""))):
                self.assertIn(ref, seen, f"{step['name']}'s if: reads steps.{ref}")
            if "id" in step:
                seen.add(step["id"])
        for name, out in self.action["outputs"].items():
            for ref in re.findall(r"steps\.([A-Za-z0-9_-]+)\.", out["value"]):
                self.assertIn(ref, self.step_ids, f"output {name} reads steps.{ref}")

    def test_every_input_is_used(self):
        used = {name for _, text in self._expressions()
                for name in re.findall(r"inputs\.([A-Za-z0-9_-]+)", text)}
        self.assertEqual(self.inputs - used, set())

    def test_boolean_inputs_default_to_true_or_false(self):
        """`if: inputs.x == 'true'` compares strings: a default of `yes` or
        `True` would silently disable the step."""
        for name in ("collect", "publish", "fail-on-collect-error"):
            self.assertIn(self.action["inputs"][name]["default"], ("true", "false"), name)

    def test_third_party_actions_are_pinned_to_a_commit(self):
        for step in self.steps:
            if "uses" in step:
                ref = step["uses"].rpartition("@")[2]
                self.assertRegex(ref, r"\A[0-9a-f]{40}\Z", step["uses"])

    def test_scripts_it_runs_exist(self):
        self.assertTrue(os.access(PAGES_SH, os.X_OK))


class ActionRunner:
    """Runs action.yml's steps in order, the way the Actions runner would."""

    def __init__(self, workspace, action_path, runner_temp, inputs, env):
        self.workspace = workspace
        self.action_path = action_path
        self.runner_temp = runner_temp
        self.env = env
        action = load_action()
        self.steps = action["runs"]["steps"]
        self.output_specs = action["outputs"]
        self.results = {}          # step id -> {"outputs": {...}, "outcome": str}
        self.failed = False
        self.log = []
        self.inputs = {}
        # Defaults can be expressions (`${{ github.token }}`); the runner
        # evaluates them before the action starts.
        self.inputs = {name: self._interpolate(spec.get("default", ""))
                       for name, spec in action["inputs"].items()}
        self.inputs.update(inputs)

    # -- expressions --------------------------------------------------------

    def _value(self, ref):
        ref = ref.strip()
        if ref.startswith("inputs."):
            return self.inputs[ref[len("inputs."):]]
        if ref == "github.action_path":
            return self.action_path
        if ref == "github.token":
            return "fake-job-token"
        m = re.fullmatch(r"steps\.([\w-]+)\.outputs\.([\w-]+)", ref)
        if m:
            return self.results.get(m.group(1), {}).get("outputs", {}).get(m.group(2), "")
        m = re.fullmatch(r"steps\.([\w-]+)\.outcome", ref)
        if m:
            return self.results.get(m.group(1), {}).get("outcome", "")
        m = re.fullmatch(r"'([^']*)'", ref)
        if m:
            return m.group(1)
        raise AssertionError(f"ActionRunner does not understand {ref!r}")

    def _condition(self, text):
        """True/False for an `if:`; handles `!cancelled()`, `==`, and `&&`."""
        text = EXPR_RE.sub(lambda m: m.group(1), str(text)).strip()
        result = True
        for term in (t.strip() for t in text.split("&&")):
            if term == "!cancelled()":
                continue
            left, sep, right = term.partition("==")
            if not sep:
                raise AssertionError(f"ActionRunner does not understand if: {term!r}")
            result = result and self._value(left) == self._value(right)
        return result

    def _interpolate(self, text):
        return EXPR_RE.sub(lambda m: self._value(m.group(1)), str(text))

    # -- running ------------------------------------------------------------

    def run(self):
        for step in self.steps:
            name = step["name"]
            condition = step.get("if")
            has_status_fn = condition is not None and "cancelled()" in str(condition)
            if self.failed and not has_status_fn:
                self._record(step, "skipped")
                continue
            if condition is not None and not self._condition(condition):
                self._record(step, "skipped")
                continue
            if name in NETWORK_STEPS:
                outputs = {"bin": ci_dashboard_bin()} if step.get("id") == "install" else {}
                self._record(step, "success", outputs)
                continue
            self._run_script(step)
        return {name: self._interpolate(spec["value"])
                for name, spec in self.output_specs.items()}

    def _record(self, step, outcome, outputs=None):
        if "id" in step:
            self.results[step["id"]] = {"outcome": outcome, "outputs": outputs or {}}

    def _run_script(self, step):
        fd, output_file = tempfile.mkstemp(dir=self.runner_temp)
        os.close(fd)
        env = dict(self.env)
        env.update({k: self._interpolate(v) for k, v in step.get("env", {}).items()})
        env.update({
            "GITHUB_OUTPUT": output_file,
            "GITHUB_STEP_SUMMARY": os.path.join(self.runner_temp, "summary.md"),
            "GITHUB_ACTION_PATH": self.action_path,
            "RUNNER_TEMP": self.runner_temp,
        })
        if env.get("GH_TOKEN"):
            # A collector with a token would query api.github.com for real.
            raise AssertionError(
                f"{step['name']} would reach the network: pass collect='false', "
                "or token='' to make collection fail offline")
        # The command line Actions uses for `shell: bash`.
        proc = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail",
                               "-c", step["run"]], cwd=self.workspace,
                              env=env, capture_output=True, text=True)
        self.log.append((step["name"], proc.returncode, proc.stdout, proc.stderr))
        outputs = {}
        with open(output_file) as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if not sep or "<<" in key:
                    raise AssertionError(
                        f"{step['name']}: ActionRunner only understands `key=value` "
                        f"outputs, not {line!r}; teach it the multiline form first")
                outputs[key] = value
        outcome = "success" if proc.returncode == 0 else "failure"
        self._record(step, outcome, outputs)
        if proc.returncode != 0:
            self.failed = True

    def dump(self):
        return "\n".join(f"--- {name} (exit {rc})\n{out}{err}"
                         for name, rc, out, err in self.log)


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class ActionRunTest(unittest.TestCase):
    """The whole action, end to end, minus the network."""

    def setUp(self):
        self.bin = ci_dashboard_bin()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        # Isolate from the developer's git config (signing, hooks, identity).
        gitconfig = os.path.join(tmp, "gitconfig")
        with open(gitconfig, "w") as f:
            f.write("[user]\n\tname = Test\n\temail = test@example.com\n"
                    "[init]\n\tdefaultBranch = main\n")
        self.env = {
            "PATH": os.environ["PATH"], "HOME": tmp,
            "GIT_CONFIG_GLOBAL": gitconfig, "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.origin = os.path.join(tmp, "origin.git")
        git("init", "--quiet", "--bare", self.origin, env=self.env)
        seed = os.path.join(tmp, "seed")
        git("clone", "--quiet", self.origin, seed, env=self.env)
        os.makedirs(os.path.join(seed, ".github"))
        with open(os.path.join(seed, ".github", "ci-dashboard.yml"), "w") as f:
            f.write(CONFIG)
        git("add", "-A", cwd=seed, env=self.env)
        git("commit", "--quiet", "-m", "init", cwd=seed, env=self.env)
        git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=seed, env=self.env)
        self.workspace = os.path.join(tmp, "workspace")
        git("clone", "--quiet", "--depth=1", "file://" + self.origin, self.workspace,
            env=self.env)
        self.runner_temp = os.path.join(tmp, "runner-temp")
        os.makedirs(self.runner_temp)

    def tearDown(self):
        # The worktree registration lives in the workspace; remove both.
        self._tmp.cleanup()

    def run_action(self, **inputs):
        # A fresh RUNNER_TEMP per run, like a fresh job.
        shutil.rmtree(self.runner_temp)
        os.makedirs(self.runner_temp)
        shutil.rmtree(self.workspace)
        git("clone", "--quiet", "--depth=1", "file://" + self.origin, self.workspace,
            env=self.env)
        runner = ActionRunner(self.workspace, ROOT, self.runner_temp,
                              {k.replace("_", "-"): v for k, v in inputs.items()},
                              self.env)
        outputs = runner.run()
        return runner, outputs

    def pages_files(self):
        listing = git("ls-tree", "-r", "--name-only", "gh-pages", cwd=self.origin, env=self.env)
        return set(listing.splitlines())

    def seed_pages_data(self):
        """Put fabricated snapshots on gh-pages, as earlier collections would have."""
        clone = os.path.join(self._tmp.name, "pages-seed")
        git("clone", "--quiet", self.origin, clone, env=self.env)
        git("checkout", "--quiet", "--orphan", "gh-pages", cwd=clone, env=self.env)
        git("rm", "-rf", "--quiet", ".", cwd=clone, env=self.env)
        simulate.write_history(os.path.join(clone, "data"))
        git("add", "-A", cwd=clone, env=self.env)
        git("commit", "--quiet", "-m", "seed data", cwd=clone, env=self.env)
        git("push", "--quiet", "origin", "gh-pages", cwd=clone, env=self.env)

    def test_runner_refuses_to_reach_the_network(self):
        """Defaults (collect: true, the job token) would query GitHub for real."""
        with self.assertRaisesRegex(AssertionError, "would reach the network"):
            self.run_action()

    def test_first_run_creates_the_pages_branch(self):
        runner, outputs = self.run_action(collect="false")
        self.assertFalse(runner.failed, runner.dump())
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(outputs["collect-failed"], "false")
        files = self.pages_files()
        for path in ("index.html", ".nojekyll", "ci-health/index.html",
                     "ci-health/example--alpha/index.html", "static/ci-health.css"):
            self.assertIn(path, files)
        # The orphan branch must not inherit the workspace's own files.
        self.assertNotIn(".github/ci-dashboard.yml", files)

    def test_renders_the_history_already_on_the_branch(self):
        self.seed_pages_data()
        runner, outputs = self.run_action(collect="false")
        self.assertFalse(runner.failed, runner.dump())
        self.assertEqual(outputs["changed"], "true")
        files = self.pages_files()
        self.assertIn("data/example--alpha/2026-04-14.json", files, "data kept")
        self.assertTrue(any(f.startswith("ci-health/example--alpha/jobs/") for f in files),
                        "job pages rendered from the seeded snapshots")
        index = git("show", "gh-pages:ci-health/index.html", cwd=self.origin, env=self.env)
        self.assertIn("example/alpha", index)
        self.assertNotIn("No data yet", index.split("example/alpha")[1][:400])

    def test_unchanged_data_publishes_nothing(self):
        self.seed_pages_data()
        self.run_action(collect="false")
        head = git("rev-parse", "gh-pages", cwd=self.origin, env=self.env)
        runner, outputs = self.run_action(collect="false")
        self.assertFalse(runner.failed, runner.dump())
        self.assertEqual(outputs["changed"], "false")
        self.assertEqual(git("rev-parse", "gh-pages", cwd=self.origin, env=self.env), head,
                         "rendering is a pure function of the data: no empty commits")

    def test_publish_false_builds_without_pushing(self):
        self.seed_pages_data()
        before = git("rev-parse", "gh-pages", cwd=self.origin, env=self.env)
        runner, outputs = self.run_action(collect="false", publish="false")
        self.assertFalse(runner.failed, runner.dump())
        self.assertEqual(git("rev-parse", "gh-pages", cwd=self.origin, env=self.env), before)
        self.assertEqual(outputs["changed"], "")
        self.assertTrue(os.path.isfile(os.path.join(outputs["site-dir"],
                                                    "ci-health", "example--alpha", "index.html")))

    def test_collection_failure_still_publishes_then_fails(self):
        """No token: `collect` fails fast, offline. The rest must still render
        and publish, and the action must fail at the very end."""
        self.seed_pages_data()
        runner, outputs = self.run_action(token="")
        self.assertTrue(runner.failed)
        self.assertEqual(outputs["collect-failed"], "true")
        self.assertEqual(outputs["changed"], "true", runner.dump())
        self.assertEqual(runner.results["result"]["outcome"], "failure")
        self.assertIn("ci-health/example--alpha/index.html", self.pages_files())

    def test_collection_failure_can_be_tolerated(self):
        runner, outputs = self.run_action(token="", fail_on_collect_error="false")
        self.assertFalse(runner.failed, runner.dump())
        self.assertEqual(outputs["collect-failed"], "true")

    def test_invalid_config_fails_before_touching_git(self):
        with open(os.path.join(self._tmp.name, "seed", ".github", "ci-dashboard.yml"), "w") as f:
            f.write("groups: []\n")
        seed = os.path.join(self._tmp.name, "seed")
        git("commit", "--quiet", "-am", "break config", cwd=seed, env=self.env)
        git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=seed, env=self.env)
        runner, outputs = self.run_action(collect="false")
        self.assertTrue(runner.failed)
        self.assertEqual(runner.results["site"]["outcome"], "skipped")
        self.assertNotIn("gh-pages", git("branch", "--list", cwd=self.origin, env=self.env))

    def test_config_input_selects_the_file(self):
        runner, outputs = self.run_action(collect="false", config="missing.yml")
        self.assertTrue(runner.failed)
        self.assertIn("missing.yml", runner.dump())


class PagesScriptTest(unittest.TestCase):
    """scripts/pages.sh edge cases the full-action tests do not reach."""

    def run_pages(self, command, cwd, **env):
        full = {"PATH": os.environ["PATH"], "HOME": cwd,
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **env}
        return subprocess.run(["bash", PAGES_SH, command], cwd=cwd, env=full,
                              capture_output=True, text=True)

    def test_outside_a_checkout_says_to_run_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_pages("prepare", tmp, RUNNER_TEMP=tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("actions/checkout", proc.stderr)

    def test_refuses_a_non_empty_site_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            git("init", "--quiet", tmp)
            git("remote", "add", "origin", tmp, cwd=tmp)
            site = os.path.join(tmp, "site")
            os.makedirs(site)
            open(os.path.join(site, "stray"), "w").close()
            proc = self.run_pages("prepare", tmp, SITE_DIR=site)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not empty", proc.stderr)

    def test_a_second_writer_is_never_overwritten(self):
        """gh-pages moving between prepare and publish must fail the publish,
        not force our commit over the other writer's."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                   "PATH": os.environ["PATH"], "HOME": tmp}
            origin = os.path.join(tmp, "origin.git")
            git("init", "--quiet", "--bare", "-b", "main", origin, env=env)
            other = os.path.join(tmp, "other")
            git("clone", "--quiet", origin, other, env=env)
            git("checkout", "--quiet", "-b", "gh-pages", cwd=other, env=env)
            git("commit", "--quiet", "--allow-empty", "-m", "first", cwd=other, env=env)
            git("push", "--quiet", "origin", "gh-pages", cwd=other, env=env)
            git("commit", "--quiet", "--allow-empty", "-m", "main", cwd=other, env=env)
            git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=other, env=env)
            workspace = os.path.join(tmp, "workspace")
            git("clone", "--quiet", "--depth=1", "file://" + origin, workspace, env=env)
            site = os.path.join(tmp, "site")
            proc = self.run_pages("prepare", workspace, SITE_DIR=site)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # The other writer lands first.
            git("commit", "--quiet", "--allow-empty", "-m", "other writer", cwd=other, env=env)
            git("push", "--quiet", "origin", "HEAD:refs/heads/gh-pages", cwd=other, env=env)
            theirs = git("rev-parse", "gh-pages", cwd=origin, env=env)
            open(os.path.join(site, "index.html"), "w").close()
            proc = self.run_pages("publish", workspace, SITE_DIR=site)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not a fast-forward", proc.stderr)
            self.assertEqual(git("rev-parse", "gh-pages", cwd=origin, env=env), theirs)

    def test_unknown_command_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_pages("frobnicate", tmp)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
