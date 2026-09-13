// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the persistent bottom dialog (feat-038).
 *
 * Three layers of coverage:
 *
 *   1. **Render**: the empty placeholder appears when no project is
 *      active; the textarea + send button appear once a project is
 *      selected; the dialog is a section element with the right label.
 *   2. **Submit**: typing a message and clicking Send calls the
 *      mutation with the right payload; Enter submits without a
 *      Shift modifier; Shift+Enter inserts a newline instead.
 *   3. **Response display**: when the mutation resolves, the
 *      assistant's `text` shows up in the transcript; when it rejects,
 *      an error entry shows up instead.
 *
 * The mutation hook is mocked (TanStack Query would otherwise need a
 * full QueryClientProvider wrapper) so the tests stay deterministic
 * and don't depend on the live API hook's internals.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { Dialog } from "./Dialog.tsx";

vi.mock("../lib/api/dialog.js", () => ({
  useSubmitDialog: vi.fn(),
}));

import { useSubmitDialog } from "../lib/api/dialog.js";

const useSubmitDialogMock = vi.mocked(useSubmitDialog);

interface MockApi {
  mutateAsync: ReturnType<typeof vi.fn>;
}

function setupMock(opts: {
  resolve?: { kind: "chat" | "work"; text: string };
  reject?: Error;
}): MockApi {
  const mutateAsync = vi.fn(async () => {
    if (opts.reject) throw opts.reject;
    if (opts.resolve) return opts.resolve;
    throw new Error("test fixture did not provide resolve or reject");
  });
  useSubmitDialogMock.mockReturnValue({
    mutateAsync,
    isPending: false,
    // The component only touches mutateAsync + isPending; fill the rest
    // with type-correct stubs so the mock satisfies the hook's type.
    mutate: vi.fn(),
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
  } as unknown as ReturnType<typeof useSubmitDialog>);
  return { mutateAsync };
}

describe("<Dialog />", () => {
  test("renders empty-state placeholder when no project is selected", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    render(<Dialog projectId={null} />);
    expect(screen.getByTestId("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("dialog-empty")).toHaveTextContent(
      /no messages yet/i,
    );
    // The send button is disabled when no project is selected.
    expect(screen.getByTestId("dialog-send")).toBeDisabled();
  });

  test("enables the textarea + send button when a project is selected", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    render(<Dialog projectId="proj-1" />);
    expect(screen.getByTestId("dialog-textarea")).not.toBeDisabled();
    // Empty draft -> send disabled.
    expect(screen.getByTestId("dialog-send")).toBeDisabled();
  });

  test("enables Send when the textarea has non-whitespace content", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    render(<Dialog projectId="proj-1" />);
    fireEvent.change(screen.getByTestId("dialog-textarea"), {
      target: { value: "hello" },
    });
    expect(screen.getByTestId("dialog-send")).not.toBeDisabled();
  });

  test("submitting via click posts the message and clears the draft", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "hi back" },
    });
    render(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(api.mutateAsync).toHaveBeenCalledWith({ message: "hello" });
    });
    // Draft cleared after successful submit.
    expect(textarea).toHaveValue("");
  });

  test("submitting via Enter (no Shift) posts the message", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "ack" },
    });
    render(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => {
      expect(api.mutateAsync).toHaveBeenCalledWith({ message: "ping" });
    });
  });

  test("Shift+Enter inserts a newline without submitting", () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "" },
    });
    render(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "line1" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("whitespace-only drafts do not submit", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "" },
    });
    render(<Dialog projectId="proj-1" />);
    fireEvent.change(screen.getByTestId("dialog-textarea"), {
      target: { value: "   \n  " },
    });
    fireEvent.click(screen.getByTestId("dialog-send"));
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("the user message and assistant response both appear in the transcript", async () => {
    setupMock({ resolve: { kind: "chat", text: "echo: hello" } });
    render(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(
        screen.getByTestId("dialog-entry-user"),
      ).toHaveTextContent("hello");
    });
    await waitFor(() => {
      expect(
        screen.getByTestId("dialog-entry-assistant"),
      ).toHaveTextContent("echo: hello");
    });
  });

  test("errors are surfaced as a transcript entry, not a thrown promise", async () => {
    setupMock({ reject: new Error("daemon offline") });
    render(<Dialog projectId="proj-1" />);
    fireEvent.change(screen.getByTestId("dialog-textarea"), {
      target: { value: "hi" },
    });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(
        screen.getByTestId("dialog-entry-error"),
      ).toHaveTextContent("daemon offline");
    });
  });

  test("the dialog section carries the persistent bottom-dock landmark", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    render(<Dialog projectId="proj-1" />);
    const section = screen.getByRole("region", { name: /dialog/i });
    expect(section).toBe(screen.getByTestId("dialog"));
  });
});