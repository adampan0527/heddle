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

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { Dialog } from "./Dialog.tsx";

vi.mock("../lib/api/dialog.js", () => ({
  useSubmitDialog: vi.fn(),
}));

vi.mock("../lib/api/features.js", () => ({
  useFeatures: vi.fn(),
  // feat-043: dialog commands route through these two mutations; the
  // default mock returns an idle, never-resolving hook so chat-only
  // tests never accidentally invoke them.
  useRetryFeature: vi.fn(),
  useTransitionFeature: vi.fn(),
}));

import { useSubmitDialog } from "../lib/api/dialog.js";
import {
  useFeatures,
  useRetryFeature,
  useTransitionFeature,
} from "../lib/api/features.js";

const useSubmitDialogMock = vi.mocked(useSubmitDialog);
const useFeaturesMock = vi.mocked(useFeatures);
const useRetryFeatureMock = vi.mocked(useRetryFeature);
const useTransitionFeatureMock = vi.mocked(useTransitionFeature);

interface MockApi {
  mutateAsync: ReturnType<typeof vi.fn>;
}

/** Build a TanStack-shaped idle hook result for the dialog-command
 *  mutations. Dialog tests that don't care about commands share one
 *  fixture; command-specific tests can replace via the override. */
function idleMutation(): ReturnType<typeof useRetryFeature> {
  return {
    mutateAsync: vi.fn(async () => {
      throw new Error("command mutation not expected");
    }),
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
    isPending: false,
    isSuccess: false,
    status: "idle",
    submittedAt: 0,
  } as unknown as ReturnType<typeof useRetryFeature>;
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
  // The mention dropdown queries the project's features (feat-039).
  // Default fixture: empty list, success. Individual tests can override.
  useFeaturesMock.mockReturnValue({
    data: [],
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useFeatures>);
  useRetryFeatureMock.mockReturnValue(idleMutation());
  useTransitionFeatureMock.mockReturnValue(
    idleMutation() as unknown as ReturnType<typeof useTransitionFeature>,
  );
  return { mutateAsync };
}

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>{ui}</QueryClientProvider>,
  );
}

describe("<Dialog />", () => {
  test("renders empty-state placeholder when no project is selected", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    renderWithClient(<Dialog projectId={null} />);
    expect(screen.getByTestId("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("dialog-empty")).toHaveTextContent(
      /no messages yet/i,
    );
    // The send button is disabled when no project is selected.
    expect(screen.getByTestId("dialog-send")).toBeDisabled();
  });

  test("enables the textarea + send button when a project is selected", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    renderWithClient(<Dialog projectId="proj-1" />);
    expect(screen.getByTestId("dialog-textarea")).not.toBeDisabled();
    // Empty draft -> send disabled.
    expect(screen.getByTestId("dialog-send")).toBeDisabled();
  });

  test("enables Send when the textarea has non-whitespace content", () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    renderWithClient(<Dialog projectId="proj-1" />);
    fireEvent.change(screen.getByTestId("dialog-textarea"), {
      target: { value: "hello" },
    });
    expect(screen.getByTestId("dialog-send")).not.toBeDisabled();
  });

  test("submitting via click posts the message and clears the draft", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "hi back" },
    });
    renderWithClient(<Dialog projectId="proj-1" />);
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
    renderWithClient(<Dialog projectId="proj-1" />);
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
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "line1" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("whitespace-only drafts do not submit", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "" },
    });
    renderWithClient(<Dialog projectId="proj-1" />);
    fireEvent.change(screen.getByTestId("dialog-textarea"), {
      target: { value: "   \n  " },
    });
    fireEvent.click(screen.getByTestId("dialog-send"));
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("the user message and assistant response both appear in the transcript", async () => {
    setupMock({ resolve: { kind: "chat", text: "echo: hello" } });
    renderWithClient(<Dialog projectId="proj-1" />);
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
    renderWithClient(<Dialog projectId="proj-1" />);
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
    renderWithClient(<Dialog projectId="proj-1" />);
    const section = screen.getByRole("region", { name: /dialog/i });
    expect(section).toBe(screen.getByTestId("dialog"));
  });

  test("the @feat-XXX diagnose shortcut renders a <DiagnosisReport> card", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, {
      target: { value: "@feat-042 diagnose please" },
    });
    fireEvent.click(screen.getByTestId("dialog-send"));
    // Diagnose shortcut short-circuits the mutation; it should not be
    // called at all.
    await waitFor(() => {
      expect(api.mutateAsync).not.toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.getByTestId("diagnosis-report")).toBeInTheDocument();
    });
    // The card renders all three sections.
    expect(screen.getByTestId("diagnosis-cause")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-suggestion")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-diff-section")).toBeInTheDocument();
    expect(screen.getByTestId("diagnosis-apply")).toBeInTheDocument();
    // The feature id subtitle is shown.
    expect(screen.getByTestId("diagnosis-feature-id")).toHaveTextContent(
      "feat-042",
    );
  });

  test("a non-diagnose message still goes through the mutation", async () => {
    const api = setupMock({
      resolve: { kind: "chat", text: "plain reply" },
    });
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "hello there" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(api.mutateAsync).toHaveBeenCalledWith({
        message: "hello there",
      });
    });
    expect(screen.queryByTestId("diagnosis-report")).toBeNull();
    await waitFor(() => {
      expect(
        screen.getByTestId("dialog-entry-assistant"),
      ).toHaveTextContent("plain reply");
    });
  });

  // ----- feat-043 / D-033: failure-handling dialog commands -----

  test("`@feat-XXX retry` routes to useRetryFeature (no dialog mutation)", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    const retryMutate = vi.fn(async () => ({
      project_id: "proj-1",
      feature_id: "feat-042",
      feature: {} as never,
    }));
    useRetryFeatureMock.mockReturnValue({
      ...idleMutation(),
      mutateAsync: retryMutate,
    } as unknown as ReturnType<typeof useRetryFeature>);
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "@feat-042 retry" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(retryMutate).toHaveBeenCalledWith({ featureId: "feat-042" });
    });
    // The chat mutation must not run when a command short-circuits.
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("`@feat-XXX retry-with-hint:<text>` forwards the hint verbatim", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    const retryMutate = vi.fn(async () => ({
      project_id: "proj-1",
      feature_id: "feat-042",
      feature: {} as never,
    }));
    useRetryFeatureMock.mockReturnValue({
      ...idleMutation(),
      mutateAsync: retryMutate,
    } as unknown as ReturnType<typeof useRetryFeature>);
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, {
      target: { value: "@feat-042 retry-with-hint:use OpenAI" },
    });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(retryMutate).toHaveBeenCalledWith({
        featureId: "feat-042",
        hint: "use OpenAI",
      });
    });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("`@feat-XXX mark-done` routes to transitionMutation action mark-done", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    const transitionMutate = vi.fn(async () => ({
      project_id: "proj-1",
      feature_id: "feat-042",
      action: "mark-done",
      feature: {} as never,
    }));
    useTransitionFeatureMock.mockReturnValue({
      ...idleMutation(),
      mutateAsync: transitionMutate,
    } as unknown as ReturnType<typeof useTransitionFeature>);
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "@feat-042 mark-done" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(transitionMutate).toHaveBeenCalledWith({
        featureId: "feat-042",
        action: "mark-done",
      });
    });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("`@feat-XXX abandon` routes to transitionMutation action abandon", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    const transitionMutate = vi.fn(async () => ({
      project_id: "proj-1",
      feature_id: "feat-042",
      action: "abandon",
      feature: {} as never,
    }));
    useTransitionFeatureMock.mockReturnValue({
      ...idleMutation(),
      mutateAsync: transitionMutate,
    } as unknown as ReturnType<typeof useTransitionFeature>);
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "@feat-042 abandon" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(transitionMutate).toHaveBeenCalledWith({
        featureId: "feat-042",
        action: "abandon",
      });
    });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("`@feat-XXX diagnose` renders a DiagnosisReport card", async () => {
    const api = setupMock({ resolve: { kind: "chat", text: "" } });
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "@feat-042 diagnose" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(screen.getByTestId("diagnosis-report")).toBeInTheDocument();
    });
    expect(api.mutateAsync).not.toHaveBeenCalled();
  });

  test("a command failure surfaces as a transcript error entry", async () => {
    setupMock({ resolve: { kind: "chat", text: "" } });
    const retryMutate = vi.fn(async () => {
      throw new Error("daemon offline");
    });
    useRetryFeatureMock.mockReturnValue({
      ...idleMutation(),
      mutateAsync: retryMutate,
    } as unknown as ReturnType<typeof useRetryFeature>);
    renderWithClient(<Dialog projectId="proj-1" />);
    const textarea = screen.getByTestId("dialog-textarea");
    fireEvent.change(textarea, { target: { value: "@feat-042 retry" } });
    fireEvent.click(screen.getByTestId("dialog-send"));
    await waitFor(() => {
      expect(
        screen.getByTestId("dialog-entry-error"),
      ).toHaveTextContent(/daemon offline/);
    });
  });
});