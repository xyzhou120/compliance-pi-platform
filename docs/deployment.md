# Linux 离线 Docker 部署

本项目镜像只包含合规 API、控制 worker 和 Pi worker。Qwen、OCR/VLM 是外部算力服务，本项目不部署模型权重、GPU、CUDA 或推理镜像。

联网构建机完成 `npm ci`、Python 依赖安装、应用镜像构建、漏洞扫描、SBOM 和离线冒烟测试。目标机只导入固定镜像、校验 SHA256、执行迁移和启动 Compose；运行期间不访问 npm、pi.dev 或公网模型服务。模型/OCR 依赖由外部算力服务提供。

构建机与目标机使用相同 CPU 架构。构建脚本先把基础镜像 tag 解析成 digest，再构建应用镜像；Pi npm lockfile 固定版本，Node 镜像内已装齐依赖，目标机不运行包管理器。

```bash
# 联网 Linux 构建机；需要 Docker daemon
python scripts/build-images.py --tag 0.1.0 --platform linux/amd64
docker pull postgres:16.4-bookworm
python scripts/build-offline-bundle.py --output release-0.1.0 --tag 0.1.0 \
  local/compliance-pi-platform:0.1.0 local/compliance-pi-worker:0.1.0 postgres:16.4-bookworm

# 离线目标机：校验后导入，最终 image ID 与清单一致
python3 release-0.1.0/load-offline-images.py release-0.1.0
cp release-0.1.0/.env.example release-0.1.0/.env
# 填入实际内网 endpoint、凭据、授权矿井和绝对挂载目录
sh release-0.1.0/start-offline.sh
```

必须由部署方提供并验收：Linux 架构、Docker/Compose 版本、PostgreSQL 数据卷、上游/OceanBase/回调网络、外部 Qwen/OCR endpoint、认证、模型 profile、并发配额、超时、TLS 和 DNS。目标应用服务器不需要 GPU。

应用启动探活只验证外部服务协议和能力，不下载模型。外部服务不可用时，任务记录 provider attempt 和明确技术缺口，进入 `unverifiable`/`failed`。

Compose 的 `backend` 网络是内部网络；`model-egress` 允许访问模型/OCR、OceanBase 和正式回调的内网地址。Docker bridge 本身不提供目标域名白名单，应由主机防火墙或受控出口代理阻断公网并允许这些明确目标。API 默认绑定宿主机 `127.0.0.1:8092`。公开的任务和证据接口没有访问令牌，接入反向代理或现有网关时必须限制可访问来源；能连到 API 的客户端可读取和操作已配置矿井的任务。

敏感配置从 `.env`/环境或平台 secret 注入，不写进镜像。发布包中的 `.env.example` 仅是占位，`SHA256SUMS` 包含模板而不包含目标机生成的 `.env`。PostgreSQL 和应用原件/缓存使用独立持久卷；备份并记录 schema 与规则 revision 后再升级，失败时恢复数据备份并回滚到上一次发布包。

范围证明是独立配置的签名回执，不是任务请求或模型声明。来源方基于已审定完整性政策生成 `payload`（authority、mine_id、check_cycle、manifest_hash、scope_complete、全部file_id/content_hash），用 `COMPLIANCE_SCOPE_PROOF_KEY` 签HMAC；宿主通过sources.json的`authority_receipt_file`读取，仍核对归属和全文件哈希。无此证明时保持source_complete=false。扫描页经独立人工复核时，可用另一密钥`COMPLIANCE_EVIDENCE_REVIEW_KEY`签转录回执，并通过`COMPLIANCE_EVIDENCE_REVIEW_RECEIPTS`指向受信输入根目录内文件；回执在运行冻结，不在完成后动态修改历史。普通浏览器复核意见只是审计记录，不签发事实。OceanBase适配器查询`mine_remote_disaster_file`时使用固定字段投影和参数化清单/周期谓词，附件链接由配置基址、桶名和对象键组成，必须配置附件服务基址后才能冻结原件。

新检查项通过可信代码注册`CheckRegistry.register()`绑定资产、事实契约、受信事实验证器和确定性评估器；计划版本包含实现指纹。请在独立运行先跑`python scripts/score-corpus.py --help`了解金标准评分接口，再用冻结同批语料测试；脚本不会创建业务结果。`scripts/benchmark.py`支持重复冷/热及P50/P95，冷缓存需显式`--cache-root`且不得指向或清理已有缓存目录。主机缓存、数据库、受控网络出口与secret应由部署方管理。

当前本机有 Docker CLI，`docker compose config --quiet` 已通过，但 Linux daemon 没有运行，因此未构建/加载镜像或做容器实跑。发布前必须在 Linux 构建机完成这四个门：构建镜像、导出/导入、自内网启动、约 100 文件真实吞吐与恢复验收。

对发布脚本的本地验证：`python -m unittest discover -s scripts -p 'test_*.py' -v` 会验证正确包通过、篡改和路径越界被拒绝。它不替代 Docker 验收。
