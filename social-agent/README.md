# Agente de redes sociales (MVP · Plan 1)

Incorpora clientes nuevos desde un formulario de Notion (con su manual de marca en PDF), genera la parrilla mensual de cada cliente con Gemini, diseña las piezas con plantillas HTML de la marca, las manda a Notion para que el cliente apruebe y programa las aprobadas en Instagram y Facebook a través de Postiz.

```
onboarding (cada minuto) Formulario Notion (+ manual PDF) → Gemini rellena → Por revisar → Aprobado → clients/<slug>/brand.yaml
plan (día 20)            Gemini (temas + copy + hashtags) → Playwright (PNG) → Postiz (sube medios) → Notion (Pendiente)
                                                                                                          │ el cliente aprueba
publish (cada minuto)    Notion (Aprobado) → Postiz programa en IG + FB → Notion (Programado)
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
3. Permisos. Postiz tiene dos conectores de Instagram; usamos **Instagram (Standalone)** porque no exige vincular el Instagram a una página de Facebook (con @vic.mercadeo Meta bloqueó ese vínculo con "No se puede establecer el enlace en este momento").
   | Caso de uso en la app | Permisos | Para |
   | --- | --- | --- |
   | *Administrar todo en tu página → Personalizar* | `pages_show_list`, `business_management`, `pages_manage_posts`, `pages_manage_engagement`, `pages_read_engagement`, `read_insights` + `pages_read_user_content` (dependencia que exige Meta) | Facebook Page |
   | *API de Instagram → Configuración de la API con inicio de sesión de Instagram* | `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_insights` | Instagram (Standalone). Funciona con cuentas de Empresa y de Creador |
   | *Administrar todo en tu página* (solo si se usa el conector Facebook Business) | `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights` | Instagram (Facebook Business). Exige el Instagram vinculado a la página |
4. Dominios y redirecciones (Meta no acepta `localhost`):
   - *Configuración → Básica*: dominio `emprendeconvic.com` y plataforma Sitio web `https://postiz.emprendeconvic.com/`.
   - *Inicio de sesión con Facebook → Configuración → URI de redireccionamiento*: `https://postiz.emprendeconvic.com/integrations/social/facebook` y `…/integrations/social/instagram`.
   - *API de Instagram → Configurar inicio de sesión para empresas*: `https://postiz.emprendeconvic.com/integrations/social/instagram-standalone`.
   - El ID y la clave de la **app de Instagram** (no son los de Facebook) van en `../postiz-docker-compose/.env` como `INSTAGRAM_APP_ID` e `INSTAGRAM_APP_SECRET`.
5. Mientras la app esté en modo desarrollo solo conectan cuentas con rol en la app: la página la conecta un admin/developer, y el Instagram debe estar en *Roles de la app → Evaluadores de Instagram* (se acepta en Instagram → *Apps y sitios web → Invitaciones de evaluador*).
6. La app pertenece a un portafolio comercial: los roles se asignan desde *business.facebook.com → Cuentas → Apps → Asignar personas*, no desde developers.facebook.com. Para vincular activos hace falta control total **en el portafolio dueño** de la página, no en uno que solo la comparte.
7. Para clientes reales: **App Review** con un screencast por permiso mostrando el flujo en Postiz, Advanced Access y app en **Live**.

Cada cliente necesita su Instagram como cuenta **profesional** (Empresa o Creador).

### Postiz (open source, autoalojado)

Instalación, túnel de Cloudflare y R2 en [`../postiz-docker-compose/README.md`](../postiz-docker-compose/README.md). Lo que importa para el agente:

1. Postiz se publica en `https://postiz.emprendeconvic.com` con un túnel de Cloudflare. No uses `*.trycloudflare.com`: el login de Postiz no funciona ahí.
2. Los medios deben ir a **Cloudflare R2** (`STORAGE_PROVIDER: 'cloudflare'`) con URL pública `https://media.emprendeconvic.com`. Meta descarga cada imagen desde esa URL, y Notion la usa para mostrar las piezas al cliente.
3. Conecta la página y el Instagram de cada cliente desde el panel de Postiz: *Add Channel → Facebook Page* e *Instagram (Standalone)*.
4. Genera la clave en *Settings → Public API* y ponla en `.env`:
   ```
   POSTIZ_URL=https://postiz.emprendeconvic.com/api
   POSTIZ_API_KEY=<clave>
   ```
5. Ejecuta `uv run social-agent integrations` y copia los IDs en `postiz_integrations` del `brand.yaml` del cliente. La clave es el identificador que imprime el comando (`instagram-standalone` o `instagram`, según el conector):
   ```yaml
   postiz_integrations:
     instagram-standalone: "<id>"
     facebook: "<id>"
   ```

La API pública de Postiz permite unas 90 publicaciones creadas por hora. Si responde 401, la clave del `.env` es de otra instalación: genera una nueva en *Settings → Public API*.

### Pinterest (pendiente)

Postiz lo soporta (`PINTEREST_CLIENT_ID`/`SECRET`, redirect `https://postiz.emprendeconvic.com/integrations/social/pinterest`, permisos `boards:read`, `boards:write`, `pins:read`, `pins:write`, `user_accounts:read`), pero Pinterest **denegó el acceso de prueba** a la app `emprendeconvic Social Publisher` (ID 1632065). Antes de volver a pedirlo: sitio web `https://emprendeconvic.com` (no la URL de redirección), la redirección en su propio campo, una política de privacidad que cargue bien y mencione Pinterest, y el dominio reclamado en Pinterest. Cuando haya acceso, el agente necesita un `pinterest_board` (ID numérico del tablero) y recortar descripción (500) y carruseles (5 imágenes).

### Notion

1. En [notion.so/my-integrations](https://www.notion.so/my-integrations) crea una integración interna y copia el token en `NOTION_TOKEN`.
2. Crea una base de datos con estas propiedades (nombres exactos):

   | Propiedad   | Tipo                                                 |
   | ----------- | ---------------------------------------------------- |
   | Pieza       | Título                                              |
   | ID          | Texto                                                |
   | Cliente     | Selección                                           |
   | Tipo        | Selección                                           |
   | Fecha       | Fecha                                                |
   | Pilar       | Selección                                           |
   | Copy        | Texto                                                |
   | Estado      | Selección: Pendiente, Aprobado, Cambios, Programado |
   | Comentarios | Texto                                                |
   | Imágenes   | Archivos y multimedia                                |
   | Postiz      | Texto                                                |

   `Estado` debe ser de tipo **Selección**, no del tipo *Estado* que Notion sugiere.
3. En la base de datos: *··· → Conexiones* → añade tu integración.
4. Copia el ID de la base de datos (o el enlace completo; el agente extrae el ID) en `NOTION_DATABASE_ID`.

### Onboarding de clientes (formulario en Notion)

El formulario de las secciones A, B y C del documento de configuración vive en una segunda base de datos, "Onboarding de clientes". Cada respuesta es una fila; el agente convierte las aprobadas en `clients/<slug>/brand.yaml`.

1. Comparte con tu integración la página de Notion donde quieres la base (*··· → Conexiones*) y créala:
   ```bash
   uv run social-agent onboarding setup --parent "<enlace de la página>"
   ```
   Copia el ID que imprime en `NOTION_ONBOARDING_DATABASE_ID`.
2. En la base: *+ Agregar vista → Formulario* (si solo hay una vista, el **+** aparece al pasar el mouse junto a su nombre). Deja fuera las propiedades internas (`Estado`, `Slug`, `Completado por IA`, `Notas del agente`) y marca como obligatoria `Marca`. Ojo: eliminar una pregunta del formulario **borra la propiedad de la base**; `onboarding run` recrea al empezar las que falten (vacías). Las respuestas pueden llegar sin `Estado`: el agente las trata como *Nuevo*. Comparte con el cliente el enlace de *Share form*, no el de la vista.
3. Formato que espera el agente:
   - **Colores:** hex con etiqueta, p. ej. `primario #16a4c8, secundario #1c80b4, fondo #ffffff, texto #0f172a, acento #87ceeb`. Solo el primario es obligatorio.
   - **Tipografías:** `títulos: Antonio, texto: Inter`, con nombres de Google Fonts (las plantillas las cargan de ahí).
   - **Datos del negocio:** un dato por línea (productos, precios, historia, horarios). Van a `facts`.
4. Ejecuta:
   ```bash
   uv run social-agent onboarding run
   ```
   **Filas nuevas** (Estado *Nuevo* o vacío): si traen el **manual de marca en PDF**, Gemini lo lee y rellena solo los campos que el cliente dejó vacíos. `Completado por IA` dice qué campos llenó y `Notas del agente`, lo que no pudo resolver. La fila pasa a **Por revisar**, también si no hay PDF.
   - **Selecciones** (Sector, Redes activas, Objetivos, Tono): solo usa las opciones que existen en Notion; lo que no encaja queda en las notas. Las opciones nuevas del código (p. ej. *Deportes y entretenimiento*) se añaden a la base sin borrar las que se crearon a mano.
   - **Colores:** Gemini copia los códigos tal como vienen; el agente convierte RGB y CMYK a hex y lo anota. Un color que solo trae Pantone queda pendiente (no hay conversión exacta sin la tabla oficial).
   - **Tipografías:** el agente comprueba en Google Fonts que la fuente cargue con los pesos de las plantillas (600/700 en títulos, 400/500 en texto). Si no, usa la alternativa digital del manual (la de títulos sirve para texto si no hay otra) y lo explica. Quita el grosor del nombre (*Lato Regular* → *Lato*).
   - **Datos del negocio:** solo lo útil para publicar; descarta tamaños de logo, márgenes y otras especificaciones de impresión.

   Revisa sobre todo lo completado por IA y pasa la fila a **Aprobado**. En la siguiente ejecución, el agente crea la carpeta del cliente, descarga el logo y deja la fila en **Activo**. Si falta algo, la deja en **Error** con el motivo en `Notas del agente`; corrige y vuelve a ponerla en Aprobado. Nunca sobrescribe un `brand.yaml` existente.

   Probado con el manual oficial de CONMEBOL Sudamericana (42 páginas): extrajo Oswald (la fuente digital del manual, porque la oficial *Branding* no está en Google Fonts), blanco y negro (los únicos colores con código) y no inventó el resto.

   En la capa gratuita de Gemini, Google puede usar el manual para entrenar sus modelos: avísale al cliente.

   **Límite de tamaño:** en el plan gratuito de Notion cada archivo subido puede pesar como máximo **5 MB** (el formulario y la API lo rechazan por encima). Si el manual pesa más, comprímelo (p. ej. con *Guardar como PDF reducido* o ilovepdf.com) antes de subirlo. Si la marca no tiene manual, sirve un PDF con capturas de su sitio web: así se hizo el onboarding de Emprendeconvic (18 páginas, 1,5 MB), y Gemini sacó de ahí pilares, público, tono y precios.
5. Conecta sus redes en Postiz y completa `postiz_integrations` con `social-agent integrations`.

Con `social-agent serve` corriendo no hace falta ejecutar `onboarding run`: la pasada se hace sola cada minuto (`APPROVAL_POLL_SECONDS`, 60 por defecto): una fila pasada a Aprobado se activa en menos de un minuto, y una respuesta nueva del formulario se rellena con su manual en ese mismo plazo.

En la parrilla, los **Objetivos** del formulario orientan las llamadas a la acción (Leads → escribir o agendar, Ventas → comprar o reservar, Reconocimiento → guardar y compartir, Fidelización → clientes actuales) y los **Competidores** solo sirven para diferenciar a la marca: el agente no los nombra y descarta los hashtags que los mencionen.

## 3. Uso

```bash
# Canales conectados en Postiz (IDs para postiz_integrations)
uv run social-agent integrations

# Banco de fotos del cliente a partir de sus publicaciones de Instagram (estilos con photos: true)
uv run social-agent fotos --client vic-mercadeo

# Rehacer las imágenes de una parrilla ya escrita con fotos nuevas generadas con IA
uv run social-agent rediseñar --client vic-mercadeo --month 2026-12 --dry-run

# Onboarding: rellenar filas nuevas con su manual y activar las aprobadas
uv run social-agent onboarding run

# Parrilla de un cliente (sube a Postiz y crea las filas en Notion)
uv run social-agent plan --client cafe-la-ladera --month 2026-12

# Programar lo aprobado (posts y carruseles) en su fecha del calendario
uv run social-agent publish

# Prueba: programar lo aprobado dentro de 10 minutos
uv run social-agent publish --now

# Dejarlo corriendo: parrilla del mes siguiente el día 20 a las 07:00 y, cada APPROVAL_POLL_SECONDS (60 s),
# revisión de aprobados y onboarding (si NOTION_ONBOARDING_DATABASE_ID está configurado)
uv run social-agent serve
```

**Nuevo cliente:** usa el formulario de onboarding (§2, *Onboarding de clientes*), o copia `clients/demo/` a `clients/<slug>/`, completa `brand.yaml` con su manual de marca y pon el logo (PNG o SVG) en la misma carpeta.

- **`facts`:** datos reales del negocio (productos, precios, historia, equipo, horarios). Es lo único concreto que el modelo puede afirmar; cualquier nombre, lugar, cifra o producto que no esté ahí sale como `[DATO POR CONFIRMAR]` para que el cliente lo complete en Notion.
- **`postiz_integrations`:** los canales del cliente (`facebook`, `instagram` o `instagram-standalone`); cada pieza se programa en todos los que haya.
- **`objectives` y `competitors`:** opcionales; los llena el onboarding. Los objetivos orientan las llamadas a la acción y los competidores nunca se nombran.

Windows: si la consola muestra caracteres raros en tildes, ejecuta con `PYTHONIOENCODING=utf-8`.

Si una pieza aprobada no se puede programar (cliente sin canales, Postiz caído), el error queda en el log, la fila sigue en *Aprobado* y se reintenta en la siguiente pasada.

### Estilo visual y banco de fotos

Sin `style`, las piezas usan las plantillas por defecto (texto sobre color plano). Para que se parezcan a lo que la marca ya publica:

```yaml
fonts:
  heading: Antonio
  body: Inter
  display: Anton        # titulares del estilo (fuente de Google Fonts; se pide sin pesos)
  hand: Gochi Hand      # notas y etiquetas manuscritas
style:
  name: papel-pizarra   # plantillas de templates/estilos/papel-pizarra/
  light_pillars: [Educativo]   # sobre papel claro; el resto, sobre pizarra con garabatos
  photos: true          # usar el banco de fotos de clients/<slug>/fotos/
```

**`papel-pizarra`** salió del análisis de las 31 publicaciones de @vic.mercadeo: titulares condensados con 1–2 palabras en el color de la marca (el campo `highlight` que escribe Gemini), papel arrugado para lo educativo, pizarra con estrellas, ondas y # para lo personal y la venta, notas manuscritas y la persona recortada (con borde blanco tipo sticker en la pizarra). Las texturas y garabatos los dibuja la plantilla (SVG), no se copian de los posts. Los carruseles llevan foto en la portada y el cierre; las láminas del medio son texto centrado.

**Banco de fotos** (`uv run social-agent fotos --client vic-mercadeo`):

1. Lee las publicaciones de Instagram con la conexión que ya tiene Postiz: toma el token del canal `instagram-standalone` del cliente de la base de datos de Postiz (`docker exec postiz-postgres psql …`, solo lectura; el permiso `instagram_business_basic` no publica ni borra).
2. Descarga las láminas de carrusel, las imágenes y las portadas de los Reels que aún no procesó (`fotos.json → seen`).
3. Recorta a la persona con **rembg** (`u2net_human_seg`, ~170 MB que se descargan la primera vez en `~/.u2net`) y se queda con la figura más grande para quitar restos del fondo.
4. **Gemini revisa cada recorte**: lo descarta si tiene texto o gráficos pegados, restos de muebles o el cuerpo cortado, describe gesto, ropa y encuadre, y detecta fotos en espejo (cámara frontal), que el agente voltea.
5. Guarda `clients/<slug>/fotos/<id>.png` y `fotos.json` (útil o no, motivo, descripción, enlace al post). La carpeta está en `.gitignore`.

Con @vic.mercadeo: 64 imágenes → 46 recortes → **16 útiles**. Al generar la parrilla, Gemini elige para cada post, carrusel e historia la foto que mejor va con su tema (sin repetir en la semana); si elige mal, se usa la menos usada. El día 20, `serve` actualiza el banco antes de la parrilla; si falla, sigue con el banco que había. Para revisar el banco a mano, abre la carpeta y cambia `usable` en `fotos.json`.

#### Fotos nuevas generadas con IA (`style.generate_photos: true`)

Para que las piezas no repitan las fotos ya publicadas, cada post, carrusel e historia lleva una **foto nueva** de la persona, generada a partir del banco:

1. Al escribir el copy, Gemini describe la foto de cada pieza en el campo `visual` (pose, gesto, ropa, objeto; distinta entre piezas de la semana).
2. `assign_photos` elige la foto del banco que mejor encaja; esa foto y dos primeros planos van como **referencias** a `GEMINI_IMAGE_MODEL` (por defecto `gemini-3-pro-image`), que genera a la misma persona en la pose descrita, sobre fondo gris liso.
3. La imagen se recorta (rembg) y la revisa el mismo filtro del banco. Hasta 2 intentos; si ninguno sirve, la pieza se queda con la foto original.
4. Se guardan en `clients/<slug>/fotos/generadas/<mes>/<pieza>.png` y no se vuelven a generar si ya existen.
5. La página de Notion de cada pieza dice *"Imagen generada con IA a partir de fotos de la marca"*. Las imágenes de Gemini llevan la marca SynthID (invisible) y Meta puede mostrar "Información de IA".

**Costo y cuota:** el modelo de imagen **no está en la capa gratuita** (responde `limit: 0`). Hay que activar la facturación del proyecto de la clave en [aistudio.google.com](https://aistudio.google.com) (*Settings → Plan / Billing*). Son ~27 imágenes por cliente y mes, más reintentos; revisa el precio por imagen en [ai.google.dev/pricing](https://ai.google.dev/pricing). Sin cuota, la parrilla del día 20 sale igual con las fotos originales y lo deja en el log.

**Rehacer las imágenes de una parrilla ya escrita** (mismos textos, fotos nuevas):

```bash
uv run social-agent rediseñar --client vic-mercadeo --month 2026-12 --dry-run   # genera y diseña en out/, sin tocar Notion
uv run social-agent rediseñar --client vic-mercadeo --month 2026-12             # reemplaza en Notion las filas Pendiente
```

Solo reemplaza las filas en **Pendiente** (las archiva y las crea de nuevo con las imágenes nuevas); las aprobadas, programadas o con cambios no se tocan y el comando las lista.

## 4. Lo que sigue siendo manual

- **Historias:** se aprueban en Notion pero no se envían a Postiz, porque la API no permite añadir stickers y una historia publicada no se puede editar. Cuando estén aprobadas, una persona sube la imagen desde la app de Instagram, añade el sticker sugerido (está en la página de Notion) y cambia el Estado a *Programado*. La imagen deja libre el espacio para el sticker. Instagram solo permite historias por API en cuentas de Empresa, no de Creador.
- **Guiones de Reel:** el Plan 1 los entrega como texto en Notion; no se publican.
- **Cambios pedidos por el cliente:** las filas en estado *Cambios* las revisa una persona.

## 5. Cambiar de modelo más adelante

Todo el código llama a `LLMProvider` (`src/social_agent/llm/base.py`). Para usar Claude u otro proveedor, añade `llm/<proveedor>.py` con el método `generate` y regístralo en `llm/__init__.py`; luego cambia `LLM_PROVIDER`.
