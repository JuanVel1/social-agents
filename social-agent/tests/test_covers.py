import io
import json
import shutil
from datetime import date

from PIL import Image

from social_agent.config import ROOT
from social_agent.covers import fill_prompt, generate_covers, identity_photos, write_image_prompt
from social_agent.llm.base import NoQuotaError
from social_agent.llm.fake import FakeProvider
from social_agent.models import (
    Brand,
    FieldValue,
    ImagePrompt,
    MonthPlan,
    Piece,
    PieceContent,
    RefField,
    ReferenceDesign,
    ReferenceReview,
)
from social_agent.photos import save_bank
from social_agent.references import load_references, references_dir, refresh_references


def _image(size=(60, 80), color=(200, 20, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _brand(tmp_path):
    shutil.copytree(ROOT / "clients" / "demo", tmp_path / "demo")
    brand = Brand.load(tmp_path, "demo")
    style = brand.style.model_copy(update={"ai_images": True, "person": "short dark beard"})
    return brand.model_copy(update={"style": style})


def _design(**update):
    base = ReferenceDesign(
        fields=[RefField(name="titulo", description="Titular", max_chars=30),
                RefField(name="nota", description="Nota adhesiva", max_chars=20)],
        image_prompt='Cover of the man. Big type reads "[[titulo]]", a sticky note reads "[[nota]]".')
    return base.model_copy(update=update)


def _piece(piece_id="1103-post", kind="post", fields=(("titulo", "MARKETING DIGITAL"), ("nota", "Cursos SEO"))):
    content = PieceContent(slot_id=piece_id, headline="Titular", caption="Texto", cta="Escríbenos", slides=[],
                           fields=[FieldValue(name=n, value=v) for n, v in fields])
    return Piece(id=piece_id, type=kind, date=date(2026, 11, 3), pillar="Educativo", topic="t", angle="a",
                 content=content, reference="pin1")


def _setup(tmp_path):
    """Marca con una foto real en el banco y la imagen del pin guardada."""
    brand = _brand(tmp_path)
    bank = tmp_path / "demo" / "fotos"
    bank.mkdir(parents=True)
    (bank / "real.png").write_bytes(_image())
    save_bank(tmp_path, "demo", {"seen": [], "photos": [
        {"id": "real", "file": "real.png", "kind": "foto", "usable": True, "framing": "medio cuerpo",
         "description": "", "reason": "", "source": "local", "date": ""}]})
    pin = references_dir(tmp_path, "demo") / "pin1"
    pin.mkdir(parents=True)
    (pin / "reference.jpg").write_bytes(_image())
    return brand


class Painter(FakeProvider):
    """Genera una imagen lisa y aprueba (o no) la revisión de textos."""

    def __init__(self, approve=True, quota=True):
        self.prompts, self.aspects, self.references = [], [], []
        self.approve, self.quota = approve, quota

    def generate(self, prompt, schema, **kwargs):
        if schema is ReferenceReview:
            return ReferenceReview(ok=self.approve, issues="" if self.approve else "texto mal escrito")
        return super().generate(prompt, schema, **kwargs)

    def generate_image(self, prompt, references=(), aspect="3:4"):
        if not self.quota:
            raise NoQuotaError("sin cuota")
        self.prompts.append(prompt)
        self.aspects.append(aspect)
        self.references.append(len(references))
        return _image((400, 400), (10, 120, 200))


def test_fill_prompt_puts_the_piece_texts_and_needs_all_of_them():
    prompt = fill_prompt(_design(), _piece(fields=(("titulo", 'El "mejor" plan'), ("nota", "Cursos SEO"))))
    assert 'reads "El \'mejor\' plan"' in prompt and '"Cursos SEO"' in prompt and "[[" not in prompt
    assert fill_prompt(_design(), _piece(fields=(("titulo", "Solo el título"),))) is None


def test_write_image_prompt_rejects_unknown_placeholders(tmp_path):
    class Writer(FakeProvider):
        def __init__(self, answers):
            self.answers, self.seen = list(answers), []

        def generate(self, prompt, schema, **kwargs):
            assert schema is ImagePrompt
            self.seen.append(prompt)
            return ImagePrompt(prompt=self.answers.pop(0))

    brand = _brand(tmp_path)
    good = 'Cover. Type reads "[[titulo]]" and a note reads "[[nota]]".'
    llm = Writer(['Cover reads "[[otro]]".', good])
    assert write_image_prompt(llm, brand, _image(), _design(image_prompt="")) == good
    assert "otro" in llm.seen[1] and "short dark beard" in llm.seen[0]
    assert write_image_prompt(Writer(["sin huecos", "tampoco"]), brand, _image(), _design(image_prompt="")) == ""


def test_identity_photos_put_the_chosen_one_first_then_faces():
    photos = [{"file": "c.png", "framing": "cuerpo entero"}, {"file": "a.png", "framing": "cara"},
              {"file": "m.png", "framing": "medio cuerpo"}, {"file": "z.png", "framing": "cara"}]
    assert [p["file"] for p in identity_photos(photos, "c.png")] == ["c.png", "a.png", "z.png"]


def test_generate_covers_saves_full_images_and_reuses_them(tmp_path):
    brand = _setup(tmp_path)
    plan = MonthPlan(client="demo", month="2026-11", pieces=[_piece(), _piece("1104-story", "story")])
    refs = [{"id": "pin1", "design": _design()}]
    llm = Painter()
    assert generate_covers(plan, brand, tmp_path, refs, llm)["generadas"] == 2
    post, story = plan.pieces
    assert Image.open(post.generated).size == (1080, 1350) and Image.open(story.generated).size == (1080, 1920)
    assert llm.aspects == ["4:5", "9:16"] and llm.references == [2, 2]  # el pin + la foto real
    assert '"MARKETING DIGITAL"' in llm.prompts[0] and "identity reference photos" in llm.prompts[0]

    again = Painter(quota=False)  # mismos textos: no se vuelve a generar nada
    plan2 = MonthPlan(client="demo", month="2026-11", pieces=[_piece(), _piece("1104-story", "story")])
    assert generate_covers(plan2, brand, tmp_path, refs, again)["reutilizadas"] == 2
    assert plan2.pieces[0].generated == post.generated


def test_rejected_or_quota_less_covers_fall_back_to_the_template(tmp_path):
    brand = _setup(tmp_path)
    refs = [{"id": "pin1", "design": _design()}]
    plan = MonthPlan(client="demo", month="2026-11", pieces=[_piece()])
    bad = Painter(approve=False)
    assert generate_covers(plan, brand, tmp_path, refs, bad)["con_plantilla"] == 1
    assert plan.pieces[0].generated == "" and len(bad.prompts) == 2 and "texto mal escrito" in bad.prompts[1]

    plan = MonthPlan(client="demo", month="2026-11", pieces=[_piece(), _piece("1105-post")])
    assert generate_covers(plan, brand, tmp_path, refs, Painter(quota=False))["con_plantilla"] == 2
    assert not any(p.generated for p in plan.pieces)

    plain = [{"id": "pin1", "design": _design(image_prompt="")}]  # referencia sin prompt: nada que generar
    assert generate_covers(plan, brand, tmp_path, plain, Painter())["generadas"] == 0


def test_references_with_a_real_person_get_an_image_prompt(tmp_path):
    from test_references import POSES, REAL, Renderer, Spotter

    brand = _brand(tmp_path)
    items = [{"id": "p1", "image": _image(), "title": "Portada"}]
    counts = refresh_references(tmp_path, brand, Spotter(), items, Renderer(False), poses=POSES, real_photos=REAL)
    saved = json.loads((references_dir(tmp_path, "demo") / "p1" / "design.json").read_text(encoding="utf-8"))
    assert counts["nuevas"] == 1 and "[[" in saved["design"]["image_prompt"]
    [ref] = load_references(tmp_path, "demo")
    assert ref["template_ok"] and ref["design"].image_prompt

    # Sin persona real en el pin, la referencia no lleva prompt.
    refresh_references(tmp_path, brand, Spotter(person=False), [{"id": "p2", "image": _image()}], Renderer(False),
                       poses=POSES, real_photos=REAL)
    other = json.loads((references_dir(tmp_path, "demo") / "p2" / "design.json").read_text(encoding="utf-8"))
    assert other["design"]["image_prompt"] == ""
