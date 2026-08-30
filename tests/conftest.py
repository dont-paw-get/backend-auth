"""
전역 pytest fixture (CLIAR-160, Phase 6).

app/core/rate_limit.py의 리미터는 프로세스(=pytest 세션) 전체에서
공유되는 모듈 전역 상태를 갖는다. TestClient로 보내는 요청은 모두
동일한 client_ip("testclient", starlette 기본값)를 쓰므로, 이
autouse fixture 없이는 예를 들어 login rate limit(10/minute)이
tests/test_auth_login.py 전체(로그인 endpoint를 40회 넘게 호출)에
걸쳐 누적되어 서로 다른 테스트끼리 429로 오염시킨다.

모든 테스트 앞뒤로 리미터 상태를 초기화해 테스트 간 격리를 보장한다.
"""

import pytest

from app.core.rate_limit import reset_rate_limits


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    reset_rate_limits()
    yield
    reset_rate_limits()
