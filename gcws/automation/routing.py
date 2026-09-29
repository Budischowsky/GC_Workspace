"""Which report files go to which target folder (the arrows' filters)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws.automation import store
from gcws.automation.workflow import Workflow, passes


@dataclass
class Delivery:
    folder_node: str
    report_node: str
    fmt: str
    src: Path
    dst: Path


def deliveries(wf: Workflow, method_id: str, ctx: dict, files: dict, tokens: Optional[dict] = None) -> list[Delivery]:
    """Files of one job (or batch) to copy, following every path method -> [Report²] -> report -> folder.

    ``ctx``: status, kind (per report node, filled in here), name, batch. ``files``:
    ``{report node id: {format: path}}``. Every arrow on the path must let the file through."""
    out = []
    for report, path_edges in wf.report_nodes(method_id):
        produced = files.get(report.id) or {}
        for fmt, src in produced.items():
            if src is None:
                continue
            c = dict(ctx, fmt=fmt, kind=report.p("kind"))
            if not all(passes(e.filter, c) for e in path_edges):
                continue
            for e in wf.outgoing(report.id):
                folder = wf.node(e.dst)
                if folder is None or folder.type != "folder" or not passes(e.filter, c):
                    continue
                dst_dir = target_dir(folder.params, dict(tokens or {}, kind=report.p("kind"),
                                                         status=ctx.get("status", "")), wf.name)
                out.append(Delivery(folder.id, report.id, fmt, Path(src), dst_dir / Path(src).name))
    return out


def target_dir(params: dict, tokens: dict, workflow_name: str = "") -> Path:
    """The folder a file goes to: the target path plus the subfolder with its placeholders."""
    base = Path(params.get("path") or "")
    sub = params.get("subfolder") if "subfolder" in params else "{batch}"
    status = {"accepted_auto": "accepted", "accepted_manual": "accepted", "control": "control needed"}
    values = {"batch": tokens.get("batch", ""), "sample": tokens.get("sample", ""), "kind": tokens.get("kind", ""),
              "status": status.get(tokens.get("status", ""), tokens.get("status", "")),
              "date": tokens.get("date") or datetime.now().strftime("%Y-%m-%d"), "workflow": workflow_name}
    text = re.sub(r"\{(\w+)\}", lambda m: store.safe_name(values.get(m.group(1), m.group(0))), sub or "")
    parts = [p for p in re.split(r"[\\/]+", text) if p.strip(" .")]
    return base.joinpath(*parts) if parts else base


def resolve_collision(dst: Path, policy: str) -> Optional[Path]:
    """Where to write ``dst`` when it exists: None = skip (policy "skip")."""
    dst = Path(dst)
    if not dst.exists() or policy == "overwrite":
        return dst
    if policy == "skip":
        return None
    n = 2
    while True:
        cand = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
        if not cand.exists():
            return cand
        n += 1
