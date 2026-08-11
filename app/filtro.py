"""
Ejecuta el Prompt de Filtros contra un mensaje entrante y devuelve la
clasificación (si aplica) en el mismo formato que espera
POST /api/v1/{tenant}/blocked_numbers del CRM.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .ai_providers import generar_respuesta
from .config import settings

logger = logging.getLogger("vitta4.filtro")

_TIPOS_VALIDOS = {"inapropiado", "prompt_injection", "irrelevante"}

_INSTRUCCION_FORMATO = (
    "\n\n---\n"
    "Responde ÚNICAMENTE con la palabra NORMAL si el mensaje no requiere ninguna acción, "
    "o con un JSON (sin texto alrededor, sin markdown) con este formato exacto si sí la requiere:\n"
    '{ "tipo_bloqueo": "inapropiado" | "prompt_injection" | "irrelevante", "Motivo_Bloqueo": "..." }'
)


def _extraer_json(texto: str) -> Optional[dict]:
    """Parsea JSON aunque venga envuelto en ```json ... ``` o con texto alrededor."""
    texto = texto.strip()
    # Quitar fences de markdown si existen
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", texto, re.DOTALL)
    if fence:
        texto = fence.group(1).strip()

    try:
        return json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        pass

    # Último intento: buscar el primer bloque { ... } dentro del texto
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def clasificar_mensaje(
    mensaje: str,
    telefono: str,
    remote_jid: str,
    instancia: str,
    filtros_prompt: str,
) -> Optional[dict]:
    """
    Devuelve None si el mensaje es normal (no requiere acción), o un dict
    listo para pasar a CrmClient.blocked_numbers(**dict) si el filtro decidió
    bloquear/pausar al contacto.
    """
    if not filtros_prompt.strip():
        return None

    system = filtros_prompt + _INSTRUCCION_FORMATO
    texto = await generar_respuesta(settings.effective_filter_provider, system, [], mensaje)

    if not texto:
        logger.warning("El filtro no devolvió respuesta — se trata el mensaje como normal")
        return None

    limpio = texto.strip()
    if limpio.upper().startswith("NORMAL"):
        return None

    data = _extraer_json(limpio)
    if not data:
        logger.warning("Respuesta del filtro no es JSON reconocible, se ignora: %s", limpio[:200])
        return None

    tipo_bloqueo = str(data.get("tipo_bloqueo", "")).strip().lower()
    if tipo_bloqueo not in _TIPOS_VALIDOS:
        logger.warning("tipo_bloqueo '%s' no reconocido, se ignora la clasificación", tipo_bloqueo)
        return None

    return {
        "numero_baneado": str(data.get("Numero_Baneado") or telefono),
        "numero_remote": str(data.get("Numero_Remote") or remote_jid or telefono),
        "tipo_bloqueo": tipo_bloqueo,
        "motivo": str(data.get("Motivo_Bloqueo") or "Clasificado por el filtro"),
        "instancia": str(data.get("instancia") or instancia),
        "mensaje": mensaje,
        "etiqueta": data.get("etiqueta"),
    }
