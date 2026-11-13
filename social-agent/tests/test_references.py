import io
import json
import shutil

from PIL import Image

from social_agent.config import ROOT
from social_agent.llm.fake import FakeProvider
from social_agent.models import Brand, FieldValue, RefField, ReferenceDesign, ReferenceReview
from social_agent.planner import plan_month
from social_agent.references import (
    build_reference,
    check_template,
    field_values,
    load_references,
    references_dir,
    refresh_references,
)
from social_agent.render import environment


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 20, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _brand(tmp_path):
    shutil.copytree(ROOT / "clients" / "demo", tmp_path / "demo")
    return Brand.load(tmp_path, "demo")


def _design(**update):
    return FakeProvider().generate("", ReferenceDesign).model_copy(update=update)


def test_check_template_rejects_scripts_urls_and_undeclared_fields():
    assert check_template(_design()) == []
    bad = _design(template=_design().template + '<script>x</script><img src="https://x.y/a.png">{{ f.otro }}')
    problems = " ".join(check_template(bad))
    assert "prohibido" in problems and "otro" in problems
    assert "extends" in " ".join(check_template(_design(template="<div>{{ f.contacto }}</div>")))
    assert "photo" in " ".join(check_template(_design(template=_design().template.replace("{{ photo }}", ""))))


def test_field_values_fill_every_field_and_cut_long_text():
    design = _design(fields=[RefField(name="a", description="", max_chars=12), RefField(name="b", description="", max_chars=5)])
    assert field_values(design, [FieldValue(name="a", value="Un texto bastante largo")]) == {"a": "Un texto…", "b": ""}


class Renderer:
    """Renderer falso: el primer intento se desborda, el segundo sale bien."""

    def __init__(self, fail_first=True):
        self.calls, self.fail_first = [], fail_first

    def __call__(self, template, design, ctx, photo):
        self.calls.append((template, ctx, photo))
        overflow = ['texto cortado: "x"'] if self.fail_first and len(self.calls) == 1 else []
        return _jpeg(), overflow


class Designer(FakeProvider):
    def __init__(self, approve=True):
        self.prompts, self.approve = [], approve

    def generate(self, prompt, schema, **kwargs):
        if schema is ReferenceDesign:
            self.prompts.append(prompt)
        if schema is ReferenceReview:
            return ReferenceReview(ok=self.approve, issues="" if self.approve else "texto encimado")
        return super().generate(prompt, schema, **kwargs)


def test_build_reference_retries_with_feedback_and_saves(tmp_path):
    brand = _brand(tmp_path)
    folder = references_dir(tmp_path, "demo") / "123"
    llm, renderer = Designer(), Renderer()
    record = build_reference(folder, brand, _jpeg(), llm, renderer, title="Santa", url="https://pin/123",
                             poses=[{"file": "poses/x.png", "description": "Con laptop"}])
    assert record["ok"] and record["attempts"] == 2
    assert "[post] texto cortado" in llm.prompts[1] and "poses/x.png" in llm.prompts[0]  # el segundo intento recibe los problemas del primero
    template, ctx, photo = renderer.calls[0]
    assert template == "123/template.html.j2" and photo == "poses/x.png"
    assert ctx["kind"] == "post" and ctx["f"] == {"contacto": "Vic", "mensaje_1": "¡Hola!"}
    assert [c[1]["kind"] for c in renderer.calls[-3:]] == ["post", "story", "slide"]  # los tres formatos
    for name in ("preview.png", "preview-story.png", "preview-slide.png", "reference.jpg"):
        assert (folder / name).exists()
    [ref] = load_references(tmp_path, "demo")
    assert ref["id"] == "123" and ref["url"] == "https://pin/123" and ref["design"].season == "navidad"


def test_rejected_references_are_not_loaded_and_existing_ones_are_skipped(tmp_path):
    brand = _brand(tmp_path)
    items = [{"id": "bad", "image": _jpeg()}]
    assert refresh_references(tmp_path, brand, Designer(approve=False), items, Renderer(False))["descartadas"] == 1
    assert load_references(tmp_path, "demo") == []
    saved = json.loads((references_dir(tmp_path, "demo") / "bad" / "design.json").read_text(encoding="utf-8"))
    assert not saved["ok"] and "encimado" in saved["issues"]
    assert refresh_references(tmp_path, brand, Designer(), items, Renderer(False))["existentes"] == 1


def test_plan_assigns_references_to_posts_and_writes_their_fields(tmp_path):
    brand = _brand(tmp_path)
    build_reference(references_dir(tmp_path, "demo") / "123", brand, _jpeg(), Designer(), Renderer(False),
                    url="https://pin/123")
    build_reference(references_dir(tmp_path, "demo") / "456", brand, _jpeg(), Designer(), Renderer(False))
    # La segunda referencia no es de temporada: el reparto de respaldo la prefiere.
    path = references_dir(tmp_path, "demo") / "456" / "design.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["design"]["season"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    plan = plan_month(brand, "2026-12", FakeProvider(), load_references(tmp_path, "demo"))
    designed = [p for p in plan.pieces if p.type in ("post", "carousel", "story")]
    assert designed and all(p.reference for p in designed)  # todas las piezas con diseño llevan referencia
    assert all(not p.reference for p in plan.pieces if p.type == "reel_script")
    first = min(designed, key=lambda p: (p.date, p.id))
    assert first.reference == "123" and first.reference_url == "https://pin/123" and first.topic.endswith("(adaptado)")
    others = [p.reference for p in designed if p is not first]
    assert set(others) == {"456"}  # las demás: la que no es de temporada
    assert all({f.name for f in p.content.fields} == {"contacto", "mensaje_1"} for p in designed)


def test_reference_template_renders_inside_the_style_base(tmp_path):
    brand = Brand.load(ROOT / "clients", "vic-mercadeo") if (ROOT / "clients" / "vic-mercadeo").exists() else _brand(tmp_path)
    folder = references_dir(tmp_path, "demo") / "123"
    folder.mkdir(parents=True)
    (folder / "template.html.j2").write_text(_design().template, encoding="utf-8")
    env = environment(ROOT / "templates", "papel-pizarra", (references_dir(tmp_path, "demo"),))
    ctx = dict(width=1080, height=1350, photo=None, f={"contacto": "Vic <3", "mensaje_1": "Hola"}, extra_font="",
               colors=brand.colors, fonts={**brand.fonts, "display": "Anton", "hand": "Gochi Hand"},
               handle="@vic", brand_name="Vic", logo=None, slide={"heading": "Lámina", "body": "Texto"},
               index=2, total=6)
    html = env.get_template("123/template.html.j2").render(kind="post", **ctx)
    assert "Vic &lt;3" in html and "--primary:" in html and "tiza" not in html  # sin el fondo del estilo
    assert "Lámina" in env.get_template("123/template.html.j2").render(kind="slide", **ctx)


def test_pose_must_come_from_the_catalog_and_is_used_in_the_plan(tmp_path):
    from social_agent.models import Piece
    from social_agent.references import apply_reference_photos

    brand = _brand(tmp_path)
    folder = references_dir(tmp_path, "demo") / "9"
    class Inventor(Designer):
        def generate(self, prompt, schema, **kwargs):
            result = super().generate(prompt, schema, **kwargs)
            return result.model_copy(update={"photo_file": "inventada.png"}) if schema is ReferenceDesign else result

    record = build_reference(folder, brand, _jpeg(), Inventor(), Renderer(False), poses=[{"file": "poses/a.png"}])
    assert not record["ok"] and "photo_file" in record["issues"]  # una pose que no está en el banco

    renderer = Renderer(False)
    assert build_reference(folder, brand, _jpeg(), Designer(), renderer, poses=[{"file": "poses/a.png"}])["ok"]
    assert renderer.calls[-1][2] == "poses/a.png"
    bank = tmp_path / "demo" / "fotos"
    (bank / "poses").mkdir(parents=True)
    (bank / "poses" / "a.png").write_bytes(_jpeg())
    pieces = [Piece(id="1", type="post", date="2026-12-01", pillar="p", topic="t", angle="a", reference="9", photo="otra.png")]
    assert apply_reference_photos(pieces, load_references(tmp_path, "demo"), bank) == 1
    assert pieces[0].photo == "poses/a.png"


def test_retry_redoes_only_rejected_references(tmp_path):
    brand = _brand(tmp_path)
    refresh_references(tmp_path, brand, Designer(approve=False), [{"id": "bad", "image": _jpeg()}], Renderer(False))
    refresh_references(tmp_path, brand, Designer(), [{"id": "good", "image": _jpeg()}], Renderer(False))
    items = [{"id": "bad", "image": _jpeg()}, {"id": "good", "image": _jpeg()}]
    counts = refresh_references(tmp_path, brand, Designer(), items, Renderer(False), retry=True)
    assert counts == {"nuevas": 1, "descartadas": 0, "existentes": 1}
    assert {r["id"] for r in load_references(tmp_path, "demo")} == {"bad", "good"}


def test_missing_template_fields_are_requested_again(tmp_path):
    from social_agent.models import Piece, PieceContent
    from social_agent.planner import fill_missing_fields

    brand = _brand(tmp_path)
    build_reference(references_dir(tmp_path, "demo") / "1", brand, _jpeg(), Designer(), Renderer(False))
    refs = {r["id"]: r for r in load_references(tmp_path, "demo")}
    piece = Piece(id="p", type="post", date="2026-12-01", pillar="x", topic="t", angle="a", reference="1",
                  content=PieceContent(slot_id="p", headline="H", slides=[], caption="C", cta="",
                                       fields=[FieldValue(name="contacto", value="Vic"), FieldValue(name="mensaje_1", value=" ")]))
    assert fill_missing_fields(brand, [piece], refs, FakeProvider()) == 1
    assert {f.name: f.value for f in piece.content.fields} == {"contacto": "Vic", "mensaje_1": "Relleno"}


def test_reference_pieces_keep_their_pose_when_the_framing_matches(tmp_path):
    from social_agent.models import Piece
    from social_agent.references import apply_reference_photos

    brand = _brand(tmp_path)
    bank = tmp_path / "demo" / "fotos"
    (bank / "poses").mkdir(parents=True)
    for name in ("a", "b", "c"):
        (bank / "poses" / f"{name}.png").write_bytes(_jpeg())
    poses = [{"file": "poses/a.png", "framing": "medio cuerpo"}, {"file": "poses/b.png", "framing": "medio cuerpo"},
             {"file": "poses/c.png", "framing": "cuerpo entero"}]
    build_reference(references_dir(tmp_path, "demo") / "9", brand, _jpeg(), Designer(), Renderer(False), poses=poses)
    piece = lambda photo: Piece(id=photo or "x", type="story", date="2026-12-01", pillar="p", topic="t", angle="a",
                                reference="9", photo=photo)
    pieces = [piece("poses/b.png"), piece("poses/c.png"), piece("")]
    assert apply_reference_photos(pieces, load_references(tmp_path, "demo"), bank, poses) == 2
    assert [p.photo for p in pieces] == ["poses/b.png", "poses/a.png", "poses/a.png"]


def test_tidy_removes_imports_and_keeps_the_font_family():
    from social_agent.references import tidy

    design = _design(extra_font="Outfit:wght@600;700", template=_design().template.replace(
        "{% block style %}", "{% block style %}@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700');"))
    fixed = tidy(design)
    assert "@import" not in fixed.template and fixed.extra_font == "Outfit" and check_template(fixed) == []
    assert tidy(_design(template=design.template)).extra_font == "Playfair Display"


def test_tidy_fixes_the_base_and_slides_are_optional():
    from social_agent.references import kinds_for, tidy

    wrong = _design(template=_design().template.replace("referencia_base.html.j2", "base.html.j2"))
    assert check_template(tidy(wrong)) == []
    missing = _design(template=_design().template.split("%}", 1)[1])
    assert tidy(missing).template.startswith('{% extends "referencia_base.html.j2" %}')
    no_slide = _design(template=_design().template.replace("slide.heading", "f.contacto").replace("slide.body", "f.mensaje_1"))
    assert check_template(no_slide) == [] and kinds_for(no_slide) == ["post", "story"]


class Spotter(Designer):
    """Además de diseñar, dice si el pin trae la fotografía de una persona y qué foto real elige."""

    def __init__(self, person=True, photo_file="real-medio.png"):
        super().__init__()
        self.person, self.photo_file, self.looked = person, photo_file, 0

    def generate(self, prompt, schema, **kwargs):
        from social_agent.models import PinSubject

        if schema is PinSubject:
            self.looked += 1
            return PinSubject(realistic_person=self.person, pose="De medio cuerpo, sonriendo",
                              photo_file=self.photo_file if self.person else "")
        if schema is ReferenceDesign:
            self.prompts.append(prompt)
            files = [p["file"] for p in json.loads(prompt.split("<poses>")[1].split("</poses>")[0])]
            return _design(photo=True, photo_file=files[0] if files else "")
        return super().generate(prompt, schema, **kwargs)


REAL = [{"file": "real-cara.png", "framing": "cara", "description": "Primer plano"},
        {"file": "real-medio.png", "framing": "medio cuerpo", "description": "Sonriendo"}]
POSES = [{"file": "poses/saludo.png", "framing": "medio cuerpo", "description": "Saluda"}]


def _real_brand(tmp_path, enabled=True):
    brand = _brand(tmp_path)
    return brand.model_copy(update={"style": brand.style.model_copy(update={"real_photo_references": enabled})})


def _saved(tmp_path, ref):
    return json.loads((references_dir(tmp_path, "demo") / ref / "design.json").read_text(encoding="utf-8"))


def test_pin_with_a_real_person_gets_a_real_photo(tmp_path):
    llm = Spotter()
    items = [{"id": "p1", "image": _jpeg(), "title": "Retrato"}]
    refresh_references(tmp_path, _real_brand(tmp_path), llm, items, Renderer(False), poses=POSES,
                       subject="la persona", real_photos=REAL)
    saved = _saved(tmp_path, "p1")
    assert saved["ok"] and saved["real_photo"] is True
    assert saved["design"]["photo_file"] == "real-medio.png"
    assert "poses/saludo.png" not in llm.prompts[0] and "fotografía real" in llm.prompts[0]


def test_pins_without_a_real_person_choose_from_the_catalog(tmp_path):
    llm = Spotter(person=False)
    items = [{"id": "p1", "image": _jpeg()}]
    refresh_references(tmp_path, _real_brand(tmp_path), llm, items, Renderer(False), poses=POSES,
                       subject="la persona", real_photos=REAL)
    saved = _saved(tmp_path, "p1")
    assert llm.looked == 1 and saved["real_photo"] is False
    assert saved["design"]["photo_file"] == "poses/saludo.png"


def test_real_photo_step_is_optional_and_needs_photos(tmp_path):
    items = [{"id": "p1", "image": _jpeg()}]
    off = Spotter()
    refresh_references(tmp_path, _real_brand(tmp_path, enabled=False), off, items, Renderer(False), poses=POSES,
                       real_photos=REAL)
    assert off.looked == 0 and _saved(tmp_path, "p1")["design"]["photo_file"] == "poses/saludo.png"

    empty = Spotter()
    refresh_references(tmp_path, _real_brand(tmp_path / "otro"), empty, items, Renderer(False), poses=POSES,
                       real_photos=[], force=True)
    assert empty.looked == 0


def test_unknown_real_photo_falls_back_to_the_same_framing():
    from social_agent.references import detect_real_subject

    assert detect_real_subject(Spotter(photo_file="no-existe.png"), _jpeg(), REAL)["file"] == "real-medio.png"
    assert detect_real_subject(Spotter(person=False), _jpeg(), REAL) is None
    assert detect_real_subject(Spotter(), _jpeg(), []) is None


def test_existing_references_are_not_looked_at_again(tmp_path):
    brand = _real_brand(tmp_path)
    items = [{"id": "p1", "image": _jpeg()}]
    refresh_references(tmp_path, brand, Spotter(person=False), items, Renderer(False), poses=POSES, real_photos=REAL)
    later = Spotter()
    assert refresh_references(tmp_path, brand, later, items, Renderer(False), poses=POSES,
                              real_photos=REAL)["existentes"] == 1
    assert later.looked == 0 and _saved(tmp_path, "p1")["real_photo"] is False


def test_pieces_of_a_real_photo_reference_keep_the_real_photo(tmp_path):
    from types import SimpleNamespace

    from social_agent.references import apply_reference_photos

    (tmp_path / "real-medio.png").write_bytes(_jpeg())
    design = _design(photo=True, photo_file="real-medio.png")
    piece = SimpleNamespace(reference="p1", photo="poses/saludo.png")
    changed = apply_reference_photos([piece], [{"id": "p1", "design": design}], tmp_path, POSES)
    assert changed == 1 and piece.photo == "real-medio.png"
