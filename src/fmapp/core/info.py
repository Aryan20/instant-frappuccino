"""Parse ``fm info`` (a Rich table) into labelled rows, flagging secrets.

fm prints credentials (Frappe admin, DB users, admin-tools basic auth) only via ``fm info``,
so we run it on demand and present the rows with reveal/copy controls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fmapp.core.textutil import clean_output

_ROW = re.compile(r"^[│|]\s*(?P<key>[^│|]*?)\s*[│|]\s?(?P<value>.*?)\s*[│|]\s*$")


@dataclass
class InfoRow:
    key: str
    value: str

    @property
    def secret(self) -> bool:
        return "password" in self.key.lower()

    @property
    def is_url(self) -> bool:
        return self.value.startswith(("http://", "https://"))


def parse_info(output: str) -> list[InfoRow]:
    rows: list[InfoRow] = []
    block: list[str] = []
    current = ""

    def flush() -> None:
        if current:
            rows.extend(_expand(current, block))

    for line in clean_output(output).splitlines():
        match = _ROW.match(line.strip())
        if not match:
            continue
        key, value = match["key"].strip(), match["value"].rstrip()
        if key:
            flush()
            current, block = key, [value]
        elif current:
            block.append(value)
    flush()
    return rows


def _expand(key: str, lines: list[str]) -> list[InfoRow]:
    """Turn multi-line cells into rows (e.g. the Admin Tools sub-table and its credentials)."""
    lines = [ln.strip() for ln in lines if ln.strip() and not set(ln.strip()) <= set("━╇─┃ ")]
    if len(lines) <= 1:
        return [InfoRow(key, re.sub(r"\s{2,}", "   ", lines[0]) if lines else "")]
    out: list[InfoRow] = []
    for line in lines:
        if m := re.match(r"^(\w[\w ]*?)\s*[│┃]\s*(https?://\S+)$", line):
            out.append(InfoRow(f"{key} · {m.group(1)}", m.group(2)))
        elif m := re.match(r"^(Username|Password):\s*(\S+)$", line):
            out.append(InfoRow(f"{key} {m.group(1).lower()}", m.group(2)))
        elif line.endswith(":") or line.startswith("Service"):
            continue
        else:
            # Service status grids like "frappe ✓  nginx ✓"
            out.append(InfoRow(key, re.sub(r"\s{2,}", "   ", line)))
    merged: list[InfoRow] = []
    for row in out:  # join consecutive status-grid lines under one key
        if merged and merged[-1].key == row.key == key:
            merged[-1].value += "   " + row.value
        else:
            merged.append(row)
    return merged
