from django.db import migrations
from django.db import models


def normalize_tx_kind(apps, schema_editor):
    EvmBroadcastTask = apps.get_model("evm", "EvmBroadcastTask")
    valid_tx_kinds = ["native_transfer", "contract_call"]
    invalid_rows = EvmBroadcastTask.objects.exclude(tx_kind__in=valid_tx_kinds)
    invalid_rows.filter(models.Q(data="") | models.Q(data="0x")).update(
        tx_kind="native_transfer"
    )
    invalid_rows.update(tx_kind="contract_call")


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0003_add_tx_kind_to_evm_broadcast_task"),
    ]

    operations = [
        migrations.RunPython(normalize_tx_kind, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="evmbroadcasttask",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("tx_kind__in", ["native_transfer", "contract_call"])
                ),
                name="ck_evm_broadcast_task_tx_kind_valid",
            ),
        ),
    ]
