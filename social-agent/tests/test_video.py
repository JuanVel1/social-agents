import shutil
import subprocess
from pathlib import Path

import pytest

from social_agent.config import ROOT, load_settings
from social_agent.models import Brand
from social_agent.video import (
    Cue, Probe, Word, check_raw, check_reel, edit_reel, group_words, make_reel, probe, reel_command,
    style_from_brand, write_srt,
)

FIXTURES = Path(__file__).parent / "fixtures"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg no está instalado")


def test_group_words_cuts_on_size_pause_and_period():
    words = [Word(0.0, 0.3, "Hola"), Word(0.3, 0.6, "emprendedor."), Word(0.7, 1.0, "Hoy"),
             Word(1.0, 1.2, "te"), Word(1.2, 1.5, "cuento"), Word(1.5, 1.8, "tres"),
             Word(1.8, 2.1, "errores"), Word(3.5, 3.9, "comunes")]
    assert [c.text for c in group_words(words)] == [
        "Hola emprendedor.", "Hoy te cuento tres", "errores", "comunes"]


def test_write_srt_format(tmp_path):
    path = write_srt([Cue(0.0, 1.25, "Hola"), Cue(61.5, 3723.004, "chao")], tmp_path / "s.srt")
    assert path.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,250\nHola\n\n"
        "2\n00:01:01,500 --> 01:02:03,004\nchao\n")


def test_style_uses_brand_colors_in_ass_order():
    brand = Brand.load(ROOT / "clients", "vic-mercadeo")  # primary #16a4c8
    style = style_from_brand(brand)
    assert "OutlineColour=&H00C8A416" in style
    assert "FontName=Antonio" in style


def test_reel_command_meets_meta_specs():
    cmd = " ".join(reel_command(Path("raw.mp4"), "subs.srt", "Bold=1", Path("reel.mp4")))
    for part in ("crop=1080:1920", "subtitles=subs.srt", "libx264", "yuv420p", "-c:a aac", "+faststart"):
        assert part in cmd


def test_checks_reject_bad_clips():
    good = Probe(30.0, 1080, 1920, "h264", "aac", "yuv420p", 12.0)
    assert check_reel(good) == []
    silent_long = Probe(120.0, 1920, 1080, "hevc", None, "yuv420p10le", 300.0)
    assert len(check_raw(silent_long)) == 2
    assert len(check_reel(silent_long)) == 5


def _synthetic_clip(path: Path, seconds: int = 4) -> Path:
    """Video horizontal 1920x1080 con tono, como un clip grabado sin voz."""
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@needs_ffmpeg
def test_make_reel_outputs_vertical_h264_with_subtitles(tmp_path):
    clip = _synthetic_clip(tmp_path / "raw.mp4")
    srt = write_srt([Cue(0.2, 2.0, "Prueba de subtítulos"), Cue(2.0, 3.8, "con la marca")], tmp_path / "raw.srt")
    out = make_reel(clip, srt, Brand.load(ROOT / "clients", "vic-mercadeo"), tmp_path / "reel.mp4")
    info = probe(out)
    assert check_reel(info) == []
    assert abs(info.duration - 4) < 0.5


@needs_ffmpeg
@pytest.mark.slow
@pytest.mark.skipif(not (FIXTURES / "voz.mp4").exists(), reason="falta tests/fixtures/voz.mp4 (clip de voz en español)")
def test_edit_reel_end_to_end_with_whisper(tmp_path):
    out = edit_reel(FIXTURES / "voz.mp4", Brand.load(ROOT / "clients", "vic-mercadeo"),
                    load_settings(), tmp_path / "reel.mp4")
    srt = out.with_suffix(".srt").read_text(encoding="utf-8")
    assert "-->" in srt
    assert check_reel(probe(out)) == []
