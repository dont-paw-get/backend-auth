# 인증 아키텍처 전환 계획: FE 주도 → BE 주도

## 1. 목적

현재 인증 흐름은 FE가 Cognito를 직접 호출하고 그 결과를 BE에 전달하는 구조다.

```
[현재]  FE ──SignUp/ConfirmSignUp/InitiateAuth──▶ Cognito
        FE ──Bearer <access_token>─────────────▶ BE (/users/bootstrap)
```

이 구조에서 BE는 토큰 검증만 하는 resource server이며, 회원가입·로그인 오케스트레이션 책임이
FE에 있다. 이를 BE가 전담하는 구조로 전환한다.

```
[목표]  FE ──POST /api/v1/auth/signup──▶ BE ──SignUp──▶ Cognito
                                         BE ──INSERT──▶ RDS
        FE ◀───────201 + member─────────  BE
```

FE는 BE의 REST API만 알면 되고, Cognito의 존재를 인지할 필요가 없다.

## 2. 현재 상태 분석

### 2.1 기존 엔드포인트

| 엔드포인트 | 파일 | 역할 |
|---|---|---|
| `POST /api/v1/auth/availability` | `app/api/auth.py` | 이메일/닉네임 중복 확인 (DB 조회만) |
| `POST /api/v1/users/bootstrap` | `app/api/users.py:104` | Cognito 인증 후 member row 생성 |
| `GET /api/v1/users/me` | `app/api/users.py:54` | 프로필 조회 |
| `PATCH /api/v1/users/me` | `app/api/users.py:66` | 프로필 수정 |
| `DELETE /api/v1/users/me` | `app/api/users.py:211` | 탈퇴 (Cognito DeleteUser + soft delete) |

### 2.2 BE에 존재하지 않는 것 (= 이번에 신설)

회원가입(SignUp), 이메일 인증(ConfirmSignUp), 로그인(InitiateAuth), 토큰 갱신,
로그아웃, 비밀번호 찾기/재설정/변경.

### 2.3 현재 Cognito 연동 자산 (재사용)

- `app/core/cognito.py:44` `verify_cognito_token` — JWKS 서명/issuer/exp/token_use/client_id 검증
- `app/core/cognito.py:113` `get_cognito_user_email` — GetUser (access token 기반)
- `app/core/cognito.py:151` `delete_cognito_user` — DeleteUser (access token 기반, IAM 불필요)
- `app/core/security.py:17` `_extract_and_verify_bearer_token` — Bearer 파싱 + 검증 공통 경로
- `app/api/deps.py` — `get_current_member` / `get_member_by_sub`

### 2.4 환경 현황

| | User Pool | App Client | 실사용자 |
|---|---|---|---|
| dev | `ap-northeast-2_y1mKz50El` | `245uq1begv38q4ij0a743lgmhb` | 테스트 계정만 |
| prod | `ap-northeast-2_PRODxxxxx` (placeholder) | `prod-client-id-xxxxxxxx` (placeholder) | **없음** |

prod가 아직 구성되지 않았으므로 App Client 교체에 따른 강제 재로그인 비용이 사실상 0이다.
이 전환을 지금 수행하는 근거가 된다.

## 3. 확정된 설계 결정

| # | 항목 | 결정 | 근거 |
|---|---|---|---|
| D1 | Cognito 호출 방식 | client secret + SECRET_HASH (non-admin API) | IAM 없이 동작, FE 우회를 물리적으로 차단 |
| D2 | 보상/복구 경로 | `AdminGetUser` / `AdminDeleteUser` 2개만 IRSA로 부여 | 고아 계정 자동 복구에 필수 |
| D3 | 토큰 전달 | access/id는 응답 body, refresh는 HttpOnly 쿠키 | 타 MSA가 access token을 Bearer로 받아야 함 + refresh XSS 차단 |
| D4 | member row 생성 시점 | SignUp 시 `PENDING` 생성 → confirm 시 `ACTIVE` | SignUp 응답의 `UserSub`로 즉시 생성 가능 |
| D5 | App Client | secret을 가진 신규 App Client로 즉시 전환 | AWS는 기존 client에 secret 추가 불가 |
| D6 | 로그인 수단 | 이메일 + 비밀번호만 | 소셜 로그인/MFA는 범위 외 |
| D7 | `/users/bootstrap` | 이번 작업에서 **제거** | `/auth/signup`에 완전 흡수 |
| D8 | 추가 범위 | 비밀번호 찾기/변경, Rate limiting/보안 하드닝, 통합 테스트 보강 | |

## 4. 전체 흐름 설계

### 4.1 회원가입

```
FE                    BE                              Cognito          RDS
│                     │                               │                │
├─POST /auth/signup──▶│                               │                │
│  email, password,   │                               │                │
│  nickname, birth,   ├─① 이메일 사전 검증 ─────────────────────────────▶│
│  gender, 약관동의    │                               │                │
│                     ├─② SignUp(SECRET_HASH)────────▶│                │
│                     │◀──UserSub─────────────────────┤                │
│                     ├─③ member INSERT (PENDING)────────────────────▶│
│                     │   member_agreement INSERT                      │
│◀─201 + code_delivery┤                               │                │
│                     │                               │                │
│  (사용자가 메일 확인) │                               │                │
├─POST /auth/signup/─▶│                               │                │
│      confirm        ├─④ ConfirmSignUp──────────────▶│                │
│  email, code        ├─⑤ member UPDATE → ACTIVE─────────────────────▶│
│◀─200────────────────┤                               │                │
```

**① 사전 검증은 email만 대상으로 한다.** CLIAR-144 최종 정책상
`member.nickname`은 UNIQUE 제약이 없고 중복을 허용하므로,
`/auth/signup`은 nickname 중복 여부를 검사하지 않는다(구현:
`app/services/signup_service.py`, `app/api/auth.py`의
`signup_endpoint` docstring 참고). `/auth/availability`가 여전히
`NICKNAME` field를 지원하는 것은 그 엔드포인트의 기존 계약을
유지하기 위함이며, signup 정책의 근거로 쓰이지 않는다(§7.2 참고).

**③ 실패 시 보상**: `AdminDeleteUser`로 Cognito 계정을 즉시 삭제하고 500을 반환한다.
사용자는 같은 이메일로 바로 재시도할 수 있다.

**② `UsernameExistsException` 발생 시 복구 경로**:

```
UsernameExistsException
   ├─ AdminGetUser(email) → sub 조회
   ├─ DB에 해당 sub의 member 있음?
   │    ├─ 있고 ACTIVE/PENDING   → 409 (이미 가입된 이메일)
   │    └─ 없음 (= 고아 계정)     → 그 sub로 member INSERT (PENDING)
   │                              + ResendConfirmationCode → 201
```

### 4.2 로그인

```
FE ──POST /auth/login {email, password}──▶ BE
                                            ├─ InitiateAuth(USER_PASSWORD_AUTH, SECRET_HASH)
                                            │    → AccessToken / IdToken / RefreshToken
                                            ├─ sub로 member 조회
                                            │    PENDING  → 403 (이메일 인증 필요)
                                            │    WITHDRAWN→ 403 (탈퇴한 계정)
                                            │    없음      → 404
FE ◀── 200 ────────────────────────────────┤
   body:   { access_token, id_token, expires_in, token_type, member }
   cookie: refresh_token  (HttpOnly, Secure, SameSite=Lax, Path=/api/v1/auth)
           refresh_sub    (HttpOnly, Secure, SameSite=Lax, Path=/api/v1/auth)
```

`refresh_sub` 쿠키가 필요한 이유: `REFRESH_TOKEN_AUTH` 호출 시에도 `SECRET_HASH`를 보내야 하는데,
SECRET_HASH는 **username(=sub)** 으로 계산된다. refresh token은 불투명(opaque) 문자열이라
BE가 여기서 sub를 추출할 수 없다. 따라서 로그인 시점에 sub를 별도 HttpOnly 쿠키로 함께 내려
`/auth/refresh`에서 재사용한다.

### 4.3 토큰 갱신 / 로그아웃

```
POST /auth/refresh   (body 없음, 쿠키만 사용)
   → InitiateAuth(REFRESH_TOKEN_AUTH, REFRESH_TOKEN=쿠키, SECRET_HASH=f(refresh_sub))
   → 200 { access_token, id_token, expires_in }
   ※ Cognito가 새 refresh token을 주면 쿠키도 갱신 (rotation 대응)
   ※ NotAuthorizedException → 401 + 쿠키 삭제

POST /auth/logout    (body 없음)
   → RevokeToken(Token=refresh 쿠키, ClientId, ClientSecret)
   → 쿠키 삭제 → 204
   ※ RevokeToken 실패해도 쿠키는 반드시 삭제하고 204 반환
```

### 4.4 비밀번호

```
POST /auth/password/forgot  {email}
   → ForgotPassword → 항상 204 (사용자 열거 방지: 미가입 이메일도 204)

POST /auth/password/reset   {email, code, new_password}
   → ConfirmForgotPassword → 204

POST /auth/password/change  {current_password, new_password}   [Bearer 필요]
   → ChangePassword(AccessToken) → 204
```

## 5. 상세 API 계약

### `POST /api/v1/auth/signup` → 201

요청
```json
{
  "email": "user@example.com",
  "password": "P@ssw0rd!",
  "nickname": "댕댕이",
  "birth_date": "1998-04-12",
  "gender": "MALE",
  "agree_terms": true,
  "agree_privacy": true,
  "agree_ai_analysis": false
}
```

응답
```json
{
  "member_id": "3f2a...",
  "email": "user@example.com",
  "status": "PENDING",
  "code_delivery": { "medium": "EMAIL", "destination": "u***@example.com" }
}
```

### `POST /api/v1/auth/signup/confirm` → 200

`{ "email", "code" }` → `{ "member_id", "email", "status": "ACTIVE" }`

### `POST /api/v1/auth/signup/resend` → 204

`{ "email" }`

### `POST /api/v1/auth/login` → 200

`{ "email", "password" }` → body에 토큰 + member, `Set-Cookie` 2개

### `POST /api/v1/auth/refresh` → 200

body 없음 → `{ "access_token", "id_token", "expires_in", "token_type" }`

### `POST /api/v1/auth/logout` → 204

body 없음

### `POST /api/v1/auth/password/forgot` → 204
### `POST /api/v1/auth/password/reset` → 204
### `POST /api/v1/auth/password/change` → 204 (Bearer)

### 유지

`POST /api/v1/auth/availability`, `GET/PATCH/DELETE /api/v1/users/me`

### 제거

`POST /api/v1/users/bootstrap` (D7)

## 6. Cognito 오류 → HTTP 매핑

전 엔드포인트가 공유하는 단일 매핑 테이블을 `app/core/cognito_errors.py`에 둔다.

| Cognito 예외 | HTTP | 응답 메시지 |
|---|---|---|
| `UsernameExistsException` | 409 | 이미 가입된 이메일입니다 (§4.1 복구 경로 먼저 시도) |
| `InvalidPasswordException` | 400 | 비밀번호 정책 위반 |
| `InvalidParameterException` | 400 | 잘못된 요청 |
| `CodeMismatchException` | 400 | 인증 코드가 올바르지 않습니다 |
| `ExpiredCodeException` | 400 | 인증 코드가 만료되었습니다 |
| `UserNotConfirmedException` | 403 | `code: "EMAIL_NOT_VERIFIED"` (FE가 인증 화면으로 라우팅) |
| `NotAuthorizedException` | 401 | **이메일 또는 비밀번호가 올바르지 않습니다** |
| `UserNotFoundException` | 401 | **이메일 또는 비밀번호가 올바르지 않습니다** (동일 문구) |
| `TooManyRequestsException` / `LimitExceededException` | 429 | 잠시 후 다시 시도해주세요 |
| `TooManyFailedAttemptsException` | 429 | 잠시 후 다시 시도해주세요 |
| 그 외 `ClientError` | 502 | 인증 서비스 오류 |
| `EndpointConnectionError` | 502 | 인증 서비스 연결 실패 |

`NotAuthorizedException`과 `UserNotFoundException`이 **완전히 동일한 상태코드와 문구**여야 한다.
다르면 응답 차이로 계정 존재 여부를 알아낼 수 있다(user enumeration).

## 7. 데이터베이스 변경

### 7.1 `member_status` ENUM에 `PENDING` 추가

신규 alembic revision 1건.

```python
def upgrade():
    op.execute("COMMIT")                      # PG: ALTER TYPE ADD VALUE는 트랜잭션 내 실행 불가
    op.execute("ALTER TYPE member_status ADD VALUE IF NOT EXISTS 'PENDING'")
```

**주의사항**

- alembic은 기본적으로 마이그레이션을 트랜잭션으로 감싸므로 `op.execute("COMMIT")`이 선행돼야 한다.
- PostgreSQL은 ENUM 값 삭제를 지원하지 않는다. `downgrade()`는 타입 재생성
  (신규 타입 생성 → 컬럼 USING 캐스팅 → 구 타입 DROP) 방식으로 작성하고,
  `PENDING` row가 남아 있으면 명시적으로 실패시킨다.
- `app/models/user.py`의 `MemberStatus`에도 `PENDING = "PENDING"` 추가.

### 7.2 `PENDING` 상태의 취급

| 위치 | 동작 |
|---|---|
| `app/api/deps.py` `get_current_member` | `PENDING`이면 403 (`EMAIL_NOT_VERIFIED`) |
| `app/api/deps.py` `get_member_by_sub` | 그대로 반환 (상태 무검사 유지) |
| `UserRepository.exists_by_email` | `PENDING`도 "사용 중"으로 계산 (Cognito가 이미 점유) |
| `UserRepository.exists_by_nickname` | `PENDING` 포함 전 상태에서 중복을 계산하지만, `/auth/signup`은 이 메서드를 호출하지 않는다(nickname은 UNIQUE가 아니며 중복을 허용하므로, §4.1 참고). `/auth/availability`(legacy)에서만 사용된다 |
| `DELETE /users/me` | `PENDING`에서도 탈퇴 가능 |

## 8. 인프라 변경

### 8.1 신규 Cognito App Client (D5)

AWS 콘솔 → User Pool `ap-northeast-2_y1mKz50El` → App client 생성.

| 설정 | 값 | 이유 |
|---|---|---|
| Generate client secret | **활성화** | BE만 호출 가능하게 (핵심) |
| `ALLOW_USER_PASSWORD_AUTH` | 활성화 | `/auth/login` |
| `ALLOW_REFRESH_TOKEN_AUTH` | 활성화 | `/auth/refresh` |
| `ALLOW_USER_SRP_AUTH` | 비활성화 | BE는 SRP를 쓰지 않음 |
| Enable token revocation | 활성화 | `/auth/logout`의 `RevokeToken` |
| Prevent user existence errors | 활성화 | 사용자 열거 방지 (§6과 이중 방어) |
| Access token 유효기간 | 1일 | |
| ID token 유효기간 | 1일 | |
| Refresh token 유효기간 | 30일 | |
| Read/write attributes | `email` 포함 | 기존 `GetUser` 경로 유지 |

User Pool 측: 로그인 식별자로 `email` alias가 활성화되어 있어야 한다(미설정 시 추가).

`aws.cognito.signin.user.admin` scope는 `InitiateAuth`로 발급된 access token에 자동 포함되므로,
기존 `get_cognito_user_email` / `delete_cognito_user` / 신규 `ChangePassword`가 그대로 동작한다.

생성 후 신규 App Client의 `COGNITO_BACKEND_CLIENT_ID`를 dev
ConfigMap에, `COGNITO_BACKEND_CLIENT_SECRET`을 k8s Secret에
추가한다(§8.3). 기존 `COGNITO_CLIENT_ID`는 교체하지 않고 그대로
둔다 — CLIAR-153(Phase 4) 구현부터 이 값은 기존 FE App Client가
발급한 Access Token 검증(`app/core/cognito.py:verify_cognito_token`)
과 CLIAR-125 legacy refresh 과도기 호환을 위한 **별도의 상시
설정**이며, 신규 backend App Client와 교체되는 관계가 아니다. 기존
FE App Client는 FE 전환 완료 확인 후 삭제하되, 삭제 시점에는
`COGNITO_CLIENT_ID`/legacy refresh 경로도 함께 정리한다(Phase 7).

### 8.2 IRSA (D2)

`AdminGetUser` / `AdminDeleteUser` 2개만 부여한다.

1. EKS 클러스터에 OIDC Identity Provider 연결 여부 확인, 없으면 생성
2. IAM Policy 생성

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["cognito-idp:AdminGetUser", "cognito-idp:AdminDeleteUser"],
       "Resource": "arn:aws:cognito-idp:ap-northeast-2:<account>:userpool/<pool-id>"
     }]
   }
   ```

3. IAM Role 생성, trust policy에 `system:serviceaccount:<ns>:backend-auth` 지정
4. `k8s/base/serviceaccount.yaml` 신규 작성, `kustomization.yaml`에 등록
5. `k8s/base/deployment.yaml`에 `serviceAccountName: backend-auth` 추가
   (현재 `serviceAccountName`이 없어 default SA를 사용 중)
6. overlay에서 환경별 role-arn 어노테이션 패치

**폴백 정책**: IRSA 자격증명이 없거나 권한이 거부되면 `AdminGetUser`/`AdminDeleteUser`는
`RuntimeError`로 처리하고, 정상 가입 경로는 영향받지 않게 한다. 보상 실패 시
`logger.error`로 고아 계정 sub를 남겨 운영 추적이 가능하게 한다.

### 8.3 설정값 추가

`app/core/config.py`

```python
# 신규 backend 전용 App Client(secret 있음). CLIAR-153(Phase 4)에서
# 실제로 사용 중이며, 기존 COGNITO_CLIENT_ID(FE App Client, secret
# 없음)와는 별개의 값이다 — 하나가 다른 하나를 대체하지 않는다.
COGNITO_BACKEND_CLIENT_ID: str | None = None      # Kubernetes ConfigMap
COGNITO_BACKEND_CLIENT_SECRET: str | None = None  # Kubernetes Secret

CORS_ALLOWED_ORIGINS: str = ""      # CSV, 쿠키 사용 시 필수
COOKIE_SECURE: bool = True          # dev(http) 검증 시에만 False
COOKIE_SAMESITE: str = "lax"
COOKIE_DOMAIN: str | None = None

RATE_LIMIT_LOGIN: str = "10/minute"
RATE_LIMIT_SIGNUP: str = "5/minute"
RATE_LIMIT_PASSWORD: str = "5/minute"
RATE_LIMIT_AVAILABILITY: str = "30/minute"
```

`k8s/secret.example.yaml`은 `COGNITO_CLIENT_SECRET`이라는 이전 이름의
placeholder 항목을 갖고 있다. 실제 배포(Phase 0/7)에서는 이를
`COGNITO_BACKEND_CLIENT_SECRET`으로, `COGNITO_BACKEND_CLIENT_ID`는
Secret이 아니라 `k8s/base/configmap.yaml`(ConfigMap)에 추가해야
한다. 이 티켓에서는 실제 k8s manifest 값이나 secret을 넣지 않았으므로
파일 자체는 아직 이전 이름 그대로다.

### 8.4 CORS (신규)

`app/main.py`에 현재 CORS 미들웨어가 없다. refresh 쿠키를 쓰려면 필수다.

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,   # 와일드카드 금지
    allow_credentials=True,                             # 쿠키 전송에 필수
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
```

`allow_credentials=True`와 `allow_origins=["*"]`는 브라우저가 함께 허용하지 않는다.
FE 도메인을 환경별로 명시해야 한다.

## 9. 보안 하드닝 (D8)

### 9.1 Rate limiting

FastAPI에는 내장 기능이 없다. prod replicas가 2 이상이면 인메모리 카운터는 파드별로 분리되어
실효 한도가 배수만큼 늘어난다는 한계를 전제하고 진행한다.

- **1차(이번 범위)**: `app/core/rate_limit.py`에 의존성 없는 슬라이딩 윈도우 리미터를 구현하고
  FastAPI dependency로 엔드포인트에 부착한다. 키는 `client IP + 엔드포인트`.
  ALB 뒤에 있으므로 `X-Forwarded-For`의 최좌측 값을 신뢰 프록시 수만큼 잘라 사용한다.
- **2차(후속 티켓)**: Redis 백엔드 또는 AWS WAF rate-based rule로 이관.
- Cognito 자체 throttling(`TooManyRequestsException`)은 §6대로 429로 그대로 전달한다.

### 9.2 사용자 열거 방지

- 로그인 실패는 원인과 무관하게 동일한 401 + 동일 문구 (§6)
- `/auth/password/forgot`은 가입 여부와 무관하게 항상 204
- Cognito App Client의 "Prevent user existence errors" 활성화 (§8.1)
- `/auth/availability`는 본질적으로 열거 벡터이므로 가장 강한 rate limit을 적용한다
  (기존 요구사항이라 엔드포인트 자체는 유지)

### 9.3 비밀번호 취급

- 요청 schema에서 `pydantic.SecretStr`을 사용해 실수로 로그/`repr`에 노출되지 않게 한다
- 예외 메시지에 요청 payload를 절대 포함하지 않는다
  (현재 `app/api/auth.py:29`가 `ValidationError` 전문을 `detail`로 반환하고 있어,
   비밀번호가 포함되는 신규 엔드포인트에는 이 패턴을 적용하지 않는다)
- Cognito 호출 실패 로그에는 sub와 오류 코드만 기록한다
  (`app/core/cognito.py:151`의 기존 로깅 원칙을 그대로 따른다)

### 9.4 감사 로그

가입/로그인/로그아웃/비밀번호 변경/탈퇴 시 `sub`, 이벤트명, 결과, 마스킹된 이메일을 남긴다.

## 10. 파일별 작업 목록

### 신규

| 파일 | 내용 |
|---|---|
| `app/core/cognito_auth.py` | boto3 Cognito 호출 래퍼 (SignUp/Confirm/Resend/InitiateAuth/Refresh/Revoke/Forgot/Reset/Change) + `_secret_hash()` |
| `app/core/cognito_admin.py` | `admin_get_user_sub()` / `admin_delete_user()` (IRSA 전용, 보상 경로) |
| `app/core/cognito_errors.py` | §6 오류 매핑 단일 테이블 |
| `app/core/cookies.py` | refresh/refresh_sub 쿠키 set/clear 헬퍼 |
| `app/core/rate_limit.py` | 슬라이딩 윈도우 리미터 + FastAPI dependency |
| `app/services/signup_service.py` | 가입 오케스트레이션 + 보상/고아 복구 |
| `app/services/login_service.py` | 로그인/갱신/로그아웃 + member 상태 검사 |
| `app/services/password_service.py` | 비밀번호 찾기/재설정/변경 |
| `alembic/versions/xxxx_add_pending_to_member_status.py` | §7.1 |
| `k8s/base/serviceaccount.yaml` | IRSA용 SA |

### 수정

| 파일 | 내용 |
|---|---|
| `app/core/config.py` | §8.3 설정값 추가 |
| `app/core/cognito.py` | 신규 모듈과 boto3 client 공유(`get_cognito_idp_client` 재사용). 검증 로직은 변경 없음 |
| `app/models/user.py` | `MemberStatus.PENDING` 추가 |
| `app/schemas/auth.py` | Signup/Confirm/Resend/Login/Token/Password 요청·응답 schema 추가 |
| `app/api/auth.py` | 신규 엔드포인트 9종 추가 |
| `app/api/deps.py` | `get_current_member`에 `PENDING` 403 처리 추가 |
| `app/api/users.py` | `POST /bootstrap` 제거 (D7) |
| `app/services/member_service.py` | `bootstrap_member`를 `PENDING` 생성용으로 조정 + `activate_member` 추가 |
| `app/repositories/user_repository.py` | `get_by_email` 추가, `exists_by_*`가 `PENDING` 포함하도록 확인 |
| `app/main.py` | CORS 미들웨어 추가 |
| `k8s/base/deployment.yaml` | `serviceAccountName` 추가 |
| `k8s/base/kustomization.yaml` | serviceaccount.yaml 등록 |
| `k8s/base/configmap.yaml` / `k8s/overlays/dev/configmap-patch.yaml` | 신규 `COGNITO_BACKEND_CLIENT_ID` 추가(기존 `COGNITO_CLIENT_ID`는 유지, §8.1) |
| `k8s/secret.example.yaml` | `COGNITO_CLIENT_SECRET` → `COGNITO_BACKEND_CLIENT_SECRET`으로 이름 갱신 |
| `.env.example` | 신규 설정값 반영 |
| `requirements.txt` | 추가 의존성 없음 (boto3 이미 존재) |

### 삭제

| 파일 | 사유 |
|---|---|
| `tests/test_users_bootstrap.py` | `/bootstrap` 제거에 따라 `tests/test_auth_signup.py`로 이관 |

## 11. 테스트 계획 (D8)

boto3 호출은 `botocore.stub.Stubber` 또는 `get_cognito_idp_client` monkeypatch로 대체한다
(기존 `tests/test_cognito.py` 방식을 따른다).

| 파일 | 검증 항목 |
|---|---|
| `tests/test_secret_hash.py` | SECRET_HASH가 `base64(HMAC-SHA256(username+client_id, secret))`와 일치 |
| `tests/test_auth_signup.py` | 정상 가입 201 / 중복 이메일 409 / 약관 미동의 400 / 비밀번호 정책 위반 400 |
| `tests/test_auth_signup_compensation.py` | member INSERT 실패 시 `AdminDeleteUser` 호출 후 500 / 고아 계정 복구 경로 |
| `tests/test_auth_confirm.py` | 정상 confirm 200 + status ACTIVE / 코드 불일치 400 / 만료 400 |
| `tests/test_auth_login.py` | 정상 로그인 200 + 쿠키 2개 / PENDING 403 / WITHDRAWN 403 / 자격증명 오류 401 |
| `tests/test_auth_login_enumeration.py` | `NotAuthorizedException`과 `UserNotFoundException`의 응답이 완전히 동일 |
| `tests/test_auth_refresh.py` | 쿠키 기반 갱신 200 / 쿠키 없음 401 / 만료 401 + 쿠키 삭제 |
| `tests/test_auth_logout.py` | 204 + 쿠키 삭제 / RevokeToken 실패해도 204 |
| `tests/test_auth_password.py` | forgot은 미가입 이메일도 204 / reset 204 / change 204 / 코드 오류 400 |
| `tests/test_cognito_error_mapping.py` | §6 표 전항목 파라미터라이즈 |
| `tests/test_rate_limit.py` | 한도 초과 시 429, 윈도우 경과 후 복구 |
| `tests/test_current_member.py` (수정) | `PENDING` 403 케이스 추가 |

## 12. 작업 순서

| Phase | 내용 | 산출물 | 선행 | 상태 |
|---|---|---|---|---|
| 0 | 신규 App Client 생성, IRSA Role/Policy 구성 | AWS 리소스, client_id/secret | — | 대기 (AWS 콘솔 작업) |
| 1 | 설정·공통 기반 (`config.py`, `cognito_auth.py`, `cognito_errors.py`, `cookies.py`) | 단위 테스트 통과 | 0 | |
| 2 | DB 마이그레이션 (`PENDING`) + 모델/deps/repository 반영 | alembic revision | — | **완료** |
| 3 | 회원가입 3종 (`signup` / `confirm` / `resend`) + 보상·복구 | 엔드포인트 + 테스트 | 1, 2 | |
| 4 | 로그인/갱신/로그아웃 + 쿠키 + CORS | 엔드포인트 + 테스트 | 1, 2 | **완료** |
| 5 | 비밀번호 3종 | 엔드포인트 + 테스트 | 1 | |
| 6 | Rate limiting + 보안 하드닝 + 감사 로그 | 미들웨어/dependency + 테스트 | 3, 4, 5 | |
| 7 | `/users/bootstrap` 제거, k8s 매니페스트 갱신, dev 배포·통합 검증 | 배포 | 6 | |
| 8 | FE 연동 가이드 문서화 (`docs/auth-api.md`) | 문서 | 7 | |

Phase 3/4/5는 Phase 1·2 완료 후 병렬 진행이 가능하다.

### Phase 2 완료 기록 (2026-08-28)

- `alembic/versions/9b41c7d2e5f3_add_pending_to_member_status.py` 신규 (head)
- `MemberStatus.PENDING` 추가, `get_current_member`가 `PENDING`을
  403 + `{"code": "EMAIL_NOT_VERIFIED"}`로 차단
- `UserRepository.get_by_email` 추가
- 테스트 188 → 206개 통과

**실 PostgreSQL 검증 완료**: 실제 PostgreSQL 18에 대해
`alembic upgrade head` / `downgrade` / 재 `upgrade`를 모두 실행해
확인했다.

| 단계 | `member_status` ENUM 값 |
|---|---|
| upgrade 전 | `ACTIVE`, `WITHDRAWN` |
| upgrade 후 (`9b41c7d2e5f3`) | `ACTIVE`, `WITHDRAWN`, `PENDING` |
| downgrade 후 | `ACTIVE`, `WITHDRAWN` |
| 재 upgrade 후 (head=`9b41c7d2e5f3`) | `ACTIVE`, `WITHDRAWN`, `PENDING` |

`op.execute("COMMIT")` → `ALTER TYPE ADD VALUE` 순서가 트랜잭션 오류
없이 동작함을 실 DB로 확인했으므로, §13의 "마이그레이션 실패로
initContainer 크래시" 위험은 이 마이그레이션 자체에 대해서는 해소된
것으로 본다.

### Phase 4 완료 기록 (2026-08-28, CLIAR-153)

- `app/services/login_service.py` 신규: login / refresh / logout 오케스트레이션
- `app/core/cognito_auth.py`: `initiate_password_auth` / `refresh_auth` /
  `revoke_refresh_token` / `get_user_sub` 추가
- `app/api/auth.py`: `POST /auth/login`, `POST /auth/logout` 신규,
  `POST /auth/refresh`를 쿠키 기반으로 전환
- `app/main.py`: CORS 미들웨어(`configure_cors`) 추가
- 테스트 319 → 440개 통과

**Cognito sub 확보 방식**: 로그인 응답의 Access Token 문자열을 서명 검증 없이
파싱하지 않고, 그 토큰으로 Cognito `GetUser`를 호출해 `sub`를 얻는다.
`verify_cognito_token`은 access token의 `client_id` claim이
`COGNITO_CLIENT_ID`(기존 FE App Client)와 같은지 검사하므로, 신규 backend App
Client가 발급한 토큰에는 그대로 쓸 수 없다.

**과도기 호환 (Phase 7에서 제거)**: `/auth/refresh`는 쿠키가 있으면 신규
경로(backend App Client + `SECRET_HASH=f(refresh_sub)`), 쿠키가 없고 body에
`refresh_token`이 있으면 CLIAR-125 legacy 경로(기존 FE App Client, SECRET_HASH
없음)를 그대로 사용한다. 기존 body 방식에 신규 client secret을 적용하지
않는다(적용하면 기존 FE가 가진 refresh token이 즉시 거부된다).

**남은 배포 설정** (코드 밖, Phase 0/7 범위):

1. `COGNITO_BACKEND_CLIENT_ID` / `COGNITO_BACKEND_CLIENT_SECRET`을 dev
   configmap/Secret에 주입. 없으면 `/auth/login`·쿠키 `/auth/refresh`가
   `secret_hash()`의 `RuntimeError`로 500이 된다(`/auth/logout`은 이 경우에도
   쿠키만 지우고 204).
2. `CORS_ALLOWED_ORIGINS`에 실제 FE origin 주입. 비어 있으면 cross-origin
   요청이 전혀 허용되지 않는다(코드에 하드코딩하지 않았다).
3. dev(http) 검증 시 `COOKIE_SECURE=False`.
4. `verify_cognito_token`(`app/core/cognito.py`)은 여전히 access
   token의 `client_id` claim을 `COGNITO_CLIENT_ID`(기존 FE App
   Client)와 비교한다. 그런데 `/auth/login`이 발급하는 access token은
   `COGNITO_BACKEND_CLIENT_ID`로 발급된 것이므로, 이 검증을 그대로
   두면 로그인 직후 그 access token으로 `/users/me` 등을 호출할 때
   401이 난다. §8.1에서 `COGNITO_CLIENT_ID`를 신규 backend App
   Client와 교체하지 않고 별도로 유지하기로 한 것과 이 항목이
   상충하므로, Phase 5/6 착수 전에 다음 중 하나로 명시적으로
   해결해야 한다: (a) `verify_cognito_token`이
   `COGNITO_BACKEND_CLIENT_ID`도 허용하도록 확장, 또는 (b) FE 전환
   완료 시점(Phase 7)에 `COGNITO_CLIENT_ID` 값 자체를 backend App
   Client로 교체. 이번 Phase 4 범위에서는 `verify_cognito_token`
   자체를 변경하지 않았으므로(수정 금지 범위), 이 미해결 상태를
   그대로 남겨 다음 Phase에 넘긴다.

실제 AWS에 대한 E2E(login → refresh → logout)는 위 설정이 주입되지 않아
수행하지 않았다.

## 13. 위험 요소

| 위험 | 영향 | 대응 |
|---|---|---|
| App Client 교체로 기존 토큰 전부 무효화 | dev 테스트 계정 강제 재로그인 | prod는 placeholder라 실사용자 영향 없음. dev 재로그인 안내 |
| `ALTER TYPE ADD VALUE`가 트랜잭션 내에서 실패 | 마이그레이션 실패로 initContainer 크래시 → 배포 중단 | `op.execute("COMMIT")` 선행, dev에서 먼저 검증 |
| IRSA 미구성 상태로 배포 | 보상/복구 경로 동작 불가 (정상 가입은 영향 없음) | Phase 0을 코드 배포보다 먼저 완료. 폴백은 §8.2 |
| Cognito SignUp 성공 후 DB 실패 | 고아 계정 | `AdminDeleteUser` 보상 + `AdminGetUser` 복구 (§4.1) |
| 인메모리 rate limit이 멀티 파드에서 부정확 | 실효 한도가 replicas 배수만큼 증가 | 한계 명시. Redis/WAF 이관은 후속 티켓 |
| `COOKIE_SECURE=True`로 dev(http) 검증 불가 | 로컬/dev에서 refresh 흐름 테스트 실패 | `APP_ENV` 기반으로 dev만 False 허용 |
| CORS 미구성으로 FE에서 쿠키 미전송 | 로그인은 되는데 refresh가 안 됨 | Phase 4에 CORS 포함. FE 도메인 확정 필요 |
| FE/BE 배포 시점 불일치 | 전환 창에서 FE 오류 | `/bootstrap` 제거는 Phase 7로 미루고, FE 전환 완료 확인 후 배포 |

## 14. FE 마이그레이션 가이드 (요약)

| 기존 (FE → Cognito 직접) | 변경 후 (FE → BE) |
|---|---|
| `CognitoUserPool.signUp()` | `POST /api/v1/auth/signup` |
| `cognitoUser.confirmRegistration()` | `POST /api/v1/auth/signup/confirm` |
| `cognitoUser.resendConfirmationCode()` | `POST /api/v1/auth/signup/resend` |
| `cognitoUser.authenticateUser()` | `POST /api/v1/auth/login` |
| `cognitoUser.refreshSession()` | `POST /api/v1/auth/refresh` (쿠키 자동 전송) |
| `cognitoUser.signOut()` | `POST /api/v1/auth/logout` |
| `cognitoUser.forgotPassword()` | `POST /api/v1/auth/password/forgot` |
| `cognitoUser.confirmPassword()` | `POST /api/v1/auth/password/reset` |
| `cognitoUser.changePassword()` | `POST /api/v1/auth/password/change` |
| `POST /api/v1/users/bootstrap` | **제거** — `/auth/signup`에 흡수 |

FE는 Cognito SDK 의존성을 완전히 제거할 수 있다. 남는 책임은 access token을 메모리에 보관하고
타 MSA 호출 시 `Authorization: Bearer`로 실어 보내는 것, 그리고 401 수신 시 `/auth/refresh`를
호출하는 것뿐이다. **모든 BE 호출에 `credentials: "include"`가 필요하다**(refresh 쿠키 전송).

## 15. 범위 외 (후속 티켓 후보)

- 소셜 로그인(Google/Kakao/Apple) — Hosted UI + Authorization Code 흐름
- MFA / `NEW_PASSWORD_REQUIRED` 등 Cognito 챌린지 처리
- Redis 기반 분산 rate limiting
- prod Cognito User Pool 실제 구성 (현재 placeholder)
- 관리자용 회원 관리 API
