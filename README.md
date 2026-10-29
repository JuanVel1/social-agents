# Agente de IA para redes sociales

Automatiza la gestión de Facebook e Instagram de los clientes de Emprende con Vic. Cada mes genera la parrilla de contenido con un LLM, diseña las piezas con la marca del cliente, las envía a Notion para aprobación y programa las aprobadas en Postiz.

```
brand.yaml del cliente
        │
        ▼
social-agent plan  →  Gemini (temas, copy, hashtags)  →  Playwright (PNG)  →  Notion (Pendiente)
                                                                                   │ el cliente aprueba
social-agent publish (cada hora)  ←─────────────────────────────────────────  Notion (Aprobado)
        └→ Postiz programa en IG + FB  →  Notion (Programado)
```

## Contenido del repositorio

| Ruta | Qué es |
|---|---|
| [`social-agent/`](social-agent/) | El agente en Python: CLI, plantillas de diseño, integración con Gemini, Notion y Postiz. [Instalación y uso](social-agent/README.md). |
| [`postiz-docker-compose/`](postiz-docker-compose/) | Postiz autoalojado en Docker, con túnel de Cloudflare y medios en R2. Basado en [gitroomhq/postiz-docker-compose](https://github.com/gitroomhq/postiz-docker-compose), con la configuración propia. [Arranque y configuración de Meta](postiz-docker-compose/README.md). |
| [`Planes de redes.md`](Planes%20de%20redes.md) | Planes comerciales (Plan 1 básico y Plan 2 pro) y sus entregables semanales. |
| [`Documento de Configuración e Implementación_ Agente de IA para Redes Sociales.md`](Documento%20de%20Configuraci%C3%B3n%20e%20Implementaci%C3%B3n_%20Agente%20de%20IA%20para%20Redes%20Sociales.md) | Onboarding del cliente, flujo operativo, estructura del informe de rendimiento y funcionalidades futuras. |
| [`arquitectura-agente-open-source.html`](arquitectura-agente-open-source.html) | Arquitectura implementada: Gemini, Playwright, Notion y Postiz. |
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

## Alcance actual (Plan 1)

- **Automático:** parrilla mensual, diseño de posts, carruseles e historias, aprobación en Notion y programación de posts y carruseles.
- **Manual:** publicar las historias (llevan stickers que la API no permite), grabar los Reels (el plan entrega solo el guion) y atender los cambios que pida el cliente.

## Secretos

Las credenciales van en `social-agent/.env` y `postiz-docker-compose/.env`, que están en `.gitignore`. Usa `social-agent/.env.example` como plantilla y no escribas claves en `docker-compose.yaml` ni en `brand.yaml`.
