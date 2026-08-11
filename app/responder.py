"""Genera la respuesta principal del bot usando el Prompt de reglas (resuelto)."""
from __future__ import annotations

import logging
from typing import Optional

from .ai_providers import generar_respuesta
from .config import settings
from .vit_loader import PromptSet

logger = logging.getLogger("vitta4.responder")

_PROMPT_VACIO_FALLBACK = (
    "Eres un asistente virtual de atención al cliente. Responde de forma breve, "
    "clara y amable en español. Si no sabes algo, dilo honestamente."
)


async def generar(mensaje: str, historial: list[dict], prompts: PromptSet) -> Optional[str]:
    system_prompt = await prompts.reglas_resueltas()
    if not system_prompt.strip():
        logger.warning("Prompt de reglas vacío — usando fallback genérico")
        system_prompt = _PROMPT_VACIO_FALLBACK

    return await generar_respuesta(settings.ai_provider, system_prompt, historial, mensaje)
