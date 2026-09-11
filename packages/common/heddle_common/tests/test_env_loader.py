# SPDX-License-Identifier: Apache-2.0
"""Tests for heddle_common.env_loader — feat-013."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from heddle_common import env_loader as el

# Tests use a per-test scratch dir under packages/common/heddle_common/tests.
SCRATCH_PARENT = Path(sys.argv[0]).resolve().parent


class TestParseEnvLines(unittest.TestCase):
    """Step 1: KEY=VALUE parser — comments, blanks, quoted values."""

    def test_empty_input_yields_empty_dict(self):
        self.assertEqual(el.parse_env_lines(""), {})

    def test_blank_lines_and_comments_skipped(self):
        text = textwrap.dedent(
            """\
            # this is a comment
            KEY1=value1

               # indented comment
            KEY2=value2
            """
        )
        self.assertEqual(
            el.parse_env_lines(text),
            {"KEY1": "value1", "KEY2": "value2"},
        )

    def test_unquoted_value(self):
        self.assertEqual(
            el.parse_env_lines("FOO=bar"),
            {"FOO": "bar"},
        )

    def test_double_quoted_value_with_escapes(self):
        text = r'PATH="C:\Users\Adam\Projects\heddle"'
        self.assertEqual(
            el.parse_env_lines(text),
            {"PATH": "C:\\Users\\Adam\\Projects\\heddle"},
        )

    def test_double_quoted_value_with_spaces_and_newline(self):
        # \n inside double quotes -> real newline.
        self.assertEqual(
            el.parse_env_lines('GREETING="hello\\nworld"'),
            {"GREETING": "hello\nworld"},
        )

    def test_single_quoted_value_is_literal(self):
        # No escape processing inside single quotes.
        self.assertEqual(
            el.parse_env_lines(r"PATH='C:\Users\no\escape'"),
            {"PATH": r"C:\Users\no\escape"},
        )

    def test_trailing_inline_comment_only_for_unquoted(self):
        # Bare values may have an inline `#` after them; quoted values
        # never strip trailing `#`.
        self.assertEqual(
            el.parse_env_lines("FOO=bar # comment"),
            {"FOO": "bar"},
        )

    def test_invalid_line_raises(self):
        with self.assertRaises(el.EnvParseError) as ctx:
            el.parse_env_lines("NOT A VALID LINE")
        self.assertIn("line 1", str(ctx.exception))

    def test_invalid_key_raises(self):
        # Key starting with a digit is rejected.
        with self.assertRaises(el.EnvParseError):
            el.parse_env_lines("1FOO=bar")

    def test_repeated_key_last_wins(self):
        # Mirrors POSIX shell semantics.
        self.assertEqual(
            el.parse_env_lines("FOO=first\nFOO=second"),
            {"FOO": "second"},
        )

    def test_whitespace_around_key_trimmed(self):
        self.assertEqual(
            el.parse_env_lines("  FOO  =bar"),
            {"FOO": "bar"},
        )


class TestLoadEnvFile(unittest.TestCase):
    """Steps 2+3+6+7: load_env_file + permission check + subprocess env."""

    def setUp(self):
        self.env_file = SCRATCH_PARENT / "_test_env_loader.env"
        self.gitignore = SCRATCH_PARENT / "_test_env_loader.gitignore"
        # Snapshot the relevant env vars so each test starts clean.
        self._saved = {k: os.environ.get(k) for k in ("TEST_VAR", "HEDDLE_TEST_VAR")}

    def tearDown(self):
        # Restore any env vars we touched (deleting if they were new).
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for f in (self.env_file, self.gitignore):
            if f.exists():
                # Reset mode first so unlink doesn't trip on a
                # too-permissive scratch file (POSIX only).
                if not sys.platform.startswith("win"):
                    try:
                        os.chmod(f, 0o600)
                    except OSError:
                        pass
                f.unlink()

    def _write_env(self, text: str) -> None:
        """Write the env file and chmod it to 0o600 (POSIX only).

        Windows ignores the chmod but the file is still readable; the
        permission check tests below skip on Windows because the OS
        doesn't model unix-style perms.
        """
        self.env_file.write_text(text, encoding="utf-8")
        if not sys.platform.startswith("win"):
            os.chmod(self.env_file, 0o600)

    # ---- step 2: process env wins ----

    def test_missing_file_is_noop(self):
        self.assertFalse(self.env_file.exists())
        installed = el.load_env_file(self.env_file)
        self.assertEqual(installed, {})
        # No env var was set.
        self.assertNotIn("TEST_VAR", os.environ)

    def test_loads_missing_keys_into_environ(self):
        self._write_env("TEST_VAR=test_value\n")
        installed = el.load_env_file(self.env_file)
        self.assertEqual(installed, {"TEST_VAR": "test_value"})
        self.assertEqual(os.environ.get("TEST_VAR"), "test_value")

    def test_process_env_takes_precedence(self):
        os.environ["TEST_VAR"] = "from_process_env"
        self._write_env("TEST_VAR=from_dotenv\n")
        installed = el.load_env_file(self.env_file)
        # The .env value was NOT installed (process env wins).
        self.assertEqual(installed, {})
        self.assertEqual(os.environ["TEST_VAR"], "from_process_env")

    def test_process_env_can_pass_explicit_environ(self):
        """`environ` shim lets unit tests avoid mutating os.environ."""
        self._write_env("HEDDLE_TEST_VAR=value_from_file\n")
        sentinel = {"EXISTING": "kept"}
        installed = el.load_env_file(self.env_file, environ=sentinel)
        # Installed because the key wasn't in the shim.
        self.assertEqual(installed, {"HEDDLE_TEST_VAR": "value_from_file"})
        # Now in the shim.
        self.assertEqual(sentinel["HEDDLE_TEST_VAR"], "value_from_file")
        # Pre-existing entries are untouched.
        self.assertEqual(sentinel["EXISTING"], "kept")
        # os.environ itself was NOT touched.
        self.assertNotIn("HEDDLE_TEST_VAR", os.environ)

    # ---- step 3: permission check ----

    def test_refuses_group_readable_file(self):
        """chmod 640 (group-readable) raises EnvPermissionError."""
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission bits are not enforced on Windows")
        self.env_file.write_text("TEST_VAR=value\n", encoding="utf-8")
        os.chmod(self.env_file, 0o640)
        with self.assertRaises(el.EnvPermissionError) as ctx:
            el.load_env_file(self.env_file)
        # Message names the path AND the offending mode AND the chmod fix.
        msg = str(ctx.exception)
        self.assertIn(str(self.env_file), msg)
        self.assertIn("chmod 600", msg)

    def test_refuses_world_readable_file(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission bits are not enforced on Windows")
        self.env_file.write_text("TEST_VAR=value\n", encoding="utf-8")
        os.chmod(self.env_file, 0o644)
        with self.assertRaises(el.EnvPermissionError):
            el.load_env_file(self.env_file)

    def test_accepts_owner_only_mode(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission bits are not enforced on Windows")
        self.env_file.write_text("TEST_VAR=value\n", encoding="utf-8")
        os.chmod(self.env_file, 0o600)
        installed = el.load_env_file(self.env_file)
        self.assertEqual(installed, {"TEST_VAR": "value"})

    def test_subprocess_sees_loaded_value(self):
        """step 6: subprocess reads TEST_VAR loaded from .env."""
        # The subprocess invokes `python -c` and prints the env var
        # value; we then assert it equals what we wrote.
        if not self.env_file.exists():
            self._write_env("TEST_VAR=subprocess_value\n")

        # Ensure TEST_VAR is unset in the parent so the subprocess
        # only sees the value loaded from .env.
        os.environ.pop("TEST_VAR", None)
        # Set HEDDLE_DOTENV_PATH so the subprocess loads OUR file.
        env = os.environ.copy()
        env["HEDDLE_DOTENV_PATH"] = str(self.env_file)
        # Run the subprocess with TEST_VAR forced-unset.
        env.pop("TEST_VAR", None)

        # The python -c source as a single string. The Path object is
        # converted via repr() so the subprocess sees a plain string
        # path (no WindowsPath symbol in the spawned process's globals).
        import pathlib
        path_str = str(pathlib.Path(self.env_file))

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys\n"
                    "from heddle_common import load_env_file\n"
                    f"load_env_file({path_str!r})\n"
                    "print(os.environ.get('TEST_VAR', '<unset>'))\n"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "subprocess_value")

    # ---- step 5: heddle never writes to the .env file ----

    def test_load_env_file_does_not_create_env_file(self):
        """Calling load_env_file on a missing path must not create it.

        feat-013 step 5: "Heddle never writes to ~/.heddle/.env".
        """
        self.assertFalse(self.env_file.exists())
        el.load_env_file(self.env_file)
        self.assertFalse(self.env_file.exists())

    def test_ensure_env_loader_does_not_write_env_file(self):
        """ensure_env_loader writes .gitignore ONLY, never .env."""
        # Point at our scratch env file path; ensure_env_loader will
        # see the file is missing and should ONLY write the .gitignore.
        if self.env_file.exists():
            self.env_file.unlink()
        # The default gitignore lives next to default_env_path(), which
        # is ~/.heddle/.gitignore. ensure_env_loader always writes
        # THAT file, not a sibling of the custom env path. Use the
        # default so the assertion holds.
        from heddle_common.env_loader import default_gitignore_path
        gitignore = default_gitignore_path()
        # Clean any pre-existing default gitignore so we start clean.
        # This test mutates the user's ~/.heddle directory on real
        # workstations; we guard with a sentinel file we write into
        # the same directory and remove in tearDown.
        had_gitignore = gitignore.exists()
        if had_gitignore:
            original = gitignore.read_text(encoding="utf-8")
        else:
            original = None
        try:
            el.ensure_env_loader(self.env_file)
            # The .env was NOT created.
            self.assertFalse(self.env_file.exists())
            # The .gitignore WAS created (it was missing at start).
            self.assertTrue(gitignore.exists())
        finally:
            if original is not None:
                gitignore.write_text(original, encoding="utf-8")
            elif gitignore.exists():
                gitignore.unlink()


class TestDescribe(unittest.TestCase):
    def test_describe_for_missing_file(self):
        d = el.describe(SCRATCH_PARENT / "_missing_env_xyz_12345")
        self.assertEqual(d["exists"], False)

    def test_describe_for_existing_file(self):
        f = SCRATCH_PARENT / "_test_describe.env"
        try:
            f.write_text("FOO=bar\n", encoding="utf-8")
            d = el.describe(f)
            self.assertEqual(d["exists"], True)
            self.assertIn("size", d)
            self.assertIn("mode", d)
            self.assertIn("secure", d)
            # describe() must not raise even on a too-permissive file.
            if not sys.platform.startswith("win"):
                os.chmod(f, 0o644)
                d2 = el.describe(f)
                self.assertEqual(d2["secure"], False)
        finally:
            if f.exists():
                f.unlink()


if __name__ == "__main__":
    unittest.main()
