# SPDX-License-Identifier: Apache-2.0
"""CLI subcommand entry. v0.1 stubs only — see feat-050 / feat-051 / feat-052 / feat-053.

`heddle configs list` is the first non-stub command and ships with
feat-011 (T-023 / D-055). It reads `~/.heddle/configs.yaml` via the
shared `heddle_common.configs_io` library so the CLI and the daemon
see the same data and same validation rules.
"""

from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="heddle")
def cli() -> None:
    """heddle — self-hostable web control plane for long-running coding agents."""


@cli.command()
@click.option(
    "--host",
    default=None,
    help="Node.js backend bind host (default 127.0.0.1). Loopback only.",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Node.js backend bind port (default 5174).",
)
@click.option(
    "--frontend-url",
    default=None,
    help="URL the browser is opened to after the backend is ready.",
)
def start(
    host: str | None,
    port: int | None,
    frontend_url: str | None,
) -> None:
    """Start the full stack (Node.js backend + Python daemon).

    Spawns ``node packages/node/dist/main.js`` (which supervises the
    Python daemon over loopback WS — feat-027) and, once the backend
    has bound its HTTP port, opens the default browser to
    ``http://localhost:5173``. Refuses to start when ``HEDDLE_NODE_HOST``
    or ``HEDDLE_BIND`` points at a non-loopback address (D-037).
    """
    # Forward explicit Click flags into the pure ``start_cmd`` via the
    # argv list. We keep ``start_cmd`` Click-free so unit tests can
    # exercise every branch without invoking the CLI group.
    from .start import start_cmd

    argv: list[str] = []
    if host is not None:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    if frontend_url is not None:
        argv += ["--frontend-url", frontend_url]
    raise SystemExit(start_cmd(argv))


@cli.command()
def dev() -> None:
    """Start dev mode (Vite + Fastify + daemon with hot reload). Not implemented in v0.1 stub."""
    click.echo("heddle dev: not yet implemented (feat-051)")


@cli.command()
def test() -> None:
    """Run the test pyramid. Not implemented in v0.1 stub."""
    click.echo("heddle test: not yet implemented (feat-052)")


@cli.command()
def lint() -> None:
    """Run linters. Not implemented in v0.1 stub."""
    click.echo("heddle lint: not yet implemented (feat-053)")


# ---------- configs subcommand group (feat-011) ----------

@cli.group()
def configs() -> None:
    """Manage the LLM provider registry at `~/.heddle/configs.yaml`."""


@configs.command("list")
@click.option(
    "--config-path",
    type=click.Path(),
    default=None,
    help="Path to configs.yaml (defaults to ~/.heddle/configs.yaml).",
)
def configs_list(config_path: str | None) -> None:
    """List the named LLM provider configs (feat-011 / T-023 / D-055).

    On first run (file does not exist) populates the four default
    templates (`anthropic-claude-sonnet` / `openai-gpt-4` /
    `bedrock-claude` / `ollama-llama`) and prints them. The `api_key`
    field is intentionally never displayed (T-015).
    """
    # Import lazily so `heddle --help` doesn't require PyYAML.
    from heddle_common import configs_io

    target = configs_io.default_configs_path() if config_path is None else __import__("pathlib").Path(config_path)
    configs = configs_io.ensure_configs(target)
    if not configs:
        click.echo("(no configs registered)")
        return
    click.echo(f"# {len(configs)} config(s) at {target}")
    click.echo("")
    for cfg in configs.values():
        marker = "✓" if __resolve_ok(cfg) else "✗"
        click.echo(f"  {marker} {cfg.name}")
        click.echo(f"      provider:    {cfg.provider}")
        click.echo(f"      model:       {cfg.model}")
        click.echo(f"      base_url:    {cfg.base_url}")
        click.echo(f"      api_key_env: {cfg.api_key_env} "
                   f"({'set' if __resolve_ok(cfg) else 'NOT SET'})")
        if cfg.extras:
            for k, v in sorted(cfg.extras.items()):
                click.echo(f"      {k}: {v}")
        click.echo("")


def __resolve_ok(cfg) -> bool:
    """True iff the env var named by cfg.api_key_env is set.

    Local helper (mangled name to keep it private to this module) so
    the CLI list view can mark which templates are ready-to-use. Never
    prints the resolved value (T-015).
    """
    from heddle_common import configs_io

    return configs_io.resolve_api_key_for_config(cfg) is not None


if __name__ == "__main__":
    cli()
