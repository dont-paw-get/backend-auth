from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_mock_ping_returns_200():
    response = client.get("/mock/ping")
    assert response.status_code == 200


def test_mock_ping_returns_pong_message():
    response = client.get("/mock/ping")
    assert response.json()["message"] == "pong"
