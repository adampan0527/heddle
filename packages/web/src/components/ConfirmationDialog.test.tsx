// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for the sandbox ConfirmationDialog (feat-055 / D-053).
 *
 * Exercises the modal's three control paths:
 *   - Approve runs onApprove; on success the dialog stays open until
 *     the host unmounts it (the host owns state, not the modal).
 *   - Deny runs onDeny exactly once.
 *   - Cancel runs onCancel exactly once.
 *
 * When ``open === false`` the modal renders nothing — that's the
 * contract the App.tsx host relies on so it can park / clear the
 * ui-store entry to control visibility.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { ConfirmationDialog } from "./ConfirmationDialog.tsx";

describe("<ConfirmationDialog />", () => {
  test("renders nothing when closed", () => {
    const { container } = render(
      <ConfirmationDialog
        open={false}
        toolName="write"
        args={{ path: "x" }}
        onApprove={async () => undefined}
        onDeny={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders the dialog with tool name + args when open", () => {
    render(
      <ConfirmationDialog
        open
        toolName="write"
        args={{ path: "src/example.ts", content: "hello" }}
        onApprove={async () => undefined}
        onDeny={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(screen.getByTestId("sandbox-confirm-dialog")).toBeInTheDocument();
    expect(screen.getByText(/allow write\?/i)).toBeInTheDocument();
    const args = screen.getByTestId("sandbox-confirm-args");
    expect(args.textContent).toContain("src/example.ts");
    expect(args.textContent).toContain("hello");
  });

  test("Approve button invokes onApprove", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(
      <ConfirmationDialog
        open
        toolName="bash"
        args={{ command: "rm -rf node_modules" }}
        onApprove={onApprove}
        onDeny={() => undefined}
        onCancel={() => undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("sandbox-confirm-approve"));
    await waitFor(() => {
      expect(onApprove).toHaveBeenCalledTimes(1);
    });
  });

  test("Approve error surfaces inline", async () => {
    const onApprove = vi
      .fn()
      .mockRejectedValue(new Error("tool exploded"));
    render(
      <ConfirmationDialog
        open
        toolName="write"
        args={{}}
        onApprove={onApprove}
        onDeny={() => undefined}
        onCancel={() => undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("sandbox-confirm-approve"));
    await waitFor(() => {
      expect(screen.getByTestId("sandbox-confirm-error")).toHaveTextContent(
        /tool exploded/i,
      );
    });
  });

  test("Deny button invokes onDeny", () => {
    const onDeny = vi.fn();
    render(
      <ConfirmationDialog
        open
        toolName="write"
        args={{}}
        onApprove={async () => undefined}
        onDeny={onDeny}
        onCancel={() => undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("sandbox-confirm-deny"));
    expect(onDeny).toHaveBeenCalledTimes(1);
  });

  test("Cancel button invokes onCancel", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmationDialog
        open
        toolName="write"
        args={{}}
        onApprove={async () => undefined}
        onDeny={() => undefined}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByTestId("sandbox-confirm-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
