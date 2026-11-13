import io
import json
import shutil
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from social_agent.config import ROOT, load_settings
from social_agent.llm.fake import FakeProvider
from social_agent.models import Brand, Moodboard, VisualGuide
from social_agent.moodboard import board_pins, guide_path, pin_image, refresh_moodboard
from social_agent.planner import _brand_brief


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), (30, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _pin(i, kind="image"):
    if kind == "video":
        return {"id": f"p{i}", "media": {"media_type": "video", "cover_image_url": f"https://i.pinimg/{i}.jpg"}}
    if kind == "multiple":
        return {"id": f"p{i}", "media": {"items": [{"images": {"1200x": {"url": f"https://i.pinimg/{i}.jpg"}}}]}}
    if kind == "none":
        return {"id": f"p{i}", "media": {}}
    return {"id": f"p{i}", "title": f"Ref {i}", "media": {"images": {"600x": {"url": f"https://i.pinimg/{i}.jpg"}}}}


def _http(pins_pages, seen):
    def handler(request: httpx.Request):
        seen.append(str(request.url))
        if "api.pinterest.com" in request.url.host:
            assert request.headers["authorization"] == "Bearer tok"
            page = int(request.url.params.get("bookmark", "0"))
            items, more = pins_pages[page], page + 1 < len(pins_pages)
            return httpx.Response(200, json={"items": items, "bookmark": str(page + 1) if more else None})
        return httpx.Response(200, content=_jpeg())
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pin_image_handles_images_carousels_and_videos():
    assert pin_image(_pin(1)).endswith("1.jpg")
    assert pin_image(_pin(2, "multiple")).endswith("2.jpg")
    assert pin_image(_pin(3, "video")).endswith("3.jpg")
    assert pin_image(_pin(4, "none")) is None


def test_board_pins_follows_bookmarks_up_to_limit():
    seen = []
    http = _http([[_pin(1), _pin(2)], [_pin(3)]], seen)
    assert [p["id"] for p in board_pins("tok", "123", http)] == ["p1", "p2", "p3"]
    assert [p["id"] for p in board_pins("tok", "123", _http([[_pin(1), _pin(2)], [_pin(3)]], []), limit=2)] == ["p1", "p2"]


class Recorder(FakeProvider):
    def __init__(self):
        self.calls = []

    def generate(self, prompt, schema, **kwargs):
        self.calls.append((prompt, len(kwargs.get("attachments", ()))))
        return super().generate(prompt, schema, **kwargs)


def _client(tmp_path):
    shutil.copytree(ROOT / "clients" / "demo", tmp_path / "demo")
    brand = Brand.load(tmp_path, "demo").model_copy(update={"moodboard": Moodboard(pinterest="abc", board="123")})
    return replace(load_settings(), clients_dir=tmp_path, pinterest_access_token=""), brand


def test_refresh_moodboard_analyzes_once_and_reaches_the_brief(tmp_path, monkeypatch):
    settings, brand = _client(tmp_path)
    tokens = []
    monkeypatch.setattr("social_agent.moodboard.postiz_token",
                        lambda s, integration, label: tokens.append(integration) or "tok")
    llm = Recorder()
    pages = [[_pin(1), _pin(2, "video"), _pin(3, "none")]]

    result = refresh_moodboard(settings, brand, llm, http=_http(pages, []))
    assert tokens == ["abc"] and result["pines"] == 2 and result["analizado"]
    assert llm.calls[0][1] == 2 and "Ref 1" in llm.calls[0][0]
    saved = json.loads(guide_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert saved["pins"] == ["p1", "p2"] and saved["guide"]["summary"]

    # Mismo tablero, mismos pines: no se vuelve a pagar el análisis.
    assert not refresh_moodboard(settings, brand, llm, http=_http(pages, []))["analizado"]
    assert len(llm.calls) == 1
    assert refresh_moodboard(settings, brand, llm, http=_http(pages, []), force=True)["analizado"]

    loaded = Brand.load(tmp_path, "demo")
    assert loaded.visual_direction.startswith("Fotos luminosas")
    assert "<visual_direction>" in _brand_brief(loaded)
    assert "visual_direction" not in loaded.model_dump()


def test_refresh_moodboard_prefers_env_token_and_needs_a_board(tmp_path, monkeypatch):
    settings, brand = _client(tmp_path)
    monkeypatch.setattr("social_agent.moodboard.postiz_token", lambda *a: pytest.fail("no debe leer Postiz"))
    seen = []
    refresh_moodboard(replace(settings, pinterest_access_token="tok"), brand, FakeProvider(),
                      http=_http([[_pin(1)]], seen))
    assert any("boards/123/pins" in url for url in seen)
    with pytest.raises(RuntimeError, match="moodboard.board"):
        refresh_moodboard(settings, brand.model_copy(update={"moodboard": Moodboard()}), FakeProvider())


def test_brief_without_moodboard_has_no_visual_direction():
    assert "<visual_direction>" not in _brand_brief(Brand.load(ROOT / "clients", "demo"))
    assert VisualGuide(summary="A", avoid=["B"]).as_text() == "A\nEvitar: B"
