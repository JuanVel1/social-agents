import json

import httpx
import pytest
import yaml

from social_agent.approval import API
from social_agent.llm.fake import FakeProvider
from social_agent.models import Brand, ColorSpec
from social_agent.onboarding import (
    PROPERTIES,
    OnboardingBoard,
    OnboardingError,
    brand_data,
    choose_font,
    color_hex,
    parse_colors,
    parse_fonts,
    row_values,
    run,
    slugify,
)


def _rich(value):
    return [{"plain_text": value}] if value else []


def notion_page(page_id="p1", **overrides):
    """Fila de Notion como la devuelve la API, con todas las propiedades del formulario."""
    values = {
        "Marca": "Café La Ladera",
        "Sector": "Alimentos y bebidas",
        "Colores": "primario #8A3B12, secundario #7a6a5c, fondo #f6efe6",
        "Tipografías": "títulos: Fraunces, texto: Work Sans",
        "Sitio web": "https://cafelaladera.co",
        "Instagram": "https://instagram.com/cafelaladera/",
        "Pilar educativo": "Enseñar a preparar buen café en casa",
        "Pilar de venta": "Plan de suscripción mensual",
        "Pilar de conexión": "",
        "Objetivos": ["Ventas", "Fidelización"],
        "Cliente ideal": "Adultos de 25 a 45 años en Medellín",
        "Competidores": "Pergamino, Café Velvet\nJuan Valdez",
        "Tono": ["Cercano", "Educativo"],
        "Datos del negocio": "- Café de origen antioqueño\n- Suscripción con entrega en Medellín",
        "Estado": "Aprobado",
        "Logo": [],
    }
    values.update(overrides)
    properties = {}
    for name, schema in PROPERTIES.items():
        kind = next(iter(schema))
        value = values.get(name)
        if kind in ("title", "rich_text"):
            prop = _rich(value)
        elif kind == "select":
            prop = {"name": value} if value else None
        elif kind == "multi_select":
            prop = [{"name": v} for v in value or []]
        elif kind == "files":
            prop = value or []
        else:
            prop = value
        properties[name] = {"type": kind, kind: prop}
    return {"id": page_id, "url": f"https://www.notion.so/{page_id}", "properties": properties}


def test_slugify_removes_accents_and_symbols():
    assert slugify("Café La Ladera!") == "cafe-la-ladera"


def test_parse_colors_by_label_order_and_defaults():
    assert parse_colors("primario #8A3B12, fondo #F6EFE6") == {
        "primary": "#8a3b12", "secondary": "#8a3b12", "background": "#f6efe6",
        "text": "#1f1f1f", "accent": "#8a3b12",
    }
    assert parse_colors("#111111 #222222")["primary"] == "#111111"
    # Sobre fondo oscuro el texto por defecto es blanco, no gris oscuro.
    assert parse_colors("primario #ffcc00, fondo #000000")["text"] == "#ffffff"
    with pytest.raises(OnboardingError):
        parse_colors("azul y blanco")


def test_parse_fonts_with_one_or_two_fonts():
    assert parse_fonts("títulos: Antonio, texto: Inter") == {"heading": "Antonio", "body": "Inter"}
    assert parse_fonts("Montserrat") == {"heading": "Montserrat", "body": "Montserrat"}
    assert parse_fonts("") == {"heading": "Inter", "body": "Inter"}
    assert parse_fonts("títulos: Playfair Display Bold, texto: Lato Regular") == {
        "heading": "Playfair Display", "body": "Lato"}
    assert parse_fonts("Open Sans SemiBold Italic") == {"heading": "Open Sans", "body": "Open Sans"}


def test_brand_data_builds_a_valid_brand():
    data = brand_data(row_values(notion_page()))
    brand = Brand(slug="cafe-la-ladera", **data)
    assert brand.tone == "Cercano y educativo"
    assert brand.handle == "@cafelaladera"
    assert [p.name for p in brand.pillars] == ["Educativo", "Conversión"]
    assert brand.facts[-1] == "Sitio web: https://cafelaladera.co"
    assert brand.competitors == ["Pergamino", "Café Velvet", "Juan Valdez"]
    assert brand.objectives == ["Ventas", "Fidelización"]
    assert brand.fonts == {"heading": "Fraunces", "body": "Work Sans"}


def test_brand_data_requires_audience():
    with pytest.raises(OnboardingError, match="Cliente ideal"):
        brand_data(row_values(notion_page(**{"Cliente ideal": ""})))


def _board(pages, updates):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/query"):
            condition = json.loads(request.content)["filter"]
            state = "Nuevo" if "or" in condition else condition["select"]["equals"]
            matching = [p for p in pages if (row_values(p)["Estado"] or "Nuevo") == state]
            return httpx.Response(200, json={"results": matching, "has_more": False})
        if request.method == "GET":
            schema = {name: {"type": next(iter(s)), **s} for name, s in PROPERTIES.items()}
            return httpx.Response(200, json={"properties": schema})
        if request.method == "PATCH":
            updates.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)["properties"]))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    return OnboardingBoard("token", "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d", client=client)


def _no_llm():
    raise AssertionError("no hay filas nuevas: no debería crearse el modelo")


def _text_of(prop):
    return "".join(t["text"]["content"] for t in prop["rich_text"])


def test_run_writes_brand_yaml_logo_and_marks_rows(tmp_path):
    logo = [{"name": "Logo café.SVG", "type": "file", "file": {"url": "https://files.example/logo.svg?sig=1"}}]
    pages = [notion_page("ok", Logo=logo), notion_page("bad", Tono=[])]
    updates = []
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b"<svg/>"

    assert run(_board(pages, updates), tmp_path, _no_llm, fetch) == {"por_revisar": 0, "activos": 1}

    folder = tmp_path / "cafe-la-ladera"
    data = yaml.safe_load((folder / "brand.yaml").read_text(encoding="utf-8"))
    assert data["logo"] == "logo.svg" and (folder / "logo.svg").read_bytes() == b"<svg/>"
    assert Brand.load(tmp_path, "cafe-la-ladera").name == "Café La Ladera"
    assert fetched == ["https://files.example/logo.svg?sig=1"]

    states = {page_id: props["Estado"]["select"]["name"] for page_id, props in updates}
    assert states == {"ok": "Activo", "bad": "Error"}
    slug = next(props for page_id, props in updates if page_id == "ok")["Slug"]
    assert slug["rich_text"][0]["text"]["content"] == "cafe-la-ladera"


def test_run_never_overwrites_an_existing_client(tmp_path):
    (tmp_path / "cafe-la-ladera").mkdir()
    (tmp_path / "cafe-la-ladera" / "brand.yaml").write_text("name: original\n", encoding="utf-8")
    updates = []
    assert run(_board([notion_page()], updates), tmp_path, _no_llm, lambda url: b"")["activos"] == 0
    assert (tmp_path / "cafe-la-ladera" / "brand.yaml").read_text(encoding="utf-8") == "name: original\n"
    assert updates[0][1]["Estado"]["select"]["name"] == "Error"


class RecordingLLM(FakeProvider):
    def __init__(self):
        self.calls = []

    def generate(self, prompt, schema, **kwargs):
        self.calls.append((prompt, kwargs))
        return super().generate(prompt, schema, **kwargs)


def test_autofill_fills_only_empty_fields_with_valid_options(tmp_path):
    manual = [{"name": "Manual de marca.pdf", "type": "file", "file": {"url": "https://files.example/manual.pdf"}}]
    # El cliente respondió Marca y Tono; el resto viene vacío y sin Estado (así llegan del formulario).
    page = notion_page("new", **{
        "Estado": None, "Manual de marca": manual, "Sector": None, "Colores": "", "Tipografías": "",
        "Pilar educativo": "", "Cliente ideal": "", "Datos del negocio": "", "Tono": ["Directo"],
    })
    updates = []
    llm = RecordingLLM()

    result = run(_board([page], updates), tmp_path, lambda: llm, lambda url: b"%PDF-1.7",
                 font_exists=lambda name, weights: name in {"Fraunces", "Work Sans"})

    assert result == {"por_revisar": 1, "activos": 0}
    prompt, kwargs = llm.calls[0]
    assert kwargs["attachments"] == [(b"%PDF-1.7", "application/pdf")]
    assert "Alimentos y bebidas" in prompt  # las opciones de Notion van en el prompt

    props = updates[0][1]
    assert props["Estado"]["select"]["name"] == "Por revisar"
    assert props["Sector"]["select"]["name"] == "Alimentos y bebidas"
    assert _text_of(props["Colores"]) == "primario #8a3b12, fondo #f5ede6"  # fondo calculado desde CMYK
    assert _text_of(props["Tipografías"]) == "títulos: Fraunces, texto: Work Sans"
    assert [o["name"] for o in props["Redes activas"]["multi_select"]] == ["Instagram"]
    assert "Tono" not in props  # el cliente ya lo había respondido
    assert "Marca" not in props
    notes = _text_of(props["Notas del agente"])
    assert "Snapchat (Redes activas)" in notes
    assert "Crema #f5ede6 calculado desde CMYK" in notes
    assert "Oro: solo viene en Pantone 871 C" in notes
    assert "Branding no está en Google Fonts" in notes
    filled = _text_of(props["Completado por IA"])
    assert "Colores" in filled and "Tono" not in filled


def test_autofill_without_pdf_goes_to_review_without_calling_the_model(tmp_path):
    updates = []
    run(_board([notion_page("new", Estado="Nuevo")], updates), tmp_path, _no_llm_rows_only(), lambda url: b"")
    props = updates[0][1]
    assert props["Estado"]["select"]["name"] == "Por revisar"
    assert "Sin manual" in _text_of(props["Notas del agente"])


def test_color_hex_from_hex_rgb_and_cmyk():
    assert color_hex(ColorSpec(hex="#FFF")) == ("#ffffff", "hex")
    assert color_hex(ColorSpec(rgb="R0 G84 B164"))[0] == "#0054a4"
    assert color_hex(ColorSpec(cmyk="C0 M20 Y60 K20"))[0] == "#cca352"
    assert color_hex(ColorSpec(pantone="Reflex Blue C")) == (None, "")


def test_choose_font_skips_fonts_missing_from_google_fonts():
    exists = lambda name, weights: name == "Oswald"  # noqa: E731
    assert choose_font("heading", ["Branding Bold", "Oswald"], exists)[0] == "Oswald"
    assert choose_font("body", ["Branding", "Arial", "Oswald"], exists)[0] == "Oswald"
    assert choose_font("body", ["Arial", ""], exists) == (
        "", "Ninguna tipografía de texto del manual (Arial) está en Google Fonts: elige una a mano")


def test_ensure_properties_adds_new_options_and_keeps_existing_ones():
    schema = {name: {"type": next(iter(s)), **s} for name, s in PROPERTIES.items()}
    schema["Sector"] = {"type": "select", "select": {"options": [{"name": "Cafeterías"}]}}
    patches = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"properties": schema})
        patches.append(json.loads(request.content)["properties"])
        return httpx.Response(200, json={})

    client = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    assert OnboardingBoard("t", "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d", client=client).ensure_properties() == ["Sector"]
    names = [o["name"] for o in patches[0]["Sector"]["select"]["options"]]
    assert names[0] == "Cafeterías" and "Deportes y entretenimiento" in names


def _no_llm_rows_only():
    class NoCalls(FakeProvider):
        def generate(self, *args, **kwargs):
            raise AssertionError("sin PDF no se llama al modelo")

    return lambda: NoCalls()
