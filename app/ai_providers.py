"""
Interfaz unificada para llamar a OpenAI, Gemini, DeepSeek o Claude.

Todas las funciones reciben (system_prompt, historial, mensaje_usuario) y
devuelven el texto de la respuesta (o None si falla), sin importarle al
resto del bot qué proveedor está detrás — así AI_PROVIDER en el .env
cambia el motor sin tocar nada más.

historial: lista de {"role": "user"|"assistant", "content": "..."}
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import settings

logger = logging.getLogger("vitta4.ai_providers")

_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


async def generar_respuesta(provider: str, system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    provider = (provider or settings.ai_provider).lower()
    try:
        if provider == "openai":
            return await _openai(system_prompt, historial, mensaje_usuario)
        if provider == "gemini":
            return await _gemini(system_prompt, historial, mensaje_usuario)
        if provider == "deepseek":
            return await _deepseek(system_prompt, historial, mensaje_usuario)
        if provider == "claude":
            return await _claude(system_prompt, historial, mensaje_usuario)
        logger.error("Proveedor de IA desconocido: %s", provider)
        return None
    except Exception:
        logger.exception("Error generando respuesta con proveedor=%s", provider)
        return None


# ── OpenAI / DeepSeek (API compatible con OpenAI) ───────────────────────────

async def _chat_openai_compatible(base_url: str, api_key: str, model: str, system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    if not api_key:
        logger.warning("Falta API key para %s", base_url)
        return None

    messages = [{"role": "system", "content": system_prompt}]
    messages += historial
    messages.append({"role": "user", "content": mensaje_usuario})

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages},
        )
    if not resp.is_success:
        logger.warning("HTTP %s de %s: %s", resp.status_code, base_url, resp.text[:300])
        return None
    data = resp.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content")


async def _openai(system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    p = settings.providers
    return await _chat_openai_compatible("https://api.openai.com/v1", p.openai_key, p.openai_model, system_prompt, historial, mensaje_usuario)


async def _deepseek(system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    p = settings.providers
    return await _chat_openai_compatible("https://api.deepseek.com", p.deepseek_key, p.deepseek_model, system_prompt, historial, mensaje_usuario)


# ── Gemini ───────────────────────────────────────────────────────────────

async def _gemini(system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    p = settings.providers
    if not p.gemini_key:
        logger.warning("Falta GEMINI_API_KEY")
        return None

    contents = []
    for turno in historial:
        role = "model" if turno.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turno.get("content", "")}]})
    contents.append({"role": "user", "parts": [{"text": mensaje_usuario}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{p.gemini_model}:generateContent?key={p.gemini_key}"
    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
    if not resp.is_success:
        logger.warning("HTTP %s de Gemini: %s", resp.status_code, resp.text[:300])
        return None
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Respuesta de Gemini sin texto: %s", data)
        return None


# ── Claude (Anthropic) ──────────────────────────────────────────────────────

async def _claude(system_prompt: str, historial: list[dict], mensaje_usuario: str) -> Optional[str]:
    p = settings.providers
    if not p.claude_key:
        logger.warning("Falta ANTHROPIC_API_KEY")
        return None

    messages = list(historial) + [{"role": "user", "content": mensaje_usuario}]

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": p.claude_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": p.claude_model,
                "system": system_prompt,
                "messages": messages,
                "max_tokens": 1024,
            },
        )
    if not resp.is_success:
        logger.warning("HTTP %s de Claude: %s", resp.status_code, resp.text[:300])
        return None
    data = resp.json()
    try:
        return data["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Respuesta de Claude sin texto: %s", data)
        return None
