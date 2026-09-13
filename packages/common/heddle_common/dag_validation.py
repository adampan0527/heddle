# SPDX-License-Identifier: Apache-2.0
"""DAG satisfiability validation for draft cards (feat-046).

When the user confirms draft cards in the Web UI's draft tray
(feat-040), the daemon (feat-028 routes) must:

  1. Check that every draft's ``depends_on`` references either an
     existing feature (already in ``feature_list.json``) or another
     draft being confirmed together. Any reference to a feature that
     does not exist is rejected with a clear error message.
  2. Detect cycles in the combined DAG (existing features + drafts
     being confirmed). A cycle means the work graph can never make
     forward progress; the whole batch is rejected.
  3. For each draft, compute the initial status from the DAG
     topology: ``pending`` if every dep is an existing ``passing``
     feature, otherwise ``blocked``.

This module is the pure-function heart of that flow (D-003 / D-004 /
D-027). It takes already-parsed inputs (draft dicts + feature dicts)
and returns the proposed-feature payload to write back plus any
error messages. No I/O, no schema_version awareness — those
concerns live in ``feature_list_io`` and ``routes``.

Cycle detection uses Kahn's algorithm (BFS on in-degree-zero nodes)
because it is iterative and never recurses — a depth-first search
on a deep dependency graph could blow the Python stack
(``RecursionError``). Kahn's algorithm has the same O(V+E)
complexity and is safer for v0.1's typical feature graph depth
(usually < 20).
"""

from __future__ import annotations

from typing import Any, Final

# Public error prefix used by both the test suite and the route
# layer when surfacing unknown-dependency errors to the Web UI.
# Stable text: the route layer greps this for diagnostic routing.
UNKNOWN_DEP_PREFIX: Final[str] = "draft"

# Status values assigned by validate_drafts to its output features.
STATUS_PENDING: Final[str] = "pending"
STATUS_BLOCKED: Final[str] = "blocked"

# Per-draft ``priority`` default when the draft omits one. Mirrors
# ``feature_list_io.add`` so the on-disk JSON is uniform.
DEFAULT_PRIORITY: Final[str] = "medium"

# Per-draft ``category`` default. Mirrors ``feature_list_io.add``.
DEFAULT_CATEGORY: Final[str] = "functional"


def _draft_id(draft: dict[str, Any]) -> str:
    """Return the draft's id or raise KeyError for the route layer to catch.

    A missing ``id`` is a programming bug (the v0.1 contract requires
    every draft to carry one), so we do not invent a fallback id —
    surfacing the KeyError keeps the bug visible rather than masking
    it with a silent rename.
    """
    return draft["id"]


def _normalize_depends_on(deps: Any) -> list[str]:
    """Coerce a draft's ``depends_on`` into a clean list of strings.

    Accepts tuple, list, or comma-separated string. Dedups while
    preserving order so the error messages name each unknown dep
    exactly once.
    """
    if deps is None:
        return []
    if isinstance(deps, str):
        raw = [d.strip() for d in deps.split(",") if d.strip()]
    else:
        raw = [str(d).strip() for d in deps if str(d).strip()]
    seen: set[str] = set()
    out: list[str] = []
    for d in raw:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _normalize_steps(steps: Any) -> list[str]:
    """Coerce ``steps`` into a list of non-empty strings.

    Missing / None / non-list types fall back to an empty list so
    the caller still gets a usable feature row.
    """
    if steps is None:
        return []
    if isinstance(steps, str):
        return [steps] if steps.strip() else []
    if not isinstance(steps, (list, tuple)):
        return []
    return [str(s) for s in steps if isinstance(s, str) and s.strip()]


def _index_existing(
    existing_features: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build id -> row map of every existing feature.

    Later rows win on duplicate ids (same policy as
    ``feature_list_io._find_feature`` which returns the first hit);
    duplicates are a malformed-feature-list condition that the
    upstream ``load()`` does not catch. We do not refuse here — the
    dag validation only needs *some* row for each id.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for feat in existing_features:
        if isinstance(feat, dict):
            fid = feat.get("id")
            if isinstance(fid, str) and fid:
                by_id[fid] = feat
    return by_id


def _check_deps_resolve(
    drafts: list[dict[str, Any]],
    existing_by_id: dict[str, dict[str, Any]],
    draft_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Verify every draft's ``depends_on`` is either existing or co-confirmed.

    Returns a list of error messages; empty list means OK. Self-deps
    (a draft depending on its own id) are reported here so the user
    sees the specific reason rather than a generic cycle message
    later in Kahn's run. Non-dict entries (which the outer loop
    already filtered out of ``draft_by_id``) are skipped here too
    so a malformed entry cannot crash the validator.
    """
    errors: list[str] = []
    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        did = _draft_id(draft)
        for dep in _normalize_depends_on(draft.get("depends_on")):
            if dep == did:
                errors.append(
                    f"{UNKNOWN_DEP_PREFIX} {did} depends on itself"
                )
                continue
            if dep in existing_by_id:
                continue
            if dep in draft_by_id:
                continue
            errors.append(
                f"{UNKNOWN_DEP_PREFIX} {did} depends on {dep} "
                f"which doesn't exist"
            )
    return errors


def _detect_cycle(
    drafts: list[dict[str, Any]],
    existing_by_id: dict[str, dict[str, Any]],
    draft_by_id: dict[str, dict[str, Any]],
) -> str | None:
    """Run Kahn's algorithm on the union graph; return cycle message or None.

    Nodes are feature ids. Edges go from "dep" -> "dependent" so a
    topological order means "do deps first". In Kahn's algorithm a
    cycle exists when some nodes never reach in-degree zero.

    The cycle message names the smallest set of node ids that
    participate in any remaining cycle so the user has something
    actionable (rather than "a cycle exists" with no pointer).
    """
    nodes: set[str] = set(existing_by_id.keys())
    nodes.update(draft_by_id.keys())
    indeg: dict[str, int] = {n: 0 for n in nodes}
    # Edge set to keep Kahn's run linear even when the union graph
    # has duplicate edges (e.g. two drafts depending on the same
    # existing feature).
    outgoing: dict[str, set[str]] = {n: set() for n in nodes}

    def _add_edge(src: str, dst: str) -> None:
        if dst not in outgoing[src]:
            outgoing[src].add(dst)
            indeg[dst] += 1

    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        did = _draft_id(draft)
        for dep in _normalize_depends_on(draft.get("depends_on")):
            if dep in nodes and dep != did:
                _add_edge(dep, did)
    # Existing features can also carry depends_on that resolve to
    # drafts being confirmed together; include those edges so the
    # cycle check is "whole DAG, not just the new draft slice".
    for feat in existing_by_id.values():
        fid = feat.get("id")
        if not isinstance(fid, str) or not fid:
            continue
        for dep in _normalize_depends_on(feat.get("depends_on")):
            if dep in nodes and dep != fid:
                _add_edge(dep, fid)

    queue: list[str] = [n for n, d in indeg.items() if d == 0]
    visited = 0
    head = 0
    while head < len(queue):
        n = queue[head]
        head += 1
        visited += 1
        for m in outgoing[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if visited == len(nodes):
        return None
    # Cycle remains — collect the offending nodes (any node that
    # never reached in-degree zero). Stable ordering for tests.
    leftover = sorted(n for n, d in indeg.items() if d > 0)
    return f"cycle detected: {' -> '.join(leftover)}"


def _build_proposed_feature(
    draft: dict[str, Any],
    *,
    existing_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Convert one draft into a feature-row dict ready for feature_list_io.add.

    Status is decided by DAG topology (D-003): ``pending`` only when
    every dep is an existing feature whose status is ``passing``.
    Otherwise the new feature is born ``blocked`` (D-004) so the
    kanban shows the work dependency clearly. Drafts depending on
    other drafts being confirmed together are always born
    ``blocked`` because the sibling drafts are not yet on disk
    during this call (the helper that consumes this output writes
    them in order, but at the moment of decision they have no
    status to be "passing").
    """
    did = _draft_id(draft)
    raw_deps = _normalize_depends_on(draft.get("depends_on"))
    all_deps_passing = True
    for dep in raw_deps:
        existing = existing_by_id.get(dep)
        if existing is None:
            # Depends on a sibling draft; not yet on disk so we cannot
            # claim "all deps passing". This is a feature-only signal
            # — the sibling will land in the same batch.
            all_deps_passing = False
            break
        if existing.get("status") != "passing":
            all_deps_passing = False
            break
    status = STATUS_PENDING if all_deps_passing else STATUS_BLOCKED
    return {
        "id": did,
        "category": draft.get("category") or DEFAULT_CATEGORY,
        "description": str(draft.get("description") or ""),
        "steps": _normalize_steps(draft.get("steps")),
        "status": status,
        "priority": draft.get("priority") or DEFAULT_PRIORITY,
        "depends_on": raw_deps,
        "kind": draft.get("kind") or "feature",
    }


def validate_drafts(
    drafts: list[dict[str, Any]],
    existing_features: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a batch of draft cards and decide their initial status.

    Parameters
    ----------
    drafts:
        The list of draft dicts the user just confirmed. Each must
        carry ``id``; ``depends_on`` / ``steps`` / ``description`` /
        ``priority`` / ``category`` / ``kind`` are optional.
    existing_features:
        The current ``feature_list.json`` features array — used
        both to resolve ``depends_on`` references and to compute
        initial status from DAG topology.

    Returns
    -------
    (proposed, errors)
        ``proposed`` is a list of feature-row dicts ready for
        ``feature_list_io.add``. ``errors`` is a list of
        human-readable rejection messages; when non-empty the
        caller MUST treat the batch as rejected and NOT write
        anything to ``feature_list.json``.
    """
    if not isinstance(drafts, list):
        raise TypeError("drafts must be a list")
    if not isinstance(existing_features, list):
        raise TypeError("existing_features must be a list")

    # Duplicate-id detection is part of validation; the wire shape
    # should not let two drafts share an id but the daemon should
    # never silently accept it either.
    seen_ids: set[str] = set()
    dup_ids: list[str] = []
    draft_by_id: dict[str, dict[str, Any]] = {}
    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        try:
            did = _draft_id(draft)
        except KeyError:
            continue
        if did in seen_ids:
            dup_ids.append(did)
            continue
        seen_ids.add(did)
        draft_by_id[did] = draft

    errors: list[str] = []
    for dup in dup_ids:
        errors.append(
            f"{UNKNOWN_DEP_PREFIX} {dup} appears more than once in "
            f"the confirm batch"
        )

    existing_by_id = _index_existing(existing_features)
    errors.extend(_check_deps_resolve(drafts, existing_by_id, draft_by_id))
    if errors:
        # Don't bother running cycle detection when deps already fail;
        # the user has actionable per-draft errors to fix first.
        return [], errors

    cycle_msg = _detect_cycle(drafts, existing_by_id, draft_by_id)
    if cycle_msg is not None:
        return [], [cycle_msg]

    proposed = [
        _build_proposed_feature(d, existing_by_id=existing_by_id)
        for d in drafts
        if isinstance(d, dict)
    ]
    return proposed, []
