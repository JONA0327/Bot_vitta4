
import asyncio
import os
import time
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
import bot_productos as _bot_productos_module
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

# ── Reglas del Bot (cargadas desde el CRM al arrancar) ───────────────────────
# Diccionario vacío = sin reglas extra (el bot funciona con solo sus prompts base).
# Se puebla en el startup event desde GET /api/v1/{tenant}/bot-reglas.
BOT_REGLAS: dict = {}


async def _cargar_reglas_bot() -> None:
    """Carga el JSON de reglas activas desde el CRM y lo almacena en BOT_REGLAS.
    Si el endpoint no está disponible o no hay reglas configuradas, BOT_REGLAS
    queda vacío y el bot funciona normalmente con sus prompts base.
    """
    global BOT_REGLAS
    _url    = CRM_URL    or os.getenv("CRM_URL",    "").rstrip("/")
    _tenant = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _token  = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not _url or not _tenant or not _token:
        print("[Reglas] CRM no configurado — sin reglas extra.")
        return
    endpoint = f"{_url}/api/v1/{_tenant}/bot-reglas"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(endpoint, headers={"X-API-Key": _token})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data:
                BOT_REGLAS = data
                version = data.get("version", "?")
                n_rest  = len(data.get("restricciones_globales", []))
                print(f"[Reglas] v{version} cargada — {n_rest} restricciones globales.")
                # Propagar al módulo del bot para que se inyecte en los prompts
                _bot_productos_module._REGLAS_ACTIVAS = BOT_REGLAS
            else:
                print("[Reglas] Sin reglas configuradas en el CRM.")
    except Exception as e:
        print(f"[Reglas] No se pudo cargar — el bot usará prompts base. ({e})")


@app.on_event("startup")
async def startup_event() -> None:
    await _cargar_reglas_bot()

INCOMING_TOKEN = os.getenv("CRM_INCOMING_TOKEN")
RESPUESTA_PRUEBA = os.getenv(
    "BOT_RESPUESTA_PRUEBA",
    "¡Hola! Soy el Bot Vitta4. Tu mensaje llegó correctamente. 🤖",
)
# Segundos de espera antes de procesar (0 = sin debounce)
DEBOUNCE_SECS = int(os.getenv("BOT_DEBOUNCE_SECS", "20"))

# Etiquetas que el bot envía al CRM al pausar la conversación.
# Se almacenan únicamente en BD (nivel sistema, no en WhatsApp).
# Configura en .env según el nombre que quieras ver en el CRM.
_ETIQUETA_CIERRE = os.getenv("BOT_ETIQUETA_CIERRE", "Cierre")
_ETIQUETA_PAUSA  = os.getenv("BOT_ETIQUETA_PAUSA",  "Pausa")
# Motivos que corresponden a un cierre definitivo (sin intención de retomar)
_MOTIVOS_CIERRE  = {
    "precio_temprano",
    "rechazo_videos",
    "post_video_cierre_usuario",
    "post_recomendacion",
}

# ── Estado de debounce (clave = "instancia:telefono") ────────────────────────
_pending_tasks: dict[str, asyncio.Task] = {}
_pending_data: dict[str, dict] = {}

# ── Conversaciones tomadas por el dueño del número (humano activo) ────────────
# Clave "instancia:telefono" → timestamp del último mensaje fromMe=true.
# Mientras esté en este dict, el bot recibe mensajes pero NO responde.
# Se expira automáticamente después de 2 horas sin actividad del dueño.
_human_activo: dict[str, float] = {}
_HUMAN_TTL_SECS: int = 7200  # 2 horas


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


def _filtrar_pautas_por_telefono(pautas: list, telefono: str) -> list:
    """Filtra las pautas que tienen el número de WhatsApp del contacto en alguno de sus campos.

    Busca en campos típicos: TELEFONO, NUMEROS, WHATSAPP, NUMERO, NUMERO_WHATSAPP (y variantes
    en minúscula). El valor puede ser un string con uno o varios números separados por coma,
    espacio, punto y coma o salto de línea.

    Retorna solo las pautas que contienen ese número; lista vacía si ninguna coincide.
    """
    if not telefono or not pautas:
        return []

    _CAMPOS = (
        "TELEFONO", "telefono", "NUMEROS", "numeros",
        "WHATSAPP", "whatsapp", "NUMERO", "numero",
        "NUMERO_WHATSAPP", "numero_whatsapp",
        "PHONE", "phone", "PHONES", "phones",
    )

    # Normalizar el teléfono: solo dígitos, sin prefijos de país duplicados comunes
    tel_norm = re.sub(r"\D", "", telefono)

    coincidentes = []
    for pauta in pautas:
        d = _pauta_datos(pauta)
        for campo in _CAMPOS:
            valor = d.get(campo)
            if not valor:
                continue
            # Extraer todos los números del campo
            numeros_campo = re.split(r"[,;\s\n]+", str(valor))
            for num in numeros_campo:
                num_norm = re.sub(r"\D", "", num.strip())
                if not num_norm:
                    continue
                # Comparación flexible: sufijo coincide (maneja prefijos de país distintos)
                if tel_norm.endswith(num_norm) or num_norm.endswith(tel_norm):
                    coincidentes.append(pauta)
                    break
            else:
                continue
            break  # ya encontramos coincidencia en este campo, pasar a la siguiente pauta

    return coincidentes


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


# ── Helpers: agente humano activo ────────────────────────────────────────────

async def _pausar_por_agente(telefono: str, remote_jid: str, instancia: str) -> None:
    """Pausa el bot para la conversación tomada por un agente humano."""
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{CRM_URL}/api/v1/{CRM_TENANT}/blocked_numbers",
                headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                json={
                    "Numero_Baneado": telefono,
                    "Numero_Remote":  remote_jid or telefono,
                    "Motivo_Bloqueo": "agente_humano",
                    "tipo_bloqueo":   "irrelevante",
                    "instancia":      instancia,
                    "etiqueta":       _ETIQUETA_PAUSA,
                },
            )
        await bot_log(instancia, "info", "AgenteActivo", f"Bot pausado por agente tel={telefono}")
    except Exception as exc:
        await bot_log(instancia, "error", "AgenteActivo", f"Error pausando por agente: {exc}")


async def _guardar_turno_agente(
    telefono: str,
    remote_jid: str,
    instancia: str,
    mensaje: str,
    agente_nombre: str = "Agente",
) -> None:
    """Guarda el mensaje de un agente en el historial del CRM sin reenviarlo al cliente.

    Usa bot-send con solo_historial=true para almacenar el turno en la conversación
    y mantener el historial completo cuando el bot sea reactivado.
    """
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN) or not mensaje:
        return
    texto_historial = f"[{agente_nombre}]: {mensaje}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                json={
                    "telefono":       telefono,
                    "remote_jid":     remote_jid or telefono,
                    "instancia":      instancia,
                    "respuesta":      texto_historial,
                    "user_message":   "",
                    "solo_historial": True,
                },
            )
        nivel = "info" if resp.status_code in (200, 201) else "warning"
        await bot_log(instancia, nivel, "AgenteActivo",
                      f"Turno agente guardado (status {resp.status_code}) tel={telefono}")
    except Exception as exc:
        await bot_log(instancia, "error", "AgenteActivo", f"Error guardando turno agente: {exc}")


# ── Procesamiento y envío (llamado tras el debounce) ─────────────────────────

async def _procesar_y_enviar(data: dict) -> None:
    """Procesa el último mensaje acumulado y lo envía al usuario vía CRM /bot-send."""
    mensaje        = data.get("mensaje", "")
    # Usar solo el último mensaje individual como user_message al CRM (evita duplicados)
    _msgs_acum     = data.get("_mensajes_acumulados") or []
    _ultimo_msg    = _msgs_acum[-1] if _msgs_acum else mensaje
    tipo_contenido = data.get("tipo_contenido", "texto")
    telefono       = data.get("telefono", "desconocido")
    instancia      = data.get("instancia", "")

    # ── Verificar si un humano está activo para esta conversación ─────────────
    # Si el dueño escribió recientemente (fromMe=true), el bot NO responde.
    # El CRM ya almacena los mensajes del cliente directamente desde Evolution,
    # así que no necesitamos reenviarlos — solo evitar que el bot genere respuesta.
    _clave_proc = f"{instancia}:{telefono}"
    _ts_human   = _human_activo.get(_clave_proc)
    if _ts_human is not None:
        if (time.time() - _ts_human) < _HUMAN_TTL_SECS:
            await bot_log(instancia, "info", "AgenteActivo",
                          f"Humano activo → mensaje recibido, bot no responde tel={telefono}")
            return
        else:
            # TTL expirado — el dueño no ha escrito en 2 horas; reactivar bot
            _human_activo.pop(_clave_proc, None)
            await bot_log(instancia, "info", "AgenteActivo",
                          f"Humano TTL expirado → bot reactivado tel={telefono}")
    url_media      = data.get("url_media", "")
    caption        = data.get("caption", "")
    titulo_fb      = data.get("titulo_fb", "")
    descripcion_fb = data.get("descripcion_fb", "")
    # Usar imagen ya descargada como base64 si está disponible (evita expiración de URL)
    thumbnail_url  = data.get("thumbnail_url_b64") or data.get("thumbnail_url", "")
    remote_jid     = data.get("remote_jid", "")
    alt_phones     = data.get("alt_phones", [])
    contact_name   = data.get("contact_name") or None

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
    # No pasar thumbnail_url como url_publicidad para mensajes de audio/video;
    # de lo contrario el filtro detecta pub_facebook=True y sobreescribe la transcripción.
    _url_publicidad = thumbnail_url if tipo_contenido not in ("audio", "video", "documento") else ""
    clasificacion = await filtrar_y_clasificar(
        mensaje=mensaje,
        tipo_original=tipo_contenido,
        url_publicidad=_url_publicidad,
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
    # Excluir audio/video/doc: ya fueron procesados correctamente (transcripción, etc.)
    if clasificacion["pub_facebook"] and tipo_contenido not in ("publicacion_facebook", "audio", "video", "documento"):
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
    elif _turnos_bot == 2:
        paso_flujo = "PASO2B"
    elif _MARKER_PASO3 not in historial_texto:
        paso_flujo = f"PASO3-entrevista (turnos_bot={_turnos_bot})"
    else:
        paso_flujo = f"PASO4-recomendacion (turnos_bot={_turnos_bot})"
    await bot_log(instancia, "info", "vitta4",
        f"tipo_intencion={tipo_intencion!r} historial={'sí' if historial_texto else 'no'} flujo={flujo} paso={paso_flujo}")

    # ── Antes de generar la respuesta: comprobar si hay pauta activa en el CRM
    # Se ejecuta para publicaciones de FB y para contactos tempranos (bot aún no ha respondido,
    # es decir _turnos_bot == 0). Usar historial_texto.strip() como proxy era incorrecto porque
    # el historial puede tener el mensaje del usuario pero aún 0 respuestas del bot.
    try:
        es_fb = clasificacion.get("pub_facebook") or tipo_contenido == "publicacion_facebook"
        es_primer_contacto = _turnos_bot == 0   # bot no ha respondido aún → contacto inicial
        if es_fb or es_primer_contacto:
            pautas = await _buscar_pautas_activas(instancia)
            await bot_log(instancia, "info", "Pautas",
                f"consultando pautas activas count={len(pautas)} es_fb={bool(es_fb)} es_primer_contacto={es_primer_contacto}")
            if pautas:
                # Solo se usa la pauta si el número del contacto está registrado en ella.
                # Si ninguna pauta tiene el número, se continúa el flujo sin pauta.
                pautas_por_tel = _filtrar_pautas_por_telefono(pautas, telefono)
                if not pautas_por_tel:
                    await bot_log(instancia, "info", "Pautas",
                        f"sin_coincidencia_telefono tel={telefono} → flujo sin pauta")
                else:
                    best = pautas_por_tel[0]
                    await bot_log(instancia, "info", "Pautas",
                        f"filtro_telefono: {len(pautas_por_tel)} pauta(s) para tel={telefono} → usando pauta")

                    def _nombre_pauta(p: dict) -> str:
                        d = _pauta_datos(p)
                        return (
                            d.get("LINEA_PRODUCTO") or d.get("linea_producto")
                            or d.get("NOMBRE_PAUTA") or d.get("nombre_pauta")
                            or d.get("nombre") or d.get("name") or ""
                        ).strip()

                    nombres_promocion = []
                    seen_nombres: set = set()
                    for _p in pautas_por_tel:
                        _n = _nombre_pauta(_p)
                        if _n and _n.lower() not in seen_nombres:
                            seen_nombres.add(_n.lower())
                            nombres_promocion.append(_n)

                    es_multi_pauta = len(nombres_promocion) > 1

                    if es_multi_pauta:
                        await bot_log(instancia, "info", "Pautas",
                            f"multi_pauta ({len(nombres_promocion)}) → listando promociones")
                        procesado.setdefault("analisis", {})["pautas_multiples"] = nombres_promocion
                        procesado["analisis"]["resumen_para_bot"] = (
                            "Actualmente tenemos estas promociones disponibles: "
                            + ", ".join(nombres_promocion)
                            + ". Pregunta al cliente por cuál desea información o si le interesa algún otro producto."
                        )
                        intencion = {
                            "intencion": "productos",
                            "confianza": 0.95,
                            "productos_mencionados": nombres_promocion,
                        }
                    else:
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
                            pauta_nombre = _bd.get("NOMBRE_PAUTA") or _bd.get("nombre_pauta") or _bd.get("nombre") or ""
                            await bot_log(instancia, "info", "Pautas",
                                f"pauta_USADA nombre={pauta_nombre!r} modo=telefono")
                            procesado.setdefault("analisis", {})["resumen_para_bot"] = matched_msg
                            _prods_act = procesado["analisis"].get("productos_mencionados") or []
                            if pauta_nombre and pauta_nombre not in _prods_act:
                                procesado["analisis"]["productos_mencionados"] = [pauta_nombre] + _prods_act
                            intencion = {
                                "intencion": "productos",
                                "confianza": 0.95,
                                "productos_mencionados": procesado["analisis"].get("productos_mencionados") or [],
                            }
    except Exception as e:
        await bot_log(instancia, "error", "Pautas", f"Error checando pautas: {e}")

    # Recalcular tras posible actualización de intencion por pauta activa
    tipo_intencion = (intencion.get("intencion") or "").lower()
    es_flujo_negocio_puro = tipo_intencion == "negocio" and not historial_texto.strip()

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
    mensagens_multi: list | None = None
    if isinstance(resultado, dict):
        # Si el bot indica que no hay productos en catálogo → pausar sin responder
        if resultado.get("pausar"):
            motivo = resultado.get("motivo", "sin_productos_catalogo")
            await bot_log(instancia, "info", "Bot",
                f"pausando conversación motivo={motivo!r} tel={telefono}")
            try:
                from Historial_Conversacion import IrrelevantConversationModel
            except ImportError:
                IrrelevantConversationModel = None
            # Pausar conversación vía CRM /blocked_numbers con tipo_bloqueo=irrelevante
            async def _pausar_conv() -> None:
                if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
                    return
                _jid = remote_jid or telefono
                _etiqueta = _ETIQUETA_CIERRE if motivo in _MOTIVOS_CIERRE else _ETIQUETA_PAUSA
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"{CRM_URL}/api/v1/{CRM_TENANT}/blocked_numbers",
                            headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                            json={
                                "Numero_Baneado": telefono,
                                "Numero_Remote":  _jid,
                                "Motivo_Bloqueo": motivo,
                                "tipo_bloqueo":   "irrelevante",
                                "instancia":      instancia,
                                "etiqueta":       _etiqueta,
                            },
                        )
                        
                        owner_phone = os.getenv("BOT_OWNER_PHONE")
                        if owner_phone:
                            mensaje_alerta = (
                                f"🚨 *ALERTA DE BOT PAUSADO* 🚨\n\n"
                                f"Instancia: {instancia}\n"
                                f"Cliente: wa.me/{telefono}\n"
                                f"Motivo: {motivo}\n\n"
                                f"La conversación fue pausada. ¡Requiere atención humana!"
                            )
                            await client.post(
                                f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                                headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                                json={
                                    "telefono": owner_phone,
                                    "instancia": instancia,
                                    "respuesta": mensaje_alerta,
                                },
                            )
                except Exception as exc:
                    await bot_log(instancia, "error", "BotSend", f"Error pausando o notificando al dueño: {exc}")
            await _pausar_conv()
            return
        respuesta = resultado.get("texto")
        medios = resultado.get("medios")
        mensagens_multi = resultado.get("mensagens")
    else:
        respuesta = resultado

    if not respuesta and not medios and not mensagens_multi:
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

    if mensagens_multi:
        n_msgs = len(mensagens_multi)
        await bot_log(instancia, "info", "Bot", f"respuesta multi-mensaje ({n_msgs} partes) tel={telefono}")
    else:
        await bot_log(instancia, "info", "Bot", f"respuesta generada ({len(respuesta or '')} chars) tel={telefono}")

    # ── Enviar al usuario vía CRM /bot-send ───────────────────────────────────
    async def _enviar(texto: str, user_msg: str = "", medios: list | None = None) -> None:
        if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
            await bot_log(instancia, "warning", "BotSend", "CRM no configurado — respuesta no enviada")
            return
        try:
            print(f"[_enviar] texto={texto[:80]!r} medios={medios}")
            async with httpx.AsyncClient(timeout=15) as client:
                _payload: dict = {
                    "telefono":     telefono,
                    "remote_jid":   remote_jid,
                    "instancia":    instancia,
                    "respuesta":    texto,
                    "user_message": user_msg,
                }
                if contact_name:
                    _payload["contact_name"] = contact_name
                if medios:
                    _payload["medios"] = medios
                resp = await client.post(
                    f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-send",
                    headers={"X-API-Key": CRM_API_TOKEN, "Content-Type": "application/json"},
                    json=_payload,
                )
                if resp.status_code not in (200, 201):
                    await bot_log(instancia, "error", "BotSend",
                        f"bot-send retornó {resp.status_code}: {resp.text[:200]}")
                else:
                    print(f"[_enviar] OK status={resp.status_code} medios_count={len(medios) if medios else 0}")
        except Exception as exc:
            await bot_log(instancia, "error", "BotSend", f"Error en bot-send: {exc}")

    if mensagens_multi:
        # Multi-message PASO4: enviar cada producto como mensaje separado
        etiqueta = procesado.get("etiqueta", mensaje[:120])
        for msg in mensagens_multi:
            txt = msg.get("texto") or ""
            mds = msg.get("medios") or None
            if txt or mds:
                await _enviar(txt, etiqueta, medios=mds)
        return

    await _enviar(respuesta, procesado.get("etiqueta", _ultimo_msg[:120]), medios=medios)

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
            mensagens2 = None
            if isinstance(resultado2, dict):
                mensagens2 = resultado2.get("mensagens")
                respuesta2 = resultado2.get("texto")
                medios2 = resultado2.get("medios")
            else:
                respuesta2 = resultado2
            if mensagens2:
                etiqueta2 = procesado.get("etiqueta", mensaje[:120])
                for msg in mensagens2:
                    txt = msg.get("texto") or ""
                    mds = msg.get("medios") or None
                    if txt or mds:
                        await _enviar(txt, etiqueta2, medios=mds)
            elif respuesta2:
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
    # Facebook: respuesta inmediata (el contexto del anuncio ya define la intención)
    # Contacto directo (no Facebook): esperar debounce para acumular mensajes y detectar urgencia
    es_publicacion_fb = bool((data or {}).get("titulo_fb") or (data or {}).get("descripcion_fb"))
    if es_primer_mensaje and es_publicacion_fb:
        sleep_secs = 0
        await bot_log(instancia, "info", "Debounce", f"primer mensaje (Facebook) → respuesta inmediata tel={telefono}")
    elif es_primer_mensaje:
        sleep_secs = DEBOUNCE_SECS
        await bot_log(instancia, "info", "Debounce", f"primer mensaje (directo) → esperando {sleep_secs}s tel={telefono}")
    else:
        sleep_secs = DEBOUNCE_SECS
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


@app.post("/mensaje-agente")
async def mensaje_agente_endpoint(request: Request) -> dict[str, Any]:
    """Endpoint que el CRM llama cuando un agente humano envía un mensaje a un cliente.

    Pausa el bot inmediatamente para esa conversación y guarda el mensaje del agente
    en el historial, de modo que el contexto quede completo al reactivar el bot.

    Payload esperado (JSON):
      telefono      — número del cliente (requerido)
      instancia     — instancia de WhatsApp (requerido)
      mensaje       — texto que escribió el agente (opcional)
      remote_jid    — remoteJid del contacto (opcional, default = telefono)
      agente_nombre — nombre del agente para el historial (opcional, default = "Agente")
      api_token     — token de autenticación (o header X-API-Token)
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    if INCOMING_TOKEN:
        token = body.get("api_token") or request.headers.get("X-API-Token")
        if token != INCOMING_TOKEN:
            raise HTTPException(status_code=401, detail="Token de acceso inválido.")

    telefono      = (body.get("telefono") or "").strip()
    instancia     = (body.get("instancia") or "").strip()
    mensaje       = (body.get("mensaje") or "").strip()
    remote_jid    = (body.get("remote_jid") or telefono).strip()
    agente_nombre = (body.get("agente_nombre") or "Agente").strip()

    if not telefono:
        raise HTTPException(status_code=400, detail="Falta el campo 'telefono'.")

    # Cancelar cualquier tarea de debounce pendiente — el bot no debe responder
    clave = f"{instancia}:{telefono}"
    tarea = _pending_tasks.pop(clave, None)
    if tarea and not tarea.done():
        tarea.cancel()
        await bot_log(instancia, "info", "AgenteActivo",
                      f"Debounce cancelado por agente tel={telefono}")
    _pending_data.pop(clave, None)

    # Pausar el bot para esta conversación
    await _pausar_por_agente(telefono, remote_jid, instancia)

    # Guardar el mensaje del agente en historial (sin reenviarlo al cliente)
    if mensaje:
        asyncio.create_task(
            _guardar_turno_agente(telefono, remote_jid, instancia, mensaje, agente_nombre)
        )

    await bot_log(instancia, "info", "AgenteActivo",
                  f"Agente {agente_nombre!r} tomó conversación tel={telefono} msg={mensaje[:60]!r}")

    return {"status": "ok", "pausado": True, "agente": agente_nombre}


@app.post("/reactivar-bot")
async def reactivar_bot(request: Request) -> dict[str, Any]:
    """Reactiva el bot para una conversación que fue tomada por el dueño.

    Elimina el flag de humano activo para que el bot vuelva a responder.
    También puede remover el bloqueo en el CRM si se indica.

    Payload: { telefono, instancia, api_token? }
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    if INCOMING_TOKEN:
        token = body.get("api_token") or request.headers.get("X-API-Token")
        if token != INCOMING_TOKEN:
            raise HTTPException(status_code=401, detail="Token de acceso inválido.")

    telefono  = (body.get("telefono") or "").strip()
    instancia = (body.get("instancia") or "").strip()

    if not telefono:
        raise HTTPException(status_code=400, detail="Falta el campo 'telefono'.")

    clave = f"{instancia}:{telefono}"
    estaba_activo = clave in _human_activo
    _human_activo.pop(clave, None)

    await bot_log(instancia, "info", "AgenteActivo",
                  f"Bot reactivado manualmente tel={telefono} (estaba_activo={estaba_activo})")

    return {"status": "ok", "bot_activo": True, "telefono": telefono}


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
    contact_name   = body.get("contact_name") or None
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

    # ── Detección de mensaje enviado por el dueño del número (no por el bot) ────
    # Dos señales posibles:
    #   1. fromMe=true en el payload de Evolution (dueño escribe directo en WhatsApp)
    #   2. tipo_remitente="agente"/"humano" enviado explícitamente por el CRM
    # En ambos casos: pausar el bot y guardar el mensaje en historial para
    # mantener el contexto completo cuando se reactive.
    _evo_key   = (evo_data.get("data", {}) or {}).get("key", {}) or {}
    _from_me   = bool(_evo_key.get("fromMe", False)) or str(body.get("fromMe", "")).lower() == "true"
    _tipo_rem  = (body.get("tipo_remitente") or "").strip().lower()
    _es_humano = _from_me or (_tipo_rem in ("agente", "humano", "asesor"))

    if _es_humano:
        _clave_h = f"{instancia}:{telefono}"
        # Marcar conversación como "humano activo" — el bot no responderá mientras esté aquí
        _human_activo[_clave_h] = time.time()
        # Cancelar cualquier tarea de debounce pendiente
        _tarea_h = _pending_tasks.pop(_clave_h, None)
        if _tarea_h and not _tarea_h.done():
            _tarea_h.cancel()
        _pending_data.pop(_clave_h, None)
        # Pausar en el CRM (blocked_numbers) para que el sistema lo refleje
        asyncio.create_task(_pausar_por_agente(telefono, _remote_jid or telefono, instancia))
        # Guardar el mensaje del dueño en historial para preservar el contexto
        if mensaje:
            _nombre_h = (body.get("agente_nombre") or body.get("contact_name") or "Humano").strip()
            asyncio.create_task(_guardar_turno_agente(
                telefono, _remote_jid or telefono, instancia, mensaje, _nombre_h
            ))
        await bot_log(instancia, "info", "AgenteActivo",
                      f"Mensaje del dueño (fromMe={_from_me}) → pausando bot tel={telefono}")
        return {"debounced": False, "humano": True}

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
        "contact_name":      contact_name,
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