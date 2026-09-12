// SPDX-License-Identifier: Apache-2.0
/**
 * Top-level app shell — feat-032 skeleton.
 *
 * Renders the header with `<ProjectSwitcher />` and two empty
 * placeholder sections for the kanban (feat-035) and the bottom
 * dialog (feat-044). Tailwind v4 utility classes only.
 */

import { ProjectSwitcher } from "./components/ProjectSwitcher.tsx";

export default function App(): React.ReactElement {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-6 py-3">
        <h1 className="text-lg font-semibold text-zinc-100">heddle</h1>
        <ProjectSwitcher />
      </header>

      <main className="flex flex-1 flex-col gap-4 p-6">
        <section
          aria-label="Kanban placeholder"
          className="flex flex-1 items-center justify-center rounded border border-dashed border-zinc-700 text-zinc-500"
        >
          Kanban (feat-035)
        </section>
        <section
          aria-label="Dialog placeholder"
          className="flex h-32 items-center justify-center rounded border border-dashed border-zinc-700 text-zinc-500"
        >
          Dialog (feat-044)
        </section>
      </main>
    </div>
  );
}
