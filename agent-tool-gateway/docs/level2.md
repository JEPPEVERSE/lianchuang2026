# Level 2：确认不能只看起来像确认

在 Level 1 的调用流程上，落实精确绑定、一次性与并发、沙箱和审计。
仍然使用模拟 Agent 和模拟邮件；MCP、恶意网页注入属于后续 Challenge。

## 确认的权限边界

`Approval` 是冻结的数据类，包含 `approval_id`、`call_id`、`user_id`、`tool_name`、
`arguments_json`、`argument_hash`、`expires_at`、`used`、`rejected` 和 `invalidated`。
完整参数保存为不可变字符串，通过 `arguments` 访问时生成新的对象，避免外部修改污染快照。

规范化采用 JSON 对象键排序、固定分隔符、UTF-8，拒绝非 JSON 数值；对完整参数计算 SHA-256。
键的排列顺序不会改变摘要，参数的实际值发生变化则会改变摘要。字符串原样保留，不做模糊匹配。
确认时再次核对原调用摘要、用户、工具、确认 ID 和参数摘要。HTTP 确认接口只接受 `action`，
不接受新的 `tool_name`、`arguments` 或自报 `user_id`。

用户身份从服务端的 Client token 配置得到。模型提供的调用 JSON 不能指定自身权限或确认用户。
真实 Agent 接入时，Client token 必须留在可信 Client 中，不能提供给模型或不可信工具进程。

## 一次性与状态变化

```text
pending ── 用户确认、重新校验通过 ──→ used ──→ executed / failed / unknown
        ├─ 用户拒绝 ──────────────→ rejected
        └─ 过期、策略撤销、资源失效 ─→ invalidated
```

确认校验、消费和执行在同一个进程锁内串行完成；工具执行前先写审计，再将确认标记为已消费。
执行中的基础设施失败不会让确认恢复成 pending。用户拒绝、确认过期或资源失效后也不能再批准。
错误用户和格式非法的确认请求只被拒绝，不会破坏原用户尚未使用的确认。
确认时还会重新评估服务端策略，防止等待确认期间策略撤销却仍然执行。

同一个调用 ID 再次提交时返回缓存结果，避免 Client 超时后重复执行。
确认接口的重复批准返回拒绝；重放原 ToolCall 则可取得已有执行结果。这是两种不同的操作。

确认过期在提交或确认时惰性检查，无需后台定时任务。默认有效期 300 秒，过期边界为 `now >= expires_at`。
终态写入后，即使测试时钟倒退，也不会把已失效的确认恢复为可用。

## 沙箱与审计

读和删只能操作 `sandbox/notes/` 中的普通文件。绝对路径、空路径段、`.`、`..`、反斜杠及空字节被拒绝。
先用 `O_NOFOLLOW` 打开沙箱根目录，再通过目录描述符逐层打开子目录和文件，阻止根目录、子目录、
文件符号链接绕过限制。用户确认后重新检查资源，防止等待期间路径被换成符号链接。

每个审计事件包含时间、用户、调用 ID、工具名、Gateway 决定、确认 ID、用户操作及执行结果。
执行开始和执行结束分别记录；拒绝、重复确认和调用重放也记录。
HTTP 层的无效凭据、非法 JSON 与未知 POST 路由均记录拒绝，凭据本身不写入日志。
无法解析的调用 ID 和工具名为 null；没有执行的事件，其 execution_result 为 null。
每次追加后 flush + fsync；日志无法写入时返回基础设施错误，不能伪装为成功。

## 演示确认过期

先停止原 Server，再在项目目录下启动短有效期版本：

```bash
.venv/bin/python -m server --approval-ttl 3
```

另一个终端执行：

```bash
.venv/bin/python -m client --scenario email
```

参数展示后等待超过 3 秒再输入 `approve`，结果应为 deny，原因是确认过期，本次不会创建邮件文件。
再发起一次新调用并在有效期内确认，即可执行。不能通过重用旧 call_id 延长确认有效期。

## 演示用户绑定和重复确认

先停止原 Server，准备服务端用户配置并启动：

```bash
cp users.example.json users.local.json
.venv/bin/python -m server --users-file users.local.json
```

示例中 Alice 与 Bob 使用不同的演示 token；`users.local.json` 已被 Git 忽略。
在另一个终端提交 Alice 的调用：

```bash
curl -s http://127.0.0.1:8081/tool-calls \
  -H 'Authorization: Bearer alice-demo-token' \
  -H 'Content-Type: application/json' \
  --data '{"call_id":"alice-mail-1","tool_name":"send_email","arguments":{"to":"mentor@example.com","subject":"演示","body":"Alice 确认的内容"}}'
```

将返回的真实 `approval_id` 填入变量，然后以 Bob 身份尝试确认：

```bash
APPROVAL_ID='填入刚才返回的 approval_id'
curl -s "http://127.0.0.1:8081/approvals/$APPROVAL_ID/decision" \
  -H 'Authorization: Bearer bob-demo-token' \
  -H 'Content-Type: application/json' --data '{"action":"approve"}'
```

应返回拒绝。由用户检查上面的收件人、标题和正文后，换成 `alice-demo-token` 提交确认才可执行。
再次批准同一个确认应返回拒绝。重新演示时使用新的 call_id。
若向确认请求添加新的工具、参数或 user_id，也会被拒绝。

## 验收测试对应关系

```bash
.venv/bin/python -m unittest discover -s tests -v
```

| 要求 | 覆盖的验证 |
| --- | --- |
| 参数与用户精确绑定 | 不可变快照、参数键顺序、工具和调用 ID 篡改、跨用户 HTTP 确认 |
| 一次性与并发 | 两线程确认、8 个 HTTP 并发确认、确认与拒绝竞争、拒绝后重试 |
| 过期 | 刚好过期拒绝、临近过期可执行、过期终态和 HTTP 重放 |
| 沙箱 | 绝对路径、目录穿越、文件/目录/根符号链接、确认期间替换路径 |
| 审计 | 决策、用户选择、执行结果、拒绝请求、审计失败、日志不含凭据 |
| Client | 展示完整参数、默认拒绝、端到端确认流程 |

全量 49 项测试，包含 Level 1 的 27 项回归和 Level 2 的 22 项验收。
测试使用临时目录、虚拟时钟和本机临时端口，不修改项目自带的沙箱文件。

## 当前实现范围

符合单进程演示场景。确认与调用状态仍保存在内存中，服务重启后不可继续使用旧确认。
锁不覆盖多进程，不能启动多个 Gateway 实例共享同一沙箱并声称实现跨实例去重。
本地审计和模拟邮件保存到磁盘，但二者没有跨文件事务；意外退出可能留下开始记录而没有结束记录。
这种情况下结果应按未知处理，人工核对日志与邮件文件，不应换一个调用 ID 自动重试。
跨重启的确认状态、数据库事务和真实外部服务的幂等键可以作为后续扩展。
