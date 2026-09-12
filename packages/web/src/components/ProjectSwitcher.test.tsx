// SPDX-License-Identifier: Apache-2.0
/**
 * Render tests for `<ProjectSwitcher />`. Mocks `useProjects` to drive
 * the component through each branch: empty state, pending, error, and
 * the populated dropdown (which is where the real feature work lives).
 *
 * The PickProjectFolderButton inside the populated dropdown is mocked
 * so we don't re-test it here — its tests live in
 * `PickProjectFolderButton.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

vi.mock("../lib/api/projects.js", () => ({
  useProjects: vi.fn(),
}));

vi.mock("../lib/state/ui-store.js", () => ({
  useUiStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      activeProjectId: null,
      setActiveProjectId: vi.fn(),
    }),
}));

vi.mock("./PickProjectFolderButton.tsx", () => ({
  PickProjectFolderButton: () => <button>Pick a project folder</button>,
}));

import { useProjects } from "../lib/api/projects.js";
import { ProjectSwitcher } from "./ProjectSwitcher.tsx";

const useProjectsMock = vi.mocked(useProjects);

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("<ProjectSwitcher />", () => {
  test("empty state: shows 'No projects yet.' + the picker CTA", () => {
    useProjectsMock.mockReturnValue({
      data: [],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    renderWithClient(<ProjectSwitcher />);
    expect(screen.getByText(/no projects yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pick a project folder/i })).toBeInTheDocument();
  });

  test("pending state: shows '(loading…)'", () => {
    useProjectsMock.mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    renderWithClient(<ProjectSwitcher />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  test("error state: shows the error message and a Retry button", () => {
    useProjectsMock.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("network down"),
    } as unknown as ReturnType<typeof useProjects>);
    renderWithClient(<ProjectSwitcher />);
    expect(screen.getByRole("alert")).toHaveTextContent(/network down/i);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("populated state: trigger button is collapsed by default", () => {
    useProjectsMock.mockReturnValue({
      data: [
        { id: "a", name: "alpha", path: "/a", added_at: "", last_accessed_at: "2026-01-01T00:00:00Z" },
        { id: "b", name: "beta", path: "/b", added_at: "", last_accessed_at: "2026-02-01T00:00:00Z" },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    renderWithClient(<ProjectSwitcher />);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /select a project/i })).toBeInTheDocument();
  });

  test("clicking the trigger opens the menu with both projects (sorted most-recent first)", () => {
    useProjectsMock.mockReturnValue({
      data: [
        { id: "a", name: "alpha", path: "/a", added_at: "", last_accessed_at: "2026-01-01T00:00:00Z" },
        { id: "b", name: "beta", path: "/b", added_at: "", last_accessed_at: "2026-02-01T00:00:00Z" },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    renderWithClient(<ProjectSwitcher />);
    fireEvent.click(screen.getByRole("button", { name: /select a project/i }));
    const menu = screen.getByRole("menu", { name: /registered projects/i });
    expect(menu).toBeInTheDocument();
    const items = screen.getAllByRole("menuitem");
    expect(items).toHaveLength(2);
    // Most recent first → beta before alpha.
    expect(items[0]).toHaveTextContent("beta");
    expect(items[1]).toHaveTextContent("alpha");
  });
});
