# Guía de Integración — API, n8n y Bots Externos

Esta plataforma actúa como el **cerebro central de IA y datos** para cualquier bot externo.
Puedes conectar n8n, bots en Laravel, Node.js, Python, o cualquier sistema que pueda hacer peticiones HTTP.

---

## Índice

1. [Autenticación](#1-autenticación)
2. [Encontrar tu tenant slug](#2-encontrar-tu-tenant-slug)
3. [Flujo general de mensajes](#3-flujo-general-de-mensajes)
4. [Integración con n8n](#4-integración-con-n8n)
5. [Integración con bots externos (Laravel, Node.js, etc.)](#5-integración-con-bots-externos)
   - [5.1 Registrar tu bot](#51-registrar-tu-bot)
   - [5.2 Modos de integración](#52-modos-de-integración)
   - [5.3 Modo A — IA completa (POST /ai/procesar)](#53-modo-a--ia-completa-post-aiprocesar)
   - [5.4 Selección de proveedor y modelo](#54-selección-de-proveedor-y-modelo)
   - [5.4 Modo C — IA + envío automático a WhatsApp](#54-modo-c--ia--envío-automático-a-whatsapp)
   - [5.5 Modo B — Solo envío a WhatsApp (POST /ai/enviar)](#55-modo-b--solo-envío-a-whatsapp-post-aienviar)
   - [5.6 Endpoint de información (GET /ai/info)](#56-endpoint-de-información-get-aiinfo)
   - [5.8 Ejemplo completo — Bot en Laravel](#58-ejemplo-completo--bot-en-laravel)
   - [5.9 Ejemplo completo — Bot en Node.js](#59-ejemplo-completo--bot-en-nodejs)
   - [5.10 Ejemplo completo — Bot en Python](#510-ejemplo-completo--bot-en-python)
6. [Referencia completa de endpoints](#6-referencia-completa-de-endpoints)
   - [6.1 IA — Endpoints de IA para bots externos](#61-ia--endpoints-de-ia-para-bots-externos)
   - [6.2 Catálogos — CRUD de datos](#62-catálogos--crud-de-datos)
   - [6.3 Conversaciones](#63-conversaciones)
   - [6.4 Números bloqueados / clasificación](#64-números-bloqueados--clasificación)
   - [6.5 Entrenamiento del bot](#65-entrenamiento-del-bot)
   - [6.6 Webhooks de Evolution (WhatsApp)](#66-webhooks-de-evolution-whatsapp)
7. [Respuestas de bloqueo y moderación](#7-respuestas-de-bloqueo-y-moderación)
8. [Errores comunes](#8-errores-comunes)

---

## 1. Autenticación

Esta plataforma usa **dos tipos de API Key** según el tipo de integración:

### 1.1 API Token Global — para n8n

Configúralo en:
> Configuración → n8n / API Global → API Token

Úsalo para autenticar las llamadas de n8n a esta plataforma:

```http
# Opción A — Cabecera HTTP (recomendada)
X-API-Key: tu-api-token-global

# Opción B — Query parameter
GET /api/v1/mi-negocio/catalogo/clientes?api_key=tu-api-token-global
```

### 1.2 API Key por Bot — para bots externos

Cada bot externo registrado recibe su **propia API Key** generada automáticamente por el sistema.

> Configuración → Bots Externos → (registrar bot y guardar) → copiar la API Key del bot

```http
# Úsala igual que el token global
X-API-Key: ext_a1b2c3d4e5f6...   # key única de ese bot
```

Ventajas:
- Cada bot tiene credenciales independientes  
- Puedes revocar o regenerar la key de un bot sin afectar a los demás  
- Si un bot es comprometido, solo debes regenerar su key

> Los endpoints marcados con 🔓 son **públicos** y no requieren token.
> Los marcados con 🔒 aceptan cualquiera de los dos tipos de key.

---

## 2. Encontrar tu tenant slug

El slug identifica tu tenant en todas las URLs. Para verificarlo:

```http
GET /api/v1/{tenant-slug}/info   🔓
```

Si el slug es correcto, responde:
```json
{
  "success": true,
  "tenant": { "slug": "mi-negocio", "nombre": "Mi Negocio S.A." },
  "modulos": [...],
  "api_base": "https://tudominio.com/api/v1/mi-negocio"
}
```

Si no sabes el slug, lo encuentras en el panel admin en la URL al entrar al tenant.

---

## 3. Flujo general de mensajes

```
Usuario de WhatsApp
       │  mensaje entrante
       ▼
Evolution API (webhook POST)
       │
       ▼
POST /webhook/whatsapp/{instancia}  (esta plataforma)
       │
       ├─► ¿Tiene n8n_webhook_url configurado?
       │       │ SÍ → llama a n8n y espera respuesta
       │       │ NO ↓
       │
       ├─► ¿Tiene bots externos activos?
       │       │ SÍ → llama a su webhook y espera respuesta
       │       │ NO ↓
       │
       └─► Lógica interna de IA / flujo de pasos
               │
               ▼
       Respuesta al usuario por WhatsApp (Evolution API)
```

> **n8n y los bots externos siempre tienen prioridad** sobre la IA interna.
> Si n8n o el bot externo responden, la IA interna no se ejecuta.

---

## 4. Integración con n8n

### 4.1 Configuración

En **Configuración → n8n / API Global** establece:
- **n8n Webhook URL** — la URL que n8n genera al crear un nodo Webhook
- **n8n Timeout** — segundos que esta plataforma espera la respuesta (default: 8)

### 4.2 Payload que esta plataforma envía a n8n

Cuando llega un mensaje de WhatsApp, esta plataforma hace:

```http
POST {tu-n8n-webhook-url}
Content-Type: application/json
```

```json
{
  "telefono":  "5215512345678",
  "instancia": "nombre-instancia-evolution",
  "mensaje":   "Hola, ¿cuánto cuesta el producto X?",
  "tenant":    "mi-negocio",
  "api_token": "tu-api-token",
  "timestamp": "2026-04-02T10:30:00+00:00",
  "evo": { ... }
}
```

| Campo       | Descripción                                                        |
|-------------|--------------------------------------------------------------------|
| `telefono`  | Número del usuario sin `@s.whatsapp.net`                           |
| `instancia` | Nombre de la instancia de Evolution API que recibió el mensaje     |
| `mensaje`   | Texto del mensaje del usuario (ya transcrito si era audio)         |
| `tenant`    | Slug del tenant para usar en llamadas de vuelta a la API           |
| `api_token` | Token para que n8n pueda llamar a los endpoints de esta plataforma |
| `evo`       | Payload raw completo de Evolution API (event + data + metadata)    |

### 4.3 Respuesta que n8n debe devolver

n8n debe responder con JSON en **menos del timeout configurado**. Formatos aceptados:

#### Respuesta normal (el bot envía texto al usuario)
```json
{ "respuesta": "Nuestros horarios son de lunes a viernes de 9am a 6pm." }
```

También se aceptan las claves alternativas `"response"` o `"message"`.

#### Respuesta con medios (imagen, video, audio, documento)
```json
{
  "respuesta": "Aquí te mando el catálogo:",
  "medios": [
    {
      "tipo":     "image",
      "url":      "https://tuservidor.com/catalogo.jpg",
      "caption":  "Catálogo 2026"
    },
    {
      "tipo":     "document",
      "url":      "https://tuservidor.com/precio.pdf",
      "fileName": "lista-de-precios.pdf"
    }
  ]
}
```

| `tipo`     | Descripción                                    |
|------------|------------------------------------------------|
| `image`    | Imagen JPG/PNG                                 |
| `video`    | Video MP4                                      |
| `audio`    | Audio MP3/OGG                                  |
| `document` | PDF u otro archivo — requiere `fileName`       |
| `sticker`  | Sticker WebP                                   |

#### Respuesta de moderación (bloquear o pausar usuario)
```json
{
  "tipo_bloqueo": "inapropiado",
  "motivo": "El usuario envió contenido ofensivo"
}
```

| `tipo_bloqueo`      | Efecto                                              |
|---------------------|-----------------------------------------------------|
| `inapropiado`       | Bloquea al número en Evolution + marca como bloqueado |
| `prompt_injection`  | Igual que `inapropiado`                             |
| `irrelevante`       | Solo pausa el bot sin bloquear el contacto          |

#### n8n en modo asíncrono
Si n8n devuelve `{"message": "Workflow was started"}`, esta plataforma lo ignora y continúa con su flujo normal de IA interna.

### 4.4 Cómo usar los datos del tenant desde n8n

Dentro del workflow de n8n puedes llamar a todos los endpoints de esta plataforma usando el `api_token` y el `tenant` que recibiste:

```
# Buscar al usuario en un módulo de clientes
GET https://tudominio.com/api/v1/{{ $json.tenant }}/clientes?telefono={{ $json.telefono }}&find_one=1
Headers: { "X-API-Key": "{{ $json.api_token }}" }
```

```
# Crear un registro
POST https://tudominio.com/api/v1/{{ $json.tenant }}/clientes
Headers: { "X-API-Key": "{{ $json.api_token }}", "Content-Type": "application/json" }
Body: { "nombre": "...", "telefono": "{{ $json.telefono }}" }
```

### 4.5 Bloquear un número desde n8n (alternativa)

En lugar de responder con `tipo_bloqueo` en el webhook, puedes llamar directamente:

```http
POST /api/v1/{tenant}/blocked_numbers
X-API-Key: tu-token
Content-Type: application/json
```
```json
{
  "Numero_Baneado": "5214444416578",
  "Numero_Remote":  "521444416578@s.whatsapp.net",
  "Motivo_Bloqueo": "Spam detectado por LLM",
  "tipo_bloqueo":   "inapropiado"
}
```

---

## 5. Integración con bots externos

Este es el modo donde **tu bot tiene su propio código** (Laravel, Node.js, Python, etc.)
y usa esta plataforma como **cerebro de IA y/o canal de entrega de WhatsApp**.

### 5.1 Registrar tu bot

Ve a **Configuración → Bots Externos** y registra tu bot con:
- **Nombre** — identificador legible
- **Framework / Tecnología** — Laravel, Node.js, Python, etc.
- **Webhook de notificación** (opcional) — URL donde esta plataforma notificará eventos a tu bot
- **Timeout** — segundos de espera para la respuesta de tu bot (3–60)

Después de guardar, el sistema **genera automáticamente una API Key única** para ese bot.  
Cópiala desde el panel de Bots Externos — la necesitarás para autenticar las llamadas de tu bot.

> Puedes **regenerar la key** en cualquier momento desde el panel. La key anterior deja de funcionar inmediatamente.

### 5.2 Modos de integración

| Modo | Endpoint | Descripción |
|------|----------|-------------|
| **A** — IA completa | `POST /ai/procesar` | Tu bot envía el mensaje → el sistema llama a la IA → devuelve la respuesta |
| **B** — Solo envío WhatsApp | `POST /ai/enviar` | Tu bot genera su propia respuesta IA → el sistema la envía por WhatsApp |
| **C** — IA + envío automático | `POST /ai/procesar` + `enviar_whatsapp: true` | Todo en una sola llamada |

Con el **Modo A/C** tu bot no necesita API keys de OpenAI, DeepSeek ni Gemini: usa las del sistema.
Con el **Modo B** tu bot usa sus propias claves de IA, pero encamina el envío de WhatsApp por aquí.

---

### 5.3 Modo A — IA completa (`POST /ai/procesar`)

```http
POST /api/v1/{tenant}/ai/procesar   🔒
X-API-Key: ext_a1b2c3...  # API Key propia del bot (generada al registrar el bot)
Content-Type: application/json
```

```json
{
  "mensaje":      "¿Cuáles son sus horarios de atención?",
  "telefono":     "5215512345678",
  "historial": [
    { "role": "user",      "content": "Hola" },
    { "role": "assistant", "content": "¡Hola! ¿En qué te puedo ayudar?" },
    { "role": "user",      "content": "¿Tienen envíos a Monterrey?" },
    { "role": "assistant", "content": "Sí, enviamos a toda la república." }
  ],
  "system_prompt": "Eres un asistente de atención al cliente de Empresa XYZ...",
  "contexto":      "El cliente tiene un pedido pendiente #4521 de $350."
}
```

| Campo           | Tipo   | Req | Descripción |
|-----------------|--------|-----|-------------|
| `mensaje`       | string | ✅  | Mensaje del usuario (máx. 4000 chars) |
| `telefono`      | string | —   | Teléfono del usuario (para logs) |
| `historial`     | array  | —   | Historial de mensajes. Máx. 50 pares `{ role, content }` |
| `system_prompt` | string | —   | Prompt del sistema. Si se omite, usa el configurado en el panel |
| `contexto`      | string | —   | Información adicional para la IA (datos del CRM, pedidos, etc.) |
#### Respuesta exitosa

```json
{
  "success":   true,
  "respuesta": "Nuestros horarios son de lunes a viernes de 9am a 6pm.",
  "enviado":   false
}
```

#### Respuesta de error

```json
{
  "success": false,
  "error":   "El webhook no pudo generar una respuesta. Verifica que esté configurado y disponible."
}
```

---

### 5.4 Modo C — IA + envío automático a WhatsApp

Agrega `enviar_whatsapp`, `instancia` y `remote_jid` al body de `/ai/procesar`
para que el sistema genere la respuesta **y** la envíe a WhatsApp en una sola llamada:

```json
{
  "mensaje":          "¿Cuándo llega mi pedido?",
  "telefono":         "5215512345678",
  "instancia":        "mi-instancia-evolution",
  "remote_jid":       "5215512345678",
  "enviar_whatsapp":  true
}
```

| Campo              | Tipo    | Req | Descripción |
|--------------------|---------|-----|-------------|
| `enviar_whatsapp`  | boolean | ✅  | Activa el envío automático |
| `instancia`        | string  | ✅  | Nombre de la instancia en Evolution API |
| `remote_jid`       | string  | ✅  | Número destino (`5215512345678` o `5215512345678@s.whatsapp.net`) |

La respuesta incluye `"enviado": true` si el envío fue exitoso:

```json
{
  "success":   true,
  "respuesta": "Su pedido llega mañana antes de las 2pm.",
  "proveedor": "openai",
  "modelo":    "gpt-4o-mini",
  "enviado":   true
}
```

---

### 5.5 Modo B — Solo envío a WhatsApp (`POST /ai/enviar`)

Cuando tu bot genera la respuesta con sus propias claves de IA,
usa este endpoint para que el sistema la envíe por WhatsApp con sus credenciales de Evolution.

```http
POST /api/v1/{tenant}/ai/enviar   🔒
X-API-Key: tu-token
Content-Type: application/json
```

#### Enviar texto

```json
{
  "instancia":  "mi-instancia-evolution",
  "remote_jid": "5215512345678",
  "texto":      "Hola, tu pedido está listo para recoger."
}
```

#### Enviar texto + medios

```json
{
  "instancia":  "mi-instancia-evolution",
  "remote_jid": "5215512345678",
  "texto":      "Aquí tienes el comprobante de pago:",
  "medios": [
    {
      "tipo":     "document",
      "url":      "https://tuservidor.com/comprobante-4521.pdf",
      "fileName": "comprobante-4521.pdf"
    },
    {
      "tipo":    "image",
      "url":     "https://tuservidor.com/logo.png",
      "caption": "Logo de la empresa"
    }
  ]
}
```

| Campo            | Tipo   | Req | Descripción |
|------------------|--------|-----|-------------|
| `instancia`      | string | ✅  | Nombre de la instancia en Evolution API |
| `remote_jid`     | string | ✅  | Número destino (con o sin `@s.whatsapp.net`) |
| `texto`          | string | —   | Mensaje de texto (máx. 4000 chars) |
| `medios`         | array  | —   | Lista de archivos adjuntos (máx. 10) |
| `medios[].tipo`  | string | ✅  | `image` \| `video` \| `audio` \| `document` \| `sticker` |
| `medios[].url`   | string | ✅  | URL pública del archivo |
| `medios[].caption` | string | — | Texto sobre la imagen/video |
| `medios[].fileName` | string | — | Nombre del archivo (requerido para `document`) |

> Se requiere al menos `texto` o un elemento en `medios`.

#### Respuesta

```json
{
  "success": true,
  "resultados": [
    { "tipo": "texto",    "enviado": true },
    { "tipo": "document", "url": "https://...", "enviado": true }
  ],
  "instancia":  "mi-instancia-evolution",
  "remote_jid": "5215512345678@s.whatsapp.net"
}
```

Si alguna parte falla, el HTTP status es `207 Multi-Status` y `"success": false`
pero el array `resultados` muestra qué partes se enviaron y cuáles no.

---

### 5.6 Endpoint de información (`GET /ai/info`)

```http
GET /api/v1/{tenant}/ai/info   🔓
```

```json
{
  "success":             true,
  "tenant":              "mi-negocio",
  "webhook_configurado": true,
  "whatsapp_disponible": true,
  "bots_registrados":    2,
  "endpoints": {
    "procesar": "https://tudominio.com/api/v1/mi-negocio/ai/procesar",
    "enviar":   "https://tudominio.com/api/v1/mi-negocio/ai/enviar"
  }
}
```

Útil para verificar conectividad y confirmar que el sistema está operativo.

### 5.7 Ejemplo completo — Bot en Laravel

```php
<?php
// En tu bot de Laravel — controlador que recibe el mensaje del usuario

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;

class WhatsAppBotController extends Controller
{
    private string $cerebroUrl;
    private string $cerebroToken;
    private string $cerebroTenant;

    public function __construct()
    {
        $this->cerebroUrl    = config('services.cerebro.url');    // https://tudominio.com
        $this->cerebroToken  = config('services.cerebro.token');  // tu-api-token
        $this->cerebroTenant = config('services.cerebro.tenant'); // mi-negocio
    }

    public function recibirMensaje(Request $request)
    {
        $telefono = $request->input('telefono');
        $mensaje  = $request->input('mensaje');

        // 1. (Opcional) Recuperar historial de conversación de tu BD
        $historial = $this->obtenerHistorial($telefono);

        // 2. (Opcional) Obtener contexto relevante del CRM
        $contexto = $this->obtenerContextoCliente($telefono);

        // 3. Llamar al cerebro de IA
        $response = Http::withHeaders([
                'X-API-Key'    => $this->cerebroToken,
                'Content-Type' => 'application/json',
            ])
            ->timeout(30)
            ->post("{$this->cerebroUrl}/api/v1/{$this->cerebroTenant}/ai/procesar", [
                'mensaje'   => $mensaje,
                'telefono'  => $telefono,
                'historial' => $historial,
                'contexto'  => $contexto,
            ]);

        if (!$response->ok()) {
            // Fallback: respuesta genérica o manejo de error
            return response()->json(['error' => 'IA no disponible'], 503);
        }

        $respuestaIA = $response->json('respuesta');

        // 4. Guardar en tu historial local
        $this->guardarHistorial($telefono, $mensaje, $respuestaIA);

        // 5. Enviar la respuesta al usuario (vía tu canal preferido)
        $this->enviarWhatsApp($telefono, $respuestaIA);

        return response()->json(['status' => 'ok']);
    }

    private function obtenerHistorial(string $telefono): array
    {
        // Tus últimas N conversaciones formateadas como pares user/assistant
        return \App\Models\MensajeLocal::where('telefono', $telefono)
            ->latest()
            ->take(10)
            ->get()
            ->reverse()
            ->flatMap(fn($m) => [
                ['role' => 'user',      'content' => $m->mensaje_usuario],
                ['role' => 'assistant', 'content' => $m->respuesta_bot],
            ])
            ->values()
            ->toArray();
    }

    private function obtenerContextoCliente(string $telefono): string
    {
        // También puedes consultar directamente el catálogo de esta plataforma:
        $cliente = Http::withHeaders(['X-API-Key' => $this->cerebroToken])
            ->get("{$this->cerebroUrl}/api/v1/{$this->cerebroTenant}/clientes", [
                'telefono' => $telefono,
                'find_one' => 1,
            ])->json('data');

        if ($cliente) {
            return "Cliente registrado: {$cliente['nombre']}. Estado: {$cliente['estado']}.";
        }

        return '';
    }
}
```

#### `config/services.php` en tu bot externo

```php
'cerebro' => [
    'url'    => env('CEREBRO_URL',    'https://tudominio.com'),
    'token'  => env('CEREBRO_TOKEN'),
    'tenant' => env('CEREBRO_TENANT', 'mi-negocio'),
],
```

#### `.env` en tu bot externo

```env
CEREBRO_URL=https://tudominio.com
CEREBRO_TOKEN=tu-api-token-secreto
CEREBRO_TENANT=mi-negocio
```

### 5.9 Ejemplo completo — Bot en Node.js (Express)

```javascript
const axios = require('axios');

const CEREBRO = {
  url:    process.env.CEREBRO_URL,
  token:  process.env.CEREBRO_TOKEN,
  tenant: process.env.CEREBRO_TENANT,
};

async function procesarMensaje(telefono, mensaje, historial = []) {
  try {
    const { data } = await axios.post(
      `${CEREBRO.url}/api/v1/${CEREBRO.tenant}/ai/procesar`,
      { mensaje, telefono, historial },
      {
        headers: {
          'X-API-Key':    CEREBRO.token,
          'Content-Type': 'application/json',
        },
        timeout: 30000,
      }
    );

    if (data.success) {
      return data.respuesta;
    }
    throw new Error(data.error);

  } catch (err) {
    console.error('[Cerebro] Error:', err.message);
    return null; // Tu bot maneja el fallback
  }
}

// Uso en tu webhook de WhatsApp
app.post('/webhook', async (req, res) => {
  const { telefono, mensaje } = req.body;
  const respuesta = await procesarMensaje(telefono, mensaje);

  if (respuesta) {
    await enviarWhatsApp(telefono, respuesta);
  }

  res.json({ status: 'ok' });
});
```

### 5.10 Ejemplo completo — Bot en Python

```python
import requests
import os

CEREBRO_URL    = os.getenv("CEREBRO_URL")
CEREBRO_TOKEN  = os.getenv("CEREBRO_TOKEN")
CEREBRO_TENANT = os.getenv("CEREBRO_TENANT")

def procesar_mensaje(mensaje: str, telefono: str, historial: list = None) -> str | None:
    """Envía un mensaje al motor de IA y devuelve la respuesta."""
    try:
        response = requests.post(
            f"{CEREBRO_URL}/api/v1/{CEREBRO_TENANT}/ai/procesar",
            json={
                "mensaje":   mensaje,
                "telefono":  telefono,
                "historial": historial or [],
            },
            headers={"X-API-Key": CEREBRO_TOKEN},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            return data["respuesta"]

    except requests.RequestException as e:
        print(f"[Cerebro] Error: {e}")

    return None
```

---

## 6. Referencia completa de endpoints

> **URL base:** `https://tudominio.com/api/v1/{tenant}`

### 6.1 IA — Endpoints de IA para bots externos

| Método | Endpoint       | Auth | Descripción |
|--------|----------------|------|-------------|
| GET    | `/ai/info`     | 🔓   | Info del proveedor activo y proveedores disponibles |
| POST   | `/ai/procesar` | 🔒   | **Modo A/C** — Genera respuesta IA (opcionalmente la envía por WhatsApp) |
| POST   | `/ai/enviar`   | 🔒   | **Modo B** — Envía texto y/o medios a WhatsApp usando las credenciales del sistema |

#### Campos clave de `/ai/procesar`

| Campo             | Descripción |
|-------------------|-------------|
| `mensaje`         | Requerido. Mensaje del usuario. |
| `historial`       | Opcional. Array de `{ role, content }`. |
| `system_prompt`   | Opcional. Sobreescribe el prompt del sistema. |
| `contexto`        | Opcional. Datos adicionales para la IA. |
| `proveedor`       | Opcional. `openai` \| `deepseek` \| `gemini`. Default: el configurado. |
| `modelo`          | Opcional. Sobreescribe el modelo del proveedor. |
| `enviar_whatsapp` | Opcional. `true` activa el Modo C (envío automático). |
| `instancia`       | Requerido con `enviar_whatsapp: true`. |
| `remote_jid`      | Requerido con `enviar_whatsapp: true`. |

#### Campos clave de `/ai/enviar`

| Campo          | Descripción |
|----------------|-------------|
| `instancia`    | Requerido. Instancia de Evolution API. |
| `remote_jid`   | Requerido. Número destino (con o sin `@s.whatsapp.net`). |
| `texto`        | Opcional. Texto a enviar. |
| `medios`       | Opcional. Array de `{ tipo, url, caption?, fileName? }`. |

---

### 6.2 Catálogos — CRUD de datos

Estos endpoints permiten leer y escribir en los módulos (tablas) que defines en el panel admin.

| Método | Endpoint              | Auth | Descripción                                      |
|--------|-----------------------|------|--------------------------------------------------|
| GET    | `/info`               | 🔓   | Info del tenant y módulos disponibles             |
| GET    | `/modulos`            | 🔒   | Lista módulos con definición de campos            |
| GET    | `/buscar?search=txt`  | 🔒   | Búsqueda libre en TODOS los módulos a la vez      |
| GET    | `/{module}`           | 🔒   | Listar / filtrar registros de un módulo           |
| GET    | `/{module}/{id}`      | 🔒   | Obtener un registro por ID                        |
| POST   | `/{module}`           | 🔒   | Crear un registro nuevo                           |
| PUT    | `/{module}/{id}`      | 🔒   | Reemplazar un registro completo                   |
| PATCH  | `/{module}/{id}`      | 🔒   | Actualizar campos específicos (por ID)            |
| PATCH  | `/{module}`           | 🔒   | Actualizar por campo (sin ID, útil en n8n)        |
| DELETE | `/{module}/{id}`      | 🔒   | Eliminar un registro                              |

#### Query params disponibles en `GET /{module}`

| Parámetro               | Descripción                                                    |
|-------------------------|----------------------------------------------------------------|
| `?search=texto`         | Búsqueda libre en todos los campos del módulo                  |
| `?{campo}=valor`        | Filtro exacto por cualquier campo (ej: `?telefono=5215512345678`) |
| `?find_one=1`           | Devuelve un objeto único — ideal cuando buscas un registro específico |
| `?per_page=25`          | Registros por página (máximo 200, default 25)                  |
| `?page=2`               | Número de página                                               |
| `?sort=campo&order=asc` | Ordenar por campo (asc / desc)                                 |

#### Ejemplos de catálogos

```http
# Buscar un cliente por teléfono (ideal en n8n o tu bot)
GET /api/v1/mi-negocio/clientes?telefono=5215512345678&find_one=1
X-API-Key: tu-token

# Crear un cliente nuevo
POST /api/v1/mi-negocio/clientes
X-API-Key: tu-token
Content-Type: application/json

{ "nombre": "Juan Pérez", "telefono": "5215512345678", "email": "juan@mail.com" }

# Actualizar solo el estado de un cliente (sin conocer el ID)
PATCH /api/v1/mi-negocio/clientes
X-API-Key: tu-token
Content-Type: application/json

{
  "buscar_por":   "telefono",
  "buscar_valor": "5215512345678",
  "estado":       "cliente_activo"
}
```

---

### 6.3 Conversaciones

Permite consultar y actualizar el historial de conversaciones registradas en la plataforma.

| Método | Endpoint                              | Auth | Descripción                          |
|--------|---------------------------------------|------|--------------------------------------|
| GET    | `/conversaciones`                     | 🔒   | Listar / filtrar conversaciones       |
| GET    | `/conversaciones/{id}`                | 🔒   | Obtener una conversación por ID       |
| PATCH  | `/conversaciones/{id}`                | 🔒   | Actualizar campos de una conversación |
| GET    | `/conversaciones/phone/{phone}`       | 🔒   | Últimas conversaciones de un teléfono |

#### Filtros en `GET /conversaciones`

| Parámetro      | Descripción                                               |
|----------------|-----------------------------------------------------------|
| `?phone=`      | Filtra por número de teléfono exacto                      |
| `?instancia=`  | Filtra por instancia de Evolution API                     |
| `?status=`     | `ok` / `bloqueado` / `pausado` / `ok_n8n`                 |
| `?search=`     | Búsqueda en mensaje del usuario y respuesta del bot       |
| `?desde=`      | Fecha mínima `YYYY-MM-DD`                                 |
| `?hasta=`      | Fecha máxima `YYYY-MM-DD`                                 |
| `?per_page=`   | Registros por página (máx 200, default 50)                |
| `?find_one=1`  | Devuelve objeto único                                     |

#### Ejemplo — obtener historial de un contacto desde n8n

```http
GET /api/v1/mi-negocio/conversaciones/phone/5215512345678?per_page=10
X-API-Key: tu-token
```

Respuesta:
```json
{
  "success": true,
  "data": [
    {
      "id": 42,
      "phone": "5215512345678",
      "instancia": "mi-instancia",
      "user_message": "Hola, ¿tienen envíos?",
      "bot_response": "Sí, enviamos a toda la república.",
      "status": "ok",
      "created_at": "2026-04-02T10:30:00.000000Z"
    }
  ],
  "meta": { "total": 8, "per_page": 10, "current_page": 1 }
}
```

---

### 6.4 Números bloqueados / clasificación

Endpoint para que n8n (u otro sistema externo) reporte mensajes inapropiados o irrelevantes.

```http
POST /api/v1/{tenant}/blocked_numbers   🔒
X-API-Key: tu-token
Content-Type: application/json
```

```json
{
  "Numero_Baneado": "5214444416578",
  "Numero_Remote":  "521444416578@s.whatsapp.net",
  "Motivo_Bloqueo": "Intento de prompt injection detectado",
  "tipo_bloqueo":   "prompt_injection"
}
```

| `tipo_bloqueo`     | Efecto                                                                   |
|--------------------|--------------------------------------------------------------------------|
| `inapropiado`      | Bloquea número en Evolution API + registra en BD + marca conversación    |
| `prompt_injection` | Igual que `inapropiado`                                                  |
| `irrelevante`      | Pausa el bot (no responde más) sin bloquear el contacto en WhatsApp      |

#### Campo `etiqueta` (solo para `tipo_bloqueo: irrelevante`)

Cuando el tipo es `irrelevante` puedes incluir el campo opcional `etiqueta` para clasificar
la pausa a nivel sistema. El CRM la guarda tal cual en la columna `etiqueta` de
`irrelevant_conversations` — no la interpreta ni la procesa.

```json
{
  "Numero_Baneado": "5214444416578",
  "Numero_Remote":  "521444416578@s.whatsapp.net",
  "Motivo_Bloqueo": "rechazo_videos",
  "tipo_bloqueo":   "irrelevante",
  "instancia":      "mi-instancia",
  "etiqueta":       "Cierre"
}
```

> **Diseño multi-bot:** el CRM no define ni conoce los nombres de etiquetas. Es el bot
> quien decide qué etiqueta enviar según su propio contexto. Esto permite conectar
> cualquier bot al CRM sin tener que configurar etiquetas en el panel.
>
> **En Bot_vitta4** las etiquetas se configuran con las variables de entorno:
> - `BOT_ETIQUETA_CIERRE` (default `Cierre`) — para motivos de rechazo/post-venta
> - `BOT_ETIQUETA_PAUSA` (default `Pausa`) — para motivos sin catálogo u otros

---

### 6.5 Entrenamiento del bot

Permite que n8n o un bot externo **aproveche las respuestas reales de agentes humanos** para mejorar las respuestas automáticas del bot. El flujo es:

1. El sistema captura automáticamente pares `pregunta → respuesta humana` cuando un agente responde mientras el bot está apagado/pausado.
2. Desde la API puedes **leer** esos pares aprobados para usarlos como contexto RAG, o **registrar** nuevos pares desde tu propio bot/n8n.
3. Un humano aprueba o rechaza los pares desde el panel `/bot/entrenamiento`.
4. Los pares aprobados se pueden exportar en formato JSONL para hacer fine-tuning en OpenAI.

| Método | Endpoint                    | Auth | Descripción |
|--------|-----------------------------|------|-------------|
| GET    | `/entrenamiento`            | 🔒   | Lista todos los pares pregunta→respuesta aprobados |
| GET    | `/entrenamiento/buscar`     | 🔒   | Busca los pares más relevantes para una consulta (para RAG) |
| POST   | `/entrenamiento`            | 🔒   | Registra un par nuevo (queda pendiente de revisión humana) |

---

#### GET `/entrenamiento` — Lista pares aprobados

```http
GET /api/v1/mi-negocio/entrenamiento
X-API-Key: tu-token
```

Query params opcionales:

| Parámetro    | Descripción                                              |
|--------------|----------------------------------------------------------|
| `?limit=100` | Máximo de resultados (1–200, default 100)                |
| `?instancia=` | Filtrar por instancia de WhatsApp                       |

Respuesta:
```json
{
  "success": true,
  "total": 42,
  "pares": [
    {
      "id": 7,
      "pregunta":  "¿Tienen envíos a Monterrey?",
      "respuesta": "Sí, enviamos a toda la república en 3-5 días hábiles.",
      "instancia": "mi-instancia",
      "fuente":    "bot_apagado",
      "created_at": "2026-04-06T14:22:00.000000Z"
    }
  ]
}
```

---

#### GET `/entrenamiento/buscar` — Búsqueda semántica por palabras clave

Ideal para usar **antes de llamar a `/ai/procesar`**: busca ejemplos reales que se parezcan a la pregunta actual y se los pasas como `contexto` a la IA.

```http
GET /api/v1/mi-negocio/entrenamiento/buscar?q=envios+monterrey&limit=3
X-API-Key: tu-token
```

| Parámetro    | Descripción                                              |
|--------------|----------------------------------------------------------|
| `?q=`        | **Requerido.** Texto de la pregunta del usuario          |
| `?limit=5`   | Número de resultados (1–20, default 5)                   |
| `?instancia=` | Filtrar por instancia                                   |

Respuesta:
```json
{
  "success": true,
  "consulta": "envios monterrey",
  "resultados": 2,
  "pares": [
    {
      "id": 7,
      "pregunta":  "¿Tienen envíos a Monterrey?",
      "respuesta": "Sí, enviamos a toda la república en 3-5 días hábiles.",
      "fuente": "bot_apagado"
    }
  ]
}
```

##### Ejemplo de uso en n8n (flujo RAG)

```
Mensaje recibido del usuario
       │
       ▼
HTTP Request — GET /entrenamiento/buscar?q={mensaje}&limit=3
       │
       ▼  pares encontrados
Set Node — construir contexto:
  "Ejemplos de respuestas anteriores:\n"
  + para cada par: "P: {pregunta}\nR: {respuesta}\n"
       │
       ▼
HTTP Request — POST /ai/procesar
  { "mensaje": "...", "contexto": "<contexto construido>" }
       │
       ▼
Respuesta enviada al usuario
```

---

#### POST `/entrenamiento` — Registrar un par nuevo

Registra pares directamente desde tu bot externo o n8n. El par queda en estado **pendiente** hasta que un humano lo apruebe desde el panel.

```http
POST /api/v1/mi-negocio/entrenamiento
X-API-Key: tu-token
Content-Type: application/json
```

```json
{
  "pregunta":  "¿Cuánto tarda el envío a CDMX?",
  "respuesta": "Los envíos a CDMX tardan 1-2 días hábiles.",
  "instancia": "mi-instancia",
  "phone":     "5215512345678",
  "notas":     "Confirmado por agente Sofía el 2026-04-06"
}
```

| Campo       | Tipo   | Descripción                                              |
|-------------|--------|----------------------------------------------------------|
| `pregunta`  | string | **Requerido.** Texto del cliente (máx 4000 chars)        |
| `respuesta` | string | **Requerido.** Respuesta del agente (máx 4000 chars)     |
| `instancia` | string | Opcional. Instancia de WhatsApp de origen                |
| `phone`     | string | Opcional. Número del cliente                             |
| `notas`     | string | Opcional. Contexto adicional para el revisor             |

Respuesta `201`:
```json
{
  "success": true,
  "id": 43,
  "estado":  "pendiente_revision",
  "mensaje": "Par de entrenamiento registrado. Pendiente de aprobación en el panel."
}
```

---

#### Ciclo completo de mejora continua

```
┌─────────────────────────────────────────────────────────────────┐
│  Bot apagado / pausado → agente humano responde                 │
│        ↓  (captura automática vía webhook)                      │
│  Tabla conversaciones_entrenamiento_bot  [pendiente]            │
│        ↓                                                        │
│  Panel /bot/entrenamiento  →  humano aprueba/rechaza            │
│        ↓                                                        │
│  GET /entrenamiento         → bot externo / n8n usa los pares   │
│  GET /entrenamiento/buscar  → RAG en tiempo real                │
│        ↓                                                        │
│  Exportar .jsonl  →  Fine-tune en OpenAI  →  actualizar modelo  │
└─────────────────────────────────────────────────────────────────┘
```

---

### 6.6 Webhooks de Evolution (WhatsApp)

Estos endpoints son los que configuras **en Evolution API** como destino de los eventos de WhatsApp. No los llamas tú directamente.

```
POST /webhook/whatsapp/{instancia}
POST /webhook/bloqueo/{instancia}
```

Donde `{instancia}` es el nombre exacto de la instancia en Evolution API.

---

## 7. Respuestas de bloqueo y moderación

### Flujo recomendado con n8n (filtro LLM)

```
Mensaje del usuario
       │
       ▼
n8n — Nodo HTTP Request (recibe el payload de esta plataforma)
       │
       ▼
n8n — Nodo LLM / OpenAI (clasifica el mensaje)
       │
       ├─► "relevante"       → responde con  { "respuesta": "..." }
       ├─► "inapropiado"     → responde con  { "tipo_bloqueo": "inapropiado", "motivo": "..." }
       ├─► "prompt_injection"→ responde con  { "tipo_bloqueo": "prompt_injection", "motivo": "..." }
       └─► "irrelevante"     → responde con  { "tipo_bloqueo": "irrelevante", "motivo": "..." }
```

> `irrelevante` es para mensajes fuera del alcance del bot (ej: alguien pidiendo delivery
> cuando el bot es de seguros). Pausa el bot para ese contacto sin bloquearlo.
> Un agente humano puede reanudar el bot desde el panel.

---

## 8. Errores comunes

| Código HTTP | Causa                                  | Solución                                                     |
|-------------|----------------------------------------|--------------------------------------------------------------|
| `401`       | Token inválido o ausente               | Verifica `X-API-Key` o `?api_key=` en la petición           |
| `404`       | Tenant slug incorrecto                 | Consulta `GET /api/v1/{slug}/info` para verificar el slug    |
| `422`       | Validación fallida                     | Revisa el body JSON — revisa los campos requeridos           |
| `503`       | API Token no configurado en la BD      | Ve a Configuración → n8n / API Global y define el token      |
| `503`       | La IA no pudo generar respuesta        | Verifica que el proveedor de IA (OpenAI, etc.) esté configurado |
| `504`       | Timeout al esperar n8n                 | Aumenta el timeout en Configuración o revisa tu workflow     |

### Timeout de n8n

Si n8n tarda más del timeout configurado, esta plataforma no espera y continúa con su flujo interno de IA. Para integraciones críticas, mantén el timeout en **15-20 segundos** y optimiza tu workflow de n8n.

### n8n en modo "respuesta inmediata"

Asegúrate de que tu nodo Respond to Webhook en n8n esté configurado en **"Using 'Respond to Webhook' node"** (no en "When last node finishes"). De lo contrario, n8n devuelve `{"message":"Workflow was started"}` de forma asíncrona y esta plataforma lo ignora.

---

## Resumen rápido de configuración

| Qué configurar                | Dónde                                    | Afecta a |
|-------------------------------|------------------------------------------|----------|
| API Token (n8n / global)      | Configuración → n8n / API Global → API Token   | Autenticación para n8n y llamadas globales      |
| API Key por bot               | Configuración → Bots Externos → API Key del bot | Autenticación exclusiva de cada bot externo     |
| n8n Webhook URL               | Configuración → n8n / API Global → n8n URL     | Reenvío de mensajes entrantes a n8n             |
| n8n Timeout                   | Configuración → n8n / API Global → Timeout     | Segundos de espera antes de fallback            |
| Proveedor de IA               | Configuración → Conectar APIs            | Proveedor por defecto en `/ai/procesar` |
| API Key de OpenAI/DeepSeek/Gemini | Configuración → Conectar APIs        | Habilita ese proveedor en `/ai/info` y `/ai/procesar` |
| Evolution URL y API Key       | Configuración → Evolution API            | Habilita `/ai/enviar` y Modo C de `/ai/procesar` |
| System Prompt                 | Configuración → Bot                      | Respuestas IA cuando no se envía `system_prompt` |
| Bots externos registrados     | Configuración → Bots Externos            | Registro informativo de integraciones |
