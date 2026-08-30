# 인증 전환 작업 계획 (TODO)

BE 주도 Cognito 인증 전환(CLIAR-148 계열)의 **남은 작업만** 추적한다.

설계 결정·흐름·API 계약·Cognito 오류 매핑·보안 정책·구현 현황 등 명세는
`docs/auth-architecture.md`로 분리했다. 이 문서에는 아직 하지 않은 일만 남긴다.

## 진행 현황

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 신규 App Client 생성 / IRSA Role·Policy | **부분 완료** — App Client 완료, IRSA 미구성 |
| 1 | 설정·공통 기반 (`config` / `cognito_auth` / `cognito_errors` / `cookies`) | 완료 |
| 2 | DB 마이그레이션(`PENDING`) + 모델·deps·repository | 완료 |
| 3 | 회원가입 3종 + 보상·복구 | 완료 (보상 경로는 IRSA 의존, B 참고) |
| 4 | 로그인/갱신/로그아웃 + 쿠키 + CORS | 완료 |
| 5 | 비밀번호 3종 | 완료 |
| 6 | Rate limiting + 보안 하드닝 + 감사 로그 | 완료 |
| 7 | `/users/bootstrap` 제거, 매니페스트 갱신, dev 통합 검증 | 완료 (CLIAR-162 / CLIAR-175) |
| 8 | FE 연동 가이드 문서화 | **미완료** (C 참고) |

Phase 4에서 넘겼던 미해결 이슈(`verify_cognito_token`이 검사하는 `client_id`가
기존 FE App Client와 충돌하던 문제)는 CLIAR-162 Phase 7에서 해소됐다 —
`_required_client_id()`가 `COGNITO_BACKEND_CLIENT_ID`를 사용한다.

## A. prod 데이터베이스 연결 — 최우선

prod `backend-auth`가 기동하지 못하는 직접 원인이다. `migrate` initContainer가
`psycopg.errors.ConnectionTimeout`으로 실패한다. 아키텍처 문제(멀티아키 이미지)는
해결됐고 이것이 다음 관문이다.

`dpyb-auth/backend-auth-secret`의 `DATABASE_URL`이 **dev Aurora**
(`dpyb-dev.cluster-...`)를 가리키고 있다. dev Secret을 복사해 만든 것으로 보인다.
prod EKS와 dev Aurora는 VPC가 달라 접근 자체가 되지 않는다.

- [ ] **prod Aurora 엔드포인트 확보.** 획득 경로 세 가지:
      ① book 담당자에게 문의(prod `dpyb_auth`를 생성한 당사자)
      ② AWS 콘솔 → RDS → prod Aurora writer 엔드포인트
      ③ `dpyb-book/backend-book-secret`의 호스트 참고 (book은 prod에서 정상 동작 중)
      — `rds:DescribeDBClusters`는 현재 IAM 사용자에게 `explicit deny`다
- [ ] auth 전용 DB 자격증명 확인 (book과 공용인지, 별도 계정인지)
- [ ] `DATABASE_URL`을 `postgresql+psycopg://<user>:<pass>@<prod-aurora>:5432/dpyb_auth`로 교체
      — **DB 이름만 치환하면 안 된다.** 호스트가 dev로 남는다
- [ ] prod `dpyb_auth`의 내용 확인. 비어 있으면 initContainer의
      `alembic upgrade head`가 전체 마이그레이션을 수행하는 것이 정상이다
      (dev는 버전 테이블을 복사해 no-op이 정상이었다 — 반대 상황)
- [ ] 파드 재생성 후 `migrate` 로그, `/health`, 로그인 스모크 검증

절차와 확인 항목: `docs/db-per-service.md`

## B. IRSA — 보상/복구 경로 (Phase 0 잔여)

`app/core/cognito_auth.py`의 `admin_get_user`는 이미 구현돼 있으나 IAM 권한이 없어
동작하지 않는다. 정상 가입 경로는 영향받지 않지만, Cognito SignUp 성공 후 DB INSERT가
실패하면 고아 계정이 남는다.

- [ ] EKS 클러스터의 OIDC Identity Provider 연결 확인, 없으면 생성
- [ ] IAM Policy 생성 — `cognito-idp:AdminGetUser`, `cognito-idp:AdminDeleteUser` 2개만
- [ ] IAM Role 생성, trust policy에 `system:serviceaccount:<ns>:backend-auth` 지정
- [ ] `k8s/base/serviceaccount.yaml` 신규 작성 + `k8s/base/kustomization.yaml`에 등록
- [ ] `k8s/base/deployment.yaml`에 `serviceAccountName: backend-auth` 추가
      (현재 `serviceAccountName`이 없어 default SA를 쓴다)
- [ ] overlay에서 환경별 role-arn 어노테이션 패치

권한 범위와 폴백 정책: `docs/auth-architecture.md` §8.2

## C. FE 연동 문서 (Phase 8)

- [ ] `docs/auth-api.md` 작성 — 엔드포인트 계약, 쿠키 동작,
      모든 호출에 `credentials: "include"`가 필요하다는 점
      (초안 재료는 `docs/auth-architecture.md` §5와 §14)

## D. prod Cognito 구성

`docs/auth-architecture.md` §2.4의 prod User Pool / App Client가 아직 placeholder다.

- [ ] prod User Pool / backend App Client 실제 생성 (설정값은 §8.1 표 그대로)
- [ ] prod `COGNITO_BACKEND_CLIENT_ID`(ConfigMap) / `COGNITO_BACKEND_CLIENT_SECRET`(Secret) 주입
      — 없으면 `/auth/login`과 쿠키 `/auth/refresh`가 `secret_hash()`의 `RuntimeError`로 500이 된다
- [ ] prod `CORS_ALLOWED_ORIGINS` 주입 — 비어 있으면 cross-origin 요청이 전혀 허용되지 않는다

## 후속 티켓 후보 (범위 외)

- 소셜 로그인(Google/Kakao/Apple) — Hosted UI + Authorization Code 흐름
- MFA / `NEW_PASSWORD_REQUIRED` 등 Cognito 챌린지 처리
- Redis 또는 AWS WAF 기반 분산 rate limiting
  (현재 인메모리라 replicas 수만큼 실효 한도가 늘어난다)
- 관리자용 회원 관리 API
