from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone
from internal_api.serializers.invoices import InternalInvoiceDetailSerializer
from internal_api.viewsets.invoices import InternalInvoiceViewSet

from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoiceProtocol


class InternalInvoiceCreateDeprecatedTests(SimpleTestCase):
    """内部 API 不再承担 Invoice 创建职责。"""

    def test_internal_invoice_api_disables_post(self):
        self.assertNotIn("post", InternalInvoiceViewSet.http_method_names)


class InternalInvoiceDetailSerializerTests(SimpleTestCase):
    """内部 API 账单详情字段测试。"""

    def test_detail_includes_billing_mode_and_protocol(self):
        invoice = Invoice(
            sys_no="INV-test",
            out_no="internal-detail-order",
            title="Internal detail",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now(),
            billing_mode=InvoiceBillingMode.CONTRACT,
            protocol=InvoiceProtocol.EPAY_V1,
        )

        data = InternalInvoiceDetailSerializer(invoice).data

        self.assertEqual(data["billing_mode"], InvoiceBillingMode.CONTRACT)
        self.assertEqual(data["protocol"], InvoiceProtocol.EPAY_V1)
