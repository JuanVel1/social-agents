"""Personaje 3D animado de la marca: una caricatura de la persona en estilo de película animada.

1. `make_masters`: convierte una foto real en personaje (varias variantes) con Qwen-Image-Edit, un Space
   gratuito de Hugging Face. Al editar la foto real conserva el pelo, la barba, los piercings y la ropa.
2. `choose_master`: la variante elegida queda como fotos/personaje/maestro.png.
3. `fill_poses`: a partir del maestro genera el banco de poses (POSES), recorta cada una y la revisa el
   modelo. La cuota gratuita alcanza para pocas imágenes al día, así que avanza por tandas: guarda cada
   pose al terminarla y retoma donde quedó (lo hace solo `serve` cada pocas horas).

Las poses entran en el banco de fotos (fotos.json) con kind "personaje" y la parrilla las asigna igual
que las fotos reales cuando el estilo tiene `character: true`.
"""
from __future__ import annotations

import io
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image

from .llm.base import LLMProvider, NoQuotaError
from .models import Brand
from .photos import CHARACTER, Cutter, bank_dir, load_bank, review, save_bank

log = logging.getLogger(__name__)

POSE_ATTEMPTS = 2
MASTER_SEEDS = (11, 23, 37, 41, 53)

MASTER_PROMPT = (
    "Transform this photo into a 3D animated movie character, Pixar-like full color render. Keep exactly "
    "the same person: same face shape, same hairstyle and hair length, same beard, same nose piercing and "
    "earrings, same skin tone and the same clothes. Friendly expressive eyes, slightly stylized "
    "proportions, smooth 3D shading, soft studio lighting, plain light grey background. No text."
)
POSE_PROMPT = (
    "Keep the exact same 3D animated character from this image: same face, hairstyle, beard, piercings, "
    "earrings, skin tone, clothes and Pixar-like render style. Change only the pose and expression: "
    "{pose}. Show the whole character from head to knees, centered, on a plain light grey background. "
    "No text, no logos."
)


@dataclass(frozen=True)
class Pose:
    id: str
    description: str  # para elegir la pose de cada pieza (va al catálogo de assign_photos)
    prompt: str


POSES = (
    Pose("explicando", "Explicando algo con una mano abierta, sonriendo", "explaining something with one open hand raised, friendly smile"),
    Pose("senalando-arriba", "Señalando hacia arriba con el índice, entusiasmado", "pointing up with his index finger, excited expression"),
    Pose("senalando-lado", "Señalando a un lado con ambas manos (hacia el texto)", "pointing to one side with both hands, smiling"),
    Pose("brazos-cruzados", "Brazos cruzados, seguro y sonriente", "arms crossed, confident smile"),
    Pose("laptop", "Trabajando concentrado en una laptop que sostiene", "holding an open laptop and typing on it, focused"),
    Pose("celular", "Mirando su celular y sonriendo", "looking at his smartphone and smiling"),
    Pose("celebrando", "Celebrando con los puños arriba", "celebrating with both fists raised, big happy smile"),
    Pose("pensando", "Pensando con la mano en la barbilla", "thinking with his hand on his chin, looking up"),
    Pose("pulgar-arriba", "Pulgar arriba y guiño", "giving a thumbs up and winking"),
    Pose("sorprendido", "Sorprendido, boca abierta y manos cerca de la cara", "surprised with his mouth open and hands near his cheeks"),
    Pose("cafe", "Relajado con una taza de café", "holding a coffee mug, relaxed and smiling"),
    Pose("libreta", "Escribiendo en una libreta", "writing in a notebook with a pen, concentrated"),
    Pose("megafono", "Anunciando algo con un megáfono", "shouting happily into a megaphone, energetic"),
    Pose("cartel", "Sosteniendo un cartel en blanco con ambas manos", "holding a blank white sign board with both hands"),
    Pose("saludo", "Saludando con la mano", "waving hello with one hand, warm smile"),
    Pose("duda", "Encogido de hombros con las palmas arriba, con duda", "shrugging with both palms up, puzzled expression"),
    Pose("lupa", "Examinando algo con una lupa, curioso", "looking through a magnifying glass, curious"),
    Pose("corazon", "Haciendo un corazón con las manos", "making a heart shape with his hands, smiling"),
    Pose("sentado-laptop", "Sentado en un banco con la laptop en las piernas", "sitting on a stool with a laptop on his lap"),
    Pose("microfono", "Hablando a un micrófono de mano", "speaking into a handheld microphone"),
    Pose("bienvenida", "Presentando con los brazos abiertos, acogedor", "presenting with both arms open wide, welcoming"),
    Pose("cohete", "Sosteniendo un cohete de juguete, emocionado (crecimiento)", "holding a small toy rocket, excited"),
    Pose("caminando", "Caminando decidido hacia adelante", "walking forward confidently, mid-stride"),
    Pose("manos-bolsillos", "Manos en los bolsillos, mirada segura a la cámara", "hands in his pockets, confident look at the camera"),
)


class Editor(Protocol):
    def __call__(self, image: bytes, prompt: str, seed: int) -> bytes: ...


class QwenEditor:
    """Qwen-Image-Edit en un Space de Hugging Face (ZeroGPU). Cada imagen gasta ~1 min de la cuota diaria."""

    SPACE = "Qwen/Qwen-Image-Edit"

    def __init__(self, token: str, space: str = SPACE, steps: int = 40):
        if not token:
            raise RuntimeError("Falta HUGGINGFACE_API_TOKEN en .env para generar el personaje")
        self.token, self.space, self.steps = token, space, steps
        self._client = None

    def __call__(self, image: bytes, prompt: str, seed: int) -> bytes:
        from gradio_client import Client, handle_file
        from gradio_client.exceptions import AppError

        self._client = self._client or Client(self.space, token=self.token, verbose=False)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "entrada.png"
            source.write_bytes(image)
            try:
                result = self._client.predict(image=handle_file(str(source)), prompt=prompt, seed=seed,
                                              randomize_seed=False, true_guidance_scale=4.0,
                                              num_inference_steps=self.steps, api_name="/infer")
            except AppError as exc:
                if "quota" in str(exc).lower():
                    raise NoQuotaError(f"cuota de GPU de Hugging Face agotada por hoy ({exc})") from exc
                raise
        output = result[0] if isinstance(result, (list, tuple)) else result
        if isinstance(output, dict):
            output = output.get("path") or output.get("url")
        return Path(output).read_bytes()


def character_dir(clients_dir: Path, slug: str) -> Path:
    return bank_dir(clients_dir, slug) / CHARACTER


def _png(data: bytes) -> bytes:
    buffer = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def make_masters(clients_dir: Path, brand: Brand, photo: Path, editor: Editor, variants: int = 3) -> list[Path]:
    """Genera variantes del personaje a partir de una foto real. Devuelve las que alcanzó a hacer."""
    folder = character_dir(clients_dir, brand.slug)
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for seed in MASTER_SEEDS[:variants]:
        target = folder / f"maestro-{seed}.png"
        if not target.exists():
            try:
                target.write_bytes(_png(editor(photo.read_bytes(), MASTER_PROMPT, seed)))
            except NoQuotaError:
                if made:
                    log.warning("Sin cuota: solo salieron %s variantes del personaje", len(made))
                    break
                raise
        made.append(target)
    return made


def choose_master(clients_dir: Path, slug: str, variant: int | str) -> Path:
    folder = character_dir(clients_dir, slug)
    source = folder / f"maestro-{variant}.png"
    if not source.exists():
        options = ", ".join(p.stem.removeprefix("maestro-") for p in sorted(folder.glob("maestro-*.png")))
        raise FileNotFoundError(f"No existe la variante {variant}. Hay: {options or 'ninguna'}")
    target = folder / "maestro.png"
    shutil.copyfile(source, target)
    return target


def master_path(clients_dir: Path, slug: str) -> Path:
    return character_dir(clients_dir, slug) / "maestro.png"


def pending_poses(bank: dict) -> list[Pose]:
    entries = {p["id"]: p for p in bank["photos"]}
    return [pose for pose in POSES
            if not (e := entries.get(f"{CHARACTER}-{pose.id}"))
            or (not e["usable"] and e.get("attempts", 1) < POSE_ATTEMPTS)]


def fill_poses(clients_dir: Path, brand: Brand, llm: LLMProvider, editor: Editor, *,
               cutter: Callable[[bytes], bytes | None] | None = None, limit: int | None = None) -> dict:
    """Genera las poses que faltan en el banco hasta terminar, llegar a `limit` o quedarse sin cuota."""
    master = master_path(clients_dir, brand.slug)
    if not master.exists():
        raise FileNotFoundError(f"Falta {master}: crea el personaje con `social-agent personaje maestro` "
                                "y elige una variante con `social-agent personaje elegir`")
    bank = load_bank(clients_dir, brand.slug)
    entries = {p["id"]: p for p in bank["photos"]}
    counts = {"nuevas": 0, "descartadas": 0, "sin_cuota": False}
    for pose in pending_poses(bank)[:limit]:
        pose_id = f"{CHARACTER}-{pose.id}"
        entry = entries.get(pose_id)
        attempt = entry.get("attempts", 1) if entry else 0
        try:
            image = editor(master.read_bytes(), POSE_PROMPT.format(pose=pose.prompt), MASTER_SEEDS[0] + attempt)
        except NoQuotaError as exc:
            log.info("Banco del personaje de %s en pausa: %s", brand.slug, exc)
            counts["sin_cuota"] = True
            break
        cutter = cutter or Cutter("u2net")  # con los objetos que sostiene (laptop, taza, megáfono...)
        png = cutter(image)
        verdict = review(llm, [(pose_id, png)], subject="un solo personaje 3D animado (el de la marca)")[pose_id] \
            if png else None
        file = f"{CHARACTER}/{pose.id}.png"
        if png:
            (bank_dir(clients_dir, brand.slug) / file).write_bytes(png)
        record = {"id": pose_id, "file": file, "kind": CHARACTER, "source": "IA", "date": "",
                  "usable": bool(verdict and verdict.usable),
                  "reason": verdict.reason if verdict else "no se pudo recortar",
                  "description": pose.description, "framing": verdict.framing if verdict else "",
                  "attempts": attempt + 1}
        if entry:
            entry.update(record)
        else:
            bank["photos"].append(record)
            entries[pose_id] = record
        counts["nuevas" if record["usable"] else "descartadas"] += 1
        save_bank(clients_dir, brand.slug, bank)  # la cuota se acaba en cualquier momento: guardar cada pose
    counts["faltan"] = len(pending_poses(bank))
    counts["listas"] = sum(1 for p in bank["photos"] if p.get("kind") == CHARACTER and p["usable"])
    log.info("Personaje de %s: %s", brand.slug, counts)
    return counts
