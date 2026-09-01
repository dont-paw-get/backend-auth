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

    # CLIAR-162 Phase 7: 기존 FE App Client(COGNITO_CLIENT_ID, secret
    # 없음) 설정은 최종 제거되었다 — 프론트 인증이 Cognito/backend-auth와
    # 전혀 연동된 적이 없음이 확인되어, verify_cognito_token(app/core/
    # cognito.py)이 더 이상 이 값을 허용하지 않는다. 남아 있는 k8s
    # manifest(prod, 및 base의 placeholder)에서 COGNITO_CLIENT_ID
    # 환경변수가 계속 주입되더라도, extra="ignore" 설정 덕분에 조용히
    # 무시되며 Settings() 생성에 영향을 주지 않는다.
    #
    # COGNITO_BACKEND_CLIENT_ID / COGNITO_BACKEND_CLIENT_SECRET: BE
    # 주도 인증 전환(PLAN.md)의 신규 backend 전용 App Client(secret
    # 있음)이며, 이제 verify_cognito_token이 허용하는 유일한
    # client_id다. Optional로 두는 이유: prod configmap에는 아직 이
    # 값이 주입되지 않았고(Phase 0 미완료), required로 두면 그 상태의
    # prod에서 Settings() 생성 자체가 실패한다. 실제 secret 값은 이
    # 파일이나 .env.example, 로그 어디에도 기록하지 않는다.
    COGNITO_BACKEND_CLIENT_ID: str | None = None
    COGNITO_BACKEND_CLIENT_SECRET: str | None = None

    # CLIAR-148: Phase 4(로그인/쿠키)에서 실제로 쓰일 설정값.
    # CLIAR-153에서 CORS 미들웨어(app/main.py)와 refresh 쿠키
    # (app/core/cookies.py)에 실제로 연결되었다.
    #
    # CORS_ALLOWED_ORIGINS: 쉼표로 구분된 origin 목록. 기본값이 빈
    # 문자열인 것은 의도적이다. 아직 이 환경변수가 주입되지 않은
    # dev/prod configmap에서도 startup이 깨지지 않아야 하기 때문이다
    # (required로 두면 Settings() 생성 자체가 실패한다). 값이 비어
    # 있으면 어떤 cross-origin 요청도 허용되지 않으며, 이는 CORS
    # 미들웨어가 아예 없던 기존 동작과 동일하다. 실제 FE origin은
    # 코드에 하드코딩하지 않고 환경별 configmap에서 주입한다.
    CORS_ALLOWED_ORIGINS: str = ""
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str | None = None

    # CLIAR-160 (Phase 6): app/core/rate_limit.py가 소비하는 인증 API
    # rate limit 정책값. PLAN.md §8.3/§9.1에 이미 정의되어 있던 값을
    # 그대로 가져온다(이번 티켓에서 새로 정한 숫자가 아니다). 형식은
    # "<count>/<second|minute|hour>".
    RATE_LIMIT_LOGIN: str = "10/minute"
    RATE_LIMIT_SIGNUP: str = "5/minute"
    RATE_LIMIT_PASSWORD: str = "5/minute"

    # 관측(app/core/logging_config.py): stdout -> Grafana Alloy -> Loki.
    #
    # LOG_FORMAT은 "json"(기본, 수집용)과 "text"(로컬 개발용 사람이
    # 읽는 형식) 중 하나다. 어느 쪽이든 민감정보 마스킹은 동일하게
    # 적용된다.
    #
    # 분산 추적 설정(OTEL_SERVICE_NAME / OTEL_EXPORTER_OTLP_ENDPOINT /
    # OTEL_RESOURCE_ATTRIBUTES)은 의도적으로 여기에 두지 않는다.
    # OpenTelemetry SDK가 그 표준 환경변수들을 직접 읽으며
    # (app/core/tracing.py), Settings에 중복 정의하면 "SDK가 읽는 값"과
    # "우리가 읽는 값"이 어긋날 수 있다. model_config의 extra="ignore"
    # 덕분에 OTEL_* 환경변수가 주입돼도 Settings 생성에는 영향이 없다.
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """
        CORS_ALLOWED_ORIGINS(CSV)를 origin 목록으로 파싱한다.

        와일드카드("*")는 명시적으로 제외한다. 브라우저는
        allow_credentials=True와 allow_origins=["*"] 조합을 허용하지
        않으므로(refresh 쿠키 전송이 불가능해진다), 설정에 "*"가
        들어오더라도 조용히 그 조합이 만들어지지 않게 한다.
        """
        origins = [
            origin.strip()
            for origin in self.CORS_ALLOWED_ORIGINS.split(",")
            if origin.strip()
        ]
        return [origin for origin in origins if origin != "*"]


settings = Settings()