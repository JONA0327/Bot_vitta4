"""Adjunta imagen y video del catálogo cuando el bot da información DETALLADA de un producto.

No depende de que la IA emita marcadores: si la respuesta es larga (detallada) y menciona exactamente un
producto del catálogo, se manda su imagen y video (una sola vez por contacto y producto).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata

from .crm_client import crm

logger = logging.getLogger("vitta4.medios")

MIN_DETALLE = 350  # caracteres: por debajo es una frase corta (etapa de indagar), no se mandan medios
_TTL = 600
_cache: dict = {"t": 0.0, "items": []}
# ponytail: en memoria; se pierde al reiniciar el bot (reenviaría una vez). Persistir si molesta.
_enviados: dict[str, set] = {}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\b4life\b", " ", s)).strip()


def _elegir(productos: list[dict], texto: str) -> dict | None:
    """El único producto del catálogo mencionado en `texto` (None si 0 o varios: ambiguo)."""
    t = f" {_norm(texto)} "
    citados = [p for p in productos if f" {p['n']} " in t]
    return citados[0] if len(citados) == 1 else None


async def _productos() -> list[dict]:
    if time.time() - _cache["t"] > _TTL:
        items = []
        for fila in await crm.prompt_catalogo("productos", per_page=200):
            d = fila.get("datos", fila)
            p = d.get("producto")
            nombre = (p.get("items") or [""])[0] if isinstance(p, dict) else str(p or "")
            n = _norm(nombre)
            if len(n) >= 3 and (d.get("imagen") or d.get("video")):
                items.append({"id": fila.get("id"), "n": n, "imagen": d.get("imagen"), "video": d.get("video")})
        _cache.update(t=time.time(), items=items)
    return _cache["items"]


async def medios_para(telefono: str, texto: str) -> list[dict]:
    if len(texto) < MIN_DETALLE:
        return []
    try:
        p = _elegir(await _productos(), texto)
    except Exception:
        logger.exception("No se pudo consultar el catálogo para adjuntar medios")
        return []
    vistos = _enviados.setdefault(telefono, set())
    if not p or p["id"] in vistos:
        return []
    vistos.add(p["id"])
    logger.info("Adjuntando medios del producto %s a tel=%s", p["n"], telefono)
    return [{"tipo": tipo, "url": p[tipo]} for tipo in ("imagen", "video") if p.get(tipo)]
