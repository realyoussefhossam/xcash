from __future__ import annotations

import time

import structlog
from django.conf import settings
from django.db import IntegrityError
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from eth_utils import keccak
from tron.adapter import TronAdapter
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.contracts_codec import predict_tron_vault_slot_address
from tron.intents import TronTxIntent
from tron.intents import build_vault_slot_collect_intent
from tron.intents import build_vault_slot_deploy_intent

from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from common.fields import AddressField
from common.fields import HashField
from common.models import UndeletableModel
from core.models import SystemWallet
from core.runtime_settings import get_vault_slot_collect_delay
from projects.models import Customer

logger = structlog.get_logger()


class TronVaultSlotUsage(models.TextChoices):
    DEPOSIT = "deposit", _("用户充币")
    INVOICE = "invoice", _("账单收款")


class TronWatchCursor(models.Model):
    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.CASCADE,
        related_name="tron_watch_cursors",
        verbose_name=_("链"),
    )
    contract_address = AddressField(_("合约地址"))
    last_scanned_block = models.PositiveIntegerField(_("已扫描到的区块"), default=0)
    enabled = models.BooleanField(_("启用"), default=True)
    last_error = models.CharField(_("最近错误"), max_length=255, blank=True, default="")
    last_error_at = models.DateTimeField(_("最近错误时间"), blank=True, null=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "contract_address"),
                name="uniq_tron_watch_cursor_chain_contract_address",
            ),
        ]
        ordering = ("chain_id", "contract_address")
        verbose_name = _("Tron 扫描游标")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.chain.code}:{self.contract_address}"


class TronTxTask(UndeletableModel):
    """Tron 主动链上任务。

    Tron 没有 EVM nonce；任务稳定身份由 TxTask 锚点承载，过期后重签会生成新 txID，
    历史 hash 通过 TxHash 追加，业务操作仅限 deploy/collect 这类幂等动作。
    """

    base_task = models.OneToOneField(
        "chains.TxTask",
        on_delete=models.CASCADE,
        related_name="tron_task",
        verbose_name=_("通用链上任务"),
    )
    sender = models.ForeignKey(
        "chains.Address",
        on_delete=models.PROTECT,
        verbose_name=_("发送地址"),
    )
    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.PROTECT,
        verbose_name=_("网络"),
    )
    to = AddressField(_("To"))
    function_selector = models.CharField(_("函数签名"), max_length=128)
    parameter = models.TextField(_("ABI 参数"), blank=True, default="")
    fee_limit = models.PositiveBigIntegerField(_("Fee Limit"))
    expiration = models.PositiveBigIntegerField(_("过期时间(ms)"), null=True, blank=True)
    ref_block_bytes = models.CharField(_("Ref Block Bytes"), max_length=16, blank=True, default="")
    ref_block_hash = models.CharField(_("Ref Block Hash"), max_length=32, blank=True, default="")
    signed_payload = models.JSONField(_("已签名链上载荷"), default=dict, blank=True)
    tx_id = HashField(unique=False, null=True, blank=True, verbose_name=_("当前 TxID"))
    last_attempt_at = models.DateTimeField(_("上次尝试时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        verbose_name = _("Tron 链上任务")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.base_task.tx_hash or f"tron-task-{self.pk or 'unsaved'}"

    @property
    def status(self) -> str:
        return self.base_task.display_status

    @property
    def can_rebroadcast(self) -> bool:
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        if base_task.status == TxTaskStatus.QUEUED:
            return True
        if base_task.status != TxTaskStatus.PENDING_CHAIN:
            return False
        return self.is_expired()

    def is_expired(self) -> bool:
        if self.expiration is None:
            return False
        return int(time.time() * 1000) >= int(self.expiration)

    def broadcast(self) -> None:
        if not self.can_rebroadcast:
            return
        self.record_broadcast_attempt()
        self.validate_fee_limit()
        client = TronHttpClient(chain=self.chain)
        unsigned = client.trigger_smart_contract(
            owner_address=self.sender.address,
            contract_address=self.to,
            function_selector=self.function_selector,
            parameter=self.parameter,
            fee_limit=self.fee_limit,
        )
        transaction = unsigned.get("transaction")
        if not isinstance(transaction, dict):
            raise TronClientError(f"invalid trigger transaction from {self.chain.code}")

        signed = self.sender.sign_tron_transaction(unsigned_transaction=transaction)
        self.persist_signed_payload(signed_payload=signed.raw_transaction, tx_id=signed.tx_hash)

        response = client.broadcast_transaction(transaction=signed.raw_transaction)
        if response.get("result") is True:
            self.mark_pending_chain()
            return
        if self.is_duplicate_broadcast_response(response):
            self.mark_pending_chain()
            return
        message = response.get("message") or response.get("code") or response
        raise TronClientError(f"tron broadcast failed: {message}")

    def record_broadcast_attempt(self) -> None:
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["last_attempt_at"])

    def validate_fee_limit(self) -> None:
        if self.fee_limit <= 0:
            raise ValueError("Tron fee_limit must be > 0")

    def persist_signed_payload(self, *, signed_payload: dict, tx_id: str) -> None:
        raw_data = signed_payload.get("raw_data") or {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        self.signed_payload = signed_payload
        self.tx_id = tx_id
        self.expiration = raw_data.get("expiration") or None
        self.ref_block_bytes = str(raw_data.get("ref_block_bytes") or "")
        self.ref_block_hash = str(raw_data.get("ref_block_hash") or "")
        self.save(
            update_fields=[
                "signed_payload",
                "tx_id",
                "expiration",
                "ref_block_bytes",
                "ref_block_hash",
            ]
        )
        self.base_task.append_tx_hash(tx_id)

    def mark_pending_chain(self) -> None:
        TxTask.objects.filter(
            pk=self.base_task_id,
            status__in=(TxTaskStatus.QUEUED, TxTaskStatus.PENDING_CHAIN),
        ).update(
            status=TxTaskStatus.PENDING_CHAIN,
            updated_at=timezone.now(),
        )

    @staticmethod
    def is_duplicate_broadcast_response(response: dict) -> bool:
        code = str(response.get("code") or "").upper()
        message = str(response.get("message") or "").upper()
        return "DUP_TRANSACTION" in code or "DUP_TRANSACTION" in message

    @classmethod
    def schedule(cls, intent: TronTxIntent) -> TronTxTask:
        if intent.verify_fn is not None:
            intent.verify_fn()
        with db_transaction.atomic():
            base_task = TxTask.objects.create(
                chain=intent.chain,
                sender=intent.sender,
                tx_type=intent.tx_type,
                status=TxTaskStatus.QUEUED,
            )
            return cls.objects.create(
                base_task=base_task,
                sender=intent.sender,
                chain=intent.chain,
                to=intent.to,
                function_selector=intent.function_selector,
                parameter=intent.parameter,
                fee_limit=intent.fee_limit,
            )


class TronVaultSlot(models.Model):
    """项目在 Tron 链上的 XcashVaultSlot，仅用于 TRC20 收款/归集。"""

    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    usage = models.CharField(
        _("用途"),
        choices=TronVaultSlotUsage,
        max_length=16,
        db_index=True,
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("客户"),
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    invoice_index = models.PositiveIntegerField(_("账单槽位序号"), null=True, blank=True)
    address = AddressField(_("收款地址"))
    salt = models.BinaryField(_("CREATE2 Salt"), max_length=32)
    deploy_tx_task = models.OneToOneField(
        "tron.TronTxTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployed_vault_slot",
        verbose_name=_("部署交易任务"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("customer", "chain"),
                name="uniq_tron_vault_slot_customer_chain",
            ),
            models.UniqueConstraint(
                fields=("project", "usage", "chain", "invoice_index"),
                name="uniq_tron_vault_slot_project_usage_chain_invoice_index",
            ),
            models.UniqueConstraint(
                fields=("chain", "address"),
                name="uniq_tron_vault_slot_chain_address",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        usage=TronVaultSlotUsage.DEPOSIT,
                        customer__isnull=False,
                        invoice_index__isnull=True,
                    )
                    | models.Q(
                        usage=TronVaultSlotUsage.INVOICE,
                        customer__isnull=True,
                        invoice_index__isnull=False,
                    )
                ),
                name="ck_tron_vault_slot_usage_customer",
            ),
        ]
        verbose_name = _("Tron 收款地址")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.address

    def save(self, *args, **kwargs):
        if self.chain_id and self.chain.type != ChainType.TRON:
            raise ValueError("TronVaultSlot 仅支持 Tron 链")
        if self.usage == TronVaultSlotUsage.DEPOSIT:
            if self.customer_id is None:
                raise ValueError("TronVaultSlot customer is required for deposit usage")
            if self.project_id is None:
                self.project_id = self.customer.project_id
            if self.invoice_index is not None:
                raise ValueError("invoice_index must be empty for deposit usage")
        elif self.usage == TronVaultSlotUsage.INVOICE:
            if self.customer_id is not None:
                raise ValueError("customer must be empty for invoice usage")
            if self.invoice_index is None:
                raise ValueError("invoice_index is required for invoice usage")
        return super().save(*args, **kwargs)

    @property
    def is_deployed(self) -> bool:
        if self.deploy_tx_task_id is None:
            return False
        return self.deploy_tx_task.base_task.status == TxTaskStatus.CONFIRMED

    @staticmethod
    def build_salt(
        *,
        usage: TronVaultSlotUsage,
        customer: Customer | None = None,
        project_id: int | None = None,
        invoice_index: int | None = None,
    ) -> bytes:
        if usage == TronVaultSlotUsage.DEPOSIT:
            if customer is None:
                raise ValueError("customer is required for deposit salt")
            return keccak(
                b"xcash:tron-vault-slot:deposit:"
                + str(customer.project_id).encode()
                + b":"
                + customer.uid.encode()
            )
        if usage == TronVaultSlotUsage.INVOICE:
            if project_id is None or invoice_index is None:
                raise ValueError("project_id and invoice_index are required")
            return keccak(
                b"xcash:tron-vault-slot:invoice:"
                + str(project_id).encode()
                + b":"
                + str(invoice_index).encode()
            )
        raise ValueError(f"unsupported TronVaultSlot usage: {usage}")

    @staticmethod
    def predict_address(*, vault: str, salt: bytes) -> str:
        return predict_tron_vault_slot_address(
            vault=vault,
            salt=salt,
            factory=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
            vault_slot_template=settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS,
        )

    @staticmethod
    def ensure_deposit_address(chain: Chain, customer: Customer) -> str:
        if chain.type != ChainType.TRON:
            raise ValueError("TronVaultSlot 仅支持 Tron 链")
        if not settings.TRON_VAULT_SLOT_FACTORY_ADDRESS or not settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS:
            raise RuntimeError("Tron VaultSlot factory/template 未配置")

        project = customer.project
        existing = TronVaultSlot.objects.filter(
            chain=chain,
            project=project,
            usage=TronVaultSlotUsage.DEPOSIT,
            customer=customer,
        ).first()
        if existing is not None:
            db_transaction.on_commit(
                lambda slot_pk=existing.pk: TronVaultSlot.schedule_deploy(slot_pk)
            )
            return existing.address

        if not project.vault:
            raise RuntimeError(f"Project {customer.project_id} VaultSlot Vault 地址未配置")
        salt = TronVaultSlot.build_salt(
            usage=TronVaultSlotUsage.DEPOSIT,
            customer=customer,
        )
        slot_address = TronVaultSlot.predict_address(vault=project.vault, salt=salt)
        try:
            slot, created = TronVaultSlot.objects.get_or_create(
                chain=chain,
                project=project,
                usage=TronVaultSlotUsage.DEPOSIT,
                customer=customer,
                defaults={"address": slot_address, "salt": salt},
            )
        except IntegrityError as exc:
            try:
                slot = TronVaultSlot.objects.get(
                    chain=chain,
                    project=project,
                    usage=TronVaultSlotUsage.DEPOSIT,
                    customer=customer,
                )
            except TronVaultSlot.DoesNotExist as not_exist_exc:
                raise exc from not_exist_exc
        else:
            if created:
                db_transaction.on_commit(
                    lambda slot_pk=slot.pk: TronVaultSlot.schedule_deploy(slot_pk)
                )
        return slot.address

    @staticmethod
    def ensure_invoice_address(*, project, chain: Chain, invoice_index: int) -> str:
        if chain.type != ChainType.TRON:
            raise ValueError("TronVaultSlot 仅支持 Tron 链")
        if not settings.TRON_VAULT_SLOT_FACTORY_ADDRESS or not settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS:
            raise RuntimeError("Tron VaultSlot factory/template 未配置")

        existing = TronVaultSlot.objects.filter(
            chain=chain,
            project=project,
            usage=TronVaultSlotUsage.INVOICE,
            invoice_index=invoice_index,
        ).first()
        if existing is not None:
            db_transaction.on_commit(
                lambda slot_pk=existing.pk: TronVaultSlot.schedule_deploy(slot_pk)
            )
            return existing.address

        if not project.vault:
            raise RuntimeError(f"Project {project.pk} VaultSlot Vault 地址未配置")
        salt = TronVaultSlot.build_salt(
            usage=TronVaultSlotUsage.INVOICE,
            project_id=project.pk,
            invoice_index=invoice_index,
        )
        slot_address = TronVaultSlot.predict_address(vault=project.vault, salt=salt)
        try:
            slot, created = TronVaultSlot.objects.get_or_create(
                chain=chain,
                project=project,
                usage=TronVaultSlotUsage.INVOICE,
                invoice_index=invoice_index,
                defaults={"address": slot_address, "salt": salt},
            )
        except IntegrityError as exc:
            try:
                slot = TronVaultSlot.objects.get(
                    chain=chain,
                    project=project,
                    usage=TronVaultSlotUsage.INVOICE,
                    invoice_index=invoice_index,
                )
            except TronVaultSlot.DoesNotExist as not_exist_exc:
                raise exc from not_exist_exc
        else:
            if created:
                db_transaction.on_commit(
                    lambda slot_pk=slot.pk: TronVaultSlot.schedule_deploy(slot_pk)
                )
        return slot.address

    @staticmethod
    def schedule_deploy(slot_pk: int) -> TronTxTask | None:
        with db_transaction.atomic():
            slot = (
                TronVaultSlot.objects.select_for_update(of=("self",))
                .select_related("chain", "project")
                .get(pk=slot_pk)
            )
            slot.refresh_from_db(fields=["deploy_tx_task"])
            if slot.deploy_tx_task_id is not None:
                deploy_task = TronTxTask.objects.select_related("base_task").get(
                    pk=slot.deploy_tx_task_id
                )
                if deploy_task.base_task.status != TxTaskStatus.FAILED:
                    return deploy_task

            if TronAdapter().is_contract(slot.chain, slot.address):
                return None

            system_wallet = SystemWallet.get_current()
            sender = system_wallet.wallet.get_address(
                chain_type=ChainType.TRON,
                usage=AddressUsage.HotWallet,
            )
            if not slot.project.vault:
                raise RuntimeError(f"Project {slot.project_id} VaultSlot Vault 地址未配置")
            intent = build_vault_slot_deploy_intent(
                sender=sender,
                chain=slot.chain,
                factory_address=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
                vault_address=slot.project.vault,
                salt=bytes(slot.salt),
            )
            task = TronTxTask.schedule(intent)
            TronVaultSlot.objects.filter(pk=slot.pk).update(deploy_tx_task=task)
            return task

    @staticmethod
    def schedule_collect_for_deposit(deposit_pk: int) -> TronVaultSlotCollectSchedule | None:
        from deposits.models import Deposit

        deposit = Deposit.objects.select_related(
            "customer",
            "transfer__chain",
            "transfer__crypto",
        ).get(pk=deposit_pk)
        transfer = deposit.transfer
        if transfer.crypto == transfer.chain.native_coin:
            return None
        slot = TronVaultSlot.objects.get(
            chain=transfer.chain,
            customer=deposit.customer,
            usage=TronVaultSlotUsage.DEPOSIT,
            address=transfer.to_address,
        )
        return TronVaultSlot.schedule_collect_for_slot(
            chain=transfer.chain,
            crypto=transfer.crypto,
            slot=slot,
        )

    @staticmethod
    def schedule_collect_for_invoice(invoice_pk: int) -> TronVaultSlotCollectSchedule | None:
        from invoices.models import Invoice

        invoice = Invoice.objects.select_related("project", "chain", "crypto").get(
            pk=invoice_pk
        )
        if invoice.chain_id is None or invoice.crypto_id is None or not invoice.pay_address:
            return None
        if invoice.crypto == invoice.chain.native_coin:
            return None
        slot = TronVaultSlot.objects.get(
            chain=invoice.chain,
            project=invoice.project,
            usage=TronVaultSlotUsage.INVOICE,
            address=invoice.pay_address,
        )
        return TronVaultSlot.schedule_collect_for_slot(
            chain=invoice.chain,
            crypto=invoice.crypto,
            slot=slot,
        )

    @staticmethod
    def schedule_collect_for_slot(
        *,
        chain: Chain,
        crypto,
        slot: TronVaultSlot,
    ) -> TronVaultSlotCollectSchedule:
        if not crypto.address(chain):
            raise RuntimeError(f"Crypto {crypto.symbol} 未部署在链 {chain.code}")
        return TronVaultSlotCollectSchedule.ensure_pending(
            chain=chain,
            vault_slot=slot,
            crypto=crypto,
        )

    @staticmethod
    def create_collect_tx_task_for_slot(*, chain: Chain, crypto, slot: TronVaultSlot) -> TronTxTask:
        token_address = crypto.address(chain)
        if not token_address:
            raise RuntimeError(f"Crypto {crypto.symbol} 未部署在链 {chain.code}")
        sender = SystemWallet.get_current().wallet.get_address(
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
        )
        intent = build_vault_slot_collect_intent(
            sender=sender,
            chain=chain,
            vault_slot_address=slot.address,
            token_address=token_address,
        )
        # 不去重复用在途归集任务:归集计划 tx_task 是 OneToOne,复用同一任务会让两个计划
        # 撞唯一约束;而 collect 是按当前余额全额清扫的幂等操作,为每个到期计划各建一笔
        # 任务最多多一次空扫(余额为 0 时模板直接 return),不会重复归集。
        return TronTxTask.schedule(intent)

    @staticmethod
    def matched_addresses_for_candidates(*, chain: Chain, candidates: set[str]) -> set[str]:
        if not candidates:
            return set()
        return set(
            TronVaultSlot.objects.filter(
                chain=chain,
                address__in=candidates,
            ).values_list("address", flat=True)
        )


class TronVaultSlotCollectSchedule(models.Model):
    vault_slot = models.ForeignKey(
        TronVaultSlot,
        on_delete=models.CASCADE,
        related_name="collect_schedules",
        verbose_name=_("收款地址"),
    )
    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    crypto = models.ForeignKey(
        "currencies.Crypto",
        on_delete=models.PROTECT,
        verbose_name=_("币种"),
    )
    due_at = models.DateTimeField(_("计划执行时间"), db_index=True)
    tx_task = models.OneToOneField(
        "tron.TronTxTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vault_slot_collect_schedule",
        verbose_name=_("链上任务"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "vault_slot", "crypto"),
                condition=models.Q(tx_task__isnull=True),
                name="uniq_pending_tron_vault_slot_collect",
            ),
        ]
        ordering = ("due_at", "pk")
        verbose_name = _("Tron 收款地址归集计划")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.chain_id}:{self.vault_slot_id}:{self.crypto_id}"

    @classmethod
    def ensure_pending(
        cls,
        *,
        chain: Chain,
        vault_slot: TronVaultSlot,
        crypto,
    ) -> TronVaultSlotCollectSchedule:
        existing = cls.objects.filter(
            chain=chain,
            vault_slot=vault_slot,
            crypto=crypto,
            tx_task__isnull=True,
        ).first()
        if existing is not None:
            return existing
        due_at = timezone.now() + get_vault_slot_collect_delay()
        try:
            return cls.objects.create(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
                due_at=due_at,
            )
        except IntegrityError:
            return cls.objects.get(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
                tx_task__isnull=True,
            )

    def create_tx_task(self) -> TronTxTask:
        return TronVaultSlot.create_collect_tx_task_for_slot(
            chain=self.chain,
            crypto=self.crypto,
            slot=self.vault_slot,
        )

    @classmethod
    def execute_due(cls, *, limit: int = 32) -> int:
        now = timezone.now()
        created_count = 0
        with db_transaction.atomic():
            schedules = list(
                cls.objects.select_for_update(skip_locked=True)
                .select_related("chain", "crypto", "vault_slot")
                .filter(tx_task__isnull=True, due_at__lte=now)
                .order_by("due_at", "pk")[:limit]
            )
            for schedule in schedules:
                # 归集必须打到「已部署」的 slot:TVM 对无 code 地址的合约调用会返回
                # success 但什么都不做,过早建任务会被回执确认误判为归集成功(资金仍滞留)。
                # 未部署则跳过本轮,等部署确认后下一轮再建。
                if not TronAdapter().is_contract(
                    schedule.chain, schedule.vault_slot.address
                ):
                    continue
                # 单条用 savepoint 隔离:个别计划建/绑任务失败不回滚整批归集调度。
                try:
                    with db_transaction.atomic():
                        tx_task = schedule.create_tx_task()
                        schedule.tx_task = tx_task
                        schedule.save(update_fields=["tx_task", "updated_at"])
                except IntegrityError:
                    logger.warning(
                        "Tron 归集计划绑定任务失败,跳过",
                        schedule_id=schedule.pk,
                    )
                    continue
                created_count += 1
        return created_count
