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

from typing import Optional, List, Dict, Any

# ─────────────────────────────────────────────────────────────────────────────
# Reglas activas del bot — se inyectan desde main.py al arrancar el servidor.
# Si el dict está vacío el bot funciona normalmente con sus prompts base.
# No importar desde main.py directamente (circular import).
# main.py hace:  import bot_productos as _m; _m._REGLAS_ACTIVAS = BOT_REGLAS
# ─────────────────────────────────────────────────────────────────────────────
_REGLAS_ACTIVAS: dict = {}


def _construir_addon_reglas(paso: str) -> str:
    """Genera el bloque de instrucciones adicionales que se anexa al system prompt.
    Retorna cadena vacía si no hay reglas configuradas.

    Args:
        paso: Clave del paso ('paso1', 'paso2', 'paso2b', 'paso3', 'paso4', 'productos').
    """
    if not _REGLAS_ACTIVAS:
        return ""

    partes: list[str] = []

    # 1. Restricciones globales (aplican a todos los pasos)
    restricciones = _REGLAS_ACTIVAS.get("restricciones_globales") or []
    if restricciones:
        partes.append(
            "RESTRICCIONES GLOBALES (cumplir siempre):\n"
            + "\n".join(f"- {r}" for r in restricciones if r)
        )

    # 2. Instrucción específica de este paso
    paso_instruccion = ((_REGLAS_ACTIVAS.get("pasos") or {}).get(paso) or "").strip()
    if paso_instruccion:
        partes.append(f"INSTRUCCIÓN ESPECÍFICA PARA ESTE PASO:\n{paso_instruccion}")

    # 3. Conocimiento extra de la empresa
    conocimiento = (_REGLAS_ACTIVAS.get("conocimiento_extra") or "").strip()
    if conocimiento:
        partes.append(f"CONOCIMIENTO EXTRA DE LA EMPRESA:\n{conocimiento}")

    # 4. Tono
    tono = _REGLAS_ACTIVAS.get("tono") or {}
    tono_parts: list[str] = []
    if tono.get("nivel"):
        tono_parts.append(f"tono {tono['nivel']}")
    if tono.get("tutear") is True:
        tono_parts.append("tutea al cliente")
    if tono.get("emojis") is True:
        tono_parts.append("usa emojis moderadamente")
    if tono_parts:
        partes.append("TONO: " + ", ".join(tono_parts) + ".")

    if not partes:
        return ""

    return (
        "\n\n--- INSTRUCCIONES ADICIONALES DEL SISTEMA CRM ---\n"
        + "\n\n".join(partes)
        + "\n---"
    )



def _detectar_pregunta_info_producto(texto: str) -> str:
    """Si el usuario pregunta por información/descripción de un producto específico,
    retorna el nombre del producto mencionado. Si no, retorna cadena vacía."""
    patron = re.search(
        r"(?:qu[eé]\s+es|para\s+qu[eé]\s+(?:es|sirve)|en\s+qu[eé]\s+ayuda|"
        r"de\s+qu[eé]\s+(?:trata|se\s+trata)|info(?:rmaci[oó]n)?\s+(?:del?|sobre)|"
        r"cu[eé]ntame\s+(?:del?|sobre)|h[aá]blame\s+(?:del?|sobre))"
        r"\s+(?:el\s+|la\s+|los\s+|las\s+)?([A-Za-z0-9\-\s]{2,40}?)(?:[?.,!]|$)",
        texto,
        re.IGNORECASE,
    )
    if patron:
        return patron.group(1).strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# System prompt del bot de productos
# ─────────────────────────────────────────────────────────────────────────────
_PRODUCTOS_SYSTEM = f"""Eres un asesor de bienestar y suplementación (4Life) que atiende por WhatsApp.

ESTILO — escribe como un humano real en WhatsApp, NO como folleto ni correo formal:
- Frases cortas y directas. Sin introducción ni despedida larga.
- Máximo 3 líneas por mensaje. Si necesitas más, divide en mensajes.
- Sin emojis decorativos innecesarios — solo si encajan (máximo 1).
- Tono cercano, empático, natural — como alguien que sabe del tema y te habla de tú.

MULETILLAS PROHIBIDAS (cero tolerancia, ni una vez):
entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, sin duda, claro que sí,
interesante, genial, qué bueno, me alegra, fantástico, entendido, de acuerdo, por supuesto que sí,
encantado, con placer, absolutamente, efectivamente, justamente, exactamente.

PRODUCTO QUE EL CLIENTE PIDIÓ:
- Si el cliente llegó preguntando por un producto específico, habla SOLO de ese producto.
- NO ofrezcas otros productos a menos que el cliente los pida o el tuyo no esté en catálogo.
- Si el producto pedido NO está en el catálogo disponible → NO improvises ni inventes. Guarda silencio (el sistema pausará la conversación para que un humano responda).

REGLAS CRÍTICAS:
- SOLO menciona productos del CATÁLOGO DISPONIBLE.
- NO hagas promesas médicas ni digas que cura enfermedades.
- No inventes precios. Di: "te paso el precio especial".
- Termina con UN cierre simple: "¿Te explico cómo pedirlo? 😊" o "¿Quieres que te dé más detalle?"

Los productos disponibles provienen exclusivamente del catálogo del CRM y se pasan en cada consulta.
"""

# Prompt exclusivo para el primer contacto (PASO 1)
_PASO1_SYSTEM = """Eres una asesora de ventas de bienestar y suplementación (4Life) que atiende por WhatsApp.

MISIÓN: Iniciar con CONEXIÓN genuina — sin vender de inmediato. El cliente llega de un anuncio de Facebook \
o por interés propio; tu primer objetivo es que se sienta escuchado y bienvenido, no bombardeado con productos.

INSTRUCCIONES OBLIGATORIAS:
- DEBES presentarte con tu nombre exactamente como se te indica en el campo "Tu nombre:".
  Formato: "[saludo]! Soy [tu nombre], ..." — NUNCA omitas tu nombre.
- Saluda según la hora indicada ("¡Buenos días!", "¡Buenas tardes!", "¡Buenas noches!").
- Transmite genuino interés en ayudar — sé cálida y empática, no robótica.
- Cierra SIEMPRE pidiendo el nombre del cliente. Varía la frase: "¿Con quién tengo el gusto?", "¿Me permite saber su nombre?", "¿Cómo le llamo?", etc.
- MÁXIMO 2 líneas cortas. Sin párrafos, sin listar productos, sin frases largas.
- Un emoji como mucho, solo al final si encaja naturalmente.
- NO menciones que eres IA ni sistema automatizado.
- NO vendas ni listes beneficios todavía.

Ejemplo de formato correcto (2 líneas máximo):
"¡Buenas tardes! Soy Valeria, me alegra que estés aquí 😊
¿Con quién tengo el gusto?"

SI el usuario llegó desde una publicación de Facebook y hay PRODUCTOS DETECTADOS:
- Menciona 1 o 2 de esos productos por su nombre COMPLETO exactamente como aparece en la lista.
  Ej: "vi que te interesó el Digestive 4Life y el Aloe Vera Stix".
- USA el nombre completo del producto siempre (ej. "Digestive 4Life", NO solo "Digestive").
- Si el producto tiene nombre compuesto (ej. "Transfer Factor Plus"), úsalo completo.
- NO menciones la marca '4Life' por separado como si fuera otro producto — va dentro del nombre.
- Cierra pidiendo el nombre. Total: máximo 2 líneas.

SI el usuario llegó desde una publicación de Facebook pero NO hay productos específicos:
- Menciona el TEMA DE SALUD de la publicación en UNA sola frase natural, sin enumerar productos.
  Ej: "vi que te interesa el bienestar digestivo" o "vi tu interés en energía y vitalidad".
- Pide su nombre al final. Total: máximo 2 líneas.

SI el usuario escribe sin contexto de Facebook (contacto directo):
- Escribe el saludo estándar: preséntate y pide el nombre.
- Puedes usar frases como: "¿Cuéntame, qué fue lo que más te llamó la atención?"

NOMBRE Y MULETILLAS:
- Tu nombre: pronúncialo solo en este primer mensaje, al inicio del saludo.
- El nombre del cliente: úsalo UNA sola vez en el segundo mensaje al reconocer que lo compartió.
  NUNCA más en el resto de la conversación.
- NO uses muletillas ni frases de relleno en ningún mensaje (nada de "claro", "claro que sí",
  "perfecto", "excelente", "entiendo", "por supuesto", "con mucho gusto", "me alegra que preguntes", etc.).
"""

# Prompt para PASO 2 — manejo del nombre + pregunta sobre conocimiento de la compañía
_PASO2_SYSTEM = """Eres una asesora de ventas real de 4Life que atiende por WhatsApp.
Eres cálida, cercana y genuinamente empática — hablas de tú al cliente como una persona real.

CONTEXTO: Es el SEGUNDO intercambio. En el mensaje anterior le pediste el nombre al cliente.
Esta es la fase de CONEXIÓN + inicio de DETECCIÓN — no vendas todavía.

TU TAREA:
1. Lee el mensaje del cliente con atención:
   - Si dio su nombre: úsalo UNA SOLA VEZ al inicio de tu respuesta con una frase breve y cálida \
(ej. "¡Un gusto [nombre]!" o "¡Qué gusto [nombre]!"). \
Después de este mensaje NUNCA vuelvas a usar su nombre.
   - Si pregunta para qué quieres su nombre: explica brevemente que es para una atención \
más personalizada, pero que no hay problema si prefiere no compartirlo. Continúa igual.
   - Si no dio nombre ni preguntó nada especial: pasa directamente al siguiente punto.

2. Después (con nombre o sin él), haz SIEMPRE esta pregunta de DETECCIÓN:
   "¿Ya conoces la compañía 4Life y los beneficios de sus productos?"
   - Si el cliente llegó desde Facebook con un producto específico, puedes orientar la pregunta:
     "¿Ya conoces 4Life? ¿Sabes para qué sirve el [nombre del producto]?"

REGLAS DE ESTILO:
- Habla de tú, sé cálida y directa. Nada de frases de manual de ventas.
- MÁXIMO 2 líneas cortas en total.
- Un emoji si encaja natural (ej. 😊, 💚). No fuerces.
- NO saludes de nuevo.
- PALABRAS PROHIBIDAS (ni una vez): entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, sin duda, claro que sí, interesante, genial, qué bueno, me alegra, fantástico, entendido, de acuerdo.
- NO menciones que eres IA, bot o sistema automatizado.
- NO preguntes sobre su necesidad todavía.
"""

# Prompt para PASO 2B — respuesta al conocimiento de la compañía → lleva a la pregunta de padecimiento
_PASO2B_SYSTEM = """Eres una asesora de ventas de 4Life que atiende por WhatsApp.
Tuteas al cliente. Eres cálida, natural y directa.

CONTEXTO: En el mensaje anterior le preguntaste si ya conoce la compañía y los beneficios de los productos.

TU TAREA — fase de EDUCACIÓN simple:
Evalúa la respuesta del cliente y actúa según el caso:

CASO SÍ — El cliente ya conoce la compañía (responde "sí", "sí la conozco", "claro", "un poco", etc.):
- No expliques la empresa. Ve directo a la pregunta final.

CASO NO — El cliente NO conoce la compañía (responde "no", "no la conozco", "¿qué es?", o similar):
- En UNA frase breve menciona que 4Life es una empresa de bienestar y salud con más de 25 años, \
respaldada por Transfer Factor — tecnología que educa y fortalece el sistema inmune de forma natural.
- Ejemplo: "4Life es una empresa de bienestar con productos respaldados por Transfer Factor, \
que fortalece el sistema inmune de forma natural — tienen opciones para casi cualquier necesidad de salud 💚"
- Luego haz la pregunta final.

CASO AMBIGUO — Respuesta confusa, no relacionada o sin contexto claro:
- Trátalo como CASO NO y sigue el mismo camino.

PREGUNTA FINAL (obligatoria en todos los casos, puedes variar ligeramente la redacción):
"¿Estás buscando algo para algún padecimiento o área de salud específica?"

REGLAS DE ESTILO:
- Habla de tú. MÁXIMO 3 líneas cortas en total.
- Un emoji si encaja natural (ej. 💚, 😊). No fuerces.
- NO hagas promesas médicas.
- PALABRAS PROHIBIDAS: entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, sin duda, claro que sí, interesante, genial, qué bueno, me alegra, fantástico, entendido.
- NO menciones precios ni resultados garantizados.
- NO repitas el nombre del cliente.
"""

# Prompt PASO 3 — Entrevista breve para identificar la condición (máx. 2 preguntas)
_PASO3_SYSTEM = """Eres una asesora de salud natural que atiende por WhatsApp.
Tu misión es entender la situación de la persona con el MÍNIMO de preguntas posible: máximo 2 en toda la entrevista.
Eres genuinamente empática y hablas como una persona real, cálida y directa — no como un folleto ni un bot.

FASE DE DETECCIÓN PROFUNDA:
Haz las preguntas CLAVE que te permitan recomendar el producto ideal para su necesidad específica.
Escucha activamente — cada respuesta del cliente es información valiosa.

SI EL CLIENTE MENCIONA PRECIO DURANTE ESTA FASE:
- No es una objeción — es una señal de interés. No detengas el flujo.
- Responde brevemente: "Con gusto te explico el precio 😊 Antes, ¿me cuentas [siguiente pregunta]? Así te recomiendo lo que más te conviene."
- Continúa con la pregunta de diagnóstico correspondiente.

SI EL CLIENTE PREGUNTA PARA QUÉ SIRVE EL PRODUCTO:
- NO des explicaciones del producto todavía. Sigue el flujo normal de diagnóstico.
- Continúa con la pregunta de diagnóstico correspondiente de forma natural.

ESTRATEGIA SEGÚN EL TIPO DE CONDICIÓN:
  • Condición AGUDA o SIMPLE (gripe, dolor puntual, baja energía, digestión ocasional, etc.):
    — Con UNA sola pregunta obtienes lo suficiente.
    — Antes de preguntar, acusa recibo de lo que dijo con UNA frase empática y breve que demuestre que lo escuchaste \
(ej. "Eso tiene solución, te lo aseguro.", "Eso afecta mucho el día a día.").
    — Luego pregunta directamente por la molestia principal, cuánto tiempo lleva y cómo afecta su día.
  • Condición CRÓNICA o COMPLEJA (diabetes, artritis, hipertensión, tiroides, fatiga crónica,
    dolor crónico, problemas hormonales, digestión recurrente, sobrepeso persistente, etc.):
    — Usa exactamente 2 preguntas para obtener el contexto completo.
    — En cada pregunta, abre con UNA frase empática y breve que valide lo que compartió antes de preguntar.
    — Pregunta 1: síntoma(s) específico(s) que más le afectan + cuánto tiempo lleva con esto.
    — Pregunta 2: qué factores lo empeoran o alivian + si lleva algún tratamiento o medicamento previo.

REGLAS DE ESTILO:
- MÁXIMO 2 preguntas en toda la entrevista. NUNCA hagas una tercera.
- UNA sola pregunta por mensaje.
- MÁXIMO 2 líneas por respuesta. SIN emojis — la calidez la transmites con las palabras.
- Suena humano/a: varía las frases empáticas, que sean auténticas.
- NO menciones el nombre de la persona.
- PALABRAS PROHIBIDAS (ni una vez): entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, sin duda, claro que sí, interesante, genial, qué bueno, me alegra, fantástico, entendido, de acuerdo.
- NO recomiendes productos todavía.
- NO menciones precios, marcas ni 4Life.
- NO des diagnósticos al paciente.
- REVISA el historial; NUNCA repitas una pregunta ya respondida.
- PREGUNTAS RESTANTES: {preguntas_restantes}. Cuando llegues a 0 usa [[LISTO]] obligatoriamente.

CUANDO TENGAS INFORMACIÓN SUFICIENTE O LAS PREGUNTAS SE AGOTEN:
- Inicia tu respuesta con la línea exacta: [[LISTO]]
- Escribe 1 frase corta de transición cálida que transmita que tomaste nota y vas a ayudar. \
Ej: "Con lo que me compartiste ya sé exactamente cómo orientarte."
"""

# Señal que el bot incluye cuando PASO 3 está completo
PASO3_SIGNAL = "[[LISTO]]"

# ─────────────────────────────────────────────────────────────────────────────
# Detección de urgencia — palabras que indican desesperación o situación crítica
# ─────────────────────────────────────────────────────────────────────────────
_URGENCIA_RE = re.compile(
    r"\b(desesperado|desesperada|urgente|urgencia|urgentemente|"
    r"quimioterapia|quimio|cáncer|cancer|leucemia|tumor|metastasis|metástasis|"
    r"muy mal|ya no aguanto|no puedo más|no puedo mas|emergencia|"
    r"se está muriendo|se esta muriendo|está grave|esta grave|"
    r"no sé qué hacer|no se que hacer|angustiado|angustiada|"
    r"desesperación|desesperacion|en agonía|en agonia|crítico|critico)\b",
    re.IGNORECASE,
)


def _es_urgente(texto: str) -> bool:
    return bool(_URGENCIA_RE.search(texto))


def _historial_tiene_urgencia(historial_texto: str) -> bool:
    msgs = re.findall(r"(?:^|\n)Usuario:\s*(.*?)(?=\nBot:|\Z)", historial_texto, re.DOTALL)
    return any(_es_urgente(m) for m in msgs)


# Prompt para el flujo de urgencia — responde con empatía profunda, salta el flujo comercial
_PASO_URGENCIA_SYSTEM = """Eres una asesora de salud natural con profunda empatía, atendiendo por WhatsApp.
La persona frente a ti está en un estado de urgencia o desesperación — enfermedad grave, quimioterapia
de un ser querido, situación crítica de salud propia.

MISIÓN: Que esta persona sienta que hay alguien real del otro lado que entiende y puede ayudar.
NO sigas el flujo normal de ventas.

INSTRUCCIONES:
1. Nombra específicamente lo que compartió — demuestra que lo escuchaste de verdad.
   MAL: "Entiendo lo difícil que es tu situación."
   BIEN: "Tener a un hijo atravesando quimioterapia es una de las situaciones más agotadoras para una familia."
2. Transmite con convicción que tienes opciones concretas que pueden ayudar.
3. Haz UNA sola pregunta directa para entender exactamente qué necesitan ahora mismo:
   ¿Es para el paciente directamente? ¿Para el cuidador? ¿Cuál es el síntoma o necesidad más urgente?
4. NUNCA preguntes si conoce la compañía ni hagas presentaciones de empresa.
5. Suena humano, presente y seguro — como una persona que realmente puede ayudar.

TONO: Cálido, cercano, sin dramatismo vacío. Sin emojis alegres. Sin lenguaje de ventas.
PALABRAS PROHIBIDAS: entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, sin duda, claro que sí, interesante, genial, qué bueno, me alegra, fantástico, entendido.
FORMATO: Máximo 3 líneas. Frases cortas. Como habla un humano real en WhatsApp.
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


# Nombres de LÍNEAS 4Life (gamas): NO son productos individuales.
# Si la IA los mete en productos_mencionados por error, se filtran.
_LINEAS_4LIFE_SET: set[str] = {
    "4life digestive", "digestive 4life",
    "4life transform", "transform 4life",
    "4life immune", "immune 4life",
    "4life energy", "energy 4life",
    "4life skin", "skin 4life",
    "4life weight", "transfer factor"  # genérico sin variante = línea
}

# Mapa de nombres cortos/abreviados → nombre completo oficial del producto 4Life
_NOMBRES_COMPLETOS_4LIFE: dict[str, str] = {
    "digestive":        "Digestive 4Life",
    "prebiotics":       "PreBiotics 4Life",
    "pre-biotics":      "Pre-Biotics 4Life",
    "aloe vera":        "Aloe Vera Stix",
    "belle vie":        "Transfer Factor Belle Vie",
    "burn":             "4Life Burn",
    "cardio4life":      "Cardio4Life",
    "riovida":          "RioVida",
    "rio vida":         "RioVida",
    "riolife":          "RioLife",
    "nanofactor":       "NanoFactor",
    "tea4life":         "Tea4Life",
    "tea 4life":        "Tea4Life",
    "bioeFA":           "BioEFA",
    "bioefa":           "BioEFA",
}


def _expandir_nombre_producto(nombre: str) -> str:
    """Devuelve el nombre completo oficial si el nombre recibido es una forma abreviada conocida."""
    key = nombre.strip().lower()
    return _NOMBRES_COMPLETOS_4LIFE.get(key, nombre)


def _extraer_productos_contexto(analisis: dict, intencion: dict) -> list[str]:
    """Extrae lista de productos detectados de FB/imagen del análisis.

    Prioridad: analisis['productos_mencionados'] (visión directa) > intencion > items > producto_mencionado.
    Cuando la visión ya provee productos, NO se mezcla con los de intención para evitar duplicados
    con variantes parciales del mismo nombre (ej: 'Digest' + 'Digestive' → solo 'Digestive').
    """
    # 1. Productos detectados directamente desde la imagen/FB (fuente primaria)
    productos_vision: list[str] = [p for p in (analisis.get("productos_mencionados") or []) if p]

    if productos_vision:
        productos = productos_vision
    else:
        # 2. Fallback: intención (texto) + items de imagen estática + producto_mencionado
        productos = [p for p in (intencion.get("productos_mencionados") or []) if p]
        if isinstance(analisis.get("items"), list):
            for item in analisis["items"]:
                if item and item not in productos:
                    productos.append(item)
        producto_principal = analisis.get("producto_mencionado") or ""
        if producto_principal and producto_principal not in productos:
            productos.insert(0, producto_principal)

    # 3. Filtrar nombres de LÍNEA que la IA pudo colar como producto
    #    y también el token suelto "4Life" si ya hay productos con él en el nombre.
    nombre_linea_analisis = (analisis.get("nombre_linea") or "").lower().strip()
    productos = [
        p for p in productos
        if p.lower().strip() not in _LINEAS_4LIFE_SET
        and p.lower().strip() != nombre_linea_analisis
        and p.lower().strip() != "4life"
    ]

    # 4. Expandir nombres abreviados al nombre completo oficial
    productos = [_expandir_nombre_producto(p) for p in productos]

    # 5. Deduplicar: eliminar términos que sean substring de otro en la lista
    #    Ej: ["Digest", "Digestive", "Digestive 4Life"] → ["Digestive 4Life"]
    if len(productos) > 1:
        prods_lower = [p.lower() for p in productos]
        filtrado = [
            prod for i, prod in enumerate(productos)
            if not any(
                i != j and prods_lower[i] in prods_lower[j]
                for j in range(len(productos))
            )
        ]
        productos = filtrado if filtrado else productos

    return productos


# ── Diccionario de productos 4Life → categoría/línea
_LINEAS_4LIFE: dict[str, str] = {
    # Digestive / Gut Health
    "prebiotics":          "salud digestiva",
    "pre-biotics":         "salud digestiva",
    "aloe vera stix":      "salud digestiva",
    "aloe vera":           "salud digestiva",
    "digestive 4life":     "salud digestiva",
    "digest 4life":        "salud digestiva",
    "pro-tf":              "salud digestiva",
    "probiotic":           "salud digestiva",
    # Energy / Weight
    "tea4life":            "energía y control de peso",
    "tea 4life":           "energía y control de peso",
    "4life burn":          "quema de grasa y energía",
    "burn":                "quema de grasa y energía",
    "fit4life":            "control de peso",
    "shape":               "control de peso",
    # Immune Support
    "transfer factor":     "apoyo inmunológico",
    "transferfactor":      "apoyo inmunológico",
    "transfer factor plus":"apoyo inmunológico",
    "nanofactor":          "apoyo inmunológico",
    # Antioxidants / Vitality
    "riovida":             "vitalidad e inmunidad",
    "rio vida":            "vitalidad e inmunidad",
    "riovida stix":        "vitalidad e inmunidad",
    "riolife":             "vitalidad y antioxidantes",
    # Skin / Beauty
    "collagen":            "cuidado de la piel",
    "collagen plus":       "cuidado de la piel",
    "skin care":           "cuidado de la piel",
    # Cardiovascular
    "cardio4life":         "salud cardiovascular",
    # Hormonal / Wellness
    "female balance":      "bienestar femenino",
    "male factor":         "bienestar masculino",
    # Bone / Joint
    "joint support":       "salud articular",
    "bone support":        "salud ósea",
    # Brain
    "brain support":       "salud cerebral",
    "focus 4 life":        "salud cerebral y enfoque",
}


def _detectar_linea_productos(productos: list[str]) -> str:
    """Retorna la categoría/línea de 4Life más probable para la lista de productos."""
    if not productos:
        return ""
    encontradas: list[str] = []
    for prod in productos:
        clave = prod.lower().strip()
        for k, v in _LINEAS_4LIFE.items():
            if k in clave or clave in k:
                if v not in encontradas:
                    encontradas.append(v)
                break
    if not encontradas:
        return ""
    if len(set(encontradas)) == 1:
        return encontradas[0]
    return " y ".join(encontradas[:2])


# ── Helpers CRM / catálogos
CRM_URL = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT = float(os.getenv("CRM_TIMEOUT", "8"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "10"))


async def _crm_get(module: str, params: dict | None = None) -> list | dict | None:
    # Re-leer en tiempo de ejecución por si load_dotenv() se llamó después de la importación
    _url    = CRM_URL    or os.getenv("CRM_URL", "").rstrip("/")
    _tenant = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _token  = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _tenant and _token):
        print("[CRM] _crm_get: CRM no configurado (CRM_URL/CRM_TENANT/CRM_API_TOKEN vacíos)")
        return None
    url = f"{_url}/api/v1/{_tenant}/{module}"
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-API-Key": _token}, params=params or {})
            resp.raise_for_status()
            data = resp.json()
            try:
                count = len(data) if isinstance(data, list) else (len(data.keys()) if isinstance(data, dict) else 'n/a')
            except Exception:
                count = 'n/a'
            print(f"[CRM] GET {module} params={params} status={resp.status_code} items={count}")
            return data
    except Exception as e:
        print(f"[CRM] error GET {module}: {e}")
        return None


async def _crm_get_by_id(module: str, id: Any) -> dict | None:
    # Re-leer en tiempo de ejecución por si load_dotenv() se llamó después de la importación
    _url    = CRM_URL    or os.getenv("CRM_URL", "").rstrip("/")
    _tenant = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _token  = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _tenant and _token):
        return None
    url = f"{_url}/api/v1/{_tenant}/{module}/{id}"
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-API-Key": _token})
            resp.raise_for_status()
            data = resp.json()
            # Algunos endpoints devuelven {data: {...}}
            if isinstance(data, dict) and data.get("data") is not None:
                return data.get("data")
            return data
    except Exception as e:
        print(f"[CRM] error GET {module}/{id}: {e}")
        return None


async def _crm_get_entrenamiento(q: str | None = None, limit: int = 5, instancia: str | None = None) -> list:
    """Obtiene pares de conversación aprobados del CRM para usar como ejemplos en los prompts.

    Sin `q`: GET /api/v1/{tenant}/entrenamiento?limit=N  (pares aprobados generales)
    Con `q`: GET /api/v1/{tenant}/entrenamiento/buscar?q={q}&limit=N  (búsqueda por similitud)

    Retorna lista de dicts con al menos {pregunta, respuesta}. Falla silenciosamente → [].
    """
    _url    = CRM_URL    or os.getenv("CRM_URL", "").rstrip("/")
    _tenant = CRM_TENANT or os.getenv("CRM_TENANT", "")
    _token  = CRM_API_TOKEN or os.getenv("CRM_API_TOKEN", "")
    if not (_url and _tenant and _token):
        return []
    try:
        params: dict = {"limit": limit}
        if instancia:
            params["instancia"] = instancia
        if q:
            endpoint = f"{_url}/api/v1/{_tenant}/entrenamiento/buscar"
            params["q"] = q
        else:
            endpoint = f"{_url}/api/v1/{_tenant}/entrenamiento"
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(endpoint, headers={"X-API-Key": _token}, params=params)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) and data.get("data") is not None else data
            if isinstance(items, list):
                print(f"[CRM-ENTRENA] q={q!r} → {len(items)} registros")
                # Log de campos del primer registro para detectar estructura del CRM
                if items:
                    primer = items[0]
                    claves_top = list(primer.keys()) if isinstance(primer, dict) else "no-dict"
                    claves_datos = list(primer["datos"].keys()) if isinstance(primer, dict) and isinstance(primer.get("datos"), dict) else "sin-datos"
                    print(f"[CRM-ENTRENA] estructura primer registro: top={claves_top} datos={claves_datos}")
                return items
    except Exception as e:
        print(f"[CRM-ENTRENA] error: {e}")
    return []


def _formatear_ejemplos_entrenamiento(pares: list) -> str:
    """Convierte pares {pregunta, respuesta} en un bloque de texto para inyectar en system prompt.

    Desenvuelve la capa 'datos' que el CRM envuelve en sus registros (igual que _pick_field).
    También extrae prompt_generado si el CRM lo incluye.
    Retorna cadena vacía si no hay pares ni instrucciones.
    """
    if not pares:
        return ""
    lineas = ["EJEMPLOS DE CONVERSACIÓN APROBADOS (usa estos como guía de tono y estilo):"]
    prompts_extra: list[str] = []
    for p in pares:
        # Desenvolver capa 'datos' — los registros del CRM anidan sus campos aquí
        d = p.get("datos") if isinstance(p.get("datos"), dict) else p

        pregunta = str(
            d.get("pregunta") or d.get("question") or d.get("PREGUNTA") or
            p.get("pregunta") or ""
        ).strip()
        respuesta = str(
            d.get("respuesta") or d.get("response") or d.get("answer") or d.get("RESPUESTA") or
            p.get("respuesta") or ""
        ).strip()
        if pregunta and respuesta:
            lineas.append(f"Cliente: {pregunta}\nAsesor: {respuesta}")

        pg = str(
            d.get("prompt_generado") or d.get("promptGenerado") or d.get("PROMPT_GENERADO") or
            p.get("prompt_generado") or p.get("promptGenerado") or ""
        ).strip()
        if pg and pg not in prompts_extra:
            prompts_extra.append(pg)

    encontrados = len(lineas) - 1
    print(f"[Entrenamiento] {len(pares)} registros CRM → {encontrados} pares usables, {len(prompts_extra)} prompts_generados")
    if len(lineas) <= 1 and not prompts_extra:
        return ""
    parts: list[str] = []
    if len(lineas) > 1:
        parts.append("\n---\n".join(lineas))
    if prompts_extra:
        parts.append("INSTRUCCIONES DE ENTRENAMIENTO:\n" + "\n".join(f"- {pg}" for pg in prompts_extra))
    return "\n\n".join(parts)


def _pick_field(rec: dict, keys: List[str]) -> Optional[Any]:
    """Extract a field from a CatalogRecord, automatically unwrapping the 'datos' layer."""
    # CatalogRecord API responses nest all field values inside a 'datos' dict
    datos: dict = rec
    if isinstance(rec.get("datos"), dict):
        datos = rec["datos"]
    for k in keys:
        if k in datos and datos[k] not in (None, ""):
            return datos[k]
    # Fallback: top-level (for non-CatalogRecord dicts)
    if datos is not rec:
        for k in keys:
            if k in rec and rec[k] not in (None, ""):
                return rec[k]
    return None


def _is_disponible(p: dict) -> bool:
    """Retorna True si el producto está disponible según su campo de disponibilidad."""
    val = _pick_field(p, ["DISPONIBLE", "disponible", "available", "status", "STATUS"])
    if val is None:
        return True  # si no hay campo, asumir disponible
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "si", "sí", "disponible", "activo", "available")


def _all_strings(v, _depth: int = 0) -> list[str]:
    """Extrae recursivamente todos los strings de un valor (puede ser str/list/dict anidado)."""
    if _depth > 5:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        out = []
        for item in v:
            out.extend(_all_strings(item, _depth + 1))
        return out
    if isinstance(v, dict):
        out = []
        for val in v.values():
            out.extend(_all_strings(val, _depth + 1))
        return out
    return []


def _make_absolute_url(url: str) -> str:
    """Convierte una URL relativa en absoluta usando CRM_URL.

    Soporta:
    - URLs absolutas (http/https) → devuelve tal cual
    - Rutas con / inicial → {CRM_URL}{path}
    - Rutas relativas sin / (ej. catalog/...) → {CRM_URL}/storage/{path}
    """
    if url.startswith('http://') or url.startswith('https://'):
        return url
    _base = CRM_URL or os.getenv('CRM_URL', '').rstrip('/')
    if not _base:
        return url
    if url.startswith('/'):
        return _base + url
    # Ruta relativa sin / (paths del disco 'public' de Laravel como catalog/...)
    return _base + '/storage/' + url


def _nombre_producto_limpio(valor) -> str:
    """Extrae un nombre legible del campo 'producto' del catálogo.

    El campo puede ser:
    - Un dict con clave 'items': ["Nombre Producto"]
    - Un string que representa ese dict (Python repr o JSON)
    - Un string simple
    """
    if isinstance(valor, dict):
        items = valor.get('items') or valor.get('Items') or []
        if isinstance(items, list) and items:
            return ', '.join(str(i) for i in items if i)
        return str(valor.get('categoria') or next(iter(valor.values()), ''))
    if isinstance(valor, str):
        # Intentar parsear como JSON
        try:
            import json as _j
            parsed = _j.loads(valor)
            if isinstance(parsed, dict):
                return _nombre_producto_limpio(parsed)
        except Exception:
            pass
        # Intentar parsear como Python repr (ast.literal_eval)
        try:
            import ast
            parsed = ast.literal_eval(valor)
            if isinstance(parsed, dict):
                return _nombre_producto_limpio(parsed)
        except Exception:
            pass
        return valor
    return str(valor) if valor else ''


def _pick_imagen(p: dict) -> str:
    """Busca la URL de imagen de un producto escaneando todos los campos recursivamente."""
    datos = p.get("datos") if isinstance(p.get("datos"), dict) else p

    _IMG_KEYS = [
        "IMAGEN", "imagen", "FOTO", "foto", "IMAGE", "image",
        "IMAGEN_PRODUCTO", "imagen_producto", "IMAGEN_URL", "imagen_url",
        "URL_IMAGEN", "url_imagen", "PHOTO", "photo", "IMG", "img",
        "IMAGE_URL", "image_url", "FOTO_URL", "foto_url",
        "thumbnail", "THUMBNAIL", "portada", "PORTADA",
    ]
    _IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')
    _IMG_PATHS = ('/storage/', '/uploads/', '/catalog/', '/images/', '/media/', '/img/')

    def _is_image_url(s: str) -> bool:
        # Imágenes almacenadas como base64 data URI en el catálogo
        if s.startswith('data:image/') and ';base64,' in s:
            return True
        sl = s.lower()
        if any(sl.endswith(ext) or (ext + '?') in sl or (ext + '&') in sl for ext in _IMG_EXTS):
            return True
        # Relative paths that look like storage paths
        if s.startswith('/') and any(p in sl for p in _IMG_PATHS):
            return True
        if s.startswith('http') and any(p in sl for p in _IMG_PATHS):
            return True
        return False

    pid = p.get('id', '?')
    all_field_vals = {k: str(v)[:80] for k, v in datos.items()}
    print(f"[pick_imagen] id={pid} todos_campos={list(datos.keys())}")
    print(f"[pick_imagen] id={pid} valores={all_field_vals}")

    # 1) Campos con nombres conocidos (valores pueden ser nested)
    for key in _IMG_KEYS:
        if key in datos:
            for sv in _all_strings(datos[key]):
                if _is_image_url(sv):
                    url = _make_absolute_url(sv)
                    print(f"[pick_imagen] id={pid} ENCONTRADO en campo conocido '{key}': {url!r}")
                    return url

    # 2) Escanear TODOS los campos recursivamente
    for k, v in datos.items():
        if k in _IMG_KEYS:  # ya revisados arriba
            continue
        for sv in _all_strings(v):
            if _is_image_url(sv):
                url = _make_absolute_url(sv)
                print(f"[pick_imagen] id={pid} ENCONTRADO en campo genérico '{k}': {url!r}")
                return url
    print(f"[pick_imagen] id={pid} NO encontrado — ningún campo tiene URL de imagen")
    return ""


def _pick_video(p: dict) -> str:
    """Busca la URL de video de un producto escaneando todos los campos recursivamente."""
    datos = p.get("datos") if isinstance(p.get("datos"), dict) else p

    _VID_KEYS = [
        "VIDEO", "video", "VIDEO_URL", "video_url", "URL_VIDEO", "url_video",
        "VIDEO_LINK", "video_link", "LINK_VIDEO", "link_video",
        "VIDEO_PRODUCTO", "video_producto", "ENLACE", "enlace", "LINK", "link",
    ]
    _VID_EXTS = ('.mp4', '.mov', '.avi', '.webm', '.mkv')
    _VID_DOMAINS = ('youtube.com', 'youtu.be', 'vimeo.com', 'drive.google.com', 'fb.watch')

    def _is_video_url(s: str) -> bool:
        sl = s.lower()
        if any(sl.endswith(ext) or (ext + '?') in sl or (ext + '&') in sl for ext in _VID_EXTS):
            return True
        if s.startswith('http') and any(d in sl for d in _VID_DOMAINS):
            return True
        return False

    pid = p.get('id', '?')
    print(f"[pick_video] id={pid} todos_campos={list(datos.keys())}")

    # 1) Campos con nombres conocidos (valores pueden ser nested)
    for key in _VID_KEYS:
        if key in datos:
            for sv in _all_strings(datos[key]):
                print(f"[pick_video] id={pid} campo conocido '{key}' valor={sv!r}")
                if _is_video_url(sv):
                    url = _make_absolute_url(sv)
                    print(f"[pick_video] id={pid} ENCONTRADO en '{key}': {url!r}")
                    return url

    # 2) Escanear TODOS los campos recursivamente
    for k, v in datos.items():
        if k in _VID_KEYS:
            continue
        for sv in _all_strings(v):
            if _is_video_url(sv):
                url = _make_absolute_url(sv)
                print(f"[pick_video] id={pid} ENCONTRADO en campo genérico '{k}': {url!r}")
                return url
    print(f"[pick_video] id={pid} NO encontrado — ningún campo tiene URL de video")
    return ""



async def _obtener_todos_testimonios(per_page: int = 100) -> list:
    """Obtiene todos los testimonios sin filtro de búsqueda."""
    res = await _crm_get("testimonios", {"per_page": per_page})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _obtener_todos_paquetes(per_page: int = 100) -> list:
    """Obtiene todos los paquetes sin filtro de búsqueda."""
    res = await _crm_get("paquetes", {"per_page": per_page})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _obtener_todos_productos(per_page: int = 100) -> list:
    """Obtiene todos los productos sin filtro de búsqueda."""
    res = await _crm_get("productos", {"per_page": per_page})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _ia_elegir_testimonio(
    testimonios: list,
    condicion: str,
    sintomas: list,
    causas: list,
) -> dict | None:
    """Usa IA para determinar cuál testimonio/condición crónica coincide mejor con
    los síntomas y causas del paciente. Retorna el testimonio elegido o None."""
    if not testimonios or not OPENAI_API_KEY:
        return None

    # Construir resumen de testimonios para el prompt
    lines = []
    for i, t in enumerate(testimonios):
        cond = _pick_field(t, ["CONDICION_CRONICA", "condicion_cronica", "condicion", "CONDICION"]) or ""
        desc = _pick_field(t, ["DESCRIPCION", "descripcion", "description"]) or ""
        lines.append(f"[{i}] Condición: {cond}\nDescripción: {desc[:300]}")

    testimonio_txt = "\n---\n".join(lines)
    sintomas_txt = ", ".join(sintomas) if sintomas else "no especificados"
    causas_txt = ", ".join(causas) if causas else "no especificadas"

    prompt = (
        f"Un paciente tiene la siguiente situación:\n"
        f"- Condición principal: {condicion}\n"
        f"- Síntomas: {sintomas_txt}\n"
        f"- Posibles causas: {causas_txt}\n\n"
        f"Tienes estos registros de condiciones crónicas disponibles:\n{testimonio_txt}\n\n"
        "Responde ÚNICAMENTE con el número entre corchetes del registro más relevante para este paciente, "
        "o -1 si ninguno es relevante. Solo el número, sin explicación. Ejemplo: 0"
    )

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            idx = int(re.search(r"-?\d+", raw).group())
            if 0 <= idx < len(testimonios):
                chosen = testimonios[idx]
                cond_log = _pick_field(chosen, ["CONDICION_CRONICA", "condicion_cronica", "condicion", "CONDICION"]) or ""
                print(f"[CRMCAT] IA eligió testimonio idx={idx} condicion='{cond_log}'")
                return chosen
    except Exception as e:
        print(f"[CRMCAT] error en _ia_elegir_testimonio: {e}")
    return None


async def _ia_elegir_paquete(
    paquetes: list,
    condicion: str,
    sintomas: list,
    causas: list,
    sugeridos_ids: list,
) -> dict | None:
    """Usa IA para determinar cuál paquete contiene los productos sugeridos
    y es más relevante para la condición del paciente."""
    if not paquetes or not OPENAI_API_KEY:
        return None

    # Primero: filtrar paquetes que contienen al menos uno de los IDs sugeridos
    if sugeridos_ids:
        candidatos = []
        for p in paquetes:
            pf = _pick_field(p, ["PRODUCTOS", "productos", "productos_ids", "productos_sugeridos"]) or ""
            paq_ids = set(_extract_ids_from_field(pf))
            if paq_ids.intersection(set(sugeridos_ids)):
                candidatos.append(p)
        if candidatos:
            # Si solo hay uno, retornarlo directamente
            if len(candidatos) == 1:
                nombre = _pick_field(candidatos[0], ["NOMBRE_PAQUETE", "nombre_paquete", "NOMBRE", "nombre"]) or ""
                print(f"[CRMCAT] paquete directo por IDs: '{nombre}'")
                return candidatos[0]
            paquetes = candidatos  # reducir al subconjunto relevante

    lines = []
    for i, p in enumerate(paquetes):
        nombre = _pick_field(p, ["NOMBRE_PAQUETE", "nombre_paquete", "NOMBRE", "nombre"]) or ""
        desc = _pick_field(p, ["DESCRIPCION", "descripcion", "description"]) or ""
        prods_field = _pick_field(p, ["PRODUCTOS", "productos", "productos_ids"]) or ""
        lines.append(f"[{i}] Paquete: {nombre}\nDescripción: {desc[:200]}\nProductos IDs: {prods_field}")

    paquetes_txt = "\n---\n".join(lines)
    sintomas_txt = ", ".join(sintomas) if sintomas else "no especificados"

    prompt = (
        f"Un paciente con {condicion} (síntomas: {sintomas_txt}) necesita suplementos.\n"
        f"Estos son los paquetes disponibles:\n{paquetes_txt}\n\n"
        "Responde ÚNICAMENTE con el número entre corchetes del paquete más adecuado, "
        "o -1 si ninguno aplica. Solo el número. Ejemplo: 0"
    )

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            idx = int(re.search(r"-?\d+", raw).group())
            if 0 <= idx < len(paquetes):
                nombre = _pick_field(paquetes[idx], ["NOMBRE_PAQUETE", "nombre_paquete", "NOMBRE", "nombre"]) or ""
                print(f"[CRMCAT] IA eligió paquete idx={idx} nombre='{nombre}'")
                return paquetes[idx]
    except Exception as e:
        print(f"[CRMCAT] error en _ia_elegir_paquete: {e}")
    return None


async def _ia_elegir_productos(
    productos: list,
    condicion: str,
    sintomas: list,
    causas: list | None = None,
) -> list:
    """Usa IA para filtrar ESTRICTAMENTE los productos genuinamente relevantes.
    Analiza la descripción completa de cada producto para decidir si aplica.
    Si ningún producto ayuda realmente a la condición, retorna lista vacía."""
    disponibles = [p for p in productos if _is_disponible(p)]
    if not disponibles or not OPENAI_API_KEY:
        return []

    causas = causas or []
    lines = []
    for i, p in enumerate(disponibles):
        nombre = _pick_field(p, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or ""
        desc = _pick_field(p, ["DESCRIPCION", "descripcion", "description"]) or ""
        # Incluir descripción completa sin truncar para análisis preciso
        lines.append(f"[{i}] Producto: {nombre}\nDescripción: {desc}")

    productos_txt = "\n---\n".join(lines)
    sintomas_txt = ", ".join(sintomas) if sintomas else "no especificados"
    causas_txt = ", ".join(causas) if causas else "no especificadas"

    system_prompt = (
        "Eres un médico especialista en medicina integrativa y nutrición clínica. "
        "Analiza CADA producto con criterio clínico riguroso: lee su descripción completa "
        "y evalúa si sus ingredientes activos tienen evidencia real de beneficio para la condición exacta del paciente. "
        "Sé MUY ESTRICTO:\n"
        "- Solo incluye productos cuya descripción demuestre claramente que ayuda a esa condición específica, "
        "  sus síntomas o sus causas raíz.\n"
        "- Rechaza productos genéricos de bienestar que no aborden directamente la condición.\n"
        "- Rechaza productos cuya descripción hable de algo completamente diferente.\n"
        "- Prefiere calidad sobre cantidad: es mejor recomendar 1 producto excelente que 3 mediocres.\n"
        "- Máximo 3 productos. Mínimo 0 (si ninguno aplica genuinamente, responde -1)."
    )

    user_prompt = (
        f"Condición clínica del paciente: {condicion}\n"
        f"Síntomas específicos: {sintomas_txt}\n"
        f"Causas o factores contribuyentes: {causas_txt}\n\n"
        f"Catálogo de productos DISPONIBLES:\n{productos_txt}\n\n"
        "INSTRUCCIÓN CRÍTICA: Evalúa clínicamente cada producto contra la condición del paciente. "
        "Lista SOLO los índices de productos que genuinamente pueden ayudar (máximo 3). "
        "Si ninguno aplica realmente, responde exactamente: -1\n"
        "Responde solo con índices separados por coma o -1. Sin texto adicional. Ejemplo: 0,2"
    )

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0,
                    "max_tokens": 30,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            # Si la IA dice -1, ningún producto aplica
            if raw.strip().startswith("-1") or raw.strip() == "-1":
                print(f"[CRMCAT] IA determinó que ningún producto aplica para condición='{condicion}'")
                return []
            indices = [int(x.strip()) for x in re.findall(r"\d+", raw) if 0 <= int(x.strip()) < len(disponibles)]
            if indices:
                seleccionados = [disponibles[i] for i in indices]
                nombres = [str(_pick_field(p, ["PRODUCTO","producto","NOMBRE","nombre","title"]) or "?") for p in seleccionados]
                print(f"[CRMCAT] IA seleccionó {len(seleccionados)} productos: {nombres} para condición='{condicion}'")
                return seleccionados
    except Exception as e:
        print(f"[CRMCAT] error en _ia_elegir_productos: {e}")
    return []


async def _buscar_testimonios_por_condicion(query: str) -> list:
    res = await _crm_get("testimonios", {"search": query, "per_page": 10})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _buscar_paquetes_por_query(query: str) -> list:
    res = await _crm_get("paquetes", {"search": query, "per_page": 10})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _buscar_productos_por_query(query: str, per_page: int = 10) -> list:
    res = await _crm_get("productos", {"search": query, "per_page": per_page})
    if not res:
        return []
    data = res.get("data") if isinstance(res, dict) and res.get("data") is not None else res
    return data or []


async def _obtener_productos_por_ids(ids: List[int]) -> list:
    """Obtiene productos por IDs buscando dentro de la lista completa.
    El endpoint individual /productos/{id} no existe en la API, así que
    descargamos todos y filtramos por el campo 'id' del record.
    """
    if not ids:
        return []
    todos = await _obtener_todos_productos()
    target = set(ids)
    result = []
    for p in todos:
        try:
            if int(p.get("id", -1)) in target:
                result.append(p)
        except (TypeError, ValueError):
            pass
    if not result:
        # Fallback: si el ID está dentro de datos (algunas implementaciones)
        for p in todos:
            datos = p.get("datos") if isinstance(p.get("datos"), dict) else {}
            try:
                inner_id = int(datos.get("ID") or datos.get("id") or -1)
                if inner_id in target:
                    result.append(p)
            except (TypeError, ValueError):
                pass
    print(f"[CRM] _obtener_productos_por_ids ids={ids} → encontrados={len(result)}")
    return result


def _extract_ids_from_field(value: Any) -> list:
    """Extrae IDs numéricos de un campo (puede ser lista, cadena 'ID: 11, ID: 10' o JSON)."""
    if value is None:
        return []
    if isinstance(value, list):
        ids = []
        for v in value:
            try:
                ids.append(int(v))
            except Exception:
                m = re.search(r"(\d+)", str(v))
                if m:
                    ids.append(int(m.group(1)))
        return ids
    s = str(value)
    ids = [int(x) for x in re.findall(r"(\d+)", s)]
    return ids


async def _buscar_en_catalogos(
    historial_texto: str,
    analisis: dict,
    intencion: dict,
    texto_usuario: str,
    terminos_busqueda: str = "",
    condicion_detectada: str = "",
    causas_posibles: list | None = None,
    sintomas: list | None = None,
) -> tuple[list, dict | None]:
    """Busca productos usando IA para analizar catálogos completos.

    Lógica:
    1. Obtiene TODOS los testimonios (sin filtro) y usa IA para elegir el más relevante
       según condición, síntomas y causas del paciente.
    2. Obtiene los productos_sugeridos del testimonio elegido.
    3. Obtiene TODOS los paquetes (sin filtro) y usa IA para elegir el que contiene
       los productos sugeridos y mejor aplica a la condición.
    4. Si hay paquete: usa los productos del paquete (solo disponibles).
    5. Si no hay paquete: usa los productos individuales (solo disponibles).
    6. Fallback: obtiene todos los productos y usa IA para filtrar los más relevantes.

    Retorna: (lista_productos, paquete_info_o_None)
    """
    causas_posibles = causas_posibles or []
    sintomas = sintomas or []

    # Extraer nombres de productos del historial/contexto (para FB)
    productos_hist: list[str] = list(intencion.get("productos_mencionados") or [])
    if isinstance(analisis.get("items"), list):
        for p in analisis["items"]:
            if p and p not in productos_hist:
                productos_hist.append(p)
    if not productos_hist and historial_texto:
        m_lista = re.search(r"\[([^\[\]]+(?:,|Stix|Life|Tea|Pre|Aloe|4Life)[^\[\]]+)\]", historial_texto)
        if m_lista:
            productos_hist = [p.strip() for p in m_lista.group(1).split(",") if p.strip()]
        if not productos_hist:
            m_titulo = re.search(r"Titulo anuncio:\s*(.+)", historial_texto, re.IGNORECASE)
            if m_titulo:
                productos_hist = [m_titulo.group(1).strip()]

    print(f"[CRMCAT] buscar_en_catalogos condicion='{condicion_detectada}' sintomas={sintomas} causas={causas_posibles} productos_hist={productos_hist}")

    # ── 0) Si el cliente pidió productos ESPECÍFICOS: buscar solo esos ────────
    # NO hacer búsqueda semántica. Si el producto exacto no está en catálogo,
    # retornar vacío → PASO4 pausa para que un humano responda.
    if productos_hist:
        todos_esp = await _obtener_todos_productos()
        disp_esp  = [p for p in todos_esp if _is_disponible(p)]
        encontrados_esp: list = []
        for nombre_pedido in productos_hist:
            # Expandir abreviaciones conocidas (ej. "TF Plus" → "Transfer Factor Plus")
            nombre_norm = _expandir_nombre_producto(nombre_pedido).lower().strip()
            for prod in disp_esp:
                nombre_prod = str(
                    _pick_field(prod, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or ""
                ).lower().strip()
                if nombre_norm and nombre_prod and (
                    nombre_norm in nombre_prod or nombre_prod in nombre_norm
                ):
                    if prod not in encontrados_esp:
                        encontrados_esp.append(prod)
        print(f"[CRMCAT] búsqueda específica: pedidos={productos_hist} → {len(encontrados_esp)} encontrados")
        # Retornar solo lo encontrado. Si vacío → dispara sin_productos_catalogo en PASO4.
        return encontrados_esp, None

    product_records: list = []
    paquete_info: dict | None = None

    # ── 1) Obtener TODOS los testimonios y elegir con IA ─────────────────────
    try:
        todos_testimonios = await _obtener_todos_testimonios()
        print(f"[CRMCAT] total testimonios={len(todos_testimonios)}")
    except Exception as e:
        print(f"[CRMCAT] error obteniendo testimonios: {e}")
        todos_testimonios = []

    chosen = None
    if todos_testimonios and condicion_detectada:
        chosen = await _ia_elegir_testimonio(
            todos_testimonios,
            condicion=condicion_detectada,
            sintomas=sintomas,
            causas=causas_posibles,
        )

    if chosen:
        sugeridos = _pick_field(chosen, ["PRODUCTOS_SUGERIDOS", "productos_sugeridos", "productos", "PRODUCTOS"])
        sugeridos_ids: list[int] = _extract_ids_from_field(sugeridos)
        print(f"[CRMCAT] productos_sugeridos ids={sugeridos_ids}")

        if sugeridos_ids:
            suggested_prods = await _obtener_productos_por_ids(sugeridos_ids)

            # ── 2) Obtener TODOS los paquetes y elegir con IA ─────────────────
            try:
                todos_paquetes = await _obtener_todos_paquetes()
                print(f"[CRMCAT] total paquetes={len(todos_paquetes)}")
            except Exception as e:
                print(f"[CRMCAT] error obteniendo paquetes: {e}")
                todos_paquetes = []

            paquete_encontrado: dict | None = None
            if todos_paquetes:
                paquete_encontrado = await _ia_elegir_paquete(
                    todos_paquetes,
                    condicion=condicion_detectada,
                    sintomas=sintomas,
                    causas=causas_posibles,
                    sugeridos_ids=sugeridos_ids,
                )

            if paquete_encontrado:
                paq_prod_field = _pick_field(paquete_encontrado, ["PRODUCTOS", "productos", "productos_ids", "productos_sugeridos"]) or ""
                paq_prod_ids = _extract_ids_from_field(paq_prod_field)
                prods = await _obtener_productos_por_ids(paq_prod_ids)
                prods_disp = [p for p in prods if _is_disponible(p)]
                print(f"[CRMCAT] paquete IA → {len(prods_disp)} productos disponibles")
                product_records.extend(prods_disp)
                paquete_info = paquete_encontrado
            else:
                prods_disp = [p for p in suggested_prods if _is_disponible(p)]
                print(f"[CRMCAT] sin paquete → {len(prods_disp)} productos individuales disponibles")
                product_records.extend(prods_disp)

    # ── 3) Fallback: obtener TODOS los productos y elegir con IA ─────────────
    if not product_records:
        try:
            todos_productos = await _obtener_todos_productos()
            print(f"[CRMCAT] fallback IA sobre {len(todos_productos)} productos")
        except Exception as e:
            print(f"[CRMCAT] error obteniendo productos: {e}")
            todos_productos = []

        if todos_productos and condicion_detectada:
            product_records = await _ia_elegir_productos(
                todos_productos,
                condicion=condicion_detectada,
                sintomas=sintomas,
                causas=causas_posibles,
            )
        elif todos_productos:
            product_records = [p for p in todos_productos if _is_disponible(p)][:3]

    return product_records, paquete_info


async def _responder_paso1(instancia: str, analisis: dict | None = None, intencion: dict | None = None) -> str | None:
    """Genera el mensaje de bienvenida (PASO 1) usando IA."""
    analisis = analisis or {}
    intencion = intencion or {}
    saludo = _saludo_hora_mexico()
    nombre_bot = instancia.strip() or "4Life"

    # ── Varias pautas activas: listar promociones en vez de usar mensaje individual ──
    pautas_multiples = analisis.get("pautas_multiples") or []
    if pautas_multiples:
        saludo_cap = saludo.capitalize()
        lista = "\n".join(f"🟢 {n}" for n in pautas_multiples)
        texto = (
            f"¡{saludo_cap}! Soy {nombre_bot}, actualmente tenemos estas promociones disponibles:\n\n"
            f"{lista}\n\n"
            "¿Te interesa información de alguno de estos productos o de algún otro?"
            "\n¿Con quién tengo el gusto?"
        )
        return texto

    # ── Pauta detectada: usar el mensaje personalizado tal cual ───────────────
    mensaje_pauta = analisis.get("mensaje_pauta") or ""
    if mensaje_pauta:
        saludo_cap = saludo.capitalize()
        prefijo = f"¡{saludo_cap}! Soy {nombre_bot}, "
        # Quitar saludo inicial del mensaje de la pauta si ya hay uno, para no repetirlo
        _cuerpo_pauta = re.sub(
            r"^[\s\W]*(¡|!)?\s*(?:hola[,!]?\s*|buenos?\s+(?:d[ií]as?|tardes?|noches?)[,!]?\s*|buenas[,!]?\s*)+",
            "",
            mensaje_pauta.lstrip(),
            flags=re.IGNORECASE,
        ).lstrip(" ,!\n")
        # Quitar frases que hagan alusión a la pauta/anuncio/publicación
        _cuerpo_pauta = re.sub(
            r"[^.!?\n]*\b(?:vi(?:ste)?|respond(?:iste|ió)|lleg(?:aste|ó)|contesta(?:ste|ó)|"
            r"hiciste clic|diste clic|te interesó|te llegó|encontraste|a través de|desde)"
            r"[^.!?\n]*\b(?:anuncio|pauta|publicaci[oó]n|campa[ñn]a|post|ad\b|promoci[oó]n)"
            r"[^.!?\n]*[.!?\n]?",
            "",
            _cuerpo_pauta,
            flags=re.IGNORECASE,
        ).strip()
        texto = prefijo + _cuerpo_pauta
        # Si el mensaje ya pide el nombre, no añadir otra pregunta
        _pide_nombre = re.search(
            r"(tu nombre|cómo te llam|cómo se llam|cómo le llam|con quién tengo el gusto"
            r"|me permite saber.*nombre|me dices tu nombre|me das tu nombre|con quién hablo"
            r"|cómo te puedo llamar|cómo te llamo|tu nombre\?)",
            mensaje_pauta,
            re.IGNORECASE,
        )
        if not _pide_nombre:
            texto = texto.rstrip() + "\n¿Con quién tengo el gusto?"
        return texto

    # Detectar si viene de publicación FB con productos específicos
    productos_fb = _extraer_productos_contexto(analisis, intencion)
    tiene_fb = bool(
        productos_fb
        or analisis.get("resumen_para_bot")
        or analisis.get("descripcion_publicacion")
        or analisis.get("mensaje_pauta")
    )

    if tiene_fb:
        resumen_fb = analisis.get("resumen_para_bot") or analisis.get("descripcion_publicacion") or analisis.get("mensaje_pauta") or ""
        contexto_usuario = analisis.get("contexto_usuario") or ""
        nombre_linea = analisis.get("nombre_linea") or ""

        if productos_fb:
            lista_productos = ", ".join(productos_fb[:3])
            linea_hint = (
                f"\nLÍNEA A LA QUE PERTENECEN: {nombre_linea} — menciona la línea de forma natural "
                f"para agrupar los productos (ej: 'vi que te interesó la línea {nombre_linea}, "
                f"con productos como {lista_productos}')."
            ) if nombre_linea else ""
            prompt_usuario = (
                f"Hora del día: {saludo}.\n"
                f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
                f"PRODUCTOS INDIVIDUALES DETECTADOS: {lista_productos}{linea_hint}\n"
                "Escribe el saludo siguiendo las instrucciones para 'publicación FB con PRODUCTOS DETECTADOS'. "
                "Menciona los productos INDIVIDUALES por nombre; si hay línea, úsala para agruparlos. "
                "NO menciones '4Life' como marca aislada ni 'generar ingresos'. "
                "Un solo emoji al final si encaja."
            )
        else:
            # Solo línea detectada, sin productos individuales
            tema_salud = (
                f"la línea {nombre_linea}" if nombre_linea
                else (contexto_usuario or resumen_fb or "sus productos de salud")
            )
            prompt_usuario = (
                f"Hora del día: {saludo}.\n"
                f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
                f"CONTEXTO: El usuario llegó desde una publicación sobre: {tema_salud}\n"
                "Escribe el saludo siguiendo las instrucciones para 'publicación FB sin productos específicos'. "
                "Menciona el tema/línea de forma natural, sin enumerar productos individuales. "
                "NO menciones '4Life' como marca aislada ni 'generar ingresos'. "
                "Un solo emoji al final si encaja."
            )
    else:
        prompt_usuario = (
            f"Hora del día: {saludo}.\n"
            f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
            "Escribe el mensaje de bienvenida siguiendo todas las instrucciones del sistema."
        )

    _pares_p1 = _formatear_ejemplos_entrenamiento(await _crm_get_entrenamiento(limit=3))
    messages = [
        {"role": "system", "content": _PASO1_SYSTEM + _construir_addon_reglas("paso1")},
    ]
    if _pares_p1:
        messages.append({"role": "system", "content": _pares_p1})
    messages.append({"role": "user", "content": prompt_usuario})

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
        lista = ", ".join(productos[:3]) if productos else ""
        contexto = lista if lista else (analisis.get("resumen_para_bot") or resumen_historial or "nuestra publicación")
        # Detectar categoría/línea de los productos detectados para enriquecer la pregunta
        linea_paso2 = _detectar_linea_productos(productos) if productos else (analisis.get("nombre_linea") or "")
        linea_ctx = f" (línea de {linea_paso2})" if linea_paso2 else ""
        if lista:
            return (
                f"El cliente llegó interesado en estos productos de 4Life: {lista}{linea_ctx}. "
                "Pregunta de forma natural qué quiere MEJORAR o FORTALECER. "
                "Menciona 1 o 2 de los productos por nombre dentro de la pregunta de forma conversacional "
                "(ej. '¿Qué te gustaría mejorar con el PreBiotics y el Aloe Vera Stix?'). "
                "NO ofrezcas la opción de 'generar ingresos' — el cliente está enfocado en productos."
            )
        else:
            return (
                f"El cliente llegó desde nuestra publicación sobre: {contexto}. "
                "Pregunta de forma natural qué quiere MEJORAR o FORTALECER en relación a ese tema de salud. "
                "NO menciones nombres de productos específicos ni 'generar ingresos'."
            )
    else:
        return (
            "No hay productos específicos detectados. "
            "Pregunta de forma abierta y natural qué producto busca o qué necesidad quiere resolver "
            "(energía, inmunidad, digestión, peso, bienestar general, etc.)."
        )


async def _responder_paso2(
    texto_usuario: str,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
) -> str | None:
    """PASO 2 — Manejo del nombre + pregunta sobre conocimiento de la compañía."""
    _pares_p2 = _formatear_ejemplos_entrenamiento(
        await _crm_get_entrenamiento(q=texto_usuario, limit=3)
    )
    messages = [
        {"role": "system", "content": _PASO2_SYSTEM + _construir_addon_reglas("paso2")},
    ]
    if _pares_p2:
        messages.append({"role": "system", "content": _pares_p2})
    messages.extend([
        {
            "role": "system",
            "content": f"HISTORIAL:\n{historial_texto}",
        },
        {"role": "user", "content": texto_usuario},
    ])

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


async def _responder_paso2b(
    texto_usuario: str,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
) -> str | None:
    """PASO 2B — Respuesta al conocimiento de la compañía + pregunta sobre padecimiento."""
    _pares_p2b = _formatear_ejemplos_entrenamiento(
        await _crm_get_entrenamiento(q=texto_usuario, limit=3)
    )
    messages = [
        {"role": "system", "content": _PASO2B_SYSTEM + _construir_addon_reglas("paso2b")},
    ]
    if _pares_p2b:
        messages.append({"role": "system", "content": _pares_p2b})
    messages.extend([
        {
            "role": "system",
            "content": f"HISTORIAL:\n{historial_texto}",
        },
        {"role": "user", "content": texto_usuario},
    ])

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
                    "max_tokens": 150,
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
    preguntas_restantes: int = 2,
) -> str | None:
    """PASO 3 — Diagnóstico profundo: entender el problema antes de recomendar."""
    contexto_extra = ""
    if analisis.get("resumen_para_bot"):
        contexto_extra = f"\nContexto de llegada del usuario: {analisis['resumen_para_bot']}"

    system_prompt = _PASO3_SYSTEM.format(preguntas_restantes=preguntas_restantes)
    _pares_p3 = _formatear_ejemplos_entrenamiento(
        await _crm_get_entrenamiento(q=texto_usuario, limit=3)
    )
    messages = [
        {"role": "system", "content": system_prompt + contexto_extra + _construir_addon_reglas("paso3")},
    ]
    if _pares_p3:
        messages.append({"role": "system", "content": _pares_p3})
    messages.extend([
        {
            "role": "system",
            "content": f"HISTORIAL:\n{historial_texto}",
        },
        {"role": "user", "content": texto_usuario},
    ])

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
                    "max_tokens": 200,
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


async def _responder_urgencia(
    texto_usuario: str,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
) -> str | None:
    """Responde a un usuario que llega en estado de urgencia o desesperación.

    Salta PASO2/2B y responde con empatía profunda + una pregunta clínica directa.
    """
    if not OPENAI_API_KEY:
        return None

    _pares = _formatear_ejemplos_entrenamiento(
        await _crm_get_entrenamiento(q="urgencia grave quimioterapia desesperado critico", limit=3)
    )
    messages = [
        {"role": "system", "content": _PASO_URGENCIA_SYSTEM + _construir_addon_reglas("paso3")},
    ]
    if _pares:
        messages.append({"role": "system", "content": _pares})
    if historial_texto.strip():
        messages.append({"role": "system", "content": f"HISTORIAL:\n{historial_texto}"})
    messages.append({"role": "user", "content": texto_usuario})

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.8,
                    "max_tokens": 200,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            texto = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"[Urgencia] respuesta generada para: {texto_usuario[:60]!r}")
            return texto
    except Exception as e:
        print(f"[Urgencia] error: {e}")
        return None


async def _clasificar_post_video(texto_usuario: str) -> str:
    """Clasifica la intención del usuario después de recibir los videos recomendados.

    Retorna:
      "nuevo_producto" — pregunta por otro producto o tiene otra necesidad de salud.
      "pausar"         — pregunta por precio/costo, dice que después decide, o cierra la
                         conversación de forma que no tiene sentido seguir el flujo.
    """
    if not OPENAI_API_KEY:
        return "nuevo_producto"

    prompt = (
        f"El usuario acaba de recibir los videos de los productos recomendados por WhatsApp. "
        f"Ahora envía este mensaje: \"{texto_usuario}\"\n\n"
        "Clasifica el mensaje en UNA de estas categorías:\n"
        "1. nuevo_producto — el usuario quiere información sobre otro producto, tiene otra "
        "   necesidad de salud, pregunta algo sobre los productos mostrados, o expresa interés "
        "   en continuar la conversación.\n"
        "2. pausar — el usuario pregunta por precio/costo/cuánto vale, dice que después te "
        "   dice, que lo piensa, que no le interesa por ahora, se despide, o cualquier frase "
        "   que indique que no desea continuar el flujo de asesoría en este momento.\n\n"
        "Responde ÚNICAMENTE con 'nuevo_producto' o 'pausar'."
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0,
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            resultado = resp.json()["choices"][0]["message"]["content"].strip().lower()
            clasificacion = "pausar" if "pausar" in resultado else "nuevo_producto"
            print(f"[PostVideo] clasificacion={clasificacion!r} texto={texto_usuario[:80]!r}")
            return clasificacion
    except Exception as e:
        print(f"[PostVideo] error clasificando: {e}")
        return "nuevo_producto"


# ─────────────────────────────────────────────────────────────────────────────
# Función principal
# ─────────────────────────────────────────────────────────────────────────────
# System prompt para análisis clínico de la entrevista (PASO 4)
_PASO4_ANALISIS_SYSTEM = """Eres un médico especialista en medicina integrativa y nutrición clínica con 20 años de experiencia.
Tu tarea es analizar una entrevista de salud registrada en una conversación de WhatsApp y determinar,
con la mayor precisión clínica posible, qué condición o conjunto de condiciones padece el paciente.

CRITERIOS DE ANÁLISIS:
- Basa tu análisis ÚNICAMENTE en los síntomas, duración, factores e historial descritos en la conversación.
- Sé MUY ESPECÍFICO en el diagnóstico. Ejemplos de precisión requerida:
    * NO "problemas digestivos" → SÍ "síndrome de intestino irritable" o "reflujo gastroesofágico crónico"
    * NO "cansancio" → SÍ "fatiga crónica" o "anemia ferropénica" o "hipotiroidismo subclinico"
    * NO "dolor de cabeza" → SÍ "migraña tensional" o "cefalea crónica por estrés"
    * NO "dolor articular" → SÍ "artritis reumatoide temprana" o "artrosis de rodilla bilateral"
    * NO "problemas hormonales" → SÍ "síndrome de ovario poliquístico" o "menopausia temprana"
- Si los síntomas apuntan a más de una condición, lista todas en el diagnóstico diferencial.
- Identifica las causas raíz más probables, no solo los síntomas superficiales.
- Los términos de búsqueda deben ser palabras clave técnicas Y coloquiales que permitan encontrar
  suplementos relevantes en un catálogo (ej: "reflujo acidez digestión enzimas probióticos").

IMPORTANTE: No diagnostiques al paciente directamente — tu análisis es para uso interno del sistema
para seleccionar los suplementos más apropiados."""


async def _analizar_entrevista_paso4(historial_texto: str) -> dict:
    """Analiza el historial de PASO 3 con LLM clínico y extrae condición precisa, síntomas y búsqueda.

    Retorna dict con:
      condicion_principal  : str  — ej. "síndrome de intestino irritable"
      diagnostico_diferencial: list — ej. ["colitis ulcerosa leve", "sobrecrecimiento bacteriano"]
      sintomas             : list — ej. ["distensión abdominal", "dolor tipo cólico", "alternancia diarrea-estreñimiento"]
      causas_posibles      : list — ej. ["disbiosis intestinal", "estrés crónico", "dieta alta en ultraprocesados"]
      terminos_busqueda    : str  — palabras clave técnicas y coloquiales para el catálogo de suplementos
      severidad            : str  — "leve" | "moderada" | "severa"
    """
    if not OPENAI_API_KEY or not historial_texto:
        return {}

    user_prompt = (
        f"CONVERSACIÓN DE ENTREVISTA DE SALUD:\n{historial_texto}\n\n"
        "Analiza esta conversación y responde ÚNICAMENTE con JSON válido (sin bloques markdown) "
        "con esta estructura exacta:\n"
        '{"condicion_principal":"nombre clínico específico de la condición principal",'
        '"diagnostico_diferencial":["condición alternativa 1","condición alternativa 2"],'
        '"sintomas":["síntoma específico 1","síntoma específico 2","síntoma específico 3"],'
        '"causas_posibles":["causa raíz 1","causa raíz 2"],'
        '"terminos_busqueda":"palabras clave técnicas y coloquiales para buscar suplementos en catálogo",'
        '"severidad":"leve|moderada|severa"}'
    )

    try:
        async with httpx.AsyncClient(timeout=max(OPENAI_TIMEOUT, 30.0)) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "temperature": 0.1,
                    "max_tokens": 500,
                    "messages": [
                        {"role": "system", "content": _PASO4_ANALISIS_SYSTEM + _construir_addon_reglas("paso4")},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            # Eliminar bloque markdown si el modelo lo incluyó
            if content.startswith("```"):
                content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
            result = json.loads(content)
            print(f"[PASO4] analisis_clinico={result!r}")
            return result
    except Exception as e:
        print(f"[PASO4] Error en _analizar_entrevista_paso4: {e}")
        return {}


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

    # --- Manejo: si la conversación previa preguntó por videos y el usuario responde afirmativamente
    try:
        if historial_texto and texto_usuario:
            # Detectar si el usuario dice "sí" a los videos
            user_yes = bool(re.search(
                r"^\s*(si|sí|s)(\b|\W)|si por favor|sí por favor|si,|sí,|claro|dale|mándalo|envialo|envíalo",
                texto_usuario.strip().lower()
            ))
            if user_yes:
                # PRIMARIO: buscar marcador [[PRODUTOS_IDS:id1|id2]] (insertado por PASO4)
                ids_marker = re.search(r"\[\[PRODUTOS_IDS:([^\]]+)\]\]", historial_texto)
                # SECUNDARIO: revisar el último mensaje COMPLETO del bot (DOTALL) en busca de "video"
                all_bot_msgs = re.findall(r"Bot:\s*(.*?)(?=\nUsuario:|\Z)", historial_texto, re.DOTALL)
                last_bot_full = (all_bot_msgs[-1] if all_bot_msgs else "").strip()
                bot_asked_video = bool(ids_marker) or bool(
                    re.search(r"deseas ver|quieres ver|deseas que te comparta|responde \*?s[ií]\*?", last_bot_full, re.IGNORECASE)
                )
                if bot_asked_video:
                    medios = []
                    if ids_marker:
                        # Obtener productos por IDs via endpoint batch (?__ids=1,2,3)
                        prod_ids = [int(x.strip()) for x in ids_marker.group(1).split("|") if x.strip().isdigit()]
                        print(f"[VideoHandler] buscando videos para prod_ids={prod_ids}")
                        if prod_ids:
                            ids_csv = ",".join(str(i) for i in prod_ids)
                            batch_res = await _crm_get("productos", {"__ids": ids_csv, "per_page": 50})
                            batch_prods = []
                            if batch_res:
                                raw = batch_res.get("data") if isinstance(batch_res, dict) and batch_res.get("data") is not None else batch_res
                                batch_prods = raw if isinstance(raw, list) else []
                            # Fallback: individual lookups if batch returned nothing
                            if not batch_prods:
                                for pid in prod_ids:
                                    pd = await _crm_get_by_id("productos", pid)
                                    if pd:
                                        batch_prods.append(pd)
                            for prod_data in batch_prods:
                                datos_layer = prod_data.get("datos") if isinstance(prod_data.get("datos"), dict) else prod_data
                                pid = prod_data.get("id", "?")
                                print(f"[VideoHandler] id={pid} campos={list(datos_layer.keys())[:20]}")
                                video_url = _pick_video(prod_data)
                                _nombre_raw = _pick_field(prod_data, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or f"Producto {pid}"
                                nombre = _nombre_producto_limpio(_nombre_raw) or str(_nombre_raw)
                                print(f"[VideoHandler] id={pid} nombre={nombre!r} video_url={video_url!r}")
                                if video_url:
                                    medios.append({"tipo": "video", "url": video_url, "caption": nombre})
                    else:
                        # Fallback: extraer nombres del marcador antiguo o corchetes
                        name_marker = re.search(r"\[\[PRODUCTOS:([^\]]+)\]\]", historial_texto)
                        if name_marker:
                            prod_names = [n.strip() for n in name_marker.group(1).split("|") if n.strip()]
                        else:
                            m = re.search(r"\[([^\[\]]{3,120})\]", last_bot_full)
                            prod_names = [p.strip() for p in m.group(1).split(",") if p.strip()] if m else []
                        if prod_names:
                            todos = await _obtener_todos_productos()
                            for name in prod_names:
                                name_lower = name.lower()
                                match_prod = None
                                for p in todos:
                                    pnombre = str(_pick_field(p, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or "").lower()
                                    if pnombre == name_lower or name_lower in pnombre or pnombre in name_lower:
                                        match_prod = p
                                        break
                                if match_prod:
                                    video_url = _pick_video(match_prod)
                                    if video_url:
                                        medios.append({"tipo": "video", "url": video_url, "caption": name})

                    if medios:
                        return {"texto": "Aquí tienes los videos de los productos recomendados 🎥", "medios": medios}
                    else:
                        return "No encontré videos disponibles para esos productos en este momento."

            # Detectar si el usuario rechaza los videos ("no")
            user_no = bool(re.search(
                r"^\s*no\b|no gracias|no quiero|no,? gracias|^nope|^nel\b|^paso\b",
                texto_usuario.strip().lower()
            ))
            if user_no:
                all_bot_msgs_no = re.findall(r"Bot:\s*(.*?)(?=\nUsuario:|\Z)", historial_texto, re.DOTALL)
                last_bot_no = (all_bot_msgs_no[-1] if all_bot_msgs_no else "").strip()
                bot_pregunto_video = bool(re.search(
                    r"deseas que te comparta los videos|deseas ver los videos|quieres ver los videos",
                    last_bot_no, re.IGNORECASE
                ))
                if bot_pregunto_video:
                    print(f"[VideoHandler] usuario rechazó los videos → pausando")
                    return {"texto": None, "pausar": True, "motivo": "rechazo_videos"}
    except Exception as _ve:
        print(f"[VideoHandler] error: {_ve}")

    # ── POST-VIDEO: el bot ya envió los videos y el usuario envía un nuevo mensaje ──
    # Detectar si el último mensaje del bot fue el envío de videos. Si es así,
    # clasificar la intención: "nuevo_producto" → reiniciar entrevista (PASO3),
    # "pausar" → señal de pausa para que main.py detenga el bot en el CRM.
    try:
        if historial_texto:
            all_bot_msgs_pv = re.findall(r"Bot:\s*(.*?)(?=\nUsuario:|\Z)", historial_texto, re.DOTALL)
            last_bot_pv = (all_bot_msgs_pv[-1] if all_bot_msgs_pv else "").strip()
            bot_envio_videos = bool(re.search(
                r"(aquí tienes los videos|aqui tienes los videos"
                r"|videos de los productos|no encontré videos|no encontre videos)",
                last_bot_pv, re.IGNORECASE
            ))
            if bot_envio_videos:
                intento_pv = await _clasificar_post_video(texto_usuario)
                if intento_pv == "pausar":
                    return {"texto": None, "pausar": True, "motivo": "post_video_cierre_usuario"}
                # nuevo_producto → reiniciar entrevista clínica (PASO3) para el nuevo tema.
                print(f"[PostVideo] nuevo ciclo de indagación para: {texto_usuario[:80]!r}")
                return await _responder_paso3(
                    texto_usuario, historial_texto, analisis, intencion,
                    preguntas_restantes=2,
                )
    except Exception as _pv_e:
        print(f"[PostVideo] error: {_pv_e}")

    # ── DETECCIÓN TEMPRANA DE PRECIO: pausar en cualquier etapa antes de PASO4 ──
    # Si el usuario pregunta precio/costo al inicio o durante la entrevista,
    # ya conoce los productos → pausar para que lo atienda un humano.
    _precio_pattern = re.compile(
        r"\b(precio|costo|cuánto|cuanto|cuánto cuesta|cuanto cuesta|cuánto vale|cuanto vale"
        r"|cuánto es|cuanto es|cuánto cobr|cuanto cobr|cuánto están|cuanto están"
        r"|cuánto tiene|cuanto tiene|cuál es el precio|cual es el precio"
        r"|a cuánto|a cuanto|valor|tarifa|inversión|inversion|cotiza"
        r"|qué precio|que precio|dame el precio|quiero saber el precio"
        r"|cuánto me sale|cuanto me sale|sale el|cuánto sale|cuanto sale)\b",
        re.IGNORECASE,
    )
    if _precio_pattern.search(texto_usuario):
        print(f"[PrecioTemprano] precio detectado antes de PASO4 → pausando")
        return {"texto": None, "pausar": True, "motivo": "precio_temprano"}

    # ── PASO 1: Primer contacto (sin historial o sin respuesta previa del bot) ─
    if not historial_texto.strip() or _contar_turnos_bot(historial_texto) == 0:
        # Si el primer mensaje ya indica urgencia, no pedir el nombre — ir directo a empatía
        if _es_urgente(texto_usuario):
            print(f"[Urgencia] detectada en PASO1 → flujo urgencia desde primer contacto")
            return await _responder_urgencia(texto_usuario, historial_texto, analisis, intencion)
        return await _responder_paso1(instancia, analisis, intencion)

    _turnos = _contar_turnos_bot(historial_texto)
    _MARKER_PASO3_DONE = "Estoy examinando tu situación"
    _en_urgencia = _es_urgente(texto_usuario) or _historial_tiene_urgencia(historial_texto)

    # ── FLUJO DE URGENCIA: persona desesperada, situación crítica de salud ──────
    # Si se detecta urgencia (desesperación, quimioterapia, enfermedad grave, etc.)
    # se omiten PASO2 y PASO2B — responder con empatía + entrevista clínica directa.
    if _en_urgencia:
        if _turnos == 1:
            # Primera respuesta tras PASO1: empatía profunda + pregunta clínica directa
            print(f"[Urgencia] detectada en turno 1 → flujo urgencia activado")
            return await _responder_urgencia(texto_usuario, historial_texto, analisis, intencion)
        # turnos >= 2: ya se respondió con empatía, permitir máximo 1 pregunta clínica más
        _preguntas_hechas_urg = _turnos - 2
        if _preguntas_hechas_urg < 1:
            print(f"[Urgencia] turno {_turnos} → PASO3 urgencia preguntas_restantes=1")
            return await _responder_paso3(
                texto_usuario, historial_texto, analisis, intencion,
                preguntas_restantes=1,
            )
        # ≥1 pregunta clínica realizada → caer a PASO4 directamente

    # ── PASO 2: Segunda respuesta — manejo del nombre + pregunta sobre la compañía ──
    if not _en_urgencia and _turnos == 1:
        return await _responder_paso2(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 2B: Respuesta al conocimiento de la compañía + pregunta de padecimiento ──
    if not _en_urgencia and _turnos == 2:
        return await _responder_paso2b(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 3: Diagnóstico profundo (hasta que el bot tenga suficiente info) ─
    # Se detecta el fin de PASO 3 por la presencia del marcador en historial
    # o cuando se alcanza el límite duro de 2 preguntas
    if not _en_urgencia:
        preguntas_paso3 = max(0, _turnos - 3)
        preguntas_restantes = max(0, 2 - preguntas_paso3)
        if _MARKER_PASO3_DONE not in historial_texto and preguntas_paso3 < 2:
            return await _responder_paso3(
                texto_usuario, historial_texto, analisis, intencion,
                preguntas_restantes=preguntas_restantes,
            )

    # ── POST-PASO4: Si ya se enviaron productos, pausar ante CUALQUIER mensaje ──
    # Señales que indican que PASO4 ya corrió y envió la información:
    #   a) marcador [[PRODUTOS_IDS:...]] en el historial (productos sin video)
    #   b) frase “Te comparto los videos” (envío automático de videos)
    #   c) tarjetas de producto en formato negrita Bot: *Nombre*\n (JSON path)
    _paso4_ya_envio = (
        bool(re.search(r"\[\[PRODUTOS_IDS:", historial_texto))
        or bool(re.search(r"te comparto los videos|deseas que te comparta los videos", historial_texto, re.IGNORECASE))
        or (
            _MARKER_PASO3_DONE in historial_texto
            and bool(re.search(r"Bot:.*\*[A-Za-záéíóúÁÉÍÓÚñÑ]", historial_texto))
        )
    )
    if _paso4_ya_envio:
        print(f"[PostP4] productos ya enviados → pausando")
        return {"texto": None, "pausar": True, "motivo": "post_recomendacion"}

    # ── PASO 4: Recomendación de productos (PASO 3 ya completado) ─────────────
    # Paso 4a — Analizar la entrevista PASO3 para extraer síntomas/condición/términos de búsqueda
    analisis_entrevista: dict = {}
    try:
        analisis_entrevista = await _analizar_entrevista_paso4(historial_texto)
    except Exception as e:
        print(f"[PASO4] Error llamando _analizar_entrevista_paso4: {e}")

    # Términos limpios para el catálogo (producto del análisis LLM)
    terminos_catalogo = analisis_entrevista.get("terminos_busqueda", "")
    condicion_detectada = analisis_entrevista.get("condicion_principal", "")
    sintomas_detectados: list[str] = analisis_entrevista.get("sintomas") or []

    contexto_partes: list[str] = []

    # Productos detectados en imagen/publicación
    productos_mencionados: list[str] = intencion.get("productos_mencionados") or []
    if isinstance(analisis.get("items"), list):
        productos_mencionados = list({*productos_mencionados, *analisis["items"]})
    if productos_mencionados:
        contexto_partes.append(f"Productos detectados en el contexto: {', '.join(productos_mencionados)}")

    # Condición y síntomas detectados en la entrevista
    if condicion_detectada:
        contexto_partes.append(f"Condición detectada en entrevista: {condicion_detectada}")
    if sintomas_detectados:
        contexto_partes.append(f"Síntomas: {', '.join(sintomas_detectados)}")

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

    # ── Buscar en catálogos usando condición, causas y términos del análisis ──
    try:
        productos_catalogo, paquete_encontrado = await _buscar_en_catalogos(
            historial_texto, analisis, intencion, texto_usuario,
            terminos_busqueda=terminos_catalogo,
            condicion_detectada=condicion_detectada,
            causas_posibles=analisis_entrevista.get("causas_posibles") or [],
            sintomas=sintomas_detectados,
        )
    except Exception:
        productos_catalogo = []
        paquete_encontrado = None

    if productos_catalogo:
        # Condición clínica completa para el prompt de recomendación
        diferencial: list[str] = analisis_entrevista.get("diagnostico_diferencial") or []
        severidad: str = analisis_entrevista.get("severidad") or ""
        condicion = condicion_detectada or intencion.get("resumen") or analisis.get("contexto_usuario") or ""
        condicion_completa = condicion
        if sintomas_detectados:
            condicion_completa += f" — síntomas: {', '.join(sintomas_detectados)}"
        if diferencial:
            condicion_completa += f" — diferencial: {', '.join(diferencial)}"
        if severidad:
            condicion_completa += f" — severidad: {severidad}"

        # Contexto del paquete si fue encontrado
        paquete_ctx = ""
        if paquete_encontrado:
            paq_nombre = _pick_field(paquete_encontrado, ["NOMBRE_PAQUETE", "nombre_paquete", "NOMBRE", "nombre", "name"]) or ""
            paq_desc = _pick_field(paquete_encontrado, ["DESCRIPCION", "descripcion", "description"]) or ""
            if paq_nombre or paq_desc:
                paquete_ctx = f"\nPaquete recomendado: {paq_nombre}\nDescripción del paquete: {paq_desc}"

        import json as _json

        partes_prod = []
        img_map: dict[str, str] = {}   # nombre_lower → url_imagen
        for p in productos_catalogo[:6]:
            nombre = str(_pick_field(p, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or p.get("nombre") or "")
            descripcion = str(_pick_field(p, ["DESCRIPCION", "descripcion", "description"]) or p.get("descripcion") or "")
            imagen = _pick_imagen(p)
            datos_layer = p.get("datos") if isinstance(p.get("datos"), dict) else p
            es_b64 = imagen.startswith('data:image/') if imagen else False
            print(f"[PASO4] prod id={p.get('id')} campos={list(datos_layer.keys())[:20]} "
                  f"imagen={'base64({} chars)'.format(len(imagen)) if es_b64 else repr(imagen)}")
            partes_prod.append(f"Nombre: {nombre}\nDescripción: {descripcion}")
            if nombre and imagen:
                img_map[nombre.lower()] = imagen

        # Marcador IDs para recuperar videos en el siguiente turno
        ids_str = "|".join(str(p.get("id", "")) for p in productos_catalogo[:6] if p.get("id"))
        ids_marker = f"[[PRODUTOS_IDS:{ids_str}]]" if ids_str else ""

        # Si el cliente llegó preguntando por un producto específico, avisarlo al LLM
        _productos_pedidos_str = ", ".join(productos_mencionados) if productos_mencionados else ""
        _regla_producto_especifico = (
            f"REGLA PRIORITARIA: El cliente preguntó específicamente por {_productos_pedidos_str}. "
            "Habla ÚNICAMENTE de ese producto. NO ofrezcas otros aunque aparezcan en el catálogo. "
            if _productos_pedidos_str else ""
        )

        instruccion_contexto = (
            f"El cliente tiene: {condicion_completa}. "
            + (f"Paquete: '{str(_pick_field(paquete_encontrado, ['NOMBRE_PAQUETE','nombre_paquete','NOMBRE','nombre','name']) or '')}'. " if paquete_encontrado else "")
            + _regla_producto_especifico
            + "Explica en 2 frases directas cómo cada producto ayuda a su caso. "
            + "SOLO menciona productos de 'Productos del catálogo'. "
            + "Si ninguno aplica, deja 'productos' vacío — no inventes. "
            + "Responde SOLO con JSON válido (sin texto fuera del JSON):\n"
            '{"intro": "1 frase corta y natural sobre su situación", '
            '"productos": [{"nombre": "nombre exacto del producto", "descripcion": "2 frases directas sobre cómo ayuda a su caso"}]}'
        )
        user_prompt = (
            f"Condición clínica del cliente: {condicion_completa}{paquete_ctx}\n"
            "Productos del catálogo:\n"
            + "\n---\n".join(partes_prod)
            + f"\n\n{instruccion_contexto}"
        )

        _pares_p4 = _formatear_ejemplos_entrenamiento(
            await _crm_get_entrenamiento(q=condicion_detectada or terminos_catalogo, limit=5)
        )
        messages = [
            {"role": "system", "content": _PRODUCTOS_SYSTEM + _construir_addon_reglas("productos")},
        ]
        if _pares_p4:
            messages.append({"role": "system", "content": _pares_p4})
        messages.append({"role": "user", "content": user_prompt})

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
                        "max_tokens": 700,
                        "response_format": {"type": "json_object"},
                        "messages": messages,
                    },
                )
                resp.raise_for_status()
                raw_content = resp.json()["choices"][0]["message"]["content"].strip()

                # Intentar parsear JSON estructurado para enviar cada producto por separado
                try:
                    llm_data = _json.loads(raw_content)
                    intro_text = str(llm_data.get("intro") or "").strip()
                    llm_prods = llm_data.get("productos") or []
                    if intro_text and llm_prods:
                        print(f"[PASO4] img_map keys={list(img_map.keys())} count={len(img_map)}")
                        mensagens: list[dict] = [{"texto": intro_text}]
                        for prod_info in llm_prods[:6]:
                            prod_nombre = str(prod_info.get("nombre") or "").strip()
                            prod_desc = str(prod_info.get("descripcion") or "").strip()
                            if not prod_nombre:
                                continue
                            # Buscar imagen por nombre (coincidencia parcial)
                            img_url = ""
                            for key, url in img_map.items():
                                if key in prod_nombre.lower() or prod_nombre.lower() in key:
                                    img_url = url
                                    break
                            es_b64_img = img_url.startswith('data:image/') if img_url else False
                            print(f"[PASO4] prod_nombre={prod_nombre!r} "
                                  f"img={'base64({} chars)'.format(len(img_url)) if es_b64_img else repr(img_url)}")
                            prod_medios = [{"tipo": "imagen", "url": img_url, "caption": prod_nombre}] if img_url else []
                            mensagens.append({"texto": f"*{prod_nombre}*\n{prod_desc}", "medios": prod_medios})
                        # Preguntar si desea ver los videos (solo si hay alguno disponible)
                        _hay_videos = any(_pick_video(_p) for _p in productos_catalogo[:6])
                        if ids_marker and _hay_videos:
                            mensagens.append({"texto": f"¿Deseas que te comparta los videos sobre los productos? {ids_marker}"})
                        elif ids_marker:
                            mensagens.append({"texto": ids_marker})
                        return {"mensagens": mensagens}
                except Exception as _je:
                    print(f"[PASO4] JSON parse error: {_je} — usando texto plano")

                # Fallback: texto plano — preguntar si desea ver los videos
                recomendacion = raw_content
                _hay_videos_fb = any(_pick_video(_p) for _p in productos_catalogo[:6])
                medios_imagenes_fallback = [{"tipo": "imagen", "url": url, "caption": n} for n, url in img_map.items()]
                if ids_marker and _hay_videos_fb:
                    pregunta_video = f"¿Deseas que te comparta los videos sobre los productos? {ids_marker}"
                    if medios_imagenes_fallback:
                        return {"mensagens": [{"texto": recomendacion, "medios": medios_imagenes_fallback}, {"texto": pregunta_video}]}
                    return {"mensagens": [{"texto": recomendacion}, {"texto": pregunta_video}]}
                if ids_marker:
                    recomendacion += "\n" + ids_marker
                if medios_imagenes_fallback:
                    return {"texto": recomendacion, "medios": medios_imagenes_fallback}
                return recomendacion
        except Exception as _e:
            print(f"[PASO4] error LLM: {_e}")

    # Sin productos en catálogo → no responder y pausar la conversación
    # (el bot SIEMPRE depende del catálogo; nunca genera respuestas genéricas en PASO4)
    print(f"[PASO4] sin productos en catálogo para condicion='{condicion_detectada}' → pausando")
    return {"texto": None, "pausar": True, "motivo": "sin_productos_catalogo"}

