from django.urls import path
from django_smart_ratelimit import rate_limit

from .views import LoginView

app_name = "users"

urlpatterns = [
    path(
        "login",
        rate_limit(
            key="ip",
            rate="100/h",
            skip_if=lambda req: req.method != "POST",
        )(LoginView.as_view()),
        name="login",
    ),
]
