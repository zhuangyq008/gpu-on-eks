# Qwen-VL on EKS（低冷启动、弹性 GPU 推理）

在客户自己的 AWS 账号里，用 CloudShell 交互式部署 Qwen3.5-35B-A3B（FP8）图片分析服务：
- 按负载自动扩缩容；主力 GPU 机型没货时自动回退；
- 冷启动经过优化，新节点上约 3 分 40 秒开始服务；
- 集群同时承载客户自己的非 GPU 业务。

## 文档

| 文档 | 给谁看 | 内容 |
|---|---|---|
| [04 架构与使用手册](docs/04-architecture-and-operations.md) | **先看这篇** | 当前实现的架构、冷启动全流程、KEDA + Karpenter 弹性伸缩、使用和运维、可观测性怎么用 |
| [05 测试报告](docs/05-test-report.md) | 验收 | 测试环境、方法、各项结果（部署、伸缩、回退、冷启动分段、推理性能、安全评审），以及未覆盖的项 |
| [03 部署参数与交互](docs/03-deployment-parameters.md) | 部署人员 | CloudShell 约束、4 个问题、参数分层（含哪些已生效）、AZ 选择、命令、实现进度 |
| [01 方案设计](docs/01-architecture-design.md) | 架构评审 | 设计目标和取舍：模型与 GPU 选型、冷启动设计、弹性设计、成本、风险。开头有「设计与当前实现的差异」 |
| [02 可观测性设计](docs/02-observability-design.md) | 架构评审 | CloudWatch / Grafana 两种模式的完整设计（大部分还没有实现，开头有实现状态） |

## 当前状态（2026-11-04）

| 已实现，并在 ap-southeast-5 验证 | 计划中 |
|---|---|
| 交互式部署、断线续跑、用保存的配置更新 | 网关（internal ALB / 公网 HTTPS + API Key）、NetworkPolicy |
| 网络、EKS、Karpenter、通用 CPU 节点池、GPU 节点池 | 可观测性 CloudWatch 模式（面板、告警、日志集中采集） |
| 权重同步到 S3（逐文件校验哈希）、vLLM 从 S3 流式加载 | `status` / `test` / `scale` / `destroy` 命令 |
| KEDA 按负载扩缩容、主力没货时自动回退 | 镜像同步到 ECR / 预缓存 |
| 冷启动优化：NVMe + SOCI 并行拉取、S3 编译缓存 | 按负载扩缩容的压测、g7e 实机验证 |

> ⚠️ **推理服务目前没有认证，只能在集群内访问**（ClusterIP）。网关上线之前，不要通过 LoadBalancer 或 Ingress 把它暴露出去。

## 在 CloudShell 里使用

交付包里要有预先编译好的 CDK（`infra/dist/`，不在 git 里）。直接 `git clone` 下来部署，会在第一个阶段报错。打包和上传的方法见 [04 §5.1](docs/04-architecture-and-operations.md#51-部署续跑更新)。

```bash
cd gpu-on-eks
./qwenctl preflight                  # 只做环境检查（只读）
./qwenctl deploy                     # 环境检查 + 4 个问题 + 确认 → 部署
./qwenctl deploy --resume            # 断线或失败后，从中断的阶段继续
./qwenctl deploy --update            # 用保存的配置重新执行所有阶段（升级、改参数后）
./qwenctl deploy --plan              # 只生成并保存配置，不部署
./qwenctl deploy --yes --approve     # 无人值守：默认值 + 直接部署
./qwenctl deploy --config config.yaml   # 带高级配置（需要 PyYAML）

export KUBECONFIG=~/.qwenctl/qwen-vl/kubeconfig   # 部署完成后操作集群
```

调用服务、部署业务、调整副本、暂停 GPU、清理环境、排查问题，见 [04 §5、§6](docs/04-architecture-and-operations.md#5-使用手册)。
**请备份 `~/.qwenctl/qwen-vl/config.json`**：这是更新和续跑用的唯一一份配置。**已有部署只用 `--resume` / `--update`**：再运行不带参数的 `deploy` 会覆盖这份配置。

## 目录

```
qwenctl              CloudShell 里的启动脚本
cli/qwenctl/         交互式部署工具（Python 3.9+，只依赖 boto3）
cli/tests/           单元测试（pytest，用假的 AWS 访问层，不访问网络）
infra/               CDK TypeScript 工程，输入是 qwenctl 生成的部署配置 JSON
e2e/                 验收脚本（在 vLLM 容器里运行的推理测试）
docs/                架构、使用手册、测试报告、设计文档
versions.json        所有锁定版本（工具、chart、镜像 digest、模型 revision）
```

## 开发

```bash
# Python
cd cli && python3 -m pytest --cov

# CDK
cd infra && npm ci && npm run typecheck && npx jest --coverage
npm run build && npx cdk synth -c configFile=$HOME/.qwenctl/qwen-vl/config.json

# 锁定版本（Python 和 CDK 共用）
cat versions.json
```
