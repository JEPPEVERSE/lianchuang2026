# AI 学习辅助系统 · Level 1 / Level 2

## Level 2：真实 Markdown、图检索与引用问答

Python 3.9+，仅使用标准库。SQLite 保存课程原文、知识网络、题目/题型和问答快照。
两次模型角色分别负责分析与回答，默认使用 DeepSeek；检索使用词法 BM25、别名与一跳图扩展。

### 配置模型

在本项目目录使用 `.env`，如尚不存在可从 `.env.example` 复制：

```bash
cd "AI learning system"
cp .env.example .env
```

仅在新配置时执行复制，避免覆盖已有密钥。编辑 `.env`：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=填写你的密钥
```

也接受 `DEEPSEEK_API_KEY`。环境变量优先于 .env。密钥不保存到数据库，.env 已被 Git 忽略。
当前模型名依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/)；
使用 [JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 并在本地进一步校验字段与引用。
服务需支持 `/chat/completions`；若其他兼容服务不支持 JSON mode，可设置 `LLM_JSON_MODE=0`，
仍会要求 JSON 文本并执行本地校验。HTTP 错误仅显示状态码，不输出可能含敏感内容的响应正文。

### 小规模试跑与完整导入

```bash
python3 -m learning_system ingest data/linear_algebra/resources --kind resource --limit 2
python3 -m learning_system ingest data/linear_algebra/resources --kind resource
python3 -m learning_system ingest data/linear_algebra/problems --kind problem
python3 -m learning_system point "矩阵的秩"
python3 -m learning_system graph
python3 -m learning_system plan
python3 -m learning_system search "为什么满列秩能保证最小二乘系数唯一？"
python3 -m learning_system ask "为什么满列秩能保证最小二乘系数唯一？"
python3 -m learning_system ask "给出随机化 SVD 的完整实现及误差界" --json
python3 -m learning_system history
```

资料集共 20 份讲义和 40 道模拟题。题型会由模型提取，输入目录必须区分 resource/problem。
不要导入 solutions/ 或 evaluation_queries.json。
首次导入会把对应正文发送到配置的模型服务并产生 API 费用；建议先小规模检查结果。
重复导入同路径、同内容会跳过，不产生分析调用。

默认对 DeepSeek 显式设置 `LLM_THINKING=disabled`，避免默认思考耗尽输出预算。
分析与问答分别使用 `LLM_ANALYSIS_MAX_TOKENS=8192`、`LLM_QA_MAX_TOKENS=2048`；
角色配置优先于旧 `LLM_MAX_TOKENS`。`LLM_ANALYSIS_BATCH_CHARS=3000` 控制每批原文长度。
stderr 的 `[model]` 统计显示耗时、输入/输出 token、思考 token（服务提供时）与停止原因，
不包含密钥、原文或思考正文。截断时不按相同预算重复请求；合法 JSON 的校验错误仍允许一次纠正。

默认数据库位于 `runtime/learning.sqlite3`，可在命令前后加 `--db 路径` 指定独立数据库。
ingest 每行输出一份文件的导入结果；其他查询默认输出 JSON，ask 默认输出可读答案与原文引用。
命令失败返回非零退出码。目录按文件逐份发布；失败文件回滚，之前成功的文件仍保留。
修改已入库文件会拒绝覆盖；如需重新实验，可指定新的数据库，不必删除原数据。

### 测试与设计说明

```bash
python3 -m unittest discover -s tests -v
```

测试无需 DeepSeek 密钥，使用明确的模拟模型输出；HTTP 集成测试会启动本机临时服务。
包括重复导入、增量整合、别名归一、来源引用、题型、循环回滚、资料不足和进程重启恢复。
它们验证系统机制；实际模型的语义提取、证据充分性和数学正确性仍需另行评估。
如运行环境不允许本机监听，可以只运行 test_level2.py 与 test_network.py。

详细工件、成本与边界见 [Level 2 设计说明](docs/level2_design.md)。
本版本没有 embedding 或 PPR，不是完整 HippoRAG；尚不更新学生掌握度。

## Level 1：模拟分析结果

Python 3.10+，仅使用标准库，不需要安装依赖或配置模型 API。

本阶段假设 AI 已完成资料/真题分析，以 JSON 模拟 `AnalyzeReq` 输入。
资料路径仅用于来源关联，不会读取对应 Markdown 文件。示例中的路径是模拟标识。
从 Markdown 原文提取知识、语义同义合并和可靠问答留到 Level 2。

## 启动与演示

从仓库根目录进入本项目：

```bash
cd "AI learning system"
python3 -m learning_system examples/course.json
python3 -m learning_system examples/course.json examples/extra.json
python3 -m learning_system examples/course.json --json
python3 -m unittest discover -s tests -v
```

第一条命令展示全部节点、前置关系、资料/真题关联及学习顺序：
`矩阵与矩阵运算 → 初等行变换 → 矩阵的秩 → 线性方程组`。
第二条命令在已有网络中整合逆矩阵资料；不重复创建矩阵的秩节点。
第三条命令输出机器可读的完整图、学习计划与考频。
输入不合法时 CLI 返回非零退出码和错误原因。

## 输入格式

每个文件是分析结果数组。资料和真题可以混合输入。

```json
[
  {
    "type": "resource",
    "resource": {"id": "res1", "path": "课程.md"},
    "points": [{"id": "a", "name": "基础"}, {"id": "b", "name": "进阶"}],
    "edges": [{"from": "a", "to": "b"}]
  },
  {
    "type": "problem",
    "problem": {"id": "q1", "name": "第一题", "content": "题目正文"},
    "points": [{"id": "b", "name": "进阶"}]
  }
]
```

`from → to` 表示学 `to` 之前必须学 `from`。
关系类型分为知识点 `prerequisite_of`、资料 `covers`、真题 `examines`。
三类 ID 建议使用不同命名空间（如 `res_`、`q_`），关系端点类型也可由关系类型确定。
字段严格校验，不接受缺失或额外字段。ID 与名称必须是非空字符串。
同一知识点 ID 的名称必须一致，不会自动猜测两个名称是否为同一概念。

## 接口与要求对应

```python
from learning_system import SimulationNetwork

network = SimulationNetwork()
network.integrate(analysis)        # 一次分析结果
network.integrate_many(analyses)   # 一个批次，支持批次内前向引用
graph = network.export_graph()    # 所有节点和全部关系
plan = network.make_plan()        # 每项包含 point 与空 actions 列表
links = network.query_point("b")  # 前置知识、资料、真题 ID
ranking = network.point_rank(10)  # 按涉及的不同真题数量排序
```

`integrate` / `make_plan` 对应任务书必做接口；`query_point` 提前实现 Level 2
需要的关联查询；`point_rank` 为辅助功能。知识点资料关联和真题关联保存完整。

## 核心设计与边界

- 按稳定 ID 合并节点，用集合去重边和关联。重复输入不会重复计数。
- 整合是追加式的：同一资料/真题新增知识关联会合并，缺失旧关联不意味着删除。
  同 ID 的资料路径、题目内容或知识点名称发生冲突时拒绝；替换/删除与版本更新尚未实现。
- 在候选副本中整合并校验，成功后才发布新状态。单次整合或批次失败均不修改已有网络。
- 前置关系使用 Kahn 拓扑排序。每个知识点恰好出现一次，包括孤立点和不连通分量。
  同时可学习的节点按 ID 的 Python 字符串顺序排列，以保证结果稳定。
- 未知边端点、自环和循环依赖均拒绝。错误中的 blocked points 可能包含环的下游节点，
  不代表每个被阻塞点都处于环内。
- 计划的动作列表为空，满足 Level 1 范围。没有模型调用、学习状态更新或动态计划。
- 网络在进程内维护；CLI 通过输入 JSON 重建网络。导出图是展示快照，尚无快照恢复接口。
  学习状态持久化属于后续等级。
- API 返回脱离内部状态的快照，调用方修改返回值不会篡改网络。

## 验证

测试覆盖增量整合、跨资料节点复用、真题考频去重、完整前置顺序、菱形依赖、
孤立点、不连通图、空图、循环/自环拒绝、未知引用、冲突定义、失败回滚和快照隔离。

本阶段使用独立轻量模块，不依赖旧 susu-agent 的五角色教学流程，后续可以通过适配器
将模型结构化分析结果接入 `integrate`，并复用其模型配置、检索与学生状态仓库。
