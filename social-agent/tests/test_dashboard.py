import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from social_agent import progress
from social_agent.config import ROOT, load_settings
from social_agent.dashboard import server as panel


@pytest.fixture
def running(tmp_path):
    settings = replace(load_settings(), out_dir=tmp_path / "out", clients_dir=ROOT / "clients")
    srv = panel.make_server(settings, port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv, base
    srv.dashboard.stop.set()
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.read()


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _write_run(dashboard, **overrides):
    state = progress.initial_state("20261112-101010-plan-demo-0001", "plan", {"month": "2026-11"}, "demo",
                                   status="running", pid=os.getpid())
    state.update(overrides)
    progress.write_json(dashboard.run_path(state["id"]), state)
    return state


def test_index_meta_and_runs(running):
    srv, base = running
    status, html = _get(base + "/")
    assert status == 200 and b"gsap.min.js" in html
    assert _get(base + "/static/vendor/gsap.min.js")[0] == 200
    meta = json.loads(_get(base + "/api/meta")[1])
    assert "demo" in meta["clients"] and "plan" in meta["launch"]
    assert [s["key"] for s in meta["flows"]["plan"]][0] == "marca"

    _write_run(srv.dashboard)
    runs = json.loads(_get(base + "/api/runs")[1])
    assert runs[0]["id"] == "20261112-101010-plan-demo-0001" and runs[0]["steps_total"] == len(progress.FLOWS["plan"])
    run = json.loads(_get(base + "/api/runs/20261112-101010-plan-demo-0001")[1])
    assert run["client"] == "demo" and run["cancellable"] is False


def test_static_and_files_are_confined(running, tmp_path):
    srv, base = running
    for path in ("/static/../server.py", "/static/..%2Fserver.py"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(base + path)
        assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/files?path=" + urllib.request.quote(str(ROOT / "pyproject.toml")))
    assert exc.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/files?path=" + urllib.request.quote(str(ROOT / "out" / ".." / ".env")))
    assert exc.value.code == 403
    image = srv.dashboard.settings.out_dir / "demo" / "2026-11" / "1103-post.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG fake")
    status, body = _get(base + "/files?path=" + urllib.request.quote(str(image)))
    assert status == 200 and body.startswith(b"\x89PNG")


@pytest.mark.parametrize("body, message", [
    ({"command": "serve"}, "no permitido"),
    ({"command": "plan", "args": {"client": "demo"}}, "Falta month"),
    ({"command": "plan", "args": {"client": "demo", "month": "2026-13"}}, "AAAA-MM"),
    ({"command": "plan", "args": {"client": "../etc", "month": "2026-11"}}, "Cliente desconocido"),
    ({"command": "plan", "args": {"client": "demo", "month": "2026-11", "extra": "x"}}, "no permitidos"),
    ({"command": "plan", "args": {"client": "demo", "month": "2026-11", "dry_run": "--rm"}}, "true o false"),
])
def test_launch_rejects_anything_outside_the_allowlist(running, body, message):
    _, base = running
    status, data = _post(base + "/api/runs", body)
    assert status == 400 and message in data["error"]


def test_launch_requires_same_origin(running):
    _, base = running
    status, _ = _post(base + "/api/runs", {"command": "publish"}, {"Origin": "https://otra-web.example"})
    assert status == 403


def test_argv_maps_flags_and_values(running):
    srv, _ = running
    argv = srv.dashboard.argv("plan", {"client": "demo", "month": "2026-11", "dry_run": True, "reemplazar": False})
    assert argv == ["plan", "--client", "demo", "--month", "2026-11", "--dry-run"]
    assert srv.dashboard.argv("onboarding run", {}) == ["onboarding", "run"]


def test_cancel_only_runs_launched_here(running):
    srv, base = running
    _write_run(srv.dashboard)
    status, data = _post(base + "/api/runs/20261112-101010-plan-demo-0001/cancel", {})
    assert status == 409


def test_watch_marks_dead_processes_as_interrupted(running):
    srv, _ = running
    state = _write_run(srv.dashboard, pid=999_999_9)
    state["steps"][0]["status"] = "running"
    progress.write_json(srv.dashboard.run_path(state["id"]), state)
    srv.dashboard.watch_once()
    data = srv.dashboard.run(state["id"])
    assert data["status"] == "interrupted"
    assert data["steps"][0]["status"] == "error"


def test_events_stream_pushes_changed_runs(running):
    srv, base = running
    res = urllib.request.urlopen(base + "/api/events", timeout=5)
    assert res.readline().startswith(b"retry")
    res.readline()
    time.sleep(0.1)
    _write_run(srv.dashboard)
    event = res.readline()
    data = res.readline()
    res.close()
    assert event == b"event: run\n"
    assert json.loads(data.removeprefix(b"data: "))["id"] == "20261112-101010-plan-demo-0001"


def test_launch_runs_the_cli_in_a_subprocess(running):
    """De punta a punta, sin tocar nada externo: demo no tiene parrilla de 2000-01 que rediseñar, así que
    el proceso falla y la ejecución queda en error."""
    srv, base = running
    status, data = _post(base + "/api/runs", {"command": "rediseñar", "args": {"client": "demo", "month": "2000-01"},
                                              "fake_llm": True})
    assert status == 201
    run_id = data["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        srv.dashboard.watch_once()
        run = srv.dashboard.run(run_id)
        if run and run["status"] not in panel.ACTIVE:
            break
        time.sleep(0.3)
    assert run["status"] == "error", srv.dashboard.log_tail(run_id)
    assert run["command"] == "rediseñar" and run["pid"] > 0
