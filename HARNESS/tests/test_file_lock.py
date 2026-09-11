"""Tests for tools/_file_lock.py.

We use multiprocessing to avoid GIL interactions. Cross-process
locks are the real test - threads would be serialised by the GIL
and the second acquire would appear to succeed when it should have
blocked.

The lock targets a tmp_path file. Each subprocess gets a unique
role (holder vs waiter) and we assert that the waiter times out.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

import _file_lock

TIMEOUT = 1.0


def _setup_child(short_timeout):
    _file_lock._LOCK_TIMEOUT_SECONDS = short_timeout


def _child_run_exclusive_holder(target, ready_q, result_q, short_timeout):
    _setup_child(short_timeout)
    try:
        with _file_lock.file_lock(target, exclusive=True):
            ready_q.put("ready")
            time.sleep(short_timeout + 1.0)
            result_q.put("done")
    except Exception as e:
        result_q.put(f"error: {e!r}")


def _child_run_shared_holder(target, ready_q, result_q, short_timeout):
    _setup_child(short_timeout)
    try:
        with _file_lock.file_lock(target, exclusive=False):
            ready_q.put("ready")
            time.sleep(short_timeout + 1.0)
            result_q.put("done")
    except Exception as e:
        result_q.put(f"error: {e!r}")


def _child_run_exclusive_with_timeout(target, result_q, short_timeout):
    _setup_child(short_timeout)
    try:
        with _file_lock.file_lock(target, exclusive=True):
            result_q.put("acquired")
    except TimeoutError:
        result_q.put("timeout")
    except Exception as e:
        result_q.put(f"error: {e!r}")


def _run_exclusive_then_exclusive(target):
    ctx = multiprocessing.get_context("spawn")
    ready_q = ctx.Queue()
    result_a = ctx.Queue()
    result_b = ctx.Queue()

    proc_a = ctx.Process(
        target=_child_run_exclusive_holder,
        args=(target, ready_q, result_a, TIMEOUT),
    )
    proc_b = ctx.Process(
        target=_child_run_exclusive_with_timeout,
        args=(target, result_b, TIMEOUT),
    )
    proc_a.start()
    assert ready_q.get(timeout=TIMEOUT + 1.0) == "ready"
    proc_b.start()
    proc_b.join(timeout=TIMEOUT + 1.5)
    if proc_b.is_alive():
        proc_b.terminate()
        proc_b.join()
    proc_a.join(timeout=TIMEOUT + 2.0)
    if proc_a.is_alive():
        proc_a.terminate()
        proc_a.join()
    return result_a.get_nowait(), result_b.get_nowait()


def _run_shared_then_exclusive(target):
    ctx = multiprocessing.get_context("spawn")
    ready_q = ctx.Queue()
    result_a = ctx.Queue()
    result_b = ctx.Queue()

    proc_a = ctx.Process(
        target=_child_run_shared_holder,
        args=(target, ready_q, result_a, TIMEOUT),
    )
    proc_b = ctx.Process(
        target=_child_run_exclusive_with_timeout,
        args=(target, result_b, TIMEOUT),
    )
    proc_a.start()
    assert ready_q.get(timeout=TIMEOUT + 1.0) == "ready"
    proc_b.start()
    proc_b.join(timeout=TIMEOUT + 1.5)
    if proc_b.is_alive():
        proc_b.terminate()
        proc_b.join()
    proc_a.join(timeout=TIMEOUT + 2.0)
    if proc_a.is_alive():
        proc_a.terminate()
        proc_a.join()
    return result_a.get_nowait(), result_b.get_nowait()


pytestmark = pytest.mark.skipif(
    sys.platform not in ("win32", "linux", "darwin"),
    reason="file_lock only tested on POSIX + Windows",
)


class TestFileLockContention:
    def _make_lock_target(self, tmp_path):
        target = tmp_path / "lock_target.txt"
        target.write_text("lock file", encoding="utf-8")
        return target

    def test_sentinel_path_derives_sibling(self, tmp_path):
        target = tmp_path / "lock_target.txt"
        assert _file_lock.sentinel_path(target) == tmp_path / "lock_target.txt.lock"

    def test_holder_can_read_data_file(self, tmp_path):
        """Holding file_lock must not block reads of the data file itself.

        Regression for the byte-0 msvcrt.locking bug: pre-fix, Windows
        raised PermissionError on open(path, 'r') from the same
        process while the lock was held. The sentinel design decouples
        the data file from the lock target, so the data file remains
        freely readable while a lock is held.

        Runs in-process so it exercises the same-process read path
        that triggered the original bug. Cross-platform: passes on
        POSIX (lock target is now a sibling, not the data file) and
        on Windows (data file is never opened by file_lock).
        """
        target = self._make_lock_target(tmp_path)
        with _file_lock.file_lock(target):
            # This is the regression: opening the data file for read
            # while the lock is held must succeed.
            with target.open("r", encoding="utf-8") as fh:
                assert fh.read() == "lock file"

    def test_two_exclusive_locks_one_times_out(self, tmp_path):
        target = self._make_lock_target(tmp_path)
        a_out, b_out = _run_exclusive_then_exclusive(target)
        assert a_out == "done", f"holder failed: {a_out}"
        assert b_out == "timeout", (
            f"second exclusive acquire should time out, got: {b_out}"
        )

    def test_shared_then_exclusive_times_out(self, tmp_path):
        target = self._make_lock_target(tmp_path)
        a_out, b_out = _run_shared_then_exclusive(target)
        assert a_out == "done", f"shared holder failed: {a_out}"
        assert b_out == "timeout", (
            f"exclusive acquire under shared holder should time out, got: {b_out}"
        )

    def test_release_allows_next_acquire(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        ready_q = ctx.Queue()
        result_a = ctx.Queue()
        result_b = ctx.Queue()

        target = self._make_lock_target(tmp_path)

        proc_a = ctx.Process(
            target=_child_run_exclusive_holder,
            args=(target, ready_q, result_a, 0.2),
        )
        proc_b = ctx.Process(
            target=_child_run_exclusive_with_timeout,
            args=(target, result_b, 1.0),
        )
        proc_a.start()
        assert ready_q.get(timeout=2.0) == "ready"
        proc_a.join(timeout=3.0)
        assert not proc_a.is_alive()

        proc_b.start()
        proc_b.join(timeout=3.0)
        if proc_b.is_alive():
            proc_b.terminate()
            proc_b.join()
        a_out = result_a.get_nowait()
        b_out = result_b.get_nowait()
        assert a_out == "done", f"holder failed: {a_out}"
        assert b_out == "acquired", (
            f"second exclusive acquire after release should succeed, got: {b_out}"
        )
