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

    # CLIAR-148: 과도기 동안 두 App Client가 병행 유지된다.
    # - COGNITO_CLIENT_ID: 기존 FE App Client(secret 없음). FE가 아직
    #   직접 로그인/토큰 발급에 사용 중이며, verify_cognito_token이
    #   Access Token의 client_id claim을 이 값과 비교한다. 이번
    #   티켓에서는 값/용도를 변경하지 않는다.
    COGNITO_CLIENT_ID: str

    # - COGNITO_BACKEND_CLIENT_ID / COGNITO_BACKEND_CLIENT_SECRET: BE
    #   주도 인증 전환(PLAN.md)을 위한 신규 backend 전용 App Client
    #   (secret 있음). Phase 1에서는 이 값을 사용하는 실제 endpoint가
    #   아직 없으므로, 배포 환경에 해당 환경변수가 없어도 기존
    #   backend-auth import/startup이 깨지지 않도록 Optional로 둔다
    #   (기존 COGNITO_CLIENT_ID처럼 required로 두면, 신규 환경변수가
    #   아직 배포되지 않은 dev/prod에서 Settings() 생성 자체가
    #   실패한다). 실제 secret 값은 이 파일이나 .env.example, 로그
    #   어디에도 기록하지 않는다.
    COGNITO_BACKEND_CLIENT_ID: str | None = None
    COGNITO_BACKEND_CLIENT_SECRET: str | None = None

    # CLIAR-148: Phase 4(로그인/쿠키)에서 실제로 쓰일 설정값. 이번
    # 티켓에서는 CORS 미들웨어나 쿠키 발급 endpoint에 연결하지 않으므로
    # 합리적인 기본값만 둔다.
    CORS_ALLOWED_ORIGINS: str = ""
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str | None = None


settings = Settings()