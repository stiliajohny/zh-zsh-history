"""Filters applied before a command is stored."""

from __future__ import annotations

import re
from collections.abc import Mapping

REGEX_LIMIT = (
    "Regex filters run before a command is stored, but they cannot guarantee "
    "that a secret is removed."
)


def rejection_reason(command: str, config: dict) -> str | None:
    """Return why a command must not be stored, or None when it may be stored."""
    if config.get("paused"):
        return "paused"
    if command == "":
        return "empty"
    if config.get("ignore_space", True) and command[:1].isspace():
        return "leading-space"
    for pattern in config.get("exclude_regex") or []:
        try:
            if re.search(pattern, command):
                return "pattern"
        except re.error:
            continue
    return None


def filter_environment(environ: Mapping[str, str], allowlist: list[str]) -> dict[str, str]:
    """Keep only allowlisted environment variables."""
    allowed = set(allowlist)
    return {key: environ[key] for key in sorted(allowed) if key in environ}


def invalid_patterns(patterns: list[str]) -> list[str]:
    """Return regex patterns that do not compile."""
    invalid: list[str] = []
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error:
            invalid.append(pattern)
    return invalid
