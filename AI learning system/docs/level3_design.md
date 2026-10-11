# Level 3 实施与验收

## 运行

推荐 Python 3.12。项目根目录创建 `.venv`，安装 `requirements.txt`，配置 `.env`。
启动：`.venv/bin/python -m learning_system.web`，浏览器打开 http://127.0.0.1:8765。
macOS 可双击 `start_web.command`。默认数据库为项目 `runtime/learning.sqlite3`，原 Level 2 数据不迁移覆盖。

Web 含学习工作台、资料与检索、学习档案、知识网络。先导入教学资料与练习题，再构建索引。
“导入内置线性代数”按照所选文件类型分别导入 resources/ 与 problems/，不导入 solutions/。
构建首次调用 DeepSeek 抽取事实并下载 BGE，后续复用缓存。服务仅监听本机。
首次下载如 Hugging Face 超时，可显式配置 `HF_ENDPOINT=https://hf-mirror.com`；该配置只用于模型下载。
当前默认 BGE 固定版本 `7999e1d3359715c523056ef9478215996d62a620`，CPU 编码。
更换模型须同时设置模型版本并重建索引，不能混用向量。密钥仅通过本地 `.env` 加载，不进入页面。

## HippoRAG 2 核心与项目适配

参考 https://arxiv.org/abs/2502.14802 与 https://github.com/OSU-NLP-Group/HippoRAG 。
本项目实现核心流程，不是官方包包装或论文实验的逐项复现：

1. 逐段 OpenIE 抽取 subject/relation/object/quote，要求 quote 原文连续存在。
2. 实体、三元组、段落使用真实 BGE embedding，长内容按 tokenizer 窗口全部覆盖。
3. 检索图含事实关系、同义连接、段落包含关系；与学习前置有向无环图独立。
4. 问题匹配三元组与段落，LLM recognition 筛选候选三元组 ID。
5. 实体与段落共同作为 PPR 起点，段落分数形成排序；无选中三元组时使用 dense 结果。
6. 项目额外用 RRF 融合 BM25 排名。QA 仍只依据原文快照回答并校验引用。

项目适配包括数学条件保留、来源摘录校验、token 多窗口取最高匹配、每实体最多五个候选同义邻居、
保守同义阈值0.9、启发式实体频率权重，以及 BM25 融合。参数需通过中文数学检索评估调整。
同义相似不等于实体身份，不自动合并；实体只按规范名称与课程别名精确映射。
索引只覆盖 resource。题目供 planner/practice 工具查询，不作为课程定理的证据。

SQLite 中保存事实抽取与向量缓存。已发布索引包含原文快照、图与向量，事务切换活动版本。
资料变更会使索引过期，拒绝混合查询；构建失败保留旧发布版本与已完成缓存。
原 Level 2 仍不支持修改已有路径文件；上传新版本使用内容寻址路径，会增加一份资料而非替换。
资料删除、替换和旧索引清理暂不提供，以保留来源审计。

## 学习闭环

`LearningRecord` 先持久化；模型失败仍保留待处理证据，重复提交相同 ID/内容不重复记录。
记录类型：question、attempt、understood、confused。作答必须关联真实题目，且知识点必须与题目匹配。
`StudentModel` 包含每知识点掌握值（允许未知）、置信度、依据类型、薄弱情况与证据记录 ID。
自述理解不能冒充已验证作答；self_report 置信度上限0.5。掌握值是启发式估计，不是校准概率。
updater 只处理未处理记录，每批最多10条/12000正文字符，保存更新决策，并原子标记处理完成。

planner 接收学生模型、当前状态、已有计划和课程索引，通过受限的 point/search/problems/records 工具按需获取信息。
最多3次规划轮次，每轮最多3个工具请求；最终只能发布校验通过的 read/review/practice/assess 动作。
阅读必须引用关联资料；练习必须引用关联题目；共同出现的知识点遵循已有前置顺序。
工具名称和所有 ID 都由后端校验，模型不能执行任意函数、访问任意路径或修改数据库。

初次目标、目标变化、关键反馈、当前动作完成、用户要求检查触发规划。
每5轮普通问答进行一次便宜的程序检查；无新的规划事件则不强制调用模型。
普通问题先累计；重要反馈会触发 updater，planner 使用已评估状态；结束session汇总待评估记录。
目标与动作由 planner 设置，当前问题/反馈/执行状态由后端及时更新。
已完成动作保留，新计划有递增版本；决策及输入快照可回看。完成练习需先提交对应作答。
一个学生同时只允许一个开放session。未知掌握不当作零分，前置图用于规划约束，不由PPR决定学习顺序。

## Web 与成本

标准库 HTTP 服务与原生 JS/CSS，无 Node 构建步骤。模型工作在单写入任务队列中执行，页面轮询显示阶段与耗时。
页面刷新可继续跟踪同一服务进程的任务；服务重启后未完成任务需重新发起，缓存与学习记录仍保留。
只接受本机 Host、同源 JSON 写请求；原文和模型文本通过 textContent 展示，禁止当成 HTML 执行。
不提供公网部署/账户系统；不能直接把本地端口暴露到互联网。

BGE 模型复用，向量、OpenIE、三元组筛选结果分别缓存。索引不会每轮重建。
普通已规划问答通常仅 recognition+QA 两次调用；相同问题可复用 recognition。
重要反馈额外调用 updater/planner，planner 按需查工具可能增加调用。
每个任务统计实际调用数、输入/输出token与耗时；stderr同时记录角色统计。

## 验收

1. 导入资料与题目，在资料页看到真实文件与题目计数。
2. 构建索引，在检索页查看候选/保留三元组数量、PPR迭代与原文段落。
3. 以“掌握矩阵的秩”为目标开始session，得到引用真实资料的动作。
4. 提交“我分不清秩与行数”的反馈，档案中出现低掌握估计与证据，新计划安排概念复习。
5. 提交真实题目的作答，检查状态和计划变化，完成当前动作后再次规划。
6. 重启恢复状态，查看历史决策，拒绝未知ID、错误工具、过期版本与无作答的练习完成操作。

测试：`.venv/bin/python -m unittest discover -s tests -v`。
检索对比：`.venv/bin/python -m learning_system.evaluate --limit 12`，在线模式会向 DeepSeek 发送评估问题和候选三元组。
本地对比：`.venv/bin/python -m learning_system.evaluate --offline --limit 12`，仅 BM25 与 dense，不调用模型服务。
评价使用 expected_file_coverage 章节覆盖代理指标，不能宣称回答正确率或人工标注Recall。
测试 fixture 验证协议与算法机制，不代表真实模型质量；实际 DeepSeek 测试单独记录。

## 已知限制

真实摘录不证明三元组与回答逻辑正确；数学条件、实体同义关系和掌握估计仍可能被模型判断错。
采用官方思路自行实现，并未直接使用官方代码；不同encoder与适配权重使论文参数不能照搬。
同义连接、PPR与额外筛选不保证优于BM25，需要以本项目问题集比较。
题库没有导入时不能制定真实练习动作，planner只能安排现有资料支持的阅读/复习。
主动生成新检测题、可靠自动判题、稳定错误诊断和大规模ANN索引属于后续扩展。

## 本次真实验证

41 项单元/本地 HTTP 集成测试通过，模型与向量 fixture 均为测试专用。
最初小规模验证：BGE 固定版本真实运行，21 个段落构建 148 条三元组、245 个实体、436 条检索边。
独立数据库 runtime/level3_smoke.sqlite3 使用真实 DeepSeek 验证：矩阵秩问答为 answered，
PPR 迭代 21 次、保留 7 条三元组；困惑后掌握估计 0.15（self_report），作答后 0.75（answer_evidence），
计划依次从 V1 更新至 V2、V3。测试反馈不写入正式学生档案。
完整证据见 runtime/level3_real_validation.json。12 个问题完成本地 BM25/dense 对比，
输出 runtime/retrieval_evaluation_offline.json；现有资料只覆盖部分章节，不据此宣称召回提升。
完整问题集在线 HippoRAG 对比未执行：自动审批拒绝了评估问题外发，已改为本地评估。
正式数据库题库已导入第一份原创练习题，共 8 道，其余题库尚未导入。
2026-10-11 已完成全部 20 份教学资料入库：142 个段落、149 个知识点。
新 HippoRAG 索引 index_94d6d80d610cac5ca109b788 覆盖全部 142 个段落，
包含 896 条三元组、1390 个实体、2826 条边，状态 ready。
导入报告为 runtime/full_resources_import.json，数据库备份为 runtime/before_full_resources_import.sqlite3。
一次字段缺失的事实抽取被校验拦截，随后复用缓存重试成功，未绕过验证。
旧前置图中三条把秩的应用当作秩定义前置的关系经检查反向修正；原数据库备份与变更说明
位于 runtime/before_prerequisite_review.sqlite3 和 runtime/prerequisite_review.json。

## 模拟演示学生

`demo-simulated` 为独立模拟学生，原 `local` 学生保留。38 条记录明确标注模拟，
通过真实 DeepSeek 问答、updater 与 planner 运行五阶段闭环，覆盖 9 个知识点，计划更新至 V6。
不是模型训练或真实用户效果实验；重复同题只能说明当前练习的表现，不能推断泛化能力。
正交投影和特征值仅有自述反馈，最终掌握值仍为未知；参数方程组保留为薄弱项。
报告 `runtime/demo_simulation.json` 保存五阶段完整学生状态和计划快照，
备份 `runtime/before_demo_simulation.sqlite3` 保留模拟前数据，`runtime/demo_ready.sqlite3` 保存可演示终态。
该模拟用于历史验证，现已按用户要求归档清理；当前使用 V2 空白档案。
可复用脚本：从项目根目录运行 `PYTHONPATH=. .venv/bin/python scripts/seed_demo.py`，
重跑会跳过已保存记录和已完成阶段，不直接修改掌握值。需要已配置模型服务和现有题库。
演示建议：先看学习档案，再看计划与历史决策，最后继续提交 LA-Q006 参数分类的正确作答，观察状态和计划变化。

## 工件 V2 与空白档案

工件升级至 schema_version=2。artifacts.py 提供兼容读取和动作生命周期管理，
level3_store.py 使用 app_settings 记录幂等迁移版本与当前学生。旧记录来源无法确认时记为 unknown，
旧掌握摘要保留，但不凭空生成作答表现、独立完成记录或已解决误解。
学习记录增加 origin、assistance、attempt_no；origin 由调用端内部确定，Web 用户不能通过输入覆盖。
模拟脚本明确传入 simulated。assistance 是用户声明而非程序验证的独立作答证明。
student_model 将 self_report 与 performance 分开，保留原 mastery/basis/confidence 摘要；
自述不能覆盖已有作答估计。performance 明确标记 model 评估来源，正确性仍需人工核对。
misconceptions 保存活跃/已解决状态与两类证据；解决必须有匹配的纠正作答。
evidence_summary 按数据库记录统计次数、不同题目数和声明独立次数。
updater 对每个本轮知识点必须更新或明确 deferred_points；延期原因持久化并进入审计，
记录被处理不等于掌握被确认。新证据到达后可以重新评估延期知识点。

计划有 goal_id，动作有 goal_id、completion_criteria、completion_record_ids、evaluation_status。
相同目标/动作类型/知识点/资料与题目集合的待执行动作重规划后保留ID；被替换动作归档。
改目标会生成新goal_id，旧动作归档，不混入新目标。完成动作只表示执行完成，答错仍可完成练习；
表现正确与否由 evaluation_status 单独表示。目标完成采用证据检查，不使用分数阈值自动宣告完成。
teaching_state.current_issue 绑定知识点、问题、诊断与记录；轮次和 needs_plan 仍是程序调度字段。

检索增加 query_point_ids（名称/别名明确命中）、evidence_point_ids（返回原文关联）、
真正扩展的 expanded_point_ids 和逐项 scores。无法明确识别问题知识点时允许空列表，
不会把所有相关知识点自动当作提问主题。该问题仍通过 qa_sessions 归属当前session并保存问答。
旧向量和事实索引继续复用，无需重建。

按用户要求，原 demo-simulated 的38条记录、11条决策和3条独占问答已从正式库移除，
相关演示报告和恢复快照移到 runtime/archive/。操作前完整备份也在该目录。
当前选中新建的空白学生，没有session、学习记录或掌握估计；原local真实历史保留。
详见 runtime/profile_reset.json。后续 Web 创建session会使用这个新学生。
脚本 scripts/reset_learning_profile.py 会备份、清理指定演示学生并选择新的空白档案。
真实 V2 模型验证在独立 /tmp 数据库运行，没有向正式档案添加测试记录。
