"""Конфігурація лише з середовища (12-factor). Секрети в коді та в git не живуть."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KA_", extra="ignore")

    env: str = "dev"  # dev | prod
    # у prod документація API вимкнена (див. завдання №164 у SkillBoost)
    docs_enabled: bool = True
    cors_origins: list[str] = []  # порожньо = CORS вимкнено; "*" не використовуємо

    # JWT. У prod секрет обовʼязковий (>= 32 символи), у dev без нього береться випадковий на процес
    jwt_secret: SecretStr = SecretStr("")
    jwt_issuer: str = "knowledge-assistant"
    jwt_audience: str = "knowledge-assistant-api"
    access_ttl: int = 15 * 60
    refresh_idle_ttl: int = 7 * 24 * 3600  # термін за неактивністю
    refresh_absolute_ttl: int = 30 * 24 * 3600  # межа, яку ротація не продовжує

    # rate limit: спроб входу на (IP, email) і на email за вікно; запитів API на користувача
    login_limit: int = 5
    login_email_limit: int = 20
    login_window: int = 60
    api_limit: int = 120
    api_window: int = 60

    # файли: local (тека) або s3
    storage_backend: str = "local"
    storage_dir: str = "./data/uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    s3_bucket: str = ""
    s3_endpoint_url: str = ""  # для MinIO/LocalStack; для AWS порожньо
    s3_region: str = "eu-central-1"
    s3_access_key: SecretStr = SecretStr("")  # порожньо = ланцюжок облікових даних AWS (роль)
    s3_secret_key: SecretStr = SecretStr("")
    presign_ttl: int = 300

    # порожньо = без БД/Redis (SQLite і MemoryKV у памʼяті): зручно для швидких тестів
    database_url: str = ""  # postgresql+psycopg://user:pass@host:5432/db
    redis_url: str = ""  # redis://host:6379/0

    # проксі LLM (OpenAI-сумісний): використовується з M4
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    embedding_model: str = ""  # порожньо = HashEmbedder (dev); інакше {llm_base_url}/embeddings


@lru_cache
def get_settings() -> Settings:
    return Settings()
