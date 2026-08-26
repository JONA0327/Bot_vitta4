"""Carga la configuración del bot desde el .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw.isdigit() else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class ProviderKeys:
    openai_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    gemini_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-1.5-flash"))
    deepseek_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-chat"))
    claude_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-haiku-4-5-20251001"))


@dataclass(frozen=True)
class Settings:
    # CRM
    crm_base_url: str = field(default_factory=lambda: _env("CRM_BASE_URL").rstrip("/"))
    crm_tenant_slug: str = field(default_factory=lambda: _env("CRM_TENANT_SLUG"))
    crm_api_token: str = field(default_factory=lambda: _env("CRM_API_TOKEN"))
    crm_prompt_api_key: str = field(default_factory=lambda: _env("CRM_PROMPT_API_KEY"))

    # Servidor
    bot_host: str = field(default_factory=lambda: _env("BOT_HOST", "0.0.0.0"))
    bot_port: int = field(default_factory=lambda: _env_int("BOT_PORT", 8000))
    bot_webhook_secret: str = field(default_factory=lambda: _env("BOT_WEBHOOK_SECRET"))

    # Prompts .vit
    # "api"  (recomendado) → los pide directo al CRM (GET /prompt/reglas y
    #         /prompt/filtros) con CRM_PROMPT_API_KEY, cacheados VIT_CACHE_SECONDS.
    # "file" → los lee de disco (VIT_REGLAS_PATH / VIT_FILTROS_PATH), el flujo
    #         viejo de exportar el .vit desde el panel y copiarlo a mano.
    vit_source: str = field(default_factory=lambda: _env("VIT_SOURCE", "api").lower())
    vit_cache_seconds: int = field(default_factory=lambda: _env_int("VIT_CACHE_SECONDS", 60))
    vit_reglas_path: str = field(default_factory=lambda: _env("VIT_REGLAS_PATH", "./prompts/reglas.vit"))
    vit_filtros_path: str = field(default_factory=lambda: _env("VIT_FILTROS_PATH", "./prompts/filtros.vit"))
    vit_reload_on_change: bool = field(default_factory=lambda: _env_bool("VIT_RELOAD_ON_CHANGE", True))

    # Ráfagas de mensajes: margen (segundos) desde el primer mensaje de una
    # racha para encolar los siguientes y responderlos juntos en uno solo.
    debounce_seconds: int = field(default_factory=lambda: _env_int("DEBOUNCE_SECONDS", 40))

    # Proveedor de IA
    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "openai").lower())
    filter_ai_provider: str = field(default_factory=lambda: _env("FILTER_AI_PROVIDER").lower())
    providers: ProviderKeys = field(default_factory=ProviderKeys)

    # Anti-ciclo
    memoria_limit: int = field(default_factory=lambda: _env_int("MEMORIA_LIMIT", 10))
    max_respuestas_repetidas: int = field(default_factory=lambda: _env_int("MAX_RESPUESTAS_REPETIDAS", 2))

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    def crm_url(self, path: str) -> str:
        """Arma una URL completa del CRM: {base}/{tenant}/{path}"""
        path = path.lstrip("/")
        return f"{self.crm_base_url}/{self.crm_tenant_slug}/{path}"

    @property
    def effective_filter_provider(self) -> str:
        return self.filter_ai_provider or self.ai_provider

    def validar(self) -> list[str]:
        """Devuelve una lista de problemas de configuración (vacía si todo bien)."""
        problemas = []
        if not self.crm_base_url:
            problemas.append("CRM_BASE_URL no está configurado.")
        if not self.crm_tenant_slug:
            problemas.append("CRM_TENANT_SLUG no está configurado.")
        if not self.crm_api_token:
            problemas.append("CRM_API_TOKEN no está configurado (necesario para /bot-send).")
        if not self.crm_prompt_api_key:
            problemas.append("CRM_PROMPT_API_KEY no está configurado (necesario para /memoria y /prompt/*).")
        if self.vit_source not in ("api", "file"):
            problemas.append(f"VIT_SOURCE='{self.vit_source}' no es válido (usa api|file).")
        if self.ai_provider not in ("openai", "gemini", "deepseek", "claude"):
            problemas.append(f"AI_PROVIDER='{self.ai_provider}' no es válido (usa openai|gemini|deepseek|claude).")
        return problemas


settings = Settings()
