
import asyncio
import os
from typing import Any
import re
from difflib import SequenceMatcher

import httpx
from dotenv import load_dotenv

# Cargar .env ANTES de importar otros módulos propios para que sus os.getenv()
# de nivel de módulo (CRM_URL, CRM_TENANT, CRM_API_TOKEN, etc.) reciban los valores.
load_dotenv()

from fastapi import FastAPI, HTTPException, Request

from BotLogger import CRM_API_TOKEN, CRM_TENANT, CRM_URL, bot_log
from bot_productos import responder_productos
from Filtro_Mensajes import (
    _descargar_imagen_base64,
    analizar_conversacion_entrenamiento,
    analizar_intencion,
    filtrar_y_clasificar,
    procesar_contenido,
)
from Historial_Conversacion import formatear_historial_para_ia, obtener_historial

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
async def _buscar_pautas_activas(instancia: str = "") -> list:
    """Consulta el módulo 'pautas' del CRM y retorna la lista de registros activos.
    Si no hay CRM configurado o falla la llamada, retorna lista vacía.
    """
    # Fallback de runtime: si los módulos se cargaron antes del load_dotenv
    _url    = CRM_URL    or os.getenv("CRM_URL",    "").rstrip("/")
    _tenant = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _token  = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not _url or not _tenant or not _token:
        return []
    url = f"{_url}/api/v1/{_tenant}/pautas?per_page=200"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"X-API-Key": _token})
            resp.raise_for_status()
            data = resp.json()
            records = []
            if isinstance(data, dict) and data.get("data") is not None:
                records = data.get("data") or []
            elif isinstance(data, list):
                records = data
    except Exception as e:
        print(f"[Pautas] error al consultar CRM: {e}")
        await bot_log(instancia, "error", "Pautas", f"error al consultar CRM: {e}")
        return []

    def _record_datos(r: dict) -> dict:
        """Unwrap the 'datos' layer from a CatalogRecord API response."""
        if isinstance(r.get("datos"), dict):
            return r["datos"]
        return r

    def _activo(r: dict) -> bool:
        d = _record_datos(r)
        v = None
        for k in ("STATUS", "status", "activo", "Activo", "Activo_Pauta", "estado"):
            if k in d:
                v = str(d[k]).lower()
                break
        if v is None:
            v = ""
        return v in ("activo", "activa", "true", "1", "si", "sí", "enabled")

    return [r for r in (records or []) if _activo(r)]


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _pauta_datos(pauta: dict) -> dict:
    """Unwrap the 'datos' layer from a CatalogRecord API response."""
    if isinstance(pauta.get("datos"), dict):
        return pauta["datos"]
    return pauta


def _word_overlap(a: str, b: str) -> float:
    """Fraction of words in the shorter string that appear in the longer string."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb:
        return 0.0
    shorter = wa if len(wa) <= len(wb) else wb
    longer  = wa | wb  # union — word must appear in either
    common  = wa & wb
    return len(common) / len(shorter)


def _contains_any_order(text: str, phrase: str) -> bool:
    """True if ALL words of phrase appear in text (order-independent)."""
    words = phrase.split()
    return bool(words) and all(w in text for w in words)


def _score_pauta_match(pauta: dict, fb: dict) -> float:
    """Devuelve un puntaje (float) que indica cuán probable es que la pauta
    corresponda a la publicación de Facebook (`fb`)."""
    d = _pauta_datos(pauta)
    pauta_title = _normalize_text(
        d.get("NOMBRE_PAUTA") or d.get("nombre_pauta") or d.get("titulo") or d.get("name") or ""
    )
    pauta_msg = _normalize_text(d.get("MENSAJE") or d.get("mensaje") or d.get("descripcion") or d.get("message") or "")
    pauta_img = (d.get("IMAGEN_PAUTA") or d.get("imagen_pauta") or d.get("imagen") or "")
    pauta_full = f"{pauta_title} {pauta_msg}"

    fb_title = _normalize_text(fb.get("titulo") or fb.get("title") or "")
    fb_msg = _normalize_text(fb.get("descripcion") or fb.get("mensaje") or fb.get("resumen_para_bot") or "")
    fb_products = [_normalize_text(p) for p in (fb.get("productos_mencionados") or []) if p]
    fb_line = _normalize_text(fb.get("nombre_linea") or "")
    fb_img = fb.get("imagen") or ""
    fb_full = f"{fb_title} {fb_msg} {fb_line} {' '.join(fb_products)}"

    score = 0.0

    # ── Productos en común (fuerte indicio) — orden-independiente ─────────────
    for prod in fb_products:
        if prod:
            if _contains_any_order(pauta_full, prod) or _contains_any_order(prod, pauta_title):
                score += 3.0
            elif _word_overlap(prod, pauta_title) >= 0.5:
                score += 2.0

    # ── Línea de producto coincidente — orden-independiente ───────────────────
    if fb_line:
        if _contains_any_order(pauta_full, fb_line) or _contains_any_order(fb_line, pauta_title):
            score += 2.0
        else:
            overlap = _word_overlap(fb_line, pauta_title)
            if overlap >= 0.5:
                score += 2.0 * overlap  # proporcional

    # ── Similitud fuzzy de título y mensaje ───────────────────────────────────
    if fb_title and pauta_title:
        score += SequenceMatcher(None, fb_title, pauta_title).ratio()
    if fb_msg and pauta_msg:
        score += SequenceMatcher(None, fb_msg, pauta_msg).ratio()

    # ── Overlap general de palabras entre toda la info FB y la pauta ──────────
    global_overlap = _word_overlap(fb_full, pauta_full)
    if global_overlap >= 0.3:
        score += global_overlap

    # ── Comparación de nombre de archivo de imagen ────────────────────────────
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
    _respuesta_pauta_directa: str | None = None
    _medios_pauta_directa: list | None = None
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

    # ── Antes de generar la respuesta: comprobar si hay pauta activa en el CRM
    # Se ejecuta tanto para publicaciones de FB como para el primer contacto (historial vacío)
    try:
        es_fb = clasificacion.get("pub_facebook") or tipo_contenido == "publicacion_facebook"
        es_primer_contacto = not historial_texto.strip()
        if es_fb or es_primer_contacto:
            pautas = await _buscar_pautas_activas(instancia)
            await bot_log(instancia, "info", "Pautas",
                f"consultando pautas activas count={len(pautas)} es_fb={bool(es_fb)} es_primer_contacto={es_primer_contacto}")
            if pautas:
                analisis_data = procesado.get("analisis") or {}
                # Para FB usamos los campos de la publicación.
                # Para mensajes no-FB usamos el texto del usuario como "descripcion" para el scoring,
                # lo que permite comparar contra el contenido/mensaje de las pautas.
                fb_info = {
                    "titulo": titulo_fb or "",
                    "descripcion": descripcion_fb or texto_para_bot,   # ← clave: fallback al texto del usuario
                    "productos_mencionados": analisis_data.get("productos_mencionados") or analisis_data.get("items") or [],
                    "nombre_linea": analisis_data.get("nombre_linea") or "",
                    "imagen": thumbnail_url or url_media,
                }

                # Calcular score para TODAS las pautas
                scores: list[tuple[float, dict]] = []
                for p in pautas:
                    try:
                        s = _score_pauta_match(p, fb_info)
                    except Exception:
                        s = 0.0
                    scores.append((s, p))
                scores.sort(key=lambda x: x[0], reverse=True)

                best_score, best = scores[0] if scores else (0.0, None)
                second_score = scores[1][0] if len(scores) > 1 else 0.0

                await bot_log(instancia, "info", "Pautas",
                    f"scores top2: {best_score:.2f} / {second_score:.2f} "
                    f"mejor={(_pauta_datos(best).get('NOMBRE_PAUTA') or _pauta_datos(best).get('nombre_pauta')) if best else None!r}")

                # Umbrales de decisión:
                #   FB            : score >= 1.0 (coincidencia semántica por palabras clave)
                #   no-FB 1 pauta : score >= 0   (no hay otra opción; se usa la única activa)
                #   no-FB >1 pauta: score >= 1.0 Y debe superar a la segunda en al menos 0.5
                #                   (evita usar una pauta equivocada cuando hay varias activas)
                usar_pauta = False
                if best:
                    if es_fb:
                        usar_pauta = best_score >= 1.0
                    elif len(pautas) == 1:
                        usar_pauta = True  # única pauta activa → se usa siempre en primer contacto
                    else:
                        usar_pauta = best_score >= 1.0 and (best_score - second_score) >= 0.5

                if usar_pauta and best:
                    _bd = _pauta_datos(best)
                    matched_msg = _bd.get("MENSAJE") or _bd.get("mensaje") or _bd.get("message") or ""
                    if matched_msg:
                        procesado.setdefault("analisis", {})["pauta_detectada"] = best
                        procesado.setdefault("analisis", {})["mensaje_pauta"] = matched_msg
                        tipo_pauta = str(_bd.get("TIPO") or _bd.get("tipo") or "").lower()
                        if "negocio" in tipo_pauta or "afili" in tipo_pauta:
                            intencion = {"intencion": "negocio", "confianza": 0.95, "productos_mencionados": []}
                        elif "product" in tipo_pauta or "producto" in tipo_pauta:
                            intencion = {"intencion": "productos", "confianza": 0.95, "productos_mencionados": []}
                        else:
                            alt_int = await analizar_intencion(matched_msg) if matched_msg else None
                            if alt_int:
                                intencion = alt_int
                        await bot_log(instancia, "info", "Pautas",
                            f"pauta_USADA nombre={_bd.get('NOMBRE_PAUTA') or _bd.get('nombre_pauta')!r} "
                            f"score={best_score:.2f} modo={'fb' if es_fb else 'primer_contacto'}")
                        # Marcar la respuesta directa de la pauta para enviarla sin LLM
                        pauta_imagen = _bd.get("IMAGEN_PAUTA") or _bd.get("imagen_pauta") or _bd.get("imagen") or ""
                        _respuesta_pauta_directa = matched_msg
                        _medios_pauta_directa = [{"tipo": "imagen", "url": pauta_imagen, "caption": matched_msg}] if pauta_imagen else None
                else:
                    _respuesta_pauta_directa = None
                    _medios_pauta_directa = None
                    await bot_log(instancia, "info", "Pautas",
                        f"no_match_con_pautas count={len(pautas)} best_score={best_score:.2f} "
                        f"second_score={second_score:.2f} es_fb={bool(es_fb)}")
    except Exception as e:
        await bot_log(instancia, "error", "Pautas", f"Error checando pautas: {e}")

    # ── Si hay respuesta directa de pauta, enviarla y terminar ───────────────
    if _respuesta_pauta_directa:
        await bot_log(instancia, "info", "Bot", f"respuesta PAUTA directa ({len(_respuesta_pauta_directa)} chars) tel={telefono}")

        async def _enviar_pauta(texto: str, user_msg: str = "", medios: list | None = None) -> None:
            if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
                await bot_log(instancia, "warning", "BotSend", "CRM no configurado — respuesta no enviada")
                return
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    payload: dict = {
                        "telefono":     telefono,
                        "remote_jid":   remote_jid,
                        "instancia":    instancia,
                        "respuesta":    texto,
                        "user_message": user_msg,
                    }
                    if medios:
                        payload["medios"] = medios
                    resp = await client.post(
                        f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                        headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                        json=payload,
                    )
                    if resp.status_code not in (200, 201):
                        await bot_log(instancia, "error", "BotSend",
                            f"bot-send retornó {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                await bot_log(instancia, "error", "BotSend", f"Error en bot-send: {exc}")

        await _enviar_pauta(_respuesta_pauta_directa, procesado.get("etiqueta", mensaje[:120]), medios=_medios_pauta_directa)
        return

    resultado = None
    if not es_flujo_negocio_puro:
        resultado = await responder_productos(
            texto_usuario=texto_para_bot,
            historial_texto=historial_texto,
            analisis=procesado.get("analisis") or {},
            intencion=intencion,
            instancia=instancia,
        )

    respuesta: str | None = None
    medios = None
    if isinstance(resultado, dict):
        respuesta = resultado.get("texto")
        medios = resultado.get("medios")
    else:
        respuesta = resultado

    if not respuesta and not medios:
        respuesta = RESPUESTA_PRUEBA
        await bot_log(instancia, "warning", "vitta4", f"usando respuesta genérica tel={telefono}")

    # Detectar si PASO 3 terminó (GPT incluyó [[LISTO]] o frases comunes de cierre)
    paso3_listo = False
    try:
        if respuesta and isinstance(respuesta, str):
            if PASO3_SIGNAL in respuesta:
                paso3_listo = True
                respuesta = respuesta.replace(PASO3_SIGNAL, "").strip()
                await bot_log(instancia, "info", "Bot", f"PASO3 completado (marker) → enviando mensaje de transición tel={telefono}")
            else:
                # Heurística: frases típicas que indican que la entrevista terminó
                if re.search(r"(ya tengo todo lo que necesito|tengo todo lo que necesito|ya tengo toda la información que necesito|con lo que me has contado ya tengo todo lo que necesito|perfecto, con lo que me has contado ya tengo todo lo que necesito)", respuesta, re.IGNORECASE):
                    paso3_listo = True
                    await bot_log(instancia, "info", "Bot", f"PASO3 completado (phrase) → enviando mensaje de transición tel={telefono}")
    except Exception:
        pass

    await bot_log(instancia, "info", "Bot", f"respuesta generada ({len(respuesta)} chars) tel={telefono}")

    # ── Enviar al usuario vía CRM /bot-send ───────────────────────────────────
    async def _enviar(texto: str, user_msg: str = "", medios: list | None = None) -> None:
        if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
            await bot_log(instancia, "warning", "BotSend", "CRM no configurado — respuesta no enviada")
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                    headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                    json=(
                        {
                            "telefono":     telefono,
                            "remote_jid":   remote_jid,
                            "instancia":    instancia,
                            "respuesta":    texto,
                            "user_message": user_msg,
                        }
                        if not medios
                        else {
                            "telefono":     telefono,
                            "remote_jid":   remote_jid,
                            "instancia":    instancia,
                            "respuesta":    texto,
                            "user_message": user_msg,
                            "medios":        medios,
                        }
                    ),
                )
                if resp.status_code not in (200, 201):
                    await bot_log(instancia, "error", "BotSend",
                        f"bot-send retornó {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            await bot_log(instancia, "error", "BotSend", f"Error en bot-send: {exc}")

    await _enviar(respuesta, procesado.get("etiqueta", mensaje[:120]), medios=medios)

    if paso3_listo:
        # Enviar mensaje de transición
        transition_text = "Estoy examinando tu situación para brindarte la mejor información 🔍"
        await _enviar(transition_text)
        # Intentar avanzar inmediatamente a PASO4: llamar de nuevo a responder_productos
        try:
            temp_hist = historial_texto + ("\nBot: " + transition_text if historial_texto else "Bot: " + transition_text)
            resultado2 = None
            if not es_flujo_negocio_puro:
                resultado2 = await responder_productos(
                    texto_usuario=texto_para_bot,
                    historial_texto=temp_hist,
                    analisis=procesado.get("analisis") or {},
                    intencion=intencion,
                    instancia=instancia,
                )
            respuesta2 = None
            medios2 = None
            if isinstance(resultado2, dict):
                respuesta2 = resultado2.get("texto")
                medios2 = resultado2.get("medios")
            else:
                respuesta2 = resultado2
            if respuesta2:
                await bot_log(instancia, "info", "Bot", f"respuesta PASO4 generada ({len(respuesta2)} chars) tel={telefono}")
                await _enviar(respuesta2, procesado.get("etiqueta", mensaje[:120]), medios=medios2)
        except Exception as e:
            await bot_log(instancia, "error", "Bot", f"Error generando PASO4 inmediatamente: {e}")


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


# ── Entrenamiento IA ──────────────────────────────────────────────────────────

@app.post("/entrenamiento-ia")
async def entrenamiento_ia(request: Request) -> dict[str, Any]:
    """Recibe lote de conversaciones del CRM, analiza con IA y retorna/callback resultados."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    # Validar token entrante
    if INCOMING_TOKEN:
        request_token = body.get("api_token") or request.headers.get("X-API-Token")
        if request_token != INCOMING_TOKEN:
            raise HTTPException(status_code=401, detail="Token de acceso inválido.")

    instancia     = body.get("instancia", "")
    conversaciones = body.get("conversaciones", [])
    callback_url  = body.get("callback_url", "")
    api_key       = body.get("api_key", "")

    if not conversaciones:
        raise HTTPException(status_code=400, detail="No se recibieron conversaciones.")

    await bot_log(instancia, "info", "EntrenamientoIA",
                  f"Recibidas {len(conversaciones)} conversaciones para análisis.")

    # Procesar en background para responder rápido
    asyncio.create_task(
        _procesar_entrenamiento_ia(instancia, conversaciones, callback_url, api_key)
    )
    return {"status": "procesando", "total": len(conversaciones)}


async def _procesar_entrenamiento_ia(
    instancia: str,
    conversaciones: list[dict],
    callback_url: str,
    api_key: str,
) -> None:
    """Procesa cada conversación con IA y envía resultados al callback URL."""
    resultados: list[dict] = []

    for conv in conversaciones:
        telefono  = conv.get("telefono", "")
        ultimo_id = conv.get("ultimo_id", 0)
        pares     = conv.get("pares", [])

        try:
            meta = await analizar_conversacion_entrenamiento(pares)
            prompt_generado = ""
            if meta and meta.get("recomendaciones"):
                recs = "; ".join(meta["recomendaciones"][:5])
                prompt_generado = (
                    f"Cuando el cliente presente objeciones, recuerda: {recs}. "
                    f"La calidad general fue {meta.get('calidad_general', 'regular')} "
                    f"(puntaje {meta.get('puntaje', 0)}/10)."
                )

            resultados.append({
                "telefono":       telefono,
                "ultimo_id":      ultimo_id,
                "metadatos":      meta or {},
                "prompt_generado": prompt_generado,
            })
            await bot_log(instancia, "info", "EntrenamientoIA",
                          f"Análisis OK tel={telefono} calidad={meta.get('calidad_general') if meta else 'N/A'}")
        except Exception as exc:
            await bot_log(instancia, "error", "EntrenamientoIA",
                          f"Error analizando tel={telefono}: {exc}")
            resultados.append({
                "telefono":  telefono,
                "ultimo_id": ultimo_id,
                "error":     str(exc),
            })

    # Enviar resultados al callback del CRM
    if callback_url and resultados:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    callback_url,
                    json={"resultados": resultados},
                    headers={"X-API-Key": api_key} if api_key else {},
                )
            await bot_log(instancia, "info", "EntrenamientoIA",
                          f"Resultados enviados a callback: {len(resultados)} conversaciones.")
        except Exception as exc:
            await bot_log(instancia, "error", "EntrenamientoIA",
                          f"Error enviando callback: {exc}")