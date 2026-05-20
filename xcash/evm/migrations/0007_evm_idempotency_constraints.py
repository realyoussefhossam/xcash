from django.db import migrations
from django.db import models
from django_migration_linter.operations import IgnoreMigration

ACTIVE_STATUSES = ("created", "broadcasted", "confirmed")


def normalize_active_idempotency_duplicates(apps, schema_editor):
    """Normalize active x402/CREATE2 idempotency duplicates before unique constraints.

    For each active duplicate group, rows are ordered by primary key. The first row is kept.
    Later duplicate rows are marked status="dropped" and failure_reason="" only when
    broadcast_task_id is None or status == "created", because those rows have not safely
    entered the chain broadcast lifecycle. If any later duplicate row is already broadcasted
    or confirmed, the migration raises RuntimeError with the conflicting primary keys instead
    of guessing which business record to keep; operators must resolve those rows before
    rerunning migrations. The reverse migration is a no-op because overwritten status values
    cannot be reconstructed.
    """
    X402Facilitation = apps.get_model("evm", "X402Facilitation")
    ContractDeployCollection = apps.get_model("evm", "ContractDeployCollection")

    _normalize_duplicate_groups(
        X402Facilitation,
        group_fields=(
            "chain_id",
            "crypto_id",
            "authorization_from_address",
            "authorization_nonce",
        ),
    )
    _normalize_duplicate_groups(
        ContractDeployCollection,
        group_fields=("chain_id", "factory_address", "salt"),
    )
    _normalize_duplicate_groups(
        ContractDeployCollection,
        group_fields=("chain_id", "collector_address"),
    )


def _normalize_duplicate_groups(model, *, group_fields):
    duplicate_groups = (
        model.objects.filter(status__in=ACTIVE_STATUSES)
        .values(*group_fields)
        .annotate(row_count=models.Count("pk"))
        .filter(row_count__gt=1)
    )

    for group in duplicate_groups:
        filters = {field: group[field] for field in group_fields}
        rows = list(
            model.objects.filter(status__in=ACTIVE_STATUSES, **filters).order_by("pk"),
        )
        keep = rows[0]
        duplicate_pks = [row.pk for row in rows[1:]]
        unsafe_pks = [
            row.pk
            for row in rows[1:]
            if row.status in {"broadcasted", "confirmed"}
        ]
        if unsafe_pks:
            raise RuntimeError(
                f"{model.__name__} active idempotency duplicate conflict: "
                f"keep_pk={keep.pk}, duplicate_pks={duplicate_pks}, "
                f"unsafe_pks={unsafe_pks}",
            )

        for row in rows[1:]:
            if row.broadcast_task_id is None or row.status == "created":
                row.status = "dropped"
                row.failure_reason = ""
                row.save(update_fields=["status", "failure_reason", "updated_at"])


def noop_reverse(apps, schema_editor):
    """No-op reverse: dropped duplicate statuses overwrite prior values and cannot be restored."""


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0006_alter_contractdeploycollection_failure_reason_and_more"),
    ]

    # 已通过 RunPython 归一化可机械处理的重复行；无法机械判断的已广播冲突会中止迁移，故安全。
    operations = [
        IgnoreMigration(),
        migrations.RunPython(normalize_active_idempotency_duplicates, noop_reverse),
        migrations.AddConstraint(
            model_name="x402facilitation",
            constraint=models.UniqueConstraint(
                fields=(
                    "chain",
                    "crypto",
                    "authorization_from_address",
                    "authorization_nonce",
                ),
                condition=models.Q(
                    status__in=["created", "broadcasted", "confirmed"],
                ),
                name="uniq_active_x402_authorization_nonce",
            ),
        ),
        migrations.AddConstraint(
            model_name="contractdeploycollection",
            constraint=models.UniqueConstraint(
                fields=("chain", "factory_address", "salt"),
                condition=models.Q(
                    status__in=["created", "broadcasted", "confirmed"],
                ),
                name="uniq_active_create2_chain_factory_salt",
            ),
        ),
        migrations.AddConstraint(
            model_name="contractdeploycollection",
            constraint=models.UniqueConstraint(
                fields=("chain", "collector_address"),
                condition=models.Q(
                    status__in=["created", "broadcasted", "confirmed"],
                ),
                name="uniq_active_create2_chain_collector",
            ),
        ),
    ]
