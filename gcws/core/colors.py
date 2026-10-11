"""Run colours without Qt: the light theme's run colours and the shades of a double determination.

``gcws.ui.theme`` takes its light run colours and shade steps from here; the workspace without
Qt (unattended processing) colours runs with them, as the main window does in the light theme.
"""
from __future__ import annotations

#: run colours: colour-blind-safe (Okabe-Ito first), all dark enough for thin lines on white
RUN_COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#8C564B", "#6A3D9A",
              "#B8860B", "#E7298A", "#1F3A93", "#11A579", "#A6761D", "#7F3C8D", "#3969AC", "#666666"]
#: the light theme's icon outline and fill colours, which the shades mix towards
INK, PAPER = "#33424D", "#FFFFFF"
#: rank -> (towards, share): B paler, C deeper, D paler still; further ranks ("PAPER", 0.75)
SHADES = {1: ("PAPER", 0.45), 2: ("INK", 0.3), 3: ("PAPER", 0.65)}


def _rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def shade(color: str, rank: int, ink: str = INK, paper: str = PAPER) -> str:
    """``color`` for the ``rank``-th determination of a double (N-fold) determination: the first as it
    is, the second paler (towards the background), the third deeper, ... (``#rrggbb``, lower case)."""
    if rank <= 0:
        return color
    towards, share = SHADES.get(rank, ("PAPER", 0.75))
    a, b = _rgb(color), _rgb(paper if towards == "PAPER" else ink)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * share) for x, y in zip(a, b))
