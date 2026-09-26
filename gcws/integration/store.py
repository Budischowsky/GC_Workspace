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
        """The method new runs start with: the one a loaded processing method set, else built-in."""
        chosen = self._defaults().get(signal_kind)
        if chosen in self.methods:
            return chosen
        return nias_fid_method().name if signal_kind == "FID" else ms_method().name

    def _defaults_file(self) -> Path:
        return self.folder.parent / "method_defaults.json"

    def _defaults(self) -> dict:
        import json
        try:
            data = json.loads(self._defaults_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_default(self, signal_kind: str, name: str | None) -> None:
        """Make ``name`` the method new ``signal_kind`` runs start with (None: built-in again)."""
        import json
        data = self._defaults()
        if name:
            data[signal_kind] = name
        else:
            data.pop(signal_kind, None)
        self._defaults_file().parent.mkdir(parents=True, exist_ok=True)
        self._defaults_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
