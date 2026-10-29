# Windows 本地启动

本地开发直接运行 Python 和 Node，Linux 生产使用 Docker。业务工作台由 API 同源托管，访问 `http://127.0.0.1:8092/` 或 `/workbench`；没有额外前端进程、npm 构建或 CDN 依赖。模型、OCR 和视觉服务均访问已有内网服务器。

已有 8092 服务时不要重复启动或自动结束其进程。若它在前端路由加入前已启动，需由服务使用者在合适时机重启该 API，随后刷新工作台。本文的启动命令不代表本轮已重启用户服务。

## 1. 首次准备

在 PowerShell 中进入工程根目录：

```powershell
Set-Location 'C:\Users\86158\Documents\ChatGPT\检查规则梳理\compliance-pi-platform'
python --version
node --version
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

要求 Python 3.12+、Node 22.19+。setup 脚本创建独立 `.venv`，按锁定依赖安装 Python 和 Pi SDK；首次安装需要可访问依赖源。当前电脑已经有 `.venv` 和 Pi node_modules，依赖未变更时可直接启动。

`-ExecutionPolicy Bypass` 仅作用于本次 PowerShell 子进程，不修改系统执行策略。脚本中的配置读取不执行配置内容。

## 2. 配置与不调用模型的演示

`start-local.ps1` 默认会加载 `.env.local`，其中可能已经启用真实 Pi。**不要把现有配置直接当作无模型演示配置**，也不要覆盖现有 `.env.local`。在未配置过的全新工程中，脚本默认 `PI_ENABLED=false`；已有真实配置时，本轮界面验收使用下面的隔离应用。

本工作区已准备 `.local/ui-preview/preview_app.py`（本地验收产物，非安装包入口）。它显式创建独立 Settings，Pi 禁用、数据库为 `.local/ui-preview/ui.sqlite3`，不加载 `.env.local`。在根目录启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn preview_app:app --app-dir .local/ui-preview --host 127.0.0.1 --port 18192
```

访问 `http://127.0.0.1:18192/` 后工作台直接加载，无需访问令牌。该实例没有后台控制 worker，新增任务保持排队，可验证暂停恢复；已有合成运行可验证结果、证据和原文。可运行 `.\.venv\Scripts\python.exe .local/ui-preview/seed_verification.py` 补充命题引用、PDF 与分页样例。仅关闭自己启动的 18192 进程，不操作 8092。

以下两窗口演示适用于全新、确认未启用真实模型的本地配置：

打开两个 PowerShell 窗口，都进入上述目录。

窗口一启动 API：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Role api
```

窗口二启动控制 worker：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Role worker
```

API 保持运行，浏览器访问工作台 `http://127.0.0.1:8092/`。任务、结果、证据和原件接口不要求访问令牌；可访问该服务地址的人可以读取和操作服务端配置允许的矿井任务，部署时应由主机或网关限制网络入口。Pi 内部接口仍使用独立内部令牌。健康检查为 `/health`。Swagger `/docs` 的资源默认来自 CDN，业务工作台无此依赖。

再开一个 PowerShell 窗口提交演示任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-task.ps1
```

演示使用 `fixtures` 中的合成文本、检查项 `stop_work_unload` 和 demo-mine。只有未被配置覆盖的默认 `PI_ENABLED=false` 才不会调用 Pi；务必核对所使用的配置。预期执行状态为 completed、业务结果为 pending；资料和命题不完整时不输出通过。脚本自动打印结果，任务标识保存在 `.local/last-demo-task.json`。

工作台流程：打开页面 → 新建检查 → 查看所选运行 → 命题/原始文件/候选 → 查看证据 → 中文搜索或按文件 ID 过滤 → 展开引用 → 查看原文。PDF 支持原件及证据页图，Word/Excel 下载后本机打开。规则页显示所选运行冻结版本；未选择运行时显示当前预览。技术失败、待核验、无法核验与执行完成、回写成功分别展示。新增任务需由控制 worker 处理，不会因浏览器提交而直接完成。

运行详情新增“覆盖与对比”：区分资料范围证明和实际取证覆盖、按文件或页查看处理单元；输入另一运行ID可对比同矿同项的来源与命题，不修改历史。证据页“核验追问”只根据冻结命题、缺口及原文引用回答；对新资料只提示建立新执行。候选列表的逐字段复核仅保存意见及其证据ID，不修改已有结论。配置`COMPLIANCE_SCOPE_PROOF_KEY`和来源`authority_receipt_file`后，平台才可能核对权威范围证明；缺失时保留pending。签名转录复核需服务端配置`COMPLIANCE_EVIDENCE_REVIEW_KEY`和受信回执文件，普通页面意见不能替代签名回执。

本地数据分别位于 `.local/windows.sqlite3`、`.local/artifacts`，不读写旧服务数据库。关闭进程用所在窗口的 Ctrl+C。

任务没有总运行时长截止。30 分钟只是性能目标，运行超过它会继续完成资料处理、检索和评估。旧 `COMPLIANCE_TASK_TIMEOUT`、`PI_DEADLINE_MS` 以及计划/请求中的 deadline 字段不再截断任务。租约心跳和暂停控制持续有效；`PI_MODEL_REQUEST_TIMEOUT_MS`（默认 120000）、OCR 和 PDF 处理超时仅约束单次调用，单次失败会记录原因并继续其他资料。

## 3. 接入私有 Qwen 与 OCR

复制独立本地配置：

```powershell
if (-not (Test-Path .env.local)) { Copy-Item .env.local.example .env.local }
notepad .env.local
```

至少填写：

```dotenv
PI_ENABLED=true
PI_MODEL_BASE_URL=http://你的模型内网地址:端口/v1
PI_MODEL_ID=模型服务器实际公布的model-id
PI_MODEL_API_KEY=如需认证填写
OCR_ENDPOINT=http://你的OCR内网地址:端口/ocr
OCR_PROVIDER=http-json
OCR_AUTHORIZATION=如需认证填写完整Authorization头值
```

OCR 支持 `http-json` 和 `multipart-http`，以已有服务接口为准。只有视觉服务时，OCR_ENDPOINT 留空，填写 VISION_ENDPOINT、VISION_MODEL 和 VISION_AUTHORIZATION。本项目不安装或部署模型。API key 是模型服务的认证信息；`model-id` 必须使用服务器实际提供的标识，不从模型名称猜测。

保存后重启 API 和控制 worker，并增加第三个运行窗口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Role pi
```

Pi 健康检查是 `http://127.0.0.1:8093/health`。它的 `ready=true` 表示所需配置存在；私有模型是否实际连通仍以真实请求为准。三个进程都读取同一个 `.env.local`，不会自动读取生产 `.env.example`。

## 4. 改用自己的资料

复制 config/sources.example.json 为服务器本地配置，设置 mode=local_manifest，按矿井和周期指向 manifest。manifest 中每个文件使用相对 COMPLIANCE_INPUT_ROOT 的路径，登记 file_id、role、material_group 和 report_date；同时在 `.env.local` 配置 COMPLIANCE_SOURCE_CONFIG、COMPLIANCE_INPUT_ROOT 和 COMPLIANCE_ALLOWED_MINES。不要把实际凭据放进 Git。

任务通过工作台或 POST /inspection/tasks 提交，只传 mineId、checkCycle、items 和新的 executionId；不能由任务传任意文件路径。演示脚本固定 demo-mine；检查真实矿井时可在工作台选择已配置矿井，或用 PowerShell 按下面例子调用：

```powershell
. .\scripts\local-env.ps1
$request = @{ mineId='实际矿井编码'; checkCycle='2026-10'; executionId=[Guid]::NewGuid().ToString('N'); items=@(@{itemCode='stop_work_unload'}) }
$response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8092/inspection/tasks' -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Depth 6)))
$taskId = [string]$response.Headers['X-Inspection-Task']
Get-LocalJson -Uri ('http://127.0.0.1:8092/inspection/tasks/' + $taskId)
```

没有填写 resultCallbackUrl 时不会回调。需要回写测试时，先配置允许的目标域名及认证，再按已有回调协议联调。

## 5. Linux Docker 部署

本地脚本用于 Windows 开发；生产使用 infra/docker/compose.offline.yml。先在联网 Linux 构建机执行镜像构建和离线包导出，再在目标 Linux 校验、导入、启动。具体命令见 [Docker 离线部署](deployment.md)。Windows 的启动成功不代表 Linux 镜像已经完成验收。
