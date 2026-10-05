"""The user's own app library ("My Apps"): custom repos plus saved marketplace apps."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from fmapp.core import paths
from fmapp.core.models import AppRef


@dataclass
class CatalogApp:
    title: str
    repo: str
    ref: str = ""
    subdir: str = ""
    description: str = ""
    marketplace_name: str = ""  # set when saved from the marketplace
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_ref(self) -> AppRef:
        return AppRef(repo=self.repo, ref=self.ref, subdir=self.subdir)


class Catalog:
    def __init__(self, file: Path | None = None) -> None:
        self.file = file or paths.config_dir() / "my_apps.json"
        self.apps: list[CatalogApp] = self._load()

    def _load(self) -> list[CatalogApp]:
        try:
            raw = json.loads(self.file.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        known = {f.name for f in fields(CatalogApp)}
        return [CatalogApp(**{k: v for k, v in item.items() if k in known}) for item in raw]

    def save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in self.apps], indent=2))
        tmp.replace(self.file)

    def upsert(self, app: CatalogApp) -> None:
        AppRef.parse(app.repo)  # validate before persisting
        for i, existing in enumerate(self.apps):
            if existing.id == app.id:
                self.apps[i] = app
                break
        else:
            self.apps.append(app)
        self.save()

    def remove(self, app_id: str) -> None:
        self.apps = [a for a in self.apps if a.id != app_id]
        self.save()

    def find_marketplace(self, name: str) -> CatalogApp | None:
        return next((a for a in self.apps if a.marketplace_name == name), None)
