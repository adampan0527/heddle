"""Tests for tools/_atomic_io.py.

Covers the byte-level guarantees of atomic_write_text / json and the
crash-recovery contract: if the rename step fails the existing file
must be left intact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _atomic_io


class TestAtomicWriteText:
    def test_round_trip_bytes(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        payload = "hello world\nwith multiple lines\n"
        _atomic_io.atomic_write_text(target, payload)
        assert target.read_bytes() == payload.encode("utf-8")

    def test_overwrites_existing_file(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        target.write_text("original", encoding="utf-8")
        _atomic_io.atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "deeper" / "nested" / "out.txt"
        assert not target.parent.exists()
        _atomic_io.atomic_write_text(target, "x")
        assert target.read_text(encoding="utf-8") == "x"

    def test_unicode_round_trip(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        payload = "中文 + emoji 🎉\n"
        _atomic_io.atomic_write_text(target, payload)
        assert target.read_text(encoding="utf-8") == payload


class TestAtomicWriteJson:
    def test_round_trip_dict(self, tmp_path: Path):
        target = tmp_path / "out.json"
        payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
        _atomic_io.atomic_write_json(target, payload)
        assert json.loads(target.read_text(encoding="utf-8")) == payload

    def test_indent_kwarg(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_io.atomic_write_json(target, {"k": "v"}, indent=4)
        text = target.read_text(encoding="utf-8")
        # indent=4 puts each key on its own line with 4-space indent.
        assert "\n    \"k\"" in text

    def test_ensure_ascii_kwarg_keeps_unicode(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_io.atomic_write_json(target, {"k": "中文"}, ensure_ascii=True)
        text = target.read_text(encoding="utf-8")
        # With ensure_ascii=True the literal characters are escaped.
        assert "\\u" in text

    def test_ensure_ascii_false_preserves_unicode(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_io.atomic_write_json(target, {"k": "中文"}, ensure_ascii=False)
        text = target.read_text(encoding="utf-8")
        assert "中文" in text

    def test_trailing_newline_added(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_io.atomic_write_json(target, {"k": "v"})
        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")


class TestLoadJson:
    def test_loads_existing(self, tmp_path: Path):
        target = tmp_path / "out.json"
        target.write_text(json.dumps({"k": "v"}), encoding="utf-8")
        assert _atomic_io.load_json(target) == {"k": "v"}

    def test_missing_file_raises(self, tmp_path: Path):
        target = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError):
            _atomic_io.load_json(target)


class TestAtomicCrashSafety:
    """If os.replace fails the existing file must NOT be touched."""

    def test_replace_failure_leaves_existing_file_intact(
        self, tmp_path: Path, monkeypatch
    ):
        target = tmp_path / "out.txt"
        original_bytes = b"original contents\n"
        target.write_bytes(original_bytes)

        # Force os.replace to raise OSError. This simulates a
        # transient rename failure (cross-device move, EACCES, etc.).
        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("os.replace", boom)

        with pytest.raises(OSError):
            _atomic_io.atomic_write_text(target, "new contents")

        # Existing file is byte-for-byte identical to the pre-write
        # state. Crucially, it must NOT have been truncated.
        assert target.read_bytes() == original_bytes

    def test_replace_failure_when_file_does_not_exist(
        self, tmp_path: Path, monkeypatch
    ):
        target = tmp_path / "fresh.txt"
        assert not target.exists()

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("os.replace", boom)

        with pytest.raises(OSError):
            _atomic_io.atomic_write_text(target, "x")

        # The original file still does not exist - we never created
        # a truncated version of it.
        assert not target.exists()

    def test_replace_failure_leaves_no_truncated_target(
        self, tmp_path: Path, monkeypatch
    ):
        # A truncated-but-non-empty file would be the failure mode
        # we are protecting against. Confirm the on-disk artifact
        # after a failed write has not been replaced with anything
        # partial.
        target = tmp_path / "out.txt"
        original = b"do not touch me\n"
        target.write_bytes(original)

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("os.replace", boom)

        with pytest.raises(OSError):
            _atomic_io.atomic_write_text(target, "DIFFERENT PAYLOAD\n")

        size_after = target.stat().st_size
        # If truncation occurred the size would be < len(original).
        assert size_after == len(original)
        assert target.read_bytes() == original
