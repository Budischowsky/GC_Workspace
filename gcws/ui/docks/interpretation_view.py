"""Readable rendering of an :class:`~gcws.ms.interpret.Interpretation` (HTML cards)."""
from __future__ import annotations

import html

from PySide6.QtWidgets import QTextBrowser

from gcws.ui import theme

LEVEL_OF = {"high": "ok", "medium": "warn", "low": "neutral",
            "probable": "ok", "possible": "warn", "uncertain": "neutral",
            "strong": "ok", "fair": "warn", "weak": "neutral",
            "clear": "ok", "likely": "warn"}
CHECK_ICON = {"ok": "✔", "warn": "⚠", "bad": "✖", "info": "ℹ"}


def _e(text) -> str:
    return html.escape(str(text))


def _card(title: str, body: str, level: str = "accent") -> str:
    fg = theme.LEVELS.get(level, theme.LEVELS["accent"])[0]
    return (f'<table width="100%" cellspacing="0" cellpadding="7" style="margin-bottom:8px; '
            f'border:1px solid {theme.BORDER}; background:{theme.SURFACE};">'
            f'<tr><td style="border-left:4px solid {fg};">'
            f'<div style="color:{theme.MUTED}; font-size:8.5pt; font-weight:600; letter-spacing:0.5px;">'
            f'{_e(title.upper())}</div><div>{body}</div></td></tr></table>')


def _bullets(items) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return "<ul style='margin:2px 0 0 -18px;'>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def render(res, spectrum_label: str = "") -> str:
    """HTML for an interpretation result (``None``: placeholder)."""
    if res is None:
        return (f"<p style='color:{theme.MUTED}'>Select a peak or right-click a chromatogram to interpret "
                "its mass spectrum.</p>")
    parts = []
    head = f"<div style='color:{theme.MUTED}'>{_e(spectrum_label)}</div>" if spectrum_label else ""
    if res.classes:
        c = res.classes[0]
        body = (f"<div style='font-size:12pt; font-weight:600; color:{theme.TEXT}'>{_e(c.label)} "
                f"{theme.chip_html(c.level + ' confidence', LEVEL_OF.get(c.level, 'neutral'))}</div>"
                f"<div style='color:{theme.MUTED}'>{_e(c.group)}</div>"
                + _bullets(c.evidence[:6]))
        if c.contra:
            body += f"<div style='color:{theme.WARN}'>against: {_e('; '.join(c.contra))}</div>"
        if c.examples:
            body += f"<div style='color:{theme.MUTED}; margin-top:3px'>Typical: {_e(c.examples)}</div>"
        if c.note:
            body += f"<div style='color:{theme.MUTED}'>{_e(c.note)}</div>"
        parts.append(_card("Most likely substance class", body, LEVEL_OF.get(c.level, "accent")))
    else:
        parts.append(_card("Substance class", "<b>No characteristic class pattern found.</b> "
                           "Use the library search; the molecular ion and element hints below still apply.",
                           "neutral"))
    if res.compounds:
        rows = "".join(
            f"<tr><td valign='top'>{theme.chip_html(h.level, LEVEL_OF.get(h.level, 'neutral'))}</td>"
            f"<td><b>{_e(h.name)}</b><br><span style='color:{theme.MUTED}'>CAS {_e(h.cas or '-')}"
            f"{'  ·  MW ' + str(h.mw) if h.mw else ''}  ·  key ions {_e(', '.join(map(str, h.key_ions)))}</span></td>"
            f"<td align='right' valign='top'>{100 * h.score:.0f}</td></tr>" for h in res.compounds)
        parts.append(_card("Substance clues (typical ion patterns)",
                           f"<table width='100%' cellpadding='3' style='vertical-align:top'>{rows}</table>", "info"))
    # molecular ion and elements
    if res.m is not None:
        m = res.m
        body = (f"<div style='font-size:11pt'><b>m/z {m.mz}</b> "
                f"{theme.chip_html(m.level, LEVEL_OF.get(m.level, 'neutral'))}</div>" + _bullets(m.evidence))
        if res.m_alternatives:
            body += (f"<div style='color:{theme.MUTED}'>Alternatives if the top ions belong to another compound: "
                     + ", ".join(f"m/z {a.mz}" for a in res.m_alternatives) + "</div>")
        parts.append(_card("Molecular ion M⁺·", body, LEVEL_OF.get(m.level, "neutral")))
    elif res.m_note:
        parts.append(_card("Molecular ion M⁺·", _e(res.m_note), "neutral"))
    el = []
    for h in res.isotopes:
        el.append(f"{theme.chip_html(h.label, LEVEL_OF.get(h.certainty, 'neutral'))} {_e(h.text)}")
    if res.carbon:
        n, lo, hi = res.carbon
        el.append(f"<b>≈ C{n}</b> <span style='color:{theme.MUTED}'>(C{lo}–C{hi}, from the M+1 isotope peak)</span>")
    if not res.isotopes and res.m is not None:
        el.append(f"<span style='color:{theme.MUTED}'>no Cl / Br / S / Si isotope pattern at the molecular ion</span>")
    if el:
        parts.append(_card("Elements", "<br>".join(el), "accent"))
    if res.formulas:
        rows = "".join(f"<tr><td><b>{_e(f.formula)}</b></td><td align='right'>RDBE {f.rdbe:g}</td>"
                       f"<td align='right'>isotope fit {100 * f.fit:.0f} %</td></tr>" for f in res.formulas)
        parts.append(_card("Formula suggestions for M (unit resolution)",
                           f"<table width='100%' cellpadding='2'>{rows}</table>"
                           f"<div style='color:{theme.MUTED}'>Ranked by isotope pattern, chemical rules and the "
                           "interpreted class; confirm by accurate mass or a reference.</div>", "neutral"))
    if res.series or res.losses:
        body = ""
        if res.series:
            body += "<b>Ion series</b>" + _bullets(
                f"{s.label}: {', '.join(map(str, s.members[:8]))} ({100 * s.fraction:.0f} % of the ion current)"
                for s in res.series)
        if res.losses:
            body += "<b>Losses from M⁺·</b>" + _bullets(
                f"M−{l.loss} → m/z {l.to_mz} ({l.rel:.0f} %): {l.meaning}" for l in res.losses)
        parts.append(_card("Fragmentation", body, "neutral"))
    if len(res.classes) > 1:
        rows = "".join(f"<tr><td>{theme.chip_html(c.level, LEVEL_OF.get(c.level, 'neutral'))}</td>"
                       f"<td>{_e(c.label)}</td><td align='right'>{100 * c.score:.0f}</td></tr>"
                       for c in res.classes[1:5])
        parts.append(_card("Other possible classes", f"<table width='100%' cellpadding='2'>{rows}</table>", "neutral"))
    if res.checks or res.warnings:
        lines = [f"<span style='color:{theme.status_color(lvl if lvl != 'info' else 'info').name()}'>"
                 f"{CHECK_ICON.get(lvl, '•')}</span> {_e(t)}" for lvl, t in res.checks]
        lines += [f"<span style='color:{theme.WARN}'>⚠</span> {_e(w)}" for w in res.warnings]
        parts.append(_card("Checks", "<br>".join(lines), "warn" if res.warnings else "info"))
    parts.append(f"<p style='color:{theme.FAINT}; font-size:8pt'>Clues from interpretation rules and typical EI "
                 "ion patterns, not an identification: confirm with the library search or a reference standard. "
                 "Laboratory rules can be added in interpret_rules.json.</p>")
    return (f"<html><body style='font-family:Segoe UI, sans-serif; font-size:9.5pt; color:{theme.TEXT}'>"
            f"{head}{''.join(parts)}</body></html>")


class InterpretationView(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.show_result(None)

    def show_result(self, res, label: str = "") -> None:
        self.setHtml(render(res, label))
