"""
BE 주도 인증 전환(PLAN.md)을 위한 신규 backend 전용 Cognito App
Client(secret 있음) 호출 공통 기반 (CLIAR-148, Phase 1).

기존 app/core/cognito.py는 FE App Client(secret 없음)로 발급된 Access
Token 검증/GetUser/DeleteUser를 담당하며 이번 티켓에서 변경하지
않는다. 이 모듈은 그와 별도로, 앞으로 SignUp/ConfirmSignUp/
InitiateAuth 등 "secret이 있는 App Client"로 호출할 non-admin
Cognito API들이 공통으로 필요로 하는 SECRET_HASH 계산만 우선
제공한다.

Phase 1 범위: SECRET_HASH 계산 함수만 구현한다. SignUp 등 실제 API
wrapper는 그 기능을 실제로 사용하는 Phase 3/4/5에서 추가한다(미리
만들어두고 쓰지 않는 코드를 최소화하기 위함).
"""

import base64
import hashlib
import hmac

from app.core.cognito import get_cognito_idp_client  # noqa: F401  (Phase 3+ wrapper가 재사용)
from app.core.config import settings


def secret_hash(username: str) -> str:
    """
    AWS Cognito SECRET_HASH를 계산한다.

    규칙(AWS 공식):
        message = username + client_id
        key     = client_secret
        digest  = HMAC-SHA256(key, message)
        result  = base64(digest)

    client_id/client_secret은 항상 settings.COGNITO_BACKEND_CLIENT_ID/
    settings.COGNITO_BACKEND_CLIENT_SECRET에서 읽으며 하드코딩하지
    않는다. 이 값들은 신규 backend 전용 App Client(secret 보유) 것이며,
    기존 FE App Client(COGNITO_CLIENT_ID, secret 없음)와는 다른 값이다.

    신규 backend App Client 설정이 아직 배포 환경에 없는 경우(Phase 1
    시점에는 정상적인 상태), 값이 비어 있으면 조용히 잘못된 해시를
    계산하지 않고 명확한 RuntimeError를 발생시킨다.
    """
    client_id = settings.COGNITO_BACKEND_CLIENT_ID
    client_secret = settings.COGNITO_BACKEND_CLIENT_SECRET

    if not client_id or not client_secret:
        raise RuntimeError(
            "COGNITO_BACKEND_CLIENT_ID/COGNITO_BACKEND_CLIENT_SECRET "
            "must be configured to compute a Cognito SECRET_HASH"
        )

    message = (username + client_id).encode("utf-8")
    key = client_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()

    return base64.b64encode(digest).decode("utf-8")
