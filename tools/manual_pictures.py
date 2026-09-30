"""Draws the pictures of the user manual (``gcws/manual/img/*.svg``).

The pictures use the light theme's colours; the manual window swaps them for the current theme
(``gcws.ui.dialogs.manual.themed_svg``). Only what Qt's SVG renderer draws is used: plain shapes
with attributes, no style sheets, no markers.

    .venv/Scripts/python.exe tools/manual_pictures.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gcws.ui.theme import LIGHT as C  # noqa: E402

OUT = ROOT / "gcws" / "manual" / "img"
FONT = "Segoe UI, Arial, sans-serif"
W = 760


class Picture:
    def __init__(self, height: int, width: int = W):
        self.w, self.h = width, height
        self.items: list[str] = []

    def add(self, s: str) -> None:
        self.items.append(s)

    def text(self, x, y, s, size=13, color=None, anchor="start", bold=False, italic=False):
        s = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
                 f'fill="{color or C["TEXT"]}" text-anchor="{anchor}"'
                 + (' font-weight="600"' if bold else "") + (' font-style="italic"' if italic else "")
                 + f'>{s}</text>')

    def lines(self, x, y, rows, size=13, color=None, anchor="start", gap=1.35, bold_first=False):
        for n, row in enumerate(rows):
            self.text(x, y + n * size * gap, row, size, color, anchor, bold=bold_first and n == 0)

    def box(self, x, y, w, h, fill=None, stroke=None, r=8, width=1.2):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" ry="{r}" '
                 f'fill="{fill or C["SURFACE_ALT"]}" stroke="{stroke or C["BORDER_STRONG"]}" '
                 f'stroke-width="{width}"/>')

    def label_box(self, x, y, w, h, rows, fill=None, stroke=None, size=13, color=None, bold_first=True):
        self.box(x, y, w, h, fill, stroke)
        top = y + h / 2 - (len(rows) - 1) * size * 1.35 / 2 + size * 0.36
        self.lines(x + w / 2, top, rows, size, color, "middle", bold_first=bold_first)

    def line(self, x1, y1, x2, y2, color=None, width=1.4, dash=""):
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color or C["MUTED"]}" '
                 f'stroke-width="{width}"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")

    def arrow(self, x1, y1, x2, y2, color=None, width=1.6, head=7.0, dash=""):
        color = color or C["MUTED"]
        a = math.atan2(y2 - y1, x2 - x1)
        bx, by = x2 - head * math.cos(a), y2 - head * math.sin(a)
        self.line(x1, y1, bx, by, color, width, dash)
        p = [(x2, y2), (bx - head * 0.55 * math.sin(a), by + head * 0.55 * math.cos(a)),
             (bx + head * 0.55 * math.sin(a), by - head * 0.55 * math.cos(a))]
        self.add('<polygon points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in p) + f'" fill="{color}"/>')

    def poly(self, points, color=None, width=1.8, fill="none", opacity=1.0, dash=""):
        self.add('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
                 + f'" fill="{fill}" fill-opacity="{opacity}" stroke="{color or C["ACCENT"]}" '
                 f'stroke-width="{width}" stroke-linejoin="round"'
                 + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")

    def area(self, points, fill, opacity=0.35):
        self.add('<polygon points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
                 + f'" fill="{fill}" fill-opacity="{opacity}" stroke="none"/>')

    def circle(self, x, y, r, fill, stroke="none"):
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{stroke}"/>')

    def save(self, name: str) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        body = "\n".join(self.items)
        (OUT / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}">\n{body}\n</svg>\n', encoding="utf-8", newline="\n")


def trace(x0, x1, base, peaks, step=1.5, tail=0.0):
    """Points of a chromatogram: ``peaks`` = [(centre x, height, sigma)], drawn upwards from ``base``."""
    pts = []
    x = x0
    while x <= x1 + 1e-6:
        y = 0.0
        for c, h, s in peaks:
            d = x - c
            sig = s * (1 + tail) if d > 0 else s
            y += h * math.exp(-0.5 * (d / sig) ** 2)
        pts.append((x, base - y))
        x += step
    return pts


def between(points, a, b):
    return [p for p in points if a <= p[0] <= b]


# -- library search ---------------------------------------------------------------------------------

def search_two_steps():
    p = Picture(250)
    p.label_box(6, 80, 118, 70, ["Spectrum", "of one peak"], C["ACCENT_SOFT"], C["ACCENT"])
    p.arrow(126, 115, 152, 115)
    # step 1
    p.box(154, 30, 250, 170, C["SURFACE_ALT"])
    p.text(279, 54, "Step 1 - Shortlist", 14, C["ACCENT_TEXT"], "middle", bold=True)
    p.lines(279, 78, ["a quick comparison with", "every reference spectrum"], 12.5, C["MUTED"], "middle")
    for n in range(9):                                   # the whole library: many small marks
        for m in range(18):
            keep = (n * 18 + m) % 23 == 0
            p.add(f'<rect x="{172 + m * 12:.0f}" y="{112 + n * 8:.0f}" width="9" height="5" rx="1" ry="1" '
                  f'fill="{C["ACCENT"] if keep else C["BORDER_STRONG"]}"/>')
    p.text(279, 218, "e.g. 1.8 million references", 12, C["MUTED"], "middle")
    p.arrow(406, 115, 482, 115)
    p.text(443, 103, "the 300 best", 11.5, C["ACCENT_TEXT"], "middle", bold=True)
    p.text(443, 132, "each way", 11.5, C["MUTED"], "middle")
    # step 2
    p.box(484, 30, 166, 170, C["SURFACE_ALT"])
    p.text(567, 54, "Step 2 - Exact score", 14, C["ACCENT_TEXT"], "middle", bold=True)
    p.lines(567, 78, ["only the shortlist is", "scored precisely"], 12.5, C["MUTED"], "middle")
    for n in range(6):
        w = 120 - n * 15
        p.add(f'<rect x="504" y="{112 + n * 13}" width="{w}" height="8" rx="2" ry="2" '
              f'fill="{C["ACCENT"] if n < 3 else C["ACCENT_SOFT2"]}"/>')
    p.arrow(652, 115, 674, 115)
    p.label_box(676, 80, 78, 70, ["Hit list", "best first"], C["OK_SOFT"], C["OK"])
    p.save("search-two-steps.svg")


def search_standard_vs_fast():
    p = Picture(300)
    # standard
    p.text(190, 22, "Standard search", 14, C["TEXT"], "middle", bold=True)
    p.text(190, 40, "one peak after the other", 12.5, C["MUTED"], "middle")
    for n in range(4):
        y = 62 + n * 52
        name = f"Peak {n + 1}" if n < 3 else "Peak 375"
        if n == 3:
            p.text(55, y - 8, "...", 14, C["MUTED"], "middle")
        p.label_box(15, y, 80, 34, [name], C["ACCENT_SOFT"], C["ACCENT"], 12.5, bold_first=False)
        p.arrow(97, y + 17, 150, y + 17)
        p.box(152, y, 200, 34, C["NEUTRAL_SOFT"], C["BORDER_STRONG"])
        p.text(252, y + 21, "the whole library, again", 12, C["MUTED"], "middle")
    p.text(190, 286, "library read 375 times", 12.5, C["BAD"], "middle", bold=True)
    p.line(380, 12, 380, 290, C["BORDER_STRONG"], 1, "4 4")
    # fast
    p.text(575, 22, "Fast search", 14, C["TEXT"], "middle", bold=True)
    p.text(575, 40, "all peaks together", 12.5, C["MUTED"], "middle")
    for n in range(5):
        p.box(405 + n * 5, 84 + n * 7, 84, 34, C["ACCENT_SOFT"], C["ACCENT"])
    p.text(467, 134, "all 375 peaks", 12.5, C["ACCENT_TEXT"], "middle", bold=True)
    p.arrow(515, 124, 556, 124)
    for n in range(4):
        y = 66 + n * 30
        p.box(558, y, 190, 24, C["NEUTRAL_SOFT"], C["BORDER_STRONG"], 5)
        p.text(653, y + 16, f"library block {n + 1}" if n < 3 else "... last block", 12, C["MUTED"], "middle")
    p.lines(575, 214, ["each block is compared with all peaks", "in one calculation, on every processor core"],
            12.5, C["MUTED"], "middle")
    p.text(575, 286, "library read once", 12.5, C["OK"], "middle", bold=True)
    p.save("search-standard-vs-fast.svg")


def search_floor():
    p = Picture(280)
    x0, y0, y1 = 70, 228, 40
    span = y0 - y1 - 6
    p.line(x0, y0, 738, y0, C["MUTED"])
    p.line(x0, y0, x0, y1 - 8, C["MUTED"])
    p.lines(8, 44, ["better", "fit"], 12, C["MUTED"])
    p.arrow(28, 84, 28, 66, C["MUTED"], 1.2, 5)
    import random
    rnd = random.Random(4)
    floors = [0.0, 0.30, 0.50, 0.62]                        # the bar when each block starts
    for b in range(4):
        bx = x0 + 16 + b * 166
        bar = floors[b]
        p.box(bx - 8, y1 - 6, 158, span + 6, C["SURFACE_ALT"], C["BORDER"], 6, 1)
        p.text(bx + 71, y0 + 18, f"block {b + 1}", 12, C["MUTED"], "middle")
        for k in range(16):
            v = min(0.97, abs(rnd.gauss(0.22, 0.2)) + (0.5 if k in (3, 11) else 0.0))
            h = v * span
            p.add(f'<rect x="{bx + k * 9}" y="{y0 - h:.1f}" width="6" height="{h:.1f}" '
                  f'fill="{C["ACCENT"] if v >= bar else C["BORDER_STRONG"]}"/>')
        if b:
            p.line(bx - 8, y0 - bar * span, bx + 150, y0 - bar * span, C["WARN"], 2)
        else:
            p.text(bx + 71, y1 + 12, "no bar yet: all kept", 11.5, C["MUTED"], "middle")
    p.text(x0 + 16 + 3 * 166 + 146, y0 - floors[3] * span - 8, "the bar", 12.5, C["WARN"], "end", bold=True)
    p.text(x0 + 8, 264, "coloured: could still be among the 300 best - kept", 12.5, C["ACCENT_TEXT"])
    p.text(x0 + 330, 264, "grey: clearly below the bar - dropped", 12.5, C["MUTED"])
    p.save("search-floor.svg")


# -- double determination -----------------------------------------------------------------------------

def dd_overview():
    p = Picture(150)
    steps = [("1", "Remove", "the drift"), ("2", "Pair", "the peaks"), ("3", "Fill", "the gaps"),
             ("4", "One name", "per substance"), ("5", "Same", "boundaries"), ("6", "Traffic", "light")]
    w, gap = 108, 20
    for n, (num, a, b) in enumerate(steps):
        x = 8 + n * (w + gap)
        p.box(x, 40, w, 74, C["ACCENT_SOFT"] if n < 5 else C["OK_SOFT"], C["ACCENT"] if n < 5 else C["OK"])
        p.circle(x + w / 2, 40, 12, C["ACCENT"] if n < 5 else C["OK"])
        p.text(x + w / 2, 44.5, num, 12.5, C["SURFACE_ALT"], "middle", bold=True)
        p.text(x + w / 2, 76, a, 13, C["TEXT"], "middle", bold=True)
        p.text(x + w / 2, 94, b, 13, C["TEXT"], "middle", bold=True)
        if n < 5:
            p.arrow(x + w + 2, 77, x + w + gap - 2, 77)
    p.text(8, 140, "Determination A + determination B of one sample", 12.5, C["MUTED"])
    p.text(752, 140, "one list of substances", 12.5, C["MUTED"], "end")
    p.save("dd-overview.svg")


def dd_drift():
    p = Picture(300)
    peaks = [(130, 70, 7), (260, 45, 7), (300, 30, 7), (470, 80, 8), (640, 50, 8)]
    shifts = [6, 9, 10, 15, 20]                                # B elutes later, more so late in the run
    ca, cb = C["ACCENT"], C["BAD"]
    p.text(10, 18, "Before: B is a little late, and later in the run more than at the start", 13, bold=True)
    a = trace(30, 740, 120, peaks)
    b = trace(30, 740, 120, [(c + s, h * 0.9, w) for (c, h, w), s in zip(peaks, shifts)])
    p.line(30, 120, 740, 120, C["BORDER_STRONG"], 1)
    p.poly(a, ca)
    p.poly(b, cb)
    p.text(702, 40, "A", 13, ca, bold=True)
    p.text(722, 40, "B", 13, cb, bold=True)
    for (c, h, w), s in zip(peaks, shifts):
        p.line(c, 128, c + s, 128, C["WARN"], 2)
        p.text(c + s / 2, 143, f"{s * 0.4:.0f} s", 11, C["WARN"], "middle")
    p.text(10, 176, "After: B is moved onto A's time axis, piece by piece between the anchor peaks", 13, bold=True)
    p.line(30, 278, 740, 278, C["BORDER_STRONG"], 1)
    p.poly(trace(30, 740, 278, peaks), ca)
    p.poly(trace(30, 740, 278, [(c + 0.8, h * 0.9, w) for c, h, w in peaks]), cb, dash="5 3")
    for n in (0, 3, 4):
        c, h, w = peaks[n]
        p.circle(c, 278 - h - 9, 3.5, C["OK"])
    p.text(740, 196, "green dots: anchor peaks", 12, C["OK"], "end")
    p.save("dd-drift.svg")


def dd_gapfill():
    p = Picture(330)
    ca, cb = C["ACCENT"], C["BAD"]
    # A: a clear small peak
    p.text(10, 18, "Determination A: the peak was integrated", 13, bold=True)
    pa = [(110, 62, 9), (250, 30, 8)]
    a = trace(30, 360, 110, pa)
    p.line(30, 110, 360, 110, C["BORDER_STRONG"], 1)
    p.area(between(a, 226, 274) + [(274, 110), (226, 110)], ca, 0.3)
    p.poly(a, ca)
    p.text(250, 70, "found", 12, ca, "middle", bold=True)
    # B: the same peak, smaller, not integrated
    p.text(10, 158, "Determination B: nothing integrated at that time", 13, bold=True)
    pb = [(114, 58, 9), (254, 13, 8)]
    b = trace(30, 360, 250, pb)
    p.line(30, 250, 360, 250, C["BORDER_STRONG"], 1)
    p.add(f'<rect x="238" y="176" width="32" height="76" fill="{C["WARN_SOFT"]}" stroke="{C["WARN"]}" '
          f'stroke-dasharray="4 3"/>')
    p.poly(b, cb)
    p.lines(254, 272, ["expected here", "(search window)"], 11.5, C["WARN"], "middle")
    # the checks
    x = 410
    p.text(x, 18, "Is the substance really there in B?", 13, bold=True)
    checks = [("1", "Its typical ions rise and fall together", "at least 3 ions with the same shape"),
              ("2", "The spectrum there matches A's", "similarity 0.7 or more"),
              ("3", "The signal shows a real peak", "signal-to-noise 3 or more")]
    for n, (num, head, sub) in enumerate(checks):
        y = 36 + n * 58
        p.box(x, y, 340, 48, C["SURFACE_ALT"])
        p.circle(x + 22, y + 24, 11, C["ACCENT"])
        p.text(x + 22, y + 28.5, num, 12, C["SURFACE_ALT"], "middle", bold=True)
        p.text(x + 44, y + 21, head, 12.5, C["TEXT"], bold=True)
        p.text(x + 44, y + 38, sub, 12, C["MUTED"])
    p.arrow(x + 90, 212, x + 90, 240, C["OK"])
    p.arrow(x + 250, 212, x + 250, 240, C["BAD"])
    p.label_box(x, 244, 165, 60, ["all three: gap filled", "B gets the peak (yellow)"], C["OK_SOFT"], C["OK"], 12)
    p.label_box(x + 175, 244, 165, 60, ["otherwise: not detectable", "never area 0 (red)"], C["BAD_SOFT"],
                C["BAD"], 12)
    p.save("dd-gapfill.svg")


def dd_boundaries():
    p = Picture(262)
    ca, cb = C["ACCENT"], C["BAD"]
    for n, (title, sub, color, end, note) in enumerate([
            ("A - the reference", "(the better signal-to-noise)", ca, 196, ["end at the baseline"]),
            ("B - as integrated", "", cb, 138, ["end set too early:", "part of the tail is missing"]),
            ("B - proposed", "", cb, 196, ["same distance from the apex", "as in A"])]):
        x0 = 10 + n * 252
        base = 182
        pts = trace(x0 + 10, x0 + 236, base, [(x0 + 100, 105, 13)], tail=1.3)
        start, stop = x0 + 62, x0 + end
        p.text(x0 + 4, 20, title, 12.5, bold=True)
        if sub:
            p.text(x0 + 4, 37, sub, 11.5, C["MUTED"])
        p.line(x0 + 10, base, x0 + 236, base, C["BORDER_STRONG"], 1)
        seg = between(pts, start, stop)
        top_start = seg[0][1] if seg else base
        top_stop = seg[-1][1] if seg else base
        p.area([(start, top_start)] + seg + [(stop, top_stop)], color, 0.28)
        p.poly(pts, color)
        for xx in (start, stop):
            p.line(xx, base + 6, xx, base - 30, C["TEXT"], 1.6)
        p.line(x0 + 100, base + 4, x0 + 100, base + 12, C["MUTED"], 1)
        p.text(x0 + 100, base + 25, "apex", 11, C["MUTED"], "middle")
        if n == 1:
            p.arrow(stop + 5, base - 40, x0 + 196, base - 40, C["WARN"], 1.6, 6)
        p.lines(x0 + 4, 232, note, 11.5, C["MUTED"])
    p.save("dd-boundaries.svg")


def main():
    for draw in (search_two_steps, search_standard_vs_fast, search_floor, dd_overview, dd_drift, dd_gapfill,
                 dd_boundaries):
        draw()
    print(f"{len(list(OUT.glob('*.svg')))} pictures in {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
