# 群聊持久记忆

记忆使用本地 SQLite，按机器人 QQ 和群号隔离。从启用后开始积累，不导入 QQ 的旧数据库或过去日志。

## 保存与整理

- 原文和摘要位于 `nonebot/.run/memory.sqlite3`，重启保留；不提交 Git。
- 保存群友文字、提及信息和机器人成功发送的回答，不识别图片或语音正文。被图片命令插件拦截的消息不进入聊天记忆。
- 每群默认保留最近 10000 条原文，是条数而非七天限制。未整理记录不删除，因此接口长期失败时可能超过上限。
- 默认最近 20 条原文可参与回答；启用回复规划后，近期背景改为最多 4 条精简片段，旧记忆按需选择。
- 每分钟检查整理任务。北京时间 03:00 后处理前一天及尚未完成的旧记录；没有新记录时不调用模型。
- 当天未整理消息达到 100 条或约 12000 字符时，可提前增量整理。同群最短间隔 30 分钟，失败也占用间隔。
- 默认每 10 天合并日摘要，周期结束后才处理；日摘要作为来源保留。摘要是有损压缩，可能遗漏或理解错误。
- 长记录分批整理，保存处理进度。极端崩溃可能导致一次摘要请求重复计费。

## 配置

直接编辑 `nonebot/config.yaml` 的 `memory` 段，修改后重启 NoneBot。完整默认值和范围见 `src/memory.py` 的 `MemoryConfig`。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| enabled | true（仓库配置） | 是否记录和整理；关闭不会删除已有数据库 |
| max_records | 10000 | 每群原文保留软上限 |
| recent_records | 20 | 非规划模式下回答携带的近期原文条数 |
| timezone | Asia/Shanghai | 记录归属日期与整理时区 |
| daily_hour / daily_minute | 3 / 0 | 每日整理开始时间 |
| early_enabled | true | 是否允许当天提前整理 |
| early_records / early_chars | 100 / 12000 | 提前整理的条数或字符阈值 |
| early_interval_minutes | 30 | 同群提前整理最短间隔 |
| merge_days | 10 | 日摘要合并周期 |
| summary_chars / batch_chars | 1800 / 12000 | 摘要长度与批次字符预算 |
| max_calls_per_run | 12 | 每轮整理调用上限，不是每日费用上限 |
| facts_enabled / facts_per_group | true / 5000 | 结构化事实开关与每群保留上限 |
| fact_context_count | 6 | 回答最多携带的相关事实条数 |
| verify_on_challenge | true | 被质疑记错时尝试核查原文 |
| max_tool_searches | 2 | 每轮模型主动检索次数；0 关闭工具调用 |
| context_chars / context_tokens | 4000 / 2000 | 回答附加记忆的预算 |
| tool_result_chars / tool_result_tokens | 4000 / 2000 | 本轮所有检索结果共享预算 |
| batch_tokens | 8000 | 整理输入的内容 Token 预算 |

字符和 Token 预算同时生效，不包含整个人设、系统规则及服务端封装，不能作为整次 API 计费上限。降低保留上限前应备份数据。时区变更不重新分桶旧记录，合并周期变更会重新生成相应周期摘要。

## 检索、事实与归属

回答模型可以调用 `memory_search(query)` 检索当前群原文。程序固定机器人、群号和当前消息之前的边界，模型不能选择其他群或读取未来消息。质疑记忆时也会尝试原文核查；未命中不等于从未说过。

原文使用 SQLite FTS5、中文二字片段与 BM25 排序，包含少量同义词扩展；没有 FTS5 时降级为关键词扫描。这不是向量语义检索，隐晦表达可能漏检。返回相关原文及相邻片段，提供给模型时按预算截断并标记。

摘要调用同时提取结构化事实，不单独增加提取请求。事实保留人物 QQ、发言人、事件时间、陈述性质和来源记录。程序核对来源属于本群，区分本人陈述、转述、猜测和更正；部分冲突标记为待核查，仍不能保证语义正确。原文过期后不能冒充已核查来源。

当前没有网页管理或人工事实编辑入口；已有数据库中的修订状态仍保留并可被读取。停用条目不等于删除原文或重写旧摘要。

常见 API Key、Bearer 和带密码标签的内容在进入上下文或落库前脱敏，不能识别所有秘密。旧数据库不会自动批量重写。

## AI 接话与记忆规划

`reply.planner_enabled` 控制独立的接话判断。它使用当前消息片段和少量近期消息，决定是否回复以及检索主题、人物和记录日期；不携带完整人设，关闭思考。决定回复后才生成正式回答。

明确 @ 和关键词请求必须回复，规划失败时回退正常回答；普通消息判断失败时保持安静。已移除连续对话窗口，不再因刚回复过某人而强制接下其未 @ 消息。

参数包括 `planner_filter_enabled`、`planner_interval_seconds`、`planner_recent_records`、`planner_message_chars`、`planner_input_tokens` 和 `planner_timeout_seconds`。当前实际值以 `config.yaml` 为准。关闭本地过滤且间隔为 0 时，普通消息可能频繁进入模型判断；群内忙碌时普通消息只记录背景。

规划模式优先携带相关事实及原文，两者未命中时才从最近 60 份日摘要中尝试选择一份相关片段。不默认携带完整日摘要或周期摘要。关闭规划仍可使用独立的模型记忆检索工具。

判断本身增加一次模型调用，即使最终不回复也有费用；正式回答、摘要、周期整理及带检索结果的后续回答也会增加用量。本地 SQLite 查询本身不调用模型。

## 终端操作

在仓库根目录执行：

```bash
# 状态与累计用量
docker compose exec -T nonebot python -m src.memory_tool status

# 本群关键词检索，不调用模型
docker compose exec -T nonebot python -m src.memory_tool search --bot 机器人QQ --group 群号 --query 聚餐

# 手工整理，会调用模型；已有整理任务时返回 busy
docker compose exec -T nonebot python -m src.memory_tool run

# 可选安装本地官方分词数据
docker compose exec -T nonebot python -m src.memory_tool setup-tokenizer

# 离线测试：禁用真实运行存储，测试使用临时数据库和模拟接口
docker compose run --rm --no-deps -e MEMORY_ENABLED=false nonebot python -m unittest discover -s tests -v
```

分词数据保存于忽略提交的 `.run/`，校验固定 SHA256，只解析数据，不执行下载包代码。缺失或校验失败时用 UTF-8 字节保守估算，实际费用以 API 返回的 usage 为准。

运行中的 SQLite 请使用备份 API，或在停止服务后复制完整数据库及相关文件，不要仅复制正在写入的主文件。

## 重连消息恢复

独立收件箱 `nonebot/.run/inbox.sqlite3` 按机器人、群、消息编号去重。它与长期记忆分开；`RECOVERY_ENABLED=false` 可关闭，`MEMORY_ENABLED=false` 环境变量也会关闭真实运行收件箱。

重连时最多从最近 20 个已知群各补拉 100 条，只恢复最近 10 分钟内的消息。恢复过程中实时消息先落盘，再顺序处理。超过范围、接口不可用或 QQ 未同步的消息不保证补齐；过期请求不会突然回复。

已完成去重记录保留 7 天；处理被中断时可重新排队，断在网络发送中的记录标为 uncertain，不自动重发，避免盲目重复发送。这不保证网络发送恰好一次。

日常更新可直接重启或重建 NoneBot，NapCat 保持运行。已移除网页维护模式及更新准备脚本，不再需要先调用管理接口；重连恢复只提供上述有限补偿。
