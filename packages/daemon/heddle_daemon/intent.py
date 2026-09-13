# SPDX-License-Identifier: Apache-2.0
"""Intent classifier for user dialog input (D-017 / feat-044).

Pure-function heuristic that decides whether a single user message
should be treated as a plain conversational reply ("chat") or as a
request that should generate draft cards ("work").

v0.1 design (LLM-driven classification is post-v0.1):

    * Deterministic — no LLM call, no I/O, no clock. The function
      takes only the message text and returns ``"chat"`` or
      ``"work"``.
    * Heuristic — looks for:
        1. An ``@feat-XXX`` / ``@temp-XXX`` mention (the Web UI's
           feat-039 autocomplete inserts one in front of every
           explicit work item).
        2. A leading ``@`` of any kind (lets users type ``@`` first
           and decide later; @ always means work in v0.1).
        3. A feature id anywhere in the message (``feat-001``,
           ``feat-007``, etc.).
        4. An imperative verb at the start of the message —
           "add", "create", "implement", "fix", "build",
           "refactor", "remove", "delete", "update", "change",
           "write", "make". The list is closed; extending it is a
           deliberate decision, not a regex-everything catch-all.
    * Default — anything that matches none of the above is "chat".

The classifier is intentionally conservative: false positives (a
greeting classified as work) are worse UX than false negatives (a
genuine work request classified as chat) because a false negative
just bounces the user back to the dialog with a friendly reply,
while a false positive could insert draft cards the user did not
ask for. If a real-world message is borderline, classify it as
"chat" and let the user re-state.

Wire shape (used by ``heddle_daemon.routes._dialog_turn``):

    response = {
        "project_id": ...,
        "kind": "chat" | "work",
        "text": "<friendly reply>"             # only when kind == "chat"
        # "drafts": [...]                      # feat-045 fills this in
    }
"""

from __future__ import annotations

import re
from typing import Final, Literal

# Public type alias for the classifier's return value. Used by the
# routes handler to pick a response shape and by the tests to assert
# against the closed vocabulary. Keeping it as a Literal means a
# future caller that wants to add a third value has to edit this
# module rather than silently widening the wire shape.
Intent = Literal["chat", "work"]

# Public constants — tests assert against the exact closed set of
# verbs / pattern so an inadvertent change to the classifier's
# behavior is caught by the table-driven suite, not by an end-user
# noticing a regression in the dialog.
WORK_VERBS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "build",
        "change",
        "create",
        "delete",
        "fix",
        "implement",
        "make",
        "refactor",
        "remove",
        "update",
        "write",
    }
)

# Regex for an explicit feature-id mention anywhere in the message.
# Matches ``feat-001`` / ``feat-007`` (the project's feature-id
# convention) AND ``temp-001`` (feat-040's draft-card id convention,
# in case a user pastes a temp-XXX id back into the dialog).
# Anchored to a non-letter on either side so we don't accidentally
# match ``feat-001`` inside ``myfeat-001widget``; ``feat-001`` alone
# is fine, as is ``@feat-001`` and ``(feat-001)``.
FEATURE_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])(?:feat|temp)-\d+(?![A-Za-z0-9])"
)

# Leading-@ detection — the Web UI's @-autocomplete (feat-039) lets
# users type ``@`` first and the autocomplete menu lets them pick a
# target. The first non-whitespace character being ``@`` means "the
# user is intentionally invoking a structured command".
LEADING_AT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*@")

__all__ = [
    "FEATURE_ID_RE",
    "Intent",
    "LEADING_AT_RE",
    "WORK_VERBS",
    "classify_intent",
    "is_work_message",
]


def classify_intent(message: str) -> Intent:
    """Return ``"work"`` if the message looks like a feature request,
    otherwise ``"chat"``.

    Pure function: takes the message text, returns the closed-set
    Literal. Order of checks (cheap → expensive, common → rare):

        1. Whitespace-stripped message is empty → ``"chat"``.
           (Caller should have already rejected empties at the
           routes layer; defensive here so unit tests can exercise
           the branch without rebuilding the routes payload.)
        2. Leading ``@`` → ``"work"`` (cheapest regex).
        3. A ``feat-XXX`` / ``temp-XXX`` mention → ``"work"``.
        4. First whitespace-separated token (lowercased) is a known
           imperative verb → ``"work"``.
        5. Otherwise → ``"chat"``.

    The function does NOT call an LLM (v0.1 heuristic only); the
    function does NOT look at conversation history; the function
    does NOT inspect the project or feature_list. v0.2 may swap
    the heuristic for an LLM-driven classifier (D-017 notes
    "LLM-driven classification is post-v0.1").
    """
    if not isinstance(message, str):
        # Defensive: any non-string input (None, int, etc.) is
        # treated as chat so the caller does not crash. The routes
        # layer rejects non-string ``message`` upstream with a
        # ``RoutesError``; this branch only fires if a future
        # caller forgets that guard.
        return "chat"
    stripped = message.strip()
    if not stripped:
        return "chat"
    if LEADING_AT_RE.match(message):
        return "work"
    if FEATURE_ID_RE.search(message):
        return "work"
    first_token = stripped.split(maxsplit=1)[0].lower()
    # Strip trailing punctuation (e.g. "Add," "Fix.") so a polite
    # comma or period doesn't push the verb out of the closed set.
    first_token = first_token.rstrip(",.;:!?")
    if first_token in WORK_VERBS:
        return "work"
    return "chat"


def is_work_message(message: str) -> bool:
    """Convenience predicate — ``True`` iff ``classify_intent`` is ``"work"``.

    Saves callers from spelling out the equality check. Not part of
    the wire surface; purely a sugar layer for the routes handler
    and the tests.
    """
    return classify_intent(message) == "work"