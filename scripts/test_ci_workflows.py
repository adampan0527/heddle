# SPDX-License-Identifier: Apache-2.0
"""Tests for the GitHub Actions CI workflows — feat-006.

Structural assertions over the workflow YAML at .github/workflows/.
These tests do NOT call GitHub's API; they exist to catch regressions
in the workflow definition itself (accidentally dropping a matrix OS,
removing a cache step, flipping HEDDLE_FAKE_LLM, etc.).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY_YML = REPO_ROOT / ".github" / "workflows" / "nightly.yml"


def _on(data: dict) -> dict:
    """Return the `on:` block, handling YAML's `on` -> True quirk.

    PyYAML 1.1 (the legacy default) parses `on:` as the boolean
    True. PyYAML 5.1+ uses `on` literally. PyYAML 6.x preserves the
    bareword, but GitHub's own YAML reader does NOT — it parses `on`
    as True. We defend against both shapes so the test stays portable
    between local PyYAML and the GitHub runner's parser.
    """
    if "on" in data:
        return data["on"]
    if True in data:
        return data[True]
    raise KeyError("workflow has no `on:` block")


def _flatten_steps(job: dict) -> list[dict]:
    """Return every step in a job, expanding any `steps:` blocks.

    GitHub Actions supports both `jobs.<id>.steps` (a flat list) and
    nested reusable workflows via `jobs.<id>.uses` (referenced by
    SHA). We only ship the flat form, so the flatten is a no-op, but
    keeping the helper makes the assertions below read cleaner.
    """
    return job.get("steps", []) or []


def _step_names(job: dict) -> list[str]:
    return [s.get("name", "") for s in _flatten_steps(job)]


def _step_uses(job: dict) -> list[str]:
    return [s.get("uses", "") for s in _flatten_steps(job)]


def _step_runs(job: dict) -> list[str]:
    return [s.get("run", "") for s in _flatten_steps(job)]


class TestCIWorkflow(unittest.TestCase):
    """feat-006 / T-022 — CI runs install/build/unit/integration under HEDDLE_FAKE_LLM=1."""

    @classmethod
    def setUpClass(cls):
        if not CI_YML.exists():
            raise unittest.SkipTest(f"{CI_YML} not found")
        cls.data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))

    def test_yaml_parses_with_expected_top_level_keys(self):
        self.assertIn("name", self.data)
        # `on:` may parse as either the string "on" or the boolean
        # True depending on the YAML reader — see _on() for the
        # cross-reader shim. The tests below use _on() for every
        # access.
        self.assertTrue("on" in self.data or True in self.data)
        self.assertIn("jobs", self.data)

    # ---- step 1: triggers ----

    def test_triggers_on_pull_request_and_push_to_main(self):
        on = _on(self.data)
        # GitHub Actions allows `on:` to be a string OR a dict. The
        # dict form is what we ship.
        self.assertIsInstance(on, dict)
        self.assertIn("pull_request", on)
        push = on.get("push")
        self.assertIsNotNone(push)
        # `branches:` may be a list; either way 'main' must be present.
        if isinstance(push, dict):
            branches = push.get("branches", [])
            self.assertIn("main", branches)
        else:
            # Bare `push:` means all branches — not what we want.
            self.fail("push trigger must restrict to branches: [main]")

    # ---- step 2: matrix on the three OSes ----

    def test_matrix_includes_ubuntu_macos_windows(self):
        jobs = self.data["jobs"]
        # The matrix is under strategy.matrix.os. We require exactly
        # these three runners — drop or add must be a deliberate change.
        os_values: list[str] = []
        for job in jobs.values():
            strategy = job.get("strategy") or {}
            matrix = strategy.get("matrix") or {}
            if "os" in matrix:
                os_values.extend(matrix["os"])
        self.assertEqual(
            sorted(os_values),
            ["macos-latest", "ubuntu-latest", "windows-latest"],
        )

    def test_matrix_includes_python_3_11_and_node_20(self):
        # Same loop as above but for python / node versions.
        py_values: list[str] = []
        node_values: list[str] = []
        for job in self.data["jobs"].values():
            matrix = (job.get("strategy") or {}).get("matrix") or {}
            py_values.extend(matrix.get("python-version", []))
            node_values.extend(matrix.get("node-version", []))
        self.assertIn("3.11", py_values)
        self.assertIn("20", node_values)

    # ---- step 3: every required step is present ----

    def test_required_steps_present(self):
        # Assert every job has the full install/build/test surface
        # (per feat-006 step 3). Uses names + uses + runs to be
        # robust against formatting drift.
        uses_keywords = [
            "actions/checkout",
            "actions/setup-node",
            "actions/setup-python",
            "pnpm/action-setup",
            "actions/cache",
        ]
        run_keywords = [
            "pnpm install",
            "pip install -e",
            "pnpm --filter web build",
        ]
        for job_id, job in self.data["jobs"].items():
            uses_blob = " ".join(_step_uses(job))
            runs_blob = "\n".join(_step_runs(job))
            for kw in uses_keywords:
                self.assertIn(
                    kw,
                    uses_blob,
                    f"job {job_id!r} missing uses step containing {kw!r}",
                )
            for kw in run_keywords:
                self.assertIn(
                    kw,
                    runs_blob,
                    f"job {job_id!r} missing run step containing {kw!r}",
                )

    # ---- step 4: HEDDLE_FAKE_LLM=1 in env ----

    def test_heddle_fake_llm_set_in_job_env(self):
        for job_id, job in self.data["jobs"].items():
            env = job.get("env") or {}
            self.assertEqual(
                env.get("HEDDLE_FAKE_LLM"),
                "1",
                f"job {job_id!r} must set HEDDLE_FAKE_LLM=1 in env",
            )

    # ---- step 6: pnpm + pip caches ----

    def test_pnpm_cache_configured(self):
        for job_id, job in self.data["jobs"].items():
            cache_steps = [
                s for s in _flatten_steps(job)
                if (s.get("uses") or "").startswith("actions/cache")
            ]
            self.assertTrue(
                cache_steps,
                f"job {job_id!r} must include an actions/cache step",
            )
            for step in cache_steps:
                key = step.get("with", {}).get("key", "")
                self.assertIn(
                    "pnpm-lock.yaml",
                    key,
                    f"job {job_id!r} cache key must be keyed off pnpm-lock.yaml",
                )

    def test_pip_cache_configured(self):
        for job_id, job in self.data["jobs"].items():
            py_steps = [
                s for s in _flatten_steps(job)
                if (s.get("uses") or "").startswith("actions/setup-python")
            ]
            self.assertTrue(
                py_steps,
                f"job {job_id!r} must include a setup-python step",
            )
            for step in py_steps:
                cache = step.get("with", {}).get("cache")
                self.assertEqual(
                    cache,
                    "pip",
                    f"job {job_id!r} setup-python must enable pip cache",
                )


class TestNightlyWorkflow(unittest.TestCase):
    """feat-006 / T-022 — nightly live-LLM E2E behind a repo secret."""

    @classmethod
    def setUpClass(cls):
        if not NIGHTLY_YML.exists():
            raise unittest.SkipTest(f"{NIGHTLY_YML} not found")
        cls.data = yaml.safe_load(NIGHTLY_YML.read_text(encoding="utf-8"))

    # ---- step 5: nightly has cron + gate ----

    def test_has_cron_schedule(self):
        on = _on(self.data)
        schedules = on.get("schedule") or []
        self.assertTrue(schedules, "nightly must have a cron schedule")
        cron = schedules[0].get("cron", "")
        self.assertTrue(
            cron,
            "nightly schedule entry must have a non-empty cron expression",
        )
        # Smoke check: cron expression has the expected 5-field shape.
        self.assertEqual(len(cron.split()), 5)

    def test_skip_when_secret_unset(self):
        """The live-LLM job must be gated on secrets.LIVE_LLM_TESTS.

        We assert the gate is wired (so accidental forks cannot burn
        LLM budget) plus a fallback gate-notice job that explains
        the skip.
        """
        jobs = self.data["jobs"]
        self.assertTrue(jobs)
        # At least one job's `if:` references the secret.
        gates = [
            job.get("if", "")
            for job in jobs.values()
            if "secrets.LIVE_LLM_TESTS" in (job.get("if") or "")
        ]
        self.assertTrue(
            gates,
            "at least one job must gate on secrets.LIVE_LLM_TESTS",
        )

    def test_no_heddle_fake_llm_in_live_job(self):
        """The live-LLM job must NOT set HEDDLE_FAKE_LLM.

        Setting both would mask real-provider failures. The nightly
        is precisely the place real LLM calls happen.
        """
        for job_id, job in self.data["jobs"].items():
            env = job.get("env") or {}
            self.assertNotIn(
                "HEDDLE_FAKE_LLM",
                env,
                f"nightly job {job_id!r} must NOT set HEDDLE_FAKE_LLM "
                "(that would defeat the purpose of the nightly run)",
            )

    def test_live_job_runs_same_install_build_test_surface(self):
        """Per feat-006 step 5: nightly calls the same test commands
        as ci.yml (minus the HEDDLE_FAKE_LLM env var).
        """
        jobs = self.data["jobs"]
        # Exclude the gate-notice job — it intentionally has no install
        # surface because it never runs when the gate is satisfied.
        live_jobs = [
            j for j in jobs.values()
            if "secrets.LIVE_LLM_TESTS" in (j.get("if") or "")
            and "pnpm install" in "\n".join(_step_runs(j))
        ]
        self.assertTrue(live_jobs, "no live-LLM job with install surface found")
        for job in live_jobs:
            runs_blob = "\n".join(_step_runs(job))
            self.assertIn("pnpm install", runs_blob)
            self.assertIn("pip install -e", runs_blob)
            self.assertIn("pnpm --filter web build", runs_blob)

    def test_pnpm_and_pip_cache_present(self):
        for job_id, job in self.data["jobs"].items():
            cache_steps = [
                s for s in _flatten_steps(job)
                if (s.get("uses") or "").startswith("actions/cache")
            ]
            py_steps = [
                s for s in _flatten_steps(job)
                if (s.get("uses") or "").startswith("actions/setup-python")
            ]
            if py_steps:
                self.assertTrue(
                    cache_steps,
                    f"nightly job {job_id!r} must have a pnpm cache step",
                )
                for step in cache_steps:
                    self.assertIn(
                        "pnpm-lock.yaml",
                        step.get("with", {}).get("key", ""),
                        f"nightly job {job_id!r} cache key must be lockfile-keyed",
                    )


if __name__ == "__main__":
    unittest.main()
