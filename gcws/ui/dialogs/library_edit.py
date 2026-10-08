"""Edit library: add the current mass spectrum (with name, CAS, formula, RI ...) to a library,
browse its entries, change or delete them. The backend is :mod:`gcws.identify.library_edit`."""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from gcws.identify import library_edit as LE
from gcws.ui import theme


def entry_from_spectrum(win) -> dict:
    """What the spectrum panel shows, as pre-filled library entry fields."""
    sp = win.spectrum
    ws = win.ws
    out = {"peaks": list(sp.points()), "note": sp.spec.note if sp.spec is not None else ""}
    st, peak = sp.target_peak()
    if st is None or sp.spec is None:
        return out
    from gcws.core.keys import is_fid
    spec = sp.spec
    out["rt"] = round(float(spec.rt), 3)
    ident = st.ident_set(ws.signal_key).for_peak(peak) if peak is not None else None
    if ident is not None and ident.name and not ident.name.lower().startswith("unknown"):
        out["name"] = ident.name
        out["cas"] = ident.cas
        out["formula"] = ident.formula
        top = ident.hits[0] if ident.hits else {}
        if top.get("mw"):
            out["mw"] = top.get("mw")
    ladder = (ws.quant.get("ri") or {}).get("ladder") or {}
    if ladder:
        try:
            import gc_qc
            out["ri"] = round(gc_qc.retention_index(spec.rt + st.delay_value,
                                                    {int(k): float(v) for k, v in ladder.items()}))
        except Exception:  # noqa: BLE001 - RI is optional
            pass
    meta = st.run.meta
    column = " / ".join(x for x in ((meta.method if meta else ""), (meta.instrument if meta else "")) if x)
    out["column"] = column
    fid = f", FID {spec.rt + st.delay_value:.3f} min" if st.run.fid is not None else ""
    out["source"] = f"{st.name} ({st.run.path.name}), MS {spec.rt:.3f} min{fid}"
    if is_fid(ws.signal_key) and peak is not None and not out.get("name"):
        out["name"] = ""
    interp = getattr(sp, "interp", None)
    if interp is not None and getattr(interp, "m", None) is not None and "mw" not in out:
        out["mw"] = interp.m.mz
    return out


class EditLibraryDialog(QDialog):
    def __init__(self, win, entry: dict | None = None):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Edit library")
        self.resize(1100, 720)
        self.entry = dict(entry or {})
        self.records: list = []
        self.editing = None                  # index of the entry being edited, None = new entry
        self._busy = False
        s = QSettings()
        self.l2n_exe = LE.find_lib2nist(None, s.value("prefs/lib2nist", "") or "")

        # -- library -----------------------------------------------------------------------
        self.library = QComboBox()
        self.library.setMinimumWidth(320)
        self.library.currentIndexChanged.connect(self._library_changed)
        new_lib = QPushButton("New library...")
        new_lib.clicked.connect(self.new_library)
        self.lib_note = theme.hint("", True)
        top = QHBoxLayout()
        top.addWidget(QLabel("Library"))
        top.addWidget(self.library, 1)
        top.addWidget(new_lib)

        # -- new / edited entry --------------------------------------------------------------
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._show_spectrum)
        self.synonym = QLineEdit()
        self.cas = QLineEdit()
        self.cas.setPlaceholderText("e.g. 117-81-7")
        self.formula = QLineEdit()
        self.formula.setPlaceholderText("e.g. C24H38O4")
        self.formula.textChanged.connect(self._formula_changed)
        self.mw = QLineEdit()
        self.mw.setPlaceholderText("nominal, from the formula")
        self.ri = QLineEdit()
        self.rt = QLineEdit()
        self.column = QLineEdit()
        self.column.setPlaceholderText("column / GC method")
        self.source = QLineEdit()
        self.comment = QPlainTextEdit()
        self.comment.setMaximumHeight(70)
        self.check = QLabel()
        self.check.setObjectName("warning")
        form = QFormLayout()
        form.addRow("Name *", self.name)
        form.addRow("Synonym", self.synonym)
        form.addRow("CAS", self.cas)
        form.addRow("Formula", self.formula)
        form.addRow("MW", self.mw)
        form.addRow("Retention index", self.ri)
        form.addRow("RT [min]", self.rt)
        form.addRow("Column / method", self.column)
        form.addRow("Source", self.source)
        form.addRow("Comment", self.comment)
        form.addRow("", self.check)
        from gcws.ui.docks.spectrum import StickPlot
        self.plot = StickPlot()
        self.plot.setToolTip("The spectrum that will be stored")
        self.trim = QDoubleSpinBox()
        self.trim.setRange(0, 100)
        self.trim.setDecimals(1)
        self.trim.setSuffix(" ‰")
        self.trim.setValue(float(s.value("library_edit/trim", 1.0)))
        self.trim.setToolTip("Leave out ions below this share of the base peak (noise)")
        self.trim.valueChanged.connect(self._show_spectrum)
        self.spec_note = theme.hint("", True)
        right = QVBoxLayout()
        right.addWidget(self.plot, 1)
        tr = QHBoxLayout()
        tr.addWidget(QLabel("Keep ions from"))
        tr.addWidget(self.trim)
        tr.addWidget(QLabel("of the base peak"))
        tr.addStretch(1)
        right.addLayout(tr)
        right.addWidget(self.spec_note)
        take = QPushButton("Take current spectrum")
        take.setToolTip("The spectrum the Mass spectrum panel shows now (select a peak or right-click a "
                        "chromatogram while this window stays open)")
        take.clicked.connect(self.take_current)
        imp = QPushButton("Import MSP...")
        imp.setToolTip("A spectrum from an .msp file (with its name, CAS, formula ...)")
        imp.clicked.connect(self.import_msp)
        paste = QPushButton("Paste")
        paste.setToolTip("An MSP record or 'm/z abundance' pairs from the clipboard")
        paste.clicked.connect(self.paste_spectrum)
        typed = QPushButton("Type ions...")
        typed.setToolTip("Enter or correct the ions as 'm/z abundance' lines")
        typed.clicked.connect(self.type_ions)
        src = QHBoxLayout()
        for w in (take, imp, paste, typed):
            src.addWidget(w)
        src.addStretch(1)
        right.addLayout(src)
        self.add_btn = QPushButton("Add to library")
        theme.set_primary(self.add_btn)
        self.add_btn.clicked.connect(self.save_entry)
        self.cancel_edit = QPushButton("New entry instead")
        self.cancel_edit.clicked.connect(lambda: self._fill_form(self.entry, editing=None))
        self.cancel_edit.hide()
        entry_tab = QWidget()
        h = QHBoxLayout(entry_tab)
        left = QVBoxLayout()
        left.addLayout(form)
        b = QHBoxLayout()
        b.addStretch(1)
        b.addWidget(self.cancel_edit)
        b.addWidget(self.add_btn)
        left.addLayout(b)
        h.addLayout(left, 1)
        h.addLayout(right, 1)

        # -- browse ------------------------------------------------------------------------------
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or CAS ...")
        self.filter.textChanged.connect(self._fill_table)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "CAS", "Formula", "MW", "RI"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show_record())
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_selected())
        self.browse_plot = StickPlot()
        edit = QPushButton("Edit...")
        edit.clicked.connect(self.edit_selected)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_selected)
        reload_ = QPushButton("Reload")
        reload_.clicked.connect(self.load_entries)
        browse_tab = QWidget()
        bl = QVBoxLayout(browse_tab)
        bl.addWidget(self.filter)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(self.browse_plot)
        split.setSizes([620, 420])
        bl.addWidget(split, 1)
        bb = QHBoxLayout()
        bb.addWidget(reload_)
        bb.addStretch(1)
        bb.addWidget(edit)
        bb.addWidget(delete)
        bl.addLayout(bb)

        self.tabs = QTabWidget()
        self.tabs.addTab(entry_tab, "New entry")
        self.tabs.addTab(browse_tab, "Entries")
        self.tabs.currentChanged.connect(lambda i: self.load_entries() if i == 1 and not self.records else None)
        self.status = QLabel()
        self.status.setObjectName("hint")
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.lib_note)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)

        self._fill_libraries()
        self._fill_form(self.entry, editing=None)

    # -- libraries -----------------------------------------------------------------------------

    def _lib2nist(self):
        return LE.Lib2Nist(self.l2n_exe) if self.l2n_exe else None

    def _fill_libraries(self, select: str = ""):
        self.libs = LE.own_libraries()
        self.library.blockSignals(True)
        self.library.clear()
        for lb in self.libs:
            kind = {"nist": "NIST user library" if lb.writable else "NIST, read-only", "msp": "MSP",
                    "agilent": "Agilent .L, read-only"}.get(lb.kind, "read-only")
            self.library.addItem(f"{lb.name}   ({kind})", lb.name)
            if not lb.writable:
                item = self.library.model().item(self.library.count() - 1)
                item.setEnabled(False)
                item.setToolTip(lb.note)
        remembered = select or QSettings().value("library_edit/last", "")
        default = LE.default_library(self.libs, remembered)
        if default is not None:
            self.library.setCurrentIndex(self.library.findData(default.name))
        self.library.blockSignals(False)
        self._library_changed()

    def current_library(self):
        name = self.library.currentData()
        return next((lb for lb in self.libs if lb.name == name), None)

    def _library_changed(self, *_):
        lb = self.current_library()
        self.records = []
        self.table.setRowCount(0)
        if lb is None:
            self.lib_note.setText("No library that can take entries: create one with 'New library...' (or add "
                                  "one under Identify > Libraries...).")
        elif lb.kind == "nist" and self.l2n_exe is None:
            self.lib_note.setText("NIST Lib2NIST (lib2nist.exe) was not found; it is needed to change NIST user "
                                  "libraries. Set its path in Edit > Preferences.")
        else:
            extra = f"  Lib2NIST: {self.l2n_exe}" if lb.kind == "nist" else ""
            self.lib_note.setText(f"{lb.path}  -  {lb.note}. Before every change the library is copied to "
                                  f"the backup folder.{extra}")
        self._update_buttons()
        if self.tabs.currentIndex() == 1:
            self.load_entries()

    def new_library(self):
        name, ok = QInputDialog.getText(self, "New library", "Name of the new library:")
        if not ok or not name.strip():
            return
        try:
            info = LE.create_library(name.strip(), None, self._lib2nist())
        except LE.LibraryError as exc:
            QMessageBox.warning(self, "New library", str(exc))
            return
        self.libs.append(info)
        self.library.blockSignals(True)
        self.library.addItem(f"{info.name}   (new, created with its first entry)", info.name)
        self.library.setCurrentIndex(self.library.count() - 1)
        self.library.blockSignals(False)
        self._library_changed()
        self.tabs.setCurrentIndex(0)

    # -- form ---------------------------------------------------------------------------------------

    def _fill_form(self, e: dict, editing):
        self.editing = editing
        self.name.setText(str(e.get("name") or ""))
        self.synonym.setText(str(e.get("synonym") or ""))
        self.cas.setText(str(e.get("cas") or ""))
        self.mw.clear()
        self.formula.setText(str(e.get("formula") or ""))          # fills the MW from the formula
        if e.get("mw") not in (None, "") and not self.mw.text():
            self.mw.setText(str(e.get("mw")))
        self.ri.setText("" if e.get("ri") in (None, "") else str(e.get("ri")))
        self.rt.setText("" if e.get("rt") in (None, "") else str(e.get("rt")))
        self.column.setText(str(e.get("column") or ""))
        self.source.setText(str(e.get("source") or ""))
        self.comment.setPlainText(str(e.get("comment") or ""))
        self._peaks = list(e.get("peaks") or [])
        self.add_btn.setText("Save changes" if editing is not None else "Add to library")
        self.cancel_edit.setVisible(editing is not None and bool(self.entry.get("peaks")))
        self.tabs.setTabText(0, "Edit entry" if editing is not None else "New entry")
        self._show_spectrum()
        self._update_buttons()

    def _formula_changed(self, text):
        from gcws.ms.isotopes import nominal_mass, parse_formula
        try:
            mw = nominal_mass(parse_formula(text.replace(" ", ""))) if text.strip() else None
        except (KeyError, ValueError):
            mw = None
        if mw:
            self.mw.setText(str(mw))

    # -- spectrum sources ------------------------------------------------------------------------

    def set_spectrum(self, peaks, note: str = "", fields: dict | None = None):
        """New ions for the entry; ``fields`` fill the form where it is still empty."""
        self._peaks = [(float(m), float(a)) for m, a in peaks if float(a) > 0]
        self.entry["note"] = note
        for key, value in (fields or {}).items():
            w = {"name": self.name, "cas": self.cas, "formula": self.formula, "mw": self.mw, "ri": self.ri,
                 "rt": self.rt, "column": self.column, "source": self.source}.get(key)
            if w is not None and value not in (None, "") and not w.text().strip():
                w.setText(str(value))
        self._show_spectrum()
        self._update_buttons()

    def take_current(self):
        e = entry_from_spectrum(self.win)
        if not e.get("peaks"):
            self.status.setText("The Mass spectrum panel shows no spectrum: select a peak or right-click a "
                                "chromatogram, then press 'Take current spectrum' again.")
            return
        self.set_spectrum(e["peaks"], e.get("note", ""), e)
        self.status.setText(f"Spectrum taken: {e.get('source') or 'Mass spectrum panel'}")

    def import_msp(self, path: str = ""):
        from PySide6.QtWidgets import QFileDialog
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import a spectrum", QSettings().value("library_edit/msp_dir", ""),
                                                  "MSP spectra (*.msp *.MSP *.txt);;All files (*)")
        if not path:
            return
        from pathlib import Path
        QSettings().setValue("library_edit/msp_dir", str(Path(path).parent))
        data = Path(path).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode(LE.ENCODING, errors="replace")
        recs = [r for r in LE.parse_msp(text) if r.peaks]
        if not recs:
            QMessageBox.information(self, "Import MSP", "No spectrum found in this file.")
            return
        rec = recs[0]
        if len(recs) > 1:
            names = [f"{i + 1}: {r.name or '(no name)'}" for i, r in enumerate(recs)]
            choice, ok = QInputDialog.getItem(self, "Import MSP", "Spectrum:", names, 0, False)
            if not ok:
                return
            rec = recs[names.index(choice)]
        self.set_spectrum(rec.peaks, f"from {Path(path).name}", self._fields_of(rec))
        self.status.setText(f"Imported '{rec.name}' from {Path(path).name}")

    @staticmethod
    def _fields_of(rec) -> dict:
        return {"name": rec.name, "cas": rec.cas, "formula": rec.get("Formula"), "mw": rec.get("MW"),
                "ri": "" if rec.ri is None else f"{rec.ri:.0f}", "comment": rec.get("Comment")}

    def paste_spectrum(self, text: str | None = None):
        from PySide6.QtGui import QGuiApplication
        text = QGuiApplication.clipboard().text() if text is None else text
        peaks = LE.parse_spectrum_text(text)
        if not peaks:
            self.status.setText("The clipboard holds no spectrum (an MSP record or 'm/z abundance' pairs).")
            return
        recs = [r for r in LE.parse_msp(text) if r.peaks] if "num peaks" in text.lower() else []
        self.set_spectrum(peaks, "pasted", self._fields_of(recs[0]) if recs else None)
        self.status.setText(f"{len(peaks)} ions pasted")

    def type_ions(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Ions of the entry")
        edit = QPlainTextEdit("\n".join(f"{m:g} {a:g}" for m, a in self._peaks))
        edit.setPlaceholderText("one ion per line: m/z abundance, e.g.\n149 999\n167 320")
        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(edit)
        lay.addWidget(bb)
        dlg.resize(320, 420)
        if dlg.exec() == QDialog.Accepted:
            self.set_spectrum(LE.parse_spectrum_text(edit.toPlainText()), "typed in")

    def trimmed_peaks(self):
        if not self._peaks:
            return []
        top = max(a for _m, a in self._peaks) or 1.0
        f = 999.0 / top
        lim = self.trim.value() / 1000.0 * top
        return [(m, round(a * f, 2)) for m, a in self._peaks if a >= lim and a > 0]

    def _show_spectrum(self, *_):
        import numpy as np
        pk = self.trimmed_peaks()
        if pk:
            mz, ab = np.array([m for m, _ in pk]), np.array([a for _, a in pk])
            self.plot.show_spectrum(mz, ab, title=self.name.text() or "spectrum")
        else:
            self.plot.show_spectrum(None, None, title="no spectrum yet: Take current spectrum, Import MSP, Paste "
                                                      "or Type ions")
        note = self.entry.get("note", "") if self.editing is None else "stored spectrum"
        self.spec_note.setText(f"{len(pk)} ions" + (f"  -  {note}" if note else ""))
        QSettings().setValue("library_edit/trim", self.trim.value())

    def _update_buttons(self):
        lb = self.current_library()
        ok = lb is not None and lb.writable and not (lb.kind == "nist" and self.l2n_exe is None) and not self._busy
        self.add_btn.setEnabled(ok and bool(self._peaks if hasattr(self, "_peaks") else False))

    def build_record(self):
        return LE.new_record(self.name.text(), self.trimmed_peaks(), cas=self.cas.text().strip(),
                             formula=self.formula.text().strip(), mw=self.mw.text().strip() or None,
                             ri=self.ri.text().replace(",", ".").strip() or None,
                             rt=self.rt.text().replace(",", ".").strip() or None,
                             synonyms=[self.synonym.text()] if self.synonym.text().strip() else [],
                             comment=self.comment.toPlainText(), source=self.source.text(),
                             column=self.column.text(), analyst=_user())

    # -- actions -----------------------------------------------------------------------------------

    def _run(self, text, fn, done):
        from gcws.ui import workers
        self._busy = True
        self._update_buttons()
        self.status.setText(text)
        self.setCursor(Qt.BusyCursor)

        def finish(result):
            self._busy = False
            self.unsetCursor()
            self._update_buttons()
            done(result)

        def failed(err):
            self._busy = False
            self.unsetCursor()
            self._update_buttons()
            self.status.setText("")
            QMessageBox.warning(self, "Edit library", err.strip().splitlines()[-1])
        workers.submit(fn, on_done=finish, on_error=failed)

    def editor(self):
        lb = self.current_library()
        return LE.LibraryEditor(lb, self._lib2nist()) if lb is not None else None

    def save_entry(self):
        lb = self.current_library()
        try:
            rec = self.build_record()
        except ValueError as exc:
            self.check.setText(str(exc))
            return
        self.check.setText("")
        ed = self.editor()
        new_lib = not lb.path.exists()
        editing = self.editing
        if editing is not None:
            LE.keep_extra_fields(self.records[editing], rec)   # its ID, further synonyms, extra fields

        def work():
            res = ed.replace(editing, rec) if editing is not None else ed.add(rec)
            if new_lib:
                LE.register(lb)
            res.notes.append(LE.library_changed())
            return res

        def done(res):
            QSettings().setValue("library_edit/last", lb.name)
            what = "changed" if editing is not None else "added"
            self.win.ws.log(f"Library entry {what}", "", f"{lb.name}: {rec.name}")
            notes = [n for n in res.notes if n and not n.startswith(("added", "changed"))]
            self.status.setText(f"'{rec.name}' {what} in {lb.name} ({res.count} entries)."
                                + (f" Backup: {res.backup.name}." if res.backup else "")
                                + (" " + " ".join(notes) if notes else ""))
            self.records = []
            if new_lib:
                self._fill_libraries(lb.name)
            if editing is not None:
                self._fill_form(self.entry, editing=None)
                self.tabs.setCurrentIndex(1)
                self.load_entries()
        self._run(f"Saving into {lb.name} ...", work, done)

    def load_entries(self):
        lb = self.current_library()
        if lb is None or not lb.path.exists():
            self.records = []
            self._fill_table()
            return
        if lb.kind == "nist" and self.l2n_exe is None:
            return
        ed = self.editor()

        def done(records):
            self.records = records
            self.status.setText(f"{lb.name}: {len(records)} entries")
            self._fill_table()
        self._run(f"Reading {lb.name} ...", ed.read, done)

    def _fill_table(self, *_):
        f = self.filter.text().strip().casefold()
        self.table.setRowCount(0)
        for i, r in enumerate(self.records):
            if f and f not in r.name.casefold() and f not in r.cas:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c, v in enumerate((r.name, r.cas, r.get("Formula"), r.get("MW"),
                                   "" if r.ri is None else f"{r.ri:.0f}")):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, i)
                self.table.setItem(row, c, it)

    def _selected(self):
        it = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return it.data(Qt.UserRole) if it is not None else None

    def _show_record(self):
        import numpy as np
        i = self._selected()
        if i is None:
            return
        r = self.records[i]
        self.browse_plot.show_spectrum(np.array([m for m, _ in r.peaks]), np.array([a for _, a in r.peaks]),
                                       title=r.name)

    def edit_selected(self):
        i = self._selected()
        if i is None:
            return
        r = self.records[i]
        comment = r.get("Comment") or r.get("Comments")
        import re
        ri = r.ri
        comment = re.sub(r"\s*\|RI:[\d.]+\|", "", comment).strip()
        rt = None
        m = re.search(r"RT=([\d.]+) min", comment)
        if m:
            rt = m.group(1)
        self._fill_form({"name": r.name, "synonym": r.get("Synon"), "cas": r.cas, "formula": r.get("Formula"),
                         "mw": r.get("MW"), "ri": "" if ri is None else f"{ri:.0f}", "rt": rt or "",
                         "comment": comment, "peaks": r.peaks}, editing=i)
        self.tabs.setCurrentIndex(0)

    def delete_selected(self):
        i = self._selected()
        lb = self.current_library()
        if i is None or lb is None:
            return
        r = self.records[i]
        if QMessageBox.question(self, "Delete entry", f"Delete '{r.name}' from {lb.name}?\n\n(The library is "
                                "copied to the backup folder first.)") != QMessageBox.Yes:
            return
        ed = self.editor()

        def work():
            res = ed.delete(i)
            res.notes.append(LE.library_changed())
            return res

        def done(res):
            self.win.ws.log("Library entry deleted", "", f"{lb.name}: {r.name}")
            self.status.setText(f"'{r.name}' deleted from {lb.name} ({res.count} entries).")
            self.load_entries()
        self._run(f"Deleting from {lb.name} ...", work, done)


def _user() -> str:
    try:
        from gcws.core.audit import current_user
        return current_user()
    except Exception:  # noqa: BLE001
        return ""
