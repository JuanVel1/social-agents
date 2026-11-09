import io
from dataclasses import replace

import pytest
from PIL import Image

from social_agent import character
from social_agent.config import ROOT, load_settings
from social_agent.llm.base import NoQuotaError
from social_agent.llm.fake import FakeProvider, _json_block
from social_agent.models import Brand, PhotoReview, PhotoReviewBatch
from social_agent.photos import CHARACTER, load_bank, save_bank, usable_photos


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (30, 40), (120, 160, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _brand():
    return Brand.load(ROOT / "clients", "demo")


class Editor:
    """Editor falso: cuenta las llamadas y se queda sin cuota tras `quota` imágenes."""

    def __init__(self, quota=999):
        self.calls, self.quota = [], quota

    def __call__(self, image, prompt, seed):
        if len(self.calls) >= self.quota:
            raise NoQuotaError("cuota agotada")
        self.calls.append((prompt, seed))
        return _png()


class Reviewer(FakeProvider):
    def __init__(self, reject=()):
        self.reject, self.subjects = set(reject), []

    def generate(self, prompt, schema, **kwargs):
        if schema is PhotoReviewBatch:
            self.subjects.append(prompt)
            return PhotoReviewBatch(reviews=[PhotoReview(id=i, usable=i not in self.reject, framing="medio cuerpo")
                                             for i in _json_block(prompt, "photos")])
        return super().generate(prompt, schema, **kwargs)


def _with_master(tmp_path):
    folder = character.character_dir(tmp_path, "demo")
    folder.mkdir(parents=True)
    (folder / "maestro.png").write_bytes(_png())
    save_bank(tmp_path, "demo", {"seen": [], "photos": [
        {"id": "real", "file": "real.png", "usable": True, "framing": "cara", "description": "", "reason": "",
         "source": "", "date": ""}]})
    (tmp_path / "demo" / "fotos" / "real.png").write_bytes(_png())


def test_make_and_choose_master(tmp_path):
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(_png())
    editor = Editor(quota=2)
    made = character.make_masters(tmp_path, _brand(), photo, editor, variants=3)
    assert len(made) == 2 and len(editor.calls) == 2  # la tercera variante se quedó sin cuota
    assert character.make_masters(tmp_path, _brand(), photo, Editor(quota=0), variants=2) == made  # ya existían
    with pytest.raises(NoQuotaError):  # sin ninguna variante y sin cuota: error
        character.make_masters(tmp_path / "otro", _brand(), photo, Editor(quota=0))
    seed = made[1].stem.removeprefix("maestro-")
    assert character.choose_master(tmp_path, "demo", seed).read_bytes() == made[1].read_bytes()
    with pytest.raises(FileNotFoundError):
        character.choose_master(tmp_path, "demo", 999)


def test_fill_poses_resumes_after_quota_and_keeps_kinds_apart(tmp_path):
    _with_master(tmp_path)
    total = len(character.POSES)
    llm = Reviewer(reject={f"{CHARACTER}-{character.POSES[0].id}"})

    first = character.fill_poses(tmp_path, _brand(), llm, Editor(quota=3), cutter=lambda d: d)
    assert first["sin_cuota"] and first["nuevas"] == 2 and first["descartadas"] == 1
    assert "personaje 3D" in llm.subjects[0]

    # La pose descartada se reintenta con otra semilla; las listas no se repiten.
    llm.reject.clear()
    editor = Editor()
    second = character.fill_poses(tmp_path, _brand(), llm, editor, cutter=lambda d: d)
    assert len(editor.calls) == total - 2 and second["faltan"] == 0 and second["listas"] == total
    assert editor.calls[0][1] == character.MASTER_SEEDS[0] + 1
    assert character.fill_poses(tmp_path, _brand(), llm, Editor(quota=0), cutter=lambda d: d)["nuevas"] == 0

    poses = usable_photos(tmp_path, "demo", CHARACTER)
    assert len(poses) == total and all(p["file"].startswith(f"{CHARACTER}/") for p in poses)
    assert [p["id"] for p in usable_photos(tmp_path, "demo")] == ["real"]  # las fotos reales, aparte


def test_pose_gives_up_after_attempts(tmp_path):
    _with_master(tmp_path)
    first = character.POSES[0]
    llm = Reviewer(reject={f"{CHARACTER}-{first.id}"})
    for _ in range(3):
        character.fill_poses(tmp_path, _brand(), llm, Editor(), cutter=lambda d: d, limit=1)
    entry = next(p for p in load_bank(tmp_path, "demo")["photos"] if p["id"] == f"{CHARACTER}-{first.id}")
    assert entry["attempts"] == character.POSE_ATTEMPTS and not entry["usable"]
    assert first not in character.pending_poses(load_bank(tmp_path, "demo"))


def test_fill_poses_needs_master(tmp_path):
    with pytest.raises(FileNotFoundError, match="personaje maestro"):
        character.fill_poses(tmp_path, _brand(), Reviewer(), Editor())


def test_run_month_uses_character_poses(tmp_path, monkeypatch):
    from social_agent import pipeline

    _with_master(tmp_path)
    character.fill_poses(tmp_path, _brand(), Reviewer(), Editor(quota=4), cutter=lambda d: d)
    brand = _brand().model_copy(update={"style": _brand().style.model_copy(
        update={"photos": True, "character": True, "generate_photos": True})})
    monkeypatch.setattr(pipeline.Brand, "load", classmethod(lambda cls, d, slug: brand))
    monkeypatch.setattr(pipeline, "render_plan", lambda plan, *a: None)
    monkeypatch.setattr(pipeline, "generate_photos", lambda *a, **k: pytest.fail("no debe generar fotos"))
    settings = replace(load_settings(), clients_dir=tmp_path, out_dir=tmp_path / "out", llm_provider="fake")
    plan = pipeline.run_month(settings, "demo", "2026-12", dry_run=True)
    photos = {p.photo for p in plan.pieces if p.photo}
    assert photos and all(p.startswith(f"{CHARACTER}/") for p in photos)


def test_character_pass_makes_variants_then_poses(tmp_path, monkeypatch):
    from social_agent import jobs

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "brand.yaml").write_text("x", encoding="utf-8")
    (tmp_path / "vic").mkdir()
    (tmp_path / "vic" / "brand.yaml").write_text("x", encoding="utf-8")
    (tmp_path / "vic" / "fotos").mkdir()
    (tmp_path / "vic" / "fotos" / "ref.jpg").write_bytes(_png())
    brand = _brand().model_copy(update={"slug": "vic", "style": _brand().style.model_copy(
        update={"photos": True, "character": True, "character_photo": "ref.jpg"})})
    monkeypatch.setattr("social_agent.models.Brand.load", classmethod(lambda cls, d, slug: brand))
    editor = Editor(quota=2)
    monkeypatch.setattr(character, "QwenEditor", lambda *a: editor)
    monkeypatch.setattr("social_agent.llm.get_provider", lambda s: Reviewer())
    monkeypatch.setattr(character, "Cutter", lambda *a: (lambda d: d))
    settings = replace(load_settings(), clients_dir=tmp_path, llm_provider="fake")

    jobs.character_pass(settings)  # sin cuota a la tercera variante: no falla
    assert len(list(character.character_dir(tmp_path, "vic").glob("maestro-*.png"))) == 2
    jobs.character_pass(settings)  # aún sin maestro elegido ni cuota: espera
    character.choose_master(tmp_path, "vic", character.MASTER_SEEDS[0])
    editor.quota = 5
    jobs.character_pass(settings)
    assert len(usable_photos(tmp_path, "vic", CHARACTER)) == 3


def test_cli_personaje_without_master(tmp_path, monkeypatch, capsys):
    from social_agent import cli

    settings = replace(load_settings(), clients_dir=ROOT / "clients", huggingface_token="hf_x")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(character, "master_path", lambda d, slug: tmp_path / "no-existe.png")
    assert cli.main(["personaje", "poses", "--client", "demo"]) == 1
    assert "personaje maestro" in capsys.readouterr().out
