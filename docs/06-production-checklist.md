# 生产上线检查清单

> 版本：v1.0 | 日期：2026-11-04 | 关联：[04 架构与使用手册](04-architecture-and-operations.md)、[05 测试报告](05-test-report.md)
> 用法：上线前逐项确认。「状态」一列的含义：
> - ✅ 已就绪：当前版本已经实现并验证；
> - 🔧 可配置：已经支持，需要按客户情况打开或填写；
> - ⏳ 计划中：还没有实现，后续版本做；
> - 👤 客户决策：需要客户提供资源或做选择。

## 1. 入口与网络安全

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1.1 | Web 应用的负载均衡（AWS Load Balancer Controller：ALB Ingress / NLB） | ✅ 部署时可选 | 第 4 个问题「Web 应用入口」，默认启用。用法见 [04 §5.8](04-architecture-and-operations.md#58-部署客户-web-应用并对外提供服务) |
| 1.2 | 公网 ALB 的 WAF（AWS 托管的通用、恶意输入、IP 信誉、SQL 注入规则 + 每 IP 限速） | ✅ 随 1.1 创建，**强制挂载** | 准入策略会拒绝没带 `wafv2-acl-arn`、或者值不是本账号本 region WebACL ARN 的公网 Ingress。⚠️ ARN 格式正确但 WebACL 不存在时，ALB 会在没有 WAF 的情况下对外服务，Ingress 建好后要确认 ADDRESS 有值（[04 §5.8](04-architecture-and-operations.md#58-部署客户-web-应用并对外提供服务)）。超过 8 KB 的请求体 WAF 只检查前 8 KB，应用必须自己校验输入 |
| 1.2a | 防止误暴露：`type: LoadBalancer` 必须写 `loadBalancerClass`（否则会建出公网 Classic LB）；系统、推理、监控命名空间里不能建 `alb` Ingress 和 `type: LoadBalancer` Service；不能用旧的 `kubernetes.io/ingress.class` 注解 | ✅ 随 1.1 | 准入策略 + IngressClassParams，8 项反向测试见 [05 §7a](05-test-report.md#7a-客户-web-应用插件2026-11-04) |
| 1.2b | 集群权限按命名空间分配 | 👤 | 1.2a 的护栏防误操作，不防有意绕过：有 cluster-admin 权限的人可以删掉准入策略。给开发团队的权限按命名空间授权（RBAC，或 EKS 访问条目的命名空间范围），不要给 cluster-admin |
| 1.3 | TLS 证书 | 👤 | 客户在 ACM 里签发或导入证书（和集群同一个 region），写在 Ingress 注解里。控制器的默认安全策略是 `ELBSecurityPolicy-TLS13-1-2-2021-06`（只允许 TLS 1.2 和 1.3） |
| 1.4 | DNS | 🔧 / 👤（ExternalDNS 未实测） | 域名托管在 Route 53：部署时可选 ExternalDNS，自动建记录。IAM 只允许在所选区域里新建和更新 A / AAAA / CNAME / TXT 记录，不能删除。建议选一个专门委派给应用的子域区域（如 `apps.example.com`），不要直接选根域。否则客户在自己的 DNS 上 CNAME 到 ALB。ExternalDNS 测试时没有开启，上线前建议用一个临时托管区域实测一次 |
| 1.5 | 推理 API 的网关：API Key、限流、请求和图片大小校验、服务端关闭 thinking、禁止拉取外部 URL（SSRF） | ⏳ **推理 API 对集群外开放前必须完成** | 目前推理服务只在集群内，没有认证。Web 应用在集群内调用 `qwen-vl.qwen-vl.svc:8000` |
| 1.6 | NetworkPolicy（开启 VPC CNI 的网络策略执行；推理命名空间、Prometheus 只放行需要的来源） | ⏳ | 和 1.5 一起做 |
| 1.7 | EKS API 端点 | 👤 | 默认公网 + 私网（IAM 认证）。有要求时设 `network.eks_endpoint: private`，并在 VPC 内执行 qwenctl。限制公网来源 CIDR 的配置项计划中 |
| 1.8 | NAT 高可用 | 🔧 | 默认 1 个 NAT（PoC）。生产设 `network.nat_gateways: 2`，每个 AZ 一个 |
| 1.9 | Load Balancer Controller 的 IAM 剩余风险 | 👤 | 创建、删除负载均衡器和目标组，已经按集群 tag、VPC 限定。但修改监听器和规则、关联 / 解除 WAF 这几类操作只按 region 和账号限定（和上游策略一致）：控制器的角色理论上能改同账号同 region 里其他 ALB 的监听器和 WAF。建议这个集群用独立账号；共用账号时，用 SCP 或权限边界补充限制，并在 CloudTrail 上对 `ModifyListener`、`DisassociateWebACL` 设告警 |

## 2. 集群基础能力

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 2.1 | metrics-server（HPA 按 CPU / 内存、`kubectl top`） | ✅ | EKS 托管 add-on，跑在系统节点 |
| 2.2 | EBS CSI 驱动 + 默认 StorageClass `gp3`（加密、可扩容、按 AZ 建卷） | ✅ | 配置了客户 CMK 时，卷也用这个 CMK |
| 2.3 | EFS CSI（多 AZ 共享文件） | ⏳ 按需 | 目前没装。有共享文件需求时再加（EKS 托管 add-on 可用） |
| 2.4 | Kubernetes Secret 加密 | ✅ / 🔧 | EKS 默认用 AWS 托管密钥做信封加密。填了 `compliance.kms_key_arn` 时用客户 CMK |
| 2.5 | 节点安全 | ✅ | Bottlerocket（只读根文件系统）、IMDSv2 + hop limit 1、EBS 加密 |
| 2.6 | 镜像来源 | ⏳ | vLLM 镜像目前从 Docker Hub 匿名拉取（有限流风险）。计划同步到客户 ECR；chart 里的镜像改用 digest 引用 |

## 3. 可观测性与告警

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 3.1 | EKS 控制面日志（含审计） | ✅ | CloudWatch Logs，保留 30 天 |
| 3.2 | 容器日志集中采集 | ⏳ | 计划用 CloudWatch Observability add-on（Fluent Bit）。目前 Pod 删掉后日志就没了 |
| 3.3 | 指标和面板（Container Insights，含 GPU） | ⏳ | 计划中。目前只有集群内 Prometheus（保留 6 小时，给 KEDA 用） |
| 3.4 | 告警 → SNS 邮箱 | ⏳ | 节点 / Pod 异常、GPU 回退生效、ICE、扩缩容失明、ALB 5xx、WAF 拦截突增。部署时已经收集了告警邮箱 |
| 3.5 | 威胁检测（GuardDuty EKS Protection / Runtime Monitoring） | 👤 建议开启 | 账号级服务，由客户的安全团队统一开启 |
| 3.6 | 成本告警（AWS Budgets） | 👤 建议开启 | GPU 打满时成本较高。主力和回退同时打满时，成本是两者之和 |

## 4. 可靠性与容量

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 4.1 | 系统节点跨 2 个 AZ；Karpenter、CoreDNS、Load Balancer Controller 各 2 副本并有 PDB | ✅ | |
| 4.2 | vLLM、KEDA、Prometheus 的 PDB；GPU 节点到期的计划替换（`terminationGracePeriod`、低峰期滚动） | ⏳ | 目前节点 30 天到期强制替换，可能同时中断（[04 §4.2](04-architecture-and-operations.md#42-gpu-profile-和-nodepool)） |
| 4.3 | GPU 容量保障（ODCR，并给 Karpenter 配置容量预留选择器） | ⏳ / 👤 | 马来西亚的 g7e 在测试期间一直 ICE。需要客户和 AWS 确认容量，我们补上 Karpenter 的配置 |
| 4.4 | 主力 GPU 机型的选择 | 👤 | 马来西亚建议评估以 g6.12xlarge 为主（两个 AZ 都有货），g7e 作为补充。需要压测比较「每千张图成本」 |
| 4.5 | 扩缩容阈值 `C_sla`（压测得出） | ⏳ | 目前是占位值。压测后写入 `advanced.scaling.c_sla` |
| 4.6 | 回退所需的 G/VT 配额 | 👤 | 回退机型每副本 24 vCPU，打满时是主力的 3 倍（[04 §4.6](04-architecture-and-operations.md#46-配额和副本数上限)） |

## 5. 运维

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 5.1 | 部署工具：环境检查不再要求用不到的 CodeBuild 权限；已有部署（CloudFormation 栈或本机保存的配置）时拒绝重新提问，防止覆盖配置 | ✅ | |
| 5.2 | 配置备份 | ⏳ / 👤 | 计划存到 S3（带版本）和 SSM。目前请客户自己备份 `~/.qwenctl/<name>/config.json` |
| 5.3 | `status` / `test` / `scale` / `destroy` 命令 | ⏳ | 临时做法见 [04 §5](04-architecture-and-operations.md#5-使用手册) |
| 5.4 | 升级手册（EKS 版本、Karpenter、Bottlerocket、vLLM） | ⏳ | 交付包升级的注意事项见 [04 §5.7](04-architecture-and-operations.md#57-升级交付包的注意事项) |
| 5.5 | 访问权限交接（运维人员换人） | 🔧 | 新的 IAM 角色加进 `compliance.extra_admin_role_arns`，再 `--update` |
