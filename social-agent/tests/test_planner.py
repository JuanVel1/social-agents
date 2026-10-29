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
