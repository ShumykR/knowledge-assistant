"""Збірка залежностей за налаштуваннями; спільна для API і воркера."""

from pathlib import Path

from app.config import Settings
from app.ingest import Embedder, HashEmbedder, OpenAIEmbedder
from app.storage import LocalStorage, S3Storage, Storage


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "s3":
        import boto3  # імпорт тут: boto3 потрібен лише з S3

        access = settings.s3_access_key.get_secret_value()
        client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=access or None,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value() or None,
        )
        return S3Storage(client, settings.s3_bucket)
    return LocalStorage(Path(settings.storage_dir))


def build_embedder(settings: Settings) -> Embedder:
    if settings.llm_base_url and settings.embedding_model:
        return OpenAIEmbedder(
            settings.llm_base_url,
            settings.llm_api_key.get_secret_value(),
            settings.embedding_model,
        )
    return HashEmbedder()
