"""
BotLogger.py
────────────
Envía logs del bot Python al CRM para visualizarlos en la vista
"Actividad · Bots Externos" filtrados por instancia.

Variables de entorno requeridas (mismas que Historial_Conversacion.py):
  CRM_URL        — URL base del CRM, ej. https://vitta4.autodevsystems.com
  CRM_TENANT     — slug del tenant,   ej. vitta4
  CRM_API_TOKEN  — token de la API

Uso:
    from BotLogger import bot_log

    await bot_log("Jonathan", "info", "vitta4", "mensaje aquí")
    await bot_log("Jonathan", "error", "FB·analisis", "error=...", {"url": "..."})
"""

import os
import asyncio
from typing import Any
import httpx

CRM_URL       = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT    = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "")

# Buffer para enviar en lote y no bloquear el flujo principal
_buffer: list[dict] = []
_flush_lock = asyncio.Lock()

_NIVELES_VALIDOS = {"debug", "info", "warning", "error"}


async def bot_log(
    instancia: str,
    nivel: str,
    origen: str,
    mensaje: str,
    contexto: dict[str, Any] | None = None,
) -> None:
    """
    Agrega un log al buffer y lo envía al CRM de forma asíncrona.
    No lanza excepciones — si falla el envío, se imprime localmente.
    """
    nivel = nivel.lower() if nivel.lower() in _NIVELES_VALIDOS else "info"

    # Siempre imprimir en consola (journalctl)
    prefijo = f"[{origen}]" if origen else ""
    print(f"[BotLog·{nivel.upper()}] {prefijo} {mensaje}")

    if not CRM_URL or not CRM_TENANT or not CRM_API_TOKEN:
        return

    entry = {
        "instancia": instancia or "desconocida",
        "nivel":     nivel,
        "origen":    origen,
        "mensaje":   mensaje,
        "contexto":  contexto,
    }

    # Enviar directamente en background sin bloquear
    asyncio.create_task(_enviar([entry]))


async def _enviar(entries: list[dict]) -> None:
    """Envía el lote de logs al CRM."""
    url = f"{CRM_URL}/api/v1/{CRM_TENANT}/bot-logs"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={
                    "X-API-Key": CRM_API_TOKEN,
                    "Content-Type": "application/json",
                },
                json=entries,
            )
            resp.raise_for_status()
    except Exception as e:
        # Solo imprimir — no propagar para no romper el flujo del bot
        print(f"[BotLogger] no se pudo enviar log al CRM: {e}")
