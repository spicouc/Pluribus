"""Configuració central de l'aplicació mitjançant pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Paràmetres de configuració del servei Pluribus."""
    DB_PATH: str = "/opt/pluribus/data/pluribus.db"
    ENV_PATH: str = "/opt/pluribus/.env"
    API_PORT: int = 8790

    # Ollama embeddings
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "nomic-embed-text-v2-moe:latest"
    EMBED_DIM: int = 768

    # Chunking
    MAX_CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # Rate limit
    RATE_LIMIT: int = 100
    RATE_LIMIT_WINDOW: int = 60

    # Notion
    NOTION_API_KEY: str = ""
    NOTION_API_VERSION: str = "2022-06-28"

    # Consolidation
    CONSOLIDATION_MODEL: str = "qwen2.5:3b"

    class Config:
        env_prefix = "PLURIBUS_"
        env_file = ".env"
        extra = "ignore"


settings = Settings()
