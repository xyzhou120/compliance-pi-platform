# 平台核心补齐执行基线

2026-10-28：用户明确先完成平台核心，再对齐 stop_work_unload 细则。保持现有业务算子，不增加第二个正式检查项。公开工作台免令牌；内部 Pi 接口、冻结范围和候选验证仍有保护。

## 完成标准

- 检查项注册表管理资产、事实验证器和确定性评估器；计划编译产出资料选择、命题依赖、字段契约、覆盖契约、工具与预算，校验循环/未知算子/版本漂移。以仅测试注册的合成检查项证明扩展，不修改煤矿细则。
- 先冻结清单、原件和轻量文件库存；Pi 决定 parse_file/inspect_documents/open_page/recognize_pages，原生解析不再必须先串行扫描全部输入。未处理文件明确 not_indexed。
- Python 持久化文件/页工作单元、输入指纹、attempt、状态和结果；恢复复用成功单元，只重试失败/未提交单元；旧 fence、重复内容和暂停期间提交被拒绝。
- 模型/工具/provider 请求有预算、超时、分类重试、可取消与共享并发限额；调度保留其他矿井机会。不可取消的在途 provider 不能提前释放名额。
- 冻结范围内每个必检文件/单元具有解析、检索、识别水位；not_indexed/not_searched/searched_no_hit/source_absent/unreadable/provider_failed/conflicted分别保留。权威范围证明绑定矿井、周期、清单与原件hash，通过宿主配置的验证策略，客户端及模型不能声明完整。
- 自由文本字段有显式事实契约和独立来源锚定验证；OCR/VLM采用可配置失败/低质量复核，不以模型一致或quote匹配证明语义。证据质量无法闭合时保留候选，提供可审计人工复核记录，历史结果不可改写。
- 证据工具支持事件展开、版本比较、缺口提交；问答基于冻结命题/证据返回引用与局限；新运行差异与规则变更影响回放不覆盖旧结果。
- 运行与工具trace、provider调用/重试/排队、模型tokens、缓存、内存与耗时可汇总；基准支持固定配置的重复冷/热对照和P50/P95，时间不代表质量。
- 核心API、工作台入口、离线镜像资产及本机端到端通过回归；真实Qwen/OCR、100文件、PostgreSQL和Linux实跑作为外部验收单列，未实测不得标完成。

## 并行接口约定

根代理负责 store.py/worker.py/evidence.py/main.py、跨模块集成、部署及最终回归。各子任务仅编辑指定文件，不撤销其他任务修改。

1. 计划与覆盖：registry.py/planner.py/coverage.py及专用测试。覆盖审计纯函数输入 run、snapshot、plan、work_units、searches、server-owned proof，不能把简单bool当证明。
2. 文档：documents.py/sources.py/acquisition.py及测试。acquire_run(settings.inventory_only=true)只冻结；parse_frozen_file(snapshot,file_id,settings)返回 {file_id,evidence,coverage,coverage_gaps,parser_revision}。evidence沿用稳定ID与归属。
3. provider与质量：providers.py/validator.py/settings.py/quality.py及测试。提供多provider复核、分类重试、取消、通用字段锚定校验；现有源事实边界保留。
4. Pi：apps/pi-worker。新增工具调用 internal/runs/{id}/tools/parse_file、inspect_documents、expand_event_context、compare_versions、submit_gaps；提供持久化宿主checkpoint协议、预算和trace。
5. 导航：navigation.py及测试、前端。纯函数支持有引用的命题问答、事件展开、版本比较、跨run差异；API由根代理接入。复核记录服务在宿主账本，不允许改最终status。

## 验证与边界

重点故障：重复工具、失去fence、暂停、provider超时、进程重启、相同文件重复引用、缺失范围证明、错误矿井、OCR误读/字段翻转、冲突版本、无事件、候选关联、回调重试。所有新增表采用增量建表，不修改或清空用户数据库；所有验证只用临时库/端口/假服务，不加载.env.local，不操作8092/.idea。

## 本阶段完成状态

截至本轮代码补建，核心底座已完成本机组合验证：

- 计划注册与版本指纹：`registry.py`、`planner.validate_plan`、DAG确定性算子、受信 `fact_validator`。新增检查项需要注册资产、确定性 evaluator、事实契约及受信事实验证器；模型来源锚定不会自动升级为事实。
- 资料与覆盖：`inventory_only` 冻结文件库存、按需 `parse_file/inspect_documents`、签名范围证明、资料条件激活、分页穷举检索水位和覆盖状态。
- 工作单元与调度：SQLite/PostgreSQL兼容的WorkUnit状态、原子领取、fence恢复、成功复用、失败重试；模型/OCR/文档共享permit和矿井轮转。
- Pi：新增按需解析、批文件、事件上下文、版本比较、缺口提交、trace、预算消费、分类模型重试和共享模型permit；Pi Node 19/19通过。
- 质量与复核：OCR/VLM有界重试、fallback、低质量候选、签名人工转录复核、冲突隔离、复核意见只读追加；不改业务结论。
- 导航和工作台：确定性追问、覆盖审计、处理单元、运行比较、候选逐字段复核入口，免令牌页面仍保留。
- 基准：重复冷/热运行、P50/P95、可比性、配置安全指纹、控制进程内存峰值和缺失指标null。

最终全量回归为Python 325项、Pi Node 22项、前端辅助4项、离线脚本2项通过；compileall、Node语法、pip check、Compose config和wheel静态资产通过。早期294项Python和19项Pi数字不是当前验收基线。新增能力均使用临时库/假服务验证，未调用真实模型、未重启8092、未读取真实`.env.local`。

独立代码审查后补齐了五处关键门：检查项算子及事实验证器指纹随计划冻结；覆盖审计对失败识别和成功解析不再因遍历顺序而改变结论；文档页预算在账本原子预留；独立转录回执在运行开始冻结并以有fence的工作单元登记派生证据；定版只接受当前匹配的完整覆盖审计。前端实际浏览器验证了覆盖审计、工作单元、跨运行比较、候选逐字段复核记录和有引用的确定性追问。

平台提供签名范围证明、签名独立转录回执和质量评分脚本，但不签发第三方的完整性或业务真实性声明。来源目录、专家裁决语料和真实算力仍需部署方提供，详情见最新验证记录。

仍待外部验收：真实Qwen/OCR质量、100文件吞吐、正式OceanBase/上游/回调、PostgreSQL实际迁移、Linux Docker构建导入实跑。`stop_work_unload` 的资料取舍和具体判定细则暂停到核心验收后再与业务确认；当前没有改变其规则资产。
