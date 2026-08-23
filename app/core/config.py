from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    DATABASE_URL: str

    # AWS Cognito
    AWS_REGION: str
    COGNITO_USER_POOL_ID: str
    COGNITO_CLIENT_ID: str


settings = Settings()