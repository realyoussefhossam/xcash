from django.db import migrations
from django.db import models
from django_migration_linter.operations import IgnoreMigration


def delete_orphan_evm_broadcast_tasks(apps, schema_editor):
    """删除 base_task 为空的 EvmBroadcastTask 孤儿行。

    归一化规则：
    - 这些行不挂在 chains.BroadcastTask 跨链统一锚点上，无法参与广播状态机
      与跨链协调，是历史遗留的死数据，新生产路径 (EvmBroadcastTask.schedule)
      不会再产生此类行。
    - 直接删除，反向函数 no-op（数据不可恢复，且业务上无价值）。
    """
    EvmBroadcastTask = apps.get_model("evm", "EvmBroadcastTask")
    EvmBroadcastTask.objects.filter(base_task__isnull=True).delete()


def noop_reverse(apps, schema_editor):
    """无法恢复已删除的孤儿行，且业务上不存在合理回滚语义。"""


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0010_contractdeploycollection_collector_init_code"),
    ]

    # 已通过 RunPython 删除 base_task IS NULL 的孤儿行，故收紧为 NOT NULL 安全。
    operations = [
        IgnoreMigration(),
        migrations.RunPython(delete_orphan_evm_broadcast_tasks, noop_reverse),
        migrations.AlterField(
            model_name="evmbroadcasttask",
            name="base_task",
            field=models.OneToOneField(
                on_delete=models.deletion.CASCADE,
                related_name="evm_task",
                to="chains.broadcasttask",
                verbose_name="通用链上任务",
            ),
        ),
    ]
