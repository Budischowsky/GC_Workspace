"""Integration methods on disk (data/methods/*.json) plus built-in defaults."""
from __future__ import annotations

import re
from pathlib import Path

from gcws import paths
from gcws.integration.method import IntegrationMethod, ms_method, nias_fid_method


def _file_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\- ]+", "_", name).strip() or "method"


class MethodStore:
    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else paths.methods_dir()
        self.methods: dict[str, IntegrationMethod] = {}
        self.reload()

    def reload(self) -> None:
        self.methods = {m.name: m for m in (nias_fid_method(), ms_method())}
        builtin = paths.RESOURCES / "methods"
        for folder in (builtin, self.folder):
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.json")):
                try:
                    m = IntegrationMethod.load(f)
                    self.methods[m.name] = m
                except Exception:  # noqa: BLE001 - a broken file must not stop the app
                    continue

    def names(self) -> list[str]:
        return sorted(self.methods, key=str.casefold)

    def get(self, name: str) -> IntegrationMethod:
        m = self.methods.get(name)
        return m.copy() if m else nias_fid_method()

    def save(self, method: IntegrationMethod) -> Path:
        self.methods[method.name] = method.copy()
        path = self.folder / (_file_name(method.name) + ".json")
        method.save(path)
        return path

    def delete(self, name: str) -> None:
        path = self.folder / (_file_name(name) + ".json")
        if path.exists():
            path.unlink()
        self.reload()

    def default_name(self, signal_kind: str) -> str:
        return nias_fid_method().name if signal_kind == "FID" else ms_method().name
