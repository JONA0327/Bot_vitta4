"""Limpieza de la respuesta de la IA antes de enviarla por WhatsApp.

- limpiar_formato: quita markdown de "documento" (negritas **, encabezados #, viñetas, listas numeradas),
  porque el modelo lo manda aunque el prompt lo prohíba.
- extraer_medios: saca los marcadores [[IMAGEN: ruta]] / [[VIDEO: ruta]] que la IA copia del catálogo y los
  convierte al formato `medios` de POST /bot-send (el CRM resuelve rutas relativas y envía por Evolution).
"""
from __future__ import annotations

import re

_MARCADOR_MEDIO = re.compile(r"\[\[(IMAGEN|VIDEO):\s*([^\]]*?)\s*\]\]", re.IGNORECASE)


def limpiar_formato(texto: str) -> str:
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto, flags=re.DOTALL)
    texto = re.sub(r"__(.+?)__", r"\1", texto, flags=re.DOTALL)
    texto = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"^[ \t]*(?:[-•*]|\d+\.)[ \t]+", "", texto, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def extraer_medios(texto: str) -> tuple[str, list[dict]]:
    """Devuelve (texto sin marcadores, medios). Solo acepta rutas del catálogo o URLs http(s): nada inventado."""
    medios = []
    for tipo, url in _MARCADOR_MEDIO.findall(texto):
        if url.startswith(("catalog/", "http://", "https://")):
            medios.append({"tipo": "imagen" if tipo.upper() == "IMAGEN" else "video", "url": url})
    medios.sort(key=lambda m: m["tipo"] != "imagen")  # imagen primero, luego video
    return _MARCADOR_MEDIO.sub("", texto).strip(), medios
