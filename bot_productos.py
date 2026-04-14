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
  5. ¿Ha probado algo antes? ¿Con qué resultado?

REGLAS ESTRICTAS:
- Haz SIEMPRE solo UNA pregunta por mensaje. Corta, directa y cálida.
- MÁXIMO 2 líneas por respuesta. SIN emojis — el tono cálido lo transmites con las palabras.
- NO recomiendes productos todavía.
- NO menciones precios, marcas ni 4Life.
- REVISA el historial antes de preguntar; JAMÁS repitas una pregunta que ya fue respondida.
- PREGUNTAS RESTANTES: {preguntas_restantes}. Cuando llegues a 0 debes usar [[LISTO]] obligatoriamente.

CUANDO TENGAS SUFICIENTE INFORMACIÓN (problema, duración, causas, impacto):
- Inicia tu respuesta con la línea exacta: [[LISTO]]
- Luego escribe un mensaje corto empático de cierre, sin emojis, ej:
  "Perfecto, con lo que me has contado ya tengo todo lo que necesito."
- Usa [[LISTO]] también cuando se agoten las preguntas restantes, aunque no tengas todo.
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


# ── Helpers CRM / catálogos
CRM_URL = os.getenv("CRM_URL", "").rstrip("/")
CRM_TENANT = os.getenv("CRM_TENANT", "")
CRM_API_TOKEN = os.getenv("CRM_API_TOKEN", "")
CRM_TIMEOUT = float(os.getenv("CRM_TIMEOUT", "8"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "10"))


async def _crm_get(module: str, params: dict | None = None) -> list | dict | None:
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
        return None
    url = f"{CRM_URL}/api/v1/{CRM_TENANT}/{module}"
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-API-Key": CRM_API_TOKEN}, params=params or {})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[CRM] error GET {module}: {e}")
        return None


async def _crm_get_by_id(module: str, id: Any) -> dict | None:
    if not (CRM_URL and CRM_TENANT and CRM_API_TOKEN):
        return None
    url = f"{CRM_URL}/api/v1/{CRM_TENANT}/{module}/{id}"
    try:
        async with httpx.AsyncClient(timeout=CRM_TIMEOUT) as client:
            resp = await client.get(url, headers={"X-API-Key": CRM_API_TOKEN})
            resp.raise_for_status()
            data = resp.json()
            # Algunos endpoints devuelven {data: {...}}
            if isinstance(data, dict) and data.get("data") is not None:
                return data.get("data")
            return data
    except Exception as e:
        print(f"[CRM] error GET {module}/{id}: {e}")
        return None


def _pick_field(rec: dict, keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


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
    out = []
    for i in ids:
        try:
            rec = await _crm_get_by_id("productos", i)
            if rec:
                out.append(rec)
        except Exception:
            continue
    return out


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


async def _buscar_en_catalogos(historial_texto: str, analisis: dict, intencion: dict, texto_usuario: str) -> list:
    """Busca productos en testimonios → paquetes → productos según la lógica indicada.
    Devuelve lista de registros de producto (posiblemente vacía).
    """
    # Construir query base usando lo que tengamos del análisis / intención / historial
    partes = []
    if intencion.get("resumen"):
        partes.append(intencion.get("resumen"))
    if analisis.get("resumen_para_bot"):
        partes.append(analisis.get("resumen_para_bot"))
    if analisis.get("contexto_usuario"):
        partes.append(analisis.get("contexto_usuario"))
    # últimas líneas de usuario del historial
    if historial_texto:
        usuarios = re.findall(r"Usuario:\s*(.+)", historial_texto)
        if usuarios:
            partes.append(usuarios[-1])

    query = " ".join(p for p in partes if p)[:800]

    # 1) Buscar testimonios
    try:
        if query:
            tests = await _buscar_testimonios_por_condicion(query)
        else:
            tests = []
    except Exception:
        tests = []

    # Elegir primer testimonio relevante
    chosen = None
    if tests:
        for t in tests:
            # Campos posibles
            condicion = _pick_field(t, ["CONDICION_CRONICA", "condicion_cronica", "condicion", "CONDICION", "condicion_cronica_text"]) or ""
            descripcion = _pick_field(t, ["DESCRIPCION", "descripcion", "description"]) or ""
            # comparar por substring o similitud
            if condicion and (condicion.lower() in query.lower() or query.lower() in condicion.lower()):
                chosen = t
                break
        if not chosen:
            # fallback: elegir el primero
            chosen = tests[0]

    product_records: list = []

    # Si encontramos testimonio, buscar productos sugeridos (interpretar como id de paquete o ids)
    if chosen:
        sugeridos = _pick_field(chosen, ["PRODUCTOS_SUGERIDOS", "productos_sugeridos", "productos", "PRODUCTOS"])
        ids = _extract_ids_from_field(sugeridos)
        # Si son IDs de paquete, obtener productos del paquete
        if ids:
            # Intentar interpretar como paquetes primero
            for pid in ids:
                paquete = await _crm_get_by_id("paquetes", pid)
                if paquete:
                    # extraer ids de productos del paquete
                    prod_field = _pick_field(paquete, ["PRODUCTOS", "productos", "productos_ids", "productos_sugeridos"]) or ""
                    prod_ids = _extract_ids_from_field(prod_field)
                    if prod_ids:
                        prods = await _obtener_productos_por_ids(prod_ids)
                        product_records.extend(prods)
            # Si no se obtuvieron productos desde paquetes, interpretar ids como productos directos
            if not product_records:
                prods = await _obtener_productos_por_ids(ids)
                product_records.extend(prods)

    # 2) Si no hay productos por testimonios → buscar paquetes por query
    if not product_records:
        paquetes = await _buscar_paquetes_por_query(query)
        if paquetes:
            # elegir el paquete más relevante (primer elemento)
            paquete = paquetes[0]
            prod_field = _pick_field(paquete, ["PRODUCTOS", "productos", "productos_ids"]) or ""
            prod_ids = _extract_ids_from_field(prod_field)
            if prod_ids:
                prods = await _obtener_productos_por_ids(prod_ids)
                product_records.extend(prods)

    # 3) Si aún no hay productos → buscar directamente en productos
    if not product_records:
        productos = await _buscar_productos_por_query(query, per_page=12)
        if productos:
            product_records.extend(productos)

    # 4) Filtrar por productos que venían en la publicación (si aplica)
    productos_fb = intencion.get("productos_mencionados") or []
    items_fb = []
    if isinstance(analisis.get("items"), list):
        items_fb = analisis.get("items")
    productos_fb = [p for p in (productos_fb or []) if p] + [p for p in items_fb if p]
    productos_fb = [p.lower() for p in productos_fb]

    if productos_fb and product_records:
        filtered = []
        for pr in product_records:
            name = _pick_field(pr, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or pr.get("nombre") or pr.get("PRODUCTO") or ""
            if name and any(fb in name.lower() for fb in productos_fb):
                filtered.append(pr)
        if filtered:
            return filtered

    return product_records


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
        or analisis.get("mensaje_pauta")
    )

    if tiene_fb:
        resumen_fb = analisis.get("resumen_para_bot") or analisis.get("descripcion_publicacion") or analisis.get("mensaje_pauta") or ""
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
    preguntas_restantes: int = 5,
) -> str | None:
    """PASO 3 — Diagnóstico profundo: entender el problema antes de recomendar."""
    contexto_extra = ""
    if analisis.get("resumen_para_bot"):
        contexto_extra = f"\nContexto de llegada del usuario: {analisis['resumen_para_bot']}"

    system_prompt = _PASO3_SYSTEM.format(preguntas_restantes=preguntas_restantes)
    messages = [
        {"role": "system", "content": system_prompt + contexto_extra},
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

    # --- Manejo: si la conversación previa preguntó por videos y el usuario responde afirmativamente
    try:
        if historial_texto and texto_usuario:
            last_bots = re.findall(r"Bot:\s*(.+)", historial_texto)
            if last_bots:
                last_bot = last_bots[-1]
                bot_asked_video = bool(re.search(r"video|vídeo|ver un video|videos relacionados|video relacionado", last_bot, re.IGNORECASE))
                user_yes = bool(re.search(r"^\s*(si|sí|s)(\b|\W)|si por favor|sí por favor|si,|sí,", texto_usuario.lower()))
                if bot_asked_video and user_yes:
                    # Extraer nombres de productos listados en el último mensaje del bot
                    medios = []
                    prod_names = []
                    m = re.search(r"\[([^\]]+)\]", last_bot)
                    if m:
                        prod_names = [p.strip() for p in m.group(1).split(",") if p.strip()]
                    else:
                        # fallback: palabras con mayúsculas que parezcan nombres
                        tokens = re.findall(r"[A-Z][a-zA-Z0-9\-]{2,}(?:\s+[A-Z][a-zA-Z0-9\-]{2,})*", last_bot)
                        prod_names = tokens[:5]

                    for name in prod_names:
                        prods = await _buscar_productos_por_query(name, per_page=1)
                        if prods:
                            p = prods[0]
                            # posibles campos de video
                            video_url = _pick_field(p, ["VIDEO", "video", "video_url", "url_video", "video_link"]) or p.get("video")
                            if video_url:
                                medios.append({"tipo": "video", "url": video_url, "caption": f"Video: {name}"})

                    if medios:
                        return {"texto": "Perfecto — te envío los videos relacionados a los productos recomendados.", "medios": medios}
                    else:
                        return "No encontré videos disponibles para esos productos. ¿Deseas que te mande más información escrita?"
    except Exception:
        pass

    # ── PASO 1: Primer contacto (sin historial o sin respuesta previa del bot) ─
    if not historial_texto.strip() or _contar_turnos_bot(historial_texto) == 0:
        return await _responder_paso1(instancia, analisis, intencion)

    # ── PASO 2: Segunda respuesta — manejo del nombre + indagación ────────────
    if _contar_turnos_bot(historial_texto) == 1:
        return await _responder_paso2(texto_usuario, historial_texto, analisis, intencion)

    # ── PASO 3: Diagnóstico profundo (hasta que el bot tenga suficiente info) ─
    # Se detecta el fin de PASO 3 por la presencia del marcador en historial
    # o cuando se alcanza el límite duro de 5 preguntas
    _MARKER_PASO3_DONE = "Estoy examinando tu situación"
    preguntas_paso3 = max(0, _contar_turnos_bot(historial_texto) - 2)
    preguntas_restantes = max(0, 5 - preguntas_paso3)
    if _MARKER_PASO3_DONE not in historial_texto and preguntas_paso3 < 5:
        return await _responder_paso3(
            texto_usuario, historial_texto, analisis, intencion,
            preguntas_restantes=preguntas_restantes,
        )

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

    # ── Buscar en catálogos (testimonios → paquetes → productos)
    try:
        productos_catalogo = await _buscar_en_catalogos(historial_texto, analisis, intencion, texto_usuario)
    except Exception:
        productos_catalogo = []

    if productos_catalogo:
        # Construir prompt con los productos encontrados + condición del usuario
        condicion = intencion.get("resumen") or analisis.get("contexto_usuario") or ""
        partes_prod = []
        for p in productos_catalogo[:6]:
            nombre = _pick_field(p, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or p.get("nombre") or ""
            descripcion = _pick_field(p, ["DESCRIPCION", "descripcion", "description"]) or p.get("descripcion") or ""
            video = _pick_field(p, ["VIDEO", "video", "video_url", "url_video", "video_link"]) or p.get("video") or ""
            partes_prod.append(f"Nombre: {nombre}\nDescripción: {descripcion}\nVideo: {video}")

        user_prompt = (
            f"Condición del cliente: {condicion}\n"
            "Productos encontrados (mostrar nombre, descripción y video si existe):\n"
            + "\n---\n".join(partes_prod)
            + "\n\nINSTRUCCIONES: Eres un experto en los productos listados. Para cada producto escribe 2-3 líneas explicando para qué sirve y cómo puede ayudar específicamente a la condición del cliente. No menciones precios ni el plan de negocio. Termina preguntando: '¿Deseas ver un video relacionado a los productos recomendados? Responde sí para recibir los videos.'"
        )

        messages = [
            {"role": "system", "content": _PRODUCTOS_SYSTEM},
            {"role": "user", "content": user_prompt},
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
                        "temperature": 0.7,
                        "max_tokens": 600,
                        "messages": messages,
                    },
                )
                resp.raise_for_status()
                recomendacion = resp.json()["choices"][0]["message"]["content"].strip()
                # Asegurar la pregunta de video al final si no la incluyó
                if not re.search(r"video|vídeo", recomendacion, re.IGNORECASE):
                    recomendacion = recomendacion + "\n\n¿Deseas ver un video relacionado a los productos recomendados? Responde 'sí' para recibir los videos."
                return recomendacion
        except Exception:
            pass

    # Si no se encontraron productos en catálogos, seguir con el flujo LLM genérico
    messages: list[dict] = [{"role": "system", "content": _PRODUCTOS_SYSTEM}]

    if historial_texto:
        messages.append({"role": "system", "content": f"HISTORIAL DE LA CONVERSACIÓN (más antiguo a más reciente):\n{historial_texto}",})
    if contexto_str:
        messages.append({"role": "system", "content": f"CONTEXTO ADICIONAL DEL MENSAJE ACTUAL:\n{contexto_str}",})
    messages.append({"role": "user", "content": texto_usuario})

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
