# SPDX-License-Identifier: Apache-2.0
"""Errors raised by the auto-decomposition module (feat-045).

Split out from ``decompose.py`` so callers can catch these errors
without importing the heavier loader chain.
"""


class DecomposeError(ValueError):
    """Raised when ``decompose.json`` is malformed or unreadable.

    Surfaces as an internal_error wire envelope from ``routes.py``;
    the dialog reply still includes a friendly text so the Web UI
    shows something useful.
    """


class EmptyMessageError(ValueError):
    """Raised when ``propose_drafts`` receives an empty message."""
