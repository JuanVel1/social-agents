# Agente de redes sociales (MVP · Plan 1)

Genera la parrilla mensual de cada cliente con Gemini, diseña las piezas con plantillas HTML de la marca, las manda a Notion para que el cliente apruebe y programa las aprobadas en Instagram y Facebook a través de Postiz.

```
plan  →  Gemini (temas + copy + hashtags)  →  Playwright (PNG)  →  Postiz (sube medios)  →  Notion (Pendiente)
                                                                                                  │ el cliente aprueba
publish (cada hora)  ←────────────────────────────────────────────────────────────────────  Notion (Aprobado)
        └→ Postiz programa en IG + FB  →  Notion (Programado)
```

Por cliente y mes (Plan 1): ~9 posts, 4–5 carruseles, ~13 historias y 8–9 guiones de Reel. Las fechas salen de `SCHEDULES` en `planner.py`.

## 1. Instalación

```bash
uv sync
uv run playwright install chromium
cp .env.example .env      # y completa las claves
```

Prueba sin red ni claves (modelo falso, solo genera PNG y resumen en `out/`):

```bash
LLM_PROVIDER=fake uv run social-agent plan --client demo --month 2026-11 --dry-run
uv run pytest
```

## 2. APIs y cuentas

### Gemini (capa gratuita)
1. Entra a [aistudio.google.com](https://aistudio.google.com) → *Get API key* → crea la clave en un proyecto propio.
2. Pon la clave en `GEMINI_API_KEY`.
3. Ejecuta `uv run social-agent models` y ajusta `GEMINI_MODEL_QUALITY` y `GEMINI_MODEL_BULK` con los nombres exactos que aparezcan (Flash y Flash-Lite; los modelos Pro no están en la capa gratuita).

**Datos:** en la capa gratuita Google puede usar los prompts para mejorar sus modelos. El agente solo envía información de marca (pilares, tono, temas). No metas datos personales de clientes finales en `brand.yaml`, y avísale a cada cliente piloto.

**Límites:** el código no pasa de `GEMINI_MAX_RPM` solicitudes por minuto (8 por defecto) y reintenta los errores 429. Una parrilla mensual usa unas 6–7 llamadas por cliente.

### Meta (empieza primero: la revisión tarda semanas)
La configuración paso a paso y los errores que ya aparecieron están en [`../postiz-docker-compose/README.md`](../postiz-docker-compose/README.md) (§4 y §7). Resumen:

1. Portafolio comercial de la agencia en business.facebook.com e inicio de la **verificación del negocio**.
2. App en [developers.facebook.com](https://developers.facebook.com/apps/creation/) con el caso de uso **"Administrar todo en tu página"**. Una app *Consumer* o solo con Login da `Invalid Scopes`.
3. Permisos en *Casos de uso → Administrar todo en tu página → Personalizar*:

   | Permiso | Quién lo pide |
   |---|---|
   | `pages_show_list`, `business_management`, `pages_manage_posts`, `pages_manage_engagement`, `pages_read_engagement`, `read_insights` | Postiz (Facebook) |
   | `pages_read_user_content` | Meta, como dependencia de `pages_manage_engagement` |
   | `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights` | Postiz (Instagram), pendiente de verificar |

4. *Configuración → Básica*: dominio de la app `emprendeconvic.com` y plataforma Sitio web `https://postiz.emprendeconvic.com/`. Redirect URI: `https://postiz.emprendeconvic.com/integrations/social/facebook`. Meta no acepta `localhost` como dominio.
5. Mientras la app esté en modo desarrollo solo conectan cuentas con rol en la app (admin, developer o tester) que administren la página: sirve para pruebas y un piloto.
6. Para clientes reales: **App Review** con un screencast por permiso mostrando el flujo en Postiz, Advanced Access y app en **Live**.

Cada cliente necesita su Instagram como cuenta **profesional** vinculada a su página de Facebook.

### Postiz (open source, autoalojado)
Instalación, túnel de Cloudflare y R2 en [`../postiz-docker-compose/README.md`](../postiz-docker-compose/README.md). Lo que importa para el agente:

1. Postiz se publica en `https://postiz.emprendeconvic.com` con un túnel de Cloudflare. No uses `*.trycloudflare.com`: el login de Postiz no funciona ahí.
2. Los medios deben ir a **Cloudflare R2** (`STORAGE_PROVIDER: 'cloudflare'`) con URL pública `https://media.emprendeconvic.com`. Meta descarga cada imagen desde esa URL, y Notion la usa para mostrar las piezas al cliente.
3. Conecta la página y el Instagram de cada cliente desde el panel de Postiz (opción *Instagram (Facebook Business)*).
4. Genera la clave en *Settings → Public API* y ponla en `.env`:
   ```
   POSTIZ_URL=https://postiz.emprendeconvic.com/api
   POSTIZ_API_KEY=<clave>
   ```
5. Ejecuta `uv run social-agent integrations` y copia los IDs en `postiz_integrations` del `brand.yaml` del cliente:
   ```yaml
   postiz_integrations:
     instagram: "<id>"
     facebook: "<id>"
   ```

La API pública de Postiz permite unas 90 publicaciones creadas por hora.

### Notion
1. En [notion.so/my-integrations](https://www.notion.so/my-integrations) crea una integración interna y copia el token en `NOTION_TOKEN`.
2. Crea una base de datos con estas propiedades (nombres exactos):

   | Propiedad | Tipo |
   |---|---|
   | Pieza | Título |
   | ID | Texto |
   | Cliente | Selección |
   | Tipo | Selección |
   | Fecha | Fecha |
   | Pilar | Selección |
   | Copy | Texto |
   | Estado | Selección: Pendiente, Aprobado, Cambios, Programado |
   | Comentarios | Texto |
   | Imágenes | Archivos y multimedia |
   | Postiz | Texto |

   `Estado` debe ser de tipo **Selección**, no del tipo *Estado* que Notion sugiere.
3. En la base de datos: *··· → Conexiones* → añade tu integración.
4. Copia el ID de la base de datos (o el enlace completo; el agente extrae el ID) en `NOTION_DATABASE_ID`.

## 3. Uso

```bash
# Parrilla de un cliente (sube a Postiz y crea las filas en Notion)
uv run social-agent plan --client cafe-la-ladera --month 2026-12

# Programar lo aprobado (posts y carruseles) en su fecha del calendario
uv run social-agent publish

# Prueba: programar lo aprobado dentro de 10 minutos
uv run social-agent publish --now

# Dejarlo corriendo: parrilla del mes siguiente el día 20 a las 07:00 y revisión de aprobados cada hora
uv run social-agent serve
```

**Nuevo cliente:** copia `clients/demo/` a `clients/<slug>/`, completa `brand.yaml` con su manual de marca y pon el logo (PNG o SVG) en la misma carpeta.

- **`facts`:** datos reales del negocio (productos, precios, historia, equipo, horarios). Es lo único concreto que el modelo puede afirmar; cualquier nombre, lugar, cifra o producto que no esté ahí sale como `[DATO POR CONFIRMAR]` para que el cliente lo complete en Notion.
- **`postiz_integrations`:** los canales del cliente. Puede tener solo `facebook`, solo `instagram` o ambos; cada pieza se programa en todos los que haya.

Si una pieza aprobada no se puede programar (cliente sin canales, Postiz caído), el error queda en el log, la fila sigue en *Aprobado* y se reintenta en la siguiente pasada.

## 4. Lo que sigue siendo manual
- **Historias:** se aprueban en Notion pero no se envían a Postiz, porque la API no permite añadir stickers y una historia publicada no se puede editar. Cuando estén aprobadas, una persona sube la imagen desde la app de Instagram, añade el sticker sugerido (está en la página de Notion) y cambia el Estado a *Programado*. La imagen deja libre el espacio para el sticker. Instagram solo permite historias por API en cuentas de Empresa, no de Creador.
- **Guiones de Reel:** el Plan 1 los entrega como texto en Notion; no se publican.
- **Cambios pedidos por el cliente:** las filas en estado *Cambios* las revisa una persona.

## 5. Cambiar de modelo más adelante
Todo el código llama a `LLMProvider` (`src/social_agent/llm/base.py`). Para usar Claude u otro proveedor, añade `llm/<proveedor>.py` con el método `generate` y regístralo en `llm/__init__.py`; luego cambia `LLM_PROVIDER`.
