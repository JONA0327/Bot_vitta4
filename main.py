"""
main.py — Bot 4Life Vitta
─────────────────────────
Flujo completo:
  1. Recibe mensaje vía webhook (POST /mensaje)
  2. Valida token de autenticación (comparación en tiempo constante)
  3. Debounce: acumula mensajes del mismo contacto, espera BOT_DEBOUNCE_SECS
  4. Filtra: texto / imagen / audio / Facebook → producto | irrelevante | inapropiado
  5. Si relevante: obtiene historial CRM + genera respuesta adaptativa
  6. Envía respuesta via CRM /bot-send

Variables de entorno (.env):
  BOT_INCOMING_TOKEN  — token de validación entrante (vacío = sin validación)
  CRM_URL             — URL base del CRM (ej: https://mi-crm.com)
  CRM_TENANT          — slug del tenant en el CRM
  CRM_API_TOKEN       — token X-API-Key para el CRM
  OPENAI_API_KEY      — clave de OpenAI
  BOT_NOMBRE          — nombre del bot (default: Valeria)
  BOT_DEBOUNCE_SECS   — segundos de espera antes de procesar (default: 5)
  CRM_TIMEOUT         — timeout para llamadas al CRM en segundos (default: 8)
  OPENAI_TIMEOUT      — timeout para llamadas a OpenAI en segundos (default: 15)
  WHISPER_TIMEOUT     — timeout para transcripción de audio en segundos (default: 60)
"""

import asyncio
import os
import secrets

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator

load_dotenv()

import responder as _responder_module
from filtro import clasificar_mensaje
from historial import obtener_historial

# ── Config ────────────────────────────────────────────────────────────────────
INCOMING_TOKEN = os.getenv("BOT_INCOMING_TOKEN", "")
CRM_URL        = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT     = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN  = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT    = float(os.getenv("CRM_TIMEOUT", "8"))
DEBOUNCE_SECS  = float(os.getenv("BOT_DEBOUNCE_SECS", "5"))

# ── Debounce state ────────────────────────────────────────────────────────────
_pending_tasks: dict[str, asyncio.Task] = {}
_pending_data:  dict[str, dict]         = {}

app = FastAPI(title="Bot 4Life Vitta", version="1.0.0")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    print("=" * 55, flush=True)
    print("  Bot 4Life Vitta — verificación de conexiones", flush=True)
    print("=" * 55, flush=True)

    # ── OpenAI API Key ────────────────────────────────────────
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        print("[OpenAI]  ❌  OPENAI_API_KEY no configurada", flush=True)
    else:
        # Verify the key is valid with a lightweight models list call
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {openai_key}"},
                )
            if r.status_code == 200:
                print(f"[OpenAI]  ✅  API key válida  (···{openai_key[-6:]})", flush=True)
            elif r.status_code == 401:
                print(f"[OpenAI]  ❌  API key INVÁLIDA — verifica OPENAI_API_KEY  (···{openai_key[-6:]})", flush=True)
            else:
                print(f"[OpenAI]  ⚠️  Respuesta inesperada HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"[OpenAI]  ⚠️  No se pudo verificar: {e}", flush=True)

    # ── CRM conexión y token ──────────────────────────────────
    crm_url   = os.getenv("CRM_URL", "")
    crm_ten   = os.getenv("CRM_TENANT", "")
    crm_tok   = os.getenv("CRM_API_TOKEN", "")
    if not crm_url or not crm_ten or not crm_tok:
        print("[CRM]     ❌  CRM_URL / CRM_TENANT / CRM_API_TOKEN incompletos", flush=True)
    else:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{crm_url.rstrip('/')}/api/v1/{crm_ten}/info",
                )
            if r.status_code == 200:
                nombre = r.json().get("nombre") or crm_ten
                print(f"[CRM]     ✅  Conectado → tenant: {nombre}  ({crm_url})", flush=True)
            elif r.status_code == 404:
                print(f"[CRM]     ❌  Tenant '{crm_ten}' no encontrado en {crm_url}", flush=True)
            else:
                print(f"[CRM]     ⚠️  HTTP {r.status_code} al consultar info del tenant", flush=True)
        except Exception as e:
            print(f"[CRM]     ❌  No se pudo conectar a {crm_url}: {e}", flush=True)

        # Verify API token with an authenticated call
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{crm_url.rstrip('/')}/api/v1/{crm_ten}/entrenamiento",
                    headers={"X-API-Key": crm_tok},
                    params={"limit": 1},
                )
            if r.status_code == 200:
                print(f"[CRM]     ✅  Token API válido  (···{crm_tok[-6:]})", flush=True)
            elif r.status_code == 401:
                print(f"[CRM]     ❌  Token API INVÁLIDO — verifica CRM_API_TOKEN  (···{crm_tok[-6:]})", flush=True)
            else:
                print(f"[CRM]     ⚠️  Token: respuesta HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"[CRM]     ⚠️  No se pudo verificar token: {e}", flush=True)

    # ── Incoming token ────────────────────────────────────────
    if INCOMING_TOKEN:
        print(f"[Webhook] ✅  Token entrante configurado  (···{INCOMING_TOKEN[-4:]})", flush=True)
    else:
        print("[Webhook] ⚠️  BOT_INCOMING_TOKEN no configurado — endpoint abierto sin autenticación", flush=True)

    # ── Bot config ────────────────────────────────────────────
    bot_nombre = os.getenv("BOT_NOMBRE", "Valeria")
    print(f"[Config]  ℹ️  Nombre del bot: {bot_nombre}  |  Debounce: {DEBOUNCE_SECS}s", flush=True)

    print("-" * 55, flush=True)

    # ── CRM Rules ─────────────────────────────────────────────
    reglas = await _responder_module._cargar_reglas_crm()
    if reglas:
        _responder_module._REGLAS_ACTIVAS = reglas
        n = len(reglas.get("restricciones_globales") or [])
        print(f"[Reglas]  ✅  Cargadas — {n} restricciones globales", flush=True)
    else:
        print("[Reglas]  ℹ️  Sin reglas configuradas — usando prompts base", flush=True)

    # ── Product cache pre-warm ────────────────────────────────
    productos = await _responder_module._obtener_productos()
    if productos:
        lineas = productos.count("•")
        print(f"[Productos] ✅  Catálogo cargado — {lineas} producto(s)/paquete(s)", flush=True)
    else:
        print("[Productos] ⚠️  Catálogo vacío o CRM sin módulo de productos", flush=True)

    print("=" * 55, flush=True)


# ── Request model ─────────────────────────────────────────────────────────────

class MensajePayload(BaseModel):
    token:          str = ""
    mensaje:        str = ""
    tipo_contenido: str = "texto"
    telefono:       str
    instancia:      str
    remote_jid:     str = ""
    url_media:      str = ""
    caption:        str = ""
    titulo_fb:      str = ""
    descripcion_fb: str = ""
    thumbnail_url:  str = ""
    contact_name:   str = ""
    # Evolution API credentials — enviados por CRM cuando descargarMedia falla
    evo_url:        str = ""
    evo_key:        str = ""
    msg_key:        dict = {}
    msg_message:    dict = {}

    @field_validator("telefono", "instancia")
    @classmethod
    def _no_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("No puede estar vacío")
        return v.strip()


# ── Token validation ──────────────────────────────────────────────────────────

def _validar_token(token_body: str, token_header: str) -> bool:
    """Constant-time comparison prevents timing-based token enumeration."""
    if not INCOMING_TOKEN:
        return True  # No token configured → open endpoint
    candidato = (token_header or token_body or "").strip()
    if not candidato:
        return False
    return secrets.compare_digest(
        candidato.encode("utf-8"),
        INCOMING_TOKEN.encode("utf-8"),
    )


# ── CRM helpers ───────────────────────────────────────────────────────────────

async def _enviar_respuesta(
    telefono: str,
    remote_jid: str,
    instancia: str,
    respuesta: str,
    mensaje_usuario: str,
    contact_name: str,
    medios: list | None = None,
) -> None:
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
        print("[Send] CRM no configurado — respuesta no enviada", flush=True)
        return
    print(
        f"[Send] → CRM /bot-send  tel={telefono}  inst={instancia}  "
        f"resp_len={len(respuesta)}  medios={len(medios or [])}  preview='{respuesta[:60].strip()}'",
        flush=True,
    )
    try:
        payload: dict = {
            "telefono":     telefono,
            "remote_jid":   remote_jid,
            "instancia":    instancia,
            "respuesta":    respuesta,
            "user_message": mensaje_usuario,
            "contact_name": contact_name or None,
        }
        if medios:
            payload["medios"] = medios
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            r = await client.post(
                f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                headers={"X-API-Key": CRM_API_TOKEN},
                json=payload,
            )
        print(
            f"[Send] ✅ CRM respondió HTTP {r.status_code} — "
            f"Evolution enviará el msg al contacto",
            flush=True,
        )
    except Exception as e:
        print(f"[Send] ❌ error al enviar al CRM: {e}", flush=True)


async def _bloquear_contacto(
    telefono: str,
    remote_jid: str,
    instancia: str,
    motivo: str,
) -> None:
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
        return
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            await client.post(
                f"{CRM_URL}/api/v1/{CRM_TENANT}/blocked_numbers",
                headers={"X-API-Key": CRM_API_TOKEN},
                json={
                    "numero_baneado": telefono,
                    "numero_remote":  remote_jid,
                    "instancia":      instancia,
                    "tipo_bloqueo":   "inapropiado",
                    "etiqueta":       "Bloqueado automático",
                    "motivo_bloqueo": motivo[:200],
                },
            )
        print(f"[Bloqueo] {telefono} bloqueado — {motivo[:80]}", flush=True)
    except Exception as e:
        print(f"[Bloqueo] error: {e}", flush=True)


# ── Processing pipeline ───────────────────────────────────────────────────────

async def _procesar_mensaje(datos: dict) -> None:
    telefono     = datos["telefono"]
    instancia    = datos["instancia"]
    remote_jid   = datos.get("remote_jid", "")
    tipo         = datos.get("tipo_contenido", "texto")
    mensaje      = datos.get("mensaje", "")
    url_media    = datos.get("url_media", "")
    caption      = datos.get("caption", "")
    titulo_fb    = datos.get("titulo_fb", "")
    desc_fb      = datos.get("descripcion_fb", "")
    thumb_url    = datos.get("thumbnail_url", "")
    contact_name = datos.get("contact_name", "")
    evo_url      = datos.get("evo_url", "")
    evo_key      = datos.get("evo_key", "")
    msg_key      = datos.get("msg_key") or {}
    msg_message  = datos.get("msg_message") or {}

    # 1. Filter message
    filtro = await clasificar_mensaje(
        mensaje=mensaje,
        tipo_contenido=tipo,
        url_media=url_media,
        caption=caption,
        titulo_fb=titulo_fb,
        descripcion_fb=desc_fb,
        thumbnail_url=thumb_url,
        evo_url=evo_url,
        evo_key=evo_key,
        evo_instancia=instancia,
        msg_key=msg_key if msg_key else None,
        msg_message=msg_message if msg_message else None,
    )

    clasificacion = filtro.get("clasificacion", "producto")
    print(
        f"[Filtro] {telefono} tipo={tipo} → {clasificacion} "
        f"({filtro.get('descripcion', '')[:80]})",
        flush=True,
    )

    if clasificacion == "inapropiado":
        await _bloquear_contacto(telefono, remote_jid, instancia, filtro.get("descripcion", ""))
        return

    if clasificacion == "irrelevante":
        return  # Silent — do not respond

    # 2. Get conversation history
    historial = await obtener_historial(telefono, limit=15)
    es_primer_mensaje = len(historial) == 0
    print(
        f"[Historial] {telefono} → {len(historial)} turno(s) en CRM",
        flush=True,
    )

    # 3. Resolve effective message text
    # For audio: use Whisper transcript
    # For image: use AI description (includes what was seen) + caption if provided
    # For text: original message
    tipo_detectado = filtro.get("tipo_detectado", "texto")
    if tipo_detectado == "imagen":
        descripcion_imagen = filtro.get("descripcion", "").strip()
        texto_imagen = descripcion_imagen
        if caption:
            texto_imagen = f"{caption}\n[Imagen: {descripcion_imagen}]" if descripcion_imagen else caption
        mensaje_efectivo = texto_imagen or mensaje or ""
    else:
        mensaje_efectivo = (
            filtro.get("transcripcion")       # audio transcript
            or caption
            or mensaje
            or ""
        ).strip()

    if not mensaje_efectivo:
        return

    # 4. Generate adaptive response
    productos_detectados = filtro.get("productos_detectados") or []
    print(f"[GPT] {telefono} generando respuesta…", flush=True)
    resultado = await _responder_module.generar_respuesta(
        mensaje=mensaje_efectivo,
        historial_crm=historial,
        telefono=telefono,
        instancia=instancia,
        contact_name=contact_name,
        productos_detectados=productos_detectados,
        es_primer_mensaje=es_primer_mensaje,
    )

    # Handle both old str return (safety) and new dict return
    if isinstance(resultado, str):
        respuesta = resultado
        medios: list = []
        pause = False
        human_escalate = False
    else:
        respuesta      = resultado.get("respuesta", "")
        medios         = resultado.get("medios", [])
        pause          = resultado.get("pause", False)
        human_escalate = resultado.get("human_escalate", False)

    if not respuesta:
        print(f"[GPT] {telefono} ❌ respuesta vacía — no se envía", flush=True)
        return

    print(
        f"[GPT] {telefono} ✅ respuesta ({len(respuesta)} chars) pause={pause} "
        f"human_escalate={human_escalate} medios={len(medios)} — "
        f"'{respuesta[:60].strip()}'…",
        flush=True,
    )

    # 5. Send response via CRM → Evolution API
    await _enviar_respuesta(
        telefono, remote_jid, instancia, respuesta, mensaje_efectivo, contact_name, medios or None,
    )


async def _procesar_con_delay(clave: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SECS)
    except asyncio.CancelledError:
        return
    datos = _pending_data.pop(clave, None)
    _pending_tasks.pop(clave, None)
    if datos:
        await _procesar_mensaje(datos)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "bot": "4Life Vitta"}


@app.post("/vitta4")
async def recibir_mensaje(payload: MensajePayload, request: Request) -> dict:
    # Constant-time token validation
    token_header = request.headers.get("X-Bot-Token", "")
    if not _validar_token(payload.token, token_header):
        raise HTTPException(status_code=401, detail="Token inválido")

    clave = f"{payload.instancia}:{payload.telefono}"

    # Debounce: cancel any pending task for this contact
    task = _pending_tasks.get(clave)
    if task and not task.done():
        task.cancel()

    if clave in _pending_data:
        # Accumulate text from rapid successive messages
        prev_msg  = _pending_data[clave].get("mensaje", "")
        nuevo_msg = payload.mensaje.strip()
        if prev_msg and nuevo_msg:
            _pending_data[clave]["mensaje"] = f"{prev_msg}\n{nuevo_msg}"
        elif nuevo_msg:
            _pending_data[clave]["mensaje"] = nuevo_msg
        # If this message carries media, prefer it (e.g. image sent after caption text)
        if payload.url_media:
            _pending_data[clave]["url_media"]      = payload.url_media
            _pending_data[clave]["tipo_contenido"] = payload.tipo_contenido
            _pending_data[clave]["caption"]        = payload.caption
            if payload.evo_url:
                _pending_data[clave]["evo_url"]     = payload.evo_url
                _pending_data[clave]["evo_key"]     = payload.evo_key
                _pending_data[clave]["msg_key"]     = payload.msg_key
                _pending_data[clave]["msg_message"] = payload.msg_message
    else:
        _pending_data[clave] = {
            "telefono":       payload.telefono,
            "instancia":      payload.instancia,
            "remote_jid":     payload.remote_jid,
            "tipo_contenido": payload.tipo_contenido,
            "mensaje":        payload.mensaje,
            "url_media":      payload.url_media,
            "caption":        payload.caption,
            "titulo_fb":      payload.titulo_fb,
            "descripcion_fb": payload.descripcion_fb,
            "thumbnail_url":  payload.thumbnail_url,
            "contact_name":   payload.contact_name,
            "evo_url":        payload.evo_url,
            "evo_key":        payload.evo_key,
            "msg_key":        payload.msg_key,
            "msg_message":    payload.msg_message,
        }

    _pending_tasks[clave] = asyncio.create_task(_procesar_con_delay(clave))
    return {"ok": True, "queued": clave, "debounced": True}
