"""gcws.core.workspace: the workspace without Qt (unattended processing) and its Qt adapter."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import run_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_signal_receivers_per_workspace():
    from gcws.core.workspace import Workspace
    a, b, got = Workspace(), Workspace(), []
    slot = lambda rid: got.append(("a", rid))
    a.runChanged.connect(slot)
    b.runChanged.connect(lambda rid: got.append(("b", rid)))
    a.runChanged.emit("x")
    a.runChanged.disconnect(slot)
    a.runChanged.emit("y")
    assert got == [("a", "x")]


def test_undo_stack_steps_and_macros():
    from gcws.core.undo import UndoGroup, UndoStack, ValueCommand
    box = {"v": 0}
    cmd = lambda v: ValueCommand(f"set {v}", lambda: box["v"], lambda x: box.update(v=x), v)
    stack = UndoStack()
    stack.push(cmd(1))
    stack.beginMacro("two at once")
    stack.push(cmd(2))
    stack.push(cmd(3))
    stack.endMacro()
    assert box["v"] == 3 and stack.count() == 2
    stack.undo()
    assert box["v"] == 1 and stack.count() == 2 and stack.canRedo()
    stack.redo()
    assert box["v"] == 3
    stack.undo()
    stack.push(cmd(5))                                     # a new step drops what was undone
    assert box["v"] == 5 and stack.count() == 2 and not stack.canRedo()
    group = UndoGroup()
    group.addStack(stack)
    group.setActiveStack(stack)
    assert group.activeStack() is stack
    group.removeStack(stack)
    assert group.activeStack() is None


def test_core_shades_match_the_light_theme(qapp):
    from gcws.core import colors
    from gcws.ui import theme
    assert theme.LIGHT["RUN_COLORS"] == colors.RUN_COLORS
    assert (theme.LIGHT["INK"], theme.LIGHT["PAPER"]) == (colors.INK, colors.PAPER)
    for tokens in theme.THEMES.values():
        for c in tokens["RUN_COLORS"]:
            for rank in range(6):
                saved = theme.INK, theme.PAPER
                theme.INK, theme.PAPER = tokens["INK"], tokens["PAPER"]
                try:
                    want = theme.shade(c, rank)
                finally:
                    theme.INK, theme.PAPER = saved
                assert colors.shade(c, rank, tokens["INK"], tokens["PAPER"]) == want


def test_core_and_qt_workspace_integrate_the_same(qapp, samples):
    from gcws.core.workspace import Workspace as Core
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace as Qt
    out = []
    for cls in (Core, Qt):
        ws = cls()
        for prefix in ("07_", "08_"):
            ws.add_run(load_run(run_dir(prefix)))
        ws.recompute_quant()
        out.append([(s.color, s.shade, {k: r.digest for k, r in s.results.items()}) for s in ws.states()]
                   + [sorted(ws.quant_result.rows)])
    assert out[0] == out[1]


def test_headless_needs_no_qapplication(samples, tmp_path):
    """Load, integrate, quantify and save a project in a fresh interpreter: no QApplication, no gcws.ui."""
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from gcws.automation import headless as H
        from gcws.core import project as P
        ws = H.new_workspace()
        errors = H.add_runs(ws, [{str(run_dir("07_"))!r}])
        assert not errors, errors
        st = ws.states()[0]
        assert st.results["FID"].peaks and st.undo is not None
        ws.recompute_quant()
        P.save(ws, {str(tmp_path / "p.gcproj")!r})
        from PySide6.QtCore import QCoreApplication
        assert QCoreApplication.instance() is None
        ui = sorted(m for m in sys.modules if m == "gcws.ui" or m.startswith("gcws.ui."))
        assert not ui, ui
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=600)
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stderr[-3000:]
