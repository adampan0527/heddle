# SPDX-License-Identifier: Apache-2.0
"""Table-driven tests for ``heddle_daemon.intent.classify_intent``.

Covers the v0.1 heuristic matrix:

    * Imperative verbs at the start → work
    * ``feat-XXX`` / ``temp-XXX`` mention → work
    * Leading ``@`` → work
    * Plain question → chat
    * Greeting → chat
    * Empty / whitespace-only → chat (defensive)

Each test is a row of (message, expected_intent) so a future
contributor adding a verb or refining a regex only has to add a
row to the table — no test-method proliferation.

Per CODE_STYLE.md Part 2: pure pytest-style `def test_*` is fine.
stdlib unittest is used so the daemon tests stay aligned with
``test_routes_smoke.py``.
"""

from __future__ import annotations

import unittest

from heddle_daemon.intent import (
    FEATURE_ID_RE,
    Intent,
    LEADING_AT_RE,
    WORK_VERBS,
    classify_intent,
    is_work_message,
)


class TestClassifyIntentTable(unittest.TestCase):
    """Table-driven coverage of the v0.1 heuristic."""

    # Each row: (message, expected_intent, human-readable label).
    # Adding a new branch means adding a row, not a new method.
    ROWS: tuple[tuple[str, Intent, str], ...] = (
        # ---- work: imperative verbs ----
        ("add OAuth login", "work", "imperative verb: add"),
        ("Add OAuth login", "work", "imperative verb: Add (case-insensitive)"),
        ("Add,", "work", "imperative verb: trailing comma"),
        ("Fix.", "work", "imperative verb: trailing period"),
        ("Fix:", "work", "imperative verb: trailing colon"),
        ("Fix; this bug", "work", "imperative verb: trailing semicolon"),
        ("Implement drag-and-drop", "work", "imperative verb: implement"),
        ("Create a new endpoint", "work", "imperative verb: create"),
        ("Fix the login bug", "work", "imperative verb: fix"),
        ("Build the dashboard", "work", "imperative verb: build"),
        ("Refactor the parser", "work", "imperative verb: refactor"),
        ("Remove the old endpoint", "work", "imperative verb: remove"),
        ("Delete unused files", "work", "imperative verb: delete"),
        ("Update the README", "work", "imperative verb: update"),
        ("Change the default port", "work", "imperative verb: change"),
        ("Write tests", "work", "imperative verb: write"),
        ("Make it faster", "work", "imperative verb: make"),
        # ---- work: feature-id mention anywhere ----
        ("please look at feat-007", "work", "feat-XXX mid-sentence"),
        ("feat-007 is broken", "work", "feat-XXX at start"),
        ("see feat-001 for context", "work", "feat-XXX at end"),
        ("(feat-001) needs work", "work", "feat-XXX in parens"),
        ("@feat-044", "work", "@feat-XXX mention"),
        ("temp-001 is wrong", "work", "temp-XXX (draft id) mention"),
        ("what does feat-007 do?", "work", "feat-XXX in question (still work)"),
        # ---- work: leading @ ----
        ("@", "work", "bare @"),
        ("@help", "work", "@word"),
        ("  @feat-007  ", "work", "@feat with surrounding whitespace"),
        # ---- chat: plain question / greeting / chitchat ----
        ("hi how are you", "chat", "greeting"),
        ("hello there", "chat", "greeting"),
        ("thanks!", "chat", "thanks"),
        ("how does this work?", "chat", "plain question"),
        ("can you explain the design?", "chat", "plain question (no imperative)"),
        ("please", "chat", "single word please (no verb)"),
        ("great work", "chat", "compliment (no imperative)"),
        ("I think we should think about it", "chat", "no imperative"),
        # ---- chat: defensive ----
        ("", "chat", "empty string"),
        ("   ", "chat", "whitespace-only"),
        ("\t\n", "chat", "control whitespace"),
    )

    def test_classify_intent_table(self) -> None:
        for message, expected, label in self.ROWS:
            with self.subTest(label=label, message=message):
                self.assertEqual(
                    classify_intent(message),
                    expected,
                    f"classify_intent({message!r}) expected {expected!r}, "
                    f"got {classify_intent(message)!r} (label={label})",
                )

    def test_work_messages_round_trip_via_is_work_message(self) -> None:
        for message, expected, _label in self.ROWS:
            with self.subTest(message=message):
                self.assertEqual(
                    is_work_message(message),
                    expected == "work",
                )


class TestClassifyIntentNonString(unittest.TestCase):
    """Non-string inputs must not crash — defensive classification."""

    def test_none_is_chat(self) -> None:
        self.assertEqual(classify_intent(None), "chat")  # type: ignore[arg-type]

    def test_int_is_chat(self) -> None:
        self.assertEqual(classify_intent(42), "chat")  # type: ignore[arg-type]


class TestRegexes(unittest.TestCase):
    """Lock down the public regex constants so a regression is loud."""

    def test_feature_id_re_matches_known_ids(self) -> None:
        for s in ("feat-001", "feat-007", "feat-044", "temp-001", "temp-999"):
            with self.subTest(s=s):
                self.assertIsNotNone(FEATURE_ID_RE.search(s))

    def test_feature_id_re_rejects_fake_hits(self) -> None:
        # No letter/digit immediately adjacent.
        for s in (
            "feat-001widget",  # no boundary after
            "myfeat-001",  # no boundary before
            "feat-abc",  # not digits
            "Feature-001",  # uppercase F
        ):
            with self.subTest(s=s):
                self.assertIsNone(FEATURE_ID_RE.search(s))

    def test_leading_at_re_matches_with_whitespace(self) -> None:
        self.assertIsNotNone(LEADING_AT_RE.match("@foo"))
        self.assertIsNotNone(LEADING_AT_RE.match("  @foo"))

    def test_leading_at_re_rejects_mid_word(self) -> None:
        self.assertIsNone(LEADING_AT_RE.match("user@example.com"))


class TestWorkVerbsConstant(unittest.TestCase):
    """The closed verb set is part of the public contract."""

    def test_work_verbs_are_lowercase(self) -> None:
        for verb in WORK_VERBS:
            self.assertEqual(verb, verb.lower(), f"{verb!r} must be lowercase")

    def test_work_verbs_minimum_set(self) -> None:
        # The spec calls these out explicitly (feat-044 description).
        # A regression that removes one would silently change the
        # classifier's behavior — fail loud.
        for verb in ("add", "create", "implement", "fix"):
            self.assertIn(verb, WORK_VERBS)


if __name__ == "__main__":
    unittest.main()