from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infra
    database_url: str = "postgresql+asyncpg://postgres@localhost:55432/postgres"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    organization_id: int = 1  # instalación de una empresa; multiempresa usará el token del usuario
    media_dir: str = "./data/media"  # solo si no hay Supabase Storage configurado
    cors_origins: str = "http://localhost:3000"

    # Auth
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 60 * 12
    admin_email: str = "admin@example.com"
    admin_password: str = "admin123"

    # WhatsApp Cloud API (canal por defecto, se siembra en la BD al arrancar)
    wa_api_version: str = "v23.0"
    wa_verify_token: str = "verify-me"
    wa_app_secret: str = ""  # vacío = no se valida la firma (solo dev)
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""  # WhatsApp Business Account: necesario para listar plantillas

    # Supabase (URL y clave publicable son públicas; la secreta nunca va al frontend ni al repo)
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""  # sb_secret_...: solo backend (Storage, Admin API)
    supabase_jwks_url: str = ""  # verificar tokens de Supabase Auth (claves asimétricas)

    # URL pública del despliegue (callbacks OAuth, script de tracking, webhooks)
    public_base_url: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:3000"

    # Atribución y conversiones
    tracking_ip_salt: str = "cambia-esta-sal"
    google_ads_developer_token: str = ""
    google_ads_login_customer_id: str = ""  # MCC, si aplica
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_capi_token: str = ""  # token de sistema con ads_management/business_management (CAPI)
    meta_embedded_signup_config_id: str = ""

    # CRM
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_login_url: str = "https://login.salesforce.com"

    # SaaS / facturación
    saas_signup_enabled: bool = True
    saas_trial_days: int = 14
    saas_default_plan: str = "professional"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    platform_admin_email: str = ""  # primer administrador del back-office (si la tabla está vacía)
    platform_admin_password: str = ""

    # IA
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    default_provider: str = "anthropic"  # anthropic | openai
    default_model: str = "claude-opus-5-5"
    default_effort: str = "low"  # solo Claude: low | medium | high | xhigh | max
    transcribe_model: str = "whisper-1"  # OpenAI; vacío desactiva transcripción

    # Comportamiento del agente
    debounce_seconds: float = 2.5  # agrupa ráfagas de mensajes antes de responder
    history_limit: int = 30  # mensajes de contexto
    max_images_in_context: int = 3

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
