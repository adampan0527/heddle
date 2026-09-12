// SPDX-License-Identifier: Apache-2.0
/**
 * Domain types shared between server and browser. Each shape is a
 * verbatim port of the TypeBox schemas in
 * `packages/node/src/routes/{projects,features,dialog}.ts` so a daemon
 * schema change has to land here too.
 *
 * Date strings are ISO-8601 (Python `datetime.isoformat()` outputs).
 * Field names are snake_case to match the wire JSON exactly.
 */

/** A registered project (one row in ~/.heddle/projects.json). */
export interface Project {
  id: string;
  name: string;
  path: string;
  added_at: string;
  last_accessed_at: string;
}

/** Card kind discriminator (feat-010). */
export type FeatureKind = "feature" | "bugfix" | "enhancement";

/**
 * The 5-state machine status values from `feature_list_io`. Defined
 * here as a string-literal union so TypeScript catches typos; the
 * Python side validates the same set in `heddle_common`.
 */
export type FeatureStatus =
  | "pending"
  | "in_progress"
  | "blocked"
  | "deferred"
  | "passing";

/**
 * One attempt record on a feature's history (`attempts[]` field). The
 * shape is intentionally loose — each attempt entry has free-form
 * fields (session number, by, outcome, note, at). The browser only
 * renders a few of them, so we keep the type permissive.
 */
export interface FeatureAttempt {
  session?: number | null;
  by?: string;
  outcome?: "blocked" | "deferred" | "passing" | "regressed";
  note?: string;
  at?: string;
}

/** A full feature row from GET /api/projects/:id/features. */
export interface Feature {
  id: string;
  category: string;
  description: string;
  steps: string[];
  status: FeatureStatus;
  priority: "high" | "medium" | "low" | string;
  depends_on: string[];
  attempts: FeatureAttempt[];
  kind: FeatureKind | string;
  fixes: string | null;
  enhances: string | null;
  superseded_by: string | null;
  implementation_model: string | null;
  /** ISO date (YYYY-MM-DD). Set by `mark-deferred`; cleared on any
   *  other status transition. Optional on the wire so old daemons
   *  still load (feat-016). */
  deferred_until?: string | null;
}

/** Dialog response kind from POST /api/projects/:id/dialog. */
export type DialogKind = "chat" | "work";

/**
 * A draft card returned by the LLM when `kind === "work"`. Becomes a
 * real feature after the user clicks "Confirm all" (feat-040).
 */
export interface DraftCard {
  /** Temporary id like "temp-001". Replaced with "feat-XXX" on confirm. */
  id: string;
  title: string;
  description: string;
  steps: string[];
  depends_on: string[];
  kind: FeatureKind;
}

/** Body of POST /api/projects/:id/dialog. */
export interface DialogResponse {
  project_id: string;
  kind: DialogKind;
  text: string;
  drafts?: DraftCard[];
}

/** Body of POST /api/projects/:id/features/:fid/transition. */
export type TransitionAction = "retry" | "abandon" | "mark-done";

/** Body of POST /api/projects (add project). */
export interface AddProjectBody {
  path: string;
  name?: string | null;
}
