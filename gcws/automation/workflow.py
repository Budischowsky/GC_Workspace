"""Automation workflows: a process chart of nodes and arrows.

Watched folder (-> Local copy) -> Method -> Report² -> Report -> Target folder. Each arrow may carry a filter;
only what passes it goes on (e.g. only the Word report to folder B). Workflows are JSON files
in ``<data>/automation/workflows``; the watcher reads them, the chart editor writes them.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws.automation import store

VERSION = 1

NODE_TYPES = {
    "source": "Watched folder",
    "copy": "Local copy",
    "method": "Method",
    "report2": "Report²",
    "report": "Report",
    "folder": "Target folder",
}
#: position of each node type in the chain; arrows only go to a later step
RANK = {"source": 0, "copy": 1, "method": 2, "report2": 3, "report": 4, "folder": 5}
ALLOWED = {("source", "method"), ("source", "copy"), ("copy", "method"), ("method", "report2"),
           ("method", "report"), ("report2", "report"), ("report", "folder")}

REPORT_KINDS = {"nias": "NIAS Report", "fingerprint": "Fingerprint Report",
                "total_extraction": "Total Extraction Report", "hs_screening": "HS-Screening Report",
                "quant": "Quantification Report", "template": "Template Report"}
#: files a Report node can produce: per sample, then per batch folder
FORMATS = {
    "xlsx": "Excel report",
    "docx": "Word report",
    "pdf": "PDF report",
    "dd": "Double determination workbook",
    "batch_docx": "Batch: Word report (all samples)",
    "batch_pdf": "Batch: PDF report (all samples)",
    "batch_xlsx": "Batch: Report² summary (Excel)",
}
SAMPLE_FORMATS = ("xlsx", "docx", "pdf", "dd")
BATCH_FORMATS = ("batch_docx", "batch_pdf", "batch_xlsx")
FORMAT_SHORT = {"xlsx": "Excel", "docx": "Word", "pdf": "PDF", "dd": "DD", "batch_docx": "Batch Word",
                "batch_pdf": "Batch PDF", "batch_xlsx": "Batch summary"}

#: report states an arrow can let through
STATUS_FILTERS = {
    "accepted": "Accepted (automatically or by the analyst)",
    "accepted_auto": "Accepted automatically",
    "accepted_manual": "Accepted by the analyst",
    "control": "Control needed",
}
BLANK_REQUIREMENTS = {
    "auto": "As the blank subtraction uses them",
    "blank": "Blank",
    "blank_istd": "Blank+ISTD",
    "both": "Blank and Blank+ISTD",
    "either": "Blank or Blank+ISTD",
    "none": "No blank needed",
}
OVERWRITE = {"version": "Keep both (add _2, _3 ...)", "overwrite": "Replace", "skip": "Keep the existing file"}

DEFAULT_PARAMS = {
    "source": {"folder": "", "depth": 1, "pattern": "*", "interval_min": 5, "quiet_min": 30,
               "stable_scans": 2, "min_age_min": 2, "process_existing": False, "ignore_older_days": 14},
    "copy": {"folder": ""},
    "method": {"method": "", "search": True, "istd_detect": True, "min_confidence": "high",
               "require_blank": "auto", "timeout_min": 30},
    "report2": {"rules": None, "auto_accept": True, "notify": True},
    "report": {"kind": "nias", "formats": ["xlsx", "docx"], "batch_when": "all_accepted", "keep_middle": False},
    "folder": {"path": "", "subfolder": "{batch}", "overwrite": "version", "allow_inside_source": False},
}
SUBFOLDER_TOKENS = ("{batch}", "{sample}", "{kind}", "{status}", "{date}", "{workflow}")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:8]


@dataclass
class Node:
    id: str
    type: str
    x: float = 0.0
    y: float = 0.0
    params: dict = field(default_factory=dict)

    def p(self, key: str):
        """Parameter ``key`` with the type's default."""
        if key in self.params:
            return self.params[key]
        return copy.deepcopy(DEFAULT_PARAMS.get(self.type, {}).get(key))

    @property
    def title(self) -> str:
        return NODE_TYPES.get(self.type, self.type)

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "x": round(self.x, 1), "y": round(self.y, 1),
                "params": copy.deepcopy(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(str(d.get("id") or new_id("n")), str(d.get("type") or ""), float(d.get("x") or 0),
                   float(d.get("y") or 0), dict(d.get("params") or {}))


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    filter: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "src": self.src, "dst": self.dst, "filter": copy.deepcopy(self.filter)}

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(str(d.get("id") or new_id("e")), str(d.get("src") or ""), str(d.get("dst") or ""),
                   dict(d.get("filter") or {}))


@dataclass
class Workflow:
    id: str = field(default_factory=lambda: new_id("wf"))
    name: str = "New workflow"
    enabled: bool = False
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    comment: str = ""
    created: str = ""
    modified: str = ""
    by: str = ""
    version: int = VERSION

    # -- graph ------------------------------------------------------------------------------

    def node(self, node_id: str) -> Optional[Node]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def edge(self, edge_id: str) -> Optional[Edge]:
        return next((e for e in self.edges if e.id == edge_id), None)

    def by_type(self, kind: str) -> list[Node]:
        return [n for n in self.nodes if n.type == kind]

    @property
    def source(self) -> Optional[Node]:
        found = self.by_type("source")
        return found[0] if found else None

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def add_node(self, node_type: str, x: float = 0.0, y: float = 0.0, **params) -> Node:
        n = Node(new_id("n"), node_type, x, y, params)
        self.nodes.append(n)
        return n

    def insert_copy(self, x: float = 0.0, y: float = 0.0, **params) -> Node:
        """Adds a Local copy step between the watched folder and the methods it feeds (the filters of
        those arrows stay on the arrows into the methods)."""
        first = not self.by_type("copy")
        cp = self.add_node("copy", x, y, **params)
        src = self.source
        if first and src is not None:
            moved = [e for e in self.outgoing(src.id) if self._type(e.dst) == "method"]
            for e in moved:
                e.src = cp.id
            if moved:
                self.connect(src.id, cp.id)
                # in line on the chart: the steps after the watched folder move one place to the right
                step = max(200.0, min(self.node(e.dst).x for e in moved) - src.x)
                for n in self.nodes:
                    if n is not cp and n.x > src.x:
                        n.x += step
                cp.x, cp.y = src.x + step, src.y
        return cp

    def connect(self, src: str, dst: str, **filt) -> Edge:
        e = Edge(new_id("e"), src, dst, {k: v for k, v in filt.items() if v not in (None, "", [])})
        self.edges.append(e)
        return e

    def can_connect(self, src: str, dst: str) -> str:
        """"" when an arrow ``src`` -> ``dst`` is allowed, else the reason."""
        a, b = self.node(src), self.node(dst)
        if a is None or b is None:
            return "unknown step"
        if src == dst:
            return "an arrow needs two different steps"
        if (a.type, b.type) not in ALLOWED:
            return f"{a.title} cannot pass to {b.title}"
        if any(e.src == src and e.dst == dst for e in self.edges):
            return "these steps are already connected"
        return ""

    def remove(self, item_id: str) -> None:
        self.nodes = [n for n in self.nodes if n.id != item_id]
        self.edges = [e for e in self.edges if e.id != item_id and e.src != item_id and e.dst != item_id]

    def methods(self) -> list[Node]:
        return self.by_type("method")

    def report_nodes(self, method_id: str) -> list[tuple[Node, list[Edge]]]:
        """Report nodes fed by the method ``method_id``, each with the arrows leading to it."""
        out = []
        for e in self.outgoing(method_id):
            n = self.node(e.dst)
            if n is None:
                continue
            if n.type == "report":
                out.append((n, [e]))
            elif n.type == "report2":
                for e2 in self.outgoing(n.id):
                    r = self.node(e2.dst)
                    if r is not None and r.type == "report":
                        out.append((r, [e, e2]))
        return out

    def review_node(self, method_id: str) -> Optional[Node]:
        """The Report² step after the method (None: its reports are not reviewed)."""
        for e in self.outgoing(method_id):
            n = self.node(e.dst)
            if n is not None and n.type == "report2":
                return n
        return None

    def source_edge(self, method_id: str) -> Optional[Edge]:
        return next((e for e in self.incoming(method_id) if self._type(e.src) == "source"), None)

    def feed(self, method_id: str) -> tuple[Optional[Node], list[Edge]]:
        """How the runs reach the method: the Local copy step on the way (None: straight from the
        watched folder) and the arrows from the watched folder to the method."""
        direct = self.source_edge(method_id)
        if direct is not None:
            return None, [direct]
        for e in self.incoming(method_id):
            cp = self.node(e.src)
            if cp is not None and cp.type == "copy":
                first = next((e0 for e0 in self.incoming(cp.id) if self._type(e0.src) == "source"), None)
                return cp, [x for x in (first, e) if x is not None]
        return None, []

    @property
    def copy_step(self) -> Optional[Node]:
        """The Local copy step (one per workflow), if any."""
        found = self.by_type("copy")
        return found[0] if found else None

    def _type(self, node_id: str) -> str:
        n = self.node(node_id)
        return n.type if n is not None else ""

    # -- persistence -------------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"format": "gcws-workflow", "version": self.version, "id": self.id, "name": self.name,
                "enabled": self.enabled, "comment": self.comment, "created": self.created,
                "modified": self.modified, "by": self.by,
                "nodes": [n.to_dict() for n in self.nodes], "edges": [e.to_dict() for e in self.edges]}

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
            raise ValueError("not a GC Workspace workflow")
        return cls(str(d.get("id") or new_id("wf")), str(d.get("name") or "Workflow"), bool(d.get("enabled")),
                   [Node.from_dict(n) for n in d.get("nodes") or []],
                   [Edge.from_dict(e) for e in d.get("edges") or []], str(d.get("comment") or ""),
                   str(d.get("created") or ""), str(d.get("modified") or ""), str(d.get("by") or ""),
                   int(d.get("version") or VERSION))

    def copy(self) -> "Workflow":
        return Workflow.from_dict(self.to_dict())

    @property
    def path(self) -> Path:
        return store.workflows_dir() / f"{self.id}.json"

    def save(self) -> Path:
        from gcws.core.audit import current_user
        now = datetime.now().isoformat(timespec="seconds")
        self.created = self.created or now
        self.modified, self.by = now, current_user()
        return store.atomic_write_json(self.path, self.to_dict())

    def digest(self) -> str:
        d = self.to_dict()
        for k in ("modified", "by", "created", "enabled"):
            d.pop(k, None)
        for n in d["nodes"]:
            n.pop("x", None)
            n.pop("y", None)
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def load(path) -> Workflow:
    return Workflow.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def list_workflows() -> list[Workflow]:
    out = []
    folder = store.workflows_dir()
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append(load(f))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda w: w.name.casefold())


def find(workflow_id: str) -> Optional[Workflow]:
    p = store.workflows_dir() / f"{workflow_id}.json"
    try:
        return load(p)
    except (OSError, ValueError, TypeError):
        return None


def delete(workflow_id: str) -> None:
    try:
        (store.workflows_dir() / f"{workflow_id}.json").unlink()
    except OSError:
        pass


# -- filters ----------------------------------------------------------------------------------

def passes(filt: dict, ctx: dict) -> bool:
    """True when ``ctx`` gets through an arrow with filter ``filt``.

    ``ctx`` keys: status (accepted_auto | accepted_manual | control), fmt, kind, name (sample),
    batch (folder name). A missing criterion, or a missing value in ``ctx``, lets it through."""
    filt = filt or {}
    status = filt.get("status") or []
    if status and ctx.get("status") is not None:
        st = ctx["status"]
        ok = st in status or ("accepted" in status and st in ("accepted_auto", "accepted_manual"))
        if not ok:
            return False
    formats = filt.get("formats") or []
    if formats and ctx.get("fmt") is not None and ctx["fmt"] not in formats:
        return False
    kinds = filt.get("kinds") or []
    if kinds and ctx.get("kind") is not None and ctx["kind"] not in kinds:
        return False
    for key in ("name", "batch"):
        pat = (filt.get(key) or "").strip()
        if pat and ctx.get(key) is not None and not any(
                fnmatch.fnmatch(str(ctx[key]).casefold(), p.strip().casefold()) for p in pat.split(";") if p.strip()):
            return False
    return True


def filter_text(filt: dict) -> str:
    """Short label of an arrow's filter ("" = everything passes)."""
    filt = filt or {}
    parts = []
    if filt.get("status"):
        parts.append(" / ".join({"accepted": "accepted", "accepted_auto": "auto-accepted",
                                 "accepted_manual": "accepted by analyst", "control": "control needed"}
                                .get(s, s) for s in filt["status"]))
    if filt.get("formats"):
        parts.append(" + ".join(FORMAT_SHORT.get(f, f) for f in filt["formats"]))
    if filt.get("kinds"):
        parts.append(" / ".join(REPORT_KINDS.get(k, k).replace(" Report", "") for k in filt["kinds"]))
    if filt.get("name"):
        parts.append(f"sample {filt['name']}")
    if filt.get("batch"):
        parts.append(f"folder {filt['batch']}")
    return ", ".join(parts)


def summary(node: Node) -> str:
    """One or two lines describing a node (shown on the chart)."""
    t = node.type
    if t == "source":
        folder = node.p("folder") or "(no folder)"
        return f"{folder}\nevery {node.p('interval_min')} min"
    if t == "copy":
        return f"{node.p('folder') or '(no folder)'}\nthe copies are kept"
    if t == "method":
        extra = []
        if node.p("search"):
            extra.append("search")
        if node.p("istd_detect"):
            extra.append("ISTD detection")
        req = node.p("require_blank")
        extra.append("blank: " + ("not needed" if req == "none" else "required"))
        return f"{node.p('method') or '(no method)'}\n" + ", ".join(extra)
    if t == "report2":
        rules = node.p("rules")
        n = len([r for r in rules if r.get("enabled")]) if isinstance(rules, list) else None
        return ("default rules" if n is None else f"{n} rules") + \
            ("" if node.p("auto_accept") else ", no automatic acceptance")
    if t == "report":
        return f"{REPORT_KINDS.get(node.p('kind'), node.p('kind'))}\n" + \
            ", ".join(FORMAT_SHORT.get(f, f) for f in node.p("formats") or [])
    if t == "folder":
        return f"{node.p('path') or '(no folder)'}\n{node.p('subfolder') or ''}".rstrip()
    return ""


# -- validation --------------------------------------------------------------------------------

@dataclass
class Issue:
    level: str                  # "error" | "warning"
    item: str                   # node or edge id ("" = the workflow)
    text: str


def word_available() -> bool:
    """Microsoft Word is registered (PDF output)."""
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
        return True
    except OSError:
        return False
    except ImportError:
        return False


def validate(wf: Workflow, *, method_names: Optional[list] = None, check_paths: bool = True,
             word: Optional[bool] = None, method_loader=None) -> list[Issue]:
    """Errors (the workflow cannot run) and warnings of ``wf``.

    ``method_loader(name) -> dict`` (optional) lets the check look into the processing methods."""
    import os
    out: list[Issue] = []
    err = lambda item, text: out.append(Issue("error", item, text))
    warn = lambda item, text: out.append(Issue("warning", item, text))
    sources = wf.by_type("source")
    if not sources:
        err("", "Add a watched folder (the start of the workflow).")
    elif len(sources) > 1:
        for s in sources[1:]:
            err(s.id, "A workflow watches one folder; use a second workflow for another folder.")
    ids = {n.id for n in wf.nodes}
    for n in wf.nodes:
        if n.type not in NODE_TYPES:
            err(n.id, f"Unknown step '{n.type}'.")
    seen = set()
    for e in wf.edges:
        a, b = wf.node(e.src), wf.node(e.dst)
        if a is None or b is None:
            err(e.id, "Arrow without a start or an end.")
            continue
        if (a.type, b.type) not in ALLOWED:
            err(e.id, f"{a.title} cannot pass to {b.title}.")
        if (e.src, e.dst) in seen:
            err(e.id, "Two arrows between the same steps.")
        seen.add((e.src, e.dst))
    # reachability from the source
    reach = set()
    todo = [s.id for s in sources[:1]]
    while todo:
        n = todo.pop()
        if n in reach:
            continue
        reach.add(n)
        todo += [e.dst for e in wf.outgoing(n) if e.dst in ids]
    for n in wf.nodes:
        if n.type != "source" and n.id not in reach:
            err(n.id, f"{n.title} is not connected to the watched folder.")
    src = sources[0] if sources else None
    src_folder = (src.p("folder") or "").strip() if src else ""
    if src is not None:
        if not src_folder:
            err(src.id, "Choose the folder to watch.")
        elif check_paths and not os.path.isdir(src_folder):
            # a note only: a network drive may be away for now (no VPN); the watcher waits for it
            warn(src.id, f"The watched folder does not exist or cannot be reached now: {src_folder}. The "
                         "watcher waits for it.")
        try:
            if float(src.p("interval_min")) < 1:
                err(src.id, "Check the folder at most once a minute.")
        except (TypeError, ValueError):
            err(src.id, "The check interval must be a number of minutes.")
        if not wf.outgoing(src.id):
            err(src.id, "Connect the watched folder to a method.")
    copies = wf.by_type("copy")
    for c in copies[1:]:
        err(c.id, "A workflow makes one local copy.")
    for c in copies[:1]:
        local = (c.p("folder") or "").strip()
        if not local:
            err(c.id, "Choose the local folder for the copies.")
        else:
            if src_folder and store.is_inside(local, src_folder):
                err(c.id, "The local folder lies inside the watched folder: the copies would be watched as new "
                          "data. Choose a folder outside it.")
            elif check_paths and not os.path.isdir(local):
                warn(c.id, f"The folder does not exist yet and will be created: {local}")
            if store.is_network(local):
                warn(c.id, "The local folder is on a network drive: processing from it is not faster.")
        if not any(wf._type(e.dst) == "method" for e in wf.outgoing(c.id)):
            warn(c.id, "The local copy passes to no method: the runs are only copied.")
    for m in wf.by_type("method"):
        if wf.source_edge(m.id) is not None and any(wf._type(e.src) == "copy" for e in wf.incoming(m.id)):
            err(m.id, "A method takes its runs from the watched folder or from the local copy, not from both.")
        name = (m.p("method") or "").strip()
        if not name:
            err(m.id, "Choose the processing method.")
        elif method_names is not None and name not in method_names:
            err(m.id, f"The processing method '{name}' does not exist (Method > Save current settings as Method...).")
        if not wf.report_nodes(m.id):
            warn(m.id, "No report follows this method: the samples are processed but nothing is reported.")
        if len([e for e in wf.outgoing(m.id) if wf._type(e.dst) == "report2"]) > 1:
            err(m.id, "A method passes to one Report² step.")
    for r2 in wf.by_type("report2"):
        if not any(wf._type(e.dst) == "report" for e in wf.outgoing(r2.id)):
            warn(r2.id, "Report² passes nothing on: connect it to a report.")
    if word is None and any(f in ("pdf", "batch_pdf") for r in wf.by_type("report") for f in r.p("formats") or []):
        word = word_available()
    for r in wf.by_type("report"):
        kind = r.p("kind")
        formats = [f for f in r.p("formats") or [] if f in FORMATS]
        if kind not in REPORT_KINDS:
            err(r.id, f"Unknown report '{kind}'.")
        if method_loader is not None:
            for m in wf.by_type("method"):
                if not any(n.id == r.id for n, _ in wf.report_nodes(m.id)) or not m.p("method"):
                    continue
                try:
                    sec = (method_loader(m.p("method")) or {}).get("sections") or {}
                except Exception:  # noqa: BLE001 - reported as missing above
                    continue
                mode = (sec.get("quant") or {}).get("mode", "nias_mgkg")
                tpl = sec.get("report_template") or {}
                if kind == "template" and not tpl.get("columns"):
                    err(r.id, f"The method '{m.p('method')}' has no report template: design one under Method > "
                              "Report template..., choose Use in method (or Save to method) and save the method.")
                if kind == "nias" and not sec.get("migration"):
                    warn(r.id, f"The method '{m.p('method')}' has no migration conditions: the NIAS report "
                               "cannot be made (enter them and save the method again).")
                from gcws.report.service import kind_fits
                if not kind_fits(kind, {"mode": mode}):
                    err(r.id, f"The method '{m.p('method')}' quantifies in the mode '{mode}': it cannot make "
                              f"the {REPORT_KINDS.get(kind, kind)}.")
        if not formats:
            err(r.id, "Choose at least one format (Word, Excel, PDF).")
        if "dd" in formats and kind != "nias":
            warn(r.id, "The double determination workbook is written for NIAS reports only.")
        if any(f in ("pdf", "batch_pdf") for f in formats) and word is False:
            warn(r.id, "PDF needs Microsoft Word on this PC; the PDF will be missing.")
        outs = [e for e in wf.outgoing(r.id) if wf._type(e.dst) == "folder"]
        if not outs:
            warn(r.id, "No target folder: the reports stay in Report² only.")
        for e in outs:
            want = e.filter.get("formats") or []
            if want and not set(want) & set(formats):
                err(e.id, "This arrow lets no file through: the report does not write these formats.")
            elif set(want) - set(formats):
                warn(e.id, "The arrow names formats the report does not write: "
                     + ", ".join(FORMAT_SHORT.get(f, f) for f in sorted(set(want) - set(formats))))
    for f in wf.by_type("folder"):
        path = (f.p("path") or "").strip()
        if not path:
            err(f.id, "Choose the target folder.")
            continue
        if src_folder and store.is_inside(path, src_folder) and not f.p("allow_inside_source"):
            err(f.id, "The target lies inside the watched folder; reports would be written into the raw data. "
                      "Choose another folder (or allow it in the target's settings).")
        if check_paths and not os.path.isdir(path):
            warn(f.id, f"The folder does not exist yet and will be created: {path}")
        if len(path) > 180:
            warn(f.id, "Long path: file names may exceed the Windows limit.")
        sub = f.p("subfolder") or ""
        bad = [t for t in re.findall(r"\{[^}]*\}", sub) if t not in SUBFOLDER_TOKENS]
        if bad:
            err(f.id, "Unknown placeholder(s) in the subfolder: " + ", ".join(bad))
        if not wf.incoming(f.id):
            warn(f.id, "Nothing arrives in this folder.")
    return out


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]
