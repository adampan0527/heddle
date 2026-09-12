// SPDX-License-Identifier: Apache-2.0
/**
 * Render tests for `<PickProjectFolderButton />`. Mocks `useCreateProject`
 * and the UI store so we can assert: idle → click → path prompt →
 * submit → mutation called + active project updated; error path
 * surfaces the daemon error code.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

const mutateAsync = vi.fn();
const setActiveProjectId = vi.fn();

vi.mock("../lib/api/projects.js", () => ({
  useCreateProject: () => ({
    mutateAsync,
    // Surface the same shape TanStack Query exposes so the component
    // type-checks without further casts.
    isPending: false,
    isError: false,
    error: null,
    data: undefined,
    mutate: vi.fn(),
    reset: vi.fn(),
    variables: undefined,
    context: undefined,
    failureCount: 0,
    failureReason: null,
    status: "idle",
  }),
}));

vi.mock("../lib/state/ui-store.js", () => ({
  useUiStore: (selector: (s: { setActiveProjectId: unknown }) => unknown) =>
    selector({ setActiveProjectId }),
}));

import { PickProjectFolderButton } from "./PickProjectFolderButton.tsx";

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("<PickProjectFolderButton />", () => {
  beforeEach(() => {
    mutateAsync.mockReset();
    setActiveProjectId.mockReset();
    window.localStorage.clear();
  });

  test("idle: shows the 'Pick a project folder' button", () => {
    renderWithClient(<PickProjectFolderButton />);
    expect(screen.getByRole("button", { name: /pick a project folder/i })).toBeInTheDocument();
  });

  test("clicking the button reveals the path prompt", async () => {
    renderWithClient(<PickProjectFolderButton />);
    fireEvent.click(screen.getByRole("button", { name: /pick a project folder/i }));
    expect(await screen.findByLabelText(/absolute path to project folder/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^add$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
  });

  test("submitting an empty path shows an inline error", async () => {
    renderWithClient(<PickProjectFolderButton />);
    fireEvent.click(screen.getByRole("button", { name: /pick a project folder/i }));
    const input = await screen.findByLabelText(/absolute path to project folder/i);
    // Submit empty: the form's submit handler calls preventDefault and
    // sets the error status without firing mutateAsync.
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/path cannot be empty/i);
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  test("successful submit calls the mutation and sets the active project", async () => {
    mutateAsync.mockResolvedValueOnce({
      id: "new-1",
      name: "new",
      path: "/tmp/new",
      added_at: "2026-09-13T00:00:00Z",
      last_accessed_at: "2026-09-13T00:00:00Z",
    });
    renderWithClient(<PickProjectFolderButton />);
    fireEvent.click(screen.getByRole("button", { name: /pick a project folder/i }));
    const input = (await screen.findByLabelText(
      /absolute path to project folder/i,
    )) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "/tmp/new" } });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith({ path: "/tmp/new" });
    });
    expect(setActiveProjectId).toHaveBeenCalledWith("new-1");
  });

  test("failed submit surfaces the daemon error message", async () => {
    mutateAsync.mockRejectedValueOnce(new Error("boom"));
    renderWithClient(<PickProjectFolderButton />);
    fireEvent.click(screen.getByRole("button", { name: /pick a project folder/i }));
    const input = (await screen.findByLabelText(
      /absolute path to project folder/i,
    )) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "/tmp/bad" } });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("boom");
    });
    expect(setActiveProjectId).not.toHaveBeenCalled();
  });
});
