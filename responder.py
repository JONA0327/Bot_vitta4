"""
responder.py — Motor adaptativo de respuestas para Bot 4Life Vitta
──────────────────────────────────────────────────────────────────
Sin pasos fijos: el bot se adapta a cada conversación usando:
  1. Catálogo de productos en vivo del CRM (caché 5 min)
  2. Ejemplos de entrenamiento aprobados buscados por similitud (RAG)
  3. Instrucciones de mejora del análisis IA (caché 30 min)
  4. Detección anti-ciclo para evitar repetir temas ya cubiertos
  5. Historial completo de conversación en formato OpenAI
  6. Reglas y restricciones configuradas en el CRM

Seguridad:
  - El mensaje del usuario NUNCA entra al system prompt (evita prompt injection).
  - Todo input del CRM se valida de tipo antes de usarlo.
  - Timeouts en todas las llamadas HTTP externas.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

import httpx
import zoneinfo

# ── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "15"))
CRM_URL        = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT     = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN  = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT    = float(os.getenv("CRM_TIMEOUT", "8"))
BOT_NOMBRE     = os.getenv("BOT_NOMBRE", "Valeria")

# ── Module-level caches ───────────────────────────────────────────────────────
# Injected by main.py on startup via _cargar_reglas_crm()
_REGLAS_ACTIVAS: dict = {}

# Shared product catalog (same for all contacts)
_PRODUCT_CACHE: dict = {}   # {"data": str, "expires_at": float}

# Per-instancia IA analysis instructions
_ANALISIS_CACHE: dict = {}  # {instancia_key: {"data": str, "expires_at": float}}

_PRODUCT_TTL  = 300.0   # 5 minutes
_ANALISIS_TTL = 1800.0  # 30 minutes

# Topics tracked for anti-cycle detection (label → keywords to search in bot responses)
_TEMAS_KW: list[tuple[str, list[str]]] = [
    ("Transfer Factor",  ["transfer factor"]),
    ("RioVida",          ["riovida"]),
    ("precio",           ["precio", "cuesta", "vale", "cuánto", "cuanto"]),
    ("beneficios",       ["beneficio", "sirve para", "ayuda con"]),
    ("envío",            ["envío", "envio", "entrega", "llega"]),
    ("paquetes",         ["paquete", "combo"]),
    ("ofertas",          ["oferta", "descuento", "promoción", "promocion"]),
    ("ingredientes",     ["ingrediente", "componente", "fórmula", "formula"]),
    ("dosis",            ["dosis", "cómo tomar", "como tomar"]),
    ("testimonios",      ["testimonio", "experiencia", "resultado"]),
]


# ── Fecha/hora helpers ────────────────────────────────────────────────────────

def _saludo_hora() -> str:
    try:
        tz = zoneinfo.ZoneInfo("America/Mexico_City")
    except Exception:
        tz = None
    hora = datetime.now(tz).hour if tz else datetime.now().hour
    if 5 <= hora < 12:
        return "buenos días"
    elif 12 <= hora < 19:
        return "buenas tardes"
    return "buenas noches"


# ── CRM HTTP helper ───────────────────────────────────────────────────────────

async def _crm_get(modulo: str, params: dict | None = None) -> list | dict | None:
    _url = CRM_URL or os.getenv("CRM_URL", "").rstrip("/")
    _ten = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _tok = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _ten and _tok):
        return None
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(
                f"{_url}/api/v1/{_ten}/{modulo}",
                headers={"X-API-Key": _tok},
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[CRM] GET /{modulo} error: {e}")
        return None


# ── Product catalog ───────────────────────────────────────────────────────────

def _pick_field(rec: dict, keys: list[str]) -> Any:
    """Extracts a field from a CatalogRecord, unwrapping the nested 'datos' layer if present."""
    datos = rec.get("datos") if isinstance(rec.get("datos"), dict) else rec
    for k in keys:
        v = datos.get(k)
        if v not in (None, ""):
            return v
    if datos is not rec:
        for k in keys:
            v = rec.get(k)
            if v not in (None, ""):
                return v
    return None


def _formatear_lista_productos(items: list, etiqueta: str) -> str:
    if not items:
        return ""
    lineas = [f"{etiqueta}:"]
    for p in items:
        nombre = _pick_field(p, ["NOMBRE", "nombre", "name", "NAME"]) or "Producto"
        precio = _pick_field(p, ["PRECIO", "precio", "PRICE", "price"])
        desc   = _pick_field(p, [
            "DESCRIPCION", "descripcion", "DESCRIPTION", "description",
            "DESCRIPCION_CORTA", "descripcion_corta",
        ])
        linea = f"• {nombre}"
        if precio:
            linea += f": ${precio}"
        if desc:
            linea += f" — {str(desc)[:120]}"
        lineas.append(linea)
    return "\n".join(lineas)


async def _obtener_productos() -> str:
    """Returns formatted product + package catalog. Cached for _PRODUCT_TTL seconds."""
    now = time.monotonic()
    if _PRODUCT_CACHE.get("expires_at", 0.0) > now:
        return _PRODUCT_CACHE["data"]

    res_productos = await _crm_get("productos", {"per_page": 50})
    res_paquetes  = await _crm_get("paquetes",  {"per_page": 50})

    def _extract_list(res: list | dict | None) -> list:
        if res is None:
            return []
        if isinstance(res, list):
            return res
        return res.get("data") or []

    productos = _extract_list(res_productos)
    paquetes  = _extract_list(res_paquetes)

    partes: list[str] = []
    if productos:
        partes.append(_formatear_lista_productos(productos, "CATÁLOGO DE PRODUCTOS 4LIFE"))
    if paquetes:
        partes.append(_formatear_lista_productos(paquetes, "PAQUETES Y OFERTAS DISPONIBLES"))

    resultado = "\n\n".join(partes)
    _PRODUCT_CACHE["data"]       = resultado
    _PRODUCT_CACHE["expires_at"] = now + _PRODUCT_TTL
    return resultado


# ── Training RAG ──────────────────────────────────────────────────────────────

async def _crm_get_entrenamiento(q: str | None = None, limit: int = 10) -> list:
    _url = CRM_URL or os.getenv("CRM_URL", "").rstrip("/")
    _ten = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _tok = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _ten and _tok):
        return []
    try:
        if q:
            endpoint = f"{_url}/api/v1/{_ten}/entrenamiento/buscar"
            params: dict = {"q": q, "limit": limit}
        else:
            endpoint = f"{_url}/api/v1/{_ten}/entrenamiento"
            params = {"limit": limit}
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(endpoint, headers={"X-API-Key": _tok}, params=params)
            resp.raise_for_status()
            data  = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            return items if isinstance(items, list) else []
    except Exception as e:
        print(f"[Entrena·RAG] error: {e}")
        return []


def _formatear_ejemplos(pares: list) -> str:
    if not pares:
        return ""
    lineas: list[str] = ["EJEMPLOS DE CONVERSACIÓN APROBADOS (guía de tono y estilo):"]
    prompts_extra: list[str] = []
    for p in pares:
        d        = p.get("datos") if isinstance(p.get("datos"), dict) else p
        pregunta  = str(d.get("pregunta")  or p.get("pregunta")  or "").strip()
        respuesta = str(d.get("respuesta") or p.get("respuesta") or "").strip()
        if pregunta and respuesta:
            lineas.append(f"Cliente: {pregunta}\nAsesor: {respuesta}")
        pg = str(d.get("prompt_generado") or p.get("prompt_generado") or "").strip()
        if pg and pg not in prompts_extra:
            prompts_extra.append(pg)
    if len(lineas) <= 1 and not prompts_extra:
        return ""
    partes: list[str] = []
    if len(lineas) > 1:
        partes.append("\n---\n".join(lineas))
    if prompts_extra:
        partes.append("INSTRUCCIONES DE ENTRENAMIENTO:\n" + "\n".join(f"• {pg}" for pg in prompts_extra))
    return "\n\n".join(partes)


async def _buscar_entrenamiento(mensaje: str) -> str:
    """Searches approved training pairs by semantic similarity, falls back to general list."""
    pares = await _crm_get_entrenamiento(q=mensaje, limit=5)
    if not pares:
        pares = await _crm_get_entrenamiento(q=None, limit=15)
    return _formatear_ejemplos(pares)


# ── IA Analysis Instructions ──────────────────────────────────────────────────

def _formatear_analisis(items: list) -> str:
    prompts: list[str]     = []
    debilidades: list[str] = []

    for item in items:
        pg = (item.get("prompt_generado") or "").strip()
        if pg and len(pg) > 10:
            prompts.append(pg)

        meta = item.get("metadatos") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        for d in (meta.get("puntos_debiles") or []):
            d_str = str(d).strip()
            if d_str and d_str not in debilidades:
                debilidades.append(d_str)

    if not prompts and not debilidades:
        return ""

    partes: list[str] = []
    if prompts:
        partes.append(
            "INSTRUCCIONES DE MEJORA (aprendidas de análisis de conversaciones reales):\n"
            + "\n\n".join(f"• {p}" for p in prompts[:3])
        )
    if debilidades:
        partes.append(
            "ÁREAS A MEJORAR (detectadas por el sistema de entrenamiento):\n"
            + "\n".join(f"• {d}" for d in debilidades[:5])
        )
    return "\n\n".join(partes)


async def _obtener_analisis_entrenamiento(instancia: str) -> str:
    """Returns IA-generated improvement instructions. Cached per instancia for _ANALISIS_TTL seconds."""
    now       = time.monotonic()
    cache_key = instancia or "_default"
    entry     = _ANALISIS_CACHE.get(cache_key)
    if entry and entry["expires_at"] > now:
        return entry["data"]

    _url = CRM_URL or os.getenv("CRM_URL", "").rstrip("/")
    _ten = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _tok = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _ten and _tok):
        return ""

    try:
        params: dict = {"limit": 5}
        if instancia:
            params["instancia"] = instancia
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(
                f"{_url}/api/v1/{_ten}/entrenamiento/analisis",
                headers={"X-API-Key": _tok},
                params=params,
            )
            resp.raise_for_status()
            data  = resp.json()
            items = data.get("data") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
    except Exception as e:
        print(f"[Analisis·IA] error: {e}")
        # Cache a short error window to avoid hammering the CRM on repeated failures
        _ANALISIS_CACHE[cache_key] = {"data": "", "expires_at": now + 60.0}
        return ""

    resultado = _formatear_analisis(items)
    _ANALISIS_CACHE[cache_key] = {"data": resultado, "expires_at": now + _ANALISIS_TTL}
    return resultado


# ── Anti-cycle detection ──────────────────────────────────────────────────────

def _detectar_temas_repetidos(historial_crm: list[dict]) -> list[str]:
    """Returns topics mentioned 3+ times in the last 6 bot responses."""
    ultimas = [
        (t.get("bot_response") or "").lower()
        for t in historial_crm[-6:]
        if t.get("bot_response")
    ]
    if not ultimas:
        return []
    texto = " ".join(ultimas)
    repetidos: list[str] = []
    for etiqueta, keywords in _TEMAS_KW:
        if sum(kw in texto for kw in keywords) >= 3:
            repetidos.append(etiqueta)
    return repetidos


# ── CRM Rules ─────────────────────────────────────────────────────────────────

def _construir_addon_reglas() -> str:
    if not _REGLAS_ACTIVAS:
        return ""
    partes: list[str] = []

    restricciones = _REGLAS_ACTIVAS.get("restricciones_globales") or []
    if restricciones:
        partes.append("RESTRICCIONES OBLIGATORIAS:\n" + "\n".join(f"• {r}" for r in restricciones if r))

    conocimiento = (_REGLAS_ACTIVAS.get("conocimiento_extra") or "").strip()
    if conocimiento:
        partes.append(f"CONOCIMIENTO ADICIONAL:\n{conocimiento}")

    tono = _REGLAS_ACTIVAS.get("tono") or {}
    tono_parts: list[str] = []
    if tono.get("nivel"):
        tono_parts.append(f"tono {tono['nivel']}")
    if tono.get("tutear") is True:
        tono_parts.append("tutear al cliente (usar 'tú')")
    if tono.get("emojis") is True:
        tono_parts.append("usar emojis moderadamente")
    if tono_parts:
        partes.append("TONO: " + ", ".join(tono_parts))

    if not partes:
        return ""
    return "\n\n--- CONFIGURACIÓN CRM ---\n" + "\n\n".join(partes) + "\n---"


async def _cargar_reglas_crm() -> dict:
    _url = CRM_URL or os.getenv("CRM_URL", "").rstrip("/")
    _ten = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _tok = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _ten and _tok):
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_url}/api/v1/{_ten}/bot-reglas",
                headers={"X-API-Key": _tok},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Reglas] no se pudo cargar: {e}")
        return {}


# ── System prompt builder ─────────────────────────────────────────────────────

def _nombre_desde_instancia(instancia: str) -> str:
    """Converts an instance name like 'vitta4_valeria' → 'Vitta4 Valeria'."""
    return instancia.replace("-", " ").replace("_", " ").title()


def _construir_system_prompt(
    contexto_productos: str,
    ejemplos_entrenamiento: str,
    instrucciones_analisis: str,
    temas_repetidos: list[str],
    bot_nombre: str = "",
) -> str:
    hora       = _saludo_hora()
    nombre_bot = bot_nombre or BOT_NOMBRE or "Asesora"

    # NOTE: user input is NEVER included here — it goes as role:user in the messages array.
    partes: list[str] = [
        f"""Eres {nombre_bot}, asesora de bienestar y productos 4Life. Eres cálida, profesional y empática.
Tu misión es brindar información sobre los productos y ofertas exclusivas de 4Life.

FORMA DE CONVERSAR:
• Adáptate al contexto: no sigas pasos fijos ni guiones, responde lo que realmente se pregunta.
• Sé concisa y natural: no abrumes con información que no fue pedida.
• No repitas el mismo saludo ni la misma presentación en cada mensaje.
• Si el cliente no ha dado su nombre, puedes pedirlo cuando sea natural hacerlo.
• Nunca inventes precios, beneficios o propiedades que no estén en el catálogo.
• Si no tienes información sobre algo, dilo honestamente en lugar de inventar.
• Hora actual: {hora}."""
    ]

    if instrucciones_analisis:
        partes.append(instrucciones_analisis)

    if temas_repetidos:
        lista = ", ".join(temas_repetidos)
        partes.append(
            f"ANTI-CICLO — Estos temas ya fueron cubiertos en esta conversación. "
            f"NO los repitas a menos que el cliente lo pida explícitamente:\n{lista}"
        )

    if contexto_productos:
        partes.append(contexto_productos)

    if ejemplos_entrenamiento:
        partes.append(ejemplos_entrenamiento)

    addon = _construir_addon_reglas()
    if addon:
        partes.append(addon)

    return "\n\n".join(partes)


# ── History converter ─────────────────────────────────────────────────────────

def _historial_a_mensajes(historial_crm: list[dict], limit: int = 15) -> list[dict]:
    """Converts CRM history to OpenAI [{role, content}] format. Capped at `limit` turns."""
    mensajes: list[dict] = []
    for turno in historial_crm[-limit:]:
        usr = (turno.get("user_message") or "").strip()
        bot = (turno.get("bot_response") or "").strip()
        if usr:
            mensajes.append({"role": "user",      "content": usr})
        if bot:
            mensajes.append({"role": "assistant", "content": bot})
    return mensajes


# ── Background training save ──────────────────────────────────────────────────

async def _guardar_entrenamiento_bg(
    pregunta: str,
    respuesta: str,
    telefono: str,
    instancia: str,
) -> None:
    """Fire-and-forget: saves the Q&A pair to CRM for human review and future training."""
    _url = CRM_URL or os.getenv("CRM_URL", "").rstrip("/")
    _ten = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _tok = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _ten and _tok):
        return
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            await client.post(
                f"{_url}/api/v1/{_ten}/entrenamiento",
                headers={"X-API-Key": _tok},
                json={
                    "pregunta":  pregunta[:2000],
                    "respuesta": respuesta[:2000],
                    "instancia": instancia,
                    "phone":     telefono,
                    "fuente":    "bot_externo",
                },
            )
    except Exception as e:
        print(f"[Entrena·BG] error guardando par: {e}")


# ── Main function ─────────────────────────────────────────────────────────────

async def generar_respuesta(
    mensaje: str,
    historial_crm: list[dict],
    telefono: str,
    instancia: str,
    contact_name: str = "",
) -> str:
    """
    Generates a natural, adaptive response using GPT-4o-mini.

    Security: user input is passed as role:user, NEVER injected into the system prompt.
    Returns the response text, or empty string on error.
    """
    if not OPENAI_API_KEY:
        print("[Responder] OPENAI_API_KEY no configurada")
        return ""

    # Fetch all context sources concurrently
    contexto_productos, ejemplos, instrucciones = await asyncio.gather(
        _obtener_productos(),
        _buscar_entrenamiento(mensaje),
        _obtener_analisis_entrenamiento(instancia),
    )

    temas_repetidos = _detectar_temas_repetidos(historial_crm)

    # Derive bot name: env var takes priority, else use formatted instancia name
    bot_nombre = BOT_NOMBRE or _nombre_desde_instancia(instancia)

    system_prompt = _construir_system_prompt(
        contexto_productos=contexto_productos,
        ejemplos_entrenamiento=ejemplos,
        instrucciones_analisis=instrucciones,
        temas_repetidos=temas_repetidos,
        bot_nombre=bot_nombre,
    )

    # Build messages: history + current user message
    mensajes = _historial_a_mensajes(historial_crm)
    mensajes.append({"role": "user", "content": mensaje})  # user input never in system prompt

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "gpt-4o-mini",
                    "temperature": 0.7,
                    "max_tokens":  500,
                    "messages":    [{"role": "system", "content": system_prompt}] + mensajes,
                },
            )
            resp.raise_for_status()
            respuesta = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Responder] error OpenAI: {e}")
        return ""

    # Save Q&A pair for future human review and training (non-blocking)
    asyncio.create_task(_guardar_entrenamiento_bg(mensaje, respuesta, telefono, instancia))

    return respuesta
