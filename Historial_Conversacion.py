"""
Historial_Conversacion.py
─────────────────────────
Consulta el historial de conversaciones del CRM para un número de teléfono,
usando el endpoint:

  GET {CRM_URL}/api/v1/{CRM_TENANT}/conversaciones/phone/{phone}?limit={limit}
  Header: X-API-Key: {CRM_API_TOKEN}

Variables de entorno requeridas:
  CRM_URL        — URL base del CRM, ej. https://vitta4.autodevsystems.com
  CRM_TENANT     — slug del tenant,   ej. vitta4
  CRM_API_TOKEN  — token de la API,   ej. abc123
"""

import os
import httpx

CRM_URL       = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT    = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT   = float(os.getenv("CRM_TIMEOUT", "8"))


async def obtener_historial(telefono: str, alternativas: list[str] | None = None, limit: int = 15) -> list[dict]:
    """
    Busca el historial del número en el CRM probando `telefono` y luego
    cada candidato en `alternativas` hasta encontrar resultados.

    Esto cubre el caso en que Evolution usa LID (remoteJid = alias)
    en vez del número de teléfono real (remoteJidAlt).

    Cada item retornado: {user_message, bot_response, created_at}
    Retorna [] si falta configuración o hay un error en todos los intentos.
    """
    if not CRM_URL or not CRM_TENANT or not CRM_API_TOKEN:
        return []

    # Construir lista de candidatos únicos (sin vacíos)
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
            print(f"[Historial] ERROR candidato={candidato!r} url={url} error={e}")
            continue

        if not isinstance(rows, list) or not rows:
            print(f"[Historial] sin resultados para candidato={candidato!r}")
            continue

        print(f"[Historial] encontrado {len(rows)} registros para candidato={candidato!r}")
        # Encontrado — ordenar cronológicamente y retornar
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

    print(f"[Historial] ningún candidato retornó resultados: {candidatos}")
    return []


def formatear_historial_para_ia(historial: list[dict], max_turnos: int = 10) -> str:
    """
    Convierte el historial a un string legible para incluir en prompts de IA.
    Limita a los últimos `max_turnos` para no superar contexto.
    """
    if not historial:
        return ""

    turnos = historial[-max_turnos:]
    lineas = []
    for t in turnos:
        usr = (t.get("user_message") or "").strip()
        bot = (t.get("bot_response") or "").strip()
        if usr:
            lineas.append(f"Usuario: {usr}")
        if bot:
            lineas.append(f"Bot: {bot}")

    return "\n".join(lineas)
