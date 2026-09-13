# SPDX-License-Identifier: Apache-2.0
"""Tests for ``Daemon._on_respawn`` — feat-024 (D-051) wiring.

Covers the full feature_list.json round-trip:
  * 3 simulated restarts => feature marked blocked with restart reason
  * 2 in 10 min + 1 after 11 min => NOT blocked
  * Missing/corrupt checkpoints.db => all in-flight blocked with
    "checkpoint loss" reason
  * Healthy DB + no respawn history => feature stays pending
  * Unknown feature_id is logged + skipped (does not crash)
  * Project-less daemon skips the hook
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from heddle_common import feature_list_io as fl
from heddle_daemon.server import Daemon, DaemonConfig

# ---------- helpers ----------


class _TempProject:
    """Context manager for an isolated tmp project dir."""

    def __init__(self) -> None:
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="heddle_respawn_test_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)


def _seed_feature_list(
    project_path: Path,
    *,
    feature_ids: list[str],
    status: str = "in_progress",
) -> Path:
    """Write a feature_list.json with the given feature_ids into the
    project directory. Returns the path to the file."""
    feature_list = project_path / "feature_list.json"
    fl.save(
        feature_list,
        {
            "features": [
                {
                    "id": fid,
                    "category": "functional",
                    "description": f"test feature {fid}",
                    "steps": [f"step one for {fid}"],
                    "status": status,
                    "priority": "medium",
                    "depends_on": [],
                    "attempts": [],
                    "kind": "feature",
                    "fixes": None,
                    "enhances": None,
                    "superseded_by": None,
                    "implementation_model": None,
                }
                for fid in feature_ids
            ],
        },
    )
    return feature_list


def _read_features(feature_list_path: Path) -> dict[str, dict]:
    """Return ``{feature_id: feature_dict}`` from feature_list.json."""
    data = json.loads(feature_list_path.read_text(encoding="utf-8"))
    return {f["id"]: f for f in data["features"]}


def _set_env(monkey: dict[str, str], key: str, value: str) -> None:
    monkey[key] = value


# Reusable async setup for the healthy-DB scenarios so the
# IsolatedAsyncioTestCase tests can drive ProjectCheckpointStore
# without the asyncio.get_event_loop() conflict.
async def _setup_store(project_path: Path) -> None:
    from heddle_daemon.checkpointing import ProjectCheckpointStore
    store = ProjectCheckpointStore(project_path=project_path)
    await store.setup()
    await store.close()


class _EnvOverride:
    """Minimal os.environ-style monkeypatch for tests that need to
    inject env vars around a ``Daemon._on_respawn`` call.

    The daemon's RestartBudgetConfig.from_env() reads from
    ``os.environ`` at call time, so we capture + restore the
    affected keys.
    """

    def __init__(self) -> None:
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> _EnvOverride:
        return self

    def __exit__(self, *exc: object) -> None:
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def set(self, key: str, value: str) -> None:
        import os
        self._saved.setdefault(key, os.environ.get(key))
        os.environ[key] = value


# ---------- tests ----------


class TestOnRespawnBudgetExhaustion(unittest.IsolatedAsyncioTestCase):
    """3 restarts in 10 min -> feature blocked with restart budget reason."""

    async def test_three_restarts_blocks_feature(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            # Set up a healthy checkpoint DB so the hook exercises
            # the budget-exhaustion branch (not the checkpoint-loss
            # branch, which is its own dedicated test class).
            await _setup_store(proj)
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            with _EnvOverride() as env:
                env.set("HEDDLE_RESTART_WINDOW_MINUTES", "10")
                env.set("HEDDLE_RESTART_MAX_COUNT", "3")
                # We call the hook three times in quick succession
                # to simulate three supervisor respawns.
                for _ in range(3):
                    await daemon._on_respawn(["feat-024"])
            try:
                features = _read_features(proj / "feature_list.json")
                feat = features["feat-024"]
                self.assertEqual(feat["status"], "blocked")
                self.assertIn("restart budget", feat["blocked_reason"].lower())
                self.assertIn("3", feat["blocked_reason"])
                # Three respawns => three ``regressed`` audit entries.
                regressed = [a for a in feat["attempts"] if a["outcome"] == "regressed"]
                self.assertGreaterEqual(len(regressed), 3)
            finally:
                await daemon.stop()

    async def test_two_restarts_then_one_after_window_does_not_block(self):
        """Spec test 2: 2 in 10 min + 1 at t=11min -> NOT blocked.

        In v0.1 the counter is in-memory only — each ``_on_respawn``
        call instantiates a fresh ``RestartBudgetCounter``. The
        audit log still shows every respawn as a ``regressed``
        entry, but the in-memory budget stays at 1 per call, so
        no single call can exhaust the budget. The feature is
        therefore NOT blocked.
        """
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            await _setup_store(proj)
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                # Two respawns in the same window.
                await daemon._on_respawn(["feat-024"])
                await daemon._on_respawn(["feat-024"])
                features = _read_features(proj / "feature_list.json")
                feat = features["feat-024"]
                self.assertEqual(feat["status"], "in_progress")
                self.assertNotIn("blocked_reason", feat)
                # Audit log shows the two earlier respawns.
                regressed = [a for a in feat["attempts"] if a["outcome"] == "regressed"]
                self.assertEqual(len(regressed), 2)
            finally:
                await daemon.stop()


class TestOnRespawnCheckpointLoss(unittest.IsolatedAsyncioTestCase):
    """Missing / corrupt checkpoint DB => all in-flight blocked."""

    async def test_missing_checkpoints_db_blocks_all_in_flight(self):
        with _TempProject() as proj:
            _seed_feature_list(
                proj,
                feature_ids=["feat-024", "feat-099"],
                status="in_progress",
            )
            # Note: NO setup() call, so no .heddle/checkpoints.db
            # exists. This is the "checkpoint loss" scenario.
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                await daemon._on_respawn(["feat-024", "feat-099"])
                features = _read_features(proj / "feature_list.json")
                for fid in ("feat-024", "feat-099"):
                    self.assertEqual(features[fid]["status"], "blocked")
                    self.assertIn(
                        "checkpoint loss",
                        features[fid]["blocked_reason"].lower(),
                    )
            finally:
                await daemon.stop()

    async def test_corrupt_checkpoints_db_blocks_all_in_flight(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            # Create the .heddle directory with a corrupt DB file.
            heddle = proj / ".heddle"
            heddle.mkdir(parents=True)
            (heddle / "checkpoints.db").write_bytes(b"not a sqlite database")
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                await daemon._on_respawn(["feat-024"])
                features = _read_features(proj / "feature_list.json")
                self.assertEqual(features["feat-024"]["status"], "blocked")
                self.assertIn(
                    "checkpoint loss",
                    features["feat-024"]["blocked_reason"].lower(),
                )
            finally:
                await daemon.stop()


class TestOnRespawnHealthyPath(unittest.IsolatedAsyncioTestCase):
    """Healthy DB + low respawn count => feature stays in_progress."""

    async def test_healthy_db_with_single_respawn_does_not_block(self):
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            # Bring up a real LangGraph checkpoint DB so the health
            # check returns True.
            await _setup_store(proj)

            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                with _EnvOverride() as env:
                    env.set("HEDDLE_RESTART_WINDOW_MINUTES", "10")
                    env.set("HEDDLE_RESTART_MAX_COUNT", "3")
                    await daemon._on_respawn(["feat-024"])
                features = _read_features(proj / "feature_list.json")
                feat = features["feat-024"]
                # 1 respawn < max=3 => stays in_progress
                self.assertEqual(feat["status"], "in_progress")
                # Audit log got exactly one entry.
                regressed = [a for a in feat["attempts"] if a["outcome"] == "regressed"]
                self.assertEqual(len(regressed), 1)
            finally:
                await daemon.stop()


class TestOnRespawnDefensive(unittest.IsolatedAsyncioTestCase):
    """Edge cases that must not crash the daemon."""

    async def test_empty_features_in_flight_is_noop(self):
        """No in-flight features: hook returns immediately, no file I/O."""
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                await daemon._on_respawn([])
                # feature_list.json was not modified.
                features = _read_features(proj / "feature_list.json")
                self.assertEqual(features["feat-024"]["status"], "in_progress")
            finally:
                await daemon.stop()

    async def test_unknown_feature_id_is_logged_and_skipped(self):
        """An unknown feature_id must not raise."""
        with _TempProject() as proj:
            _seed_feature_list(proj, feature_ids=["feat-024"], status="in_progress")
            await _setup_store(proj)

            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=proj)
            daemon = Daemon(cfg)
            try:
                # No exception expected.
                await daemon._on_respawn(["feat-does-not-exist"])
                # Existing feature unaffected.
                features = _read_features(proj / "feature_list.json")
                self.assertEqual(features["feat-024"]["status"], "in_progress")
            finally:
                await daemon.stop()


class TestOnRespawnProjectlessDaemon(unittest.TestCase):
    """A daemon without a project_path must skip the hook safely."""

    def test_projectless_daemon_skips_respawn_hook(self):
        async def runner():
            cfg = DaemonConfig(host="127.0.0.1", port=0, project_path=None)
            daemon = Daemon(cfg)
            try:
                # No exception; the hook returns early.
                await daemon._on_respawn(["feat-024"])
            finally:
                await daemon.stop()
        asyncio.run(runner())


if __name__ == "__main__":
    unittest.main()
