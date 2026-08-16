"""
Orquesta el procesamiento de UN mensaje entrante: filtro → historial →
respuesta de IA → anti-ciclo → envío (bot-send).

Esta es la pieza que "respeta el control de estado": el filtro puede cortar
aquí mismo (bloquear/pausar) sin generar respuesta, y el anti-ciclo puede
inyectar {PAUSAR} si detecta que el bot se está repitiendo — el CRM
(BotSendApiController) es quien interpreta esos comandos {TAG} al recibir
la llamada a /bot-send.
"""
from __future__ import annotations

import difflib
import logging

from . import filtro, responder
from .config import settings
from .crm_client import crm
from .vit_loader import PromptSet

logger = logging.getLogger("vitta4.conversation")

UMBRAL_SIMILITUD = 0.87


def _historial_para_ia(filas: list[dict]) -> list[dict]:
    """Convierte el historial de /memoria al formato {role, content} para la IA."""
    historial: list[dict] = []
    for fila in filas:
        user_msg = (fila.get("user_message") or "").strip()
        bot_msg = (fila.get("bot_response") or "").strip()
        if user_msg:
            historial.append({"role": "user", "content": user_msg})
        if bot_msg:
            historial.append({"role": "assistant", "content": bot_msg})
    return historial


def _ultimas_respuestas_bot(filas: list[dict], n: int) -> list[str]:
    respuestas = [(f.get("bot_response") or "").strip() for f in filas if (f.get("bot_response") or "").strip()]
    return respuestas[-n:]


def _parecidas(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _bot_en_bucle(nueva_respuesta: str, anteriores: list[str], minimo: int) -> bool:
    """True si `nueva_respuesta` es casi idéntica a las últimas `minimo` respuestas del bot."""
    if len(anteriores) < minimo:
        return False
    ultimas = anteriores[-minimo:]
    return all(_parecidas(nueva_respuesta, r) >= UMBRAL_SIMILITUD for r in ultimas)


async def procesar_mensaje(payload: dict, prompts: PromptSet) -> None:
    """
    payload: lo que el CRM reenvía en POST /webhook —
      { telefono, remote_jid, instancia, mensaje, contact_name, tenant, api_token, timestamp }
    """
    telefono = str(payload.get("telefono", "")).strip()
    remote_jid = str(payload.get("remote_jid", "")).strip() or telefono
    instancia = str(payload.get("instancia", "")).strip()
    mensaje = str(payload.get("mensaje", "")).strip()
    contact_name = payload.get("contact_name")

    if not telefono or not instancia:
        logger.warning("Payload incompleto del CRM, se ignora: %s", payload)
        return

    # 1) Filtro — clasifica el mensaje. Si aplica, bloquea/pausa y NO responde
    #    (mismo criterio documentado: "si el mensaje es normal, no llames a
    #    blocked_numbers y deja que el bot responda con el Prompt de reglas").
    try:
        clasificacion = await filtro.clasificar_mensaje(
            mensaje=mensaje,
            telefono=telefono,
            remote_jid=remote_jid,
            instancia=instancia,
            filtros_prompt=await prompts.filtros(),
        )
    except Exception:
        logger.exception("Error ejecutando el filtro — se continúa como mensaje normal")
        clasificacion = None

    if clasificacion:
        logger.info("Filtro clasificó tel=%s como %s", telefono, clasificacion["tipo_bloqueo"])
        await crm.blocked_numbers(
            numero_baneado=clasificacion["numero_baneado"],
            numero_remote=clasificacion["numero_remote"],
            tipo_bloqueo=clasificacion["tipo_bloqueo"],
            motivo=clasificacion["motivo"],
            instancia=clasificacion["instancia"],
            mensaje=clasificacion["mensaje"],
            etiqueta=clasificacion.get("etiqueta"),
        )
        return

    # 2) Historial reciente (memoria persistida en el CRM — no en el bot, así
    #    el anti-ciclo funciona igual aunque el bot se reinicie o corra en
    #    varias instancias).
    try:
        filas = await crm.memoria(telefono, limit=settings.memoria_limit, instancia=instancia)
    except Exception:
        logger.exception("No se pudo obtener /memoria — se responde sin historial")
        filas = []

    historial = _historial_para_ia(filas)

    # 3) Generar respuesta con el Prompt de reglas (catálogos resueltos).
    respuesta = await responder.generar(mensaje, historial, prompts)

    if not respuesta:
        logger.error("La IA no devolvió respuesta para tel=%s — se registra sin enviar texto", telefono)
        await crm.bot_send(telefono=telefono, instancia=instancia, remote_jid=remote_jid, respuesta="", user_message=mensaje, contact_name=contact_name, solo_registrar=True, status="bot_sin_respuesta")
        return

    # 4) Anti-ciclo: si el bot está a punto de repetir su propia respuesta
    #    N veces seguidas, no la reenvía tal cual — agrega {PAUSAR} para que
    #    el CRM detenga al bot en esa conversación y quede para un humano.
    anteriores = _ultimas_respuestas_bot(filas, settings.max_respuestas_repetidas)
    if _bot_en_bucle(respuesta, anteriores, settings.max_respuestas_repetidas):
        logger.warning("Bucle detectado para tel=%s — pausando y escalando a humano", telefono)
        respuesta = (
            "Voy a pedirle a un miembro de nuestro equipo que te ayude con esto directamente. "
            "En un momento te contactan. {PAUSAR}"
        )

    # 5) Enviar — el CRM parsea los comandos {TAG}, los quita del texto visible,
    #    aplica el estado, y entrega el mensaje limpio por WhatsApp.
    resultado = await crm.bot_send(
        telefono=telefono,
        instancia=instancia,
        remote_jid=remote_jid,
        respuesta=respuesta,
        user_message=mensaje,
        contact_name=contact_name,
    )

    if not resultado.get("success"):
        logger.error("bot-send falló para tel=%s: %s", telefono, resultado)
    elif resultado.get("enviado") is False:
        # El CRM registró la respuesta pero NO la envió por WhatsApp — el bot
        # está apagado globalmente o esa instancia está pausada desde el panel.
        logger.info("bot-send registrado sin enviar (bot apagado/instancia pausada) tel=%s", telefono)
