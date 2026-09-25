"""Audit trail: who changed what, when, from what to what."""
from __future__ import annotations

import getpass
from dataclasses import asdict, dataclass, field
from datetime import datetime


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class AuditRecord:
    action: str
    run: str = ""
    detail: str = ""
    before: str = ""
    after: str = ""
    reason: str = ""
    user: str = field(default_factory=current_user)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditRecord":
        return cls(**{k: d.get(k, "") for k in ("action", "run", "detail", "before", "after",
                                                 "reason", "user", "timestamp")})


class AuditLog:
    def __init__(self):
        self.records: list[AuditRecord] = []
        self.listeners = []

    def add(self, rec: AuditRecord) -> None:
        self.records.append(rec)
        for fn in list(self.listeners):
            fn(rec)

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.records]

    def load(self, items) -> None:
        self.records = [AuditRecord.from_dict(d) for d in items or []]
