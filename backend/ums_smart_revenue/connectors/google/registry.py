"""CLI --connector dispatch registry.

B2.4 registers 'youtube-reporting'. B2.5 adds 'youtube-analytics'.
B2.6 adds 'adsense-management'. Unknown keys raise ValueError at
argparse time.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

_RunnerFn = Callable[..., Any]
_REGISTRY: dict[str, _RunnerFn] = {}


def register_connector(*, key: str, runner: _RunnerFn) -> None:
    _REGISTRY[key] = runner


def dispatch_connector(*, key: str) -> _RunnerFn:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"unknown connector key: {key!r}") from exc


def known_keys() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
