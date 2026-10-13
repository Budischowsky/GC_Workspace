"""gcws.core.settings (settings.json, CASINFO path) and the import direction core -> ui."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gcws import paths
from gcws.core import settings as S


@pytest.fixture
def folders(tmp_path, monkeypatch):
    out = {}
    for name in ("DATA", "RESOURCES", "ROOT"):
        out[name] = tmp_path / name.lower()
        out[name].mkdir()
        monkeypatch.setattr(paths, name, out[name])
    return out


def test_settings_round_trip_and_broken_file(folders):
    assert S.load() == {}                                  # no file yet
    S.save({"standard_cas_path": "x.xlsx", "ä": 1})
    assert S.load() == {"standard_cas_path": "x.xlsx", "ä": 1}
    S.settings_file().write_text("{broken", encoding="utf-8")
    assert S.load() == {}


def test_cas_path_relative_in_the_first_folder_that_has_it(folders):
    assert S.cas_path() is None                            # default name, nowhere
    (folders["ROOT"] / "CASINFO.xlsx").write_bytes(b"")
    assert S.cas_path() == folders["ROOT"] / "CASINFO.xlsx"
    (folders["RESOURCES"] / "CASINFO.xlsx").write_bytes(b"")
    assert S.cas_path() == folders["RESOURCES"] / "CASINFO.xlsx"


def test_cas_path_absolute_and_odd_values(folders, tmp_path):
    own = tmp_path / "own.xlsx"
    S.save({"standard_cas_path": str(own)})
    assert S.cas_path() is None                            # absolute, missing
    own.write_bytes(b"")
    assert S.cas_path() == own
    S.save({"standard_cas_path": None})
    assert S.cas_path() is None


def test_the_three_casinfo_readers_agree(folders):
    from gcws.quant import substance_flags
    from gcws.report import assemble
    (folders["DATA"] / "CASINFO.xlsx").write_bytes(b"")
    assert assemble.cas_path() == substance_flags.casinfo_path() == folders["DATA"] / "CASINFO.xlsx"


#: modules outside gcws.ui that may still import it (the app start, the tray watcher's icons and the
#: manual's mode names); everything else depends on core only
UI_ALLOWED = {"app.py", "automation/watcher.py", "manual/__init__.py"}


def test_only_the_allowed_modules_outside_ui_import_it():
    root = Path(__file__).resolve().parents[1] / "gcws"
    found = set()
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if rel.startswith("ui/"):
            continue
        if re.search(r"^\s*(from|import)\s+gcws\.ui\b", f.read_text(encoding="utf-8"), re.M):
            found.add(rel)
    assert found <= UI_ALLOWED, sorted(found - UI_ALLOWED)
