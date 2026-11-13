"""Servidor del panel: solo biblioteca estándar, solo en 127.0.0.1.

Lee out/_runs/*.json (lo que escribe progress.py en cualquier proceso), los empuja al navegador por SSE
y lanza comandos de la CLI como subprocesos (así Playwright y las esperas no bloquean el servidor).
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import progress
from ..config import ROOT, Settings

STATIC = Path(__file__).parent / "static"
ACTIVE = ("starting", "running")
SCAN_EVERY = 0.4  # s entre revisiones de out/_runs para el SSE
WATCH_EVERY = 2.0  # s entre revisiones de procesos caídos

# Lo que se puede lanzar desde el panel y el tipo de cada argumento. Nada fuera de aquí llega a la CLI.
LAUNCH: dict[str, dict[str, str]] = {
    "plan": {"client": "client", "month": "month", "dry_run": "flag", "reemplazar": "flag"},
    "rediseñar": {"client": "client", "month": "month", "dry_run": "flag"},
    "publish": {"now": "flag"},
    "fotos": {"client": "client", "importar": "flag"},
    "moodboard": {"client": "client", "forzar": "flag", "rehacer_referencias": "flag",
                  "solo_referencias": "flag", "revalidar": "flag", "reintentar_descartadas": "flag"},
    "onboarding run": {},
}
REQUIRED = {"client", "month"}
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RUN_ID = re.compile(r"^[\w\-.]+$", re.UNICODE)
SERVABLE = {".png", ".jpg", ".jpeg", ".webp", ".md", ".json", ".gif"}


class LaunchError(ValueError):
    pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def read_json(path: Path) -> dict | None:
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, PermissionError, OSError):
            time.sleep(0.03)
    return None


def summarize(run: dict) -> dict:
    steps = run.get("steps", [])
    current = next((s for s in steps if s.get("status") in ("running", "waiting")), None)
    return {k: run.get(k) for k in ("id", "command", "title", "client", "group", "status", "started", "finished",
                                     "pct", "error", "args")} | {
        "current": current.get("label") if current else None,
        "steps_done": sum(1 for s in steps if s.get("status") in ("done", "error", "skipped")),
        "steps_total": len(steps),
    }


class Dashboard:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runs_dir = Path(os.environ.get(progress.RUNS_DIR_ENV) or settings.out_dir / "_runs")
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # ------------------------------------------------------------------ lectura
    def run_path(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise LaunchError("id no válido")
        return self.runs_dir / f"{run_id}.json"

    def runs(self) -> list[dict]:
        found = []
        for path in self.runs_dir.glob("*.json"):
            if data := read_json(path):
                data["cancellable"] = path.stem in self.procs
                found.append(summarize(data) | {"cancellable": data["cancellable"]})
        return sorted(found, key=lambda r: r.get("started") or "", reverse=True)

    def run(self, run_id: str) -> dict | None:
        data = read_json(self.run_path(run_id))
        if data is not None:
            data["cancellable"] = run_id in self.procs  # solo las que lanzó este panel
        return data

    def log_tail(self, run_id: str, size: int = 64_000) -> str:
        path = self.run_path(run_id).with_suffix(".log")
        if not path.exists():
            return ""
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - size))
            return fh.read().decode("utf-8", errors="replace")

    def clients(self) -> list[str]:
        return sorted(p.name for p in self.settings.clients_dir.iterdir() if (p / "brand.yaml").exists())

    def meta(self) -> dict:
        return {"clients": self.clients(), "launch": LAUNCH, "required": sorted(REQUIRED),
                "flows": {k: [{"key": a, "label": b, "weight": c} for a, b, c in v] for k, v in progress.FLOWS.items()},
                "titles": progress.TITLES, "llm": self.settings.llm_provider}

    def file(self, raw: str) -> Path:
        path = Path(unquote(raw)).resolve()
        roots = [self.settings.out_dir.resolve(), self.settings.clients_dir.resolve()]
        if not any(path.is_relative_to(root) for root in roots) or path.suffix.lower() not in SERVABLE:
            raise PermissionError("Fuera de out/ y clients/")
        if not path.is_file():
            raise FileNotFoundError(raw)
        return path

    # ------------------------------------------------------------------ lanzar
    def argv(self, command: str, args: dict) -> list[str]:
        spec = LAUNCH.get(command)
        if spec is None:
            raise LaunchError(f"Comando no permitido: {command}")
        unknown = set(args) - set(spec)
        if unknown:
            raise LaunchError(f"Argumentos no permitidos: {', '.join(sorted(unknown))}")
        argv = command.split()
        for name, kind in spec.items():
            value = args.get(name)
            if value in (None, "", False):
                if name in REQUIRED:
                    raise LaunchError(f"Falta {name}")
                continue
            flag = "--" + name.replace("_", "-")
            if kind == "flag":
                if value is not True:
                    raise LaunchError(f"{name} debe ser true o false")
                argv.append(flag)
            elif kind == "client":
                if value not in self.clients():
                    raise LaunchError(f"Cliente desconocido: {value}")
                argv += [flag, value]
            elif kind == "month":
                if not isinstance(value, str) or not MONTH.match(value):
                    raise LaunchError("El mes va como AAAA-MM")
                argv += [flag, value]
            elif kind == "int":
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise LaunchError(f"{name} debe ser un número") from None
                if not 1 <= number <= 100:
                    raise LaunchError(f"{name} fuera de rango")
                argv += [flag, str(number)]
            elif kind == "file":
                if not isinstance(value, str) or not Path(value).is_file():
                    raise LaunchError(f"No existe el archivo: {value}")
                argv += [flag, str(Path(value).resolve())]
        return argv

    def launch(self, command: str, args: dict, fake_llm: bool = False, fake_delay: float = 0) -> str:
        argv = self.argv(command, args)
        client = args.get("client") or ""
        run_id = progress.new_id(command, client)
        env = {**os.environ, progress.RUN_ENV: run_id, progress.RUNS_DIR_ENV: str(self.runs_dir),
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if fake_llm:
            env["LLM_PROVIDER"] = "fake"
            env["FAKE_LLM_DELAY"] = str(max(0.0, min(float(fake_delay or 0), 10.0)))
        log_path = self.run_path(run_id).with_suffix(".log")
        info = {k: v for k, v in args.items() if v not in (None, "", False)}
        if fake_llm:
            info["llm"] = "fake"
        with self.lock:
            # Estado inicial antes de que el proceso escriba el suyo: el panel lo pinta al instante.
            progress.write_json(self.run_path(run_id), progress.initial_state(run_id, command, info, client))
            log_file = log_path.open("wb")
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen([sys.executable, "-m", "social_agent.cli", *argv], cwd=ROOT, env=env,
                                    stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
            log_file.close()
            self.procs[run_id] = proc
            state = progress.initial_state(run_id, command, info, client, pid=proc.pid)
            state["launched"] = True
            if not (current := self.run(run_id)) or current.get("status") == "starting":
                progress.write_json(self.run_path(run_id), state)
        return run_id

    def cancel(self, run_id: str) -> bool:
        with self.lock:
            proc = self.procs.get(run_id)
        if proc is None:
            return False
        data = self.run(run_id) or {}
        kill_tree(proc.pid)
        if data.get("pid") and data["pid"] != proc.pid:
            kill_tree(data["pid"])
        self._finish(run_id, "cancelled", "Cancelada desde el panel")
        return True

    def _finish(self, run_id: str, status: str, error: str) -> None:
        data = self.run(run_id)
        if not data or data.get("status") not in ACTIVE:
            return
        data["status"], data["error"] = status, data.get("error") or error
        data["finished"] = progress._now()
        for s in data.get("steps", []):
            if s.get("status") in ("running", "waiting"):
                s["status"], s["error"] = "error", s.get("error") or error
                s["wait"] = None
        progress.write_json(self.run_path(run_id), data)

    # ------------------------------------------------------------------ vigilancia
    def watch_once(self) -> None:
        """Marca las ejecuciones cuyo proceso murió sin cerrar su estado (crash, ventana cerrada)."""
        with self.lock:
            procs = dict(self.procs)
        for run_id, proc in procs.items():
            code = proc.poll()
            if code is not None:
                if code != 0:
                    tail = self.log_tail(run_id, 4000).strip().splitlines()[-6:]
                    self._finish(run_id, "error", f"El proceso terminó con código {code}"
                                 + (f": {tail[-1][:300]}" if tail else ""))
                else:
                    self._finish(run_id, "interrupted", "El proceso terminó sin cerrar la ejecución")
                with self.lock:
                    self.procs.pop(run_id, None)
        for path in self.runs_dir.glob("*.json"):
            data = read_json(path)
            run_id = path.stem
            if not data or data.get("status") not in ACTIVE or run_id in procs:
                continue
            if not pid_alive(int(data.get("pid") or 0)):
                self._finish(run_id, "interrupted", "El proceso ya no está corriendo")

    def watch(self) -> None:
        while not self.stop.wait(WATCH_EVERY):
            try:
                self.watch_once()
            except Exception:  # la vigilancia no debe tumbar el panel
                pass

    def scan(self) -> dict[str, float]:
        out = {}
        for path in self.runs_dir.glob("*.json"):
            try:
                out[path.stem] = path.stat().st_mtime
            except FileNotFoundError:
                pass
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = "SocialAgentPanel/1.0"
    dashboard: Dashboard

    def log_message(self, format, *args):  # sin ruido en la consola
        pass

    # ------------------------------------------------------------------ utilidades
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            raise LaunchError("Petición demasiado grande")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise LaunchError("Se esperaba un objeto JSON")
        return data

    def _same_origin(self) -> bool:
        """Solo el propio panel puede lanzar o cancelar (evita que otra web abierta lo haga)."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    # ------------------------------------------------------------------ rutas
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = unquote(url.path)
        d = self.dashboard
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if path == "/api/meta":
                return self._json(d.meta())
            if path == "/api/runs":
                return self._json(d.runs())
            if path == "/api/events":
                return self._events()
            if m := re.fullmatch(r"/api/runs/([^/]+)/log", path):
                return self._send(200, d.log_tail(m.group(1)).encode("utf-8"), "text/plain; charset=utf-8")
            if m := re.fullmatch(r"/api/runs/([^/]+)", path):
                run = d.run(m.group(1))
                return self._json(run) if run else self._error(404, "No existe")
            if path == "/files":
                target = d.file(parse_qs(url.query).get("path", [""])[0])
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix == ".md":
                    kind = "text/plain; charset=utf-8"
                return self._send(200, target.read_bytes(), kind)
            return self._error(404, "No existe")
        except LaunchError as exc:
            return self._error(400, str(exc))
        except PermissionError as exc:
            return self._error(403, str(exc))
        except FileNotFoundError:
            return self._error(404, "No existe")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        d = self.dashboard
        if not self._same_origin():
            return self._error(403, "Origen no permitido")
        try:
            if path == "/api/runs":
                body = self._body()
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    raise LaunchError("args debe ser un objeto")
                run_id = d.launch(str(body.get("command", "")), args, bool(body.get("fake_llm")),
                                  float(body.get("fake_delay") or 0))
                return self._json({"id": run_id}, 201)
            if path == "/api/diag":  # medidas de la ventana del navegador (diagnóstico de diseño)
                progress.write_json(d.settings.out_dir / "_panel_diag.json", self._body())
                return self._json({"ok": True})
            if m := re.fullmatch(r"/api/runs/([^/]+)/cancel", path):
                d.run_path(m.group(1))
                if d.cancel(m.group(1)):
                    return self._json({"ok": True})
                return self._error(409, "Solo se pueden cancelar las ejecuciones lanzadas desde este panel")
            return self._error(404, "No existe")
        except (LaunchError, json.JSONDecodeError, ValueError) as exc:
            return self._error(400, str(exc))

    def _static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            return self._error(404, "No existe")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript",):
            kind += "; charset=utf-8"
        self._send(200, target.read_bytes(), kind)

    def _events(self) -> None:
        """SSE: manda cada ejecución que cambie (completa) y las que desaparecen."""
        d = self.dashboard
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        known = d.scan()
        last_ping = time.monotonic()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not d.stop.is_set():
                time.sleep(SCAN_EVERY)
                now = d.scan()
                for run_id, mtime in now.items():
                    if known.get(run_id) != mtime:
                        data = d.run(run_id)
                        if data:
                            self._event("run", data)
                for run_id in set(known) - set(now):
                    self._event("remove", {"id": run_id})
                known = now
                if time.monotonic() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _event(self, name: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    dashboard = Dashboard(settings)
    handler = type("PanelHandler", (Handler,), {"dashboard": dashboard})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.dashboard = dashboard
    threading.Thread(target=dashboard.watch, daemon=True, name="panel-watch").start()
    return server


def open_in_browser(url: str) -> None:
    """Brave si está instalado; si no, el navegador por defecto."""
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"))
    else:
        candidates += [Path("/usr/bin/brave-browser"), Path("/usr/bin/brave")]
    for exe in candidates:
        if exe.is_file():
            subprocess.Popen([str(exe), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


def serve_dashboard(settings: Settings, port: int = 8765, open_browser: bool = True) -> None:
    server = make_server(settings, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Panel del agente en {url} (Ctrl+C para cerrarlo; las ejecuciones en curso siguen)")
    if open_browser:
        threading.Timer(0.6, open_in_browser, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.dashboard.stop.set()
        server.server_close()
