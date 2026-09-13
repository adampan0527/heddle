// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the draft cards tray — feat-040.
 *
 * Four layers of coverage:
 *
 *   1. **Render**: tray is hidden when no drafts; one row per draft
 *      when present; checkbox reflects `selectedIds` from the store.
 *   2. **Selection toggle**: clicking the checkbox adds/removes the
 *      id from `selectedIds`.
 *   3. **Confirm-all button**: disabled when 0 kept, enabled when at
 *      least one kept; click calls `useConfirmDrafts` with the kept
 *      drafts and clears the store on success.
 *   4. **Cancel**: clicking the cancel button clears the store (and
 *      therefore hides the tray).
 *
 * The ui-store is real (Zustand) but uses a fresh `localStorage`
 * shim per test so persisted state never leaks between cases. The
 * `useConfirmDrafts` hook is mocked because TanStack Query's
 * `mutateAsync` would otherwise need a QueryClientProvider wrapper.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { DraftCard } from "@heddle/shared";

import { DraftTray } from "./DraftTray.tsx";
import { useUiStore } from "../lib/state/ui-store.ts";

vi.mock("../lib/api/features.js", () => ({
  useConfirmDrafts: vi.fn(),
}));

import { useConfirmDrafts } from "../lib/api/features.js";

const useConfirmDraftsMock = vi.mocked(useConfirmDrafts);

interface MockConfirmApi {
  mutate: ReturnType<typeof vi.fn>;
}

function setupConfirmMock(opts: {
  onMutate?: (body: { drafts: DraftCard[] }) => void;
}): MockConfirmApi {
  const mutate = vi.fn((body, config) => {
    if (opts.onMutate) opts.onMutate(body);
    // Simulate the onSuccess side-effect the component relies on.
    if (config && typeof config === "object" && config.onSuccess) {
      config.onSuccess({ project_id: "proj-1", feature_ids: ["feat-101"] });
    }
    return undefined;
  });
  useConfirmDraftsMock.mockReturnValue({
    mutate,
    isPending: false,
    // Fill the rest of the UseMutationResult shape with type-correct
    // stubs so the hook typechecks.
    mutateAsync: vi.fn(),
    reset: vi.fn(),
    variables: undefined,
    context: undefined,
    data: undefined,
    error: null,
    failureCount: 0,
    failureReason: null,
    isError: false,
    isIdle: true,
    isPaused: false,
    isSuccess: false,
    status: "idle",
    submittedAt: 0,
  } as unknown as ReturnType<typeof useConfirmDrafts>);
  return { mutate };
}

function makeDraft(overrides: Partial<DraftCard>): DraftCard {
  return {
    id: "temp-001",
    title: "OAuth login",
    description: "Allow users to sign in with Google and GitHub.",
    steps: ["Wire Google OAuth", "Wire GitHub OAuth", "Test"],
    depends_on: [],
    kind: "feature",
    ...overrides,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Reset the store and the localStorage shim between tests so state
// never bleeds across cases (zustand's persist middleware keeps the
// store hydrated from localStorage).
beforeEach(() => {
  // Wipe any previous localStorage state from earlier suites.
  if (typeof window !== "undefined" && window.localStorage) {
    window.localStorage.clear();
  }
  // Reset the zustand store to its defaults before each test.
  useUiStore.setState({
    activeProjectId: null,
    drafts: {},
    selectedIds: [],
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("<DraftTray /> rendering", () => {
  test("renders nothing when there are no drafts", () => {
    setupConfirmMock({});
    const { container } = renderWithClient(
      <DraftTray projectId="proj-1" />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders one row per draft with temp id, title, and description", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: {
        "temp-001": makeDraft({ id: "temp-001", title: "OAuth login" }),
        "temp-002": makeDraft({ id: "temp-002", title: "Dark mode" }),
      },
      selectedIds: [],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    expect(screen.getByTestId("draft-tray")).toBeInTheDocument();
    const rows = screen.getAllByTestId("draft-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("temp-001")).toBeInTheDocument();
    expect(screen.getByText("OAuth login")).toBeInTheDocument();
    expect(screen.getByText("Dark mode")).toBeInTheDocument();
  });

  test("shows the kept-count summary", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: {
        "temp-001": makeDraft({ id: "temp-001" }),
        "temp-002": makeDraft({ id: "temp-002" }),
      },
      selectedIds: ["temp-001"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    expect(screen.getByTestId("draft-tray-count")).toHaveTextContent("1 kept");
  });
});

describe("<DraftTray /> selection toggle", () => {
  test("clicking a checkbox adds the id to selectedIds", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: [],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    const box = screen.getByTestId("draft-checkbox");
    fireEvent.click(box);
    expect(useUiStore.getState().selectedIds).toContain("temp-001");
  });

  test("clicking a selected checkbox removes the id from selectedIds", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: ["temp-001"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    const box = screen.getByTestId("draft-checkbox");
    expect(box).toBeChecked();
    fireEvent.click(box);
    expect(useUiStore.getState().selectedIds).not.toContain("temp-001");
  });
});

describe("<DraftTray /> confirm-all button", () => {
  test("the confirm button is disabled when nothing is kept", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: [],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    expect(screen.getByTestId("draft-tray-confirm")).toBeDisabled();
  });

  test("the confirm button is enabled when at least one draft is kept", () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: ["temp-001"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    expect(screen.getByTestId("draft-tray-confirm")).not.toBeDisabled();
    expect(screen.getByTestId("draft-tray-confirm")).toHaveTextContent(
      "Confirm all (1)",
    );
  });

  test("clicking confirm calls the mutation with only the kept drafts", async () => {
    const captured: { drafts: DraftCard[] }[] = [];
    const api = setupConfirmMock({
      onMutate: (body) => captured.push(body),
    });
    useUiStore.setState({
      drafts: {
        "temp-001": makeDraft({ id: "temp-001", title: "OAuth login" }),
        "temp-002": makeDraft({ id: "temp-002", title: "Dark mode" }),
      },
      selectedIds: ["temp-002"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    await act(async () => {
      fireEvent.click(screen.getByTestId("draft-tray-confirm"));
    });
    expect(api.mutate).toHaveBeenCalledTimes(1);
    expect(captured).toHaveLength(1);
    expect(captured[0]!.drafts).toHaveLength(1);
    expect(captured[0]!.drafts[0]!.id).toBe("temp-002");
  });

  test("the tray clears after a successful confirm", async () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: ["temp-001"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    expect(screen.getByTestId("draft-tray")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByTestId("draft-tray-confirm"));
    });
    await waitFor(() => {
      expect(useUiStore.getState().drafts).toEqual({});
      expect(useUiStore.getState().selectedIds).toEqual([]);
    });
    expect(screen.queryByTestId("draft-tray")).toBeNull();
  });
});

describe("<DraftTray /> cancel button", () => {
  test("clicking cancel clears the drafts and hides the tray", async () => {
    setupConfirmMock({});
    useUiStore.setState({
      drafts: { "temp-001": makeDraft({ id: "temp-001" }) },
      selectedIds: ["temp-001"],
    });
    renderWithClient(<DraftTray projectId="proj-1" />);
    fireEvent.click(screen.getByTestId("draft-tray-cancel"));
    expect(useUiStore.getState().drafts).toEqual({});
    expect(useUiStore.getState().selectedIds).toEqual([]);
    expect(screen.queryByTestId("draft-tray")).toBeNull();
  });
});