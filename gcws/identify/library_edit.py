"""Own spectra in an existing library ("Edit library", as in ChemStation Enhanced Data Analysis).

Where entries can go:

* **NIST MS Search user libraries** (a folder with ``USER.DBU``, e.g.
  ``Library/CCAlu_GCMS``). Such a library is changed through NIST's own
  converter Lib2NIST: the library is exported to MSP, the entries are added,
  edited or deleted there, a new library is built next to it in a temporary
  folder and checked by exporting it again. Only then is the original copied
  to a backup (``<data>/library_backups``) and replaced. SpectrAtlas and NIST MS
  Search read the result like any user library.
* **MSP files** (created here when no Lib2NIST is available); the built-in
  search reads them like any library.
* **Agilent ``.L``, NIST main/replicate and Wiley/Shimadzu libraries** are
  read-only: their binary formats belong to their vendors.

The libraries are the analyst's (Identify > Libraries..., ``gcws.libsearch.store``);
after a change the search index of the library is rebuilt on the next search.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

#: Greek letters and symbols NIST writes as words (MSP files are ANSI)
_ASCII = {"α": ".alpha.", "β": ".beta.", "γ": ".gamma.", "δ": ".delta.", "ω": ".omega.", "µ": "u", "μ": "u",
          "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "±": "+/-"}
ENCODING = "cp1252"
CAS_KEYS = ("cas#", "casno", "cas")


def ansi(text: str) -> str:
    """``text`` in the characters an MSP file / NIST library can hold."""
    out = "".join(_ASCII.get(ch, ch) for ch in str(text or ""))
    return out.encode(ENCODING, errors="replace").decode(ENCODING).replace("\r", " ").replace("\n", " ").strip()


# -- MSP records ------------------------------------------------------------------------------

@dataclass
class MspRecord:
    """One MSP entry; header fields keep their order and spelling so untouched entries are
    written back exactly as read."""
    fields: list[list[str]] = field(default_factory=list)      # [key, value]
    peaks: list[tuple[float, float]] = field(default_factory=list)
    raw_peaks: Optional[list[str]] = None                     # the original peak lines

    def get(self, key: str, default: str = "") -> str:
        keys = CAS_KEYS if key.lower() in CAS_KEYS else (key.lower(),)
        for k, v in self.fields:
            if k.lower() in keys:
                return v
        return default

    def set(self, key: str, value) -> None:
        keys = CAS_KEYS if key.lower() in CAS_KEYS else (key.lower(),)
        value = "" if value is None else str(value)
        for f in self.fields:
            if f[0].lower() in keys:
                if value:
                    f[1] = value
                else:
                    self.fields.remove(f)
                return
        if value:
            self.fields.append([key, value])

    @property
    def name(self) -> str:
        return self.get("Name")

    @property
    def cas(self) -> str:
        return format_cas(self.get("CAS#"))

    @property
    def ri(self) -> Optional[float]:
        v = self.get("RI")
        m = re.search(r"\|RI:(\d+(?:\.\d+)?)\|", self.get("Comment") + " " + self.get("Comments"))
        try:
            return float(v) if v else (float(m.group(1)) if m else None)
        except ValueError:
            return None

    def set_peaks(self, peaks) -> None:
        self.peaks = [(float(m), float(a)) for m, a in peaks]
        self.raw_peaks = None

    def to_text(self) -> str:
        lines = [f"{k}: {v}" for k, v in self.fields]
        lines.append(f"Num Peaks: {len(self.peaks)}")
        if self.raw_peaks is not None:
            lines += self.raw_peaks
        else:
            row = []
            for mz, ab in self.peaks:
                row.append(f"{_num(mz)} {_num(ab)};")
                if len(row) == 5:
                    lines.append(" ".join(row))
                    row = []
            if row:
                lines.append(" ".join(row))
        return "\r\n".join(lines) + "\r\n"


def _num(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.4f}".rstrip("0").rstrip(".")


#: the header fields the entry form shows and writes (``new_record``); RI goes into the comment
FORM_FIELDS = {"name", "synon", "formula", "mw", "comment", "comments", "ri"} | set(CAS_KEYS)


def keep_extra_fields(old: MspRecord, new: MspRecord) -> MspRecord:
    """``new`` (an edited entry, from the form) with what the form does not show of ``old``: its
    further synonyms (the form shows the first; all go when it is cleared) and its other fields
    (library IDs, InChIKey, ...)."""
    old_syn = [v for k, v in old.fields if k.lower() == "synon"]
    new_syn = [v for k, v in new.fields if k.lower() == "synon"]
    if new_syn:
        at = max(i for i, (k, _v) in enumerate(new.fields) if k.lower() == "synon") + 1
        new.fields[at:at] = [["Synon", v] for v in old_syn[1:] if v not in new_syn]
    for k, v in old.fields:
        if k.lower() not in FORM_FIELDS and not new.get(k):
            new.fields.append([k, v])
    return new


def format_cas(value: str) -> str:
    """``117817`` / ``117-81-7`` -> ``117-81-7`` ("" for none or 0)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or int(digits) == 0 or len(digits) < 5:
        return ""
    return f"{digits[:-3]}-{digits[-3:-1]}-{digits[-1]}"


#: a number of an MSP peak line (as the search's reader takes it: decimals, exponents)
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def parse_msp(text: str) -> list[MspRecord]:
    records, cur, in_peaks = [], None, False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if cur is not None:
                records.append(cur)
            cur, in_peaks = None, False
            continue
        if in_peaks and re.match(r"(?i)name\s*:", s):
            records.append(cur)                   # a record without a blank line before it
            cur, in_peaks = None, False
        if cur is None:
            cur = MspRecord(raw_peaks=[])
        if in_peaks:
            cur.raw_peaks.append(line)
            for m, a in re.findall(rf"({_NUMBER})[ \t:,]+({_NUMBER})", s):
                cur.peaks.append((float(m), float(a)))
            continue
        key, sep, value = s.partition(":")
        if not sep:
            continue
        if key.strip().lower() == "num peaks":
            in_peaks = True
            continue
        cur.fields.append([key.strip(), value.strip()])
    if cur is not None:
        records.append(cur)
    return records


def msp_encoding(data: bytes) -> str:
    """The encoding of an MSP file as the search reads it (``msp.decode``): UTF-16 or UTF-8 when the
    bytes are that, else ANSI (cp1252). A pure ASCII file stays ANSI."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        data.decode("ascii")
        return ENCODING
    except UnicodeDecodeError:
        pass
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return ENCODING


def write_msp(records) -> str:
    return "\r\n".join(r.to_text() for r in records) + "\r\n"


def new_record(name: str, peaks, cas: str = "", formula: str = "", mw=None, ri=None, rt=None,
               synonyms=(), comment: str = "", source: str = "", column: str = "", analyst: str = "") -> MspRecord:
    """A library entry; the extra information goes into the comment (RI also as NIST's
    ``|RI:n|`` tag, which Lib2NIST and MS Search understand)."""
    if not ansi(name):
        raise ValueError("The entry needs a name.")
    if not peaks:
        raise ValueError("The spectrum is empty.")
    r = MspRecord()
    r.set("Name", ansi(name))
    for syn in synonyms:
        if ansi(syn):
            r.fields.append(["Synon", ansi(syn)])
    if formula:
        r.set("Formula", ansi(formula).replace(" ", ""))
    if mw not in (None, ""):
        r.set("MW", _num(float(mw)))
    if cas:
        c = format_cas(cas)
        if not c:
            raise ValueError(f"Not a CAS number: {cas}")
        r.set("CAS#", c)
    parts = []
    if rt not in (None, ""):
        parts.append(f"RT={float(rt):.3f} min")
    if column:
        parts.append(f"Column: {ansi(column)}")
    if source:
        parts.append(f"Source: {ansi(source)}")
    if analyst:
        parts.append(f"Analyst: {ansi(analyst)}")
    parts.append(f"GC Workspace {datetime.now():%Y-%m-%d}")
    if comment:
        parts.append(ansi(comment))
    text = "; ".join(parts)
    if ri not in (None, ""):
        text += f" |RI:{int(round(float(ri)))}|"
    r.set("Comment", text)
    r.set_peaks(peaks)
    return r


# -- libraries ------------------------------------------------------------------------------------

@dataclass
class LibraryInfo:
    name: str
    path: Path
    kind: str                   # "nist" | "msp" | "agilent"
    writable: bool
    note: str = ""
    root: Optional[Path] = None

    @property
    def label(self) -> str:
        """The name SpectrAtlas reports for it (path relative to the Atlas folder)."""
        try:
            return str(self.path.relative_to(self.root)) if self.root else self.name
        except ValueError:
            return self.name


def atlas_root() -> Optional[Path]:
    try:
        import gc_atlas
        return Path(gc_atlas.atlas_root())
    except Exception:  # noqa: BLE001 - SpectrAtlas not installed / not configured
        return None


def _files_lower(folder: Path) -> set[str]:
    try:
        return {p.name.lower() for p in folder.iterdir() if p.is_file()}
    except OSError:
        return set()


def list_libraries(root: Optional[Path] = None) -> list[LibraryInfo]:
    """The libraries of SpectrAtlas that can take own entries (and the read-only Agilent ones)."""
    root = Path(root) if root is not None else atlas_root()
    if root is None:
        return []
    out = []
    lib = root / "Library"
    if lib.is_dir():
        for d in sorted((p for p in lib.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
            names = _files_lower(d)
            if "user.dbu" in names:
                out.append(LibraryInfo(d.name, d, "nist", True, "NIST MS Search user library", root))
            elif "header.ind" in names or d.suffix.lower() == ".l":
                out.append(LibraryInfo(d.name, d, "agilent", False,
                                       "Agilent .L libraries can only be edited in ChemStation; use its NIST "
                                       "copy (e.g. CCAlu_GCMS)", root))
    own = root / "libraries" / "gcws"
    if own.is_dir():
        for f in sorted(own.glob("*.msp")):
            out.append(LibraryInfo(f.stem, f, "msp", True, "GC Workspace MSP library (SpectrAtlas references)", root))
    return out


def own_libraries() -> list[LibraryInfo]:
    """The analyst's libraries (Identify > Libraries...), writable or not."""
    from gcws.libsearch import store
    out = []
    for s in store.load():
        p = Path(s.path)
        if s.kind == "msp":
            out.append(LibraryInfo(s.name, p, "msp", True, "MSP library"))
        elif s.kind == "nist":
            user = "user.dbu" in _files_lower(p)
            out.append(LibraryInfo(s.name, p, "nist", user, "NIST MS Search user library" if user else
                                   "NIST main / replicate libraries are read-only; add own spectra to a user "
                                   "library"))
        elif s.kind == "agilent":
            out.append(LibraryInfo(s.name, p, "agilent", False,
                                   "Agilent .L libraries can only be edited in ChemStation; use its NIST copy "
                                   "(e.g. CCAlu_GCMS) or an MSP library"))
        else:
            out.append(LibraryInfo(s.name, p, s.kind, False, "Wiley / Shimadzu libraries are read-only"))
    return out


def parse_spectrum_text(text: str) -> list[tuple[float, float]]:
    """Peaks from pasted text: an MSP record, or lines / pairs of "m/z abundance"."""
    text = str(text or "")
    if re.search(r"(?im)^\s*num\s*peaks\s*:", text):
        recs = [r for r in parse_msp(text) if r.peaks]
        if recs:
            return list(recs[0].peaks)
    number = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ";" in line:                                   # MSP peak lines: "91 999; 92 450"
            pairs += re.findall(rf"({number})\s*[\s:,]\s*({number})", line)
            continue
        tokens = [t for t in re.split(r"[\s:]+", line) if t]
        if len(tokens) == 1 and tokens[0].count(",") == 1:
            tokens = tokens[0].split(",")                 # "91,999"
        elif len(tokens) == 2:
            tokens = [t.replace(",", ".") for t in tokens]  # "91 999,5" (decimal comma)
        pairs += [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2)]
    peaks: dict[float, float] = {}
    for m, a in pairs:
        try:
            mz, ab = float(m), float(a)
        except ValueError:
            continue
        if 1 <= mz <= 10000 and ab > 0:
            peaks[mz] = peaks.get(mz, 0.0) + ab
    return sorted(peaks.items())


def default_library(libs: list[LibraryInfo], remembered: str = "") -> Optional[LibraryInfo]:
    writable = [lb for lb in libs if lb.writable]
    for lb in writable:
        if remembered and lb.name == remembered:
            return lb
    for pref in ("ccalu_gcms", "ccalu"):
        for lb in writable:
            if lb.name.casefold().startswith(pref) and lb.kind == "nist":
                return lb
    return writable[0] if writable else None


def find_lib2nist(root: Optional[Path] = None, configured: str = "") -> Optional[Path]:
    """NIST's converter: the configured path, the MS Search folder next to the SpectrAtlas
    libraries, or an installed NIST MS Search."""
    cands = []
    if configured:
        cands.append(Path(configured))
    root = Path(root) if root is not None else atlas_root()
    if root is not None:
        cands.append(root / "Library" / "Software" / "NISTMS" / "MSSEARCH" / "lib2nist.exe")
    try:
        import gc_nist
        inst = gc_nist.installation()
        folder = getattr(inst, "mssearch_dir", None) or getattr(inst, "directory", None)
        if folder:
            cands.append(Path(folder) / "lib2nist.exe")
    except Exception:  # noqa: BLE001 - NIST not installed
        pass
    for base in (Path("C:/"), Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
                 Path(os.environ.get("ProgramFiles", "C:/Program Files"))):
        cands += sorted(base.glob("NIST*/MSSEARCH/lib2nist.exe"))
    return next((c for c in cands if c.is_file()), None)


class LibraryError(RuntimeError):
    pass


class Lib2Nist:
    """Command-line use of NIST Lib2NIST with a private .ini (the registry stays untouched)."""

    def __init__(self, exe: Path):
        self.exe = Path(exe)

    def _ini(self, folder: Path, text_out: bool) -> Path:
        ini = folder / ("export.ini" if text_out else "build.ini")
        ini.write_text("[Directory]\r\nNIST=<None>\r\n[Output]\r\n"
                       f"Text={1 if text_out else 0}\r\nTextFileType=0\r\nDB={0 if text_out else 1}\r\n"
                       "CalcMW=0\r\nIncludeSynonyms=1\r\nKeepIDs=1\r\nLinkMOLfile=0\r\nMzAdd=0\r\nMzMpy=1\r\n"
                       "NeedSubset=0\r\n", encoding="ascii")
        return ini

    def _run(self, args: list[str], folder: Path, what: str) -> str:
        log = folder / f"{what}.log"
        cmd = [str(self.exe), "/log5", str(log)] + args
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=900,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LibraryError(f"Lib2NIST could not run: {exc}") from exc
        text = log.read_text(encoding=ENCODING, errors="replace") if log.exists() else ""
        if r.returncode != 0:
            raise LibraryError(f"Lib2NIST failed ({what}, code {r.returncode}): {text[-400:]}")
        return text

    def export(self, library: Path, msp: Path) -> list[MspRecord]:
        folder = msp.parent
        self._run([str(self._ini(folder, True)), "/OutMSP", str(library), "=" + str(msp)], folder, "export")
        if not msp.is_file():
            raise LibraryError(f"Lib2NIST did not export {library.name}")
        return parse_msp(msp.read_text(encoding=ENCODING, errors="replace"))

    def build(self, msp: Path, target: Path) -> None:
        folder = msp.parent
        self._run([str(self._ini(folder, False)), "/OutLib", "/KeepIDs:Y", "/IncludeSynonyms:Y", "/UseSubset:N",
                   "/NoAlias", str(msp), "=" + str(target)], folder, "build")
        if not (target / "USER.DBU").is_file() and "user.dbu" not in _files_lower(target):
            raise LibraryError(f"Lib2NIST did not build {target.name}")


@dataclass
class SaveResult:
    count: int
    backup: Optional[Path] = None
    notes: list[str] = field(default_factory=list)


class LibraryEditor:
    """Read and change the entries of one library (see the module notes)."""

    def __init__(self, info: LibraryInfo, lib2nist: Optional[Lib2Nist] = None, backup_dir: Optional[Path] = None):
        self.info = info
        self.lib2nist = lib2nist
        if backup_dir is None:
            from gcws import paths
            backup_dir = paths.DATA / "library_backups"
        self.backup_dir = Path(backup_dir)

    def _need_lib2nist(self) -> Lib2Nist:
        if self.lib2nist is None:
            raise LibraryError("NIST Lib2NIST (lib2nist.exe) was not found: it is needed to change a NIST user "
                               "library. Set its path in Edit > Preferences, or save into a GC Workspace MSP "
                               "library instead.")
        return self.lib2nist

    def read(self) -> list[MspRecord]:
        if not self.info.writable and self.info.kind != "nist":
            raise LibraryError(self.info.note or "This library cannot be read here.")
        if self.info.kind == "msp":
            data = self.info.path.read_bytes()
            return parse_msp(data.decode(msp_encoding(data), errors="replace"))
        tmp = Path(tempfile.mkdtemp(prefix="gcws_lib_"))
        try:
            return self._need_lib2nist().export(self.info.path, tmp / "export.msp")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def save(self, records: list[MspRecord], change: str = "") -> SaveResult:
        """Replace the library's entries by ``records`` (checked before the original is touched)."""
        if not self.info.writable:
            raise LibraryError(self.info.note or "This library is read-only.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # one file / folder name (library names can hold dots or a SpectrAtlas path: "libraries\Own.msp")
        backup = self.backup_dir / (re.sub(r'[<>:"/\\|?*]+', "_", self.info.name) + f"-{stamp}")
        text = write_msp(records)
        if self.info.kind == "msp":
            path = self.info.path
            encoding = ENCODING
            msp_backup = backup.with_name(backup.name + ".msp")
            if path.exists():
                encoding = msp_encoding(path.read_bytes())       # written back as it was read
                shutil.copy2(path, msp_backup)
            tmp = path.with_suffix(".msp.tmp")
            tmp.write_text(text, encoding=encoding, errors="replace", newline="")
            os.replace(tmp, path)
            return SaveResult(len(records), msp_backup if msp_backup.exists() else None)
        l2n = self._need_lib2nist()
        tmp = Path(tempfile.mkdtemp(prefix="gcws_lib_"))
        try:
            msp = tmp / "new.msp"
            msp.write_text(text, encoding=ENCODING, errors="replace", newline="")
            built = tmp / "out" / self.info.path.name
            l2n.build(msp, built)
            (tmp / "check").mkdir()
            check = l2n.export(built, tmp / "check" / "verify.msp")
            want = sorted(r.name for r in records)
            if len(check) != len(records) or sorted(r.name for r in check) != want:
                raise LibraryError(f"The rebuilt library holds {len(check)} entries instead of {len(records)}; "
                                   "the original library was not changed.")
            if self.info.path.exists():
                shutil.copytree(self.info.path, backup)
            self._swap(built, self.info.path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return SaveResult(len(records), backup if backup.exists() else None,
                          [f"{change} ({len(records)} entries)"] if change else [])

    @staticmethod
    def _swap(built: Path, target: Path) -> None:
        """Put ``built`` in place of ``target``; the original stays intact if anything fails."""
        old = target.with_name(target.name + ".gcws-old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            try:
                target.rename(old)
            except OSError as exc:
                raise LibraryError(f"{target.name} is in use ({exc.strerror}). Close NIST MS Search and try "
                                   "again; the library was not changed.") from exc
        try:
            shutil.copytree(built, target)
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            if old.exists():
                old.rename(target)
            raise LibraryError(f"Could not write {target.name}: {exc}; the library was not changed.") from exc
        shutil.rmtree(old, ignore_errors=True)

    # -- entry changes ------------------------------------------------------------------------

    def add(self, record: MspRecord) -> SaveResult:
        records = self.read() if self.info.path.exists() else []
        records.append(record)
        return self.save(records, f"added {record.name}")

    def replace(self, index: int, record: MspRecord) -> SaveResult:
        records = self.read()
        if not 0 <= index < len(records):
            raise LibraryError("The entry is no longer in the library.")
        records[index] = record
        return self.save(records, f"changed {record.name}")

    def delete(self, index: int) -> SaveResult:
        records = self.read()
        if not 0 <= index < len(records):
            raise LibraryError("The entry is no longer in the library.")
        gone = records.pop(index)
        return self.save(records, f"deleted {gone.name}")


def create_library(name: str, folder: Optional[Path] = None, lib2nist: Optional[Lib2Nist] = None,
                   kind: str = "") -> LibraryInfo:
    """A new, empty own library in ``folder`` (default ``<data>/libraries``): a NIST user library
    (with Lib2NIST) or an MSP file. Its first entry creates it; it is then added to the
    analyst's libraries (:func:`register`)."""
    from gcws import paths
    folder = Path(folder) if folder is not None else paths.DATA / "libraries"
    clean = re.sub(r'[<>:"/\\|?*.]+', "_", name).strip(" _")
    if not clean:
        raise LibraryError("Please enter a library name.")
    kind = kind or ("nist" if lib2nist is not None else "msp")
    if kind == "nist":
        if lib2nist is None:
            raise LibraryError("A NIST user library needs Lib2NIST (Edit > Preferences).")
        path = folder / clean
        if path.exists():
            raise LibraryError(f"{clean} already exists.")
        return LibraryInfo(clean, path, "nist", True, "NIST MS Search user library (new)")
    path = folder / f"{clean}.msp"
    if path.exists():
        raise LibraryError(f"{clean} already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return LibraryInfo(clean, path, "msp", True, "MSP library (new)")


def register(info: LibraryInfo) -> None:
    """Add a (new) library to the analyst's libraries and to every search method."""
    from gcws.libsearch import store
    libs = store.load()
    if not any(Path(s.path).resolve() == Path(info.path).resolve() for s in libs):
        libs = store.add(libs, [store.LibrarySpec(info.name, info.kind, str(info.path))])
        store.save(libs)
    enable_in_search_methods(info.name)
    library_changed()


def library_changed() -> str:
    """After a change: the built-in search reads the library again ("" = done)."""
    try:
        from gcws.libsearch import service
        service.reset()
    except Exception as exc:  # noqa: BLE001 - the library is saved; the next start reads it anyway
        return f"The search sees the change after a restart ({exc})"
    return ""


def rescan_atlas() -> str:
    """Ask a running SpectrAtlas to read its libraries again ("" = done or not running)."""
    try:
        import gc_atlas
        for base, _status in gc_atlas.running_servers():
            gc_atlas.request(base, "/api/library/rescan", {}, timeout=10)
    except Exception as exc:  # noqa: BLE001 - the library is saved; the next start rescans anyway
        return f"SpectrAtlas will see the change after its next start ({exc})"
    return ""


def enable_in_search_methods(label: str) -> None:
    """A new library is searched by every library-search method (SpectrAtlas adds new ones off)."""
    import gc_search_method as SM
    store = SM.MethodStore()
    changed = False
    for name in store.names():
        m = store.get(name)
        if not any(e.name == label for e in m.libraries):
            m.libraries.append(SM.LibraryEntry(label, True))
            store.put(m)
            changed = True
    if changed:
        store.save()
