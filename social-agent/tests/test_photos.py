import io
import json
import subprocess
from dataclasses import replace
from datetime import date

import httpx
import numpy as np
import pytest
from PIL import Image

from social_agent.config import ROOT, load_settings
from social_agent.llm.fake import FakeProvider
from social_agent.models import (
    Brand,
    MonthPlan,
    PhotoChoice,
    PhotoChoices,
    PhotoReview,
    PhotoReviewBatch,
    Piece,
    PieceContent,
    Slide,
)
from social_agent.photos import (
    assign_photos,
    instagram_token,
    largest_figure,
    load_bank,
    media_images,
    refresh_bank,
    usable_photos,
)
from social_agent.render import environment, highlight


def _brand(**update):
    brand = Brand.load(ROOT / "clients", "demo")
    return brand.model_copy(update={"postiz_integrations": {"instagram-standalone": "abc123"}, **update})


def _png(color=(200, 30, 30, 255), size=(40, 60)) -> bytes:
    """PNG con la mitad izquierda de un color y la derecha de otro, para detectar el espejo."""
    image = Image.new("RGBA", size, color)
    image.paste((10, 10, 200, 255), (size[0] // 2, 0, size[0], size[1]))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_largest_figure_drops_leftovers_and_tiny_figures():
    rgba = np.zeros((100, 100, 4), dtype=np.uint8)
    rgba[10:90, 10:50, 3] = 255  # la persona
    rgba[5:8, 80:83, 3] = 255  # resto de una lámpara
    kept = largest_figure(rgba)
    assert kept[50, 30, 3] == 255 and kept[6, 81, 3] == 0

    tiny = np.zeros((100, 100, 4), dtype=np.uint8)
    tiny[0:5, 0:5, 3] = 255
    assert largest_figure(tiny) is None


def test_media_images_uses_reel_covers_and_carousel_slides():
    reel = {"id": "1", "media_type": "VIDEO", "thumbnail_url": "https://x/portada.jpg"}
    carousel = {"id": "2", "media_type": "CAROUSEL_ALBUM", "children": {"data": [
        {"media_type": "IMAGE", "media_url": "https://x/a.jpg"},
        {"media_type": "VIDEO", "thumbnail_url": "https://x/b.jpg"},
    ]}}
    assert media_images(reel) == [("1-0", "https://x/portada.jpg")]
    assert media_images(carousel) == [("2-0", "https://x/a.jpg"), ("2-1", "https://x/b.jpg")]


def test_instagram_token_reads_postiz_database():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="IGAAtoken\n", stderr="")

    assert instagram_token(load_settings(), _brand(), run=run) == "IGAAtoken"
    assert calls[0][:3] == ["docker", "exec", "postiz-postgres"] and "abc123" in calls[0][-1]
    with pytest.raises(ValueError):
        instagram_token(load_settings(), _brand(postiz_integrations={"instagram-standalone": "x'; drop"}), run=run)
    with pytest.raises(RuntimeError, match="instagram-standalone"):
        instagram_token(load_settings(), _brand(postiz_integrations={"facebook": "f1"}), run=run)


class MirrorLLM(FakeProvider):
    """La primera foto sale con texto al revés; la segunda no sirve."""

    def generate(self, prompt, schema, **kwargs):
        if schema is PhotoReviewBatch:
            return PhotoReviewBatch(reviews=[
                PhotoReview(id="", usable=True, mirrored_text=True, description="Sonriendo", framing="cara"),
                PhotoReview(id="", usable=False, reason="Texto pegado"),
            ])
        return super().generate(prompt, schema, **kwargs)


def test_refresh_bank_saves_cutouts_flips_mirrored_and_skips_seen(tmp_path):
    def handler(request):
        if request.url.host == "graph.instagram.com":
            return httpx.Response(200, json={"data": [
                {"id": "10", "media_type": "IMAGE", "media_url": "https://cdn/1.jpg", "permalink": "https://ig/p/10",
                 "timestamp": "2026-07-01T10:00:00+0000"},
                {"id": "11", "media_type": "VIDEO", "thumbnail_url": "https://cdn/2.jpg", "timestamp": "2026-07-02"},
            ]})
        return httpx.Response(200, content=b"jpg")

    settings = replace(load_settings(), clients_dir=tmp_path)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = refresh_bank(settings, _brand(), MirrorLLM(), token="t", http=http, cutter=lambda data: _png())
    assert result == {"nuevas": 2, "utiles_nuevas": 1, "utiles": 1}

    bank = load_bank(tmp_path, "demo")
    assert bank["seen"] == ["10-0", "11-0"]
    good = next(p for p in bank["photos"] if p["id"] == "10-0")
    assert good["usable"] and good["source"] == "https://ig/p/10" and good["date"] == "2026-07-01"
    flipped = Image.open(tmp_path / "demo" / "fotos" / "10-0.png").convert("RGBA")
    assert flipped.getpixel((0, 0))[:3] == (10, 10, 200)  # el lado derecho pasó a la izquierda
    assert [p["file"] for p in usable_photos(tmp_path, "demo")] == ["10-0.png"]

    again = refresh_bank(settings, _brand(), MirrorLLM(), token="t", http=http,
                         cutter=lambda data: pytest.fail("ya procesada"))
    assert again["nuevas"] == 0


def _plan():
    content = PieceContent(slot_id="", headline="Tema", slides=[Slide(heading="h", body="b")], caption="c", cta="x")
    pieces = [Piece(id=f"0{i}-{t}", type=t, date=date(2026, 12, i), pillar="Educativo", topic="Tema", angle="a",
                    content=content.model_copy(update={"slot_id": f"0{i}-{t}"}))
              for i, t in enumerate(["post", "carousel", "story", "reel_script"], start=1)]
    return MonthPlan(client="demo", month="2026-12", pieces=pieces)


def test_assign_photos_covers_designed_pieces_and_falls_back_on_bad_choices():
    class BadChoice(FakeProvider):
        def generate(self, prompt, schema, **kwargs):
            return PhotoChoices(choices=[PhotoChoice(slot_id="01-post", photo="no-existe.png")])

    photos = [{"file": f"{n}.png", "framing": "cara", "description": "d"} for n in ("a", "b")]
    plan = _plan()
    assign_photos(plan, photos, BadChoice())
    by_type = {p.type: p.photo for p in plan.pieces}
    assert by_type["reel_script"] == ""
    assert {by_type["post"], by_type["carousel"], by_type["story"]} <= {"a.png", "b.png"}

    plan = _plan()
    assign_photos(plan, photos, FakeProvider())
    assert [p.photo for p in plan.pieces[:3]] == ["a.png", "b.png", "a.png"]


def test_highlight_wraps_words_and_escapes_html():
    assert str(highlight("Tu plan para <mayo>", "PLAN")) == "Tu <em>plan</em> para &lt;mayo&gt;"
    assert str(highlight("Sin cambios", "otra")) == "Sin cambios"


@pytest.mark.parametrize("light", [True, False])
def test_style_templates_render_with_photo_and_highlight(light):
    env = environment(ROOT / "templates", "papel-pizarra")
    brand = _brand(fonts={"heading": "Antonio", "body": "Inter", "display": "Anton"})
    html = env.get_template("post.html.j2").render(
        width=1080, height=1350, colors=brand.colors, fonts=brand.fonts, handle="@marca", brand_name="Marca",
        logo=None, pillar="Educativo", headline="El error que frena", highlight_words="frena", cta="",
        sticker="", slide=Slide(heading="h", body="Cuerpo"), photo="data:image/png;base64,AAA", light=light)
    assert "<em>frena</em>" in html and 'class="photo"' in html
    assert ("feDiffuseLighting" in html) == light  # papel en lo claro, pizarra en lo oscuro
    assert "family=Anton&" in html  # la fuente de titulares se pide sin pesos (solo tiene 400)


def test_brands_without_style_keep_default_templates():
    env = environment(ROOT / "templates")
    assert "estilos" not in env.get_template("post.html.j2").filename


# --- Fotos generadas con IA ---

from social_agent.llm.base import NoQuotaError  # noqa: E402
from social_agent.llm.fake import _json_block  # noqa: E402
from social_agent.photos import describe_visuals, generate_photos, save_bank  # noqa: E402


def _bank(tmp_path):
    """Banco con un primer plano y una foto de medio cuerpo."""
    folder = tmp_path / "demo" / "fotos"
    folder.mkdir(parents=True)
    for name in ("cara.png", "medio.png"):
        (folder / name).write_bytes(_png())
    save_bank(tmp_path, "demo", {"seen": [], "photos": [
        {"id": "cara", "file": "cara.png", "usable": True, "framing": "cara", "description": "", "reason": "", "source": "", "date": ""},
        {"id": "medio", "file": "medio.png", "usable": True, "framing": "medio cuerpo", "description": "", "reason": "", "source": "", "date": ""},
    ]})
    return folder


def _visual_plan():
    plan = _plan()
    for p in plan.pieces:
        p.photo = "medio.png" if p.type != "reel_script" else ""
        p.content.visual = "Sonriendo con una laptop"
    return plan


class Recorder(FakeProvider):
    def __init__(self, usable=True):
        self.images, self.usable = [], usable

    def generate_image(self, prompt, references=(), aspect="3:4"):
        self.images.append((prompt, len(references)))
        return super().generate_image(prompt, references, aspect)

    def generate(self, prompt, schema, **kwargs):
        if schema is PhotoReviewBatch:
            return PhotoReviewBatch(reviews=[PhotoReview(id=i, usable=self.usable, reason="borroso")
                                             for i in _json_block(prompt, "photos")])
        return super().generate(prompt, schema, **kwargs)


def test_generate_photos_replaces_reference_with_new_image(tmp_path):
    folder = _bank(tmp_path)
    llm, plan = Recorder(), _visual_plan()
    result = generate_photos(plan, _brand(), tmp_path, llm, cutter=lambda data: _png())
    assert result == {"generadas": 3, "reutilizadas": 0, "originales": 0}
    assert [p.photo for p in plan.pieces[:3]] == [f"generadas/2026-12/{p.id}.png" for p in plan.pieces[:3]]
    assert (folder / plan.pieces[0].photo).exists() and plan.pieces[3].photo == ""
    prompt, refs = llm.images[0]
    assert "Sonriendo con una laptop" in prompt and refs == 2  # la foto elegida + el primer plano

    again = Recorder()
    plan2 = _visual_plan()
    assert generate_photos(plan2, _brand(), tmp_path, again, cutter=lambda d: _png())["reutilizadas"] == 3
    assert again.images == []  # ya existían: no se paga otra generación


def test_generate_photos_keeps_original_after_rejected_attempts(tmp_path):
    _bank(tmp_path)
    llm, plan = Recorder(usable=False), _visual_plan()
    result = generate_photos(plan, _brand(), tmp_path, llm, cutter=lambda data: _png())
    assert result["originales"] == 3 and len(llm.images) == 6  # 2 intentos por pieza
    assert plan.pieces[0].photo == "medio.png"


def test_generate_photos_stops_without_quota(tmp_path):
    _bank(tmp_path)

    class NoQuota(FakeProvider):
        def generate_image(self, *args, **kwargs):
            raise NoQuotaError("activa la facturación")

    with pytest.raises(NoQuotaError):
        generate_photos(_visual_plan(), _brand(), tmp_path, NoQuota(), cutter=lambda data: _png())


def test_describe_visuals_fills_only_missing():
    plan = _plan()
    plan.pieces[0].content.visual = "Ya tenía"
    assert describe_visuals(plan, _brand(), FakeProvider()) == 2  # carrusel e historia; el guion no lleva foto
    assert plan.pieces[0].content.visual == "Ya tenía" and plan.pieces[1].content.visual
    assert plan.pieces[3].content.visual == ""


def test_redesign_month_replaces_only_pending_rows(tmp_path, monkeypatch):
    from social_agent import pipeline

    _bank(tmp_path)
    settings = replace(load_settings(), clients_dir=tmp_path, out_dir=tmp_path / "out", llm_provider="fake")
    plan = _visual_plan()
    for p in plan.pieces:
        p.content.visual = ""  # parrilla escrita antes de que existiera el campo
    pipeline.save_plan(settings, plan)
    brand = _brand()
    monkeypatch.setattr(pipeline.Brand, "load", classmethod(lambda cls, d, slug: brand))
    monkeypatch.setattr(pipeline, "render_plan", lambda plan, *a: [setattr(p, "images", []) for p in plan.pieces])
    monkeypatch.setattr("social_agent.photos.Cutter", lambda: (lambda data: _png()))

    events = []

    class Board:
        def __init__(self, *a):
            pass

        def rows(self, prefix):
            events.append(("rows", prefix))
            return {f"demo/2026-12/{plan.pieces[0].id}": ("p0", "Pendiente"),
                    f"demo/2026-12/{plan.pieces[1].id}": ("p1", "Aprobado")}

        def archive(self, page_id):
            events.append(("archive", page_id))

        def add_piece(self, client, month, piece):
            events.append(("add", piece.id))

    class Postiz:
        def __init__(self, *a):
            pass

    monkeypatch.setattr(pipeline, "NotionBoard", Board)
    monkeypatch.setattr(pipeline, "PostizClient", Postiz)

    result = pipeline.redesign_month(settings, "demo", "2026-12", dry_run=False)
    assert result["visuales"] == 3 and result["generadas"] == 3
    assert result["sin_tocar"] == [f"{plan.pieces[1].id} (Aprobado)"]
    assert ("archive", "p0") in events and ("archive", "p1") not in events
    added = [e[1] for e in events if e[0] == "add"]
    assert plan.pieces[1].id not in added and len(added) == 3  # la pendiente y las que no tenían fila
    saved = pipeline.MonthPlan.model_validate_json(pipeline.plan_path(settings, "demo", "2026-12").read_text(encoding="utf-8"))
    assert saved.pieces[0].photo.startswith("generadas/")


def test_import_photos_adds_real_photos_untouched_and_skips_repeats(tmp_path):
    from social_agent.photos import PHOTO, import_photos, loose_images

    folder = tmp_path / "demo" / "fotos"
    folder.mkdir(parents=True)
    photo = folder / "victor.jpg"
    Image.new("RGB", (40, 60), (30, 90, 200)).save(photo, format="JPEG")
    cut = folder / "recortada.png"  # ya viene con fondo transparente: no pasa por el recorte
    canvas = Image.new("RGBA", (40, 60), (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", (20, 60), (200, 30, 30, 255)), (10, 0))
    canvas.save(cut)
    assert loose_images(tmp_path, "demo") == [cut, photo]

    cutouts = []
    result = import_photos(tmp_path, _brand(), FakeProvider(), [cut, photo],
                           cutter=lambda data: cutouts.append(data) or _png())
    assert len(cutouts) == 1 and result["nuevas"] == 2 and result["ya_estaban"] == 0

    bank = load_bank(tmp_path, "demo")
    assert all(p["kind"] == PHOTO and p["source"] == "local" for p in bank["photos"])
    assert {p["original"] for p in bank["photos"]} == {"victor.jpg", "recortada.png"}
    own = next(p for p in bank["photos"] if p["original"] == "recortada.png")
    saved = Image.open(folder / own["file"]).convert("RGBA")
    assert saved.size == (20, 60) and saved.getpixel((0, 0)) == (200, 30, 30, 255)  # mismos píxeles, sin voltear
    assert len(usable_photos(tmp_path, "demo")) == result["utiles"]
    assert loose_images(tmp_path, "demo") == [cut, photo]  # los originales no se borran...

    again = import_photos(tmp_path, _brand(), FakeProvider(), loose_images(tmp_path, "demo"),
                          cutter=lambda data: pytest.fail("ya importada"))
    assert again["nuevas"] == 0 and again["ya_estaban"] == 2  # ...pero no se importan dos veces
