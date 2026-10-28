# compliance-pi-platform

基于 Pi 的跨文档合规检查平台独立工程。

续建恢复入口：[当前交接与待办](docs/handoff-2026-10-28.md)。前端已完成隔离浏览器验收，真实模型/资料与 Linux 验收仍待执行；上下文压缩或切换开发者后先读它，再核对工作区实际文件。工作台与 API 同源，Windows 用法见 [本地启动](docs/windows-local-start.md)。

工作台和公开任务/证据接口直接访问，无令牌弹窗；请将 API 绑定在受控网络入口。Pi 与控制服务之间的内部接口仍需独立凭证。

平台核心扩展和验收边界见 [平台核心补建](docs/core-platform-plan-2026-10-28.md) 与 [验证记录](docs/verification-2026-10-28.md)。当前先完成通用取证、覆盖、恢复、证据导航和质量测试底座；`stop_work_unload` 资料与细则留待业务方对齐，不因平台测试而变更。

独立的 Python 控制面和 Node/Pi 取证 worker，用于规则驱动的多文件合规检查。Pi session 不承担业务状态，候选经过来源验证和确定性规则后才能进入结果。Pi worker 使用 ESM JavaScript 调用真实 SDK，减少离线运行时的构建步骤。

## 建设原则

这是新工程。旧 `compliance-service` 只提供领域契约、规则来源和差分样本参考；本工程不导入旧包、不共享旧数据库、不直接复用旧运行时。Pi SDK 与文件接入通过显式接口连接，让任务身份、证据来源、覆盖门和并发调度可单独验证。

方案中发现并修正的风险：

- Pi 的 session/checkpoint 不承担业务任务状态；
- Pi 并行工具执行不等于全局并发控制，宿主必须限制并发、超时和取消；
- 模型输出不能自行声明 verified、pass 或 no_pass；
- source completeness、文件版本和原文定位必须由服务端提供；
- 外部 Qwen/OCR/VLM 只作为内网服务调用，不在本工程部署模型。

## 当前状态

- 第一阶段契约与规则计划见 `packages/contracts`、`rule_assets`；
- Pi runtime 固定 `@earendil-works/pi-agent-core` / `pi-ai` 1.4.0 和 lockfile；实际 Pi Agent 已通过本机 OpenAI-compatible 测试服务验证并行工具调用；
- Qwen/OCR/VLM 是外部服务，本项目只保存 endpoint/profile/timeout/concurrency 配置；
- `pass` 需要完整覆盖，明确反证才允许 `no_pass`，缺证/冲突/不可读保留 `pending` 或 `unverifiable`。

已完成本机端到端验证：任务经 FastAPI 受理后，由控制 worker 调用真实 Pi SDK worker；Pi 通过本机 OpenAI-compatible 假模型并行调用证据工具，候选经过服务端来源绑定后执行确定性评估，并通过本机 HTTP 回调确认 delivery。该测试不使用真实模型、OceanBase 或生产文件。

## 开发运行

Windows PowerShell 的完整步骤和启动脚本见 [Windows 本地启动](docs/windows-local-start.md)。本地可先运行不调用模型的演示；Linux 生产使用 Docker。

```bash
python -m venv .venv
# Linux: source .venv/bin/activate；Windows: .venv\Scripts\Activate.ps1
python -m pip install -c requirements.lock -e '.[test,render,oceanbase]'
cd apps/pi-worker
npm ci --ignore-scripts
cd ../..
uvicorn apps.control_api.main:app --host 127.0.0.1 --port 8092
```

API 和 worker 分进程运行：`python -m apps.control_api.worker`；Pi 服务入口为 `node apps/pi-worker/src/server.mjs`。真实模型调用需配置 `PI_MODEL_BASE_URL`、`PI_MODEL_ID`、`EVIDENCE_API_URL` 和 `INTERNAL_TOKEN`；生产同时要求可信上游 token、授权矿井和 PostgreSQL。配置示例见 `.env.example`。

来源由服务器上的 `COMPLIANCE_SOURCE_CONFIG` 指定，格式见 [来源配置](config/sources.example.json)。本地可将 `COMPLIANCE_INPUT_ROOT` 指向 `fixtures`，复制来源配置为部署专用 `sources.json`，使用 `demo-mine/2026-10` 的合成资料。实际启用 Pi 时设置 `PI_ENABLED=true`；本地未启用时明确记录 `pi_execution_disabled` 缺口，不输出通过。上游不能指定任意本地路径。

OceanBase 适配器提供参数化只读查询、周期/资料频率限界、查询冻结、逐附件收据和失败恢复；真实只读视图列名、驱动和生产权限必须独立联调。Search 第一版为冻结范围内的中文双字切分 BM25 与字段过滤，导航按命题、事件关联、引用和候选状态展开；向量、OpenSearch 和大规模知识库仍是后续适配项。

## 验证

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s scripts -p 'test_*.py' -v
cd apps/pi-worker && npm test
# 返回工程根目录后执行
docker compose --env-file .env.example -f infra/docker/compose.offline.yml config --quiet
```

本机 fake provider 和合成样本仅证明代码契约、工具并行、超时、来源隔离和规则门禁，不证明真实煤矿业务准确率或 100 文件 30 分钟吞吐。正式 OceanBase/上游/回调、专家 gold 及 Linux Docker 实跑属于独立验收项。

真实资料基准入口：`python -m scripts.benchmark --mine <矿井> --cycle <YYYY-MM> --output .local/benchmark.json`。它会调用部署配置中的真实服务，记录耗时、文件哈希、覆盖及识别尝试；不会清空已有缓存，也尚未汇总全量 Pi tokens、队列等待、内存峰值或多次运行 P95。冷启动比较使用新配置的缓存目录，不能以热缓存结果冒充首次处理。当前验证记录见 [2026-10-28 验证](docs/verification-2026-10-28.md)。

Linux 离线交付见 [部署说明](docs/deployment.md)，设计调整见 [架构决策](docs/architecture-decisions.md)。镜像不包含任何模型权重或 OCR 推理服务。
