
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

@app.post("/vitta4")
async def vitta4(request: Request):
    body = await request.json()
    # Por ahora, solo imprime lo que llegó
    print("=== Mensaje recibido ===")
    print(f"Teléfono : {body.get('telefono')}")
    print(f"Mensaje  : {body.get('mensaje')}")
    print(f"Tenant   : {body.get('tenant')}")
    print("=======================")
    return {"respuesta": "Mensaje recibido correctamente"}