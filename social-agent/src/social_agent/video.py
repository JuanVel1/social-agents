"""Edición de Reels sin CapCut: subtítulos con faster-whisper y montaje vertical con FFmpeg."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import Brand

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30
# Límites que aplicamos antes de subir a Meta (Reels por API: video con audio, 3-90 s seguros).
MIN_SECONDS, MAX_SECONDS, MAX_MB = 3, 90, 100


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    pix_fmt: str
    size_mb: float


def probe(path: Path) -> Probe:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return Probe(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        video_codec=video["codec_name"],
        audio_codec=audio["codec_name"] if audio else None,
        pix_fmt=video.get("pix_fmt", ""),
        size_mb=int(data["format"]["size"]) / 1_000_000,
    )


def check_raw(info: Probe) -> list[str]:
    """Problemas del clip grabado que impiden hacer un Reel publicable."""
    problems = []
    if info.audio_codec is None:
        problems.append("el clip no tiene audio")
    if not MIN_SECONDS <= info.duration <= MAX_SECONDS:
        problems.append(f"dura {info.duration:.1f} s (debe estar entre {MIN_SECONDS} y {MAX_SECONDS})")
    return problems


def check_reel(info: Probe) -> list[str]:
    """Lo que Meta exige al Reel final; lista vacía si está listo para subir."""
    problems = check_raw(info)
    if (info.width, info.height) != (WIDTH, HEIGHT):
        problems.append(f"mide {info.width}x{info.height} (debe ser {WIDTH}x{HEIGHT})")
    if info.video_codec != "h264" or info.pix_fmt != "yuv420p":
        problems.append(f"video {info.video_codec}/{info.pix_fmt} (debe ser h264/yuv420p)")
    if info.audio_codec not in (None, "aac"):
        problems.append(f"audio {info.audio_codec} (debe ser aac)")
    if info.size_mb > MAX_MB:
        problems.append(f"pesa {info.size_mb:.0f} MB (máximo {MAX_MB})")
    return problems


def transcribe(clip: Path, settings: Settings) -> list[Word]:
    """Palabras con sus tiempos. El modelo se descarga la primera vez a la caché de Hugging Face."""
    from faster_whisper import WhisperModel

    model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                         compute_type=settings.whisper_compute_type)
    segments, _ = model.transcribe(str(clip), language="es", word_timestamps=True, vad_filter=True)
    return [Word(w.start, w.end, w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]


def group_words(words: list[Word], max_words: int = 4, max_gap: float = 0.6) -> list[Cue]:
    """Frases cortas para pantalla vertical: corta cada max_words, en pausas o tras punto."""
    cues: list[Cue] = []
    chunk: list[Word] = []
    for word in words:
        if chunk and (len(chunk) == max_words or word.start - chunk[-1].end > max_gap
                      or chunk[-1].text[-1] in ".?!"):
            cues.append(Cue(chunk[0].start, chunk[-1].end, " ".join(w.text for w in chunk)))
            chunk = []
        chunk.append(word)
    if chunk:
        cues.append(Cue(chunk[0].start, chunk[-1].end, " ".join(w.text for w in chunk)))
    return cues


def _timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    blocks = [f"{i}\n{_timestamp(c.start)} --> {_timestamp(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _ass_color(hex_color: str) -> str:
    """#RRGGBB → &H00BBGGRR (formato de color de ASS/libass)."""
    r, g, b = hex_color.lstrip("#")[0:2], hex_color.lstrip("#")[2:4], hex_color.lstrip("#")[4:6]
    return f"&H00{b}{g}{r}".upper()


def style_from_brand(brand: Brand) -> str:
    """Texto blanco en negrita sobre una caja del color primario de la marca.
    FontSize y MarginV van en la escala de libass para SRT (288 px de alto)."""
    box = _ass_color(brand.colors.get("primary", "#000000"))
    font = brand.fonts.get("heading", "Arial")
    return (f"FontName={font},FontSize=11,Bold=1,PrimaryColour=&H00FFFFFF,"
            f"BorderStyle=3,OutlineColour={box},BackColour={box},Outline=1,Shadow=0,"
            f"Alignment=2,MarginV=70")


def reel_command(clip: Path, srt_name: str, style: str, out: Path) -> list[str]:
    """srt_name va sin ruta: el filtro subtitles no acepta 'C:\\...' y FFmpeg corre en la carpeta del .srt."""
    vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
          f"subtitles={srt_name}:force_style='{style}'")
    return [
        "ffmpeg", "-y", "-v", "error", "-i", str(clip), "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-af", "loudnorm",
        "-movflags", "+faststart", str(out),
    ]


def make_reel(clip: Path, srt: Path, brand: Brand, out: Path) -> Path:
    cmd = reel_command(clip.resolve(), srt.name, style_from_brand(brand), out.resolve())
    subprocess.run(cmd, cwd=srt.parent, capture_output=True, text=True, check=True)
    log.info("Reel listo: %s", out)
    return out


def edit_reel(clip: Path, brand: Brand, settings: Settings, out: Path) -> Path:
    """Clip grabado → Reel 1080x1920 con subtítulos de marca. Falla antes de procesar si el clip no sirve."""
    problems = check_raw(probe(clip))
    if problems:
        raise ValueError(f"{clip.name}: " + "; ".join(problems))
    srt = write_srt(group_words(transcribe(clip, settings)), out.with_suffix(".srt"))
    make_reel(clip, srt, brand, out)
    problems = check_reel(probe(out))
    if problems:
        raise ValueError(f"{out.name}: " + "; ".join(problems))
    return out
