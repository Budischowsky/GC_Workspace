"""Desktop EI Atlas search, review and unknown-register research history."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.request import Request, ProxyHandler, build_opener
import uuid

import gc_atlas_store as store


def atlas_config():
    """Read ``ei_atlas_config.json`` next to this module; never raise."""
    config = Path(__file__).resolve().with_name('ei_atlas_config.json')
    try:
        settings = json.loads(config.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def atlas_root():
    """Locate the EI Atlas project without relying on one machine's layout.

    The candidates are tried in order and the first one that really holds
    ``app.py`` wins, so this folder stays copyable between PCs: ``EI_ATLAS_HOME``,
    the ``atlas_root`` entry of ``ei_atlas_config.json`` (a relative entry counts
    from this folder), a sibling or nested ``UnknownEvaluation`` folder and last
    the historical Desktop location.
    """
    here = Path(__file__).resolve().parent
    candidates = []
    for value in (os.environ.get('EI_ATLAS_HOME'), atlas_config().get('atlas_root')):
        if value:
            candidate = Path(value)
            candidates.append(candidate if candidate.is_absolute() else here / candidate)
    candidates += [here / 'UnknownEvaluation', here.parent / 'UnknownEvaluation',
                   Path.home() / 'Desktop' / 'KI Projects' / 'UnknownEvaluation']
    for root in candidates:
        try:
            if (root / 'app.py').is_file():
                return root
        except OSError:
            continue
    raise RuntimeError(
        'EI Atlas nicht gefunden. Das NIAS-Reporting laeuft ohne EI Atlas weiter.\n'
        'Fuer die Atlas-Suche den Ordner "UnknownEvaluation" neben diesen Ordner legen,\n'
        'oder "atlas_root" in ei_atlas_config.json bzw. EI_ATLAS_HOME darauf zeigen lassen.\n\n'
        'Gesucht wurde in:\n' + '\n'.join('  ' + str(item) for item in candidates))


def request(base, path, payload=None, timeout=300):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    req = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    # Local spectra must not pass through a system HTTP proxy.
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        if hasattr(exc, 'read'):
            try:
                raise RuntimeError(json.loads(exc.read()).get('error', str(exc))) from exc
            except (ValueError, AttributeError):
                pass
        raise


_START_LOCK = threading.Lock()

# A stale Atlas of an older integration keeps its port for as long as it runs, so the
# search needs clearly more ports than the three an update leaves behind.
ATLAS_PORTS = tuple(range(8765, 8781))


def port_free(port):
    """True when nothing accepts a loopback connection on this port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(.5)
        return probe.connect_ex(('127.0.0.1', port)) != 0


def atlas_version(root):
    """``atlas_version.py`` of the Atlas folder, loaded by path (it is not on sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('ei_atlas_version', Path(root) / 'atlas_version.py')
    if spec is None or spec.loader is None:
        raise RuntimeError(f'EI Atlas in {root} ist zu alt (atlas_version.py fehlt).')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_identity(root):
    """``(integration_version, code_id)`` a server started from ``root`` now reports."""
    version = atlas_version(root)
    return version.INTEGRATION_VERSION, version.code_id(root)


def is_atlas(status):
    """True when the service on a port is an EI Atlas (of any version)."""
    return isinstance(status, dict) and 'integration_version' in status and 'ready' in status


def usable_status(status, require_tabs, identity=None):
    """True for a running Atlas this integration may talk to.

    With ``identity`` the server must also run the current code: a server
    outlives NIAS, so one started before an update would otherwise answer with
    the old library scan (or none at all) until the PC is restarted.
    """
    if not is_atlas(status) or (require_tabs and not status.get('desktop_tabs')):
        return False
    if identity is None:
        return status.get('integration_version', 0) >= 2
    version, code = identity
    if status.get('integration_version') != version or status.get('code_id') != code:
        return False
    # A finished scan without a single library although the default folder
    # holds files is a failed start, not a result worth keeping.
    return not (status.get('ready') and not status.get('count') and not status.get('libraries')
                and not status.get('error') and status.get('library_files'))


def shutdown(base, timeout=8.0):
    """Ask the Atlas at ``base`` to stop and wait until its port is free."""
    port = int(base.rsplit(':', 1)[1])
    try:
        request(base, '/api/shutdown', {}, timeout=3)
    except Exception:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_free(port):
            return True
        time.sleep(.2)
    return False


def running_servers():
    """``[(base, status)]`` of every EI Atlas listening on the Atlas ports."""
    found = []
    for port in ATLAS_PORTS:
        if port_free(port):
            continue
        base = f'http://127.0.0.1:{port}'
        try:
            status = request(base, '/api/status', timeout=2)
        except Exception:
            continue
        if is_atlas(status):
            found.append((base, status))
    return found


def restart_server(require_tabs=False):
    """Stop every running EI Atlas and start a fresh one (rescans all libraries)."""
    with _START_LOCK:
        for base, _status in running_servers():
            shutdown(base)
    return ensure_server(require_tabs)


def ensure_server(require_tabs=False):
    with _START_LOCK:
        root = atlas_root()
        identity = expected_identity(root)
        free = []
        for port in ATLAS_PORTS:
            if port_free(port):
                free.append(port)
                continue
            base = f'http://127.0.0.1:{port}'
            try:
                status = request(base, '/api/status', timeout=2)
            except Exception:
                continue
            if usable_status(status, require_tabs, identity):
                return base
            if is_atlas(status) and not (require_tabs and not status.get('desktop_tabs')):
                # An EI Atlas started from older code (or whose scan found
                # nothing): replace it, its port becomes free for the new one.
                if shutdown(base):
                    free.append(port)
                continue
            # A foreign service holds this port; try the next one.
        if not free:
            raise RuntimeError(
                f'Kein freier Port fuer den EI Atlas ({ATLAS_PORTS[0]}-{ATLAS_PORTS[-1]} belegt).\n'
                'Aeltere EI-Atlas-Fenster schliessen bzw. "Stop EI Atlas.cmd" ausfuehren\n'
                'und die Suche danach erneut starten.')
        candidates = [root / '.nias-venv' / 'Scripts' / 'python.exe', root / '.venv' / 'Scripts' / 'python.exe', Path(sys.executable),
                      Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe']
        python = None
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        for candidate in candidates:
            if candidate.is_file():
                try:
                    if subprocess.run([str(candidate), '-c', 'import numpy'], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=15, creationflags=flags).returncode == 0:
                        python = candidate
                        break
                except (OSError, subprocess.TimeoutExpired):
                    continue
        if python is None:
            raise RuntimeError('Python mit NumPy fehlt. Bitte Start EI Atlas.cmd einmal ausfuehren.')
        for port in free:
            base = f'http://127.0.0.1:{port}'
            with (root / 'desktop-integration.log').open('ab') as log:
                process = subprocess.Popen([str(python), str(root / 'app.py'), '--port', str(port)],
                    cwd=str(root), stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
            for _ in range(100):
                if process.poll() is not None:
                    break  # The port was taken between the probe and the start; use the next one.
                try:
                    if usable_status(request(base, '/api/status', timeout=1), require_tabs, identity):
                        return base
                except Exception:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('EI Atlas antwortet noch nicht. Bitte Suche erneut starten.')
        raise RuntimeError('EI Atlas konnte nicht starten. Details: ' + str(root / 'desktop-integration.log'))


def load_text(status):
    """German one-liner of the server's library scan for status lines."""
    load = status.get('load') or {}
    if load.get('total'):
        current = Path(str(load.get('current') or '')).name
        return (f"Bibliotheken werden geladen: {load.get('done', 0)} / {load['total']}"
                + (f' – {current}' if current else '') + ' …')
    return 'EI Atlas startet …'


def wait_ready(base, timeout=900, cancelled=None, progress=None, stall=180):
    """Block until the server's libraries are loaded; returns ``/api/status``.

    ``progress(status)`` sees every poll. A first index build of large
    libraries takes minutes, so the overall ``timeout`` is generous; a scan
    whose message and counters have not moved for ``stall`` seconds fails
    earlier.
    """
    started = time.monotonic()
    last, moved = None, started
    while True:
        status = request(base, '/api/status', timeout=5)
        if status.get('error'):
            raise RuntimeError('EI Atlas konnte die Bibliotheken nicht laden: ' + str(status['error']))
        if status.get('ready'):
            return status
        if progress is not None:
            progress(status)
        if cancelled is not None and cancelled():
            raise RuntimeError('Abgebrochen.')
        marker = (status.get('message'), json.dumps(status.get('load'), sort_keys=True))
        now = time.monotonic()
        if marker != last:
            last, moved = marker, now
        if now - started > timeout or now - moved > stall:
            raise RuntimeError('EI Atlas lädt die Bibliotheken noch (' + str(status.get('message') or '')
                               + '). Bitte später erneut versuchen oder „EI Atlas neu starten“.')
        time.sleep(.3)


def msp_text(snapshot):
    points = store.clean_spectrum(snapshot.get('spectrum') or [])
    name = str(snapshot.get('name') or 'GC unknown').replace('\n', ' ').replace('\r', ' ')
    lines = ['Name: ' + name, 'Ionization: EI', 'Comment: GC displayed spectrum; tentative research']
    if snapshot.get('rt') is not None:
        lines.append('RetentionTime: ' + str(float(snapshot['rt'])))
    lines += ['Num Peaks: ' + str(len(points))]
    lines += [f'{m!r} {i!r}' for m, i in points]
    return '\n'.join(lines) + '\n'


class AtlasSession:
    """One private desktop process per Tk application, with routed tab events."""
    def __init__(self, master):
        self.master = master
        self.outgoing = queue.Queue()
        self.tabs = {}
        self.closed = False
        self.process = None
        self.started = False

    def launch(self):
        try:
            root = atlas_root()
            candidates = [root / '.nias-venv/Scripts/python.exe', root / '.venv/Scripts/python.exe', Path(sys.executable)]
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            python = next((p for p in candidates if p.is_file() and subprocess.run(
                [str(p), '-c', 'import webview, clr, numpy'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags, timeout=20).returncode == 0), None)
            if python is None:
                raise RuntimeError('Native EI-Atlas-Umgebung fehlt. requirements-desktop.txt installieren.')
            with (root / 'desktop-window.log').open('ab') as log:
                self.process = subprocess.Popen([str(python), str(root / 'desktop_host.py'), '--research', '--tabs',
                    '--integration-dir', str(Path(__file__).resolve().parent)], cwd=str(root),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', creationflags=flags)
                def write_requests():
                    try:
                        while True:
                            payload = self.outgoing.get()
                            if payload is None:
                                return
                            self.process.stdin.write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + '\n')
                            self.process.stdin.flush()
                    except (OSError, ValueError) as exc:
                        self.broadcast(dict(event='error', value=str(exc)))
                threading.Thread(target=write_requests, daemon=True).start()
                for line in self.process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and 'event' in event:
                        tab = self.tabs.get(event.get('tab_id'))
                        if tab:
                            tab.messages.put(event)
                if self.process.wait():
                    raise RuntimeError('EI-Atlas-Fenster konnte nicht ausgeführt werden. Details: ' + str(root / 'desktop-window.log'))
        except Exception as exc:
            self.broadcast(dict(event='error', value=str(exc)))
        finally:
            self.closed = True
            self.outgoing.put(None)
            self.broadcast(dict(event='closed'))

    def broadcast(self, event):
        for tab in list(self.tabs.values()):
            tab.messages.put(event)


class ResearchWindow:
    """A spectrum tab in the shared Atlas window; callbacks stay on Tk."""
    def __init__(self, parent, snapshot, db_path, context=None, entry_id=None,
                 spectrum_id=None, on_saved=None, initial=None):
        self.master = parent._root()
        self.snapshot, self.context = deepcopy(snapshot), deepcopy(context or {})
        self.db_path, self.entry_id = Path(db_path), entry_id
        self.on_saved = on_saved
        self.messages = queue.Queue()
        self.closed = False
        self.tab_id = uuid.uuid4().hex
        payload = dict(snapshot=self.snapshot, context=self.context, db_path=str(self.db_path.resolve()),
                       entry_id=entry_id, spectrum_id=spectrum_id, initial=deepcopy(initial or {}),
                       parent_hwnd=self.master.winfo_id(), tab_id=self.tab_id)
        # Validate before launching, including exact floating-point input.
        json.dumps(payload, allow_nan=False)
        session = getattr(self.master, '_ei_atlas_session', None)
        if session is None or session.closed or (session.process is not None and session.process.poll() is not None):
            session = AtlasSession(self.master)
            self.master._ei_atlas_session = session
        self.session = session
        session.tabs[self.tab_id] = self
        session.outgoing.put(payload)
        if not session.started:
            session.started = True
            threading.Thread(target=session.launch, daemon=True).start()
        self.master.after(100, self.poll)

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                event = self.messages.get_nowait()
                if event['event'] == 'saved':
                    self.entry_id = event['value']['entry_id']
                    if self.on_saved:
                        try:
                            self.on_saved(event['value'])
                        except Exception:
                            pass
                elif event['event'] == 'register':
                    self.entry_id = event['value']
                    self.open_register()
                elif event['event'] == 'error':
                    messagebox.showerror('EI Atlas', event['value'], parent=self.master)
                    self.closed = True
                elif event['event'] == 'closed':
                    self.closed = True
        except queue.Empty:
            pass
        if not self.closed:
            self.master.after(100, self.poll)
        else:
            self.session.tabs.pop(self.tab_id, None)

    def open_register(self):
        import gc_unknowns
        window = gc_unknowns.open_register(self.master, self.db_path)
        if self.entry_id is not None:
            window.reload()
            key = str(self.entry_id)
            if window.tree.exists(key):
                window.tree.selection_set(key)
                window.tree.see(key)
        window.lift()


def open_research(parent, snapshot, db_path, **kwargs):
    try:
        store.clean_spectrum(snapshot.get('spectrum') or [])
        return ResearchWindow(parent, snapshot, db_path, **kwargs)
    except Exception as exc:
        messagebox.showerror('EI Atlas', str(exc), parent=parent)
        return None


def open_hits(parent, snapshot, **kwargs):
    from gc_atlas_ui import open_hits as show
    return show(parent, snapshot, **kwargs)


def open_library(parent):
    """Open the full local library application without requiring a GC peak."""
    messages = queue.Queue()
    def launch():
        try:
            root = atlas_root()
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            candidates = [Path(sys.executable), root / '.venv/Scripts/python.exe', root / '.nias-venv/Scripts/python.exe']
            for python in candidates:
                if python.is_file() and subprocess.run([str(python), '-c', 'import webview, numpy'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=20).returncode == 0:
                    with (root / 'desktop-window.log').open('ab') as log:
                        subprocess.Popen([str(python), str(root / 'desktop_host.py'), '--integration-dir', str(Path(__file__).resolve().parent)],
                            cwd=root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
                    messages.put(None)
                    return
            raise RuntimeError('Bitte Setup NIAS.cmd ausführen: EI-Atlas-Laufzeit fehlt.')
        except Exception as exc:
            messages.put(str(exc))
    def poll():
        try:
            error = messages.get_nowait()
        except queue.Empty:
            parent.after(100, poll)
            return
        if error:
            messagebox.showerror('EI Atlas', error, parent=parent)
    threading.Thread(target=launch, daemon=True).start()
    parent.after(100, poll)


def show_history(parent, db_path, entry_id, on_saved=None):
    try:
        records = store.history(db_path, entry_id)
    except Exception as exc:
        messagebox.showerror('EI Atlas · Verlauf', str(exc), parent=parent)
        return
    window = tk.Toplevel(parent)
    window.title(f'EI Atlas · Untersuchungsverlauf · Eintrag {entry_id}')
    window.geometry('1050x700')
    tree = ttk.Treeview(window, columns=('date','decision','candidate'), show='headings', height=8)
    for key, label in [('date','Zeitpunkt'),('decision','Bewertung'),('candidate','Kandidat')]:
        tree.heading(key, text=label)
    tree.pack(fill=tk.X, padx=10, pady=10)
    details = tk.Text(window, wrap='word')
    details.pack(fill=tk.BOTH, expand=True, padx=10)
    def selected():
        selection = tree.selection()
        return records[int(selection[0])] if selection else None
    def display(_event=None):
        record = selected()
        details.configure(state='normal')
        details.delete('1.0','end')
        if record:
            payload = json.loads(record['payload_json'])
            details.insert('end', f"Beobachtungen: {record['note']}\n\nNächste Schritte: {record['next_steps']}\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            details.insert('end', 'Noch keine EI-Atlas-Untersuchung gespeichert.')
        details.configure(state='disabled')
    def export():
        record = selected()
        if not record:
            return
        path = filedialog.asksaveasfilename(parent=window, defaultextension='.json', initialfile=f'EI-UNK-{entry_id}.json', filetypes=[('JSON','*.json')])
        if path:
            try:
                Path(path).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('Export', str(exc), parent=window)
    def resume():
        record = selected()
        if record:
            payload = json.loads(record['payload_json'])
            open_research(parent, payload['snapshot'], db_path, context=payload['context'],
                          entry_id=entry_id, spectrum_id=record['spectrum_id'], initial=payload,
                          on_saved=saved)
    def reload_records():
        previous = selected()
        try:
            records[:] = store.history(db_path, entry_id)
        except Exception as exc:
            messagebox.showerror('EI Atlas · Verlauf', str(exc), parent=window)
            return
        tree.delete(*tree.get_children())
        target = '0'
        for i, record in enumerate(records):
            tree.insert('', 'end', iid=str(i), values=(record['created_at'], record['decision'], record['candidate_name']))
            if previous and record['investigation_id'] == previous['investigation_id']:
                target = str(i)
        if records:
            tree.selection_set(target)
        display()
    def saved(record):
        if window.winfo_exists():
            reload_records()
        if on_saved:
            on_saved(record)
    ttk.Button(window, text='Gespeicherte Untersuchung öffnen / fortsetzen', command=resume).pack(pady=(8,0))
    ttk.Button(window, text='Vollständige Untersuchung als JSON exportieren', command=export).pack(pady=10)
    ttk.Button(window, text='Aktualisieren', command=reload_records).pack(pady=(0, 10))
    tree.bind('<<TreeviewSelect>>', display)
    tree.bind('<Return>', lambda _event: resume())
    tree.bind('<Double-1>', lambda _event: resume())
    from gc_register_ui import enable_tree_copy
    enable_tree_copy(tree)
    for i, record in enumerate(records):
        tree.insert('', 'end', iid=str(i), values=(record['created_at'],record['decision'],record['candidate_name']))
    if records:
        tree.selection_set('0')
    display()
    return window
