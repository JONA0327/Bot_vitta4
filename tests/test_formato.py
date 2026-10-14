"""Check rápido: python tests/test_formato.py"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.formato import extraer_medios, limpiar_formato

sucio = "¡Perfecto!\n\n**RioVida Jugo** — el clásico:\n- Práctico\n- Rápido\n\n1. Uno\n### Título\n\n\n\nFin {PAUSAR}"
limpio = limpiar_formato(sucio)
assert "**" not in limpio and "- " not in limpio and "###" not in limpio and "1." not in limpio, limpio
assert "RioVida Jugo" in limpio and limpio.endswith("{PAUSAR}") and "\n\n\n" not in limpio

texto, medios = extraer_medios("Mira 💛 [[VIDEO: catalog/x/v.mp4]] [[IMAGEN: catalog/x/i.jpg]] [[IMAGEN: inventado.png]]")
assert texto == "Mira 💛" and [m["tipo"] for m in medios] == ["imagen", "video"], (texto, medios)
assert medios[0]["url"] == "catalog/x/i.jpg"  # la ruta inventada se descartó
print("ok")

# --- medios del catálogo (detección de producto único) ---
import asyncio
from app import medios_catalogo as mc

prods = [{"id": 28, "n": mc._norm("4Life Avanzado"), "imagen": "catalog/a.jpg", "video": "catalog/a.mp4"},
         {"id": 38, "n": mc._norm("TF Max"), "imagen": "catalog/m.jpg", "video": None}]
largo = "El 4Life Avanzado está diseñado para apoyar su sistema inmunológico. " * 6
assert mc._elegir(prods, largo)["id"] == 28
assert mc._elegir(prods, largo + " También el TF Max.") is None  # dos productos = ambiguo
mc._cache.update(t=1e18, items=prods)  # catálogo falso en caché
assert asyncio.run(mc.medios_para("521", largo)) == [{"tipo": "imagen", "url": "catalog/a.jpg"}, {"tipo": "video", "url": "catalog/a.mp4"}]
assert asyncio.run(mc.medios_para("521", largo)) == []          # no se repite al mismo contacto
assert asyncio.run(mc.medios_para("999", "El Avanzado es bueno")) == []  # mensaje muy corto: sin medios
medio = ("El 4Life Avanzado apoya sus defensas con proteínas de calostro y yema de huevo, como complemento diario "
         "de su alimentación. Son suplementos alimenticios, no medicamentos, y cada persona responde distinto. "
         "¿Quiere saber más o el costo?")
assert 200 <= len(medio) < 350 and asyncio.run(mc.medios_para("888", medio)) != []  # respuesta mediana: ahora sí lleva medios
print("ok medios")

# --- documentos de referencia ---
from app import documentos
assert documentos.construir_bloque([]) == ""
b = documentos.construir_bloque([{"modulo": "Productos", "archivo": "catalogo.pdf", "fragmento": "4Life Avanzado: 90 cápsulas."}])
assert "DOCUMENTOS DE REFERENCIA" in b and "Productos — catalogo.pdf" in b and "90 cápsulas" in b
print("ok documentos")
