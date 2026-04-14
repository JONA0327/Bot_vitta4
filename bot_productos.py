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
_PASO1_SYSTEM = """Eres una asesora de ventas de 4Life que atiende por WhatsApp.

Tu tarea es escribir el PRIMER mensaje de bienvenida. Sé cálida, empática y MUY breve.

INSTRUCCIONES OBLIGATORIAS:
- DEBES presentarte con tu nombre exactamente como se te indica en el campo "Tu nombre:".
  Formato: "[saludo]! Soy [tu nombre], ..." — NUNCA omitas tu nombre.
- Saluda según la hora indicada ("¡Buenos días!", "¡Buenas tardes!", "¡Buenas noches!").
- Transmite genuino interés en ayudar.
- Cierra SIEMPRE pidiendo el nombre del cliente. Varía la frase: "¿Con quién tengo el gusto?", "¿Me permite saber su nombre?", "¿Cómo le llamo?", etc.
- MÁXIMO 2 líneas cortas. Sin párrafos, sin listar productos, sin frases largas.
- Un emoji como mucho, solo al final si encaja naturalmente.
- NO menciones que eres IA ni sistema automatizado.

Ejemplo de formato correcto (2 líneas máximo):
"¡Buenas tardes! Soy Valeria, me alegra que estés aquí 😊
¿Con quién tengo el gusto?"

SI el usuario llegó desde una publicación (se te indicará CONTEXTO):
- Reconoce su interés en el TEMA DE SALUD en UNA sola frase natural, NO menciones nombres de productos ni de líneas.
- Pide su nombre al final. Total: máximo 2 líneas.
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
- MÁXIMO 2 líneas cortas en total. Sin párrafos largos.
- Un emoji como mucho.
- NO saludes de nuevo (ya saludaste).
- NO menciones que eres IA, bot o sistema automatizado.
- NO repitas información del mensaje anterior.
- NO expliques el producto, solo haz la pregunta de indagación.
"""

# Prompt PASO 3 — Diagnóstico profundo antes de recomendar
_PASO3_SYSTEM = """Eres una asesora de ventas de 4Life que atiende por WhatsApp.
Estás en la FASE DE DIAGNÓSTICO — tu misión es entender el problema del cliente en profundidad
ANTES de recomendar cualquier producto.

TU TAREA (una pregunta a la vez):
Haz las preguntas necesarias para entender:
  1. ¿Cuál es exactamente su problema o necesidad?
  2. ¿Cuánto tiempo lleva con ese problema?
  3. ¿Qué lo causa o qué lo desencadena?
  4. ¿Cuánto le afecta en su vida diaria (escala 1-10 o descripción)?
  5. ¿Cómo se siente actualmente al respecto?
  6. ¿Ha probado algo antes? ¿Con qué resultado?

REGLAS:
- Haz SIEMPRE solo UNA pregunta por mensaje. Natural, cálida, como una amiga.
- MÁXIMO 2 líneas por respuesta. Un emoji como mucho.
- NO recomiendes productos todavía.
- NO menciones precios, marcas ni 4Life.
- Usa el historial para no repetir preguntas ya respondidas.

CUANDO TENGAS TODA LA INFORMACIÓN NECESARIA:
- Cuando hayas entendido problema, causas, duración e impacto,  
  inicia tu respuesta con la línea exacta: [[LISTO]]
- Después escribe un mensaje corto de cierre empático, ej:
  "Gracias, con lo que me has contado ya tengo todo lo que necesito 😊"
- [[LISTO]] ÚNICAMENTE cuando tengas suficiente info para hacer una recomendación precisa.
"""

# Señal que el bot incluye cuando PASO 3 está completo
PASO3_SIGNAL = "[[LISTO]]"


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


def _extraer_productos_contexto(analisis: dict, intencion: dict) -> list[str]:
    """Extrae lista de productos detectados de FB/imagen del análisis."""
    productos: list[str] = list(intencion.get("productos_mencionados") or [])
    if isinstance(analisis.get("items"), list):
        for item in analisis["items"]:
            if item and item not in productos:
                productos.append(item)
    producto_principal = analisis.get("producto_mencionado") or ""
    if producto_principal and producto_principal not in productos:
        productos.insert(0, producto_principal)
    return productos


async def _responder_paso1(instancia: str, analisis: dict | None = None, intencion: dict | None = None) -> str | None:
    """Genera el mensaje de bienvenida (PASO 1) usando IA."""
    analisis = analisis or {}
    intencion = intencion or {}
    saludo = _saludo_hora_mexico()
    nombre_bot = instancia.strip() or "4Life"

    # Detectar si viene de publicación FB con productos específicos
    productos_fb = _extraer_productos_contexto(analisis, intencion)
    tiene_fb = bool(
        productos_fb
        or analisis.get("resumen_para_bot")
        or analisis.get("descripcion_publicacion")
    )

    if tiene_fb:
        resumen_fb = analisis.get("resumen_para_bot") or analisis.get("descripcion_publicacion") or ""
        contexto_usuario = analisis.get("contexto_usuario") or ""
        nombre_linea = analisis.get("nombre_linea") or ""
        # Usar el resumen/contexto para transmitir el TEMA DE SALUD, no el nombre técnico de línea/producto
        tema_salud = contexto_usuario or resumen_fb or (f"la línea {nombre_linea}" if nombre_linea else "sus productos de salud")
        prompt_usuario = (
            f"Hora del día: {saludo}.\n"
            f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
            f"CONTEXTO: El usuario llegó desde una pauta con este tema: {tema_salud}\n"
            "MÁXIMO 2 líneas cortas. "
            "Reconoce su interés en el tema de salud de forma natural y empática, sin mencionar nombres técnicos de líneas ni productos. "
            "Ej: 'Vi que le interesó lo relacionado con la digestión/salud digestiva/bienestar...' "
            "NO menciones '4Life' como marca ni 'generar ingresos'. "
            "Cierra pidiendo el nombre respetuosamente. Un solo emoji al final."
        )
    else:
        prompt_usuario = (
            f"Hora del día: {saludo}.\n"
            f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
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
                    "max_tokens": 120,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _construir_pregunta_indagacion(analisis: dict, intencion: dict, historial_texto: str = "") -> str:
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

    # Si no se detectaron productos en el mensaje actual, buscarlos en el historial
    # (en PASO1 el anuncio de FB quedó registrado en el historial como primer turno)
    resumen_historial = ""
    if not productos and not analisis.get("resumen_para_bot") and not analisis.get("descripcion_publicacion"):
        import re
        # Buscar productos mencionados en la primera respuesta del bot del historial
        match_productos = re.search(
            r"\[CONTEXTO_ANUNCIO\].*?(?:Titulo anuncio|titulo).*?:\s*(.+)",
            historial_texto, re.IGNORECASE | re.DOTALL
        )
        if match_productos:
            linea = match_productos.group(1).split("\n")[0].strip()
            if linea:
                productos = [linea]
        # También buscar lista de productos entre corchetes en el historial del usuario
        if not productos:
            match_lista = re.search(r"\[([^\[\]]+(?:,|Stix|Life|Tea|Pre|Aloe)[^\[\]]+)\]", historial_texto)
            if match_lista:
                productos = [p.strip() for p in match_lista.group(1).split(",") if p.strip()]
        # Si hay contexto en el historial de una publicación FB, extraer resumen
        if not productos:
            match_resumen = re.search(
                r"(?:Texto anuncio|descripci[oó]n):\s*\"?(.{10,120})",
                historial_texto, re.IGNORECASE
            )
            if match_resumen:
                resumen_historial = match_resumen.group(1).strip().rstrip('"')

    tiene_productos_fb = bool(
        productos
        or analisis.get("resumen_para_bot")
        or analisis.get("descripcion_publicacion")
        or resumen_historial
    )

    if tiene_productos_fb:
        lista = ", ".join(productos[:3]) if productos else "este producto"
        contexto = lista if productos else (analisis.get("resumen_para_bot") or resumen_historial or "este producto")
        return (
            f"El cliente llegó interesado en: {contexto}. "
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
    pregunta = _construir_pregunta_indagacion(analisis, intencion, historial_texto)
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
                    "max_tokens": 120,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


async def _responder_paso3(
    texto_usuario: str,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
) -> str | None:
    """PASO 3 — Diagnóstico profundo: entender el problema antes de recomendar."""
    contexto_extra = ""
    if analisis.get("resumen_para_bot"):
        contexto_extra = f"\nContexto de llegada del usuario: {analisis['resumen_para_bot']}"

    messages = [
        {"role": "system", "content": _PASO3_SYSTEM + contexto_extra},
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
                    "max_tokens": 150,
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

    # ── PASO 1: Primer contacto (sin historial o sin respuesta previa del bot) ─
    if not historial_texto.strip() or _contar_turnos_bot(historial_texto) == 0:
        return await _responder_paso1(instancia, analisis, intencion)

    # ── PASO 2: Segunda respuesta — manejo del nombre + indagación ────────────
    if _contar_turnos_bot(historial_texto) == 1:
        return await _responder_paso2(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 3: Diagnóstico profundo (hasta que el bot tenga suficiente info) ─
    # Se detecta el fin de PASO 3 por la presencia del marcador en historial
    _MARKER_PASO3_DONE = "Estoy examinando tu situación"
    if _MARKER_PASO3_DONE not in historial_texto:
        return await _responder_paso3(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 4: Recomendación de productos (PASO 3 ya completado) ─────────────
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
