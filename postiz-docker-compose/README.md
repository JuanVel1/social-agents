# Postiz autoalojado (publicador del agente de redes)

Postiz programa y publica en Facebook e Instagram las piezas que aprueba el cliente. Corre en Docker en este equipo y se expone a internet con un túnel de Cloudflare en **https://postiz.emprendeconvic.com**.

```
Navegador / Meta / agente
        │ HTTPS
        ▼
Cloudflare (DNS de emprendeconvic.com)
        │ túnel
        ▼
cloudflared  ──red postiz-network──▶  postiz:5000  ──▶ Postgres · Redis · Temporal
                                          │
                                          └──▶ Cloudflare R2 (imágenes públicas que descarga Meta)
```

> Las credenciales (App Secret de Meta, claves de R2, token del túnel, `JWT_SECRET`) van en un `.env` junto a este compose y se referencian como `${VARIABLE}`. No las escribas directamente en `docker-compose.yaml`.

## 1. Arranque

```bash
docker compose up -d
docker ps          # espera ~2 min hasta que postiz aparezca como (healthy)
```

Panel: https://postiz.emprendeconvic.com. Después de crear tu usuario, pon `DISABLE_REGISTRATION: 'true'`.

## 2. URLs de Postiz

Las tres deben usar el dominio público, no `localhost`:

```yaml
MAIN_URL: 'https://postiz.emprendeconvic.com'
FRONTEND_URL: 'https://postiz.emprendeconvic.com'
NEXT_PUBLIC_BACKEND_URL: 'https://postiz.emprendeconvic.com/api'
```

Tras cambiarlas: `docker compose up -d postiz` (con `restart` no relee las variables).

Otros valores que se ajustaron:

| Variable | Valor | Por qué |
|---|---|---|
| `API_LIMIT` | `500` | Límite por hora de la API pública. Con 30 no alcanza ni para subir las ~48 imágenes de una parrilla. |
| `JWT_SECRET` | aleatorio (`openssl rand -hex 32`) | El del repositorio es público. |
| `DISABLE_REGISTRATION` | `'true'` tras crear tu usuario | El panel queda expuesto en internet. |

## 3. Túnel de Cloudflare

El dominio `emprendeconvic.com` está registrado en Hostinger, pero su DNS lo administra Cloudflare (nameservers `*.ns.cloudflare.com`), lo que permite un túnel fijo.

1. **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**, nombre `postiz`. Copia solo el token.
2. **Public Hostname → Add a public hostname**:
   - Subdomain `postiz` · Domain `emprendeconvic.com`
   - Service: `http://postiz:5000` (con `http://`; es el nombre del contenedor dentro de Docker, no `localhost:4007`)
3. Añade el servicio al compose y el token al `.env` (`CF_TUNNEL_TOKEN=...`):

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: postiz-tunnel
    restart: always
    command: tunnel --no-autoupdate run --token ${CF_TUNNEL_TOKEN}
    networks:
      - postiz-network
    depends_on:
      - postiz
```

**No lances el túnel con `docker run`**: ese contenedor queda en la red `bridge` por defecto, no encuentra `postiz:5000` y Cloudflare responde **502 Bad Gateway**. Si ya lo hiciste: `docker network connect postiz-docker-compose_postiz-network <contenedor>` o, mejor, bórralo y usa el servicio del compose. No dejes dos conectores con el mismo token a la vez.

**No uses `*.trycloudflare.com` ni `*.ngrok-free.app`**: Postiz guarda la cookie de sesión en el dominio raíz (`.trycloudflare.com`), los navegadores la rechazan porque es un dominio público compartido y el login se queda en un bucle. Además esas URLs cambian en cada reinicio. Con `emprendeconvic.com` la cookie queda en `.emprendeconvic.com`.

## 4. App de Meta

### 4.1 Crear la app
1. [developers.facebook.com/apps/creation](https://developers.facebook.com/apps/creation/) → selecciona el portafolio comercial.
2. Caso de uso: **"Administrar todo en tu página"** (*Manage everything on your Page*). Una app tipo *Consumer* o creada solo con el caso de uso de Login **no** puede pedir permisos de páginas y produce `Invalid Scopes`.
3. Copia el App ID y el App Secret (*Configuración de la app → Básica*) al `.env` como `FACEBOOK_APP_ID` y `FACEBOOK_APP_SECRET`.

### 4.2 Permisos (Casos de uso → Administrar todo en tu página → Personalizar → Agregar)

Postiz pide estos seis para Facebook:

| Permiso | Para qué |
|---|---|
| `pages_show_list` | Listar las páginas del usuario |
| `business_management` | Acceder a páginas del portafolio comercial |
| `pages_manage_posts` | Publicar en la página |
| `pages_manage_engagement` | Gestionar comentarios |
| `pages_read_engagement` | Leer interacciones |
| `read_insights` | Métricas |

Y Meta exige además esta **dependencia**, aunque Postiz no la pida:

| Permiso | Motivo |
|---|---|
| `pages_read_user_content` | Requisito de `pages_manage_engagement` |

Si el diálogo de Facebook muestra `Invalid Scopes: <permiso>`, ese permiso falta en el caso de uso: agrégalo y espera 1–2 minutos.

**Instagram (pendiente de verificar):** al conectar *Instagram (Facebook Business)* en Postiz se esperan `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments` e `instagram_manage_insights`. Agrégalos igual cuando el diálogo los pida.

### 4.3 Dominios y redirección
- *Configuración de la app → Básica*:
  - **Dominios de la app:** `emprendeconvic.com` (Meta no acepta `localhost`).
  - **+ Agregar plataforma → Sitio web → URL del sitio:** `https://postiz.emprendeconvic.com/`
- *Facebook Login for Business → Configuración → Configuración del cliente de OAuth*:
  - Inicio de sesión del cliente de OAuth y de OAuth web: **Sí**. Aplicar HTTPS y modo estricto: **Sí**.
  - **URI de redireccionamiento de OAuth válidos:** `https://postiz.emprendeconvic.com/integrations/social/facebook` (exacta, sin `/` final).

### 4.4 Quién puede conectar y publicar
- Mientras la app esté en **modo desarrollo** solo pueden conectar cuentas con rol en la app (admin, developer o tester) que además administren la página. Las publicaciones con imagen pueden verse solo para esas cuentas.
- Para clientes reales: verificación del negocio → App Review (screencast por permiso) → Advanced Access → pasar la app a **Live**.
- El Instagram de cada cliente debe ser cuenta **profesional** vinculada a su página de Facebook. En Postiz se conecta con **Instagram (Facebook Business)**, no con *Standalone*.

## 5. Almacenamiento en Cloudflare R2

Con `STORAGE_PROVIDER: 'local'` las imágenes quedan en el disco del contenedor y Meta no siempre puede descargarlas; para publicar hay que usar R2. Configurado y verificado el 28-oct-2026: subida desde el panel de Postiz y lectura pública en `https://media.emprendeconvic.com/...`.

`CLOUDFLARE_BUCKETNAME` es el **nombre del bucket** (`postiz-media`), no el dominio público: con el dominio ahí R2 responde *Access Denied*. `CLOUDFLARE_REGION: 'auto'` es obligatorio (Postiz lo pasa al cliente S3).

1. **Bucket:** Cloudflare → R2 → *Create bucket* → `postiz-media`, ubicación *Automatic*, clase *Standard*.
2. **Token:** R2 → *Manage API tokens* → *Create Account API token* → permiso **Object Read & Write** limitado a `postiz-media`. Copia Account ID, Access Key ID y Secret Access Key.
3. **Dominio público:** bucket → *Settings → Custom Domains → Connect Domain* → `media.emprendeconvic.com` (el dominio ya está en Cloudflare, así que el DNS se crea solo). Evita `*.r2.dev`: tiene límite de peticiones y es solo para pruebas.
4. **CORS:** bucket → *Settings → CORS policy*:
   ```json
   [
     {
       "AllowedOrigins": ["https://postiz.emprendeconvic.com"],
       "AllowedMethods": ["GET", "POST", "HEAD", "PUT", "DELETE"],
       "AllowedHeaders": ["Authorization", "x-amz-date", "x-amz-content-sha256", "content-type"],
       "ExposeHeaders": ["ETag", "Location"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```
5. **Compose** (los secretos en `.env`), y elimina `UPLOAD_DIRECTORY` y `NEXT_PUBLIC_UPLOAD_DIRECTORY`:
   ```yaml
   STORAGE_PROVIDER: 'cloudflare'
   CLOUDFLARE_ACCOUNT_ID: ${CLOUDFLARE_ACCOUNT_ID}
   CLOUDFLARE_ACCESS_KEY: ${CLOUDFLARE_ACCESS_KEY}
   CLOUDFLARE_SECRET_ACCESS_KEY: ${CLOUDFLARE_SECRET_ACCESS_KEY}
   CLOUDFLARE_BUCKETNAME: 'postiz-media'
   CLOUDFLARE_BUCKET_URL: 'https://media.emprendeconvic.com'
   CLOUDFLARE_REGION: 'auto'
   ```
   `CLOUDFLARE_BUCKET_URL` es la URL **pública**, no el endpoint `*.r2.cloudflarestorage.com`.
6. `docker compose up -d postiz`, sube una imagen desde el panel y abre su URL en incógnito: debe empezar por `https://media.emprendeconvic.com/`.

## 6. Conexión con el agente

En Postiz: *Settings → Public API* → copia la clave. En `../social-agent/.env`:

```
POSTIZ_URL=https://postiz.emprendeconvic.com/api
POSTIZ_API_KEY=<clave>
```

Luego `uv run social-agent integrations` lista los IDs de Facebook e Instagram para el `brand.yaml` de cada cliente.

## 7. Problemas que ya aparecieron

| Síntoma | Causa | Solución |
|---|---|---|
| `Invalid Scopes: pages_show_list, business_management, …` | App sin el caso de uso de páginas (tipo Consumer o solo Login) | Caso de uso *Administrar todo en tu página* (§4.1) |
| `Invalid Scopes: pages_manage_engagement` | Permiso quitado del caso de uso | Agregarlo en *Personalizar* |
| `Invalid Scopes: pages_read_user_content` | Dependencia de `pages_manage_engagement` | Agregarlo en *Personalizar* |
| "El dominio de esta URL no está incluido en los dominios de la app" | Falta dominio y plataforma Sitio web | §4.3 |
| "Debe contener un dominio de nivel superior" al poner `localhost` | Meta no acepta `localhost` como dominio | Usar `emprendeconvic.com` |
| El login de Postiz no avanza | Cookie rechazada en `*.trycloudflare.com` | Túnel con dominio propio (§3) |
| 502 Bad Gateway | Contenedor del túnel fuera de `postiz-network` | Servicio `cloudflared` en el compose (§3) |
| "Formato de URL de servicio no válido" en Cloudflare | Falta el protocolo | `http://postiz:5000` |
| `AccessDenied` al subir a R2 | Nombre del bucket mal puesto (se puso el dominio) o token sin *Object Read & Write* | §5 |
| `no live upstreams … /auth/avatars/*.jpg` en los logs | Imágenes decorativas del login | Ignorar |

## Referencias
- Configuración de Postiz: https://docs.postiz.com/configuration/reference
- Facebook en Postiz: https://docs.postiz.com/providers/facebook
- R2 en Postiz: https://docs.postiz.com/configuration/r2
- Migración desde versiones viejas de Postiz: https://docs.postiz.com/installation/migration
- Repositorio original del compose: https://github.com/gitroomhq/postiz-docker-compose
