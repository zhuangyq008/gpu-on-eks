# Qwen-VL on EKS（低冷启动、弹性 GPU 推理）

在客户自己的 AWS 账号里，用 CloudShell 交互式部署 Qwen3.5-35B-A3B 图片分析服务。集群同时支持客户的非 GPU 业务负载。

| 文档 | 内容 |
|---|---|
| [01 架构设计](docs/01-architecture-design.md) | 模型与 GPU 选型、冷启动优化、弹性伸缩、成本 |
| [02 可观测性](docs/02-observability-design.md) | 冷启动时间线、指标/日志/trace、CloudWatch ↔ Grafana 两种模式 |
| [03 部署参数与交互](docs/03-deployment-parameters.md) | CloudShell 约束、4 个问题、参数分层、AZ 选择、命令 |

## 在 CloudShell 里使用

```bash
cd gpu-on-eks
./qwenctl preflight          # 只做环境检查（只读）
./qwenctl deploy             # 环境检查 + 4 个问题 + 确认 → 部署
./qwenctl deploy --plan      # 只生成并保存配置，不部署
./qwenctl deploy --yes       # 全部用默认值生成配置（不部署，除非再加 --approve）
./qwenctl deploy --config config.yaml   # 带高级配置（需要 PyYAML）
```

> **当前版本会真正部署**：网络、EKS（含系统节点组和 Karpenter）、通用 CPU 节点池和 GPU 节点池。确认页之后才会创建资源；
> 只想看配置用 `--plan`。KEDA、网关、可观测性、vLLM 等后续阶段还在开发中。

```bash
./qwenctl deploy --resume     # 断线或失败后，从中断的阶段继续
./qwenctl deploy --yes --approve   # 无人值守：默认值 + 直接部署
export KUBECONFIG=~/.qwenctl/qwen-vl/kubeconfig   # 部署完成后操作集群
```

## 目录

```
qwenctl              CloudShell 里的启动脚本
cli/qwenctl/         交互式部署工具（Python 3.9+，只依赖 boto3）
cli/tests/           单元测试（pytest，用假的 AWS 访问层，不访问网络）
infra/               CDK TypeScript 工程，输入是 qwenctl 生成的部署配置 JSON
docs/                设计文档
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
