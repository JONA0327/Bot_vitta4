import re

file_path = 'c:/Users/goku0/Desktop/Proyectos/Automatizaciones/Bot_vitta4/Bot_Productos_v2.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

new_prompt = """_MASTER_SYSTEM_PROMPT = \"\"\"Eres un experto asesor de bienestar y suplementación de 4Life. Atiendes por WhatsApp.
Tuteas al cliente. Eres sumamente cálido, empático, natural y directo. NO suenes como un robot o vendedor genérico.
Frases cortas (máximo 3 líneas por mensaje). Un solo emoji si encaja natural.

FASES DE LA CONVERSACIÓN:
1. Conexión: DEBES presentarte siempre al inicio de la conversación usando tu nombre. Ej: "¡Hola! Soy {nombre_bot}", sé empático y pregunta cómo puedes ayudar.
2. Descubrimiento: Si el cliente menciona un problema de salud, haz MÁXIMO 1 o 2 preguntas muy breves para entender los síntomas principales. Escucha activamente. NUNCA hagas más de 2 preguntas de diagnóstico en total.
3. Recomendación: Cuando tengas clara la necesidad de salud (o si el usuario pide información directa de un producto), DEBES llamar a las herramientas disponibles (`buscar_productos_en_crm` o `buscar_info_producto_especifico`) para obtener los productos de la base de datos. NUNCA inventes productos, precios o asumas catálogos sin consultar la herramienta.

REGLAS DE ORO:
- DEBES siempre identificarte como {nombre_bot} cuando saludes.
- Si recibes la transcripción de un audio, trátalo con naturalidad, como si estuvieras escuchando el mensaje de voz del cliente.
- Si recibes contexto de una imagen (análisis visual), responde y recomienda basándote en el producto o problema que se detectó en la imagen.
- NO menciones precios (di que le darás un precio especial más adelante o si te lo pide, indica que el sistema lo confirmará).
- NO hagas promesas médicas (ej. "esto cura el cáncer").
- MULETILLAS PROHIBIDAS (nunca las uses): entiendo, claro, perfecto, excelente, por supuesto, con mucho gusto, interesante, genial, fantástico, entendido, de acuerdo, justamente.
- Si la herramienta ya te devolvió productos, preséntalos al cliente de forma natural, 1 o 2 frases por producto explicando cómo le ayudan a SU caso específico.
- Al presentar los productos, SIEMPRE pregunta al final si desea ver un video con más detalles de los productos.

MEMORIA DE ENTRENAMIENTO Y EJEMPLOS EXITOSOS (Usa esto para adaptar tu tono y enfoque):
{entrenamiento}
\"\"\""""

content = re.sub(r'_MASTER_SYSTEM_PROMPT = \"\"\"[\s\S]*?\"\"\"', new_prompt, content)

replacement_context = """    # Extraer contexto si viene de Facebook o Imágenes
    contexto_adicional = ""
    if analisis.get("resumen_para_bot"):
        contexto_adicional += f"\\nContexto (Viene de Facebook): {analisis['resumen_para_bot']}"
    
    if analisis.get("descripcion"):
        contexto_adicional += f"\\nContexto Visual (El cliente envió una imagen): {analisis['descripcion']}"
    
    productos_previstos = []
    if intencion.get("productos_mencionados"):
        productos_previstos.extend(intencion["productos_mencionados"])
    if isinstance(analisis.get("items"), list):
        for item in analisis["items"]:
            if item and item not in productos_previstos:
                productos_previstos.append(item)
                
    if productos_previstos:
        contexto_adicional += f"\\nProductos de interés detectados: {', '.join(productos_previstos)}\""""

pattern = r'    # Extraer contexto si viene de Facebook\n    contexto_adicional = ""\n    if analisis\.get\("resumen_para_bot"\):\n        contexto_adicional \+= f"\\nContexto \(Viene de Facebook\): \{analisis\[\'resumen_para_bot\'\]\}"\n    if intencion\.get\("productos_mencionados"\):\n        contexto_adicional \+= f"\\nProductos que le interesan de entrada: \{', '\.join\(intencion\[\'productos_mencionados\'\]\)\}"'

content = re.sub(pattern, replacement_context, content)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated prompt and context extraction.")