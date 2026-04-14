
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
from Filtro_Mensajes import filtrar_y_clasificar, filtro_mensaje, procesar_contenido, analizar_intencion, OPENAI_API_KEY
from Historial_Conversacion import obtener_historial, formatear_historial_para_ia
from bot_productos import responder_productos

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

    mensaje        = body.get("mensaje", "")
    tipo_contenido = body.get("tipo_contenido", "texto")
    telefono       = body.get("telefono", "desconocido")
    instancia      = body.get("instancia", "")

    # Campos de media y publicación de Facebook
    url_media      = body.get("url_media", "")
    caption        = body.get("caption", "")
    titulo_fb      = body.get("titulo_fb", "")
    descripcion_fb = body.get("descripcion_fb", "")
    thumbnail_url  = body.get("thumbnail_url", "")

    if not mensaje and tipo_contenido == "texto":
        raise HTTPException(status_code=400, detail="El campo 'mensaje' es obligatorio para tipo texto.")

    print(f"[vitta4] tel={telefono} tipo={tipo_contenido} msg={mensaje[:60]!r}")

    # ── Extraer identificadores alternativos del payload de Evolution ────────
    # Evolution puede usar LID (remoteJid = alias numérico) en vez del número real.
    # También viene remoteJidAlt con el número real. Probamos ambos para ubicar
    # la conversación correcta en la tabla del CRM.
    evo_data = body.get("evo", {}) or {}
    _remote_jid     = (evo_data.get("data", {}) or {}).get("key", {}).get("remoteJid", "") or ""
    _remote_jid_alt = (evo_data.get("data", {}) or {}).get("key", {}).get("remoteJidAlt", "") or ""

    def _strip_jid(jid: str) -> str:
        """Quita el sufijo @s.whatsapp.net / @lid / etc."""
        return jid.split("@")[0] if "@" in jid else jid

    # Candidatos: telefono ya limpio del CRM + ambos extraídos del evo
    _alt_phones: list[str] = []
    for jid in [_remote_jid, _remote_jid_alt]:
        num = _strip_jid(jid)
        if num and num != telefono:
            _alt_phones.append(num)

    # ── Historial de conversación desde CRM ──────────────────────────────────
    historial = await obtener_historial(telefono, alternativas=_alt_phones)
    historial_texto = formatear_historial_para_ia(historial)
    if historial_texto:
        print(f"[vitta4] historial={len(historial)} turnos para tel={telefono}")

    # ── Procesar contenido según tipo (audio/imagen/facebook/texto) ──────────
    procesado = await procesar_contenido(
        tipo_contenido=tipo_contenido,
        mensaje=mensaje,
        url_media=url_media,
        caption=caption,
        titulo_fb=titulo_fb,
        descripcion_fb=descripcion_fb,
        thumbnail_url=thumbnail_url,
    )
    texto_para_bot = procesado["texto_procesado"]

    # Reanalizar intención con contexto del historial si hay conversación previa
    if historial_texto:
        texto_con_historial = f"{historial_texto}\nUsuario: {texto_para_bot}"
        intencion_con_ctx = await analizar_intencion(texto_con_historial)
        if intencion_con_ctx:
            procesado["intencion"] = intencion_con_ctx

    print(f"[vitta4] texto_procesado={texto_para_bot[:80]!r} intencion={procesado.get('intencion')}")

    # ── Filtro unificado: detecta pub_facebook + clasifica en un solo paso ───
    clasificacion = await filtrar_y_clasificar(
        mensaje=mensaje,
        tipo_original=tipo_contenido,
        url_publicidad=thumbnail_url,
        telefono=telefono,
        remote_jid=_remote_jid or telefono,
        historial_texto=historial_texto,
    )
    print(f"[vitta4] filtro={clasificacion}")

    if clasificacion["filtro_active"]:
        tipo_bloqueo = clasificacion["tipo_bloqueo"] or "inapropiado"
        if tipo_bloqueo in ("inapropiado", "prompt_injection"):
            print(f"[vitta4] BLOQUEADO ({tipo_bloqueo}) — tel={telefono}")
            return {
                "tipo_bloqueo": "inapropiado",
                "motivo": f"Mensaje bloqueado: {tipo_bloqueo}.",
            }
        print(f"[vitta4] PAUSADO ({tipo_bloqueo}) — tel={telefono}")
        return {
            "tipo_bloqueo": "irrelevante",
            "motivo": "Mensaje fuera del contexto del negocio.",
        }

    # Si el filtro detectó pub_facebook, forzamos el tipo para que el flujo lo trate igual
    if clasificacion["pub_facebook"] and tipo_contenido != "publicacion_facebook":
        tipo_contenido = "publicacion_facebook"
        # Re-procesar con el tipo correcto si aún no fue procesado como FB
        if procesado.get("tipo_contenido") != "publicacion_facebook":
            procesado = await procesar_contenido(
                tipo_contenido="publicacion_facebook",
                mensaje=mensaje,
                url_media=url_media,
                caption=caption,
                titulo_fb=titulo_fb,
                descripcion_fb=descripcion_fb,
                thumbnail_url=thumbnail_url,
            )
            texto_para_bot = procesado["texto_procesado"]

    # ── Respuesta según intención detectada ──────────────────────────────────
    intencion = procesado.get("intencion") or {}
    tipo_intencion = (intencion.get("intencion") or "").lower()

    if tipo_intencion in ("productos", "mixto"):
        print(f"[vitta4] flujo=productos — tel={telefono}")
        respuesta = await responder_productos(
            texto_usuario=texto_para_bot,
            historial_texto=historial_texto,
            analisis=procesado.get("analisis") or {},
            intencion=intencion,
            instancia=instancia,
        )
        if respuesta:
            return {
                "success": True,
                "respuesta": respuesta,
                "texto_procesado": procesado["etiqueta"],
            }
        # Si falta OPENAI_API_KEY, cae al mensaje de prueba

    # ── Respuesta genérica (sin flujo específico o sin clave OpenAI) ──────────
    return {
        "success": True,
        "respuesta": RESPUESTA_PRUEBA,
        "texto_procesado": procesado["etiqueta"],   # label con ícono para historial de conversación
    }