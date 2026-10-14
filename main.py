"""
vitta4_Bot_3.0 — punto de entrada.

El CRM reenvía cada mensaje de WhatsApp entrante (que no esté pausado/
bloqueado) a POST /webhook. Este endpoint responde de inmediato (ack) y
procesa el mensaje en segundo plano, para que el CRM nunca se quede
esperando a que la IA termine de pensar.

Ráfagas de mensajes: si el mismo número manda varios mensajes seguidos, no
se dispara una respuesta por cada uno. El primer mensaje abre una ventana de
`settings.debounce_seconds` (por defecto 40s); todo lo que llegue en esa
ventana se encola, y al cerrarse se procesan todos juntos como un solo
mensaje combinado, para que el bot responda una única vez a la ráfaga.

Arranque:
    pip install -r requirements.txt
    cp .env.example .env   # y llena los valores
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.conversation import procesar_mensaje
from app.vit_loader import PromptSet

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vitta4.main")

app = FastAPI(title="vitta4_Bot_3.0")

prompts = PromptSet(
    reglas_path=settings.vit_reglas_path,
    filtros_path=settings.vit_filtros_path,
    reload_on_change=settings.vit_reload_on_change,
    source=settings.vit_source,
    cache_seconds=settings.vit_cache_seconds,
)

# Ráfagas de mensajes pendientes por teléfono: {telefono: {"mensajes": [...], "payload_base": {...}}}
_pendientes: dict[str, dict] = {}


@app.on_event("startup")
async def startup() -> None:
    problemas = settings.validar()
    if problemas:
        logger.warning("Configuración incompleta — revisa tu .env:")
        for p in problemas:
            logger.warning("  - %s", p)
    else:
        logger.info("Configuración OK — proveedor de IA: %s (filtro: %s)", settings.ai_provider, settings.effective_filter_provider)

    logger.info("Fuente de los prompts (.vit): %s (cache=%ss)", settings.vit_source, settings.vit_cache_seconds)
    logger.info("Ventana de ráfaga de mensajes: %ss", settings.debounce_seconds)

    if not (await prompts.reglas()).strip():
        origen = settings.vit_reglas_path if settings.vit_source == "file" else "GET /prompt/reglas"
        logger.warning("El Prompt de reglas (%s) está vacío o no existe.", origen)
    if not (await prompts.filtros()).strip():
        origen = settings.vit_filtros_path if settings.vit_source == "file" else "GET /prompt/filtros"
        logger.info("El Prompt de filtros (%s) está vacío — el bot no clasificará mensajes.", origen)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "ai_provider": settings.ai_provider,
        "filter_ai_provider": settings.effective_filter_provider,
        "config_ok": not settings.validar(),
    }


@app.post("/webhook")
async def webhook(request: Request, x_bot_secret: str | None = Header(default=None)) -> JSONResponse:
    if settings.bot_webhook_secret and x_bot_secret != settings.bot_webhook_secret:
        raise HTTPException(status_code=401, detail="X-Bot-Secret inválido o ausente")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body inválido, se esperaba JSON")

    telefono = str(payload.get("telefono", "")).strip()
    if not telefono:
        raise HTTPException(status_code=422, detail="Falta 'telefono' en el payload")

    # Ack inmediato — el procesamiento (IA, catálogos, envío) sigue en segundo
    # plano, así el CRM/Evolution no esperan a que termine de responder.
    _encolar_mensaje(telefono, payload)
    return JSONResponse({"status": "recibido"})


def _encolar_mensaje(telefono: str, payload: dict) -> None:
    mensaje = str(payload.get("mensaje", "")).strip()
    entrada = _pendientes.get(telefono)

    if entrada is None:
        _pendientes[telefono] = {
            "mensajes": [mensaje] if mensaje else [],
            "payload_base": payload,
        }
        asyncio.create_task(_flush_tras_espera(telefono))
    else:
        if mensaje:
            entrada["mensajes"].append(mensaje)
        # Metadata más reciente (contact_name, remote_jid, instancia, etc.)
        entrada["payload_base"] = payload


async def _flush_tras_espera(telefono: str) -> None:
    await asyncio.sleep(settings.debounce_seconds)
    entrada = _pendientes.pop(telefono, None)
    if entrada is None:
        return

    payload_final = dict(entrada["payload_base"])
    payload_final["mensaje"] = "\n".join(m for m in entrada["mensajes"] if m).strip()

    if len(entrada["mensajes"]) > 1:
        logger.info("Ráfaga de %d mensajes agrupada para tel=%s", len(entrada["mensajes"]), telefono)

    try:
        await procesar_mensaje(payload_final, prompts)
    except Exception:
        logger.exception("Error procesando ráfaga de mensajes: %s", telefono)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.bot_host, port=settings.bot_port, reload=False)
