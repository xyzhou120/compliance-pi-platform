# 独立工程首轮验证记录

2026-10-28 后续改动：工作台访问令牌与公开 API `X-Inspection-Token` 校验已移除。下文浏览器33项验收记录属于移除前版本，其中连接、401条目不再适用；本次改动的独立测试和浏览器复验结果见文末续记。内部 Pi 接口凭证仍保留。

本工程位于 `compliance-pi-platform`，有独立 Git 仓库、虚拟环境、Node lockfile、规则资产和测试。运行代码不导入旧服务，也不使用旧服务数据库。当前范围为 `stop_work_unload` 首个检查项。

## 验证结果

| 验证 | 结果 | 证明范围 |
| --- | --- | --- |
| `.venv` Python unittest discovery | 187 项通过 | 含新增 9 项前端 API 回归；规则、候选、来源、租约/fence、outbox、本机完整链路 |
| Pi worker `npm test` | 12 项通过 | 真实 Pi SDK 1.4.0、本机 OpenAI-compatible 协议、并行工具、全局限额、预算、取消、幂等、错误隔离 |
| 离线包脚本测试 | 2 项通过 | 正常 SHA256 包、篡改及路径越界拒绝 |
| Python compileall / Node syntax | 通过 | Python/Node 语法与入口 |
| `pip check` | 通过 | 当前隔离环境安装的依赖一致性 |
| Compose config / git diff check | 通过 | 配置展开和差异格式 |
| 前端 Node 辅助测试 | 4 项通过 | 转义、独立状态标签、定位、分页文案 |
| 真实 Chromium 界面检查 | 33 个断言通过 | 18192 独立应用；16 项常规交互、17 项命题/PDF/异常/并发响应检查 |
| wheel 构建与逐字节资产校验 | 通过 | frontend 的 HTML/CSS/JS、规则 JSON 与契约 schema 完整；不等于镜像构建 |

本轮重新运行测试，未沿用 178 项历史数字。首次冷启动中 Python full_flow 的本机服务未在 10 秒内就绪、Pi 首个 SDK 测试达到 deadline；未修改其实现或放宽超时，完整复跑分别为 187/187（31.977 秒）、12/12（2.78 秒）。首次失败保存在 `.local/verification/python.log`、`pi.log`，成功复跑为 `python-retry.log`、`pi-retry.log`。这是测试启动时序风险，不能抹去首次失败。

复现常规回归：

```powershell
.\.venv\Scripts\python.exe scripts/verify.py
node --test tests/frontend-unit.test.mjs
node --check frontend/app.js
.\.venv\Scripts\python.exe -m pip check
docker compose --env-file .env.example -f infra/docker/compose.offline.yml config --quiet
```

`scripts/verify.py` 输出 `.local/verification/report.json`，它保留首次失败；本轮汇总另见 `regression-summary.json`，不能仅看一份历史汇总判断现状。

最终前端资产修改后再次构建 wheel，证据为 `.local/verification/wheel-assets-final.json`、`wheel-build-final.log`。可用 `.\.venv\Scripts\python.exe .local/verification/refresh_wheel.py` 复现：从范围受控的源码副本执行隔离 `pip wheel --no-deps`，逐字节核对归档资产；不会打包 `.env.local`、数据库、`.idea` 或 node_modules。当前 wheel 共49个文件。

## 前端浏览器验收与修复

使用真实 Chromium/Playwright CLI 和 `.local/ui-preview/preview_app.py`，显式 PI_ENABLED=false、独立 SQLite、独立18192端口；没有加载 `.env.local`，没有向真实模型、OCR、OceanBase 或回调发请求。用户8092未停止或覆盖，`.idea` 未修改。

- 连接令牌、错误令牌文案、会话保存、X-Inspection-Task受理标识、选中行、列表刷新、20条分页/上下页、空筛选、任务提交、暂停/恢复、增量阶段进度。
- TXT 原件鉴权预览/下载、PDF Blob iframe 原件预览、PDF 单页 PNG；候选空状态、中文检索、单条完整证据、命题引用和导航引用直达、冻结规则计划。
- 合成运行通过真实 `run_once`、原生解析、确定性评估和 `Store.finalize` 产生 `pending`/`unverifiable`。`failed` 展示、401和服务不可达使用浏览器路由故障注入，不宣称实际生产故障演练。
- 延迟首条证据响应后点击第二条，验证迟到响应不覆盖新选择；隐藏页面轮询停止、可见后恢复以合成 visibilitychange 和 document.hidden 状态验证，尚非物理设备/操作系统后台调度证明。
- 桌面1440×1000、移动390×844；截图检查规则、运行、证据布局，修复前规则页scrollWidth=513，修复后390，无页面横向溢出。表格可在内部横向滚动。没有声称完整 WCAG 审计或真手机触摸测试。

修复文件：`frontend/app.js` 统一所有API的401重连，切换run清空旧详情/引用/预览，对证据详情和预览加入请求序号以拒绝迟到响应；`frontend/styles.css` 允许窄屏区段头换行、长版本ID和范围ID折行。沿用原有状态与布局，不新增依赖。

浏览器步骤/日志：`.local/verification/browser-flow.js`、`browser-flow.log`、`browser-edge-cases.js`、`browser-edge-cases.log`。本轮 CLI 由既有 npm cache 调用，无新增前端依赖；先启动18192，再用 `npx.cmd --yes --package @playwright/cli playwright-cli -s=compliance-ui open http://127.0.0.1:18192 --headed`，随后执行 `run-code --filename .local/verification/browser-flow.js`、`run-code --filename .local/verification/browser-edge-cases.js`。这些文件是本机验收产物，不作为生产运行依赖。

截图位于同目录 `desktop-runs.png`、`desktop-evidence.png`、`mobile-rules.png`、`mobile-evidence.png`、`pdf-page.png`。视觉验收依据 PRODUCT.md/DESIGN.md，无外部参考图；最终窄屏规则及证据截图已人工读取检查。没有外部资源请求，故障注入外无浏览器JS异常。

端到端测试使用真实 FastAPI、控制 worker、真实 Pi SDK Node HTTP 服务、本机模拟 Qwen 的 OpenAI-compatible SSE、冻结 TXT 文件和本机 HTTP callback。模型并行查询证据后提交一个可由原生原文独立解析的事实候选，Python 结果为 `pending`，随后 callback 返回 `code=200/success=true`，outbox 记为 delivered。此测试没有调用私有模型/OCR、真实 OceanBase 或生产上游。

另有真实 PDF 页面渲染为 PNG、外部 OCR 本机协议、缓存复用和 stale fence 拒绝的测试。OCR 输出始终作为 model candidate 保存；它不会因为返回 JSON 或文字相同就变成真实执行事实。

## 已修复的集成缺陷

- 控制 worker 续租、Pi 实际 HTTP 调用、失败缺口、完整 source/candidate/evidence 合并、最终事务和 outbox 已接通；持续 worker 不会处理一项就退出。
- Pi 默认并行能力与宿主并发池分别保留；候选提交工具不会强制同批其它读取/OCR 工具串行。
- 模型自报 quote/hash/verified 不作为事实；引用 ID 由服务器解析到被冻结原文，语义事实须由确定性解析器独立验证。
- 同字节附件保留各自身份但不制造独立证据；来源角色冲突变成缺口。
- 原生解析缓存与 OCR 成功缓存有内容哈希/配置版本；失败不会固化为成功缓存。
- 文件 report_date/source_metadata 保留，防止版本冲突门失效。
- 接入 OceanBase 前要求明确资料 cadence，月度记录在 SQL 中限界、查询行数有预算，年度要求保留版本供适用校验。
- 业务 API、worker、Pi 返回统一 planRevision；已受理规则/执行器发生变化时拒绝以新代码静默恢复旧运行。
- 定版事务验证 run、snapshot、已登记证据、pass 的完整性与命题、no_pass 的反证引用。
- 回调 DTO 仅输出上游原始附件 URL，不把本机路径或 file_id 冒充附件地址；无明确附件问题时省略 attachmentContent。
- Docker 安装 PDF 渲染依赖；API/worker 注入同一回调策略；Compose 在 API/worker 启动前完成新库初始化。

## 尚未证明的能力

- PostgreSQL 真实运行、多副本并发和生产库升级迁移；SQLite 测试不替代这些验证。
- Linux 容器构建/导入/实际运行：本机 Docker CLI 可用但 Linux daemon 不可用。
- 约 100 份真实材料、真实私有 Qwen/OCR 的完整覆盖、业务准确率与 30 分钟目标。
- 正式 OceanBase 表结构/只读视图、任务生产方契约、回调实收与对端幂等。
- OCR/自由文本候选的全面语义升级：无法独立验证的字段保持候选，生产事实发布仍需通过经审定的质量策略。
- 第二检查项、向量/OpenSearch 适配器和专家盲测。首版交互工作台已经完成本机合成资料验收，真实业务可用性仍随真实模型和资料联调验收。

## 下一阶段明确入口

1. 固定真实矿井/周期、约100份同批输入及字节哈希、规则planRevision、Qwen/OCR/VLM服务和模型profile、提示/schema版本、算力硬件与并发配置；保持旧服务为只读差分参考。当前仓库中的演示样例不能替代这批资料。
2. 真实Pi与OCR联通后，验证引用绑定、不可读/冲突语义、限流、超时和重试，再进行冷/热缓存及并发1/2/4对照。`python -m scripts.benchmark --mine <mine> --cycle <YYYY-MM> --output <local-json>`会创建新executionId、记录文件哈希/耗时/覆盖/recognition attempts，不会自动删除缓存；当前还未汇总全量Pi tokens、模型排队时长、内存峰值或P95，因此单次脚本输出不足以证明完整性能门槛。
3. P0≤45分钟，P1多次运行中位数约30分钟且P95≤45分钟，必须同时满足证据覆盖与经审定质量门槛。当前未执行真实100文件基准。
4. Linux构建/离线包导出导入、PostgreSQL迁移、应用静态页面/鉴权、外部服务内网调用、恢复及回调实收按 `docs/deployment.md` 执行。本机Docker daemon不可用（desktop-linux pipe不存在），不能在当前环境提供容器实跑证据。

## 公开接口免令牌续记

按用户后续决定移除工作台弹窗、浏览器令牌状态和公开任务/规则/运行/证据/原件 API 的 `X-Inspection-Token` 校验；删除部署模板与 Windows 演示脚本对该变量的要求。`/internal/*` 与 Pi worker 的内部令牌、lease/fence、矿井范围和文件哈希约束仍保留。生产配置仍要求内部令牌和矿井列表。已有 `.env.local` 未改，旧变量即使存在也不再被公开接口使用。

改动前新增两项回归均按预期返回 401；改动后 Python 全量 187/187、Pi 12/12、前端辅助 4/4、离线脚本 2/2 通过，compileall、Node 语法、pip check、Compose config 和 diff check 通过。隔离18192进程重启后，真实 Chromium 首屏直接加载26个历史任务，未出现连接弹窗；`/v1/runtime`、列表、运行详情、原件预览、无令牌提交任务均未发送 `X-Inspection-Token`，提交后任务数增至27且运行显示排队中。390px运行页曾由表头内绝对定位的隐藏文本撑到542px，修复后页面宽390px，表格仍可在自身容器内滚动。wheel 已重新构建并逐字节核对静态资产。用户8092进程未重启，当前仍是旧运行版本，需由其管理者重启后才能使用新行为。

该轮证明独立工程的第一条业务主链可运行，不能用于宣称生产放行。

## 平台核心补建验证（最新续记）

业务规则取舍暂停，`stop_work_unload` 资产未改。平台新增注册式检查项、冻结计划指纹、确定性算子图及独立事实验证器；库存先冻结、Pi按需解析文件；持久工作单元支持原子领取、旧fence拒绝、成功复用、失败单元恢复；模型/OCR/文档共享permit，矿井领取轮转；来源完整性由宿主HMAC回执绑定矿井、周期、清单及全部文件哈希，未证明不允许pass。

Pi工具新增按需文件、批文件、事件上下文、版本比较、显式缺口与trace；有工具/页/token/模型轮数/截止时间/分类重试预算。OCR可有界重试并转视觉复核，保留各provider候选；只有受信独立验证器或冻结签名复核回执才能发布对应的来源事实，冲突回执隔离。工作台新增覆盖审计、处理单元、确定性追问、运行比较及逐字段复核意见。复核意见不改历史结论。

合成检查项的临时SQLite集成测试已覆盖：API受理→库存冻结→按需解析→分页穷尽检索→合法来源证明→确定性pass；伪造或漏文件证明、未索引/未读完、候选反值均不能放行；新fence仅重试失败文件。另有真实Pi SDK与假模型、PDF+假OCR、签名转录复核及浏览器交互。`scripts/benchmark.py` 增加重复冷/热、可比性与P50/P95；`scripts/score-corpus.py` 可对已裁决金标准评分，未裁决不计分，未知分母为null。

本轮最新执行的准确测试数量、最终wheel文件数和Compose结果，应以文末新验收输出为准，不沿用前文187/12/33数字。未执行真实Qwen/OCR、同批100文件、正式OceanBase/上下游、PostgreSQL迁移或Linux Docker容器运行。这些是外部验收门，不可从本地合成测试推断。

**最终本机验收（当前源码）：** `.venv` unittest discovery **325/325**，Pi Node **22/22**，前端辅助 **4/4**，离线包脚本 **2/2**；Python compileall、Pi/frontend Node syntax、pip check、Compose config(`--env-file .env.example`)和`git diff --check`通过。wheel在代码与README定稿后重建并核对资产，见`.local/verification/wheel-assets-final.json`。浏览器使用18192独立应用：覆盖审计显示未获范围证明/已闭合取证1单元；处理单元显示成功解析；同矿同项运行差异、候选逐字段复核意见、确定性追问均实际交互成功，390px证据页无横向溢出且无JS异常。复核意见后历史结果仍为pending。注意18192为隔离合成库，不等于真实业务验收。
