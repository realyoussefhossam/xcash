"""POST /internal/v1/projects/{appid}/vault 的行为契约测试。

覆盖：set-once 写入、多签校验失败、不可变性（已设置则 409）、鉴权。
多签校验本身（链上 RPC）在 projects/tests.py 单测，这里桩掉校验器只验端点编排。
"""

from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from projects.models import Project

AUTH_HEADER = "Bearer test-internal-token"
# 任意合法 EVM checksum 地址，代表"已通过多签校验"的归集地址。
VALID_VAULT = "0x52908400098527886E0F7030069857D2E4169EE7"

VALIDATOR_PATH = "internal_api.serializers.projects.validate_vault_is_multisig"


@pytest.fixture
def project(db):
    return Project.objects.create(name="vault-test-project")


def _url(project):
    return f"/internal/v1/projects/{project.appid}/vault"


@pytest.mark.django_db
class TestSetVault:
    def test_set_vault_success(self, client, project):
        assert project.vault in (None, "")
        with patch(VALIDATOR_PATH, return_value=VALID_VAULT):
            response = client.post(
                _url(project),
                data={"vault": VALID_VAULT},
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
        assert response.status_code == 200
        assert response.json()["vault_address"] == VALID_VAULT
        project.refresh_from_db()
        assert project.vault == VALID_VAULT

    def test_set_vault_rejects_invalid_multisig(self, client, project):
        with patch(
            VALIDATOR_PATH,
            side_effect=DjangoValidationError("不是有效多签合约"),
        ):
            response = client.post(
                _url(project),
                data={"vault": VALID_VAULT},
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
        assert response.status_code == 400
        project.refresh_from_db()
        assert project.vault in (None, "")

    def test_set_vault_is_immutable_once_set(self, client, project):
        other = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
        project.vault = VALID_VAULT
        project.save(update_fields=["vault"])

        # 已设置：即使传入合法多签地址也必须被拒绝，且不触发链上校验。
        with patch(VALIDATOR_PATH, return_value=other) as validator:
            response = client.post(
                _url(project),
                data={"vault": other},
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
        assert response.status_code == 409
        validator.assert_not_called()
        project.refresh_from_db()
        assert project.vault == VALID_VAULT

    def test_set_vault_requires_auth(self, client, project):
        with patch(VALIDATOR_PATH, return_value=VALID_VAULT):
            response = client.post(
                _url(project),
                data={"vault": VALID_VAULT},
                content_type="application/json",
            )
        assert response.status_code in (401, 403)
        project.refresh_from_db()
        assert project.vault in (None, "")
