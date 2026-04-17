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

SI el usuario llegó desde una publicación FB y se te proveen PRODUCTOS DETECTADOS:
- Menciona 1 o 2 de esos productos por nombre de forma natural (ej. "vi que te interesó el PreBiotics y el Aloe Vera Stix").
- NO menciones la categoría ni la línea por separado — los nombres de los productos ya comunican todo.
- Cierra pidiendo el nombre. Total: máximo 2 líneas.

SI el usuario llegó desde una publicación FB pero NO hay productos específicos:
- Reconoce su interés en el TEMA DE SALUD en UNA sola frase natural, sin mencionar nombres de productos.
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

# Prompt PASO 3 — Entrevista clínica estructurada para identificar la condición
_PASO3_SYSTEM = """Eres una especialista en salud natural con formación clínica que atiende por WhatsApp.
Tu misión es realizar una ENTREVISTA CLÍNICA ESTRUCTURADA para identificar con la mayor precisión
posible la condición de salud del paciente ANTES de recomendar cualquier producto.
NO eres médico y NO debes dar diagnósticos médicos al paciente — eso es tarea interna del sistema.
Tu función aquí es recopilar información clínica completa haciendo UNA pregunta a la vez.

METODOLOGÍA CLÍNICA (sigue este orden según lo que ya tienes en el historial):
  1. Síntoma PRINCIPAL: ¿Qué molestia exacta sientes? (pide descripción detallada si es vaga)
  2. LOCALIZACIÓN: ¿Dónde exactamente lo sientes? ¿Se extiende o irradia a otro lugar?
  3. TEMPORALIDAD: ¿Cuánto tiempo llevas así? ¿Es constante o va y viene? ¿Con qué frecuencia?
  4. INTENSIDAD e IMPACTO: ¿Qué tanto afecta tu vida diaria? (del 1 al 10, o descríbelo)
  5. FACTORES: ¿Qué lo empeora? ¿Qué lo alivia? ¿En qué momento del día es peor?
  6. SÍNTOMAS ASOCIADOS: ¿Tienes otros síntomas que aparecen junto con este? (sueño, digestión, ánimo, etc.)
  7. HISTORIAL: ¿Te han dado algún diagnóstico médico? ¿Has tomado algo — medicamento o suplemento — antes?

REGLAS ESTRICTAS:
- Haz SIEMPRE solo UNA pregunta por mensaje. Corta, específica y cálida.
- MÁXIMO 2 líneas por respuesta. SIN emojis — la calidez la transmites con las palabras.
- NO recomiendes productos todavía.
- NO menciones precios, marcas ni 4Life.
- NO des diagnósticos al paciente — solo recaba información.
- REVISA el historial antes de preguntar; JAMÁS repitas una pregunta ya respondida.
- Si una respuesta es muy vaga o ambigua, profundiza con una pregunta de aclaración antes de avanzar.
- PREGUNTAS RESTANTES: {preguntas_restantes}. Cuando llegues a 0 usa [[LISTO]] obligatoriamente.

CUANDO TENGAS INFORMACIÓN SUFICIENTE (mínimo: síntoma principal + duración + factores + impacto):
- Inicia tu respuesta con la línea exacta: [[LISTO]]
- Luego escribe un mensaje corto y empático de transición, sin emojis, ej:
  "Con todo lo que me compartiste ya tengo lo necesario para orientarte bien."
- Usa [[LISTO]] también cuando se agoten las preguntas restantes, aunque no lo tengas todo.
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

    # 3. Deduplicar: eliminar términos que sean substring de otro en la lista
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
    """Convierte una URL relativa (que empieza con /) en absoluta usando CRM_URL."""
    if url.startswith('http://') or url.startswith('https://'):
        return url
    if url.startswith('/'):
        _base = CRM_URL or os.getenv('CRM_URL', '').rstrip('/')
        if _base:
            return _base + url
    return url


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

    # ── 4) Filtrar por productos mencionados en publicación FB (si aplica) ────
    items_fb = list(analisis.get("items") or []) if isinstance(analisis.get("items"), list) else []
    filtro_nombres = [p.lower() for p in productos_hist if p] + [p.lower() for p in items_fb if p]
    seen: set = set()
    filtro_unico = []
    for n in filtro_nombres:
        if n not in seen:
            seen.add(n)
            filtro_unico.append(n)

    if filtro_unico and product_records:
        filtered = [
            pr for pr in product_records
            if any(
                fb in str(_pick_field(pr, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or "").lower()
                for fb in filtro_unico
            )
        ]
        if filtered:
            return filtered, paquete_info

    return product_records, paquete_info


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

        if productos_fb:
            lista_productos = ", ".join(productos_fb[:3])
            prompt_usuario = (
                f"Hora del día: {saludo}.\n"
                f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
                f"PRODUCTOS DETECTADOS de la publicación: {lista_productos}\n"
                "Escribe el saludo siguiendo las instrucciones para 'publicación FB con PRODUCTOS DETECTADOS'. "
                "Menciona 1 o 2 de los productos por nombre de forma natural. "
                "NO menciones la categoría ni la línea por separado. "
                "NO menciones '4Life' como marca aislada ni 'generar ingresos'. "
                "Un solo emoji al final si encaja."
            )
        else:
            tema_salud = contexto_usuario or resumen_fb or (f"la línea {nombre_linea}" if nombre_linea else "sus productos de salud")
            prompt_usuario = (
                f"Hora del día: {saludo}.\n"
                f"Tu nombre (preséntate con este nombre): {nombre_bot}.\n"
                f"CONTEXTO: El usuario llegó desde una publicación con este tema de salud: {tema_salud}\n"
                "Escribe el saludo siguiendo las instrucciones para 'publicación FB sin productos específicos'. "
                "Reconoce el interés en salud de forma natural, sin mencionar productos ni '4Life' como marca. "
                "Un solo emoji al final si encaja."
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
                        {"role": "system", "content": _PASO4_ANALISIS_SYSTEM},
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
                    re.search(r"video|vídeo", last_bot_full, re.IGNORECASE)
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
                                nombre = str(_pick_field(prod_data, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or f"Producto {pid}")
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
    except Exception as _ve:
        print(f"[VideoHandler] error: {_ve}")

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

        instruccion_contexto = (
            f"El cliente tiene: {condicion_completa}. "
            + (f"Los productos provienen del paquete '{str(_pick_field(paquete_encontrado, ['NOMBRE_PAQUETE','nombre_paquete','NOMBRE','nombre','name']) or '')}'. " if paquete_encontrado else "")
            + "Explica con precisión clínica cómo cada producto ayuda ESPECÍFICAMENTE a su condición. "
            + "Responde SOLO con un JSON válido con esta estructura exacta (sin texto fuera del JSON):\n"
            '{"intro": "1-2 oraciones personalizadas mencionando su condición específica", '
            '"productos": [{"nombre": "nombre exacto del producto", "descripcion": "2-3 frases clínicas explicando cómo ayuda a su condición y síntomas específicos"}]}'
        )

        user_prompt = (
            f"Condición clínica del cliente: {condicion_completa}{paquete_ctx}\n"
            "Productos del catálogo:\n"
            + "\n---\n".join(partes_prod)
            + f"\n\n{instruccion_contexto}"
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
                        video_q = "¿Deseas ver un video de los productos recomendados? Responde *sí* para recibir los videos."
                        mensagens.append({"texto": video_q + ("\n" + ids_marker if ids_marker else "")})
                        return {"mensagens": mensagens}
                except Exception as _je:
                    print(f"[PASO4] JSON parse error: {_je} — usando texto plano")

                # Fallback: texto plano
                recomendacion = raw_content
                if not re.search(r"video|vídeo", recomendacion, re.IGNORECASE):
                    recomendacion += "\n\n¿Deseas ver un video de los productos? Responde sí para recibirlos."
                if ids_marker:
                    recomendacion += "\n" + ids_marker
                # En fallback, enviar imágenes junto al texto
                medios_imagenes_fallback = [{"tipo": "imagen", "url": url, "caption": n} for n, url in img_map.items()]
                if medios_imagenes_fallback:
                    return {"texto": recomendacion, "medios": medios_imagenes_fallback}
                return recomendacion
        except Exception as _e:
            print(f"[PASO4] error LLM: {_e}")

    # Sin productos en catálogo → no responder y pausar la conversación
    # (el bot SIEMPRE depende del catálogo; nunca genera respuestas genéricas en PASO4)
    print(f"[PASO4] sin productos en catálogo para condicion='{condicion_detectada}' → pausando")
    return {"texto": None, "pausar": True, "motivo": "sin_productos_catalogo"}

