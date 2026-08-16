# vitta4_Bot_3.0

Bot de WhatsApp en Python que usa el CRM (`CRM_AUTOMATIZADOR`) como backend:
pide los prompts `.vit` directo al CRM por API (Configuración → Prompt), resuelve
los catálogos en vivo, genera la respuesta con el proveedor de IA que elijas, y
respeta los comandos de control (`{PAUSAR}`/`{BLOQUEADO}`/`{BANEADO}`/`{CONTINUAR}`)
y de IA (`{ANALIZAR_IMAGEN}`/`{TRANSCRIBIR_AUDIO}`).

## Arquitectura (quién llama a quién)

```
WhatsApp → Evolution API → CRM (/webhook/whatsapp/{instancia})
                              │  (si el contacto no está pausado/bloqueado)
                              ▼
                    POST {bot_webhook_url}  ──────────►  vitta4_Bot_3.0 (/webhook)
                                                              │ ack inmediato
                                                              │ procesa en 2do plano:
                                                              │  1. Prompt de Filtros → ¿bloquear/pausar?
                                                              │     └─ sí → POST /blocked_numbers al CRM, fin
                                                              │  2. GET /memoria (historial real, anti-ciclo)
                                                              │  3. GET /prompt/reglas (cacheado) + catálogos → IA
                                                              │  4. POST /bot-send al CRM
                                                              ▼
                                              CRM parsea {TAG}, aplica estado,
                                              y envía el texto limpio por Evolution API
```

Configura `bot_webhook_url` en el CRM (Configuración → Entorno → Webhook del
bot) apuntando a donde corra este bot (ej. `https://tu-dominio.com/webhook`).

## Arranque

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env          # y llena los valores (ver abajo)

uvicorn main:app --host 0.0.0.0 --port 8000
```

## Variables del `.env` que debes llenar

| Variable | De dónde sale |
|---|---|
| `CRM_BASE_URL` | Tu dominio del CRM + `/api/v1` |
| `CRM_TENANT_SLUG` | Slug de tu negocio en el CRM |
| `CRM_API_TOKEN` | Configuración → Entorno → API Token general |
| `CRM_PROMPT_API_KEY` | Configuración → Prompt → API Key de consulta |
| `VIT_SOURCE` | `api` (recomendado, default) para pedir los prompts directo al CRM, o `file` para el modo viejo de disco |
| `AI_PROVIDER` | `openai` \| `gemini` \| `deepseek` \| `claude` |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` | Solo llena la del proveedor que elegiste |

### Cómo llega el prompt al bot

Con `VIT_SOURCE=api` (default) el bot ya no necesita nada en disco: al
arrancar, y luego cada `VIT_CACHE_SECONDS` (default 60s), pide
`GET /prompt/reglas` y `GET /prompt/filtros` al CRM con `CRM_PROMPT_API_KEY`.
Cualquier cambio que guardes en Configuración → Prompt del panel se refleja
solo, sin tocar el servidor del bot ni reiniciarlo.

Si prefieres el flujo manual, pon `VIT_SOURCE=file` — entonces el bot lee
`prompts/reglas.vit` y `prompts/filtros.vit` de disco (los que exportas desde
Configuración → Prompt → Exportar .vit). Con `VIT_RELOAD_ON_CHANGE=true`
(default) no hace falta reiniciar el bot al reemplazar esos archivos.

## Anti-ciclo

El bot no guarda su propio historial — usa `GET /memoria` (el historial real
que ya vive en el CRM) antes de cada respuesta, tanto para dar contexto a la
IA como para detectar si está a punto de repetir su propia respuesta
`MAX_RESPUESTAS_REPETIDAS` veces seguidas. Si detecta el bucle, en vez de
reenviar lo mismo agrega `{PAUSAR}` para que un humano tome la conversación.
Esto funciona igual aunque el bot se reinicie o corras varias instancias,
porque el estado vive en el CRM, no en el bot.
