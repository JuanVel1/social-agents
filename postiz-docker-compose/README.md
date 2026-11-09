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

> Las credenciales van en un `.env` junto a este compose y se referencian como `${VARIABLE}`. No las escribas directamente en `docker-compose.yaml`.
>
> | Variable | Qué es |
> | --- | --- |
> | `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` | App de Meta (§4.1) |
> | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` | App de Instagram dentro de la misma app de Meta (§4.3); no son los de Facebook |
> | `JWT_SECRET` | Aleatorio (§2) |
> | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ACCESS_KEY`, `CLOUDFLARE_SECRET_KEY`, `CLOUDFLARE_BUCKET_NAME` | R2 (§5) |
> | `CF_TUNNEL_TOKEN` | Túnel de Cloudflare (§3) |

## 1. Arranque

Con Docker Desktop abierto:

```bash
docker compose up -d
docker ps          # espera ~2 min hasta que postiz aparezca como (healthy)
```

Los contenedores tienen `restart: always`: al abrir Docker Desktop vuelven a arrancar solos, pero tras cambiar el compose o el `.env` hay que ejecutar `docker compose up -d` para que tomen la configuración nueva. En los primeros segundos después de arrancar, la web y la API pueden responder **502** mientras Postiz termina de cargar.

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

| Variable                 | Valor                                | Por qué                                                                                                |
| ------------------------ | ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `API_LIMIT`            | `500`                              | Límite por hora de la API pública. Con 30 no alcanza ni para subir las ~48 imágenes de una parrilla. |
| `JWT_SECRET`           | aleatorio (`openssl rand -hex 32`) | El del repositorio es público.                                                                         |
| `DISABLE_REGISTRATION` | `'true'` tras crear tu usuario     | El panel queda expuesto en internet.                                                                    |

## 3. Túnel de Cloudflare

El dominio `emprendeconvic.com` está registrado en Hostinger, pero su DNS lo administra Cloudflare (nameservers `*.ns.cloudflare.com`), lo que permite un túnel fijo.

1. **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**, nombre `postiz`. Copia solo el token.
2. **Public Hostname → Add a public hostname**:
   - Subdomain `postiz` · Domain `emprendeconvic.com`
   - Service: `http://postiz:5000` (con `http://`; es el nombre del contenedor dentro de Docker, no `localhost:4007`)
3. El servicio ya está en el compose; solo falta el token en el `.env` (`CF_TUNNEL_TOKEN=eyJ...`). Es una cadena larga que empieza por `eyJ`, en *Tunnels → postiz → Configure*, después de `--token`:

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

**No lances el túnel con `docker run`**: ese contenedor queda en la red `bridge` por defecto, no encuentra `postiz:5000` y Cloudflare responde **502 Bad Gateway**. En sus logs aparece `lookup postiz on 192.168.65.7:53: no such host` (esa IP es el DNS interno de Docker Desktop, no algo que haya que configurar). Cloudflare puede marcar el túnel "en buen estado" aunque no llegue a Postiz: solo indica que el conector habla con Cloudflare. Si ya lo hiciste: `docker rm -f <contenedor>` y usa el servicio del compose. No dejes dos conectores con el mismo token a la vez.

Comprobar que el túnel llega a Postiz:

```bash
docker logs --tail 20 postiz-tunnel     # "Registered tunnel connection", sin "lookup postiz"
curl -I https://postiz.emprendeconvic.com/           # 307 (redirige al login)
```

Los avisos `failed to accept QUIC stream: timeout: no recent network activity` son cortes del protocolo QUIC en la red local; el conector se reconecta solo. Si causan 502 frecuentes, cambia el comando a `tunnel --no-autoupdate --protocol http2 run --token ${CF_TUNNEL_TOKEN}`.

**No uses `*.trycloudflare.com` ni `*.ngrok-free.app`**: Postiz guarda la cookie de sesión en el dominio raíz (`.trycloudflare.com`), los navegadores la rechazan porque es un dominio público compartido y el login se queda en un bucle. Además esas URLs cambian en cada reinicio. Con `emprendeconvic.com` la cookie queda en `.emprendeconvic.com`.

## 4. App de Meta

Configuración actual: app **Bot** (ID `1278757044404022`) en el portafolio **Emprende con Vic** (ID `913731568491266`), en modo desarrollo. Canales conectados en Postiz: **Facebook Page** *Vic.mercadeo* e **Instagram (Standalone)** *@vic.mercadeo*.

### 4.1 Crear la app

1. [developers.facebook.com/apps/creation](https://developers.facebook.com/apps/creation/) → selecciona el portafolio comercial.
2. Caso de uso: **"Administrar todo en tu página"** (*Manage everything on your Page*). Una app tipo *Consumer* o creada solo con el caso de uso de Login **no** puede pedir permisos de páginas y produce `Invalid Scopes`.
3. Copia el App ID y el App Secret (*Configuración de la app → Básica*) al `.env` como `FACEBOOK_APP_ID` y `FACEBOOK_APP_SECRET`.
4. Para Instagram, agrega el caso de uso **"Administrar mensajes y contenido en Instagram"** (§4.3).

### 4.2 Permisos (Casos de uso → Administrar todo en tu página → Personalizar → Agregar)

Postiz pide estos seis para Facebook:

| Permiso                     | Para qué                                   |
| --------------------------- | ------------------------------------------- |
| `pages_show_list`         | Listar las páginas del usuario             |
| `business_management`     | Acceder a páginas del portafolio comercial |
| `pages_manage_posts`      | Publicar en la página                      |
| `pages_manage_engagement` | Gestionar comentarios                       |
| `pages_read_engagement`   | Leer interacciones                          |
| `read_insights`           | Métricas                                   |

Y Meta exige además esta **dependencia**, aunque Postiz no la pida:

| Permiso                     | Motivo                                   |
| --------------------------- | ---------------------------------------- |
| `pages_read_user_content` | Requisito de `pages_manage_engagement` |

Si el diálogo de Facebook muestra `Invalid Scopes: <permiso>`, ese permiso falta en el caso de uso: agrégalo y espera 1–2 minutos.

### 4.3 Instagram

Postiz tiene dos conectores de Instagram. Usamos **Instagram (Standalone)**.

| Conector | Requisito | Permisos | Variables |
| --- | --- | --- | --- |
| **Instagram (Standalone)** (en uso) | Cuenta profesional (Empresa o Creador). **No** necesita página de Facebook | `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_insights` | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` |
| Instagram (Facebook Business) | Instagram profesional **vinculado a una página** de Facebook | `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights` | Las de Facebook |

Por qué Standalone: con @vic.mercadeo, vincular el Instagram a la página falló por todas las vías (configuración de la página, Business Suite y app de Instagram) con *"No se puede establecer el enlace en este momento"*, y sin ese vínculo el conector Facebook Business responde *"We couldn't find any business connected to the selected pages"*.

Configuración de Standalone:

1. *Casos de uso → Administrar mensajes y contenido en Instagram → Configuración de la API con inicio de sesión de Instagram*. Meta lo marca como *legacy*, pero es el flujo que usa Postiz (`instagram.com/oauth/authorize`).
2. Agrega los cuatro permisos `instagram_business_*`. No hace falta `instagram_business_manage_messages`.
3. Copia el **identificador** y la **clave secreta de la app de Instagram** (en esa misma pantalla, distintos de los de Facebook) al `.env`:
   ```
   INSTAGRAM_APP_ID=1687443243098173
   INSTAGRAM_APP_SECRET=<clave>
   ```
   El compose ya los pasa al contenedor. Tras cambiarlos: `docker compose up -d postiz`, y comprueba con `docker exec postiz printenv INSTAGRAM_APP_ID`.
4. En *Configurar inicio de sesión para empresas*, URI de redirección: `https://postiz.emprendeconvic.com/integrations/social/instagram-standalone`.
5. *Roles de la app → Evaluadores de Instagram* → agrega la cuenta. Acéptala desde Instagram web: *Configuración → Apps y sitios web → Invitaciones de evaluador*.
6. En Postiz: *Add Channel → Instagram (Standalone)* e inicia sesión con la cuenta de Instagram.

Si un cliente sí puede vincular su Instagram a la página, también sirve Facebook Business: agrega los permisos `instagram_*` en *Administrar todo en tu página* y la URI `…/integrations/social/instagram` (§4.4). Para vincular, el usuario necesita **control total en el portafolio dueño de la página**; si solo tiene acceso compartido, Meta responde *"Additional access is required to connect"*.

### 4.4 Dominios y redirección

- *Configuración de la app → Básica*:
  - **Dominios de la app:** `emprendeconvic.com` (Meta no acepta `localhost`).
  - **+ Agregar plataforma → Sitio web → URL del sitio:** `https://postiz.emprendeconvic.com/`
- *Facebook Login for Business → Configuración → Configuración del cliente de OAuth*:
  - Inicio de sesión del cliente de OAuth y de OAuth web: **Sí**. Aplicar HTTPS y modo estricto: **Sí**.
  - **URI de redireccionamiento de OAuth válidos** (exactas, sin `/` final):
    - `https://postiz.emprendeconvic.com/integrations/social/facebook`
    - `https://postiz.emprendeconvic.com/integrations/social/instagram` (solo para Facebook Business)
- *API de Instagram → Configurar inicio de sesión para empresas*: `https://postiz.emprendeconvic.com/integrations/social/instagram-standalone`

### 4.5 Quién puede conectar y publicar

- Mientras la app esté en **modo desarrollo** solo pueden conectar cuentas con rol en la app (admin, developer o tester) que además administren la página, y cuentas de Instagram agregadas como evaluadoras. Las publicaciones con imagen pueden verse solo para esas cuentas.
- La app pertenece al portafolio, así que los roles **no** se agregan desde developers.facebook.com (*"No se puede agregar un usuario con un rol en el negocio propietario de la app"*): en [business.facebook.com/settings](https://business.facebook.com/settings) → portafolio dueño → *Cuentas → Apps → Bot → Asignar personas* → control total. El portafolio dueño aparece como `business_id` en la URL del panel de la app.
- Para clientes reales: verificación del negocio → App Review (screencast por permiso, también los `instagram_business_*`) → Advanced Access → pasar la app a **Live**.
- El Instagram de cada cliente debe ser cuenta **profesional**. Las historias por API solo funcionan con cuentas de Empresa.

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

Luego `uv run social-agent integrations` lista los canales para el `brand.yaml` de cada cliente. La clave en `postiz_integrations` es el identificador que imprime (`facebook`, `instagram-standalone` o `instagram`):

```
instagram-standalone cmvu8hpyb0001p99v3pmu2uba  Víctor Gómez | Coach de Marketing Digital & Creador de Contenido
facebook             cmvu51wbt0003o58xdrptyrnu  Vic.mercadeo
```

Si responde **401**, la clave es de otra instalación o usuario de Postiz: genera una nueva en *Settings → Public API*.

## 6.1 Pinterest (pendiente)

El compose tiene `PINTEREST_CLIENT_ID` y `PINTEREST_CLIENT_SECRET` vacíos. Pinterest denegó el acceso de prueba a la app; cuando lo apruebe, cámbialos a `${PINTEREST_CLIENT_ID}` / `${PINTEREST_CLIENT_SECRET}` con las claves en `.env`, agrega en Pinterest la redirección `https://postiz.emprendeconvic.com/integrations/social/pinterest` y ejecuta `docker compose up -d postiz`. Detalles y qué corregir en la solicitud: [`../social-agent/README.md`](../social-agent/README.md#pinterest-pendiente).

## 7. Problemas que ya aparecieron

| Síntoma                                                             | Causa                                                                                 | Solución                                              |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `Invalid Scopes: pages_show_list, business_management, …`         | App sin el caso de uso de páginas (tipo Consumer o solo Login)                       | Caso de uso *Administrar todo en tu página* (§4.1) |
| `Invalid Scopes: pages_manage_engagement`                          | Permiso quitado del caso de uso                                                       | Agregarlo en *Personalizar*                          |
| `Invalid Scopes: pages_read_user_content`                          | Dependencia de `pages_manage_engagement`                                            | Agregarlo en *Personalizar*                          |
| "El dominio de esta URL no está incluido en los dominios de la app" | Falta dominio y plataforma Sitio web                                                  | §4.4                                                  |
| "Debe contener un dominio de nivel superior" al poner `localhost`  | Meta no acepta `localhost` como dominio                                             | Usar `emprendeconvic.com`                            |
| El login de Postiz no avanza                                         | Cookie rechazada en `*.trycloudflare.com`                                           | Túnel con dominio propio (§3)                        |
| 502 Bad Gateway · `lookup postiz on 192.168.65.7:53: no such host` | Túnel lanzado con `docker run` (fuera de `postiz-network`) o Postiz apagado        | `docker compose up -d`; servicio `cloudflared` del compose (§3) |
| 502 unos segundos tras arrancar                                      | Postiz aún cargando                                                                   | Esperar a `(healthy)`                                 |
| `failed to accept QUIC stream: timeout` en el túnel                  | Cortes de QUIC en la red local                                                        | Se reconecta solo; si hay 502 frecuentes, `--protocol http2` (§3) |
| `401 Unauthorized` en `/public/v1/integrations`                      | `POSTIZ_API_KEY` de otra instalación o usuario                                       | Nueva clave en *Settings → Public API* (§6)          |
| "We couldn't find any business connected to the selected pages"      | Conector Instagram (Facebook Business) sin Instagram vinculado a la página           | Instagram (Standalone) (§4.3)                         |
| "Additional access is required to connect"                           | Sin control total en el portafolio **dueño** de la página                            | Pedir control total en ese portafolio, o usar otra página / Standalone |
| "No se puede establecer el enlace en este momento"                   | Meta bloquea vincular ese Instagram a la página                                      | Instagram (Standalone) (§4.3)                         |
| "No se puede agregar un usuario con un rol en el negocio propietario de la app" | La app es de un portafolio                                                 | *Business Settings → Cuentas → Apps → Asignar personas* (§4.5) |
| Instagram (Standalone) no abre el login o falla al volver           | Faltan `INSTAGRAM_APP_ID`/`SECRET` en el contenedor, o la cuenta no es evaluadora    | §4.3, pasos 3 y 5                                     |
| "Formato de URL de servicio no válido" en Cloudflare                | Falta el protocolo                                                                    | `http://postiz:5000`                                 |
| `AccessDenied` al subir a R2                                       | Nombre del bucket mal puesto (se puso el dominio) o token sin *Object Read & Write* | §5                                                    |
| `no live upstreams … /auth/avatars/*.jpg` en los logs             | Imágenes decorativas del login                                                       | Ignorar                                                |

## Referencias

- Configuración de Postiz: https://docs.postiz.com/configuration/reference
- Facebook en Postiz: https://docs.postiz.com/providers/facebook
- Conectores de Instagram en el código de Postiz (permisos y redirecciones): [`instagram.standalone.provider.ts`](https://github.com/gitroomhq/postiz-app/blob/main/libraries/nestjs-libraries/src/integrations/social/instagram.standalone.provider.ts) y [`instagram.provider.ts`](https://github.com/gitroomhq/postiz-app/blob/main/libraries/nestjs-libraries/src/integrations/social/instagram.provider.ts)
- R2 en Postiz: https://docs.postiz.com/configuration/r2
- Migración desde versiones viejas de Postiz: https://docs.postiz.com/installation/migration
- Repositorio original del compose: https://github.com/gitroomhq/postiz-docker-compose
