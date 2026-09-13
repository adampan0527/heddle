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
  /**
   * feat-055 / D-053: per-project sandbox level, resolved from
   * `<project>/.heddle/config.yaml` by the daemon. Absent when the
   * daemon has never read the config (legacy v0.1 behavior). The UI
   * defaults missing values to `"full"` for backward compat.
   */
  sandbox_level?: SandboxLevel;
}

/**
 * feat-055 / D-053: per-project sandbox level. The daemon's
 * `ToolDispatchMiddleware` (feat-021) enforces these levels against
 * mutating tool calls. String-literal union so typos are caught at
 * compile time; the daemon validates the same set on the wire.
 */
export type SandboxLevel = "read-only" | "edit-with-confirm" | "full";

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
export type DialogKind = "chat" | "work" | "diagnose";

/**
 * Structured diagnose report returned by the LLM when the user invokes
 * `@feat-XXX diagnose` (feat-042 / D-035). The card UI renders these
 * three sections verbatim; `diff` is optional and only present when
 * the LLM has a concrete edit to propose.
 *
 * v0.1 caveat: the daemon does not yet implement diagnose; the dialog
 * synthesizes a hard-coded example so the card is testable today.
 * feat-043 will replace the mock with a real `classify_intent()`
 * handler that returns `kind: "diagnose"` with a real payload.
 */
export interface DiagnoseResponse {
  /** Plain-language cause for the failure (always shown). */
  cause: string;
  /** Suggested next action — usually a hint to feed back into retry. */
  suggestion: string;
  /** Optional proposed edit. When present, the card renders a
   *  before/after diff with an "Apply" button (feat-043 wires the
   *  Apply handler to retry-with-hint). */
  diff?: string;
}

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
  /** Structured diagnosis payload — present when `kind === "diagnose"`. */
  diagnosis?: DiagnoseResponse;
}

/** Body of POST /api/projects/:id/features/:fid/transition. */
export type TransitionAction = "retry" | "abandon" | "mark-done";

/** Diff payload returned by destructive feat-054 ops (D-054). */
export interface FeatureDiff {
  operation: "split" | "merge" | "edit" | "deps";
  changes: unknown[];
  [extra: string]: unknown;
}

/** Body of POST /api/projects/:id/features/:fid/split. */
export interface SplitFeatureBody {
  featureId: string;
  new_features: SplitChild[];
}
export interface SplitChild {
  id?: string | null;
  title: string;
  description?: string;
  steps?: string[];
  depends_on?: string[];
  category?: string;
  priority?: "high" | "medium" | "low";
  kind?: FeatureKind;
}
export interface SplitFeatureData {
  project_id: string;
  feature_id: string;
  source: Feature;
  created: Feature[];
  diff: FeatureDiff;
}

/** Body of POST /api/projects/:id/features/merge. */
export interface MergeFeatureBody {
  source_ids: string[];
  target: SplitChild & { id: string };
}
export interface MergeFeatureData {
  project_id: string;
  sources: Feature[];
  created: Feature;
  diff: FeatureDiff;
}

/** Body of PATCH /api/projects/:id/features/:fid (feat-054 edit). */
export interface EditFeatureBody {
  featureId: string;
  title?: string;
  description?: string;
  steps?: string[];
  category?: string;
}
export interface EditFeatureData {
  project_id: string;
  feature_id: string;
  feature: Feature;
  diff: FeatureDiff;
}

/** Body of PATCH /api/projects/:id/features/:fid/priority. */
export interface ReprioritizeFeatureBody {
  featureId: string;
  priority: "high" | "medium" | "low";
}
export interface ReprioritizeFeatureData {
  project_id: string;
  feature_id: string;
  feature: Feature;
  diff: { before: { priority: string }; after: { priority: string }; changes: string[] };
}

/** Body of POST /api/projects/:id/features/:fid/deps. */
export interface UpdateDepsBody {
  featureId: string;
  add?: string[];
  remove?: string[];
}
export interface UpdateDepsData {
  project_id: string;
  feature_id: string;
  feature: Feature;
  diff: {
    before: string[];
    after: string[];
    added: string[];
    removed: string[];
  };
}

/** Body of POST /api/projects (add project). */
export interface AddProjectBody {
  path: string;
  name?: string | null;
}
