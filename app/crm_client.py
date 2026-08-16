"""Cliente HTTP para hablar con las APIs del CRM (memoria, catálogos, bot-send)."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .config import settings

logger = logging.getLogger("vitta4.crm_client")


class CrmClient:
    """
    Dos credenciales distintas, a propósito:
      - api_token         → bot-send, entrenamiento (endpoints "de acción")
      - prompt_api_key     → memoria, modulos, catálogos, prompt/reglas, prompt/filtros
                              (solo lectura, acceso acotado, para que una fuga no exponga todo)

    Nota: /modulos y /{modulo} son la MISMA API que usa el resto del CRM (ya no hay
    endpoints "/prompt/modulos" ni "/prompt/catalogo/{modulo}" separados) — solo que
    en lectura aceptan también la prompt_api_key. Ver CatalogApiController en el CRM.
    """

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(20.0, connect=10.0)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"X-API-Key": api_key, "Content-Type": "application/json"}

    async def _get(self, path: str, api_key: str, params: Optional[dict] = None) -> dict[str, Any]:
        url = settings.crm_url(path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers(api_key), params=params or {})
        return self._parsear(resp)

    async def _get_text(self, path: str, api_key: str) -> str:
        """Para endpoints que devuelven texto plano (los .vit de /prompt/*)."""
        url = settings.crm_url(path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers(api_key))
        if not resp.is_success:
            logger.warning("CRM %s → HTTP %s: %s", resp.request.url, resp.status_code, resp.text[:200])
            return ""
        return resp.text

    async def _post(self, path: str, api_key: str, body: dict) -> dict[str, Any]:
        url = settings.crm_url(path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=self._headers(api_key), json=body)
        return self._parsear(resp)

    def _parsear(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            data = {"success": False, "error": f"Respuesta no-JSON (HTTP {resp.status_code})"}
        if not resp.is_success:
            logger.warning("CRM %s → HTTP %s: %s", resp.request.url, resp.status_code, data)
        return data

    # ── Memoria (historial de un contacto) ─────────────────────────────────

    async def memoria(self, telefono: str, limit: Optional[int] = None, instancia: Optional[str] = None) -> list[dict]:
        params = {"telefono": telefono, "limit": limit or settings.memoria_limit}
        if instancia:
            params["instancia"] = instancia
        data = await self._get("memoria", settings.crm_prompt_api_key, params)
        return data.get("mensajes", []) if data.get("success") else []

    # ── Catálogos (para resolver tags [CATALOGO_x.campo]) ──────────────────
    # GET /modulos y GET /{modulo} — misma API que el resto del CRM, en modo
    # lectura acepta la prompt_api_key (ver nota de la clase).

    async def prompt_modulos(self) -> list[dict]:
        data = await self._get("modulos", settings.crm_prompt_api_key)
        return data.get("data", []) if data.get("success") else []

    async def prompt_catalogo(self, modulo_slug: str, search: Optional[str] = None, per_page: int = 50) -> list[dict]:
        params: dict[str, Any] = {"per_page": per_page}
        if search:
            params["search"] = search
        data = await self._get(modulo_slug, settings.crm_prompt_api_key, params)
        return data.get("data", []) if data.get("success") else []

    # ── Prompts .vit — el bot los pide directo, sin descargar/subir a mano ──

    async def prompt_reglas(self) -> str:
        return await self._get_text("prompt/reglas", settings.crm_prompt_api_key)

    async def prompt_filtros(self) -> str:
        return await self._get_text("prompt/filtros", settings.crm_prompt_api_key)

    # ── Envío de la respuesta + comandos de control/IA ─────────────────────

    async def bot_send(
        self,
        telefono: str,
        instancia: str,
        respuesta: str,
        remote_jid: Optional[str] = None,
        user_message: Optional[str] = None,
        contact_name: Optional[str] = None,
        medios: Optional[list[dict]] = None,
        medio: Optional[dict] = None,
        solo_registrar: bool = False,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "telefono": telefono,
            "instancia": instancia,
            "respuesta": respuesta,
            "remote_jid": remote_jid or telefono,
        }
        if user_message is not None:
            body["user_message"] = user_message
        if contact_name:
            body["contact_name"] = contact_name
        if medios:
            body["medios"] = medios
        if medio:
            body["medio"] = medio
        if solo_registrar:
            body["solo_registrar"] = True
        if status:
            body["status"] = status

        return await self._post("bot-send", settings.crm_api_token, body)

    # ── Clasificación del filtro (bloquear/pausar contacto) ─────────────────

    async def blocked_numbers(
        self,
        numero_baneado: str,
        numero_remote: str,
        tipo_bloqueo: str,
        motivo: str,
        instancia: Optional[str] = None,
        mensaje: Optional[str] = None,
        etiqueta: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "Numero_Baneado": numero_baneado,
            "Numero_Remote": numero_remote,
            "Motivo_Bloqueo": motivo,
            "tipo_bloqueo": tipo_bloqueo,
        }
        if instancia:
            body["instancia"] = instancia
        if mensaje:
            body["mensaje"] = mensaje
        if etiqueta:
            body["etiqueta"] = etiqueta
        return await self._post("blocked_numbers", settings.crm_api_token, body)

    # ── Entrenamiento (contexto RAG opcional) ───────────────────────────────

    async def entrenamiento_buscar(self, consulta: str, limit: int = 5, instancia: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"q": consulta, "limit": limit}
        if instancia:
            params["instancia"] = instancia
        data = await self._get("entrenamiento/buscar", settings.crm_api_token, params)
        return data.get("pares", []) if data.get("success") else []

    async def entrenamiento_store(self, pregunta: str, respuesta: str, instancia: Optional[str] = None, phone: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"pregunta": pregunta, "respuesta": respuesta}
        if instancia:
            body["instancia"] = instancia
        if phone:
            body["phone"] = phone
        return await self._post("entrenamiento", settings.crm_api_token, body)


crm = CrmClient()
