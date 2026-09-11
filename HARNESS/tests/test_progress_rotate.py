"""Tests for HARNESS/tools/progress_rotate.py pure helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import progress_rotate


EM = chr(0x2014)

_FAKE_PROGRESS = (
    "PROJECT PROGRESS LOG\n"
    "====================\n"
    "\n"
    "Project: [Project Name]\n"
    "Started: [Date]\n"
    "\n"
    "SESSION 0 - Initializer Agent\n"
    "-----------------------------\n"
    "Date: [Date]\n"
    "Worked on:\n"
    "- Set up project structure\n"
    "\n"
    "---\n"
    "\n"
    f"### SESSION 1 {EM} 2026-07-20\n"
    "\n"
    "**Worked on:** feat-A @ abc123\n"
    "\n"
    "**Status:** Implemented and tested\n"
    "\n"
    "**Features now passing:** feat-A (1 / 5 total)\n"
    "\n"
    "**Notes:** (none)\n"
    "\n"
    "---\n"
    "\n"
    f"### SESSION 2 {EM} 2026-07-21\n"
    "\n"
    "**Worked on:** feat-B @ def456\n"
    "\n"
    "**Status:** Implemented, not yet tested\n"
    "\n"
    "**Features now passing:** feat-B (2 / 5 total)\n"
    "\n"
    "**Notes:** (none)\n"
    "\n"
    "---\n"
    "\n"
    f"### SESSION 3 {EM} 2026-07-22\n"
    "\n"
    "**Worked on:** feat-C @ 789abc\n"
    "\n"
    "**Status:** Implemented and tested\n"
    "\n"
    "**Features now passing:** feat-C (3 / 5 total)\n"
    "\n"
    "**Notes:** (none)\n"
    "\n"
    "---\n"
    "\n"
    "## SUMMARY\n"
    "-------\n"
    "Total features: 5\n"
    "Passing: 3\n"
    "Failing: 2\n"
)


def _write_progress(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestFindSessionBlocks:
    def test_returns_three_blocks(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)

        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        assert len(blocks) == 3
        session_nums = [b[0] for b in blocks]
        assert session_nums == [1, 2, 3]

    def test_block_extent_is_correct(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)

        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        for i, (n, start, end, heading) in enumerate(blocks):
            block_text = text[start:end]
            assert f"SESSION {n}" in heading
            assert "Worked on" in block_text
        for i in range(len(blocks) - 1):
            assert blocks[i][2] == blocks[i + 1][1]

    def test_initializer_bare_session_0_not_picked_up(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        for n, _s, _e, _h in blocks:
            assert n >= 1

    def test_no_blocks_returns_empty(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, "no sessions here\n")
        text = path.read_text(encoding="utf-8")
        assert progress_rotate._find_session_blocks(text) == []


class TestSplitPrelude:
    def test_prelude_is_everything_before_first_block(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        prelude = progress_rotate._split_prelude(text, blocks)

        assert "### SESSION 1" not in prelude
        assert "### SESSION 2" not in prelude
        assert "### SESSION 3" not in prelude
        assert text.endswith(prelude + text[blocks[0][1]:])

    def test_prelude_contains_initializer_session_0(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        prelude = progress_rotate._split_prelude(text, blocks)
        assert "SESSION 0 - Initializer Agent" in prelude

    def test_no_blocks_returns_whole_text_as_prelude(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        prelude = progress_rotate._split_prelude(text, [])
        assert prelude == text


class TestSplitSummary:
    def test_summary_is_trailing_h2_section(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        summary = progress_rotate._split_summary(text, blocks)
        assert "## SUMMARY" in summary
        assert "Total features: 5" in summary

    def test_kept_payload_omits_summary(self, tmp_path):
        path = tmp_path / "current_progress.txt"
        _write_progress(path, _FAKE_PROGRESS)
        text = path.read_text(encoding="utf-8")
        blocks = progress_rotate._find_session_blocks(text)
        to_keep = blocks[2:]
        kept_payload = "".join(text[s:e] for (_n, s, e, _h) in to_keep)
        assert "## SUMMARY" not in kept_payload
        prelude = progress_rotate._split_prelude(text, blocks)
        summary = progress_rotate._split_summary(text, blocks)
        rebuilt = prelude + kept_payload + summary
        assert "## SUMMARY" in rebuilt


class TestDryRunWritesNothing:
    def test_dry_run_does_not_modify_progress_file(self, tmp_path, capsys):
        progress_path = tmp_path / "current_progress.txt"
        older_dir = tmp_path / "sessions" / "older"
        original_bytes = _FAKE_PROGRESS.encode("utf-8")
        progress_path.write_bytes(original_bytes)
        mtime_before = progress_path.stat().st_mtime

        ns = argparse.Namespace(
            progress_file=progress_path,
            older_dir=older_dir,
            keep=1,
            dry_run=True,
        )
        progress_rotate.cmd_rotate(ns)

        assert progress_path.read_bytes() == original_bytes
        assert progress_path.stat().st_mtime == mtime_before
        assert not older_dir.exists()

        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_dry_run_does_not_create_archive_file(self, tmp_path):
        progress_path = tmp_path / "current_progress.txt"
        older_dir = tmp_path / "sessions" / "older"
        progress_path.write_text(_FAKE_PROGRESS, encoding="utf-8")

        ns = argparse.Namespace(
            progress_file=progress_path,
            older_dir=older_dir,
            keep=1,
            dry_run=True,
        )
        progress_rotate.cmd_rotate(ns)

        if older_dir.exists():
            files = list(older_dir.iterdir())
            assert files == []
