from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from users.managers import UserManager


class User(AbstractUser):
    # 认证主标识从邮箱切换为用户名，避免业务账号体系继续强依赖邮件能力。
    username = models.CharField(
        _("用户名"),
        max_length=150,
        unique=True,
        error_messages={
            "unique": _("此用户名已被使用."),
        },
    )
    first_name = None
    last_name = None
    # 后台账号体系不再保留邮箱字段；这里显式置空，避免继续继承 AbstractUser.email。
    email = None

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []
    objects = UserManager()  # 使用自定义管理器

    def get_full_name(self):
        # 修复：admin 侧边栏会调用 get_full_name；当前模型已移除 first_name/last_name，需稳定回退到 username。
        return self.username or ""

    def get_short_name(self):
        # 修复：与 get_full_name 保持同一回退策略，避免头像首字母和用户名称展示继续读到无效字段。
        return self.username or ""
