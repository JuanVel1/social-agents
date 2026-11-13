from datetime import date

from social_agent.config import ROOT
from social_agent.llm.fake import FakeProvider
from social_agent.llm.gemini import RateLimiter
from social_agent.models import Brand
from social_agent.planner import build_slots, plan_month


def test_plan1_counts_for_november_2026():
    slots = build_slots("2026-11", "plan1")
    counts = {t: sum(1 for s in slots if s.type == t) for t in ("post", "carousel", "story", "reel_script")}
    assert counts == {"post": 9, "carousel": 4, "story": 13, "reel_script": 8}
    assert all(s.date.month == 11 for s in slots)
    assert len({s.id for s in slots}) == len(slots)


def test_plan_month_with_fake_provider_fills_every_piece():
    brand = Brand.load(ROOT / "clients", "demo")
    plan = plan_month(brand, "2026-11", FakeProvider())
    assert all(p.content is not None for p in plan.pieces)
    assert {p.pillar for p in plan.pieces} <= {pl.name for pl in brand.pillars}
    carousels = [p for p in plan.pieces if p.type == "carousel"]
    assert all(len(p.content.slides) >= 6 for p in carousels)
    assert all(p.hashtags for p in plan.pieces if p.type in ("post", "carousel"))
    assert plan.pieces[0].date == date(2026, 11, 2)


def test_rate_limiter_waits_after_max_calls():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    now[0] += 1
    limiter.wait()
    now[0] += 1
    limiter.wait()
    assert slept == [58.0]


def test_database_id_accepts_link_or_id():
    from social_agent.approval import database_id_from

    raw = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
    assert database_id_from(raw) == raw
    assert database_id_from("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d") == raw
    assert database_id_from(f"https://www.notion.so/agencia/Parrilla-{raw}?v=ffffffffffffffffffffffffffffffff") == raw
    assert database_id_from(f"https://app.notion.com/p/{raw}") == raw


def test_gemini_falls_back_when_model_is_busy():
    from dataclasses import replace

    from google.genai import errors

    from social_agent.config import load_settings
    from social_agent.llm.gemini import GeminiProvider

    calls = []

    class Models:
        def generate_content(self, model, contents, config):
            calls.append(model)
            if model == "principal":
                raise errors.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
            return "respuesta"

    class Client:
        models = Models()

    settings = replace(load_settings(), gemini_api_key="x", gemini_model_quality="principal",
                       gemini_fallback_models=("respaldo",), gemini_max_rpm=100)
    provider = GeminiProvider(settings, client=Client(), sleep=lambda s: None)
    assert provider._call("principal", "hola", None) == "respuesta"
    assert calls == ["principal", "principal", "respaldo"]


def _postiz_recorder():
    import json as _json

    import httpx

    from social_agent.publish import PostizClient

    sent = []

    def handler(request):
        sent.append(_json.loads(request.content))
        return httpx.Response(200, json=[{"postId": "p1"}])

    http = httpx.Client(base_url="https://postiz.test/api/public/v1", transport=httpx.MockTransport(handler))
    return PostizClient("https://postiz.test/api", "key", client=http), sent


def test_schedule_facebook_only_carousel():
    import pytest

    from social_agent.models import Piece

    brand = Brand.load(ROOT / "clients", "demo").model_copy(update={"postiz_integrations": {"facebook": "fb1"}})
    plan = plan_month(brand, "2026-11", FakeProvider())
    carousel = next(p for p in plan.pieces if p.type == "carousel")
    postiz, sent = _postiz_recorder()

    postiz.schedule(brand, carousel, "America/Bogota")
    posts = sent[0]["posts"]
    assert [p["integration"]["id"] for p in posts] == ["fb1"]
    assert posts[0]["settings"] == {"__type": "facebook"}
    assert sent[0]["date"].startswith("2026-11-03T12:00:00-05:00")

    story = next(p for p in plan.pieces if p.type == "story")
    with pytest.raises(ValueError):
        postiz.schedule(brand, story, "America/Bogota")
    assert len(sent) == 1
    assert isinstance(story, Piece)


def test_schedule_pinterest_limits_and_skips_without_board():
    from social_agent.models import Slide

    brand = Brand.load(ROOT / "clients", "demo").model_copy(update={
        "postiz_integrations": {"facebook": "fb1", "pinterest": "pin1"},
        "pinterest_board": "123456", "website": "https://emprendeconvic.com"})
    plan = plan_month(brand, "2026-11", FakeProvider())
    carousel = next(p for p in plan.pieces if p.type == "carousel")
    carousel.content.headline = "Titular " * 20
    carousel.content.caption = "palabra " * 100
    carousel.content.slides = [Slide(heading="h", body="b")] * 7
    carousel.postiz_media = [{"id": str(i), "path": f"/m{i}.png"} for i in range(7)]
    postiz, sent = _postiz_recorder()

    postiz.schedule(brand, carousel, "America/Bogota")
    facebook, pinterest = sent[0]["posts"]
    assert len(facebook["value"][0]["image"]) == 7 and facebook["value"][0]["content"] == carousel.caption_full
    settings = pinterest["settings"]
    assert settings["__type"] == "pinterest" and settings["board"] == "123456"
    assert settings["link"] == "https://emprendeconvic.com" and len(settings["title"]) <= 100
    assert len(pinterest["value"][0]["content"]) <= 500 and pinterest["value"][0]["content"].endswith("…")
    assert len(pinterest["value"][0]["image"]) == 5

    no_board = brand.model_copy(update={"pinterest_board": ""})
    postiz.schedule(no_board, carousel, "America/Bogota")
    assert [p["integration"]["id"] for p in sent[1]["posts"]] == ["fb1"]


def test_brand_brief_uses_objectives_and_hides_competitors():
    from social_agent.planner import _brand_brief

    brand = Brand.load(ROOT / "clients", "demo").model_copy(
        update={"objectives": ["Ventas"], "competitors": ["Café Velvet"]})
    brief = _brand_brief(brand)
    assert "Ventas: comprar o reservar" in brief
    assert "<competitors>Café Velvet</competitors>" in brief and "Nunca los nombres" in brief
    assert "<objectives>" not in _brand_brief(Brand.load(ROOT / "clients", "demo"))


def test_hashtags_with_competitor_names_are_dropped():
    brand = Brand.load(ROOT / "clients", "demo").model_copy(update={"competitors": ["Emprendimiento"]})
    plan = plan_month(brand, "2026-11", FakeProvider())
    tags = {h for p in plan.pieces for h in p.hashtags}
    assert tags == {"ejemplo", "marca"}


def test_serve_schedules_onboarding_only_when_configured():
    from dataclasses import replace

    from apscheduler.schedulers.background import BackgroundScheduler

    from social_agent.config import load_settings
    from social_agent.jobs import register_jobs

    settings = load_settings()
    with_form = register_jobs(BackgroundScheduler(), replace(settings, notion_onboarding_database_id="abc"))
    without = register_jobs(BackgroundScheduler(), replace(settings, notion_onboarding_database_id=""))
    assert set(with_form) == {"parrilla", "publicar", "onboarding"}
    assert set(without) == {"parrilla", "publicar"}


def test_gemini_retries_network_errors():
    import httpx

    from social_agent.config import load_settings
    from social_agent.llm.gemini import GeminiProvider

    calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise httpx.ReadError("conexión anulada")
            return "ok"

    class Client:
        models = Models()

    provider = GeminiProvider(load_settings(), client=Client(), sleep=lambda s: None)
    assert provider._call("modelo", "hola", None) == "ok"
    assert len(calls) == 2


def test_week_prompt_asks_for_highlight_and_visual():
    prompts = []

    class Recording(FakeProvider):
        def generate(self, prompt, schema, **kwargs):
            prompts.append(prompt)
            return super().generate(prompt, schema, **kwargs)

    plan = plan_month(Brand.load(ROOT / "clients", "demo"), "2026-11", Recording())
    week = next(p for p in prompts if "Escribe el contenido completo" in p)
    assert "highlight:" in week and "visual:" in week
    assert all(p.content.visual for p in plan.pieces)


def test_gemini_uses_the_backup_key_before_waiting():
    from dataclasses import replace

    from google.genai import errors

    from social_agent.config import load_settings
    from social_agent.llm.base import NoQuotaError
    from social_agent.llm.gemini import GeminiProvider

    calls, sleeps = [], []

    def client(label, fail):
        class Models:
            def generate_content(self, model, contents, config):
                calls.append((label, model))
                if fail == "busy":
                    raise errors.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
                if fail == "quota":
                    raise errors.ClientError(429, {"error": {"message": "quota exceeded, limit: 0", "status": "X"}})
                return f"respuesta de {label}"

        return type("Client", (), {"models": Models()})()

    settings = replace(load_settings(), gemini_api_key="x", gemini_api_key_backup="y",
                       gemini_fallback_models=("otro",), gemini_max_rpm=100)
    provider = GeminiProvider(settings, client=client("principal", "busy"), backup=client("respaldo", None),
                              sleep=sleeps.append)
    assert provider._call("modelo", "hola", None) == "respuesta de respaldo"
    assert calls == [("principal", "modelo"), ("respaldo", "modelo")] and sleeps == []  # sin esperar

    calls.clear()
    both = GeminiProvider(settings, client=client("principal", "quota"), backup=client("respaldo", "quota"),
                          sleep=sleeps.append)
    try:
        both._call("modelo", "hola", None)
    except NoQuotaError:
        pass
    else:
        raise AssertionError("sin cuota en ninguna clave debe avisar")
    assert set(calls) == {("principal", "modelo"), ("respaldo", "modelo"), ("principal", "otro"), ("respaldo", "otro")}

    alone = GeminiProvider(settings, client=client("principal", None), sleep=sleeps.append)
    assert len(alone.keys) == 1  # con un cliente inyectado no se crea el de respaldo


def test_run_month_replace_archives_only_pending(tmp_path, monkeypatch):
    from dataclasses import replace

    from social_agent import pipeline
    from social_agent.config import load_settings

    from social_agent.config import ROOT
    from social_agent.llm.fake import FakeProvider
    from social_agent.models import Brand

    brand = Brand.load(ROOT / "clients", "demo")
    monkeypatch.setattr(pipeline.Brand, "load", classmethod(lambda cls, d, slug: brand))
    monkeypatch.setattr(pipeline, "render_plan", lambda plan, *a: None)
    settings = replace(load_settings(), clients_dir=tmp_path, out_dir=tmp_path / "out", llm_provider="fake")
    events, sent = [], []

    class Board:
        def __init__(self, *a):
            pass

        def rows(self, prefix):
            return {f"{prefix}vieja": ("p1", "Pendiente"), f"{prefix}{first}": ("p2", "Aprobado")}

        def archive(self, page_id):
            events.append(page_id)

    first = pipeline.plan_month(brand, "2026-12", FakeProvider()).pieces[0].id
    monkeypatch.setattr(pipeline, "NotionBoard", Board)
    monkeypatch.setattr(pipeline, "send_for_approval", lambda s, plan, pieces: sent.extend(p.id for p in pieces))
    plan = pipeline.run_month(settings, "demo", "2026-12", dry_run=False, replace=True)
    assert events == ["p1"]  # la aprobada no se archiva
    assert first not in sent and len(sent) == len(plan.pieces) - 1
