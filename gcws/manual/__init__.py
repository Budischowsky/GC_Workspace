"""The user manual: Markdown chapters next to this file, shown by Help > User manual.

The chapters are plain Markdown so they can be edited and read on their own. Two things are
resolved when a chapter is loaded:

* ``{{IntegrationMethod.skim_mode}}`` becomes the default of that setting as the code has it
  now, so the manual cannot show an outdated number (``{{Features.gap_min_fraction %}}`` shows a
  fraction as a percentage);
* pictures are SVG files in ``img/``, drawn in the light theme's colours and recoloured by the
  viewer for the other themes.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parent

#: the table of contents: part -> chapter files (without ``.md``), in reading order
PARTS: list[tuple[str, list[str]]] = [
    ("Getting started", ["welcome", "window"]),
    ("Workflows", ["wf-overview", "wf-load", "wf-integrate", "wf-identify", "wf-quantify", "wf-double",
                   "wf-report", "wf-automation", "wf-methods"]),
    ("How it works", ["library-search", "double-determination"]),
    ("Reference", ["ref-menus", "ref-tools", "ref-panels", "ref-dialogs", "ref-settings"]),
]

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]+)(?:\.([A-Za-z_0-9]+))?\s*(%)?\s*\}\}")
_HEADING = re.compile(r"^(#{1,4})\s+(.*?)\s*#*\s*$")
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")


# -- defaults from the code -------------------------------------------------------------------

def _sources() -> dict:
    from gcws.features.model import Settings
    from gcws.integration.method import IntegrationMethod
    from gcws.ms.deconv import DeconvSettings
    from gcws.signal.blank import BlankOptions
    return {"IntegrationMethod": IntegrationMethod, "BlankOptions": BlankOptions,
            "DeconvSettings": DeconvSettings, "Features": Settings}


def _labels() -> dict:
    """Settings whose stored value is a key: the text the window shows for it."""
    from gcws.ui.dialogs.blank import MODES, SOURCES
    from gcws.ui.docks.events import DECONV_MODES
    return {("IntegrationMethod", "deconv_split"): DECONV_MODES,("BlankOptions", "source"): SOURCES, ("BlankOptions", "mode_fid"): MODES,
            ("BlankOptions", "mode_ms"): MODES, ("BlankOptions", "align"): {"auto": "on", "off": "off"}}


def _show(value) -> str:
    if value is None:
        return "automatic"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_show(v) for v in value)
    return str(value)


def value(name: str, attr: str | None = None, percent: bool = False) -> str:
    """The text a placeholder stands for; ``KeyError`` when the code has no such setting."""
    if attr is None:
        import gcws
        return {"version": gcws.__version__}[name]
    cls = _sources()[name]
    for f in dataclasses.fields(cls):
        if f.name == attr:
            default = f.default if f.default is not dataclasses.MISSING else                 f.default_factory() if f.default_factory is not dataclasses.MISSING else None
            if percent:
                return f"{default * 100:g} %"
            labels = _labels().get((name, attr), {})
            return (labels.get(default) if isinstance(default, str) else None) or _show(default)
    raise KeyError(f"{name}.{attr}")


def placeholders(text: str) -> list[tuple[str, str | None]]:
    return [(m.group(1), m.group(2)) for m in _PLACEHOLDER.finditer(text)]


def expand(text: str) -> str:
    def sub(m):
        try:
            return value(m.group(1), m.group(2), bool(m.group(3)))
        except KeyError:
            return "?"
    return _PLACEHOLDER.sub(sub, text)


# -- chapters ---------------------------------------------------------------------------------

def anchor(heading: str) -> str:
    """The name a link uses for a heading: ``## Gap filling`` -> ``gap-filling``."""
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    return re.sub(r"[\s]+", "-", re.sub(r"[^\w\s²-]", "", text))


@dataclass
class Section:
    level: int
    title: str
    text: str = ""                                # the section's own text, without sub-sections

    @property
    def anchor(self) -> str:
        return anchor(self.title)


@dataclass
class Chapter:
    slug: str
    part: str
    source: str                                   # the Markdown as written
    sections: list[Section] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.sections[0].title if self.sections else self.slug

    @property
    def markdown(self) -> str:
        return expand(self.source)

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.anchor == name), None)


def _sections(text: str) -> list[Section]:
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else _HEADING.match(line)
        if m:
            out.append(Section(len(m.group(1)), m.group(2)))
        elif out:
            out[-1].text += line + "\n"
    return out


@lru_cache(maxsize=1)
def chapters() -> tuple[Chapter, ...]:
    out = []
    for part, slugs in PARTS:
        for slug in slugs:
            source = (DIR / f"{slug}.md").read_text(encoding="utf-8")
            out.append(Chapter(slug, part, source, _sections(expand(source))))
    return tuple(out)


def chapter(slug: str) -> Chapter | None:
    return next((c for c in chapters() if c.slug == slug), None)


def links(text: str) -> list[tuple[bool, str]]:
    """``(is picture, target)`` of every Markdown link in ``text``."""
    return [(bool(m.group(1)), m.group(3)) for m in _LINK.finditer(text)]


def split_target(target: str, current: str) -> tuple[str, str]:
    """``chapter.md#section`` or ``#section`` -> (chapter slug, anchor)."""
    path, _, frag = target.partition("#")
    slug = Path(path).stem if path else current
    return slug, frag


# -- search -----------------------------------------------------------------------------------

@dataclass
class Hit:
    chapter: Chapter
    section: Section
    snippet: str
    score: float


def plain(text: str) -> str:
    """Markdown without its marks, for searching and snippets."""
    text = _LINK.sub(lambda m: "" if m.group(1) else m.group(2), text)
    text = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", " ", text, flags=re.M)          # table rules
    text = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", text, flags=re.M)                    # list marks
    text = re.sub(r"[`*_>#|]", " ", text)
    return " ".join(text.split())


def search(query: str, limit: int = 60) -> list[Hit]:
    """Sections that contain every word of ``query``; headings count more than text."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    hits = []
    for ch in chapters():
        for sec in ch.sections:
            title, body = sec.title.lower(), plain(sec.text)
            low = body.lower()
            if not all(w in title or w in low for w in words):
                continue
            score = sum(10.0 * (w in title) + min(low.count(w), 5) for w in words) + (4 - sec.level)
            at = min((low.find(w) for w in words if w in low), default=0)
            start = max(0, at - 50)
            snippet = ("… " if start else "") + body[start:at + 110].strip() + (" …" if at + 110 < len(body)
                                                                               else "")
            hits.append(Hit(ch, sec, snippet, score))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit]
