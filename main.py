
import asyncio
import os
from typing import Any
import re
from difflib import SequenceMatcher

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from BotLogger import CRM_API_TOKEN, CRM_TENANT, CRM_URL, bot_log
from bot_productos import responder_productos
from Filtro_Mensajes import (
    _descargar_imagen_base64,
    analizar_intencion,
    filtrar_y_clasificar,
    procesar_contenido,
)
from Historial_Conversacion import formatear_historial_para_ia, obtener_historial

load_dotenv()

app = FastAPI(title="Bot Vitta4", version="1.0.0")

INCOMING_TOKEN = os.getenv("CRM_INCOMING_TOKEN")
RESPUESTA_PRUEBA = os.getenv(
    "BOT_RESPUESTA_PRUEBA",
    "¡Hola! Soy el Bot Vitta4. Tu mensaje llegó correctamente. 🤖",
)
# Segundos de espera antes de procesar (0 = sin debounce)
DEBOUNCE_SECS = int(os.getenv("BOT_DEBOUNCE_SECS", "20"))

# ── Estado de debounce (clave = "instancia:telefono") ────────────────────────
_pending_tasks: dict[str, asyncio.Task] = {}
_pending_data: dict[str, dict] = {}


# ── Helpers: consultar pautas activas en el CRM y comparar con publicación FB
async def _buscar_pautas_activas() -> list:
    """Consulta el módulo 'pautas' del CRM y retorna la lista de registros activos.
    Si no hay CRM configurado o falla la llamada, retorna lista vacía.
    """
    if not CRM_URL or not CRM_TENANT or not CRM_API_TOKEN:
        return []
    url = f"{CRM_URL}/api/v1/{CRM_TENANT}/pautas?per_page=200"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"X-API-Key": CRM_API_TOKEN})
            resp.raise_for_status()
            data = resp.json()
            records = []
            if isinstance(data, dict) and data.get("data") is not None:
                records = data.get("data") or []
            elif isinstance(data, list):
                records = data
    except Exception as e:
        print(f"[Pautas] error al consultar CRM: {e}")
        return []

    def _activo(r: dict) -> bool:
        v = None
        for k in ("status", "STATUS", "activo", "Activo", "Activo_Pauta", "estado"):
            if k in r:
                v = str(r[k]).lower()
                break
        if v is None:
            # intentar campo genérico
            v = str(r.get("status", r.get("STATUS", ""))).lower()
        return v in ("activo", "true", "1", "si", "sí", "enabled")

    return [r for r in (records or []) if _activo(r)]


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _score_pauta_match(pauta: dict, fb: dict) -> float:
    """Devuelve un puntaje (float) que indica cuán probable es que la pauta
    corresponda a la publicación de Facebook (`fb`)."""
    pauta_title = _normalize_text(
        pauta.get("NOMBRE_PAUTA") or pauta.get("nombre_pauta") or pauta.get("titulo") or pauta.get("name") or ""
    )
    pauta_msg = _normalize_text(pauta.get("MENSAJE") or pauta.get("mensaje") or pauta.get("descripcion") or pauta.get("message") or "")
    pauta_img = (pauta.get("IMAGEN_PAUTA") or pauta.get("imagen_pauta") or pauta.get("imagen") or "")

    fb_title = _normalize_text(fb.get("titulo") or fb.get("title") or "")
    fb_msg = _normalize_text(fb.get("descripcion") or fb.get("mensaje") or fb.get("resumen_para_bot") or "")
    fb_products = [p.lower() for p in (fb.get("productos_mencionados") or []) if p]
    fb_line = _normalize_text(fb.get("nombre_linea") or "")
    fb_img = fb.get("imagen") or ""

    score = 0.0
    # Productos en común (fuerte indicio)
    for prod in fb_products:
        if prod and (prod in pauta_msg or prod in pauta_title):
            score += 3.0

    # Línea coincidente
    if fb_line and (fb_line in pauta_msg or fb_line in pauta_title):
        score += 2.0

    # Similitud de título y mensaje (fuzzy)
    if fb_title and pauta_title:
        score += SequenceMatcher(None, fb_title, pauta_title).ratio()
    if fb_msg and pauta_msg:
        score += SequenceMatcher(None, fb_msg, pauta_msg).ratio()

    # Comparación simple de nombre de archivo de imagen (si existe)
    try:
        if fb_img and pauta_img and fb_img.split("/")[-1] == pauta_img.split("/")[-1]:
            score += 1.5
    except Exception:
        pass

    return float(score)


# ── Procesamiento y envío (llamado tras el debounce) ─────────────────────────

async def _procesar_y_enviar(data: dict) -> None:
    """Procesa el último mensaje acumulado y lo envía al usuario vía CRM /bot-send."""
    mensaje        = data.get("mensaje", "")
    tipo_contenido = data.get("tipo_contenido", "texto")
    telefono       = data.get("telefono", "desconocido")
    instancia      = data.get("instancia", "")
    url_media      = data.get("url_media", "")
    caption        = data.get("caption", "")
    titulo_fb      = data.get("titulo_fb", "")
    descripcion_fb = data.get("descripcion_fb", "")
    # Usar imagen ya descargada como base64 si está disponible (evita expiración de URL)
    thumbnail_url  = data.get("thumbnail_url_b64") or data.get("thumbnail_url", "")
    remote_jid     = data.get("remote_jid", "")
    alt_phones     = data.get("alt_phones", [])

    # ── Historial (usar cache si ya se obtuvo al recibir el mensaje) ──────────
    historial = data.get("_historial_cache")
    if historial is None:
        historial = await obtener_historial(telefono, alternativas=alt_phones)
    historial_texto = formatear_historial_para_ia(historial)
    if historial_texto:
        await bot_log(instancia, "info", "Historial", f"tel={telefono} turnos={len(historial)}")

    # ── Procesar contenido ────────────────────────────────────────────────────
    procesado = await procesar_contenido(
        tipo_contenido=tipo_contenido,
        mensaje=mensaje,
        url_media=url_media,
        caption=caption,
        titulo_fb=titulo_fb,
        descripcion_fb=descripcion_fb,
        thumbnail_url=thumbnail_url,
    )
    texto_para_bot = procesado["texto_procesado"]

    if historial_texto:
        texto_con_historial = f"{historial_texto}\nUsuario: {texto_para_bot}"
        intencion_con_ctx = await analizar_intencion(texto_con_historial)
        if intencion_con_ctx:
            procesado["intencion"] = intencion_con_ctx

    await bot_log(instancia, "info", "vitta4",
        f"texto_procesado={texto_para_bot[:100]!r} intencion={procesado.get('intencion', {}).get('intencion','?')}",
        {"etiqueta": procesado.get("etiqueta"), "intencion": procesado.get("intencion")})

    # ── Filtro ────────────────────────────────────────────────────────────────
    clasificacion = await filtrar_y_clasificar(
        mensaje=mensaje,
        tipo_original=tipo_contenido,
        url_publicidad=thumbnail_url,
        telefono=telefono,
        remote_jid=remote_jid or telefono,
        historial_texto=historial_texto,
    )
    await bot_log(instancia, "info", "Filtro",
        f"filtro_active={clasificacion['filtro_active']} pub_facebook={clasificacion['pub_facebook']} tipo_bloqueo={clasificacion.get('tipo_bloqueo')}")

    if clasificacion["filtro_active"]:
        tipo_bloqueo = clasificacion["tipo_bloqueo"] or "inapropiado"
        await bot_log(instancia, "warning", "Filtro",
            f"BLOQUEADO post-debounce ({tipo_bloqueo}) tel={telefono}")
        return

    # ── Corrección de tipo si filtro detectó Facebook ─────────────────────────
    if clasificacion["pub_facebook"] and tipo_contenido != "publicacion_facebook":
        tipo_contenido = "publicacion_facebook"
        procesado = await procesar_contenido(
            tipo_contenido="publicacion_facebook",
            mensaje=mensaje,
            url_media=url_media,
            caption=caption,
            titulo_fb=titulo_fb,
            descripcion_fb=descripcion_fb,
            thumbnail_url=thumbnail_url,
        )
        texto_para_bot = procesado["texto_procesado"]

    # ── Generar respuesta ─────────────────────────────────────────────────────
    intencion = procesado.get("intencion") or {}
    tipo_intencion = (intencion.get("intencion") or "").lower()
    es_flujo_negocio_puro = tipo_intencion == "negocio" and not historial_texto.strip()
    flujo = "negocio/genérico" if es_flujo_negocio_puro else "productos"
    from bot_productos import _contar_turnos_bot, PASO3_SIGNAL
    _turnos_bot = _contar_turnos_bot(historial_texto)
    _MARKER_PASO3 = "Estoy examinando tu situación"
    if not historial_texto.strip() or _turnos_bot == 0:
        paso_flujo = "PASO1"
    elif _turnos_bot == 1:
        paso_flujo = "PASO2"
    elif _MARKER_PASO3 not in historial_texto:
        paso_flujo = f"PASO3-entrevista (turnos_bot={_turnos_bot})"
    else:
        paso_flujo = f"PASO4-recomendacion (turnos_bot={_turnos_bot})"
    await bot_log(instancia, "info", "vitta4",
        f"tipo_intencion={tipo_intencion!r} historial={'sí' if historial_texto else 'no'} flujo={flujo} paso={paso_flujo}")

    # ── Antes de generar la respuesta: comprobar si la publicación viene de una pauta activa
    try:
        if (clasificacion.get("pub_facebook") or tipo_contenido == "publicacion_facebook"):
            pautas = await _buscar_pautas_activas()
            if pautas:
                fb_info = {
                    "titulo": titulo_fb,
                    "descripcion": descripcion_fb,
                    "productos_mencionados": (procesado.get("analisis") or {}).get("productos_mencionados") or (procesado.get("analisis") or {}).get("items") or [],
                    "nombre_linea": (procesado.get("analisis") or {}).get("nombre_linea") or "",
                    "imagen": thumbnail_url or url_media,
                }
                best = None
                best_score = 0.0
                for p in pautas:
                    try:
                        s = _score_pauta_match(p, fb_info)
                    except Exception:
                        s = 0.0
                    if s > best_score:
                        best_score = s
                        best = p

                # Umbral: si hay coincidencia razonable, usar el mensaje de la pauta
                if best and best_score >= 1.5:
                    matched_msg = best.get("MENSAJE") or best.get("mensaje") or best.get("message") or ""
                    if matched_msg:
                        texto_para_bot = matched_msg
                        procesado["texto_procesado"] = matched_msg
                        procesado.setdefault("analisis", {})["pauta_detectada"] = best
                        # Forzar intención si la pauta ya indica tipo
                        tipo_pauta = str(best.get("TIPO") or best.get("tipo") or "").lower()
                        if "negocio" in tipo_pauta or "afili" in tipo_pauta:
                            intencion = {"intencion": "negocio", "confianza": 0.95, "productos_mencionados": []}
                        elif "product" in tipo_pauta or "producto" in tipo_pauta:
                            intencion = {"intencion": "productos", "confianza": 0.95, "productos_mencionados": []}
                        else:
                            alt_int = await analizar_intencion(matched_msg) if matched_msg else None
                            if alt_int:
                                intencion = alt_int
                        await bot_log(instancia, "info", "Pautas", f"pauta_detectada titulo={best.get('NOMBRE_PAUTA') or best.get('nombre_pauta')} score={best_score}")
                else:
                    await bot_log(instancia, "info", "Pautas", f"no_match_con_pautas count={len(pautas)} best_score={best_score}")
    except Exception as e:
        await bot_log(instancia, "error", "Pautas", f"Error checando pautas: {e}")

    respuesta: str | None = None
    if not es_flujo_negocio_puro:
        respuesta = await responder_productos(
            texto_usuario=texto_para_bot,
            historial_texto=historial_texto,
            analisis=procesado.get("analisis") or {},
            intencion=intencion,
            instancia=instancia,
        )
    if not respuesta:
        respuesta = RESPUESTA_PRUEBA
        await bot_log(instancia, "warning", "vitta4", f"usando respuesta genérica tel={telefono}")

    # Detectar si PASO 3 terminó (GPT incluyó [[LISTO]])
    paso3_listo = PASO3_SIGNAL in respuesta
    if paso3_listo:
        respuesta = respuesta.replace(PASO3_SIGNAL, "").strip()
        await bot_log(instancia, "info", "Bot", f"PASO3 completado → enviando mensaje de transición tel={telefono}")

    await bot_log(instancia, "info", "Bot", f"respuesta generada ({len(respuesta)} chars) tel={telefono}")

    # ── Enviar al usuario vía CRM /bot-send ───────────────────────────────────
    async def _enviar(texto: str, user_msg: str = "") -> None:
        if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
            await bot_log(instancia, "warning", "BotSend", "CRM no configurado — respuesta no enviada")
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                    headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                    json={
                        "telefono":     telefono,
                        "remote_jid":   remote_jid,
                        "instancia":    instancia,
                        "respuesta":    texto,
                        "user_message": user_msg,
                    },
                )
                if resp.status_code not in (200, 201):
                    await bot_log(instancia, "error", "BotSend",
                        f"bot-send retornó {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            await bot_log(instancia, "error", "BotSend", f"Error en bot-send: {exc}")

    await _enviar(respuesta, procesado.get("etiqueta", mensaje[:120]))

    if paso3_listo:
        await _enviar("Estoy examinando tu situación para brindarte la mejor información 🔍")


async def _ejecutar_debounce(clave: str) -> None:
    """Tarea asyncio: decide tiempo de espera según historial y procesa el último mensaje."""
    data = _pending_data.get(clave)
    instancia = (data or {}).get("instancia", "")
    telefono  = (data or {}).get("telefono", "")
    alt_phones = (data or {}).get("alt_phones", [])

    # Verificar si hay historial para decidir el tiempo de espera
    historial_cache = await obtener_historial(telefono, alternativas=alt_phones)
    es_primer_mensaje = not historial_cache
    sleep_secs = 0 if es_primer_mensaje else DEBOUNCE_SECS

    if es_primer_mensaje:
        await bot_log(instancia, "info", "Debounce", f"primer mensaje → respuesta inmediata tel={telefono}")
    else:
        await bot_log(instancia, "info", "Debounce", f"historial detectado → esperando {sleep_secs}s tel={telefono}")

    try:
        await asyncio.sleep(sleep_secs)
    except asyncio.CancelledError:
        return  # cancelado por un mensaje más reciente

    data = _pending_data.pop(clave, None)
    _pending_tasks.pop(clave, None)
    if not data:
        return
    # Cachear el historial ya obtenido para no repetir la llamada al CRM
    data["_historial_cache"] = historial_cache
    await _procesar_y_enviar(data)


# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/vitta4")
async def vitta4(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    # Validar token entrante del CRM
    if INCOMING_TOKEN:
        request_token = body.get("api_token") or request.headers.get("X-API-Token")
        if request_token != INCOMING_TOKEN:
            raise HTTPException(status_code=401, detail="Token de acceso inválido.")

    mensaje        = body.get("mensaje", "")
    tipo_contenido = body.get("tipo_contenido", "texto")
    telefono       = body.get("telefono", "desconocido")
    instancia      = body.get("instancia", "")
    url_media      = body.get("url_media", "")
    caption        = body.get("caption", "")
    titulo_fb      = body.get("titulo_fb", "")
    descripcion_fb = body.get("descripcion_fb", "")
    thumbnail_url  = body.get("thumbnail_url", "")

    if not mensaje and tipo_contenido == "texto":
        raise HTTPException(status_code=400, detail="El campo 'mensaje' es obligatorio para tipo texto.")

    await bot_log(instancia, "info", "vitta4", f"tel={telefono} tipo={tipo_contenido} msg={mensaje[:80]!r}")

    # Extraer remoteJid del payload de Evolution
    evo_data        = body.get("evo", {}) or {}
    _remote_jid     = (evo_data.get("data", {}) or {}).get("key", {}).get("remoteJid", "") or ""
    _remote_jid_alt = (evo_data.get("data", {}) or {}).get("key", {}).get("remoteJidAlt", "") or ""

    def _strip_jid(jid: str) -> str:
        return jid.split("@")[0] if "@" in jid else jid

    _alt_phones: list[str] = []
    for jid in [_remote_jid, _remote_jid_alt]:
        num = _strip_jid(jid)
        if num and num != telefono:
            _alt_phones.append(num)

    # ── Debounce: acumular mensajes del mismo contacto y procesar todos juntos ─
    clave = f"{instancia}:{telefono}"

    existing = _pending_tasks.get(clave)
    if existing and not existing.done():
        # Acumular: agregar el nuevo mensaje al historial pendiente
        prev = _pending_data.get(clave, {})
        msgs_prev = prev.get("_mensajes_acumulados", [prev["mensaje"]] if prev.get("mensaje") else [])
        msgs_acum = [m for m in msgs_prev if m] + [mensaje]
        existing.cancel()
        await bot_log(instancia, "info", "Debounce",
            f"mensaje #{len(msgs_acum)} acumulado tel={telefono}: {mensaje[:60]!r}")
    else:
        msgs_acum = [mensaje]

    # Descargar imagen de FB a base64 inmediatamente (la URL de CDN expira pronto)
    thumbnail_url_b64 = None
    if thumbnail_url and (tipo_contenido == "publicacion_facebook" or titulo_fb or descripcion_fb):
        thumbnail_url_b64 = await _descargar_imagen_base64(thumbnail_url)
        if thumbnail_url_b64:
            await bot_log(instancia, "info", "Debounce", f"imagen FB descargada OK ({len(thumbnail_url_b64)} chars)")
        else:
            await bot_log(instancia, "warning", "Debounce", "imagen FB no descargable, se usará URL")

    _pending_data[clave] = {
        "mensaje":                "\n".join(msgs_acum),  # todos los mensajes combinados
        "_mensajes_acumulados":   msgs_acum,
        "tipo_contenido":    tipo_contenido,
        "telefono":          telefono,
        "instancia":         instancia,
        "url_media":         url_media,
        "caption":           caption,
        "titulo_fb":         titulo_fb,
        "descripcion_fb":    descripcion_fb,
        "thumbnail_url":     thumbnail_url,
        "thumbnail_url_b64": thumbnail_url_b64,  # base64 pre-descargado
        "remote_jid":        _remote_jid,
        "alt_phones":        _alt_phones,
    }
    _pending_tasks[clave] = asyncio.create_task(_ejecutar_debounce(clave))

    await bot_log(instancia, "info", "Debounce",
        f"en cola, procesará en {DEBOUNCE_SECS}s tel={telefono}")
    return {"debounced": True}