from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings


class InternalCallbackTest(TestCase):
    @override_settings(
        IS_SAAS=True,
        INTERNAL_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.internal_callback.httpx.Client")
    def test_deliver_sends_post_with_bearer_token(self, mock_client_cls):
        from common.internal_callback import _deliver_internal_callback

        mock_client = mock_client_cls.return_value.__enter__.return_value

        _deliver_internal_callback(
            event="invoice.confirmed",
            appid="XC-test",
            sys_no="INV-001",
            worth="100.00",
            currency="USDT",
        )

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0] == "http://saas.local/callbacks/xcash"
        assert "Authorization" in call_kwargs.kwargs["headers"]
        payload = call_kwargs.kwargs["json"]
        assert payload["event"] == "invoice.confirmed"
        assert payload["appid"] == "XC-test"
        assert payload["sys_no"] == "INV-001"
        assert payload["worth"] == "100.00"

    @override_settings(IS_SAAS=False)
    @patch("common.internal_callback.httpx.Client")
    def test_deliver_skips_when_token_missing(self, mock_client_cls):
        from common.internal_callback import _deliver_internal_callback

        _deliver_internal_callback(
            event="invoice.confirmed",
            appid="XC-test",
            sys_no="INV-001",
            worth="100.00",
            currency="USDT",
        )

        mock_client_cls.assert_not_called()

    @override_settings(
        IS_SAAS=True,
        INTERNAL_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.internal_callback.httpx.Client")
    def test_deliver_retries_on_http_error(self, mock_client_cls):
        import httpx

        from common.internal_callback import _deliver_internal_callback

        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_response = httpx.Response(
            status_code=500, request=httpx.Request("POST", "http://test")
        )
        mock_client.post.return_value = mock_response

        mock_client.post.return_value.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "Server Error", request=mock_response.request, response=mock_response
            )
        )

        with self.assertRaises(httpx.HTTPStatusError):
            _deliver_internal_callback(
                event="invoice.confirmed",
                appid="XC-test",
                sys_no="INV-001",
                worth="100.00",
                currency="USDT",
            )

    def test_retry_countdown_is_monotonic_and_capped(self):
        from common.internal_callback import _retry_countdown

        retry_delays = [_retry_countdown(retries) for retries in range(6)]
        assert retry_delays == sorted(retry_delays)
        assert _retry_countdown(6) == retry_delays[-1]
        assert _retry_countdown(100) == retry_delays[-1]
