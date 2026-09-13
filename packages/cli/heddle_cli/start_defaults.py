# SPDX-License-Identifier: Apache-2.0
"""Default collaborators for ``heddle start`` — feat-050.

Holds the *real-world* side-effecting implementations of the four
collaborator types the start command accepts as injection points
(``PopenFactory``, ``HealthProbe``, ``BuildRunner``, ``BrowserOpener``).
Tests substitute their own no-op versions; production wiring uses
these. Kept separate from ``start.py`` so the orchestrator stays
focused on the control flow and each file remains under the 200-line
soft cap from CODE_STYLE.md.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from typing import Mapping, Sequence

import webbrowser


def default_popen(*args: object, **kwargs: object) -> subprocess.Popen:
    """Real ``subprocess.Popen``; only used when no factory is injected."""
    return subprocess.Popen(*args, **dict(kwargs))  # type: ignore[arg-type]


def default_health_probe(url: str, timeout_s: float) -> bool:
    """Default health probe: GET ``url`` and treat 2xx/3xx/4xx as up.

    We accept any non-5xx response as "up" so the probe doesn't fail
    when the backend is mid-startup and a route hasn't been wired yet.
    The goal of the probe is "is Fastify bound and answering HTTP?" —
    not "is the daemon WS handshaking". A 404 from Fastify's static
    plugin still means the listener is live.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # 4xx = Fastify is up but doesn't serve / — still good enough.
        # 5xx = Fastify is up but erroring — keep polling.
        return exc.code < 500
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def default_build_runner(cmd: Sequence[str], env: Mapping[str, str]) -> int:
    """Run ``cmd`` (a pnpm invocation) and return the exit code."""
    completed = subprocess.run(list(cmd), env=dict(env), check=False)
    return int(completed.returncode)


#: Default browser opener; thin re-export so callers can inject without
#: importing the ``webbrowser`` stdlib module themselves.
default_browser_opener = webbrowser.open


__all__ = [
    "default_browser_opener",
    "default_build_runner",
    "default_health_probe",
    "default_popen",
]
