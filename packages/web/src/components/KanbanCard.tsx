// SPDX-License-Identifier: Apache-2.0
/**
 * Single draggable kanban card — feat-035 / feat-037.
 *
 * Renders one feature row inside a column. Uses `useDraggable` so the
 * card is the drag *source*; the column owns the droppable target.
 *
 * Visual treatment per spec:
 *
 *   - bugfix cards:  orange left-edge stripe (`border-l-4 border-orange-500`)
 *                    + 🔧 wrench glyph rendered in the top-right region
 *                    of the header row, immediately before the status badge.
 *   - feature /     default styling (no left-edge stripe, no kind icon).
 *     enhancement:
 *   - blocked:      🔒 lock + "@<first-dep>" handle.
 *   - deferred:     ⏰ clock + "until YYYY-MM-DD" from `deferred_until`.
 *   - passing:      ✓ check + "completed YYYY-MM-DD" from the LAST
 *                   `attempts[].outcome === "passing"` entry's `.at`.
 *   - pending /     no status extras.
 *     in_progress:
 *
 * The cosmetic logic is split into two pure mappers — `kindDisplay`
 * and `statusExtras` — so the rules are tested without rendering
 * (see KanbanCard.test.tsx).
 *
 * While dragging: 50% opacity + slight rotation, so the source slot
 * remains visible but the dragged ghost reads as "in motion".
 */

import { useDraggable } from "@dnd-kit/core";
import type { Feature, FeatureStatus } from "@heddle/shared";

import { useKanbanStore } from "../lib/state/kanban-store.ts";

interface KanbanCardProps {
  feature: Feature;
}

/**
 * Status badge color map — lifted onto the cream theme. Tones follow
 * VISUAL_DESIGN § colors (semantic tokens):
 *   - pending / deferred → neutral bone surface
 *   - in_progress       → primary brand orange (in motion)
 *   - blocked           → amber warning
 *   - passing           → emerald success
 */
const STATUS_BADGE: Record<FeatureStatus, string> = {
  pending: "bg-surface-bone text-charcoal",
  in_progress: "bg-primary/10 text-primary",
  blocked: "bg-amber-50 text-amber-900",
  deferred: "bg-surface-bone text-mute",
  passing: "bg-emerald-50 text-emerald-900",
};

// --- kind display (Task 1) ---

interface KindDisplay {
  /** Short glyph (emoji) shown for this kind. Empty = no icon. */
  icon: string;
  /** Tailwind text-color class for the icon (empty = inherit). */
  accent: string;
  /** Extra Tailwind classes applied to the root <li>. Empty = none. */
  leftBorder: string;
}

const KIND_DISPLAY: Record<string, KindDisplay> = {
  feature: { icon: "", accent: "", leftBorder: "" },
  bugfix: {
    icon: "\u{1F527}",
    accent: "text-primary",
    leftBorder: "border-l-4 border-primary",
  },
  enhancement: { icon: "", accent: "", leftBorder: "" },
};

const DEFAULT_KIND: KindDisplay = KIND_DISPLAY["feature"]!;

/**
 * Pure mapper: feature -> its `KindDisplay`. Unknown kinds fall back
 * to the feature default (no stripe, no icon).
 */
export function kindDisplay(feature: Feature): KindDisplay {
  return KIND_DISPLAY[feature.kind] ?? DEFAULT_KIND;
}

// --- status extras (Task 2) ---

interface StatusExtra {
  /** Short glyph (emoji) shown after the status badge. */
  icon: string;
  /** Resolver for the short text shown next to the icon. */
  resolver: (f: Feature) => string | null;
}

const NO_EXTRAS: StatusExtra = { icon: "", resolver: () => null };

const STATUS_EXTRAS: Record<FeatureStatus, StatusExtra> = {
  pending: NO_EXTRAS,
  in_progress: NO_EXTRAS,
  blocked: {
    icon: "\u{1F512}",
    resolver: blockedText,
  },
  deferred: {
    icon: "\u{23F0}",
    resolver: deferredText,
  },
  passing: {
    icon: "\u{2713}",
    resolver: passingText,
  },
};

/** ISO date slice length (YYYY-MM-DD). */
const ISO_DATE_LEN = 10;

function blockedText(f: Feature): string | null {
  if (f.depends_on.length === 0) return "@??? (no dep?)";
  return `@${f.depends_on[0]}`;
}

function deferredText(f: Feature): string | null {
  const d = f.deferred_until;
  if (!d) return null;
  return `until ${d.slice(0, ISO_DATE_LEN)}`;
}

function passingText(f: Feature): string | null {
  const last = [...f.attempts].reverse().find((a) => a.outcome === "passing");
  if (!last?.at) return null;
  return `completed ${last.at.slice(0, ISO_DATE_LEN)}`;
}

/**
 * Pure mapper: feature -> `{ icon, text } | null`. Returns null when
 * the status carries no extras (pending / in_progress) or when the
 * resolver returns null (defensive: deferred without a date, etc.).
 */
export function statusExtras(
  feature: Feature,
): { icon: string; text: string } | null {
  const spec = STATUS_EXTRAS[feature.status];
  const text = spec.resolver(feature);
  if (!text) return null;
  return { icon: spec.icon, text };
}

// --- render (Task 4) ---

export function KanbanCard({ feature }: KanbanCardProps): React.ReactElement {
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({ id: feature.id });
  const draggingId = useKanbanStore((s) => s.draggingId);
  // The store drives our own opacity transform (in case parent reorders
  // during drag); dnd-kit's `isDragging` covers the canonical case.
  const storeDragging = draggingId === feature.id;

  const style: React.CSSProperties | undefined = transform
    ? {
        transform: `translate3d(${transform.x}px, ${transform.y}px, 0) rotate(1deg)`,
        zIndex: 50,
      }
    : undefined;

  const k = kindDisplay(feature);
  const extras = statusExtras(feature);

  return (
    <li
      ref={setNodeRef}
      style={style}
      data-feature-id={feature.id}
      data-status={feature.status}
      data-kind={feature.kind}
      data-status-extras={extras ? "true" : "false"}
      className={`cursor-grab select-none rounded-md border border-hairline bg-surface-card p-3 text-sm text-ink shadow-sm transition-opacity ${
        k.leftBorder
      } ${isDragging || storeDragging ? "opacity-50" : "opacity-100"}`}
      {...attributes}
      {...listeners}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-code text-xs text-ash">{feature.id}</span>
        <span className="flex items-center gap-1">
          {k.icon ? (
            <span
              aria-label={`${feature.kind} kind icon`}
              className={`text-xs ${k.accent}`}
            >
              {k.icon}
            </span>
          ) : null}
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_BADGE[feature.status]}`}
          >
            {feature.status.replace("_", " ")}
          </span>
          {extras ? (
            <span className="ml-1 text-xs text-charcoal">
              {extras.icon} {extras.text}
            </span>
          ) : null}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm text-body">{feature.description}</p>
    </li>
  );
}