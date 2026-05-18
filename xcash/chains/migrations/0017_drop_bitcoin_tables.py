from django.db import migrations


class Migration(migrations.Migration):
    """物理删除已下线的 bitcoin app 残留表与 Django 元数据。

    设计要点：
    - chain_type / failure_reason 的 AlterField 已在 0016 中完成（同一批
      ChainType 收紧改动），本迁移只承担 DROP TABLE 与元数据清理。
    - 业务数据在 0015 (RunPython) 中已全部清空，本迁移直接 DROP 整张
      bitcoin_bitcoinscancursor 物理表是安全的。
    - 使用字符串字面量，避免任何对已删除的 bitcoin app 的模型/类的引用。
    - reverse_sql 使用 noop：bitcoin app 已彻底下线，回滚没有业务意义；
      若需复活该表必须新写一个建表迁移而不是反向本迁移。
    """

    dependencies = [
        ("chains", "0016_add_non_evm_chain_type_constraint"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TABLE IF EXISTS bitcoin_bitcoinscancursor;",
                "DELETE FROM django_migrations WHERE app = 'bitcoin';",
                # auth_permission 持有 django_content_type 的外键，必须
                # 先把 bitcoin app_label 关联的权限删掉，再删 content_type。
                (
                    "DELETE FROM auth_permission WHERE content_type_id IN "
                    "(SELECT id FROM django_content_type WHERE app_label = 'bitcoin');"
                ),
                "DELETE FROM django_content_type WHERE app_label = 'bitcoin';",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
