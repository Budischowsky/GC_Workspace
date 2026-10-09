"""Analyst consistency (spec §9): runs evaluated by two analysts, compared peak by peak. It shows how far the
"ground truth" itself agrees - the ceiling for any learned rule."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from gcws.learn.match import human_items


def _class(it) -> str:
    return "removed" if it.source == "removed" else (it.row_class or "unnamed")


def _keeps(it) -> bool:
    return it.source == "final" and it.row_class != "background"


def compare_pair(ev_a, ev_b, rt_tol: float = 0.01) -> dict:
    a, b = human_items(ev_a), human_items(ev_b)
    cand = sorted((abs(x.rt - y.rt), i, j) for i, x in enumerate(a) for j, y in enumerate(b) if abs(x.rt - y.rt) <= rt_tol)
    pa, pb, pairs = set(), set(), []
    for _, i, j in cand:
        if i not in pa and j not in pb:
            pa.add(i)
            pb.add(j)
            pairs.append((a[i], b[j]))
    keep = sum(_keeps(x) == _keeps(y) for x, y in pairs)
    cls = sum(_class(x) == _class(y) for x, y in pairs)
    named = [(x, y) for x, y in pairs if x.cas and y.cas]
    diffs = [{"rt": x.rt, "a_class": _class(x), "b_class": _class(y), "a_label": x.label, "b_label": y.label}
             for x, y in pairs if _class(x) != _class(y) or _keeps(x) != _keeps(y)]
    n = len(pairs)
    return {"paired": n, "only_a": len(a) - n, "only_b": len(b) - n,
            "keep_agree": keep / n if n else None, "class_agree": cls / n if n else None,
            "cas_agree": sum(x.cas == y.cas for x, y in named) / len(named) if named else None,
            "differences": sorted(diffs, key=lambda d: d["rt"])}


def run_consistency(root: Path, out_dir: Path) -> dict:
    from gcws.learn.corpus import scan
    from gcws.learn.workbook import parse_workbook
    by_run = defaultdict(list)
    for e in scan(Path(root)):
        by_run[e.run_dir].append(e)
    pairs = []
    for run_dir, entries in sorted(by_run.items()):
        if len(entries) < 2:
            continue
        ea, eb = sorted(entries, key=lambda e: e.analyst)[:2]
        a, b = parse_workbook(Path(ea.workbook)), parse_workbook(Path(eb.workbook))
        row = {"run": Path(run_dir).name, "batch": Path(ea.batch_dir).name, "a": ea.analyst, "b": eb.analyst,
               "comparable": bool(a.final) and bool(b.final)}
        if row["comparable"]:
            row.update(compare_pair(a, b))
        pairs.append(row)
    result = {"pairs": pairs, "totals": _totals(pairs)}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "consistency.md").write_text(_markdown(result), encoding="utf-8")
    return result


def _totals(pairs: list[dict]) -> dict:
    ok = [p for p in pairs if p["comparable"] and p["paired"]]
    n = sum(p["paired"] for p in ok)
    trans = Counter()
    for p in ok:
        trans.update(f"{d['a_class']} -> {d['b_class']}" for d in p["differences"])
    return {"pairs": len(ok), "paired_peaks": n,
            "keep_agree": sum(p["keep_agree"] * p["paired"] for p in ok) / n if n else None,
            "class_agree": sum(p["class_agree"] * p["paired"] for p in ok) / n if n else None,
            "transitions": dict(trans.most_common())}


def _f(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def _markdown(result: dict) -> str:
    t = result["totals"]
    lines = ["# Analyst consistency", "", "Runs evaluated by two analysts, compared peak by peak (RT ± 0.01 min).", "",
             "## Totals", "", f"{t['pairs']} comparable pairs, {t['paired_peaks']} peaks in both.", "",
             f"- keep / remove agreement: {_f(t['keep_agree'])}", f"- label class agreement: {_f(t['class_agree'])}",
             "", "Most frequent differences (first analyst -> second):", ""]
    lines += [f"- {k}: {n}" for k, n in list(t["transitions"].items())[:10]] or ["- none"]
    lines += ["", "## Pairs", "", "| batch | run | analysts | peaks in both | only one | keep | class | CAS |",
              "|---|---|---|---|---|---|---|---|"]
    for p in result["pairs"]:
        if not p["comparable"]:
            lines.append(f"| {p['batch']} | {p['run']} | {p['a']} / {p['b']} | not comparable (a worksheet without "
                         f"rows) | | | | |")
            continue
        lines.append(f"| {p['batch']} | {p['run']} | {p['a']} / {p['b']} | {p['paired']} | "
                     f"{p['only_a']} / {p['only_b']} | {_f(p['keep_agree'])} | {_f(p['class_agree'])} | "
                     f"{_f(p['cas_agree'])} |")
    return "\n".join(lines) + "\n"
