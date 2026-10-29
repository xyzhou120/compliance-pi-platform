# 当前系统图

> 依据当前工作树实现整理。图中的“外部依赖”表示已定义接口或部署连接，不表示真实生产链路已经验收。当前公开注册的检查项只有 `stop_work_unload`；旧的 `relief_effects` 代码不作为公开检查项绘制。

## 1. 当前系统机构图

```mermaid
flowchart LR
    subgraph UP[业务与资料来源]
        USER[检查人员<br/>工作台查看与操作]
        U[上游业务系统<br/>任务请求 / 回调地址]
        M[矿井资料源<br/>本地清单或 OceanBase 目录]
    end

    subgraph PLATFORM[Independent Pi compliance platform]
        subgraph PY[Python 控制面]
            API[control-api<br/>FastAPI + 同源工作台<br/>公开任务/运行/证据 API]
            CW[control-worker<br/>租约 / fence / 采集 / 评估 / 完成]
            DB[(PostgreSQL<br/>业务账本、运行、证据、Outbox)]
            DATA[(挂载资料与配置<br/>inputs / config / artifacts)]
        end
        PI[pi-worker<br/>Node + Pi SDK<br/>证据工具与候选提交]
    end

    subgraph EXT[外部服务与交付依赖]
        MODEL[OpenAI-compatible / Qwen 模型]
        OCR[OCR / VLM]
        CB[上游结果回调]
    end

    U -->|POST /inspection/tasks| API
    USER <-->|同源工作台/公开运行 API| API
    API <--> DB
    CW <--> DB
    API <--> DATA
    CW <--> DATA
    CW -->|/v1/inspect| PI
    PI -->|内部 Evidence API<br/>取计划/证据/页面/工具| API
    M -->|服务端配置路由| CW
    API -.内部取证工具按配置调用.-> OCR
    CW -.按配置调用.-> OCR
    PI -.模型推理.-> MODEL
    CW -->|完成后 Outbox 投递| CB

    classDef core fill:#e8f3ee,stroke:#2d6a4f,color:#16352a;
    classDef store fill:#eef1f4,stroke:#58636e,color:#20252a;
    classDef ext fill:#fff3df,stroke:#b26a00,color:#4d2d00;
    class API,CW,PI core;
    class DB,DATA store;
    class USER,U,M,MODEL,OCR,CB ext;
```

**边界说明**

- `control-api` 和 `control-worker` 都属于 Python 控制面，共享 PostgreSQL 与挂载资料；worker 直接使用账本/服务，不通过 HTTP 回调 API。
- Pi 只负责按冻结计划取证、调用工具并提交候选/缺口；业务状态和最终结论由 Python 控制面持有。
- `MODEL`、`OCR`、`VLM`、OceanBase 和回调接收方均为外部部署依赖；本项目不打包模型权重或推理服务。
- 旧 `compliance-service` 不在运行时架构内。

## 2. 当前系统文件合规检查流程图

```mermaid
flowchart TD
    A[上游 POST /inspection/tasks<br/>矿井、周期、检查项、可选回调] --> B{API 校验}
    B -->|拒绝客户端 scope、未知检查项或非法回调| X1[400 / 不受理]
    B -->|通过| C[Store.accept<br/>创建任务/运行身份<br/>冻结规则计划与 scope hash]
    C --> D[control-worker 领取运行<br/>lease + fence + deadline]
    D --> E{服务端来源路由}
    E -->|local_manifest| F[读取配置根目录清单]
    E -->|oceanbase| G[固定投影查询目录<br/>按矿井/周期筛选]
    G --> H[受限下载附件<br/>HTTPS 白名单、哈希、PDF 魔数]
    F --> I[生成不可变 snapshot<br/>文件清单、哈希、manifest hash]
    H --> I
    I --> J{启用宿主补采?}
    J -->|是| K[逐文件/逐页面原生解析<br/>PDF / Word / Excel<br/>失败按文件或页面隔离]
    J -->|否| L{启用 Pi?}
    K --> L
    L -->|是| P[Pi worker<br/>内部 Evidence API 按需取证<br/>OCR / VLM 结果仍是候选]
    L -->|否，记录 pi_execution_disabled| N[证据、候选、缺口入账<br/>来源绑定与 lease/fence 检查]
    P -->|宿主补采已启用| M[宿主在剩余预算内补采未读页面]
    P -->|宿主补采未启用| N
    M --> N
    N --> O[EvidenceService 合并证据<br/>独立候选验证 / 原文定位]
    O --> Q[来源完整性与覆盖审计<br/>未覆盖文件/页面形成 gap]
    Q --> R[Python 确定性规则评估<br/>不接受模型业务 verdict]
    R --> S{结果门禁}
    S -->|pass：来源/覆盖完整<br/>必要命题均有证据且为 true| T[原子 finalize<br/>写结果、coverage audit、可选 callback outbox]
    S -->|no_pass：有登记证据的明确反证| T
    S -->|缺证、冲突、不可读或技术失败| T
    T --> V{有待投递回调?}
    V -->|否| W[运行完成<br/>工作台可查询]
    V -->|是| Z[独立 delivery worker<br/>回调 / 重试 / 记录 delivery 状态]

    classDef gate fill:#fff3df,stroke:#b26a00,color:#4d2d00;
    classDef process fill:#e8f3ee,stroke:#2d6a4f,color:#16352a;
    classDef result fill:#eef1f4,stroke:#58636e,color:#20252a;
    class B,E,J,L,S,V gate;
    class C,D,F,G,H,I,K,P,M,N,O,Q,R,T,Z process;
    class X1,W result;
```

**流程边界**

- 下载成功或目录有记录，不等于来源完整；最终 `pass` 仍需通过来源和覆盖审计。
- Pi、OCR/VLM 只能产生带来源的候选；候选须经服务端验证并进入确定性规则评估。
- 任务完成与回调交付分离：回调失败进入 Outbox 重试，不回滚已完成的检查结果。
- 当前本地配置可启用 Pi 和宿主补采；两条取证支路都受并发、超时、取消和租约约束。

## 3. 冲击危险停工撤人与卸压检查项流程图

```mermaid
flowchart TD
    A[冻结六类资料与来源范围<br/>防冲工作日报 / 临界指标 / 调度日报<br/>生产组织通知 / 卸压验收 / 防冲设计] --> B[原生解析与候选取证]
    B --> C[来源绑定的实际事实<br/>事件、工作面、时间、测点、证据引用]
    C --> D[同事件/同工作面关联<br/>冲突隔离、观测窗口检查]
    D --> E{触发识别}
    E -->|预警实测值 > 唯一有效、已批准阈值<br/>同指标/单位/测点/有效期| F[预警触发]
    E -->|动力现象 + 同一事件明确危险研判=true| G[动力现象触发]
    E -->|缺阈值、阈值歧义或动力现象缺危险确认| P1[触发缺证<br/>pending / 人工复核]
    F --> H[确认危险研判<br/>同事件、时间先后可证]
    G --> H
    H --> I{危险研判结果}
    I -->|false| J[不激活卸压义务<br/>仍核对停工处置链]
    I -->|true| K[核对实际处置时序<br/>停工 → 断电 → 撤人 → 报告调度]
    I -->|缺失/冲突/时间不清| P2[危险研判保留缺口<br/>不据此升级重大隐患]
    J --> K
    P2 --> K
    K --> L{四项动作与精确时序}
    L -->|完整、缺失或明确反证<br/>分别记录命题状态| M[保留处置命题及证据引用]
    M --> N{是否“预警超阈 + 危险确认”?}
    N -->|否| O[不激活本轮卸压核查]
    N -->|是| R[按唯一有效批准防冲设计核对卸压<br/>方法、范围、参数、施工时间<br/>验收结果、验收人、验收时间]
    R --> S{卸压证据}
    S -->|完整且逐项一致| T[卸压命题 true]
    S -->|缺失、明确不符或相互冲突| U[卸压命题 false/missing]
    O --> V[按明确反证分类]
    T --> V
    U --> V
    V --> W{证据发现分类}
    W -->|危险确认后停工/撤人明确失败<br/>或卸压不符且仍继续采掘| X[疑似重大事故隐患<br/>人工确认，不是法定结论]
    W -->|明确未断电或未报告调度| Y[疑似违规<br/>不自动套用重大隐患条款]
    W -->|无明确反证| Z{覆盖、冲突、可读性}
    Z -->|完整、无异常| AA[pass<br/>本次事件未发现相应问题]
    Z -->|缺证/命题未闭合| AB[pending<br/>待补证/人工复核]
    Z -->|冲突或必要内容不可读| AC[unverifiable<br/>待人工复核]
    X --> AD1[no_pass<br/>保留 evidence_refs<br/>human_confirmation_required=true]
    Y --> AD2[no_pass<br/>保留 evidence_refs]

    subgraph UNSUPPORTED[明确边界]
        Q[当前公开注册：仅 stop_work_unload]
        Q2[第二检查项/relief_effects：未作为公开检查项实现]
        Q --> Q2
    end

    classDef input fill:#eef1f4,stroke:#58636e,color:#20252a;
    classDef decision fill:#fff3df,stroke:#b26a00,color:#4d2d00;
    classDef normal fill:#e8f3ee,stroke:#2d6a4f,color:#16352a;
    classDef review fill:#fde8e8,stroke:#a33a3a,color:#4d1d1d;
    classDef boundary fill:#f1ecff,stroke:#6b4aa1,color:#322052;
    class A,B,C,D input;
    class E,I,L,N,S,W,Z decision;
    class F,G,H,J,K,M,O,R,T,V,AD1,AD2 normal;
    class P1,P2,U,X,Y,AB,AC review;
    class Q,Q2 boundary;
```

**判定口径**

- 当前 v3 规则把“触发识别、危险确认、处置时序、条件性卸压核查”分开；模型输出不能直接成为业务判定。
- 缺失、时间精度不足、来源冲突和不可读内容保留为缺口或不可核验；不能把缺证画成“未执行”。
- `疑似重大事故隐患`、`疑似违规` 是带证据引用的发现分类；最终状态仍是 `pass`、`no_pass`、`pending`、`unverifiable` 或 `failed`。

## 实现依据

| 图 | 主要实现依据 |
| --- | --- |
| 当前系统机构图 | `infra/docker/compose.offline.yml`；`apps/control_api/main.py`；`apps/control_api/worker.py`；`apps/pi-worker/src/server.mjs`、`runtime.mjs`；`README.md` |
| 文件合规检查流程图 | `apps/control_api/main.py:154-175`；`apps/control_api/store.py`；`apps/control_api/sources.py:234-261`；`apps/control_api/documents.py`；`apps/control_api/evidence.py`；`apps/control_api/coverage.py`；`apps/control_api/worker.py:132-300` |
| 停工撤人与卸压流程图 | `apps/control_api/registry.py:290-295`；`apps/control_api/domain/rules.py:303-366,368-493,707-819`；`rule_assets/stop_work_unload.v2.json`；`docs/stop-work-unload-rule-2026-10-28.md` |

