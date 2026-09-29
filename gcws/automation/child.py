"""The job process: ``python -m gcws --process-job <spec.json>`` / ``--batch-report <spec.json>``.

Started by the watcher for one sample (or one batch report). It has no window and never asks
anything: every outcome, including a crash, ends in ``result.json`` beside the spec. The process
ends with ``os._exit`` so a Word/Excel automation thread that hangs cannot keep it alive.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path

EXIT_OK, EXIT_FAILED, EXIT_RETRY = 0, 2, 3


def _setup(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(out_dir / "job.log"), level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace Job")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(out_dir / "settings"))
    app = QApplication.instance() or QApplication(["gcws-job"])
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:  # noqa: BLE001 - no Office automation on this PC
        pass
    return app


def process(spec_path: str, kind: str = "job") -> int:
    from gcws.automation import pipeline as PL
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_dir = Path(spec["out_dir"])
    _setup(out_dir)
    log = logging.getLogger("gcws.job")
    log.info("start %s %s", kind, spec_path)
    try:
        if kind == "batch":
            from gcws.automation.batch import batch_report
            files, warnings = batch_report(spec["batch_name"], spec["entries"], out_dir, spec.get("formats") or [],
                                           pdf_timeout=float(spec.get("pdf_timeout", 180)))
            result = PL.JobResult("accepted_auto", "", {spec["report_node"]: files}, warnings=warnings)
        else:
            result = PL.run_job(spec, progress=log.info)
        code = EXIT_RETRY if result.state == PL.RETRY else EXIT_OK
    except PermissionError as exc:
        text = traceback.format_exc()
        log.error(text)
        result, code = PL.JobResult(PL.RETRY, f"file in use: {exc}"), EXIT_RETRY
    except Exception as exc:  # noqa: BLE001
        text = traceback.format_exc()
        log.error(text)
        result, code = PL.JobResult(PL.FAILED, f"{type(exc).__name__}: {exc}"), EXIT_FAILED
    PL.write_result(out_dir, result)
    log.info("end %s: %s %s", kind, result.state, result.reason)
    logging.shutdown()
    return code


def main(argv) -> None:
    """Dispatch from ``gcws.app.main``; never returns."""
    kind = "batch" if argv[1] == "--batch-report" else "job"
    try:
        code = process(argv[2], kind)
    except Exception:  # noqa: BLE001 - even the spec could not be read
        traceback.print_exc()
        code = EXIT_FAILED
    sys.stdout.flush() if sys.stdout else None
    os._exit(code)
