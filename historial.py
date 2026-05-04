"""
historial.py
────────────
Consulta el historial de conversaciones del CRM para un número de teléfono.
"""

import os
import httpx

CRM_URL       = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT    = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT   = float(os.getenv("CRM_TIMEOUT", "8"))


async def obtener_historial(
    telefono: str,
    alternativas: list[str] | None = None,
    limit: int = 15,
) -> list[dict]:
    """Obtiene historial del CRM. Retorna lista de {user_message, bot_response, created_at}."""
    if not CRM_URL or not CRM_TENANT or not CRM_API_TOKEN:
        return []

    candidatos: list[str] = []
    for c in [telefono] + (alternativas or []):
        c = (c or "").strip()
        if c and c not in candidatos:
            candidatos.append(c)

    for candidato in candidatos:
        url = f"{CRM_URL}/api/v1/{CRM_TENANT}/conversaciones/phone/{candidato}"
        try:
            async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
                resp = await client.get(
                    url,
                    headers={"X-API-Key": CRM_API_TOKEN},
                    params={"limit": limit},
                )
                resp.raise_for_status()
                rows = resp.json().get("data", [])
        except Exception as e:
            print(f"[Historial] error candidato={candidato!r}: {e}")
            continue

        if not isinstance(rows, list) or not rows:
            continue

        rows_sorted = sorted(rows, key=lambda r: r.get("created_at", ""))
        return [
            {
                "user_message": r.get("user_message", ""),
                "bot_response": r.get("bot_response") or "",
                "created_at":   r.get("created_at", ""),
            }
            for r in rows_sorted
            if r.get("user_message")
        ]

    return []


def formatear_historial(historial: list[dict]) -> str:
    """Convierte lista de turnos en texto 'Usuario: ...\nBot: ...'"""
    if not historial:
        return ""
    lineas: list[str] = []
    for turno in historial:
        usr = (turno.get("user_message") or "").strip()
        bot = (turno.get("bot_response") or "").strip()
        if usr:
            lineas.append(f"Usuario: {usr}")
        if bot:
            lineas.append(f"Bot: {bot}")
    return "\n".join(lineas)


def contar_turnos_bot(historial_texto: str) -> int:
    """Cuenta cuántas respuestas del bot hay en el historial."""
    return historial_texto.count("\nBot:") + (1 if historial_texto.startswith("Bot:") else 0)
