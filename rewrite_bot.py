import sys
import re

file_path = 'c:/Users/goku0/Desktop/Proyectos/Automatizaciones/Bot_vitta4/Bot_Productos_v2.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

replacement_code = """
# ─── NUEVA ARQUITECTURA RAG + FUNCTION CALLING (V2) ───────────────────────────

PASO3_SIGNAL = "" # Ya no se usa en v2, pero se exporta por compatibilidad

def _contar_turnos_bot(historial_texto: str) -> int:
    return historial_texto.count("\\nBot:") + (1 if historial_texto.startswith("Bot:") else 0)

_MASTER_SYSTEM_PROMPT = \"\"\"Eres un experto asesor de bienestar y suplementación de 4Life. Atiendes por WhatsApp.
Tuteas al cliente. Eres sumamente cálido, empático, natural y directo. NO suenes como un robot o vendedor genérico.
Frases cortas (máximo 3 líneas por mensaje). Un solo emoji si encaja natural.

FASES DE LA CONVERSACIÓN:
1. Conexión: Saluda (ej. "¡Hola! Soy {nombre_bot}"), sé empático y pregunta cómo puedes ayudar.
2. Descubrimiento: Si el cliente menciona un problema de salud, haz MÁXIMO 1 o 2 preguntas muy breves para entender los síntomas principales. Escucha activamente. NUNCA hagas más de 2 preguntas de diagnóstico en total.
3. Recomendación: Cuando tengas clara la necesidad de salud (o si el usuario pide información directa de un producto), DEBES llamar a las herramientas disponibles (`buscar_productos_en_crm` o `buscar_info_producto_especifico`) para obtener los productos de la base de datos. NUNCA inventes productos, precios o asumas catálogos sin consultar la herramienta.

REGLAS DE ORO:
- NO menciones precios (di que le darás un precio especial más adelante o si te lo pide, indica que el sistema lo confirmará).
- NO hagas promesas médicas (ej. "esto cura el cáncer").
- MULETILLAS PROHIBIDAS (nunca las uses): entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, interesante, genial, fantástico, entendido, de acuerdo, justamente.
- Si la herramienta ya te devolvió productos, preséntalos al cliente de forma natural, 1 o 2 frases por producto explicando cómo le ayudan a SU caso específico.
- Al presentar los productos, SIEMPRE pregunta al final si desea ver un video con más detalles de los productos.

MEMORIA DE ENTRENAMIENTO Y EJEMPLOS EXITOSOS (Usa esto para adaptar tu tono y enfoque):
{entrenamiento}
\"\"\"

async def _ejecutar_herramienta_productos(
    nombre_herramienta: str,
    args: dict,
    historial_texto: str,
    analisis: dict,
    intencion: dict,
    texto_usuario: str
) -> dict:
    \"\"\"Ejecuta la búsqueda real en el CRM según lo que pidió el LLM.\"\"\"
    if nombre_herramienta == "buscar_productos_en_crm":
        condicion = args.get("condicion", "")
        sintomas = args.get("sintomas", [])
        print(f"[Agent] Herramienta buscar_productos_en_crm: condicion={condicion}, sintomas={sintomas}")
        
        productos, paquete = await _buscar_en_catalogos(
            historial_texto=historial_texto,
            analisis=analisis,
            intencion=intencion,
            texto_usuario=texto_usuario,
            condicion_detectada=condicion,
            sintomas=sintomas,
            causas_posibles=[]
        )
        return {"productos": productos, "paquete": paquete}

    elif nombre_herramienta == "buscar_info_producto_especifico":
        nombres = args.get("nombres_productos", [])
        print(f"[Agent] Herramienta buscar_info_producto_especifico: nombres={nombres}")
        
        productos = []
        todos = await _obtener_todos_productos()
        for nombre in nombres:
            prod = _fuzzy_match_catalogo(nombre, todos)
            if prod and prod not in productos:
                productos.append(prod)
        
        return {"productos": productos}
    
    return {}

async def responder_productos(
    texto_usuario: str,
    historial_texto: str = "",
    analisis: dict | None = None,
    intencion: dict | None = None,
    instancia: str = "",
) -> str | dict | None:
    if not OPENAI_API_KEY:
        return None

    analisis = analisis or {}
    intencion = intencion or {}
    
    # 1. Manejo de Videos (Si el cliente dijo que sí o no a los videos previos)
    try:
        if historial_texto and texto_usuario:
            user_yes = bool(re.search(
                r"^\\s*(si|sí|s)(\\b|\\W)|si por favor|sí por favor|si,|sí,|claro|dale|mándalo|envialo|envíalo",
                texto_usuario.strip().lower()
            ))
            if user_yes:
                ids_marker = re.search(r"\\[\\[PRODUTOS_IDS:([^\\]]+)\\]\\]", historial_texto)
                all_bot_msgs = re.findall(r"Bot:\\s*(.*?)(?=\\nUsuario:|\\Z)", historial_texto, re.DOTALL)
                last_bot_full = (all_bot_msgs[-1] if all_bot_msgs else "").strip()
                bot_asked_video = bool(ids_marker) or bool(
                    re.search(r"deseas ver|quieres ver|deseas que te comparta|responde \\*?s[ií]\\*?", last_bot_full, re.IGNORECASE)
                )
                if bot_asked_video:
                    medios = []
                    if ids_marker:
                        prod_ids = [int(x.strip()) for x in ids_marker.group(1).split("|") if x.strip().isdigit()]
                        if prod_ids:
                            ids_csv = ",".join(str(i) for i in prod_ids)
                            batch_res = await _crm_get("productos", {"__ids": ids_csv, "per_page": 50})
                            batch_prods = batch_res.get("data", []) if isinstance(batch_res, dict) else (batch_res or [])
                            if not batch_prods:
                                for pid in prod_ids:
                                    pd = await _crm_get_by_id("productos", pid)
                                    if pd: batch_prods.append(pd)
                            for prod_data in batch_prods:
                                video_url = _pick_video(prod_data)
                                if video_url:
                                    _n = _pick_field(prod_data, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or "Producto"
                                    medios.append({"tipo": "video", "url": video_url, "caption": str(_n)})
                    
                    if medios:
                        return {"texto": "Aquí tienes los videos de los productos recomendados 🎥", "medios": medios}
                    else:
                        return "No encontré videos disponibles para esos productos en este momento."
            
            user_no = bool(re.search(
                r"^\\s*no\\b|no gracias|no quiero|no,? gracias|^nope|^nel\\b|^paso\\b",
                texto_usuario.strip().lower()
            ))
            if user_no:
                all_bot_msgs_no = re.findall(r"Bot:\\s*(.*?)(?=\\nUsuario:|\\Z)", historial_texto, re.DOTALL)
                last_bot_no = (all_bot_msgs_no[-1] if all_bot_msgs_no else "").strip()
                bot_pregunto_video = bool(re.search(
                    r"deseas que te comparta los videos|deseas ver los videos|quieres ver los videos",
                    last_bot_no, re.IGNORECASE
                ))
                if bot_pregunto_video:
                    print(f"[VideoHandler] usuario rechazó los videos -> pausando")
                    return {"texto": None, "pausar": True, "motivo": "rechazo_videos"}
    except Exception as e:
        print(f"[VideoHandler] error: {e}")

    # 2. Verificar Pausa por Precio tras Info
    _precio_pattern = re.compile(
        r"\\b(precio|costo|cuánto|cuanto|cuánto cuesta|cuanto cuesta|cuánto vale|cuanto vale" +
        r"|cuánto es|cuanto es|cuánto cobr|cuanto cobr|cuánto están|cuanto están" +
        r"|cuál es el precio|cual es el precio|qué precio|que precio)\\b",
        re.IGNORECASE,
    )
    if _precio_pattern.search(texto_usuario):
        _info_ya_enviada = (
            bool(re.search(r"\\[\\[PRODUTOS_IDS:", historial_texto))
            or bool(re.search(r"deseas que te comparta los videos|deseas ver los videos", historial_texto, re.IGNORECASE))
            or bool(re.search(r"\\*[A-Za-záéíóúÁÉÍÓÚñÑ].{2,50}\\*\\n", historial_texto))
        )
        if _info_ya_enviada:
            print(f"[Precio] detectado después de entregar info -> pausando")
            return {"texto": None, "pausar": True, "motivo": "precio_post_info"}

    # 3. Preparar el Entorno RAG
    nombre_bot = instancia.strip() or "Valeria"
    entrenamiento_pares = await _crm_get_entrenamiento(q=texto_usuario, limit=4)
    memoria_entrenamiento = _formatear_ejemplos_entrenamiento(entrenamiento_pares)
    
    system_prompt = _MASTER_SYSTEM_PROMPT.format(
        nombre_bot=nombre_bot,
        entrenamiento=memoria_entrenamiento if memoria_entrenamiento else "No hay historial reciente, sé natural y amigable."
    ) + _construir_addon_reglas("productos")
    
    # Herramientas (Function Calling)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "buscar_productos_en_crm",
                "description": "Busca productos recomendados en el catálogo basados en los síntomas y la condición del cliente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "condicion": {
                            "type": "string",
                            "description": "La condición médica principal o el objetivo de salud (ej. 'gastritis', 'falta de energía')."
                        },
                        "sintomas": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de síntomas mencionados por el cliente (ej. ['acidez', 'dolor de estómago'])."
                        }
                    },
                    "required": ["condicion", "sintomas"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "buscar_info_producto_especifico",
                "description": "Busca información detallada en el catálogo sobre uno o más productos específicos mencionados por el cliente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nombres_productos": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de nombres de los productos que el cliente quiere consultar (ej. ['Transfer Factor Plus', 'RioVida'])."
                        }
                    },
                    "required": ["nombres_productos"]
                }
            }
        }
    ]

    messages = [
        {"role": "system", "content": system_prompt}
    ]
    if historial_texto.strip():
        messages.append({"role": "system", "content": f"HISTORIAL PREVIO DE LA CONVERSACIÓN:\\n{historial_texto}"})
    
    # Extraer contexto si viene de Facebook
    contexto_adicional = ""
    if analisis.get("resumen_para_bot"):
        contexto_adicional += f"\\nContexto (Viene de Facebook): {analisis['resumen_para_bot']}"
    if intencion.get("productos_mencionados"):
        contexto_adicional += f"\\nProductos que le interesan de entrada: {', '.join(intencion['productos_mencionados'])}"
    
    if contexto_adicional:
        messages.append({"role": "system", "content": f"INFORMACIÓN ADICIONAL DEL USUARIO ACTUAL:{contexto_adicional}"})
        
    messages.append({"role": "user", "content": texto_usuario})

    # 4. Ejecutar LLM
    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.7,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto"
                },
            )
            resp.raise_for_status()
            response_msg = resp.json()["choices"][0]["message"]
            
            # Si el modelo quiere usar una herramienta
            if response_msg.get("tool_calls"):
                tool_call = response_msg["tool_calls"][0]
                function_name = tool_call["function"]["name"]
                import json as _json
                args = _json.loads(tool_call["function"]["arguments"])
                
                # Ejecutar herramienta
                tool_result = await _ejecutar_herramienta_productos(
                    function_name, args, historial_texto, analisis, intencion, texto_usuario
                )
                
                productos_crm = tool_result.get("productos", [])
                
                if not productos_crm:
                    print(f"[Agent] La herramienta {function_name} no encontró productos.")
                    return {"texto": None, "pausar": True, "motivo": "sin_productos_catalogo"}
                
                # Formatear la respuesta de la herramienta para que el modelo la entienda
                info_productos_para_llm = []
                img_map = {}
                for p in productos_crm[:6]:
                    nombre = str(_pick_field(p, ["PRODUCTO", "producto", "NOMBRE", "nombre", "title"]) or p.get("nombre") or "")
                    descripcion = str(_pick_field(p, ["DESCRIPCION", "descripcion", "description"]) or p.get("descripcion") or "")
                    imagen = _pick_imagen(p)
                    info_productos_para_llm.append(f"Nombre: {nombre}\\nDescripción: {descripcion}")
                    if nombre and imagen:
                        img_map[nombre.lower()] = imagen
                
                ids_str = "|".join(str(p.get("id", "")) for p in productos_crm[:6] if p.get("id"))
                ids_marker = f"[[PRODUTOS_IDS:{ids_str}]]" if ids_str else ""
                
                # Limpiar request original quitando lo que openai no necesita
                clean_response_msg = {
                    "role": "assistant",
                    "content": response_msg.get("content", ""),
                    "tool_calls": response_msg.get("tool_calls")
                }
                if not clean_response_msg["content"]:
                    clean_response_msg["content"] = ""

                messages.append(clean_response_msg)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": function_name,
                    "content": "RESULTADO DEL CATÁLOGO:\\n" + "\\n---\\n".join(info_productos_para_llm) + "\\n\\nIMPORTANTE: Preséntalos al usuario de forma natural, uno por uno o en una sola lista amigable. Al final pregúntale si desea ver un video."
                })
                
                # Segunda llamada al LLM con la respuesta de la herramienta
                resp2 = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o-mini",
                        "temperature": 0.7,
                        "messages": messages
                    },
                )
                resp2.raise_for_status()
                final_text = resp2.json()["choices"][0]["message"]["content"].strip()
                
                # Construir el formato multi-mensaje si hay imágenes
                if ids_marker:
                    final_text += f"\\n{ids_marker}"
                
                if img_map:
                    # En lugar de enviar un solo texto gigante, intentamos separarlo, 
                    # pero en esta versión simplificada de agente enviaremos un solo texto con todas las imágenes.
                    # Como main.py soporta 'mensagens' (una lista de dicts con texto y medios), lo estructuramos:
                    medios_imagenes = [{"tipo": "imagen", "url": url, "caption": n} for n, url in img_map.items() if not url.startswith("data:image/")]
                    if medios_imagenes:
                        return {"texto": final_text, "medios": medios_imagenes}
                
                return final_text
            
            else:
                # Respuesta de texto normal (Fase 1 o 2)
                return response_msg["content"].strip()

    except Exception as e:
        print(f"[Agent] Error llamando a OpenAI: {e}")
        return None
"""

match = re.search(r'(_PASO4_ANALISIS_SYSTEM\s*=|async def _analizar_entrevista_paso4|async def responder_productos)', content)

if match:
    new_content = content[:match.start()] + replacement_code
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("File modified successfully.")
else:
    print("Could not find target to replace.")
