"""The review list (spec §9): every disagreement between program and analyst with its evidence, and the user's
verdicts. A verdict "program" or "both" makes that disagreement not count in the next baseline and fit; "analyst"
confirms the analyst (the program should change)."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

VERDICTS = ("analyst", "program", "both")


@dataclass
class ReviewItem:
    id: str
    batch: str
    run: str
    analyst: str
    type: str
    rt: Optional[float]
    analyst_side: dict = field(default_factory=dict)
    program_side: dict = field(default_factory=dict)
    workbook: str = ""


def item_id(batch: str, run: str, analyst: str, kind: str, rt) -> str:
    key = f"{batch}|{run}|{analyst}|{kind}|{'' if rt is None else round(rt, 2)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _folder(out_dir: Path) -> Path:
    return Path(out_dir) / "review"


def _analyst_side(it) -> dict:
    return {"decision": it.decision, "label": it.label, "cas": it.cas, "conc_mgkg": it.conc_mgkg, "area": it.area}


def _program_side(peak: Optional[dict], line: Optional[tuple]) -> dict:
    out = {}
    if peak is not None:
        out.update({k: peak.get(k) for k in ("name", "cas", "score", "class_hint", "in_blank", "blank_ratio", "area")})
        out["hits"] = peak.get("hits") or []
    if line is not None:
        out.update(report_line=line[2], report_cas=line[1])
    return out


def build_items(root: Path, out_dir: Path, method_name: str = "NIAS", process_fn=None) -> list[ReviewItem]:
    from gcws.learn.propose import load_cached_runs
    from gcws.learn.score import client_lines, score_run
    items = []
    batches = load_cached_runs(Path(root), Path(out_dir), method_name, process_fn, progress=lambda t: None)
    for runs in batches.values():
        for r in runs:
            lines = client_lines(r.prog)
            for d in score_run(r.items, r.prog, r.pairs).details:
                it = r.items[d["human"]] if d["human"] is not None else None
                peak = r.prog.peaks[d["program"]] if d["program"] is not None else None
                line = lines[d["line"]] if d["line"] is not None else None
                items.append(ReviewItem(
                    id=item_id(r.batch, Path(r.run_dir).name, r.analyst, d["type"], d["rt"]), batch=r.batch,
                    run=Path(r.run_dir).name, analyst=r.analyst, type=d["type"], rt=d["rt"],
                    analyst_side=_analyst_side(it) if it is not None else {},
                    program_side=_program_side(peak, line), workbook=r.workbook))
    return items


def write_review(items: list[ReviewItem], out_dir: Path) -> Path:
    folder = _folder(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    _atomic(folder / "items.json", json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=1))
    verdicts = load_verdicts(out_dir)
    by_type = Counter(i.type for i in items)
    by_batch = Counter(i.batch for i in items)
    decided = Counter(verdicts[i.id]["verdict"] for i in items if i.id in verdicts)
    lines = ["# Review list", "", f"{len(items)} disagreements; {sum(decided.values())} with a verdict "
             f"({', '.join(f'{k}: {n}' for k, n in decided.items()) or 'none yet'}).", "",
             "Open them in Report² > View > Learning review..., or edit verdicts.json.", "",
             "## Per type", "", "| type | count |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {n} |" for k, n in by_type.most_common()]
    lines += ["", "## Per batch", "", "| batch | count |", "|---|---|"]
    lines += [f"| {k} | {n} |" for k, n in by_batch.most_common()]
    md = folder / "review.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md


def load_items(out_dir: Path) -> Optional[list[ReviewItem]]:
    path = _folder(out_dir) / "items.json"
    if not path.is_file():
        return None
    return [ReviewItem(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def load_verdicts(out_dir: Path) -> dict:
    path = _folder(out_dir) / "verdicts.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_verdict(out_dir: Path, item_id_: str, verdict: str, note: str = "") -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    item = next((i for i in load_items(out_dir) or [] if i.id == item_id_), None)
    if item is None:
        raise KeyError(f"no review item {item_id_}")
    from gcws.core.audit import current_user
    verdicts = load_verdicts(out_dir)
    verdicts[item_id_] = {"verdict": verdict, "note": note, "by": current_user(),
                          "at": datetime.datetime.now().isoformat(timespec="seconds"),
                          "batch": item.batch, "run": item.run, "analyst": item.analyst, "type": item.type,
                          "rt": item.rt}
    folder = _folder(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    _atomic(folder / "verdicts.json", json.dumps(verdicts, ensure_ascii=False, indent=1))


def run_verdicts(out_dir: Path, batch: str, run: str, analyst: str) -> dict:
    """(type, RT rounded to 2) -> verdict for one evaluated run, as score_run takes them."""
    return {(v["type"], None if v.get("rt") is None else round(v["rt"], 2)): v["verdict"]
            for v in load_verdicts(out_dir).values()
            if v.get("batch") == batch and v.get("run") == run and v.get("analyst") == analyst}
