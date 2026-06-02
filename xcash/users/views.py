from django.contrib import admin
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from .forms import LoginForm
from .models import User


def _safe_next_path(request) -> str:
    """从请求中提取 next 参数，仅允许同站相对路径，防止 Open Redirect。"""
    next_path = request.POST.get("next") or request.GET.get("next") or "/"
    if url_has_allowed_host_and_scheme(next_path, allowed_hosts=None):
        return next_path
    return "/"


class AdminContextMixin:
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)  # noqa
        ctx.update(admin.site.each_context(self.request))  # noqa
        ctx["app_path"] = self.request.get_full_path()
        return ctx

    def dispatch(self, request, *args, **kwargs):
        if (
            request.user.is_authenticated
            and request.user.is_staff
            and request.path != "/logout/"
        ):
            return redirect("/")
        return super().dispatch(request, *args, **kwargs)


class LoginView(AdminContextMixin, FormView):
    form_class = LoginForm
    template_name = "auth/login.html"
    success_url = "/"

    def form_valid(self, form):
        username = form.cleaned_data["username"]
        password = form.cleaned_data["password"]

        user: User | None = authenticate(
            self.request, username=username, password=password
        )
        if user is None:
            form.add_error(None, _("用户名或密码错误。"))
            return self.form_invalid(form)

        auth_login(self.request, user)
        return redirect(_safe_next_path(self.request))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("登录")
        context["next"] = _safe_next_path(self.request)
        return context
