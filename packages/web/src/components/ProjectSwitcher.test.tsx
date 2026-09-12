// SPDX-License-Identifier: Apache-2.0
/**
 * Render test for `<ProjectSwitcher>`. Mocks `useProjects` to return
 * a resolved empty list and asserts the `<select>` is in the DOM with
 * the "no projects yet" placeholder option.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

vi.mock("../lib/api/projects.js", () => ({
  useProjects: () => ({
    data: [],
    isPending: false,
    isError: false,
    error: null,
  }),
}));

import { ProjectSwitcher } from "./ProjectSwitcher.tsx";

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("<ProjectSwitcher />", () => {
  test("renders a <select> when project list is empty", () => {
    renderWithClient(<ProjectSwitcher />);
    const select = screen.getByRole("combobox", { name: /active project/i });
    expect(select).toBeInTheDocument();
    expect(screen.getByText("(no projects yet)")).toBeInTheDocument();
  });
});
