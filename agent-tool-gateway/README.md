# Agent Tool Gateway

把 `Personal_AI/PolicyLayer` 适配为任务书“先别执行！——Agent 工具调用安全网关”。
固定 JSON 模拟 Agent；命令行 Client 与 HTTP Gateway 分开运行。无需真实大模型、MCP 或邮件服务。

已完成 Basic（Level 1）与确认安全进阶（Level 2）。[Level 2 验收与演示](docs/level2.md)
说明参数绑定、并发确认、过期、多用户和审计行为。

## 启动

Python 3.11+。本机已经创建 `.venv` 并安装依赖，可以直接运行下面的启动命令。
在其他机器上，先用 Python 3.11+ 创建环境并安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

启动 Server（在本目录下）：

```bash
.venv/bin/python -m server
```

另开一个终端，进入同一目录：

```bash
.venv/bin/python -m client --scenario demo
```

依次演示读取 `todo.md`、模拟邮件和越界删除。邮件步骤会显示完整参数并停下来等待输入：
只有用户输入 `approve` 才确认；其他输入均拒绝。在确认前检查 `sandbox/outbox/`，不会出现本次邮件。
确认后产生一个邮件 JSON 文件。越界删除被拒绝，不会修改沙箱外文件。

单独演示：

```bash
.venv/bin/python -m client --scenario read
.venv/bin/python -m client --scenario email
.venv/bin/python -m client --scenario escape
.venv/bin/python -m client --scenario delete
```

`delete` 会在确认后真实删除测试文件 `sandbox/notes/draft.txt`，可自行重新创建该文件。
也可以使用 `--file tool-call.json` 提交固定 JSON；相同 `call_id` 的重试返回原结果，不再次执行。

默认监听 `127.0.0.1:8081`，可用 `python -m server --port 8082` 和 Client 的 `--url` 修改端口。
默认 Client 凭据为 `local-demo-token`，仅供本机演示；自定义时，在两个终端均设置同一个
`GATEWAY_CLIENT_TOKEN`。用户身份由服务端凭据映射得到，不接受 Agent 提交的 `user_id`。
Agent 在当前项目中只是固定 JSON 的来源，不持有发起确认的能力；若后续接入独立 Agent，
必须把 Client 凭据保留在可信交互进程中，不能暴露给模型或它的执行环境。

## 结构与原项目适配

```text
client/                       固定 ToolCall、完整参数展示、用户确认/拒绝
server/__main__.py            HTTP 接口、Client 凭据、JSON 解析
server/gateway/service.py     调用去重、校验、策略、确认、执行与审计
server/gateway/policy/        来自 Personal_AI 的策略层
server/tools/registry.py      三个工具、可信 Schema、目录沙箱
sandbox/notes/                todo.md、draft.txt
sandbox/outbox/               模拟邮件文件
tests/                        策略流程、沙箱、并发和 HTTP/Client 集成测试
docs/level2.md                 Level 2 设计与手动验收步骤
users.example.json            演示用多用户配置
runtime/audit.jsonl           运行后生成的审计日志
```

实际迁移了 `Personal_AI/PolicyLayer/src/personal_ai_policy/` 的 `engine.py`、`models.py`、
`errors.py`。保留默认拒绝、显式拒绝优先、多条件匹配、请求绑定与有效期的数据模型；
操作集合适配为 `read/send/delete`。Gateway 把可信工具调用转为 `AccessRequest`，交给原策略引擎评估。
原 `PolicyEnforcer` 的字段投影适用于数据读取；这里用 `GatewayService` 接替执行边界，
增加工具校验、三态决策、待确认记录和执行结果审计。DataVault、加密及原项目的虚拟环境和配置均未迁移。

当前规则只允许三个已注册的本地工具；工具是否需要确认由服务端注册信息决定，
不接受 Agent 提交策略。策略允许表示操作具备执行资格，仍须通过该工具的确认要求。

## 接口

除 `GET /health` 外，请求需要 `Authorization: Bearer <Client 凭据>`。

### `POST /tool-calls`

```json
{
  "call_id": "call_001",
  "tool_name": "send_email",
  "arguments": {
    "to": "mentor@example.com",
    "subject": "作业链接",
    "body": "这是本次作业的文档链接。"
  }
}
```

`read_note` / `delete_note` 参数为 `{"path":"todo.md"}`，相对于 `sandbox/notes/`。
三个工具均使用 JSON Schema 检查必填字段、类型及额外字段。
工具未知、参数非法、目标越界均返回 `decision: deny`，不执行。
合法读取返回 `decision: allow`、`status: executed` 和文件内容。
邮件与合法删除返回 `decision: require_approval`、`status: pending`、`approval_id`、
工具名、完整参数和过期时间；此时不会执行工具。

### `POST /approvals/{approval_id}/decision`

```json
{"action":"approve"}
```

或 `{"action":"deny"}`。禁止在确认请求中重新指定工具和参数。
确认执行成功后返回 `status: executed` 和工具结果；拒绝后返回 `status: denied`。
合法 HTTP 请求的业务决定使用 HTTP 200；无效凭据 401、无效 JSON 400、未知路由 404。
基础设施异常返回 500 和 `status: unknown`，不能把它理解为“未执行”。

## Basic 和进阶覆盖

- 三个工具、两个接口、三种决定、Client 确认交互及三个指定演示场景已实现。
- 确认绑定 `approval_id/call_id/user_id/tool_name/argument_hash/expires_at/used`。
  参数保存为不可变 JSON 快照，读取时返回独立副本。摘要覆盖规范化后的完整参数；
  还核对原工具调用与确认 ID。默认确认有效期 300 秒，可用 `--approval-ttl` 修改。
- 过期、重复确认、换用户、修改参数/工具，以及拒绝后的确认均被拒绝。
  已过期或资源校验失败的确认进入失效终态，重复提交原调用也不会返回过时的 pending。
- 用户确认后、工具执行前，重新检查服务端策略和资源范围。
- 单进程锁将确认消费和执行串行化；同一确认的并发请求最多执行一次。
- 同一 `call_id` 绑定用户与完整调用；相同请求重放返回缓存，变更请求被拒绝。
- 路径禁止绝对路径、`..` 和符号链接；文件访问使用目录描述符与 `O_NOFOLLOW`，
  执行时重新检查，防止把已确认路径换成指向沙箱外的链接。
- 审计 JSONL 包含时间、用户、调用、工具、决定、确认 ID、用户选择和执行结果。
  执行前记录开始事件，执行后记录结果，每次写入 flush + fsync。
  未通过鉴权、JSON 解析或路由检查的 POST 请求也记录拒绝；日志不记录 Client 凭据。
  无法解析的请求没有可可信提取的调用 ID，相关字段为 null。审计不可写时不会伪报成功。
- MCP 与恶意网页注入演示尚未实现。

调用和确认保存在内存里，服务重启后失效；审计日志和模拟邮件文件保存在磁盘上。
当前不提供跨进程、跨重启的 exactly-once 保证，也没有多工作进程模式。
一次执行后的审计失败会保留内存中的 `unknown` 结果并阻止该调用再次执行。
生产环境需要持久化状态、事务和幂等的外部服务，不能仅靠内存锁。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

当前共 49 项测试；只跑 Level 2 的 22 项验收：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_level2.py' -v
```

测试使用临时沙箱，不修改自带的测试文件。包含只读执行、确认前无副作用、确认后执行一次、
拒绝、非法工具和参数、路径穿越及符号链接、过期/改参数/换用户/重复确认、并发确认、
审计失败和 HTTP/Client 端到端流程。

## 题目要求的说明

1. **Client 和 Gateway 分别负责什么？** Client 负责展示和收集真实用户选择；
   Gateway 负责可信定义、校验、决策、确认状态、实际执行和审计。
2. **为什么 Agent 输出不等于授权？** 输出只是模型的建议，可能误解意图或受到外部内容影响。
   高风险操作必须经过独立策略和实际用户交互。
3. **为什么绑定完整参数？** 批准给某人发某段内容，不代表批准同一工具改为发送另一段内容给另一人。
4. **为什么策略放在 Gateway？** 若由 Agent 自报风险，模型就能把删除或发送标成安全并绕过确认。
5. **执行超时能否重试？** 超时只说明 Client 没拿到结果，工具可能已经执行。
   当前进程存活时，用原 `call_id` 重放以取回缓存结果，不能换 ID 盲目重试。
   如果结果为 `unknown` 或服务已重启，应核对审计与邮件文件；不得直接推断未执行。
   接入真实邮件等外部服务时，还需服务支持幂等键和结果查询，否则无法保证安全重试。
