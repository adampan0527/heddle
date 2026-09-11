# heddle

Self-hostable web-based control plane for running long-running, multi-session coding agents on a project, with a DAG-aware kanban board as the primary interface.

`heddle` wraps the existing `HARNESS/` long-running agent harness with a web frontend, a Node.js backend gateway, and a Python daemon that runs the underlying agent loop.

> **Status:** v0.1 development. Not yet feature-complete or production-ready.

## Repository layout

| Path | Contents |
|---|---|
| `packages/web/` | Browser frontend (React + Vite + Tailwind) |
| `packages/cli/` | `heddle` console-script entry point (Python + click) |
| `packages/daemon/` | Python daemon (asyncio + LangGraph + LangChain) |
| `HARNESS/` | Reference implementation of the underlying agent loop (vendored, MIT-licensed) |
| `DESIGN.md` | Product design decisions (D-NNN / Q-NNN) — *what* and *why* |
| `TECH.md` | Engineering decisions (T-NNN) — *how* |
| `feature_list.json` | v0.1 feature backlog (54 features + 2 HARNESS placeholders) |
| `LICENSE` | Apache License 2.0 (this project; excludes HARNESS/) |
| `pyproject.toml` + `pnpm-workspace.yaml` | Monorepo workspace roots |

## Quick start

Prerequisites: Python ≥ 3.11, Node.js ≥ 20, pnpm ≥ 11.

```bash
# Install JS workspace deps
pnpm install

# Install Python umbrella (heddle CLI + daemon) in editable mode
pip install -e .

# Verify
heddle --version          # -> heddle, version 0.0.1
pnpm --filter web dev      # -> Vite at http://127.0.0.1:5173
```

Full launch is gated on `feat-050` (`heddle start`); for now the CLI has stubs for `start`, `dev`, `test`, `lint`.

## License

This project is licensed under the **Apache License, Version 2.0** — see the [`LICENSE`](./LICENSE) file for the full text.

The `HARNESS/` directory is **separately licensed under the MIT License** — it is a vendored reference implementation adapted from Anthropic's open-source harness pattern. See [`HARNESS/README.md`](./HARNESS/README.md) for details.

## Documentation

- [`DESIGN.md`](./DESIGN.md) — product decisions (D-001 … D-060)
- [`TECH.md`](./TECH.md) — engineering decisions (T-001 … T-032)
- [`feature_list.json`](./feature_list.json) — v0.1 feature backlog
- [`_AGENT.md`](./_AGENT.md) — agent document index (read first if you are an AI agent)
- [`HARNESS/HARNESS.md`](./HARNESS/HARNESS.md) — harness workflow rules