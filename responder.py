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

# Shared catalog caches (same for all contacts)
_PRODUCT_CACHE: dict = {}        # {"data": str, "expires_at": float}  — formatted text
_PRODUCT_CACHE_LIST: list = []   # raw producto dicts for fuzzy name matching
_PAQUETES_CACHE_LIST: list = []  # raw paquete dicts (for package→product cross-reference)
_TESTIMONIOS_CACHE_LIST: list = [] # raw testimonio dicts (for condition-based discovery)

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


def _extract_list(res: list | dict | None) -> list:
    if res is None:
        return []
    if isinstance(res, list):
        return res
    return res.get("data") or []


async def _obtener_productos() -> str:
    """Returns formatted product + package + testimonio catalog. Cached for _PRODUCT_TTL seconds."""
    global _PRODUCT_CACHE_LIST, _PAQUETES_CACHE_LIST, _TESTIMONIOS_CACHE_LIST
    now = time.monotonic()
    if _PRODUCT_CACHE.get("expires_at", 0.0) > now:
        return _PRODUCT_CACHE["data"]

    res_productos, res_paquetes, res_testimonios = await asyncio.gather(
        _crm_get("productos",   {"per_page": 100}),
        _crm_get("paquetes",    {"per_page": 50}),
        _crm_get("testimonios", {"per_page": 50}),
    )

    productos   = _extract_list(res_productos)
    paquetes    = _extract_list(res_paquetes)
    testimonios = _extract_list(res_testimonios)

    # Keep raw lists separately for cross-reference
    _PRODUCT_CACHE_LIST    = productos
    _PAQUETES_CACHE_LIST   = paquetes
    _TESTIMONIOS_CACHE_LIST = testimonios

    partes: list[str] = []
    if productos:
        partes.append(_formatear_lista_productos(productos, "CATÁLOGO DE PRODUCTOS 4LIFE"))

    resultado = "\n\n".join(partes)
    _PRODUCT_CACHE["data"]       = resultado
    _PRODUCT_CACHE["expires_at"] = now + _PRODUCT_TTL
    return resultado


def _buscar_productos_fuzzy(nombres_buscados: list[str]) -> list[dict]:
    """Finds products in the cached catalog that match any of the partial names given.

    A match occurs when any search word (≥4 chars) appears inside the product name,
    or the product name contains any search word. Case-insensitive.
    """
    if not nombres_buscados or not _PRODUCT_CACHE_LIST:
        return []
    encontrados: list[dict] = []
    for prod in _PRODUCT_CACHE_LIST:
        nombre_prod = (_pick_field(prod, ["NOMBRE", "nombre", "name", "NAME"]) or "").lower()
        for buscado in nombres_buscados:
            palabras = [w for w in buscado.lower().split() if len(w) >= 4]
            if not palabras:
                palabras = [buscado.lower().strip()]
            if any(p in nombre_prod or nombre_prod in p for p in palabras):
                if prod not in encontrados:
                    encontrados.append(prod)
                break
    return encontrados


def _formatear_productos_detectados(nombres_buscados: list[str]) -> str:
    """Returns a formatted block with full catalog info for matched products."""
    matches = _buscar_productos_fuzzy(nombres_buscados)
    if not matches:
        # No match found — just include the names as-is so GPT knows what was detected
        return (
            "PRODUCTO(S) IDENTIFICADO(S) EN ESTE MENSAJE: "
            + ", ".join(nombres_buscados)
            + "\n(Busca en el catálogo y proporciona la información más relevante)"
        )
    return _formatear_lista_productos(matches, "PRODUCTO(S) IDENTIFICADO(S) EN ESTE MENSAJE")


def _obtener_imagen_producto(nombres_buscados: list[str]) -> str | None:
    """Returns the first image URL found among products matching the given names."""
    if not nombres_buscados:
        return None
    matches = _buscar_productos_fuzzy(nombres_buscados)
    for prod in matches:
        img = _pick_field(prod, ["imagen", "IMAGEN", "image_url", "IMAGE_URL", "foto", "FOTO", "img"])
        if img and isinstance(img, str) and img.startswith("http"):
            return img
    return None


def _obtener_video_testimonio(nombres_buscados: list[str] | None = None) -> str | None:
    """Returns testimony video URL from matched product or from global config."""
    # Try product-specific testimony first
    if nombres_buscados:
        matches = _buscar_productos_fuzzy(nombres_buscados)
        for prod in matches:
            vid = _pick_field(prod, ["video_testimonio", "VIDEO_TESTIMONIO", "testimonio_url", "video_url"])
            if vid and isinstance(vid, str) and vid.startswith("http"):
                return vid
    # Fall back to global testimony video from rules/config
    if _REGLAS_ACTIVAS:
        vid = _REGLAS_ACTIVAS.get("video_testimonio") or _REGLAS_ACTIVAS.get("testimonio_url") or ""
        if vid and isinstance(vid, str) and vid.startswith("http"):
            return vid
    return None


# ── Package & Testimony cross-reference helpers ───────────────────────────────

def _get_linked_product_ids(rec: dict) -> list[int]:
    """Extracts the linked product ID array from a paquete or testimonio record.

    Handles: [16, 13, 22], [{"id": 16}], "16,13,22", or nested inside 'datos'.
    """
    datos = rec.get("datos") if isinstance(rec.get("datos"), dict) else rec

    raw = None
    for source in (datos, rec) if datos is not rec else (rec,):
        for key in ["productos", "PRODUCTOS", "productos_sugeridos", "PRODUCTOS SUGERIDOS",
                    "product_ids", "items"]:
            val = source.get(key)
            if val is not None:
                raw = val
                break
        if raw is not None:
            break

    if raw is None:
        return []

    if isinstance(raw, list):
        ids: list[int] = []
        for item in raw:
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, dict):
                pk = item.get("id") or item.get("producto_id") or item.get("pivot", {}).get("catalog_record_id")
                if pk:
                    ids.append(int(pk))
            elif isinstance(item, str) and item.strip().isdigit():
                ids.append(int(item.strip()))
        return ids
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    return []


def _productos_por_ids(ids: list[int]) -> list[dict]:
    """Fetches product records from cache by their numeric IDs."""
    if not ids or not _PRODUCT_CACHE_LIST:
        return []
    id_set = set(ids)
    return [p for p in _PRODUCT_CACHE_LIST if p.get("id") in id_set]


def _buscar_paquetes_para_productos(nombres: list[str]) -> list[dict]:
    """Given product names detected in an image, finds paquetes that contain those products.

    Returns paquetes where at least 2 of the detected product IDs appear.
    """
    if not nombres or not _PAQUETES_CACHE_LIST:
        return []

    product_matches = _buscar_productos_fuzzy(nombres)
    if not product_matches:
        return []

    detected_ids = {p.get("id") for p in product_matches if p.get("id")}
    if not detected_ids:
        return []

    resultado: list[dict] = []
    for paquete in _PAQUETES_CACHE_LIST:
        paq_ids = set(_get_linked_product_ids(paquete))
        coincidencias = len(paq_ids & detected_ids)
        if coincidencias >= 2:
            resultado.append(paquete)

    return resultado


def _expandir_paquete_con_productos(paquete: dict) -> str:
    """Formats a paquete with name, description and all its products expanded."""
    nombre = _pick_field(paquete, ["NOMBRE PAQUETE", "NOMBRE", "nombre", "nombre_paquete", "name"]) or "Paquete"
    desc   = _pick_field(paquete, ["DESCRIPCION", "DESCRIPCIÓN", "descripcion", "description"]) or ""

    lineas = [f"📦 PAQUETE: {nombre}"]
    if desc:
        lineas.append(f"Descripción: {str(desc)[:200]}")

    prod_ids = _get_linked_product_ids(paquete)
    prods    = _productos_por_ids(prod_ids)
    if prods:
        lineas.append("Productos incluidos en el paquete:")
        for p in prods:
            pnom  = _pick_field(p, ["NOMBRE", "nombre", "name"]) or "Producto"
            ppre  = _pick_field(p, ["PRECIO", "precio", "price"])
            pdesc = _pick_field(p, ["DESCRIPCION", "descripcion", "DESCRIPCION_CORTA",
                                    "descripcion_corta", "description"])
            linea = f"  • {pnom}"
            if ppre:
                linea += f" (${ppre})"
            if pdesc:
                linea += f" — {str(pdesc)[:120]}"
            lineas.append(linea)

    return "\n".join(lineas)


def _formatear_paquetes_contexto() -> str:
    """Formats all packages catalog for GPT context."""
    if not _PAQUETES_CACHE_LIST:
        return ""
    partes = ["CATÁLOGO DE PAQUETES 4LIFE:"]
    for pq in _PAQUETES_CACHE_LIST:
        partes.append(_expandir_paquete_con_productos(pq))
    return "\n\n".join(partes)


def _formatear_testimonios_contexto() -> str:
    """Formats the testimonios catalog for GPT context (condition → suggested products)."""
    if not _TESTIMONIOS_CACHE_LIST:
        return ""

    lineas = ["CATÁLOGO DE TESTIMONIOS (condición → productos sugeridos):"]
    for t in _TESTIMONIOS_CACHE_LIST:
        condicion = _pick_field(t, ["CONDICION CRONICA", "CONDICION CRÓNICA", "condicion",
                                     "condicion_cronica", "CONDICION", "condition"]) or ""
        desc      = _pick_field(t, ["DESCRIPCION", "DESCRIPCIÓN", "descripcion", "description"]) or ""

        if not condicion:
            continue

        linea = f"• Condición: {condicion}"
        if desc:
            linea += f"\n  Info: {str(desc)[:200]}"

        prod_ids = _get_linked_product_ids(t)
        prods    = _productos_por_ids(prod_ids)
        if prods:
            nombres_prods = [_pick_field(p, ["NOMBRE", "nombre"]) or "?" for p in prods]
            linea += f"\n  Productos sugeridos: {', '.join(nombres_prods)}"

            # Also check if any package groups these products
            coincidentes = [pq for pq in _PAQUETES_CACHE_LIST
                            if len(set(_get_linked_product_ids(pq)) & set(prod_ids)) >= 2]
            if coincidentes:
                nombres_paq = [
                    _pick_field(pq, ["NOMBRE PAQUETE", "NOMBRE", "nombre"]) or "Paquete"
                    for pq in coincidentes
                ]
                linea += f"\n  Paquete recomendado: {', '.join(nombres_paq)}"

        lineas.append(linea)

    return "\n".join(lineas)


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


# Nombres comunes masculinos/femeninos para inferir género del nombre de instancia
_NOMBRES_FEMENINOS = {
    "valeria", "maria", "ana", "lucia", "laura", "sofia", "carolina", "andrea",
    "patricia", "alejandra", "gabriela", "isabel", "claudia", "monica", "veronica",
    "fernanda", "daniela", "natalia", "diana", "paola", "rosa", "martha", "elena",
    "irene", "beatriz", "cristina", "alicia", "sara", "silvia", "lorena",
    "vitta", "vitalia", "esperanza", "gloria", "hilda", "norma", "yolanda",
}
_NOMBRES_MASCULINOS = {
    "carlos", "jose", "juan", "miguel", "luis", "pedro", "antonio", "jorge",
    "francisco", "manuel", "rafael", "david", "pablo", "roberto", "alejandro",
    "gabriel", "daniel", "sergio", "ricardo", "fernando", "jonathan", "jonatan",
    "andres", "mario", "javier", "hector", "omar", "ivan", "edgar", "oscar",
    "enrique", "raul", "ruben", "felix", "victor", "gerardo", "alberto", "rodrigo",
}


def _detectar_genero_instancia(instancia: str) -> str:
    """Returns 'femenino', 'masculino' or 'neutro' based on words in the instance name."""
    palabras = instancia.lower().replace("-", " ").replace("_", " ").split()
    for p in palabras:
        if p in _NOMBRES_FEMENINOS:
            return "femenino"
        if p in _NOMBRES_MASCULINOS:
            return "masculino"
    return "neutro"


def _construir_system_prompt(
    contexto_productos: str,
    ejemplos_entrenamiento: str,
    instrucciones_analisis: str,
    temas_repetidos: list[str],
    bot_nombre: str = "",
    contact_name: str = "",
    es_primer_mensaje: bool = False,
    productos_detectados: list[str] | None = None,
    genero_bot: str = "neutro",
    tipo_flujo: str = "texto",          # "imagen_multiprod" | "imagen_monoprod" | "texto"
    contexto_paquetes: str = "",        # formatted paquetes block (for imagen_multiprod)
    contexto_testimonios: str = "",     # formatted testimonios block (for texto flow)
    paquetes_detectados: list[dict] | None = None,  # matching packages for imagen_multiprod
) -> str:
    hora       = _saludo_hora()
    nombre_bot = bot_nombre or "Asesora"
    productos_detectados  = productos_detectados  or []
    paquetes_detectados   = paquetes_detectados   or []

    # Título según género
    if genero_bot == "femenino":
        titulo   = "asesora"
        calida   = "cálida"
        empatica = "empática"
    elif genero_bot == "masculino":
        titulo   = "asesor"
        calida   = "cálido"
        empatica = "empático"
    else:
        titulo   = "asesor/a"
        calida   = "cálido/a"
        empatica = "empático/a"

    partes: list[str] = [
        f"""Eres {nombre_bot}, {titulo} de bienestar con productos 4Life. Eres {calida}, genuina y {empatica}.

Tu única función es orientar a las personas sobre los productos 4Life con información honesta del catálogo.
No hablas de ningún otro tema — si el contexto de la conversación te lleva a algo fuera de 4Life, redirige con naturalidad.

CÓMO CONVERSAS:
• Te adaptas al ritmo y tono de cada persona. No sigues un guión fijo.
• Eres directa sin ser fría, y empática sin exagerar. Cero muletillas ("¡Claro!", "¡Por supuesto!", "¡Perfecto!", etc.).
• No abrumas: das la información que es útil en ese momento, no toda de golpe.
• Haces como máximo una pregunta por mensaje y esperas la respuesta.
• No repites lo que ya dijiste en mensajes anteriores a menos que el cliente lo pida.
• Nunca inventas precios, beneficios ni productos. Si no está en el catálogo, no existe.
• Hora actual en México: {hora}."""
    ]

    # ── Primer contacto ───────────────────────────────────────────────────────
    if es_primer_mensaje:
        partes.append(
            f"Es el primer mensaje de este cliente.\n"
            f"Salúdalo con '{hora}' de forma natural, preséntate como {nombre_bot} ({titulo} 4Life) "
            f"y pide su nombre — nunca asumas que ya lo sabes."
        )

    # ── Contexto según tipo de interacción ───────────────────────────────────
    if tipo_flujo == "imagen_multiprod":
        nombres_str = ", ".join(productos_detectados)
        if paquetes_detectados:
            nombres_paq = [
                _pick_field(pq, ["NOMBRE PAQUETE", "NOMBRE", "nombre"]) or "Paquete"
                for pq in paquetes_detectados
            ]
            partes.append(
                f"El cliente envió una imagen con varios productos de 4Life: {nombres_str}.\n"
                f"Esos productos forman parte del paquete: {', '.join(nombres_paq)}.\n"
                f"Presenta el paquete de forma natural — explica qué es, por qué esa combinación tiene sentido "
                f"y qué aporta cada producto. Cuando termines, si crees que un testimonio le ayudaría, pregúntale. "
                f"Añade [[PAUSE]] cuando quieras que responda antes de continuar.\n\n"
                f"Información del paquete:\n{contexto_paquetes}"
            )
        else:
            partes.append(
                f"El cliente envió una imagen con varios productos de 4Life: {nombres_str}.\n"
                f"No hay un paquete exacto para esa combinación. Presenta cada producto de forma natural "
                f"con sus beneficios y precio. Cuando termines puedes preguntarle si quiere ver un testimonio. "
                f"Añade [[PAUSE]] cuando esperes que responda."
            )

    elif tipo_flujo == "imagen_monoprod":
        nombres_str = ", ".join(productos_detectados)
        partes.append(
            f"El cliente envió una imagen del producto: {nombres_str}.\n"
            f"Habla de él de forma natural usando la info del catálogo. "
            f"Cuando termines puedes ofrecerle ver un testimonio. "
            f"Añade [[PAUSE]] cuando esperes que responda."
        )

    else:  # texto / lead Facebook
        partes.append(
            f"El cliente llegó por texto o desde una pauta — no envió imagen.\n"
            f"Tu objetivo es entender qué necesita y orientarlo hacia el producto o paquete adecuado del catálogo.\n"
            f"Para llegar ahí de forma natural: si aún no sabes su nombre, pídelo. "
            f"Entiende si ya conoce 4Life o es nuevo. Si es nuevo, una presentación muy breve basta. "
            f"Con pocas preguntas (no más de dos) descubre qué condición o molestia tiene. "
            f"Luego busca en los TESTIMONIOS si hay alguna condición que coincida — si la hay, "
            f"usa los productos sugeridos de ese testimonio para recomendar; revisa si forman un PAQUETE. "
            f"Si no hay testimonio relevante, busca el paquete más adecuado; si tampoco aplica ninguno, "
            f"ve directo a los PRODUCTOS y justifica con su descripción por qué podrían ayudarle. "
            f"Solo recomienda lo que existe en el catálogo y tiene sentido para esa condición. "
            f"Si nada aplica, añade [[HUMAN_ESCALATE]] — no inventes.\n"
            f"Añade [[PAUSE]] cada vez que esperes que el cliente responda antes de continuar."
        )

    # ── Datos del catálogo ────────────────────────────────────────────────────
    if contexto_productos:
        partes.append(contexto_productos)

    if tipo_flujo == "texto" and contexto_testimonios:
        partes.append(contexto_testimonios)

    if tipo_flujo in ("texto", "imagen_multiprod") and contexto_paquetes and not paquetes_detectados:
        partes.append(contexto_paquetes)

    if productos_detectados and tipo_flujo in ("imagen_monoprod", "imagen_multiprod"):
        bloque_prod = _formatear_productos_detectados(productos_detectados)
        if bloque_prod:
            partes.append(bloque_prod)

    # ── Marcadores ────────────────────────────────────────────────────────────
    partes.append(
        "MARCADORES — añade solo cuando corresponda, al final del mensaje:\n"
        "• [[PAUSE]] — esperas que el cliente responda antes de continuar.\n"
        "• [[SEND_TESTIMONY]] [[PAUSE]] — el cliente quiere ver el testimonio en video.\n"
        "• [[HUMAN_ESCALATE]] — ningún producto del catálogo aplica a lo que el cliente necesita."
    )

    # ── Anti-ciclo ────────────────────────────────────────────────────────────
    if temas_repetidos:
        lista = ", ".join(temas_repetidos)
        partes.append(f"Ya tocaste estos temas antes — no los repitas a menos que el cliente lo pida: {lista}")

    # ── Instrucciones de análisis IA ──────────────────────────────────────────
    if instrucciones_analisis:
        partes.append(instrucciones_analisis)

    # ── Ejemplos y reglas CRM ─────────────────────────────────────────────────
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
    productos_detectados: list[str] | None = None,
    es_primer_mensaje: bool = False,
) -> dict:
    """
    Generates a natural, adaptive response using GPT-4o-mini.

    Security: user input is passed as role:user, NEVER injected into the system prompt.
    Returns dict: {respuesta, medios, pause, send_testimony, human_escalate}
    """
    if not OPENAI_API_KEY:
        print("[Responder] OPENAI_API_KEY no configurada", flush=True)
        return {"respuesta": "", "medios": [], "pause": False, "send_testimony": False, "human_escalate": False}

    # Fetch all context sources concurrently (also pre-loads paquetes/testimonios into cache)
    print(f"[Responder] {telefono} cargando contexto (catálogo + entrenamiento + análisis)…", flush=True)
    contexto_productos, ejemplos, instrucciones = await asyncio.gather(
        _obtener_productos(),
        _buscar_entrenamiento(mensaje),
        _obtener_analisis_entrenamiento(instancia),
    )

    temas_repetidos = _detectar_temas_repetidos(historial_crm)

    bot_nombre = _nombre_desde_instancia(instancia) or BOT_NOMBRE or "Asesora"
    genero_bot = _detectar_genero_instancia(instancia)

    prods = productos_detectados or []

    # ── Determinar tipo de flujo ──────────────────────────────────────────────
    paquetes_detectados: list[dict] = []
    contexto_paquetes   = ""
    contexto_testimonios = ""

    if len(prods) >= 2:
        tipo_flujo = "imagen_multiprod"
        paquetes_detectados = _buscar_paquetes_para_productos(prods)
        if paquetes_detectados:
            partes_paq = [_expandir_paquete_con_productos(pq) for pq in paquetes_detectados]
            contexto_paquetes = "\n\n".join(partes_paq)
            print(f"[Responder] {telefono} 📦 paquete(s) coincidente(s): "
                  f"{[_pick_field(pq, ['NOMBRE PAQUETE','NOMBRE','nombre']) for pq in paquetes_detectados]}",
                  flush=True)
        else:
            contexto_paquetes = _formatear_paquetes_contexto()
            print(f"[Responder] {telefono} 📦 múltiples productos, sin paquete exacto", flush=True)
    elif len(prods) == 1:
        tipo_flujo = "imagen_monoprod"
        print(f"[Responder] {telefono} 🔍 flujo monoproducto: {prods[0]}", flush=True)
    else:
        tipo_flujo = "texto"
        contexto_paquetes    = _formatear_paquetes_contexto()
        contexto_testimonios = _formatear_testimonios_contexto()
        print(f"[Responder] {telefono} 💬 flujo texto/Facebook lead"
              f"  testimonios={'sí' if contexto_testimonios else 'no'}"
              f"  paquetes={'sí' if contexto_paquetes else 'no'}",
              flush=True)

    print(
        f"[Responder] {telefono}  bot={bot_nombre}({genero_bot})  flujo={tipo_flujo}  "
        f"primer_msg={es_primer_mensaje}  prods={prods or 'ninguno'}  "
        f"temas_repetidos={temas_repetidos or 'ninguno'}",
        flush=True,
    )

    system_prompt = _construir_system_prompt(
        contexto_productos=contexto_productos,
        ejemplos_entrenamiento=ejemplos,
        instrucciones_analisis=instrucciones,
        temas_repetidos=temas_repetidos,
        bot_nombre=bot_nombre,
        contact_name=contact_name,
        es_primer_mensaje=es_primer_mensaje,
        productos_detectados=prods,
        genero_bot=genero_bot,
        tipo_flujo=tipo_flujo,
        contexto_paquetes=contexto_paquetes,
        contexto_testimonios=contexto_testimonios,
        paquetes_detectados=paquetes_detectados,
    )

    # Build messages: history + current user message
    mensajes = _historial_a_mensajes(historial_crm)
    mensajes.append({"role": "user", "content": mensaje})  # user input never in system prompt

    print(
        f"[Responder] {telefono} → GPT-4o-mini  "
        f"sys_prompt={len(system_prompt)}ch  "
        f"mensajes={len(mensajes)} (incluyendo actual)",
        flush=True,
    )

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
                    "max_tokens":  600,
                    "messages":    [{"role": "system", "content": system_prompt}] + mensajes,
                },
            )
            resp.raise_for_status()
            respuesta_raw = resp.json()["choices"][0]["message"]["content"].strip()
            tokens = resp.json().get("usage", {})
            print(
                f"[Responder] {telefono} ✅ GPT respondió  "
                f"tokens=({tokens.get('prompt_tokens','?')}p + {tokens.get('completion_tokens','?')}c)",
                flush=True,
            )
    except Exception as e:
        print(f"[Responder] {telefono} ❌ error OpenAI: {e}", flush=True)
        return {"respuesta": "", "medios": [], "pause": False, "send_testimony": False, "human_escalate": False}

    # ── Parsear marcadores especiales ─────────────────────────────────────────
    import re as _re
    pause           = "[[PAUSE]]"           in respuesta_raw
    send_testimony  = "[[SEND_TESTIMONY]]"  in respuesta_raw
    human_escalate  = "[[HUMAN_ESCALATE]]"  in respuesta_raw

    # Quitar los marcadores del texto que se enviará al cliente
    respuesta = _re.sub(
        r"\[\[PAUSE\]\]|\[\[SEND_TESTIMONY\]\]|\[\[HUMAN_ESCALATE\]\]",
        "",
        respuesta_raw,
    ).strip()

    # ── Construir lista de medios ─────────────────────────────────────────────
    medios: list[dict] = []

    # Imagen del producto (imagen_monoprod o imagen_multiprod)
    if prods and tipo_flujo in ("imagen_monoprod", "imagen_multiprod"):
        img_url = _obtener_imagen_producto(prods)
        if img_url:
            nombre_prod = prods[0] if prods else "Producto"
            medios.append({"tipo": "imagen", "url": img_url, "caption": nombre_prod})
            print(f"[Responder] {telefono} 📷 adjuntando imagen de producto: {img_url[:80]}", flush=True)

    # Video de testimonio si GPT lo indica
    if send_testimony:
        vid_url = _obtener_video_testimonio(prods or None)
        if vid_url:
            medios.append({"tipo": "video", "url": vid_url, "caption": "Testimonio 🌟"})
            print(f"[Responder] {telefono} 🎥 adjuntando video testimonio: {vid_url[:80]}", flush=True)
        else:
            print(f"[Responder] {telefono} ⚠️ [[SEND_TESTIMONY]] pero no hay video configurado", flush=True)

    # Save Q&A pair for future human review and training (non-blocking)
    asyncio.create_task(_guardar_entrenamiento_bg(mensaje, respuesta, telefono, instancia))
    print(f"[Entrena·BG] {telefono} par guardado en background", flush=True)

    if human_escalate:
        print(f"[Responder] {telefono} 🚨 [[HUMAN_ESCALATE]] — sin producto adecuado, escalando a humano", flush=True)

    return {
        "respuesta":       respuesta,
        "medios":          medios,
        "pause":           pause,
        "send_testimony":  send_testimony,
        "human_escalate":  human_escalate,
    }
