
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Bot Vitta4", version="1.0.0")


def get_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def get_settings() -> dict[str, Any]:
    return {
        "base_url": get_env("CEREBRO_URL", "VITTA_BASE_URL"),
        "api_key": get_env("CEREBRO_TOKEN", "BOT_API_KEY", "VITTA_API_KEY"),
        "tenant": get_env("CEREBRO_TENANT", "VITTA_TENANT"),
        "timeout": float(get_env("CEREBRO_TIMEOUT", "VITTA_TIMEOUT") or "30"),
        "fallback": get_env(
            "BOT_FALLBACK_RESPONSE",
            "VITTA_FALLBACK_RESPONSE",
        ) or "En este momento no pude procesar tu mensaje. Intenta de nuevo en unos minutos.",
    }


def build_ai_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload = {"mensaje": body["mensaje"]}

    for field in (
        "telefono",
        "historial",
        "system_prompt",
        "contexto",
        "proveedor",
        "modelo",
        "instancia",
        "remote_jid",
        "enviar_whatsapp",
    ):
        value = body.get(field)
        if value is not None:
            payload[field] = value

    return payload


async def procesar_con_cerebro(body: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    tenant = body.get("tenant") or settings["tenant"]

    if not settings["base_url"] or not settings["api_key"]:
        raise HTTPException(
            status_code=500,
            detail="Faltan CEREBRO_URL y/o CEREBRO_TOKEN en variables de entorno.",
        )

    if not tenant:
        raise HTTPException(
            status_code=400,
            detail="Falta el tenant en el payload y no existe CEREBRO_TENANT configurado.",
        )

    url = f"{settings['base_url'].rstrip('/')}/api/v1/{tenant}/ai/procesar"
    payload = build_ai_payload(body)

    async with httpx.AsyncClient(timeout=settings["timeout"]) as client:
        response = await client.post(
            url,
            json=payload,
            headers={
                "X-API-Key": settings["api_key"],
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "base_url_configurada": bool(settings["base_url"]),
        "api_key_configurada": bool(settings["api_key"]),
        "tenant_configurado": bool(settings["tenant"]),
    }

@app.post("/vitta4")
async def vitta4(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    mensaje = body.get("mensaje")
    if not mensaje:
        raise HTTPException(status_code=400, detail="El campo 'mensaje' es obligatorio.")

    telefono = body.get("telefono", "")
    tenant = body.get("tenant") or get_settings()["tenant"]

    print("=== Mensaje recibido ===")
    print(f"Teléfono : {telefono}")
    print(f"Mensaje  : {mensaje}")
    print(f"Tenant   : {tenant}")
    print("=======================")

    try:
        data = await procesar_con_cerebro(body)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text.strip()
        settings = get_settings()
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "respuesta": settings["fallback"],
                "error": f"Error HTTP al llamar al sistema: {exc.response.status_code}. {error_body}",
            },
        )
    except httpx.RequestError as exc:
        settings = get_settings()
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "respuesta": settings["fallback"],
                "error": f"No se pudo conectar al sistema externo: {exc}",
            },
        )

    respuesta = data.get("respuesta")
    if not respuesta:
        settings = get_settings()
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "respuesta": settings["fallback"],
                "error": data.get("error", "El sistema no devolvió el campo 'respuesta'."),
            },
        )

    return {
        "success": True,
        "respuesta": respuesta,
        "proveedor": data.get("proveedor"),
        "modelo": data.get("modelo"),
    }