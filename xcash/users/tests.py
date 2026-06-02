from django.core.cache import cache as _cache
from django.core.management import call_command
from django.test import Client
from django.test import TestCase
from django.test import override_settings

from users.models import User


def setUpModule():
    # 用户初始化会自动创建项目与钱包；地址派生与签名已在 chains 内部闭环，测试直接走真实派生。
    _cache.clear()


def tearDownModule():
    _cache.clear()


class TestEnsureDefaultSuperuserCommand(TestCase):
    def test_creates_default_superuser_when_none_exists(self):
        call_command("ensure_default_superuser")

        admin_user = User.objects.get(username="admin")
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.check_password("Admin@123456"))

    def test_skips_creation_when_superuser_already_exists(self):
        existing = User.objects.create_superuser(
            username="existing-admin",
            password="secret",
        )

        call_command("ensure_default_superuser")

        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertTrue(User.objects.filter(pk=existing.pk).exists())
        self.assertFalse(User.objects.filter(username="admin").exists())


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class AdminLoginTests(TestCase):
    def test_password_login_creates_admin_session(self):
        user = User.objects.create_user(
            username="admin-login-user", password="secret", is_staff=True
        )
        client = Client()
        extra = {"REMOTE_ADDR": "10.0.0.11"}

        response = client.post(
            "/login?next=/", {"username": user.username, "password": "secret"}, **extra
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertEqual(client.get("/", **extra).status_code, 200)

    def test_failed_password_login_shows_form_error(self):
        client = Client()

        response = client.post(
            "/login",
            {"username": "missing-admin", "password": "bad-secret"},
            REMOTE_ADDR="10.0.0.12",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "此用户名未注册。")
