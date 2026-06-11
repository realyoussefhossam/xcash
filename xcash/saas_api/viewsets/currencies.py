from django.conf import settings
from django.db.models import Q
from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.currencies import SaasChainSerializer
from saas_api.serializers.currencies import SaasCryptoSerializer

from chains.constants import ChainCode
from chains.models import Chain
from currencies.models import Crypto


class SaasCryptoViewSet(ListModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasCryptoSerializer
    queryset = Crypto.objects.filter(active=True).prefetch_related(
        "crypto_on_chains__chain"
    )
    pagination_class = None


class SaasChainViewSet(ListModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasChainSerializer
    # 默认仅暴露已启用且非测试网的链；本地开发额外暴露 Anvil，使 SaaS 联调项目
    # 能看到本地测试链，但不把 Sepolia/Nile 等公测网混入普通链列表。
    queryset = Chain.objects.none()
    pagination_class = None

    def get_queryset(self):
        filters = Q(is_testnet=False)
        if settings.DEBUG:
            filters |= Q(code=ChainCode.Anvil)
        return Chain.objects.filter(active=True).filter(filters).order_by("sort_order")
