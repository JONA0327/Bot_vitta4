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

# El CRM (POST /blocked_numbers) solo acepta estos tipos de bloqueo.
_TIPOS_BLOQUEO = {"inapropiado", "prompt_injection"}

_INSTRUCCION_FORMATO = (
    "\n\n---\n"
    "Responde ÚNICAMENTE con el JSON del FORMATO DE SALIDA de este prompt (sin texto alrededor, "
    "sin markdown). Si el mensaje debe continuar normal, responde solo con la palabra PASAR."
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
    if limpio.upper().startswith(("NORMAL", "PASAR")):
        return None

    data = _extraer_json(limpio)
    if not data:
        logger.warning("Respuesta del filtro no es JSON reconocible, se ignora: %s", limpio[:200])
        return None

    if str(data.get("status", "")).upper() == "PASAR":
        return None

    base = {
        "numero_baneado": str(data.get("Numero_Baneado") or telefono),
        "numero_remote": str(data.get("Numero_Remote") or remote_jid or telefono),
        "motivo": str(data.get("Motivo_Bloqueo") or data.get("Motivo") or "Clasificado por el filtro"),
        "instancia": str(data.get("instancia") or instancia),
        "mensaje": mensaje,
        "etiqueta": data.get("etiqueta"),
    }
    control = str(data.get("control", "")).strip("{} ").upper()
    tipo = str(data.get("tipo_bloqueo", "")).strip().lower()

    # PAUSAR/CONTINUAR no son bloqueos: van por bot-send como comando {TAG}
    # (queda en "Pausadas", no en "Irrelevantes"). "irrelevante" legacy = pausar.
    if control in ("PAUSAR", "CONTINUAR") or tipo == "irrelevante":
        return {**base, "control": control or "PAUSAR"}

    if control in ("BANEADO", "BLOQUEADO") or tipo:
        # solicitud_baja y similares se registran como "inapropiado" (único tipo que acepta el CRM)
        return {**base, "control": "BLOQUEAR", "tipo_bloqueo": tipo if tipo in _TIPOS_BLOQUEO else "inapropiado"}

    logger.warning("Clasificación del filtro no reconocida, se ignora: %s", limpio[:200])
    return None
