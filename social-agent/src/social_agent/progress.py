"""Estado en vivo de cada ejecución del agente, para el panel local (`social-agent ui`).

Cada ejecución (un comando de la CLI, un job de `serve` o una corrida lanzada desde el panel) escribe su
estado en out/_runs/<id>.json; el panel lee esos archivos. Así se ve lo que corre en cualquier proceso.

Las funciones de este módulo no hacen nada si no hay una ejecución activa: el resto del código las llama
sin cambiar de firma y las pruebas siguen igual.

    with progress.run("plan", runs_dir, args={...}, client="demo"):
        with progress.step("render", total=len(pieces)):
            for piece in pieces:
                ...
                progress.advance(item=piece.id)
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import secrets
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# Pasos de cada comando, en orden: (clave, nombre, peso en el % global). El panel pinta todos desde el inicio.
FLOWS: dict[str, list[tuple[str, str, float]]] = {
    "plan": [
        ("marca", "Marca", 1),
        ("referencias", "Plantillas del moodboard", 1),
        ("temas", "Temas del mes", 6),
        ("asignar_referencias", "Asignar referencias", 4),
        ("copy", "Textos por semana", 25),
        ("campos", "Campos de plantilla", 3),
        ("hashtags", "Hashtags", 3),
        ("asignar_fotos", "Elegir fotos", 4),
        ("fotos_ia", "Fotos con IA", 15),
        ("poses_referencia", "Poses de referencias", 1),
        ("imagenes_ia", "Imágenes completas con IA", 20),
        ("render", "Diseño de imágenes", 20),
        ("guardar", "Guardar parrilla", 1),
        ("archivar", "Archivar pendientes", 2),
        ("aprobacion", "Enviar a aprobación", 10),
    ],
    "rediseñar": [
        ("cargar", "Cargar parrilla", 1),
        ("asignar_fotos", "Elegir poses", 4),
        ("visuales", "Describir visuales", 4),
        ("fotos_ia", "Fotos con IA", 30),
        ("render", "Diseño de imágenes", 20),
        ("guardar", "Guardar parrilla", 1),
        ("notion", "Revisar filas en Notion", 2),
        ("aprobacion", "Enviar a aprobación", 10),
    ],
    "publish": [
        ("aprobados", "Leer aprobados en Notion", 2),
        ("programar", "Programar en Postiz", 8),
    ],
    "fotos": [
        ("recortes", "Recortar publicaciones", 6),
        ("revision", "Revisión con IA", 4),
        ("banco", "Guardar banco", 1),
    ],
    "moodboard": [
        ("direccion", "Dirección visual", 5),
        ("revalidar", "Revalidar plantillas", 3),
        ("pines", "Descargar pines", 2),
        ("personas", "Personas reales en pines", 3),
        ("plantillas", "Diseñar plantillas", 15),
    ],
    "onboarding run": [
        ("preparar", "Preparar base", 1),
        ("nuevas", "Rellenar con el manual", 6),
        ("activar", "Activar aprobadas", 3),
    ],
}

TITLES = {
    "plan": "Parrilla del mes",
    "rediseñar": "Rediseño de parrilla",
    "publish": "Publicar aprobados",
    "fotos": "Banco de fotos",
    "moodboard": "Moodboard",
    "onboarding run": "Onboarding",
}

RUN_ENV = "SOCIAL_AGENT_RUN_ID"
RUNS_DIR_ENV = "SOCIAL_AGENT_RUNS_DIR"  # otra carpeta de estado (las pruebas la usan para no ensuciar out/)
KEEP_RUNS = 200
MAX_LOGS = 60
MAX_ITEMS = 300
MAX_OUTPUTS = 120
WRITE_EVERY = 0.25  # s entre escrituras de avances (los cambios de estado se escriben siempre)

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Step:
    key: str
    label: str
    weight: float = 1
    status: str = "pending"  # pending | running | waiting | done | error | skipped
    done: int = 0
    total: int | None = None
    started: str | None = None
    finished: str | None = None
    detail: str = ""
    activity: str = ""
    items: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    error: str = ""
    wait: dict | None = None

    @property
    def fraction(self) -> float:
        if self.status in ("done", "error"):
            return 1.0
        if self.total:
            return min(self.done / self.total, 1.0)
        return 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["pct"] = round(self.fraction * 100, 1) if (self.total or self.status in ("done", "error")) else None
        return data


@dataclass
class Run:
    id: str
    command: str
    path: Path
    args: dict = field(default_factory=dict)
    client: str = ""
    group: str = ""
    title: str = ""
    status: str = "running"  # starting | running | done | error | cancelled | interrupted
    pid: int = field(default_factory=os.getpid)
    started: str = field(default_factory=_now)
    finished: str | None = None
    error: str = ""
    summary: list[str] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    discard_if_empty: bool = False
    _stack: list[Step] = field(default_factory=list, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _last_write: float = field(default=0.0, repr=False)
    _active: bool = field(default=False, repr=False)

    @property
    def pct(self) -> float:
        steps = [s for s in self.steps if s.status != "skipped"]
        weight = sum(s.weight for s in steps)
        if not weight:
            return 100.0 if self.status == "done" else 0.0
        return round(100 * sum(s.weight * s.fraction for s in steps) / weight, 1)

    def find(self, key: str, label: str | None = None) -> Step:
        for s in self.steps:
            if s.key == key:
                return s
        s = Step(key=key, label=label or key.replace("_", " ").capitalize())
        self.steps.append(s)
        return s

    @property
    def current(self) -> Step | None:
        return self._stack[-1] if self._stack else None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "command": self.command, "title": self.title, "args": self.args,
            "client": self.client, "group": self.group, "status": self.status, "pid": self.pid,
            "started": self.started, "finished": self.finished, "updated": _now(), "error": self.error,
            "pct": 100.0 if self.status == "done" else self.pct, "summary": self.summary, "logs": self.logs,
            "steps": [s.as_dict() for s in self.steps],
        }

    def has_activity(self) -> bool:
        return self.status == "error" or any(s.done or s.status == "error" for s in self.steps)

    def save(self, force: bool = False) -> None:
        with self._lock:
            if self.discard_if_empty and not self.has_activity():
                return
            now = time.monotonic()
            if not force and now - self._last_write < WRITE_EVERY:
                return
            self._last_write = now
            write_json(self.path, self.as_dict())


def write_json(path: Path, data: dict) -> None:
    """Escritura atómica: el panel nunca lee un archivo a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: el panel puede tener el archivo abierto un instante
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)


_current: contextvars.ContextVar[Run | None] = contextvars.ContextVar("social_agent_run", default=None)
_env_id_used = False


def current() -> Run | None:
    return _current.get()


def new_id(command: str, client: str = "") -> str:
    slug = "-".join(part for part in (command.replace(" ", "-"), client) if part)
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slug}-{secrets.token_hex(2)}"


def initial_state(run_id: str, command: str, args: dict, client: str = "", status: str = "starting",
                  pid: int | None = None) -> dict:
    """Estado de una corrida antes de que su proceso arranque (la que lanza el panel)."""
    r = Run(id=run_id, command=command, path=Path(), args=args, client=client, status=status,
            title=TITLES.get(command, command), pid=pid or 0)
    r.steps = [Step(key=k, label=label, weight=w) for k, label, w in FLOWS.get(command, [])]
    return r.as_dict()


def _prune(runs_dir: Path) -> None:
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_RUNS]:
        old.unlink(missing_ok=True)
        old.with_suffix(".log").unlink(missing_ok=True)


@contextlib.contextmanager
def run(command: str, runs_dir: Path, *, args: dict | None = None, client: str = "", group: str = "",
        discard_if_empty: bool = False) -> Iterator[Run]:
    """Abre una ejecución. Si falla, queda en error con el motivo y la excepción sigue su curso."""
    global _env_id_used
    runs_dir = Path(os.environ.get(RUNS_DIR_ENV) or runs_dir)
    run_id = None
    if not _env_id_used and os.environ.get(RUN_ENV):
        run_id, _env_id_used = os.environ[RUN_ENV], True
    run_id = run_id or new_id(command, client)
    r = Run(id=run_id, command=command, path=runs_dir / f"{run_id}.json", args=dict(args or {}), client=client,
            group=group, title=TITLES.get(command, command), discard_if_empty=discard_if_empty)
    r.steps = [Step(key=k, label=label, weight=w) for k, label, w in FLOWS.get(command, [])]
    r._active = True
    _install_handler()
    with contextlib.suppress(OSError):
        runs_dir.mkdir(parents=True, exist_ok=True)
        _prune(runs_dir)
    token = _current.set(r)
    r.save(force=True)
    try:
        yield r
    except BaseException as exc:
        with r._lock:
            r.status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "error"
            r.error = f"{type(exc).__name__}: {exc}"
            for s in r._stack:
                s.status, s.finished = "error", _now()
                s.error = s.error or r.error
        raise
    else:
        if r.status in ("running", "starting"):
            r.status = "done"
    finally:
        with r._lock:
            r._active = False
            r.finished = _now()
            for s in r.steps:
                if s.status == "pending":
                    s.status = "skipped"
                elif s.status in ("running", "waiting"):
                    s.status, s.finished = ("done" if r.status == "done" else "error"), r.finished
            _current.reset(token)
            if r.discard_if_empty and not r.has_activity():
                r.path.unlink(missing_ok=True)
            else:
                r.save(force=True)


def fail(message: str) -> None:
    """Marca la ejecución como fallida sin excepción (p. ej. un comando que termina con código 1)."""
    if r := current():
        with r._lock:
            r.status, r.error = "error", message
        r.save(force=True)


def summary(line: str) -> None:
    """Línea de resultado (lo que la CLI imprime al final)."""
    if (r := current()) and line.strip():
        with r._lock:
            r.summary.append(line.rstrip())
        r.save()


@contextlib.contextmanager
def step(key: str, total: int | None = None, label: str | None = None) -> Iterator[Step | None]:
    """Un paso del flujo: pasa a running y al salir a done (o a error si hay excepción)."""
    r = current()
    if r is None:
        yield None
        return
    with r._lock:
        s = r.find(key, label)
        s.status, s.started, s.finished, s.error, s.wait = "running", _now(), None, "", None
        s.done, s.total, s.activity = 0, total, ""
        s.items.clear()
        r._stack.append(s)
    r.save(force=True)
    try:
        yield s
    except BaseException as exc:
        with r._lock:
            s.status, s.error = "error", f"{type(exc).__name__}: {exc}"
            s.logs.append({"t": _now(), "level": "ERROR",
                           "msg": "".join(traceback.format_exception_only(type(exc), exc)).strip()})
        raise
    else:
        with r._lock:
            if s.status in ("running", "waiting"):
                s.status = "done"
    finally:
        with r._lock:
            s.finished, s.wait, s.activity = _now(), None, ""
            if s in r._stack:
                r._stack.remove(s)
        r.save(force=True)


def tracked(key: str, label: str | None = None):
    """Decorador: toda la función es un paso."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with step(key, label=label):
                return fn(*args, **kwargs)
        return inner
    return wrap


def skip(key: str, reason: str = "") -> None:
    if r := current():
        with r._lock:
            s = r.find(key)
            if s.status == "pending":
                s.status, s.detail = "skipped", reason
        r.save(force=True)


def _step() -> tuple[Run | None, Step | None]:
    r = current()
    return r, (r.current if r else None)


def set_total(total: int) -> None:
    r, s = _step()
    if s:
        with r._lock:
            s.total = total
        r.save(force=True)


def advance(n: int = 1, item: str | None = None, status: str = "done", note: str = "") -> None:
    """Avanza el paso en curso; `item` queda en su lista (semana, pieza, pose…)."""
    r, s = _step()
    if not s:
        return
    with r._lock:
        s.done += n
        if item is not None and len(s.items) < MAX_ITEMS:
            s.items.append({"label": item, "status": status, "note": note, "t": _now()})
    r.save()


def detail(text: str) -> None:
    r, s = _step()
    if s:
        with r._lock:
            s.detail = text
        r.save()


def activity(text: str) -> None:
    """Lo que está haciendo en este instante (p. ej. esperando la respuesta de un modelo)."""
    r, s = _step()
    if s:
        with r._lock:
            s.activity = text
        r.save()


def metric(name: str, value: Any) -> None:
    r, s = _step()
    if s:
        with r._lock:
            s.metrics[name] = value
        r.save()


def count(name: str, n: int = 1) -> None:
    r, s = _step()
    if s:
        with r._lock:
            s.metrics[name] = s.metrics.get(name, 0) + n
        r.save()


def output(path: Path | str) -> None:
    r, s = _step()
    if s:
        with r._lock:
            if len(s.outputs) < MAX_OUTPUTS:
                s.outputs.append(str(Path(path).resolve()))
        r.save()


def llm_call(model: str) -> None:
    """Una llamada a un modelo: cuenta y muestra a quién se espera."""
    r, s = _step()
    if s:
        with r._lock:
            s.metrics["Llamadas al modelo"] = s.metrics.get("Llamadas al modelo", 0) + 1
            s.activity = f"Esperando respuesta de {model}…"
        r.save()


@contextlib.contextmanager
def waiting(reason: str, seconds: float) -> Iterator[None]:
    """Espera visible (límite de solicitudes, reintento): el panel muestra la cuenta regresiva."""
    r, s = _step()
    if not s:
        yield
        return
    with r._lock:
        previous = s.status
        s.status = "waiting"
        s.wait = {"reason": reason, "seconds": round(seconds, 1),
                  "until": datetime.fromtimestamp(time.time() + seconds).astimezone().isoformat()}
    r.save(force=True)
    try:
        yield
    finally:
        with r._lock:
            if s.status == "waiting":
                s.status = previous if previous != "waiting" else "running"
            s.wait = None
        r.save(force=True)


class RunLogHandler(logging.Handler):
    """Los logs de social_agent van al paso en curso de la ejecución de este hilo."""

    def emit(self, record: logging.LogRecord) -> None:
        r = current()
        if r is None or not r._active:
            return
        try:
            entry = {"t": _now(), "level": record.levelname, "msg": record.getMessage()[:2000]}
            with r._lock:
                target = r.current.logs if r.current else r.logs
                target.append(entry)
                del target[:-MAX_LOGS]
            r.save(force=record.levelno >= logging.WARNING)
        except Exception:  # un log nunca debe tumbar la ejecución
            pass


_handler: RunLogHandler | None = None


def _install_handler() -> None:
    global _handler
    if _handler is None:
        _handler = RunLogHandler(level=logging.INFO)
        logging.getLogger("social_agent").addHandler(_handler)


class Tee:
    """Copia lo que la CLI imprime al resumen de la ejecución."""

    def __init__(self, stream):
        self.stream = stream
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            summary(line)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)
