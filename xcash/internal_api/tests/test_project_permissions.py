import pytest


@pytest.mark.django_db
def test_project_list_requires_internal_token(client):
    response = client.get("/internal/v1/projects")

    assert response.status_code == 401
