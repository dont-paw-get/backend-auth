"""
app/core/cognito_auth.py의 secret_hash() 테스트 (CLIAR-148, Phase 1).

AWS Cognito SECRET_HASH 규칙:
    message = username + client_id
    key     = client_secret
    digest  = HMAC-SHA256(key, message)
    result  = base64(digest)

실제 AWS/Cognito에 접속하지 않는다. settings.COGNITO_BACKEND_CLIENT_ID/
COGNITO_BACKEND_CLIENT_SECRET을 monkeypatch로 고정값으로 바꿔서
검증한다.
"""

import base64
import hashlib
import hmac

import pytest

from app.core import cognito_auth
from app.core.config import settings


def _expected_hash(username: str, client_id: str, client_secret: str) -> str:
    """테스트에서 AWS 공식 규칙을 직접(라이브러리 재사용 없이) 재계산한다."""
    message = (username + client_id).encode("utf-8")
    key = client_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture()
def backend_client_settings(monkeypatch):
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "test-backend-client-id")
    monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "test-backend-client-secret")
    return settings


class TestSecretHashMatchesAwsRule:
    def test_matches_manually_computed_hmac_sha256_base64(self, backend_client_settings):
        result = cognito_auth.secret_hash("user@example.com")

        expected = _expected_hash(
            "user@example.com",
            "test-backend-client-id",
            "test-backend-client-secret",
        )
        assert result == expected

    def test_message_order_is_username_then_client_id(self, monkeypatch):
        """message = username + client_id 순서가 바뀌면 완전히 다른
        해시가 나와야 한다(AWS 규칙 위반 감지용 회귀 테스트)."""
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "abc")
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "shared-secret")

        result = cognito_auth.secret_hash("user@example.com")

        wrong_order = _expected_hash("abc", "user@example.com", "shared-secret")
        correct_order = _expected_hash("user@example.com", "abc", "shared-secret")

        assert result != wrong_order
        assert result == correct_order

    def test_result_is_valid_base64(self, backend_client_settings):
        result = cognito_auth.secret_hash("user@example.com")

        # base64.b64decode가 예외 없이 성공하면 유효한 base64 문자열이다.
        decoded = base64.b64decode(result)
        assert len(decoded) == 32  # SHA-256 digest는 항상 32바이트

    def test_different_usernames_produce_different_hashes(self, backend_client_settings):
        hash_a = cognito_auth.secret_hash("user-a@example.com")
        hash_b = cognito_auth.secret_hash("user-b@example.com")

        assert hash_a != hash_b


class TestSecretHashUsesSettingsNotHardcoded:
    def test_changing_client_id_changes_result(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "client-id-1")
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "same-secret")
        hash_with_client_1 = cognito_auth.secret_hash("user@example.com")

        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "client-id-2")
        hash_with_client_2 = cognito_auth.secret_hash("user@example.com")

        assert hash_with_client_1 != hash_with_client_2

    def test_changing_client_secret_changes_result(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "same-client-id")
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "secret-1")
        hash_with_secret_1 = cognito_auth.secret_hash("user@example.com")

        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "secret-2")
        hash_with_secret_2 = cognito_auth.secret_hash("user@example.com")

        assert hash_with_secret_1 != hash_with_secret_2

    def test_does_not_use_existing_fe_client_id(self, monkeypatch):
        """기존 FE App Client(settings.COGNITO_CLIENT_ID)를 실수로 쓰지
        않는지 확인한다. backend client id만 바뀌어도 결과가 달라져야
        하며, 기존 COGNITO_CLIENT_ID를 바꿔도 결과가 그대로여야 한다."""
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "backend-client-id")
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "backend-secret")
        monkeypatch.setattr(settings, "COGNITO_CLIENT_ID", "fe-client-id-one")
        result_with_fe_client_1 = cognito_auth.secret_hash("user@example.com")

        monkeypatch.setattr(settings, "COGNITO_CLIENT_ID", "fe-client-id-two")
        result_with_fe_client_2 = cognito_auth.secret_hash("user@example.com")

        assert result_with_fe_client_1 == result_with_fe_client_2


class TestSecretHashMissingConfiguration:
    def test_missing_backend_client_id_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", "some-secret")

        with pytest.raises(RuntimeError):
            cognito_auth.secret_hash("user@example.com")

    def test_missing_backend_client_secret_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", "some-client-id")
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)

        with pytest.raises(RuntimeError):
            cognito_auth.secret_hash("user@example.com")

    def test_both_missing_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_SECRET", None)

        with pytest.raises(RuntimeError):
            cognito_auth.secret_hash("user@example.com")

    def test_error_message_does_not_leak_secret_value(self, monkeypatch):
        monkeypatch.setattr(settings, "COGNITO_BACKEND_CLIENT_ID", None)
        monkeypatch.setattr(
            settings, "COGNITO_BACKEND_CLIENT_SECRET", "super-secret-value-xyz"
        )

        with pytest.raises(RuntimeError) as exc_info:
            cognito_auth.secret_hash("user@example.com")

        assert "super-secret-value-xyz" not in str(exc_info.value)


class TestSecretHashDoesNotExposeSecretValue:
    def test_result_does_not_contain_raw_secret(self, backend_client_settings):
        """결과(base64 해시)는 secret 원문을 그대로 포함하는 문자열이
        아니어야 한다(HMAC 출력이므로 원문이 그대로 나올 수 없지만,
        회귀 방지 차원에서 명시적으로 확인한다)."""
        result = cognito_auth.secret_hash("user@example.com")

        assert "test-backend-client-secret" not in result
