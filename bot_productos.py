"""
bot_productos.py
────────────────
Flujo de atención para usuarios interesados en PRODUCTOS de 4Life.

Se invoca cuando el analizador de intención detecta intencion="productos"
(o "mixto" con productos mencionados).

Entradas:
  - texto_usuario    : mensaje procesado (puede ser transcripción, descripción de imagen, etc.)
  - historial_texto  : historial formateado de la conversación (puede estar vacío)
  - analisis         : dict con el detalle del análisis de contenido (image items, FB data, etc.)
  - intencion        : dict {intencion, confianza, productos_mencionados, resumen}

Salida:
  - str con la respuesta lista para enviar al usuario vía WhatsApp
  - None si no hay clave OpenAI (se usará la respuesta genérica de RESPUESTA_PRUEBA)
"""

import os
import re
import json
import httpx
from datetime import datetime
import zoneinfo

from Filtro_Mensajes import OPENAI_API_KEY, OPENAI_TIMEOUT

# ─────────────────────────────────────────────────────────────────────────────
# Catálogo base de productos 4Life (se amplía con PRODUCTOS_EXTRA_JSON en .env)
# ─────────────────────────────────────────────────────────────────────────────
_CATALOGO_BASE = """
PRODUCTOS DESTACADOS DE 4LIFE:

1. Transfer Factor Plus Tri-Factor Formula
   - El producto estrella. Educa y fortalece el sistema inmune.
   - Eleva la actividad de las células NK hasta un 437%.
   - Indicado para: inmunidad general, personas mayores, post-enfermedad.

2. Transfer Factor Riovida
   - Bebida antioxidante con Transfer Factor + jugos de frutas del bosque.
   - Ideal para: energía diaria, bienestar general, fácil de tomar.

3. Transfer Factor Tri-Factor Formula (clásico)
   - Base inmunológica para toda la familia.
   - Más accesible en precio que el Plus.

4. 4Life Transform Burn
   - Quema de grasa + energía. Con CLA y cafeína natural.
   - Para: pérdida de peso, metabolismo activo.

5. 4Life Transform Go
   - Bebida energética con Transfer Factor. Sin azúcar.

6. ProTF (Proteína con Transfer Factor)
   - Batido de proteína de suero + soporte inmune.
   - Ideal post-entrenamiento o como sustituto de comida.

7. Digestive 4Life
   - Salud digestiva: enzimas + probióticos + Transfer Factor.
   - Para: digestión lenta, inflamación, bienestar intestinal.

8. NanoFactor
   - Fracción nanofiltrada del calostro bovino. Máxima concentración.

9. 4Life Transfer Factor Belle Vie
   - Para la mujer: equilibrio hormonal + bienestar emocional.

10. BioEFA
    - Ácidos grasos esenciales (Omega 3-6-9). Salud cardiovascular y cerebral.
"""

# Se puede sobreescribir/ampliar con JSON en la variable de entorno
_catalogo_extra = os.getenv("PRODUCTOS_EXTRA_JSON", "")
if _catalogo_extra:
    try:
        _CATALOGO_BASE += "\n\nPRODUCTOS ADICIONALES:\n" + _catalogo_extra
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# System prompt del bot de productos
# ─────────────────────────────────────────────────────────────────────────────
_PRODUCTOS_SYSTEM = f"""Eres un asesor de ventas experto de 4Life, una empresa de suplementos y productos de salud. \
Tu misión es ayudar a los prospectos a encontrar el producto ideal para su necesidad y motivarlos a comprar.

INSTRUCCIONES:
- Responde de forma cálida, profesional y entusiasta, como un asesor de confianza.
- Usa el historial de conversación para dar continuidad (no repitas preguntas ya respondidas).
- Si el usuario menciona un producto específico o viene de una publicación, enfócate en ese producto.
- Si no sabe qué busca, pregunta por su principal necesidad o preocupación de salud.
- Nunca inventes precios exactos. Di "contáctame para el precio especial" o "tengo una oferta para ti".
- Siempre termina con un llamado a la acción (CTA): "¿Te gustaría saber más?", "¿Quieres que te envíe info?", etc.
- Responde en el mismo idioma que el usuario (español por defecto).
- Máximo 3 párrafos cortos. Usa emojis ocasionalmente para WhatsApp.

CATÁLOGO DISPONIBLE:
{_CATALOGO_BASE}
"""

# Prompt exclusivo para el primer contacto (PASO 1)
_PASO1_SYSTEM = """Eres una asesora de ventas real de 4Life que atiende por WhatsApp. \
Eres cálida, cercana y natural — la gente NO debe notar que eres un bot.

Tu tarea es escribir el PRIMER mensaje de bienvenida a un cliente nuevo.

INSTRUCCIONES:
- Saluda según la hora indicada (buenos días / buenas tardes / buenas noches) de forma natural.
- Preséntate con el nombre de instancia que se te proporcione.
- Transmite estas ideas de forma fluida y natural (NO las copies literalmente, hazlo tuyo):
  · Hay promociones activas que están ayudando a muchos clientes
  · Estás aquí para orientarlos según lo que buscan
  · Pregunta si les interesa mejorar energía, inmunidad, o generar ingresos
- Después, pide el nombre de forma muy natural, como lo haría una persona real.
  Varía la expresión cada vez: "¿Con quién tengo el gusto?", "¿Cómo te llamo?", "¿Me dices tu nombre?", etc.
- Escribe todo como un solo mensaje cohesivo y natural, NO como bloques separados.
- NO uses frases robóticas ni corporativas.
- Usa emojis con moderación, donde encajen naturalmente.
- Máximo 5–6 líneas en total.
- NO menciones que eres IA, chatbot, sistema automatizado ni nada similar.
"""

# Prompt para PASO 2 — manejo del nombre + indagación de necesidad
_PASO2_SYSTEM = """Eres una asesora de ventas real de 4Life que atiende por WhatsApp. \
Eres cálida, cercana y natural — la gente NO debe notar que eres un bot.

CONTEXTO: Es el SEGUNDO intercambio. En el mensaje anterior le pediste el nombre al cliente.

TU TAREA:
1. Lee el mensaje del cliente con atención:
   - Si dio su nombre: reconócelo de forma natural y cálida \
(ej. "¡Qué gusto [nombre]!" o "Gracias [nombre] 😊"). Usa su nombre de ahora en adelante.
   - Si pregunta para qué quieres su nombre: explica brevemente que es para darle una atención \
más personalizada, pero que no hay problema si prefiere no compartirlo. Continúa igual.
   - Si no dio nombre ni preguntó nada especial: pasa directamente al siguiente punto sin mencionarlo.

2. Después (con nombre o sin él), haz la PREGUNTA DE INDAGACIÓN que se te indica a continuación.

PREGUNTA DE INDAGACIÓN A USAR:
{pregunta_indagacion}

REGLAS ADICIONALES:
- Habla de tú, sé muy natural y humano/a.
- Máximo 3–4 líneas en total.
- Emojis con moderación.
- NO saludes de nuevo (ya saludaste).
- NO menciones que eres IA, bot o sistema automatizado.
- NO repitas información del mensaje anterior.
"""



# ─────────────────────────────────────────────────────────────────────────────
# Helper: saludo PASO 1 — primer contacto
# ─────────────────────────────────────────────────────────────────────────────
def _saludo_hora_mexico() -> str:
    """Devuelve 'buenos días', 'buenas tardes' o 'buenas noches' según hora México."""
    try:
        tz = zoneinfo.ZoneInfo("America/Mexico_City")
    except Exception:
        tz = None
    hora = datetime.now(tz).hour if tz else datetime.now().hour
    if 5 <= hora < 12:
        return "buenos días"
    elif 12 <= hora < 19:
        return "buenas tardes"
    else:
        return "buenas noches"


async def _responder_paso1(instancia: str) -> str | None:
    """Genera el mensaje de bienvenida (PASO 1) usando IA."""
    saludo = _saludo_hora_mexico()
    nombre_bot = instancia.strip() or "4Life"

    prompt_usuario = (
        f"Hora del día: {saludo}.\n"
        f"Tu nombre (instancia): {nombre_bot}.\n"
        "Escribe el mensaje de bienvenida siguiendo todas las instrucciones del sistema."
    )

    messages = [
        {"role": "system", "content": _PASO1_SYSTEM},
        {"role": "user", "content": prompt_usuario},
    ]

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.85,
                    "max_tokens": 350,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _construir_pregunta_indagacion(analisis: dict, intencion: dict) -> str:
    """
    Construye la pregunta de indagación de necesidad según el contexto.
    Si hay productos detectados de FB/imagen: pregunta qué quieren mejorar con esos productos.
    Si no hay productos específicos: pregunta abierta sobre qué buscan.
    """
    # Recolectar productos detectados de todas las fuentes
    productos: list[str] = list(intencion.get("productos_mencionados") or [])
    if isinstance(analisis.get("items"), list):
        for item in analisis["items"]:
            if item and item not in productos:
                productos.append(item)
    producto_principal = analisis.get("producto_mencionado") or ""
    if producto_principal and producto_principal not in productos:
        productos.insert(0, producto_principal)

    tiene_productos_fb = bool(
        productos
        or analisis.get("resumen_para_bot")
        or analisis.get("descripcion_publicacion")
    )

    if tiene_productos_fb:
        lista = ", ".join(productos[:3]) if productos else "este producto"
        return (
            f"El cliente llegó interesado en: {lista}. "
            "Pregunta de forma natural qué quiere MEJORAR o FORTALECER en relación a esos productos "
            "(ej. digestión, energía, inmunidad, peso, etc.). "
            "NO ofrezcas la opción de 'generar ingresos' — el cliente está enfocado en productos. "
            "Personaliza la pregunta mencionando el/los producto(s) de forma natural."
        )
    else:
        return (
            "No hay productos específicos detectados. "
            "Pregunta de forma abierta y natural qué producto busca o qué necesidad quiere resolver "
            "(energía, inmunidad, digestión, peso, bienestar general, etc.). "
            "Puedes incluir 'generar ingresos' como opción si aplica."
        )


async def _responder_paso2(
    texto_usuario: str,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
) -> str | None:
    """PASO 2 — Manejo del nombre + indagación de necesidad."""
    pregunta = _construir_pregunta_indagacion(analisis, intencion)
    system = _PASO2_SYSTEM.format(pregunta_indagacion=pregunta)

    messages = [
        {"role": "system", "content": system},
        {
            "role": "system",
            "content": f"HISTORIAL:\n{historial_texto}",
        },
        {"role": "user", "content": texto_usuario},
    ]

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.8,
                    "max_tokens": 350,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _contar_turnos_bot(historial_texto: str) -> int:
    """Cuenta cuántas respuestas del bot hay en el historial."""
    return historial_texto.count("\nBot:") + (1 if historial_texto.startswith("Bot:") else 0)


# ─────────────────────────────────────────────────────────────────────────────
# Función principal
# ─────────────────────────────────────────────────────────────────────────────
async def responder_productos(
    texto_usuario: str,
    historial_texto: str = "",
    analisis: dict | None = None,
    intencion: dict | None = None,
    instancia: str = "",
) -> str | None:
    """
    Genera una respuesta personalizada sobre productos 4Life.
    Retorna el texto de respuesta o None si no hay OPENAI_API_KEY.
    """
    if not OPENAI_API_KEY:
        return None

    analisis = analisis or {}
    intencion = intencion or {}

    # ── PASO 1: Primer contacto (sin historial) ───────────────────────────────
    if not historial_texto.strip():
        return await _responder_paso1(instancia)

    # ── PASO 2: Segunda respuesta — manejo del nombre + indagación ────────────
    if _contar_turnos_bot(historial_texto) == 1:
        return await _responder_paso2(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 3+: Conversación en curso ────────────────────────────────────────
    contexto_partes: list[str] = []

    # Productos detectados en imagen/publicación
    productos_mencionados: list[str] = intencion.get("productos_mencionados") or []
    if isinstance(analisis.get("items"), list):
        productos_mencionados = list({*productos_mencionados, *analisis["items"]})
    if productos_mencionados:
        contexto_partes.append(f"Productos detectados en el contexto: {', '.join(productos_mencionados)}")

    # Descripción de imagen si viene de análisis visual
    if analisis.get("descripcion"):
        contexto_partes.append(f"Contexto visual: {analisis['descripcion']}")

    # Resumen de publicación de Facebook
    if analisis.get("resumen_para_bot"):
        contexto_partes.append(f"Publicación compartida: {analisis['resumen_para_bot']}")

    # Resumen de intención
    if intencion.get("resumen"):
        contexto_partes.append(f"Intención detectada: {intencion['resumen']}")

    contexto_str = "\n".join(contexto_partes)

    # ── Armar mensajes para ChatGPT ───────────────────────────────────────────
    messages: list[dict] = [{"role": "system", "content": _PRODUCTOS_SYSTEM}]

    # Inyectar historial como contexto (no como mensajes individuales para no
    # confundir el modelo sobre quién habla)
    if historial_texto:
        messages.append({
            "role": "system",
            "content": f"HISTORIAL DE LA CONVERSACIÓN (más antiguo a más reciente):\n{historial_texto}",
        })

    # Contexto adicional del análisis
    if contexto_str:
        messages.append({
            "role": "system",
            "content": f"CONTEXTO ADICIONAL DEL MENSAJE ACTUAL:\n{contexto_str}",
        })

    # Mensaje actual del usuario
    messages.append({"role": "user", "content": texto_usuario})

    # ── Llamar a OpenAI ───────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.7,
                    "max_tokens": 500,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
