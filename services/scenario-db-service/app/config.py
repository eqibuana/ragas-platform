from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config, sourced from env vars (or a local .env for `uvicorn app.main:app` outside Docker)."""

    database_url: str = "postgresql+psycopg://scenario:scenario@db:5432/scenario_db"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # The deployed PermataBank AI Platform — same URL ragas_evaluation.py's API_URL points at.
    ai_platform_url: str = (
        "http://k8s-aiplatfo-frontend-1732695a13-fe3650871f8fb59b.elb.ap-southeast-3.amazonaws.com"
    )

    # Judge LLM (Bedrock) + embeddings (SageMaker) for scoring RAGAS metrics during a run.
    # Same credentials as services/ragas-service/.env — copy them over, never hardcode here.
    # AWS_DEFAULT_REGION alias matches the var name already used in that .env file.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = Field(default="ap-southeast-3", validation_alias="AWS_DEFAULT_REGION")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
