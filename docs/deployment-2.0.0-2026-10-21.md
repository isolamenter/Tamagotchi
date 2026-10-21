# 大肥鱼 2.0 生产部署记录

日期：2026-10-21。运行代码提交：`6d307d4372000026cfc2d6043929f03560e74319`。目标：`gcp-vps:/home/akiya/tamagotchi`，systemd 服务 `tamagotchi`。已按用户“提交部署”授权完成发布，没有执行 Git push。

## 发布结果

- 服务 `active/running`，ASGI 入口 `dafeiyu.app:create_app --factory`。
- 后台：[大肥鱼工作台](https://tamagotchi.isolamenter.com/web)，原独立 GM 口令保留，在登录页输入；不再使用 URL 口令。
- 本地 `http://127.0.0.1:8000/healthz` 与公网 `/healthz` 均返回 `{"ok":true,"version":"2.0.0"}`。
- 新代码目录 `/home/akiya/tamagotchi/releases/6d307d4`，独立环境 `/home/akiya/tamagotchi/.venv-v2-6d307d4`。依赖按生产原有 39 项版本约束安装，没有变更其他服务或网络设施。
- 只上传已提交的运行包。包内排除实际 `.env`、所有数据库、测试、文档、日志和缓存。

## 本地与文件验证

- 完整单元／集成测试 117 项通过，Python 编译、Ruff 检查／格式检查、TOML 解析与 Git 差异检查通过；暂存后发现的两处空白已修正，迁移测试再次通过。
- 前一阶段的桌面／移动端 Playwright 验收通过，全部演示数据为虚构内容。本次没有重复浏览器操作，生产后台验证为页面／资源和鉴权 API 检查。
- 干净发布包 `dist/dafeiyu-2.0.0-6d307d4.tar.gz`，清单标记 `working_tree_dirty=false`，包内 56 个运行文件逐一与上述提交一致。
- 归档 SHA-256：`09c93211e12d07e2959f870d9bb7b552cafdd988c9b0ea66db84effdf5b65845`。上传后和启动前／验收时均核对逐文件 SHA-256，通过。
- `dist/dafeiyu-2.0.0-local.tar.gz` 和 `dist/dafeiyu-2.0.0-meme-fix-local.tar.gz` 是过期的未提交候选，未用于部署。

## 数据切换

先在旧服务运行期间试回填并验证真实外部接口，再停止旧服务，生成一致快照并正式导入新文件。未上传、覆盖、清空或恢复原 `state.db`。

| 数据 | 正式回填数量 |
| --- | ---: |
| 会话 | 1 |
| 消息 | 54031 |
| 记忆 | 1418 |
| 用户称谓 | 6 |
| 去重记录 | 716 |
| 已发布内容卡 | 22 |

- 新运行库：`/home/akiya/tamagotchi/data/dafeiyu-2.0.0-6d307d4.db`，结构版本 2。
- 逐表数量和内容摘要核验通过，新旧库完整性均为 `ok`，外键检查无异常。旧养成字段、结算卡、未发布卡和临时缓存未导入；原历史仍保留在旧库和一致快照中。
- 旧向量未伪造来源导入。按 `gemini / gemini-embedding-001` 重建 1418 条记忆向量及 160 条风格向量，维度 3072，后台待构建数量为 0。
- 原库在停写后的 SHA-256 为 `22b1a08c368dcf27dd9934effd5403830b2b991d3a70b0c7fb8d0abb4a40beae`，切换后核对未变。
- 原代码、原虚拟环境和原 `.env` 保留。配置及快照位于 `/home/akiya/tamagotchi/backups/2.0.0-6d307d4`，未执行清理或回滚。

## 服务配置与业务检查

除运行包外，服务器生成以下本服务配置，均未包含在上传包中：

- 版本目录 `.env`：沿用生产凭证和明确的模型配置，只将 `STATE_DB` 指向已核验的新库；原根目录 `.env` 未改。
- `/etc/systemd/system/tamagotchi.service.d/zz-dafeiyu-v2-6d307d4.conf`：设置新工作目录、独立环境、配置文件和工厂入口。保留原 `0.0.0.0:8000` 监听，未更改 Tunnel、防火墙或 SSH。
- `/home/akiya/tamagotchi/backups/2.0.0-6d307d4/uvicorn-logging.json`：沿用 Uvicorn 日志配置，并接入 `dafeiyu` 的 INFO 日志；当前启动配置依赖此文件，不应作为普通备份清理。

实际验证通过：

- 真实模型返回合法 JSON；合成问候场景未放行无关梗图，没有输出模型回复正文。
- 真实批量 embedding、风格索引与代表性检索通过；正式记忆索引全量重建成功。
- 飞书身份查询、三张开放梗图真实上传、webhook 校验通过；没有向群聊发送测试消息。
- 后台未鉴权返回 401，原独立 GM 口令可通过；会话、状态、消息和记忆读取成功，私有响应 `Cache-Control: no-store`。
- 本地后台页面和四项静态资源成功；公网健康、后台页面及带鉴权的 overview 成功。
- 新启动日志无 traceback、配置缺失、依赖、数据库或索引初始化错误，风格索引就绪日志已确认。

首次公网验收的默认 Python User-Agent 被 Cloudflare 返回 403，导致验收断言停止。排查确认应用本地检查正常；使用浏览器 User-Agent 后三个公网路径均返回 200，本地独立 curl 也返回正确版本。只调整验收客户端，没有修改 Cloudflare 配置，也没有为此回滚或重启服务。

## 剩余验收与风险

- 实际群内文字／图片投递、主动发言、日记和梦境未主动触发；多轮自然度与长期召回质量仍需真实使用观察。
- 生产未配置 `IMAGE_MODEL`，后台显示生图能力关闭；本次保持原有配置。
- 梗图匹配采用保守的完整句式白名单，可能少发合适的图片。发送与记录之间仍有跨系统中断窗口。
- 备份、旧库与当前新库都属于持久数据；删除、恢复和回滚仍需按仓库规范取得明确授权。

## 已部署运行文件

```text
assets/memes/eat_rice.png
assets/memes/fat_defense.png
assets/memes/overload.png
assets/memes/shocked.png
assets/memes/slack_off.png
assets/memes/smug.png
config/persona.toml
config/prompts.toml
config/settings.toml
config/style_corpus.toml
dafeiyu/__init__.py
dafeiyu/app.py
dafeiyu/config.py
dafeiyu/domain/__init__.py
dafeiyu/domain/conversation.py
dafeiyu/domain/meme.py
dafeiyu/domain/memory.py
dafeiyu/domain/reply.py
dafeiyu/domain/scene.py
dafeiyu/domain/state.py
dafeiyu/domain/style.py
dafeiyu/integrations/__init__.py
dafeiyu/integrations/feishu_client.py
dafeiyu/integrations/llm_client.py
dafeiyu/repositories/__init__.py
dafeiyu/repositories/conversation_repo.py
dafeiyu/repositories/memory_repo.py
dafeiyu/repositories/message_repo.py
dafeiyu/repositories/schema.sql
dafeiyu/repositories/sqlite.py
dafeiyu/repositories/style_repo.py
dafeiyu/repositories/system_repo.py
dafeiyu/routes/__init__.py
dafeiyu/routes/feishu.py
dafeiyu/routes/gm.py
dafeiyu/routes/health.py
dafeiyu/routes/web.py
dafeiyu/runtime.py
dafeiyu/services/__init__.py
dafeiyu/services/autonomous_service.py
dafeiyu/services/delivery.py
dafeiyu/services/incoming.py
dafeiyu/services/memory_service.py
dafeiyu/services/observer_service.py
dafeiyu/services/reply_service.py
dafeiyu/services/style_service.py
dafeiyu/tools/__init__.py
dafeiyu/tools/migrate.py
dafeiyu/tools/reembed.py
dafeiyu/tools/release.py
dafeiyu/web/character.png
dafeiyu/web/favicon.svg
dafeiyu/web/index.html
dafeiyu/web/workspace.css
dafeiyu/web/workspace.js
requirements.txt
```
