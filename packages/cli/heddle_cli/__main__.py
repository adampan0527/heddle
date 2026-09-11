"""CLI subcommand entry. v0.1 stubs only — see feat-050 / feat-051 / feat-052 / feat-053."""

from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="heddle")
def cli() -> None:
    """heddle — self-hostable web control plane for long-running coding agents."""


@cli.command()
def start() -> None:
    """Start the full stack (Node.js backend + Python daemon). Not implemented in v0.1 stub."""
    click.echo("heddle start: not yet implemented (feat-050)")


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


if __name__ == "__main__":
    cli()