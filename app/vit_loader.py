"""
Carga los archivos .vit (exportados desde Configuración → Prompt del CRM) y
resuelve los tags [CATALOGO_x] / [CATALOGO_x.campo] contra los catálogos
reales del tenant, vía la API de consulta del CRM.

Los .vit NO traen los datos resueltos (el CRM los exporta con los tags tal
cual) — resolverlos aquí, en cada mensaje, es lo que permite que el bot
siempre responda con el catálogo actualizado sin tener que re-exportar el
prompt cada vez que cambia un precio.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from .crm_client import crm

logger = logging.getLogger("vitta4.vit_loader")

_TAG_RE = re.compile(r"\[CATALOGO_([A-Z0-9_]+)(?:\.[a-zA-Z0-9_]+)?\]")

# Cache de archivos locales: {ruta: (mtime, contenido)}
_cache: dict[str, tuple[float, str]] = {}

# Cache de prompts pedidos por API: {tipo: (fetched_at_monotonic, contenido)}
_cache_api: dict[str, tuple[float, str]] = {}


async def _cargar_vit_api(tipo: str, ttl_seconds: int) -> str:
    """Pide el .vit directo al CRM (GET /prompt/reglas o /prompt/filtros).

    Cachea en memoria por `ttl_seconds` para no pegarle al CRM en cada
    mensaje — con ttl_seconds=0 lo pide siempre (útil mientras ajustas el
    prompt en el panel y quieres verlo reflejado al instante).
    """
    cached = _cache_api.get(tipo)
    if cached and ttl_seconds > 0 and (time.monotonic() - cached[0]) < ttl_seconds:
        return cached[1]

    try:
        contenido = await (crm.prompt_reglas() if tipo == "reglas" else crm.prompt_filtros())
    except Exception:
        logger.exception("No se pudo obtener /prompt/%s del CRM", tipo)
        return cached[1] if cached else ""

    _cache_api[tipo] = (time.monotonic(), contenido)
    return contenido


def cargar_vit(path: str, reload_on_change: bool = True) -> str:
    """Lee un archivo .vit del disco. Cachea por mtime si reload_on_change=True."""
    if not os.path.exists(path):
        logger.warning("Archivo .vit no encontrado: %s", path)
        return ""

    if not reload_on_change and path in _cache:
        return _cache[path][1]

    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(path, "r", encoding="utf-8") as f:
        contenido = f.read()

    _cache[path] = (mtime, contenido)
    return contenido


def extraer_modulos_referenciados(texto: str) -> set[str]:
    """Devuelve el conjunto de tags de MÓDULO (sin el .campo) mencionados en el texto."""
    return {f"CATALOGO_{m}" for m in _TAG_RE.findall(texto)}


async def resolver_catalogos(texto: str, max_registros_por_modulo: int = 60) -> str:
    """
    Si el texto menciona tags [CATALOGO_x...], le anexa al final un bloque con
    los datos reales de esos catálogos, para que la IA pueda resolverlos.
    Si no hay tags, devuelve el texto sin tocar (rápido, sin llamadas a la API).
    """
    tags_modulo = extraer_modulos_referenciados(texto)
    if not tags_modulo:
        return texto

    try:
        modulos = await crm.prompt_modulos()
    except Exception:
        logger.exception("No se pudo obtener /modulos — se responde sin datos de catálogo")
        return texto

    tag_a_modulo = {m["tag"]: m for m in modulos if m.get("tag") in tags_modulo}
    if not tag_a_modulo:
        logger.warning("Tags de catálogo mencionados en el prompt pero ningún módulo activo coincide: %s", tags_modulo)
        return texto

    bloques = []
    for tag, modulo in tag_a_modulo.items():
        try:
            registros = await crm.prompt_catalogo(modulo["slug"], per_page=max_registros_por_modulo)
        except Exception:
            logger.exception("Error consultando catálogo '%s'", modulo["slug"])
            continue

        campos = [c["slug"] for c in modulo.get("campos", [])]
        filas = []
        for r in registros:
            datos = r.get("datos", r)
            fila = {c: datos.get(c) for c in campos if c in datos}
            fila["id"] = r.get("id")
            filas.append(fila)

        bloques.append(
            f"### Datos actuales de {modulo['nombre']} ([{tag}] / {tag}.<campo>)\n"
            f"{_formatear_filas(filas)}"
        )

    if not bloques:
        return texto

    return (
        texto
        + "\n\n---\n"
        + "Usa EXCLUSIVAMENTE los siguientes datos reales para resolver los tags de catálogo mencionados arriba "
        + "(no inventes valores que no estén aquí):\n\n"
        + "\n\n".join(bloques)
    )


def _formatear_filas(filas: list[dict]) -> str:
    if not filas:
        return "(sin registros)"
    lineas = []
    for fila in filas:
        partes = ", ".join(f"{k}: {v}" for k, v in fila.items() if v not in (None, ""))
        lineas.append(f"- {partes}")
    return "\n".join(lineas)


class PromptSet:
    """
    Agrupa el prompt de reglas y el de filtros.

    source="api"  → los pide directo al CRM (GET /prompt/reglas, /prompt/filtros),
                    cacheados `cache_seconds` en memoria (recomendado).
    source="file" → los lee de disco (`reglas_path` / `filtros_path`), recargando
                    si cambia el mtime cuando `reload_on_change=True`.
    """

    def __init__(
        self,
        reglas_path: str,
        filtros_path: str,
        reload_on_change: bool = True,
        source: str = "api",
        cache_seconds: int = 60,
    ) -> None:
        self.reglas_path = reglas_path
        self.filtros_path = filtros_path
        self.reload_on_change = reload_on_change
        self.source = source
        self.cache_seconds = cache_seconds

    async def reglas(self) -> str:
        if self.source == "file":
            return cargar_vit(self.reglas_path, self.reload_on_change)
        return await _cargar_vit_api("reglas", self.cache_seconds)

    async def filtros(self) -> str:
        if self.source == "file":
            return cargar_vit(self.filtros_path, self.reload_on_change)
        return await _cargar_vit_api("filtros", self.cache_seconds)

    async def reglas_resueltas(self) -> str:
        return await resolver_catalogos(await self.reglas())
