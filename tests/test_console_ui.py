from __future__ import annotations

from fastapi.testclient import TestClient


def test_console_page_renders(client: TestClient) -> None:
    response = client.get("/console")
    assert response.status_code == 200
    assert "ATB Operator Console" in response.text
    assert 'data-action="decision"' in response.text
    assert "atb_console_api_key" in response.text


def test_root_json_exposes_console_link(client: TestClient) -> None:
    response = client.get("/", headers={"accept": "application/json"})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Agent Trust Bureau"
    assert body["docs"] == "/docs"
    assert body["console"] == "/console"


def test_docs_content_length_matches_body(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(response.content)
