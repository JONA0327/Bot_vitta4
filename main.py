
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
from Filtro_Mensajes import filtro_mensaje

load_dotenv()

app = FastAPI(title="Bot Vitta4", version="1.0.0")

INCOMING_TOKEN = os.getenv("CRM_INCOMING_TOKEN")
RESPUESTA_PRUEBA = os.getenv(
    "BOT_RESPUESTA_PRUEBA",
    "¡Hola! Soy el Bot Vitta4. Tu mensaje llegó correctamente. 🤖",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/vitta4")
async def vitta4(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="El body debe ser JSON válido.") from exc

    # Validar token entrante del CRM (solo si CRM_INCOMING_TOKEN está definido)
    if INCOMING_TOKEN:
        request_token = body.get("api_token") or request.headers.get("X-API-Token")
        if request_token != INCOMING_TOKEN:
            raise HTTPException(status_code=401, detail="Token de acceso inválido.")

    mensaje = body.get("mensaje")
    if not mensaje:
        raise HTTPException(status_code=400, detail="El campo 'mensaje' es obligatorio.")

    telefono = body.get("telefono", "desconocido")
    print(f"[vitta4] tel={telefono} msg={mensaje}")

    # ── Filtro de contenido ──────────────────────────────────────────────────
    bloqueo = await filtro_mensaje(mensaje)
    if bloqueo == "inapropiado":
        print(f"[vitta4] BLOQUEADO (inapropiado) — tel={telefono}")
        return {
            "tipo_bloqueo": "inapropiado",
            "motivo": "Mensaje con contenido inapropiado detectado por filtro.",
        }
    if bloqueo == "irrelevante":
        print(f"[vitta4] PAUSADO (irrelevante) — tel={telefono}")
        return {
            "tipo_bloqueo": "irrelevante",
            "motivo": "Mensaje fuera del contexto del negocio.",
        }

    # ── Respuesta normal ─────────────────────────────────────────────────────
    return {"success": True, "respuesta": RESPUESTA_PRUEBA}