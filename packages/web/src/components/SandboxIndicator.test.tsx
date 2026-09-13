// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the SandboxIndicator (feat-055 / D-053).
 *
 * Verifies:
 *   - The badge renders the active project's level
 *   - Clicking the badge opens the dropdown with all three levels
 *   - Picking a level fires the PATCH mutation (via useUpdateProjectSandbox)
 *   - The "Test destructive action" button only appears for
 *     edit-with-confirm and fires the host callback
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

vi.mock("../lib/api/projects.js", () => ({
  useProjects: vi.fn(),
  useUpdateProjectSandbox: vi.fn(),
}));

import {
  useProjects,
  useUpdateProjectSandbox,
} from "../lib/api/projects.js";
import { SandboxIndicator } from "./SandboxIndicator.tsx";

const useProjectsMock = vi.mocked(useProjects);
const useUpdateProjectSandboxMock = vi.mocked(useUpdateProjectSandbox);

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("<SandboxIndicator />", () => {
  test("renders nothing when no project is active", () => {
    useProjectsMock.mockReturnValue({
      data: [],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    const { container } = renderWithClient(
      <SandboxIndicator projectId={null} onTestDestructive={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders badge with current level = full when project has no sandbox_level", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    const badge = screen.getByTestId("sandbox-badge");
    expect(badge).toHaveTextContent(/full/i);
    expect(badge.dataset.level).toBe("full");
  });

  test("badge reflects project's sandbox_level", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "edit-with-confirm",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    const badge = screen.getByTestId("sandbox-badge");
    expect(badge.dataset.level).toBe("edit-with-confirm");
  });

  test("clicking the badge opens the menu with all three options", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "full",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    fireEvent.click(screen.getByTestId("sandbox-badge"));
    expect(screen.getByTestId("sandbox-option-read-only")).toBeInTheDocument();
    expect(
      screen.getByTestId("sandbox-option-edit-with-confirm"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("sandbox-option-full")).toBeInTheDocument();
  });

  test("picking a different level calls the mutation", async () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "full",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    const mutate = vi.fn().mockResolvedValue(undefined);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    fireEvent.click(screen.getByTestId("sandbox-badge"));
    fireEvent.click(screen.getByTestId("sandbox-option-read-only"));
    expect(mutate).toHaveBeenCalledWith({
      projectId: "p1",
      sandbox_level: "read-only",
    });
  });

  test("picking the current level does NOT call the mutation", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "full",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    const mutate = vi.fn();
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    fireEvent.click(screen.getByTestId("sandbox-badge"));
    fireEvent.click(screen.getByTestId("sandbox-option-full"));
    expect(mutate).not.toHaveBeenCalled();
  });

  test("'Test destructive action' button only appears at edit-with-confirm", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "edit-with-confirm",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    const onTest = vi.fn();
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={onTest} />,
    );
    const btn = screen.getByTestId("sandbox-test-destructive");
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    expect(onTest).toHaveBeenCalled();
  });

  test("'Test destructive action' button hidden at full level", () => {
    useProjectsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "alpha",
          path: "/a",
          added_at: "",
          last_accessed_at: "",
          sandbox_level: "full",
        },
      ],
      isPending: false,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useProjects>);
    useUpdateProjectSandboxMock.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateProjectSandbox>);
    renderWithClient(
      <SandboxIndicator projectId="p1" onTestDestructive={() => undefined} />,
    );
    expect(
      screen.queryByTestId("sandbox-test-destructive"),
    ).not.toBeInTheDocument();
  });
});
