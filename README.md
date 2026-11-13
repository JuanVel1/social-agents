# Agente de IA para redes sociales

Automatiza la gestión de Facebook e Instagram de los clientes de Emprende con Vic. El cliente llena un formulario en Notion (y puede subir su manual de marca en PDF); el agente arma su perfil de marca, genera cada mes la parrilla de contenido con un LLM, diseña las piezas con su identidad, las envía a Notion para aprobación y programa las aprobadas en Postiz.

```
Formulario de onboarding en Notion (+ manual PDF)
        │  al minuto: Gemini rellena lo que falte → la agencia revisa → Aprobado
        ▼
clients/<slug>/brand.yaml + logo
        │
        ▼  día 20
social-agent plan  →  Gemini (temas, copy, hashtags, moodboard de Pinterest)  →  Playwright (PNG)  →  Notion (Pendiente)
                                                                                   │ el cliente aprueba
social-agent publish (cada minuto) ←─────────────────────────────────────────  Notion (Aprobado)
        └→ Postiz programa en IG + FB  →  Notion (Programado)
```

`social-agent serve` deja las tres tareas corriendo (onboarding y revisión de aprobados cada minuto —`APPROVAL_POLL_SECONDS`—, parrilla el día 20). Lo que se pasa a Aprobado en Notion se procesa en menos de un minuto.

## Contenido del repositorio

| Ruta | Qué es |
|---|---|
| [`social-agent/`](social-agent/) | El agente en Python: CLI, onboarding de clientes, plantillas de diseño, integración con Gemini, Notion y Postiz. [Instalación y uso](social-agent/README.md). |
| [`postiz-docker-compose/`](postiz-docker-compose/) | Postiz autoalojado en Docker, con túnel de Cloudflare y medios en R2. Basado en [gitroomhq/postiz-docker-compose](https://github.com/gitroomhq/postiz-docker-compose), con la configuración propia. [Arranque y configuración de Meta](postiz-docker-compose/README.md). |
| [`Planes de redes.md`](Planes%20de%20redes.md) | Planes comerciales (Plan 1 básico y Plan 2 pro) y sus entregables semanales. |
| [`Documento de Configuración e Implementación_ Agente de IA para Redes Sociales.md`](Documento%20de%20Configuraci%C3%B3n%20e%20Implementaci%C3%B3n_%20Agente%20de%20IA%20para%20Redes%20Sociales.md) | Onboarding del cliente, flujo operativo, estructura del informe de rendimiento y funcionalidades futuras. |
| [`arquitectura-agente-open-source.html`](arquitectura-agente-open-source.html) | Arquitectura implementada: onboarding y aprobación en Notion, Gemini, Playwright, FFmpeg, Postiz, R2 y Meta. |
| [`flujo-agente-redes-sociales.html`](flujo-agente-redes-sociales.html) | Flujo del agente en la propuesta inicial (diseño en Canva). |
| [`diagrama-quien-hace-que.html`](diagrama-quien-hace-que.html) | Quién hace qué en el ciclo mensual (agente, agencia y cliente), según la propuesta inicial (Claude, Canva, Blotato). |
| [`grafico-costo-llm.html`](grafico-costo-llm.html) | Comparación del costo del LLM por cliente entre Claude y Gemini. |

Los archivos `.html` se abren directamente en el navegador. La arquitectura vigente es la de `arquitectura-agente-open-source.html`.

## Inicio rápido

Requisitos: Python 3.11+, [uv](https://docs.astral.sh/uv/) y Docker (para Postiz).

```bash
cd social-agent
uv sync
uv run playwright install chromium
cp .env.example .env      # completa las claves

# Prueba sin red ni claves: genera PNG y un resumen en out/
LLM_PROVIDER=fake uv run social-agent plan --client demo --month 2026-11 --dry-run
uv run pytest
```

Para operar con clientes reales hacen falta las cuentas de Gemini, Meta, Postiz y Notion. Los pasos están en [`social-agent/README.md`](social-agent/README.md#2-apis-y-cuentas). Empieza por Meta: la verificación del negocio y la App Review tardan semanas.

### Levantar todo

```bash
# 1. Postiz + túnel de Cloudflare (Docker Desktop abierto). Espera ~2 min a que postiz salga (healthy)
cd postiz-docker-compose
docker compose up -d
docker ps

# 2. Agente: onboarding y aprobados cada minuto, parrilla el día 20
cd ../social-agent
uv run social-agent integrations     # comprueba la conexión con Postiz
uv run social-agent serve
```

No arranques el túnel con `docker run`: queda fuera de la red de Postiz y Cloudflare responde 502. Justo después de arrancar, Postiz puede dar 502 unos segundos mientras termina de cargar.

## Estado de la configuración (noviembre 2026)

**Hecho y probado**

| Pieza | Estado |
| --- | --- |
| Postiz + túnel | Docker con el servicio `cloudflared` dentro del compose (token en `postiz-docker-compose/.env`). Publicado en https://postiz.emprendeconvic.com |
| Medios | Cloudflare R2, bucket `postiz-media`, URL pública https://media.emprendeconvic.com |
| App de Meta "Bot" | ID `1278757044404022`, en el portafolio **Emprende con Vic**. Casos de uso de página y de API de Instagram (inicio de sesión de Instagram). Juan Vel con control total asignado desde Business Settings |
| Canales en Postiz | Facebook Page **Vic.mercadeo**, **Instagram (Standalone)** @vic.mercadeo y **Pinterest** emprendeconvic (acceso Trial) conectado solo para leer el moodboard |
| Notion | Base de aprobación (`NOTION_DATABASE_ID`) y base **Onboarding de clientes** con vista Formulario (`NOTION_ONBOARDING_DATABASE_ID`) |
| Gemini | Capa gratuita; también lee manuales de marca en PDF |
| Cliente `vic-mercadeo` | Activado desde el formulario de onboarding (Emprendeconvic, con un PDF del sitio como manual). Estilo **papel-pizarra** sacado de sus 31 publicaciones, **banco de 16 fotos** recortadas de su Instagram (leídas con la conexión de Postiz). En Notion: parrilla de prueba de noviembre 2026 (34 piezas, estilo anterior) y de diciembre (36 piezas, regenerada el 9 de noviembre con `plan --reemplazar`), todas en Pendiente |
| Onboarding | Probado de punta a punta con el manual oficial de CONMEBOL Sudamericana (fila de prueba en Notion; su carpeta se borró) |

**Pendiente**

- **Meta:** verificación del negocio, App Review de cada permiso y app en **Live**. Hasta entonces solo funcionan cuentas con rol en la app.
- **Instagram ↔ página:** Meta no permite vincular @vic.mercadeo a una página ("No se puede establecer el enlace en este momento"); por eso se usa el conector Standalone. Las historias por API exigen cuenta de Empresa.
- **Pinterest (moodboard):** canal conectado en Postiz (cuenta **emprendeconvic**, acceso Trial) y configurado en `vic-mercadeo` como **moodboard**, no como canal de publicación (paso 03 del *Documento de Configuración*): tablero **Almacenamiento rápido** (`1104015364845328485`), con 9 pines de referencia ya analizados (`clients/vic-mercadeo/moodboard.json`). Para actualizarlo, guarda más pines en ese tablero: el día 20 se relee solo, o a mano con `social-agent moodboard --client vic-mercadeo`. Ver [`social-agent/README.md`](social-agent/README.md#pinterest-moodboard-investigación-visual).
- **Primera publicación real:** aprobar una pieza en Notion y comprobar que sale en Instagram y Facebook.
- **Clientes nuevos:** conectar sus redes en Postiz y copiar los IDs con `social-agent integrations` sigue siendo manual.
- **Sitio web:** la página `/es/legal` muestra `legal.pageTitle` sin traducir (afecta revisiones de Pinterest y Meta).

**Herramienta de desarrollo:** Claude Code tiene el servidor Playwright MCP con **Brave** (perfil propio en `C:\Users\jvela\.playwright-mcp\brave-profile`) para hacer pasos en Notion, Meta y Postiz; la primera vez hay que iniciar sesión a mano en esa ventana.

## Alcance actual (Plan 1)

- **Automático:** onboarding desde el formulario (con lectura del manual de marca), parrilla mensual, diseño de posts, carruseles e historias, aprobación en Notion y programación de posts y carruseles.
- **Manual:** revisar y aprobar el onboarding, conectar las redes de cada cliente en Postiz, publicar las historias (llevan stickers que la API no permite), grabar los Reels (el plan entrega solo el guion) y atender los cambios que pida el cliente.

## Secretos

Las credenciales van en `social-agent/.env` y `postiz-docker-compose/.env`, que están en `.gitignore`. Usa `social-agent/.env.example` como plantilla y no escribas claves en `docker-compose.yaml` ni en `brand.yaml`.
