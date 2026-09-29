"""Acquisition simulator: copies a finished batch into a watched folder run by run, the way the
instrument writes it (sequence log first, each .D growing, ``checksum.xml`` last, "Sequence
completed" at the end). For tests and for trying a workflow; the source is only read.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Iterator, Optional

from gcws.io import sequence as SQ


def _runs(src: Path, skip: set) -> list[Path]:
    seq = SQ.read_sequence(src, siblings=False)
    runs = [p for p in src.iterdir() if (p.is_dir() and p.suffix.lower() == ".d") or p.suffix.lower() == ".qgd"]
    runs = [p for p in runs if not any(p.name.casefold().startswith(s.casefold()) for s in skip)]
    return sorted(runs, key=lambda p: SQ.order_key(p, seq.stems or None))


def steps(src, dst_parent, *, chunks: int = 2, skip: tuple = (), write_log: bool = True) -> Iterator[str]:
    """Generator: every ``next()`` writes one more piece; yields what was written."""
    src = Path(src)
    dst = Path(dst_parent) / src.name
    dst.mkdir(parents=True, exist_ok=True)
    seq = SQ.read_sequence(src, siblings=False)
    log_text = ""
    if write_log and seq.tsv is not None:
        shutil.copy2(seq.tsv, dst / seq.tsv.name)
        if seq.log is not None:
            text = seq.log.read_text(encoding="utf-8-sig", errors="replace")
            cut = text.lower().find("sequence completed")
            log_text = text[:cut] if cut >= 0 else text
            (dst / seq.log.name).write_text(log_text, encoding="utf-8")
        yield "sequence log"
    for run in _runs(src, set(skip)):
        target = dst / run.name
        if run.is_file():
            data = run.read_bytes()
            part = max(1, len(data) // max(1, chunks))
            for i in range(0, len(data), part):
                with open(target, "ab") as f:
                    f.write(data[i:i + part])
                yield f"{run.name} {min(len(data), i + part)}/{len(data)}"
            continue
        files = [p for p in sorted(run.rglob("*")) if p.is_file()]
        last = [p for p in files if p.name.lower() == "checksum.xml"]
        body = [p for p in files if p not in last]
        per = max(1, -(-len(body) // max(1, chunks)))
        for i in range(0, len(body), per):
            for p in body[i:i + per]:
                out = target / p.relative_to(run)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, out)
            yield f"{run.name} part {i // per + 1}"
        for p in last:
            shutil.copy2(p, target / p.relative_to(run))
        yield f"{run.name} finished"
    if write_log and seq.log is not None:
        (dst / seq.log.name).write_text(log_text + "\n    Sequence completed\n", encoding="utf-8")
        yield "sequence completed"


def simulate_acquisition(src, dst_parent, *, delay_s: float = 30.0, chunks: int = 2, skip: tuple = (),
                         progress=print, sleep=time.sleep) -> Path:
    """Run :func:`steps` with ``delay_s`` between the pieces; returns the new batch folder."""
    for what in steps(src, dst_parent, chunks=chunks, skip=skip):
        progress(what)
        sleep(delay_s)
    return Path(dst_parent) / Path(src).name
