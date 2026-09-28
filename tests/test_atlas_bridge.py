"""SpectrAtlas investigation tabs take GC Workspace's theme (no window is started)."""
from unittest.mock import patch

from gcws.identify.atlas_bridge import AtlasBridge


def test_research_tab_carries_the_theme(qapp, tmp_path):
    bridge = AtlasBridge()
    snapshot = {"spectrum": [[57.0, 40.0], [91.0, 100.0]], "rt": 11.876, "name": "Peak 1"}
    with patch.object(AtlasBridge, "_launch", lambda self: None), \
            patch("gcws.identify.atlas_bridge.threading.Thread") as thread:
        bridge.open_research(snapshot, {"sample": "A"}, tmp_path / "register.sqlite", 0, "neon")
        bridge.open_research(snapshot, {"sample": "A"}, tmp_path / "register.sqlite")
    bridge.timer.stop()
    first, second = bridge.outgoing.get_nowait(), bridge.outgoing.get_nowait()
    assert first["theme"] == "neon" and second["theme"] == "light"
    assert first["tab_id"] != second["tab_id"]
    assert thread.call_count == 1          # one window process for all tabs
