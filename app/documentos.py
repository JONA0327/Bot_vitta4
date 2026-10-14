"""Contexto de documentos para la IA: fragmentos de los archivos (PDF/imagen → Markdown) subidos a los módulos
del CRM que responden a lo que el cliente pregunta (ej. un producto). Si no hay archivos o nada coincide, no agrega nada."""
from __future__ import annotations

import logging

from .crm_client import crm

logger = logging.getLogger("vitta4.documentos")

_ENCABEZADO = (
    "\n\n---\n"
    "DOCUMENTOS DE REFERENCIA (material oficial de la empresa). Si el cliente pregunta por un producto, "
    "apóyate en el fragmento de ESE producto para dar sus datos (ingredientes, presentación, modo de uso). "
    "Usa solo lo que está escrito aquí o en los datos del catálogo; no inventes nada más.\n\n"
)


def construir_bloque(fragmentos: list[dict]) -> str:
    if not fragmentos:
        return ""
    partes = [f"### {f.get('modulo', '')} — {f.get('archivo', '')}\n{f.get('fragmento', '')}" for f in fragmentos]
    return _ENCABEZADO + "\n\n".join(partes)


async def contexto(mensaje: str, historial: list[dict]) -> str:
    # La consulta incluye lo último que dijo el cliente: el producto puede haberse nombrado mensajes antes.
    previos = [m["content"] for m in historial if m.get("role") == "user"][-2:]
    consulta = " ".join(previos + [mensaje]).strip()
    if len(consulta) < 4:
        return ""
    try:
        fragmentos = await crm.archivos_relevantes(consulta)
    except Exception:
        logger.exception("No se pudieron consultar los documentos — se responde sin ellos")
        return ""
    logger.info("Documentos de referencia: %d fragmento(s)", len(fragmentos))
    return construir_bloque(fragmentos)
