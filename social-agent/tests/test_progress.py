import json
import logging
from dataclasses import replace

import pytest

from social_agent import progress
from social_agent.config import load_settings


@pytest.fixture(autouse=True)
def _explicit_runs_dir(monkeypatch):
    """Aquí cada prueba pasa su carpeta a progress.run: sin el override del conftest."""
    monkeypatch.delenv(progress.RUNS_DIR_ENV, raising=False)


def _read(runs_dir):
    files = list(runs_dir.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def _steps(data):
    return {s["key"]: s for s in data["steps"]}


def test_functions_are_noops_without_a_run():
    with progress.step("render", total=3) as s:
        progress.advance(item="x")
        progress.detail("nada")
        progress.metric("k", 1)
        with progress.waiting("espera", 1):
            pass
    assert s is None
    progress.skip("render")
    progress.fail("x")
    assert progress.current() is None


def test_step_lifecycle_and_weighted_pct(tmp_path):
    with progress.run("publish", tmp_path) as r:
        data = _read(tmp_path)
        assert data["status"] == "running"
        assert [s["status"] for s in data["steps"]] == ["pending", "pending"]
        with progress.step("aprobados"):
            progress.metric("Aprobadas", 4)
        with progress.step("programar", total=4):
            progress.advance(item="a")
            progress.advance(item="b", status="error", note="falló")
            # 2 de 4 en programar (peso 8) + aprobados listo (peso 2) → (2 + 8·0.5) / 10
            assert r.pct == 60.0
            r.save(force=True)
            live = _steps(_read(tmp_path))["programar"]
            assert live["status"] == "running" and live["pct"] == 50.0
            assert [i["status"] for i in live["items"]] == ["done", "error"]
    data = _read(tmp_path)
    assert data["status"] == "done" and data["pct"] == 100.0 and data["finished"]
    assert _steps(data)["aprobados"]["metrics"] == {"Aprobadas": 4}


def test_unrun_steps_end_skipped_and_explicit_skip_keeps_reason(tmp_path):
    with progress.run("publish", tmp_path):
        with progress.step("aprobados"):
            pass
        progress.skip("programar", "Nada aprobado")
    steps = _steps(_read(tmp_path))
    assert steps["programar"]["status"] == "skipped"
    assert steps["programar"]["detail"] == "Nada aprobado"


def test_exception_marks_step_and_run_as_error(tmp_path):
    with pytest.raises(RuntimeError):
        with progress.run("plan", tmp_path, client="demo"):
            with progress.step("marca"):
                pass
            with progress.step("temas"):
                raise RuntimeError("sin cuota")
    data = _read(tmp_path)
    steps = _steps(data)
    assert data["status"] == "error" and "sin cuota" in data["error"]
    assert steps["marca"]["status"] == "done"
    assert steps["temas"]["status"] == "error" and "sin cuota" in steps["temas"]["error"]
    assert steps["render"]["status"] == "skipped"


def test_caught_step_error_does_not_fail_the_run(tmp_path):
    with progress.run("plan", tmp_path):
        try:
            with progress.step("fotos_ia"):
                raise ValueError("sin cuota de imágenes")
        except ValueError:
            pass
    data = _read(tmp_path)
    assert data["status"] == "done"
    assert _steps(data)["fotos_ia"]["status"] == "error"


def test_fail_marks_run_without_exception(tmp_path):
    with progress.run("fotos", tmp_path):
        progress.fail("Terminó con código 1")
    assert _read(tmp_path)["status"] == "error"


def test_unknown_step_is_appended(tmp_path):
    with progress.run("publish", tmp_path):
        with progress.step("extra", label="Paso extra"):
            progress.advance()
    steps = _steps(_read(tmp_path))
    assert steps["extra"]["label"] == "Paso extra" and steps["extra"]["status"] == "done"


def test_discard_if_empty(tmp_path):
    with progress.run("publish", tmp_path, discard_if_empty=True):
        with progress.step("aprobados"):
            assert not list(tmp_path.glob("*.json"))  # sin actividad no se escribe nada
        progress.skip("programar")
    assert not list(tmp_path.glob("*.json"))

    with progress.run("publish", tmp_path, discard_if_empty=True):
        with progress.step("programar", total=1):
            progress.advance(item="cliente/2026-12/1203-post")
    assert _read(tmp_path)["status"] == "done"


def test_logs_go_to_the_current_step(tmp_path):
    log = logging.getLogger("social_agent.prueba")
    log.setLevel(logging.INFO)
    with progress.run("publish", tmp_path):
        log.info("antes de los pasos")
        with progress.step("aprobados"):
            log.warning("Escribiendo semana %s", 49)
    data = _read(tmp_path)
    assert data["logs"][0]["msg"] == "antes de los pasos"
    entry = _steps(data)["aprobados"]["logs"][0]
    assert entry == {**entry, "level": "WARNING", "msg": "Escribiendo semana 49"}


def test_waiting_sets_and_clears_state(tmp_path):
    with progress.run("publish", tmp_path) as r:
        with progress.step("aprobados"):
            with progress.waiting("Límite de 8 solicitudes por minuto", 12):
                s = r.current
                assert s.status == "waiting" and s.wait["seconds"] == 12
            assert s.status == "running" and s.wait is None


def test_run_id_from_environment_is_used_once(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "_env_id_used", False)
    monkeypatch.setenv(progress.RUN_ENV, "20261112-000000-plan-demo-abcd")
    with progress.run("plan", tmp_path) as first:
        pass
    with progress.run("plan", tmp_path) as second:
        pass
    assert first.id == "20261112-000000-plan-demo-abcd"
    assert second.id != first.id


def test_runs_dir_env_override(tmp_path, monkeypatch):
    target = tmp_path / "otra"
    monkeypatch.setenv(progress.RUNS_DIR_ENV, str(target))
    with progress.run("publish", tmp_path / "ignorada"):
        pass
    assert list(target.glob("*.json")) and not (tmp_path / "ignorada").exists()


def test_tee_copies_printed_lines_to_summary(tmp_path, capsys):
    import contextlib
    import sys

    with progress.run("fotos", tmp_path), contextlib.redirect_stdout(progress.Tee(sys.stdout)):
        print("Recortes nuevos: 3 (2 útiles)")
    assert _read(tmp_path)["summary"] == ["Recortes nuevos: 3 (2 útiles)"]
    assert "Recortes nuevos" in capsys.readouterr().out


def test_run_month_reports_every_step(tmp_path, monkeypatch):
    """Una parrilla en simulación termina con todos los pasos listos u omitidos (ninguno pendiente)."""
    from social_agent import pipeline

    def fake_render(plan, brand, templates_dir, clients_dir, out_dir):
        pieces = [p for p in plan.pieces if p.type != "reel_script"]
        with progress.step("render", total=len(pieces)):
            for p in pieces:
                progress.advance(item=p.id)

    monkeypatch.setattr(pipeline, "render_plan", fake_render)
    settings = replace(load_settings(), llm_provider="fake", out_dir=tmp_path / "out")
    runs = tmp_path / "runs"
    monkeypatch.setenv(progress.RUNS_DIR_ENV, str(runs))
    with progress.run("plan", runs, client="demo", args={"month": "2026-11"}):
        pipeline.run_month(settings, "demo", "2026-11", dry_run=True)
    data = _read(runs)
    steps = _steps(data)
    assert data["status"] == "done"
    assert all(s["status"] in ("done", "skipped") for s in data["steps"])
    assert steps["copy"]["total"] == steps["copy"]["done"] > 0
    assert steps["render"]["status"] == "done" and steps["render"]["done"] > 0
    assert steps["aprobacion"]["status"] == "skipped"
    assert steps["temas"]["metrics"]["Llamadas al modelo"] == 1
