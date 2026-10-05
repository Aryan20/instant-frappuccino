"""Helpers for turning raw CLI output into readable log text."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")
# Rich draws tables with box characters; keep them, but drop spinner frames.
_SPINNER = re.compile(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*")


def clean_output(chunk: str) -> str:
    """Strip ANSI escapes and collapse carriage-return progress redraws."""
    text = _ANSI.sub("", chunk)
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            # Keep only the final redraw of a \r-updated progress line.
            line = line.rstrip("\r").rsplit("\r", 1)[-1]
        lines.append(_SPINNER.sub("", line))
    return "\n".join(lines)


def mask(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "••••••")
    return text


def slug_to_app_name(value: str) -> str:
    """Best-effort Frappe app name from a repo slug: ``Frappe-CRM.git`` → ``frappe_crm``."""
    name = value.rstrip("/").rsplit("/", 1)[-1]
    name = name.removesuffix(".git")
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
