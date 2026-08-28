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