# 运行时只接收 schema 2，旧数据离线迁移到新文件

状态：已从当前实现确认；这是对既有决策的记录，不是新迁移计划。原始决策日期与完整选型讨论：Unknown / Needs confirmation。

## 已确认的选择

- [`Database.init_db`](../../dafeiyu/repositories/sqlite.py) 只接受完整 schema 2，或在空库中初始化 schema 2；遇到其他已有结构明确拒绝启动。
- [`migrate.py`](../../dafeiyu/tools/migrate.py) 以只读方式读取预期的 1.x 库，写入不存在的新目标文件；校验行数、摘要、外键与完整性，不覆盖旧库、不切换服务、不导入凭据缓存。
- 迁移不复制旧向量。独立的 [`reembed.py`](../../dafeiyu/tools/reembed.py) 先准备完整向量集，再校验记忆快照并在事务中替换，保存 provider/model 身份。

## 依据与后果

工具 docstring、拒绝覆盖条件及事务验证明确体现“保留来源、核验目标、显式切换”的数据安全边界；这并不证明当初选择 SQLite 而非其他数据库的原因，后者仍为 Unknown / Needs confirmation。

新安装直接使用新库；旧版升级需先生成一致的源快照、迁移到新文件并检查报告，再由操作者决定切换 `STATE_DB`。目标文件失败时可能保留，不能不经检查直接用于运行。重建向量需要真实模型配置、可能产生费用；快照变化或向量不完整时拒绝替换。

命令参数以 `python -m dafeiyu.tools.migrate --help` 和 `python -m dafeiyu.tools.reembed --help` 为准，不在此复制操作脚本。

验证依据：[`test_migration.py`](../../tests/test_migration.py)、[`test_reembed.py`](../../tests/test_reembed.py)、[`test_repositories.py`](../../tests/test_repositories.py)。
