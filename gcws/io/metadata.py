"""Run metadata from the acquisition files of an Agilent ``.D`` folder."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunMetadata:
    folder: Path
    sample_name: str = ""
    sample_id: str = ""
    method: str = ""
    acquired: str = ""
    position: str = ""
    injection_volume: str = ""
    operator: str = ""
    instrument: str = ""
    comment: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.sample_name or self.folder.stem


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def read_sample_info(path: Path) -> dict[str, str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    out: dict[str, str] = {}
    for f in root.iter("Field"):
        name = _text(f.find("Name"))
        if name:
            out[name] = _text(f.find("Value"))
    return out


def read_metadata(folder) -> RunMetadata:
    folder = Path(folder)
    meta = RunMetadata(folder=folder)
    acq = folder / "AcqData"
    info = read_sample_info(acq / "sample_info.xml")
    meta.fields = info
    meta.sample_name = info.get("Sample Name", "")
    meta.sample_id = info.get("Sample ID", "")
    meta.method = Path(info.get("Method", "")).name if info.get("Method") else ""
    meta.position = info.get("Sample Position", "")
    meta.injection_volume = next((v for k, v in info.items() if k.startswith("Inj Vol")), "")
    meta.operator = info.get("OperatorName", "")
    meta.comment = info.get("Comment", "")
    meta.acquired = info.get("AcqTime", "")
    contents = acq / "Contents.xml"
    if contents.exists():
        try:
            root = ET.parse(contents).getroot()
            meta.acquired = _text(root.find("AcquiredTime")) or meta.acquired
            meta.instrument = _text(root.find("InstrumentName"))
        except ET.ParseError:
            pass
    if not meta.method:
        methods = sorted(acq.glob("*.M")) if acq.is_dir() else []
        if methods:
            meta.method = methods[0].name
    if not meta.method:
        meta.method = _method_from_acqmeth(folder / "acqmeth.txt")
    return meta


def _method_from_acqmeth(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()[:4000]
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("latin-1")
    except UnicodeDecodeError:
        return ""
    m = re.search(r"([^\\\s]+\.M)\b", text)
    return m.group(1) if m else ""
