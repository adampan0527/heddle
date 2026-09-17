// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the kanban card visual treatment — feat-037.
 *
 * Two layers of coverage:
 *
 *   1. Pure-function tests for `kindDisplay` and `statusExtras`. These
 *      are the spec rules in tabular form: cheap, deterministic, and
 *      stable across dnd-kit / React upgrades.
 *
 *   2. Render tests that confirm:
 *      - bugfix cards carry `border-l-4 border-primary` AND a 🔧
 *        glyph in the header's top-right region.
 *      - feature / enhancement cards have NO left-edge stripe.
 *      - blocked / deferred / passing cards render their status extras
 *        (lock / clock / check + accompanying text).
 *      - pending / in_progress cards render NO status extras.
 *      - a bugfix card on the Done lane (feat-036 integration) still
 *        carries its orange stripe + wrench.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Feature } from "@heddle/shared";

import { Kanban } from "./Kanban.tsx";
import { kindDisplay, KanbanCard, statusExtras } from "./KanbanCard.tsx";

vi.mock("../lib/api/features.js", () => ({
  useFeatures: vi.fn(),
  useStartFeature: vi.fn(),
}));

import {
  useFeatures,
  useStartFeature,
} from "../lib/api/features.js";

const useFeaturesMock = vi.mocked(useFeatures);
const useStartFeatureMock = vi.mocked(useStartFeature);

function makeFeature(overrides: Partial<Feature>): Feature {
  return {
    id: "feat-001",
    category: "functional",
    description: "test feature",
    steps: [],
    status: "pending",
    priority: "high",
    depends_on: [],
    attempts: [],
    kind: "feature",
    fixes: null,
    enhances: null,
    superseded_by: null,
    implementation_model: null,
    deferred_until: null,
    ...overrides,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function setupApiMock(features: Feature[]) {
  useFeaturesMock.mockReturnValue({
    data: features,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useFeatures>);
  useStartFeatureMock.mockReturnValue({
    mutate: vi.fn(),
  } as unknown as ReturnType<typeof useStartFeature>);
}

// ---------- kindDisplay (Task 1) ----------

describe("kindDisplay", () => {
  test("feature kind returns the default (no stripe, no icon)", () => {
    expect(kindDisplay(makeFeature({ kind: "feature" }))).toEqual({
      icon: "",
      accent: "",
      leftBorder: "",
    });
  });

  test("bugfix kind returns orange stripe + wrench icon", () => {
    const k = kindDisplay(makeFeature({ kind: "bugfix" }));
    expect(k.leftBorder).toBe("border-l-4 border-primary");
    expect(k.icon).toBe("\u{1F527}"); // 🔧
    expect(k.accent).toBe("text-primary");
  });

  test("enhancement kind returns the default", () => {
    expect(kindDisplay(makeFeature({ kind: "enhancement" }))).toEqual({
      icon: "",
      accent: "",
      leftBorder: "",
    });
  });

  test("unknown kind falls back to the default", () => {
    expect(kindDisplay(makeFeature({ kind: "totally-new-kind" }))).toEqual({
      icon: "",
      accent: "",
      leftBorder: "",
    });
  });
});

// ---------- statusExtras (Task 2) ----------

describe("statusExtras", () => {
  test("pending returns null (no extras)", () => {
    expect(statusExtras(makeFeature({ status: "pending" }))).toBeNull();
  });

  test("in_progress returns null (no extras)", () => {
    expect(statusExtras(makeFeature({ status: "in_progress" }))).toBeNull();
  });

  test("blocked returns lock icon + first dep handle", () => {
    const card = makeFeature({
      status: "blocked",
      depends_on: ["feat-099"],
    });
    expect(statusExtras(card)).toEqual({
      icon: "\u{1F512}", // 🔒
      text: "@feat-099",
    });
  });

  test("blocked with empty depends_on is defensive", () => {
    const card = makeFeature({ status: "blocked", depends_on: [] });
    expect(statusExtras(card)).toEqual({
      icon: "\u{1F512}",
      text: "@??? (no dep?)",
    });
  });

  test("deferred returns clock icon + until date (YYYY-MM-DD slice)", () => {
    const card = makeFeature({
      status: "deferred",
      deferred_until: "2026-12-31",
    });
    expect(statusExtras(card)).toEqual({
      icon: "\u{23F0}", // ⏰
      text: "until 2026-12-31",
    });
  });

  test("deferred with ISO datetime string slices to YYYY-MM-DD", () => {
    const card = makeFeature({
      status: "deferred",
      deferred_until: "2026-12-31T00:00:00.000Z",
    });
    expect(statusExtras(card)).toEqual({
      icon: "\u{23F0}",
      text: "until 2026-12-31",
    });
  });

  test("deferred without deferred_until returns null defensively", () => {
    const card = makeFeature({
      status: "deferred",
      deferred_until: null,
    });
    expect(statusExtras(card)).toBeNull();
  });

  test("passing returns check + last passing-attempt date", () => {
    const card = makeFeature({
      status: "passing",
      attempts: [
        { outcome: "blocked", at: "2026-01-01" },
        { outcome: "passing", at: "2026-05-05" },
        { outcome: "passing", at: "2026-09-09" },
      ],
    });
    expect(statusExtras(card)).toEqual({
      icon: "\u{2713}", // ✓
      text: "completed 2026-09-09",
    });
  });

  test("passing without any passing attempt is null defensively", () => {
    const card = makeFeature({
      status: "passing",
      attempts: [{ outcome: "blocked", at: "2026-01-01" }],
    });
    expect(statusExtras(card)).toBeNull();
  });
});

// ---------- render (Task 4) ----------

describe("<KanbanCard /> render", () => {
  test("bugfix card has orange left-edge stripe and a wrench in the header", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-BF",
            kind: "bugfix",
            status: "pending",
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-BF").closest("[data-feature-id]");
    expect(card).not.toBeNull();
    expect(card!.tagName).toBe("LI");
    expect(card!.className).toContain("border-l-4");
    expect(card!.className).toContain("border-primary");
    expect(card).toHaveAttribute("data-kind", "bugfix");

    const header = card!.querySelector("div");
    expect(header).not.toBeNull();
    // Wrench glyph is in the header (top-right region of the card).
    expect(within(header!).getByText("\u{1F527}")).toBeInTheDocument();
  });

  test("feature card has no left-edge stripe and no kind icon", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-FE",
            kind: "feature",
            status: "pending",
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-FE").closest("[data-feature-id]");
    expect(card!.className).not.toContain("border-primary");
    expect(card).toHaveAttribute("data-kind", "feature");
    // No wrench glyph anywhere on the card.
    expect(card!.querySelector("div")!.textContent).not.toContain("\u{1F527}");
  });

  test("enhancement card has no left-edge stripe and no kind icon", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-EN",
            kind: "enhancement",
            status: "pending",
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-EN").closest("[data-feature-id]");
    expect(card!.className).not.toContain("border-primary");
    expect(card).toHaveAttribute("data-kind", "enhancement");
  });

  test("blocked card shows lock icon + @feat-XXX text", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-BL",
            status: "blocked",
            depends_on: ["feat-099"],
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-BL").closest("[data-feature-id]");
    expect(card).toHaveAttribute("data-status-extras", "true");
    expect(card!.textContent).toContain("\u{1F512}"); // lock
    expect(card!.textContent).toContain("@feat-099");
  });

  test("deferred card shows clock icon + until date", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-DF",
            status: "deferred",
            deferred_until: "2026-12-31",
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-DF").closest("[data-feature-id]");
    expect(card).toHaveAttribute("data-status-extras", "true");
    expect(card!.textContent).toContain("\u{23F0}"); // clock
    expect(card!.textContent).toContain("until 2026-12-31");
  });

  test("passing card shows check + completed date from attempts[]", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({
            id: "feat-PA",
            status: "passing",
            attempts: [{ outcome: "passing", at: "2026-09-09" }],
          })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-PA").closest("[data-feature-id]");
    expect(card).toHaveAttribute("data-status-extras", "true");
    expect(card!.textContent).toContain("\u{2713}"); // check
    expect(card!.textContent).toContain("completed 2026-09-09");
  });

  test("pending card has no status extras", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({ id: "feat-PE", status: "pending" })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-PE").closest("[data-feature-id]");
    expect(card).toHaveAttribute("data-status-extras", "false");
  });

  test("in_progress card has no status extras", () => {
    renderWithClient(
      <ul>
        <KanbanCard
          feature={makeFeature({ id: "feat-IP", status: "in_progress" })}
        />
      </ul>,
    );
    const card = screen.getByText("feat-IP").closest("[data-feature-id]");
    expect(card).toHaveAttribute("data-status-extras", "false");
  });

  // feat-036 integration: a bugfix card on the Done lane still gets the
  // orange stripe + wrench (the kind treatment is independent of the lane).
  test("bugfix card on the Done lane keeps orange stripe + wrench", () => {
    setupApiMock([
      makeFeature({
        id: "feat-BF-PASS",
        kind: "bugfix",
        status: "passing",
        attempts: [{ outcome: "passing", at: "2026-09-09" }],
      }),
    ]);
    renderWithClient(<Kanban projectId="proj-1" />);
    const card = screen.getByText("feat-BF-PASS").closest("[data-feature-id]");
    expect(card).not.toBeNull();
    expect(card!.className).toContain("border-primary");
    // Status extras still render (check + completed date).
    expect(card!.textContent).toContain("\u{2713}");
    expect(card!.textContent).toContain("completed 2026-09-09");
    // Wrench still in the header.
    const header = card!.querySelector("div");
    expect(within(header!).getByText("\u{1F527}")).toBeInTheDocument();
  });
});