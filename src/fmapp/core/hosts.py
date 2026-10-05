"""Where sites live: this computer, or a server reached over SSH."""

from __future__ import annotations

import sys
import uuid
from dataclasses import asdict, dataclass, field

from fmapp.core.settings import Settings

LOCAL_ID = "local"


@dataclass(frozen=True)
class Host:
    name: str
    destination: str = ""  # SSH alias from ~/.ssh/config or user@host; "" = this computer
    port: int = 0  # 0 = SSH default / ssh_config
    fm_home: str = "~/frappe"  # Frappe Manager home on that machine
    production: bool = False  # every change asks for confirmation first
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def is_local(self) -> bool:
        return not self.destination

    def to_dict(self) -> dict:
        return asdict(self)


LOCAL = Host(name="This Mac" if sys.platform == "darwin" else "This computer", id=LOCAL_ID)


def all_hosts(settings: Settings) -> list[Host]:
    known = Host.__dataclass_fields__
    return [LOCAL, *(Host(**{k: v for k, v in h.items() if k in known}) for h in settings.hosts)]


def get(settings: Settings, host_id: str) -> Host:
    return next((h for h in all_hosts(settings) if h.id == host_id), LOCAL)
