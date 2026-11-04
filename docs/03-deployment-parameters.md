# 部署参数与交互设计：客户 CloudShell 一键部署

> 版本：v1.0 | 日期：2026-11-04 | 关联：[01 方案设计](01-architecture-design.md)、[02 可观测性设计](02-observability-design.md)、[04 架构与使用手册](04-architecture-and-operations.md)、[05 测试报告](05-test-report.md)
> 状态：交互和参数设计已确认（2026-11-03）。**哪些已经实现、哪些还在计划中，以各节的「状态」标注和 §9 为准**；当前的用法见 [04](04-architecture-and-operations.md)。

---

## 0. 结论

| 维度 | 决策 |
|---|---|
| 交付形态 | 客户在自己账号的 CloudShell（或跳板机）里运行 `./qwenctl deploy` |
| 执行位置 | **在执行 qwenctl 的客户端上执行**（2026-11-03 确认）：AWS 资源由 CloudFormation 在服务端创建；Helm chart 和 K8s 资源由本机的 helm/kubectl 安装。不使用 CodeBuild（但环境检查目前还会检查 CodeBuild 权限，是已知问题，见 [04 §7](04-architecture-and-operations.md#7-已知限制与后续计划)） |
| IaC | CDK TypeScript → CloudFormation（状态在服务端）。TypeScript 在发布交付包时预先编译，客户端只装运行依赖（实测 215 MB） |
| 客户要回答的 | **4 个问题**：网络、访问方式、副本数、可选能力。其余参数自动探测或使用默认值 |
| 目标 region | **ap-southeast-5（马来西亚）**；工具本身不绑定 region，使用 CloudShell 所在的 region |
| AZ | **固定 2 个**，部署时按 GPU 有没有货自动选择（§4）。马来西亚实测结果：apse5-az1 + apse5-az2 |
| GPU 机型 | 按 **GPU profile** 自动选择（§3.4）。马来西亚：g7e.2xlarge（仅 az2）为主，g6.12xlarge（TP=2）回退 |
| 非 GPU 业务 | 集群自带通用 CPU 节点池，客户的普通 Pod 默认调度到这里，不需要额外提问（§3.3 `general_nodes`） |
| 可观测性展示 | 设计：已启用 IdC → Grafana（AMG）；未启用 → CloudWatch，之后可以用一条命令切到 Grafana（§5）。**当前版本尚未实现**，现在能用的手段见 [04 §6](04-architecture-and-operations.md#6-可观测性如何使用) |
| 最少副本数默认值 | 2（尽量每个 AZ 一个；马来西亚的 g7e 只在 apse5-az2，两个主力副本会在同一个 AZ） |

---

## 1. CloudShell 的限制决定了执行方式

### 1.1 限制（官方文档，2026-11 核实）

| 限制 | 影响 |
|---|---|
| 20~30 分钟没有键盘或鼠标操作，会话就会断开，**正在运行的进程不算操作**；单个会话最长约 12 小时 | 整个部署远超 30 分钟，不能在 CloudShell 里直接跑 |
| 1 vCPU / 2 GiB；只有 `$HOME` 的 1 GB 会跨会话保留；Docker 可用空间很小 | 权重（37.5 GB）和 vLLM 镜像（10 GB 以上）不能经过 CloudShell |
| 预装了 `aws`、`cdk`、`kubectl`、`jq`、`python3` + boto3、`node`/`npm`、`docker`、`tmux`；**没有 `helm`、`eksctl`、`terraform`** | `qwenctl` 只依赖 Python 3 和 boto3；helm/kubectl 由 qwenctl 下载锁定版本（§1.4） |

**结论**：长时间的步骤都在服务端执行（CloudFormation 建 EKS、集群内 Job、临时 EC2），客户端只负责发起和查进度；每个阶段都可以重复执行，断线后重跑就能接着走。所以不需要额外的执行环境。

### 1.2 执行流程（客户端执行）

```
./qwenctl deploy
  ① 环境检查（只读，约 20 s）
  ② 4 个问题 + 确认页 → 配置保存到 ~/.qwenctl/<name>/config.json
  ③ 首次需要时征得同意，执行 cdk bootstrap
  ④ 分阶段安装（进度记在 ~/.qwenctl/<name>/state.json）：
     cdk-deps      安装 CDK 运行依赖（npm ci --omit=dev）
     cdk-bootstrap 只有首次需要
     cdk-deploy    CloudFormation：网络栈 + EKS 栈（集群、系统节点组、核心 add-on、Karpenter IAM）
                   + 推理服务栈（模型桶、vLLM 和同步 Job 的 Pod Identity 角色）
     kubeconfig    写到 ~/.qwenctl/<name>/kubeconfig（不覆盖客户自己的 ~/.kube/config）
     karpenter     helm 安装 karpenter-crd + karpenter（跑在系统节点上）
     nodepools     kubectl 创建 EC2NodeClass / NodePool：通用 CPU + 每个 GPU profile 一个
     verify        等 Karpenter 就绪、EC2NodeClass Ready（说明 IAM/子网/安全组/AMI 都没问题）
     metrics       helm 安装集群内 Prometheus（kube-prometheus-stack 精简版，只给 KEDA 做查询，保留 6 小时），
                   等 Prometheus 可用后才进入下一阶段
     keda          helm 安装 KEDA（跑在系统节点上）
     model-sync    集群内 Job：把锁定 revision 的模型从 HF 同步到 S3 模型桶，逐文件校验哈希；
                   S3 上已有完成标记（_COMPLETE）就直接跳过，不起 Job
     serving       每个 GPU profile 一个 vLLM Deployment（新建时主力 = minReplicas，回退 = 0；之后副本数归 KEDA 管），
                   Run:ai Streamer 从 S3 流式加载权重；PodMonitor + 每个 profile 一个 KEDA ScaledObject；
                   等到至少一个副本就绪（主力机型没货时给出提示，不会一直卡住）；
                   把就绪副本冷编译出来的编译缓存存到 S3（已有就跳过），之后的冷启动省掉约 90 s
  ⑤ 后续阶段（待实现）：网关、可观测性（CloudWatch 模式）、镜像预缓存
```

- **断线或失败**：CloudFormation 的变更会继续在服务端执行。重新打开 CloudShell 后运行 `./qwenctl deploy --resume`，用保存的配置从第一个未完成的阶段继续，不会再提问。
- **更新**：`./qwenctl deploy --update` 用保存的配置重新执行所有阶段，不再提问（升级交付包后使用）。旧版本保存的配置缺少的派生字段会自动补上，客户的选择保持不变。
- **自动化**：`--yes` 只用默认值生成配置，不会部署；要部署必须再加 `--approve`。`--plan` 只生成配置。

### 1.3 权限和安全

- 所有操作都用**执行人自己的 IAM 身份**，CloudTrail 里能看到具体是谁；不额外创建高权限的执行角色。
- CloudFormation 通过 CDK bootstrap 的执行角色创建资源。默认 bootstrap 的执行策略是 `AdministratorAccess`；客户如果已有自定义的 bootstrap（qualifier 和执行策略），在高级配置 `cdk.bootstrap_qualifier` 里指定即可沿用。
- EKS 集群关闭了「创建者自动成为管理员」，管理员只按 access entry 显式授予：执行人 + `compliance.extra_admin_role_arns`。
- 所有节点（系统节点组和 Karpenter 节点）都是 IMDSv2 + hop limit 1，EBS 加密；控制面开启 audit 日志，日志组保留期按高级配置设置。
- Karpenter 控制器角色的信任策略只允许本集群 `kube-system/karpenter` 这个 ServiceAccount 使用（Pod Identity 会话 tag）。
- `--resume` 会重新校验保存的配置，并确认当前登录的账号和 region 与部署一致。
- 本地文件（`~/.qwenctl/` 下的配置、状态、kubeconfig、Helm values）只有本人可读写。
- 推理服务：命名空间强制 Pod Security `baseline`；容器去掉全部 capabilities、启用 seccomp RuntimeDefault；权重同步 Job 以非 root 运行。**已知例外**：vLLM 容器暂时以 root 运行（确认镜像在非 root 下的缓存目录和 NCCL 行为后再收紧）。
- 权重完整性：同步时逐文件对比 HF 公布的哈希；之后的「跳过」判断信任 S3 对象元数据。信任边界是能写模型桶的主体（只有同步 Job 的角色，vLLM 只读）。模型桶开启了版本控制，被覆盖的旧版本保留 7 天，7 天内可以回滚。
- 编译缓存（S3 `compile-cache/`，torch.compile / Inductor / Triton 产物）：**里面有生成的代码**，被投毒等于在之后所有 vLLM Pod 里执行代码，而且会一直持续（新节点、重启都会加载）。所以 vLLM 的角色对它**只读**，只有部署者（qwenctl 用 CloudShell 里的凭证）在 serving 阶段写入：从刚就绪的 Pod 打包，校验压缩包（只能是普通文件和目录、不能指向目录外、数量和大小有上限），用条件写入（不覆盖已有缓存）上传。key 按配置计算（镜像 digest、模型 revision、profile、vLLM 参数），不信任 Pod 上可改的 label / annotation，否则命名空间里能改 Pod 的人可以借部署者的凭证往桶里任意位置写。恢复端（initContainer）同样只放行普通文件和目录、限制大小，任何失败都退回冷编译，不会挡住启动。vLLM 镜像里是 Python 3.12.3，早于修复 tarfile `data` 过滤器绕过的 3.12.11（CVE-2025-4517 等）。这些绕过都要借助链接，恢复端拒绝所有链接，所以不受影响。模型前缀和缓存前缀不能互相包含（同步 Job 能写模型前缀）。**剩余窗口**：被保存的 Pod 在就绪后的几秒里已经开始接流量。要清掉缓存，删除 `compile-cache/<profile>/` 下的对象即可（下次冷启动重新编译，下次 `--update` 重新保存）。
- 扩缩容：KEDA 和 Prometheus 跑在系统节点上，命名空间强制 Pod Security baseline；Prometheus 只在集群内（ClusterIP），不对外写。扩缩容指标决定 GPU 副本数（也就是成本），所以 Prometheus **只加载两个可信命名空间里的监控对象**：推理命名空间（vLLM 的 PodMonitor）和 monitoring（kube-state-metrics）。客户业务命名空间里的 PodMonitor 或者规则加载不进来，没法伪造 `vllm:*` / `kube_pod_*` 指标。只抓 KEDA 要用的指标（不装 node-exporter，不抓 kubelet、API server、CoreDNS）。KEDA 只能读自己命名空间的 Secret。Deployment 的副本数交给单独的 field manager（`qwenctl-replicas`）持有，重跑 `--update` 不会把回退 profile 从 0 重置成 1。GPU 节点总数由每个 NodePool 的 GPU 上限兜底。注意主力和回退 profile 各自最多 maxReplicas 个副本，同时打满时成本是两者之和。能在推理命名空间创建 Pod 的主体（集群管理员级别）可以造一个调度不上的 `vllm-<profile>-*` Pod 来触发回退扩容，上限仍是 maxReplicas。**生产加固待做**：chart 里的镜像改为 digest 引用或者同步到客户 ECR；用 NetworkPolicy 限制只有 KEDA 能访问 Prometheus；用 RBAC 收紧推理命名空间的写权限。
- vLLM 目前没有认证，Service 只在集群内（ClusterIP）。**交付客户前必须**加网关 + API Key，并开启 VPC CNI 的网络策略执行、只放行网关（计划在网关阶段完成）。

### 1.4 客户端工具

| 工具 | 来源 | 校验 |
|---|---|---|
| kubectl v1.36.6、helm v3.22.1 | 官方下载地址，放到 `~/.qwenctl/bin/`（按版本命名，多个部署共用） | sha256 写在 `versions.json` 里（发布时确定），不从下载源现取，下载源被篡改也能发现 |
| Helm chart：Karpenter 1.15.1（`oci://public.ecr.aws/karpenter`）、kube-prometheus-stack 92.17.1、KEDA 2.21.0（官方 https 仓库） | 各自的官方仓库 | 先 `helm pull`，按 `versions.json` 里的 sha256 校验 chart 包，再从本地文件安装（tag 可以被重新推送）；pull 时使用隔离的 registry 配置，避免本机残留的过期 public.ecr.aws 登录导致 403（实测遇到） |
| CDK | 交付包里的 `infra/`（预编译的 `dist/` + `package-lock.json`） | `npm ci --ignore-scripts` 按 lock 文件安装（依赖的安装脚本会带着执行人的凭证运行，CDK 不需要） |

所有锁定版本都在仓库根目录的 `versions.json`，Python 和 CDK 共用。

---

## 2. 交互流程

```
$ ./qwenctl deploy
[1/3] 环境检查（只读）.................................... 约 20s
  ✓ 账号 123456789012 / us-east-1 / role/CustomerAdmin
  ✓ AZ: use1-az4 (us-east-1b), use1-az6 (us-east-1d)   g7e + g6e 都有货
  ⚠ G/VT 配额 32 vCPU，已用 0 → 最多 4 个副本        需要申请提额吗？[y/N]
  ✓ 未启用 IAM Identity Center → 可观测性使用 CloudWatch（之后可以切到 Grafana）
[2/3] 4 个问题（回车 = 默认值）
  1. 网络       [1] 新建 VPC (10.100.0.0/16)  [2] 使用现有 VPC        > 1
  2. 访问方式    [1] 仅 VPC 内部  [2] 公网 HTTPS（需要 ACM 证书）         > 1
  3. 副本数      最少 [2]  最多 [4]   常驻 ≈ $4,910/月，峰值 ≈ $13.5/h  >
  4. 可选能力    [x] 异步通道  [ ] Bedrock 溢出   告警邮箱 []           >
[3/3] 确认
  （汇总表）首次部署约 20 分钟
  继续？[Y/n]
→ 开始分阶段安装（cdk-deploy → karpenter → nodepools …）；断线后运行 ./qwenctl deploy --resume
```

> 上面是设计阶段的示意（us-east-1 的例子）。实际界面按客户所在 region 显示；问题 2（访问方式）和问题 4（可选能力）的回答**会保存，但在当前版本还不生效**（网关和可观测性阶段接入），见 §3.1 的「状态」列。

原则：
- **先检查再提问**。能列出选项的就不让客户手输（VPC、证书都从列表里选）。
- **每个问题都有默认值**，客户全部按回车也能部署成功。
- **涉及成本的问题当场显示金额**。
- 第一次的回答会保存下来。之后用 `./qwenctl deploy --update` 或 `--resume`，不会再问任何问题。**不要对已有部署再运行不带参数的 `deploy` 或 `deploy --yes`**：它们会重新生成配置并覆盖保存的回答（[04 §5.1](04-architecture-and-operations.md#51-部署续跑更新)）。

---

## 3. 参数清单

### 3.1 必问（4 个，加只在特定选项下出现的追问）

| # | 问题 | 选项（粗体为默认） | 追问 | 为什么必须问 | 状态 |
|---|---|---|---|---|---|
| 1 | 网络 | **新建 VPC** / 使用现有 VPC | 选现有 VPC → 列出 VPC 让客户选；私有子网自动筛选（有 `0.0.0.0/0` 指向 NAT/TGW 的路由，并且在 §4 选出的 2 个 AZ 里），客户确认即可 | 制造业客户常有 Landing Zone，SCP 可能禁止新建 VPC 或 IGW，或者要通过 TGW/DX 连工厂网络，脚本猜不出来 | ✅ 生效（新建 VPC 已实测；现有 VPC 有单元测试，未实测） |
| 2 | API 访问方式 | **仅 VPC 内部（internal ALB）** / 公网 HTTPS | 选公网 → 从当前 region 状态为 ISSUED 的 ACM 证书里选一个，再填来源 CIDR 白名单（不填会提示风险）。选内部 → 提示用 `qwenctl test` 在集群内验证 | 要不要暴露到公网是安全决策；公网 TLS 需要客户自己的域名和证书 | ⏳ 会保存，网关阶段生效。当前服务只在集群内 |
| 3 | 副本数 | 最少 **2**；最多 **min(8, 配额上限)** | — | 这是成本的大头，需要客户自己定 | ✅ 生效（主力 profile 的 min/max；回退 profile 的 max） |
| 4 | 可选能力 | **[x] 异步通道（SQS）** [ ] Bedrock 溢出；告警邮箱（可不填） | 勾选 Bedrock → 再确认一次，并检查当前 region 能不能用这个模型。填了邮箱 → 提示客户去邮箱确认 SNS 订阅 | Bedrock 溢出意味着数据会进 Bedrock、换成另一个模型，需要客户明确同意 | ⏳ 会保存，暂不生效（马来西亚没有可用的 Bedrock 模型） |

> 配额上限 =（G/VT On-Demand 配额 − 账号里已经在运行的 G/VT 实例占用的 vCPU）÷ 8，按 2xlarge 计算。

### 3.2 自动探测，在确认页展示（回车接受，也可以改）

| 项 | 怎么得到 |
|---|---|
| 账号、Region、当前身份 | `sts get-caller-identity` 和 CloudShell 自带的 `AWS_REGION`。如果当前身份是 assumed-role，要把 session ARN 转成 role ARN，才能授予 EKS 访问权限 |
| AZ（2 个） | 见 §4 |
| GPU 机型 | 按 profile 优先级（g7e > g6e > g6-tp2）取当前 AZ 里有货的；主力机型只在一个 AZ 时给出警告 |
| 终端节点计划 | 查 VPC 里已有的终端节点、哪些路由表已经接了 S3、每个服务在哪些 AZ 提供，算出要建的清单（§4.1） |
| G 配额 | Service Quotas `L-DB2E81BA`。不够时显示差多少，并给出提额申请的 `aws service-quotas` 命令（不会自动提交） |
| 已有的容量预留（ODCR） | 按机型和选中的 AZ 查，找到了只做提示。当前版本的 EC2NodeClass 还没有配置容量预留选择器，Karpenter 不会主动使用 ODCR |
| 可观测性展示层 | 检测 IdC：已启用 → Grafana，再问一个管理员用户名；未启用 → CloudWatch（§5）。当前版本只检测和展示，展示层尚未实现 |
| 部署名前缀 | 默认 `qwen-vl`，可以用 `--name` 指定。设计是和已有部署重名时提示客户；**当前的重名检测无效**（已知问题） |
| 新 VPC 的 CIDR | 默认 `10.100.0.0/16` |
| 模型 | 默认 Qwen3.5-35B-A3B-FP8（锁定在 `versions.json`）。部署时不问；换模型的 `qwenctl model switch` 还没有实现 |
| CDK bootstrap | 已有 → 直接用；没有 → 征得客户同意后执行 |

### 3.3 高级配置（`config.yaml`，全部可选，不写就用默认值）

> 下面每一项都会被校验，写错会直接报错。但其中一部分在当前版本**还没有接入，填了也不生效**，已在注释里标为「（未生效）」。部署后要修改，改 `~/.qwenctl/<name>/config.json` 里的 `advanced` 部分，再运行 `./qwenctl deploy --update`（见 [04 §4.8](04-architecture-and-operations.md#48-可以调的参数)）。

```yaml
serving:
  max_model_len: 32768
  max_images_per_request: 4
  max_image_pixels: 2097152          # 约 2 MP（未生效：还没有传给 vLLM，网关阶段统一校验）
  gpu_memory_utilization: 0.92
  max_num_seqs: 128                  # 单副本最大并发序列（和 GPU profile 的上限取较小者）
scaling:
  c_sla: {}                          # key 是 GPU profile 名（如 g7e-1gpu: 40）；为空时用内置的占位值。压测后手工写入实测值（qwenctl tune 未实现）
  target_utilization: 0.65
  waiting_threshold: 4
  kv_cache_threshold: 0.8            # （未生效：KV 使用率触发器还没有接入）
  warm_node_minutes: 10              # 空闲 GPU 节点保留多久（保温）
  schedule: null                     # 定时预扩容（未生效）。例：{timezone: Europe/Berlin, start: "0 8 * * 1-5", end: "0 18 * * 1-5", replicas: 4}
capacity:
  extra_fallback_types: []           # 例：[p5.4xlarge]，作为 ICE 时的第三档回退（未生效）
general_nodes:                       # 通用 CPU 节点池（客户的非 GPU 业务）
  arch: [amd64]                      # 确认业务镜像支持 arm64 后可以加上 arm64（Graviton 更便宜）
  capacity_types: [on-demand]        # 业务能容忍中断时可以加上 spot
  cpu_limit: 200                     # vCPU 上限，防止成本失控
network:
  cidr: 10.100.0.0/16                # 新建 VPC 时使用，必须是 /16 或 /17
  nat_gateways: 1                    # 运行时的流量主要走 VPC Endpoint，PoC 用 1 个 NAT 就够；生产环境改成 2
  eks_endpoint: public-and-private   # 或 private（这时客户要用 CloudShell VPC 环境执行 kubectl）
  interface_endpoints: auto          # auto = 新建 VPC 时创建、现有 VPC 时不创建；on / off 强制（§4.1）
compliance:
  kms_key_arn: null                  # 客户自己的 CMK（密钥策略要允许 logs 等服务使用）；为空时用 AWS 托管加密。GPU 节点的镜像层和临时存储在实例存储（NVMe）上，由 Nitro 硬件加密，不受 CMK 约束
  tags: {}                           # 客户 tag policy/SCP 要求的强制标签
  extra_admin_role_arns: []
observability:
  amp: auto                          # auto = 只在 Grafana 模式下启用；on = CloudWatch 模式下也提前写入 AMP（未生效）
  log_retention_days: {app: 14, access: 30, coldstart: 90}   # 当前只有 access 生效：EKS 控制面日志和 VPC Flow Logs 的保留期
image:
  source_registry: docker.io         # （未生效）Docker Hub 匿名拉取有限流（集群出口 NAT 的 IP 也可能和其他业务共享）
  credentials_secret_arn: null       # （未生效）
cdk:
  bootstrap_qualifier: null
```

### 3.4 锁定，不开放修改（`versions.json`）

- K8s 1.36、EKS add-on、Bottlerocket v1.68.1、kubectl / helm、各 Helm chart（Karpenter、kube-prometheus-stack、KEDA）的版本和 sha256，vLLM `v0.32.0` 的镜像 digest，模型 revision
- **GPU profile**（`cli/qwenctl/catalog.py`）：每种 GPU 机型对应的 TP 大小、每节点副本数、NodePool weight，详见 [01 §2.3](01-architecture-design.md#23-马来西亚ap-southeast-5实测2026-11-03)
- 节点分层：系统节点（托管节点组，带 `CriticalAddonsOnly` 污点）、通用 CPU 节点（不带污点）、GPU 节点（带 `nvidia.com/gpu` 污点），见 [01 §3.1](01-architecture-design.md#31-节点分层gpu-推理和客户业务共用一个集群)
- IAM 策略、安全组规则、加密、S3 Block Public Access、IMDSv2
- 不记录 prompt；健康检查参数和优雅下线参数

原因：这是验证过的组合，开放出去要支持的情况会多到管不过来。升级时发布新版本的交付包；编译缓存的 key 包含镜像 digest 和模型 revision，升级后第一个副本会重新编译，`--update` 时自动保存新缓存。

### 3.5 自动生成，不问

- 已实现：模型桶名（CloudFormation 生成）、权重和编译缓存在 S3 上的路径、K8s 资源名（命名约定见 [04 附录](04-architecture-and-operations.md#附录命名约定)）。
- 计划中：API Key（存 Secrets Manager）、ECR 仓库名、EBS 快照 ID（存 SSM）。

---

## 4. AZ 选择（固定 2 个）

**算法**
1. 列出当前 region 的标准 AZ，排除 Local Zone 和 Wavelength Zone。
2. 用 `describe-instance-type-offerings` 查每个 AZ 里各个 GPU profile 的代表机型有没有货。
3. 打分：先比 AZ 里最高档的 profile，再比可用的 profile 数量。例如「g7e + g6」排在「只有 g6」前面。如果用现有 VPC，只考虑有合格私有子网的 AZ。
4. 取分数最高的 2 个；同分时按 AZ ID 排序，保证每次结果一样。
5. 只有 1 个 AZ 有 GPU 时：警告「GPU 副本只能放在一个 AZ 里」，客户确认后继续（系统节点仍然跨 2 个 AZ）。一个都没有时：退出（设计是同时列出有货的 region，当前版本还没有做）。

**实现要点**
- 程序内部始终用 **AZ ID**（例如 `use1-az4`），展示时同时显示 AZ 名。同一个 AZ 名在不同账号里对应的物理 AZ 不一样，01 文档里写的 `1a/1c/1d` 是我们自己账号的结果，不能直接用。
- Karpenter：EC2NodeClass 直接指定子网 ID，**不给客户已有的子网打 tag**；NodePool 用 `topology.k8s.aws/zone-id` 限定 AZ。
- 副本分布：`topologySpreadConstraints` 设 `maxSkew: 1` 和 `whenUnsatisfiable: ScheduleAnyway`。正常情况下两个 AZ 各一个副本；某个 AZ 没有容量时，扩容也不会被卡住。

**2 个 AZ 的代价和应对**：可用的容量池比 3 个 AZ 少三分之一。所以优先选两种机型都有货的 AZ，让每个 AZ 里都能从主力机型回退。还不够的话，在 `capacity.extra_fallback_types` 里加上更多机型。

**马来西亚实测结果**：az2 同时有 g7e 和 g6.12xlarge，az1 只有 g6.12xlarge，az3 没有 GPU → 选 az1 + az2。g7e 只能放在 az2；g6.12xlarge 在两个 AZ 兜底。

### 4.1 现有 VPC 的检查和终端节点策略

选现有 VPC 时，下面任何一项不满足，这个 VPC 就会被拒绝，并且逐个子网说明原因：
- 开启了 DNS 解析和 DNS 主机名（EKS 节点注册、私有 DNS 都依赖这两项）
- 至少 2 个 AZ 各有一个带 NAT/TGW 出口、空闲 IP ≥ 128 的私有子网，并且其中有 GPU 机型
- G/VT 配额够在这些 AZ 的主力机型上启动 1 个副本（主力机型可能因为 AZ 变化变成 g6.12xlarge，每节点 48 vCPU）
- 这些 AZ 里有系统节点机型

终端节点只做加法：

| 终端节点 | 新建 VPC | 现有 VPC |
|---|---|---|
| S3 网关 | 创建 | 只加到**还没有接 S3 的**所选私有子网路由表上；全部已接则不建 |
| 接口（ecr.api、ecr.dkr、sts、eks-auth、sqs…） | 创建（服务在两个 AZ 都提供才建） | **默认不建**：私有 DNS 对整个 VPC 生效，会改变客户其他业务访问 STS/ECR 的路径，还可能让基于 `aws:SourceIp` 的 IAM 策略失效。客户确认后可以用 `interface_endpoints: on` 打开，这时安全组放行整个 VPC（主 CIDR + 附加 CIDR） |

已经存在的同名接口终端节点不重复创建。确认页会逐项列出「创建什么、跳过什么、为什么」。

---

## 5. 可观测性展示层

> **状态**：设计已确认，当前版本**尚未实现**（只检测 IdC 并在确认页展示结果）。现在能用的观测手段见 [04 §6](04-architecture-and-operations.md#6-可观测性如何使用)。

- 规则：本 region 有 AMG 且客户已启用 IdC → Grafana 模式；否则 → CloudWatch 模式。
- **马来西亚没有 AMG**，所以默认是 CloudWatch 模式。以后切 Grafana 的方案待定（01 §13 第 8 条）：在新加坡建 AMG 跨 region 查询，或者在集群内自建 Grafana。
- 两种模式的**采集层完全一样**，只有展示层不同。CloudWatch 模式切到 Grafana 只需要 `qwenctl observability grafana`，只往上加组件，不重建集群，也不中断推理。
- 详细设计（两种模式的组件对比、告警怎么落地、切换步骤、切换的代价）见 [02 §1.2](02-observability-design.md#12-展示层两种模式2026-11-03-确认)。

---

## 6. 环境检查清单

| 检查项 | 不通过时 |
|---|---|
| 身份和权限 | 用 `iam simulate-principal-policy` 抽查关键操作。权限不够就列出缺的权限并退出。SCP 的拦截无法提前判断，部署时如果报错会给出提示 |
| GPU 有没有货 | 一个 AZ 都没有 → 退出（当前版本不会列出有货的 region）；只有一个 AZ → 警告并让客户确认 |
| G/VT 配额 | 不够 → 显示差多少，给出提额申请命令；同时把最多副本数压到当前配额允许的范围（按主力机型计算，见 [04 §4.6](04-architecture-and-operations.md#46-配额和副本数上限)） |
| VPC、EIP 配额（新建 VPC 时） | 设计：不够 → 退出，并告诉客户要提哪个配额。**当前版本未检查**，配额不够时会在 CloudFormation 阶段报错 |
| CDK bootstrap | 没有 → 征得同意后执行；客户有自定义 qualifier → 从 `config.yaml` 读取 |
| 同名部署 | 设计：已经存在 → 进入更新流程，或者让客户换个名字。**当前版本检测无效**（已知问题），对已有部署请直接用 `--update` |
| ACM 证书（选公网时） | 没有 ISSUED 状态的证书 → 提示先申请，或者改用内部访问 |
| Bedrock 模型（勾选溢出时） | 当前 region 用不了 → 自动取消勾选并提示 |
| IdC | 只用来决定展示层，不会让检查失败 |
| ODCR | 只做提示 |

---

## 7. 命令一览

| 命令 | 作用 | 状态 |
|---|---|---|
| `preflight` | 只做环境检查（只读） | ✅ |
| `deploy` | 首次部署。`--resume` 从中断处继续；`--update` 用保存的配置重新执行全部阶段；`--plan` 只生成配置；`--yes --approve` 无人值守部署 | ✅ |
| `status` | 当前阶段、各组件状态、访问地址、获取 API Key 的命令 | ⏳ 临时用 [04 §6.2](04-architecture-and-operations.md#62-日常巡检) 的命令 |
| `test` | 在集群内发测试请求（选了内部访问时用它验证） | ⏳ 临时用 `e2e/infer_test.py`（[04 §5.2](04-architecture-and-operations.md#52-调用推理服务)） |
| `scale --min N --max M` | 调整副本范围，会按配额校验 | ⏳ 临时做法见 [04 §5.4](04-architecture-and-operations.md#54-调整副本范围扩缩容参数scale-命令上线前的临时做法) |
| `tune` | 压测后写入 `C_sla` 等扩缩容参数 | ⏳ |
| `model switch <preset>` | 换模型：同步新权重，然后滚动更新 | ⏳ |
| `observability grafana` | 从 CloudWatch 模式切到 Grafana 模式 | ⏳ |
| `destroy` | 先把 Karpenter 节点缩到 0 并等实例终止，再删除各个栈；EBS 快照、ECR 镜像、S3 数据删除前逐项确认 | ⏳ 临时做法见 [04 §5.6](04-architecture-and-operations.md#56-清理环境destroy-命令上线前的临时做法) |

> 为什么 `destroy` 要先缩节点：Karpenter 创建的 GPU 实例不在 CloudFormation 的管理范围里。直接删栈会留下没人管的实例继续计费，VPC 也会因为还有网卡而删不掉。

---

## 8. 配置和状态存在哪里

**当前版本**：

| 内容 | 位置 | 说明 |
|---|---|---|
| 完整配置（交互回答和 `config.yaml` 合并后的结果） | `~/.qwenctl/<name>/config.json` | **只有这一份**，`--resume` / `--update` 都靠它。请自行备份（[04 §5.1](04-architecture-and-operations.md#51-部署续跑更新)） |
| 部署进度 | `~/.qwenctl/<name>/state.json` | 丢了也没关系，`--update` 会重新执行所有阶段 |
| kubeconfig、Helm values、chart 缓存 | `~/.qwenctl/<name>/`、`~/.qwenctl/charts/` | 只有本人可读写 |
| AWS 资源 | CloudFormation 栈 `<name>-network` / `-eks` / `-serving` | 栈的输出（桶名等）每次都从 CloudFormation 读取 |

**计划中**：配置存到 artifacts 桶 `config/<name>.yaml`（开启版本控制，换一个人的 CloudShell 也能读到），快照 ID 等存到 SSM Parameter Store `/<name>/...`，API Key 存 Secrets Manager（`qwenctl status` 只显示获取命令，不打印明文）。届时本地只是缓存。

---

## 9. 实现进度（2026-11-04）

| 部分 | 状态 | 位置 |
|---|---|---|
| `qwenctl preflight`：环境检查（只读） | ✅ 已在 ap-southeast-5 实测 | `cli/qwenctl/preflight.py`、`aws_gateway.py` |
| `qwenctl deploy`：4 个问题 + 确认页 + 保存配置 | ✅ | `cli/qwenctl/questions.py`、`render.py`、`store.py` |
| 部署配置契约（Python → CDK） | ✅ zod 校验 + 契约测试 | `cli/qwenctl/models.py`、`infra/lib/config.ts`、`infra/test/contract.test.ts` |
| CDK 网络栈（新建 / 现有 VPC + 终端节点） | ✅ 新建 VPC 已真实部署；现有 VPC 只有单元测试 | `infra/lib/network-stack.ts` |
| CDK EKS 栈（集群 1.36、系统节点组、核心 add-on、Karpenter IAM 和中断队列） | ✅ 已真实部署 | `infra/lib/eks-stack.ts`、`karpenter-iam.ts` |
| 客户端安装流程（11 个阶段、断点续跑、锁定版本工具） | ✅ 已真实部署 | `cli/qwenctl/installer.py`、`toolchain.py`、`manifests.py` |
| 推理服务层：模型桶 + Pod Identity，权重同步 Job，每个 GPU profile 一个 vLLM Deployment（S3 流式加载） | ✅ 已真实部署 | `infra/lib/serving-stack.ts`、`cli/qwenctl/serving.py`、`assets/model_sync.py` |
| 弹性伸缩：集群内 Prometheus + KEDA，主力按负载扩缩容，主力没货时自动回退 | ✅ 已真实部署（按负载扩缩容还没有压测） | `cli/qwenctl/addons.py`、`autoscaling.py` |
| 冷启动：GPU 节点 NVMe RAID0 + SOCI 并行拉取、编译缓存放 S3 | ✅ 已真实部署，新节点 6 分 02 秒 → 3 分 38 秒 | `cli/qwenctl/manifests.py`、`compile_cache.py`、`assets/compile_cache_restore.py` |
| 网关（API Key）、可观测性（CloudWatch 模式）、镜像预缓存 / ECR | ⏳ | — |
| `status` / `test` / `scale` / `destroy` 等命令 | ⏳ | — |

测试数据（部署、节点池扩缩容、GPU 回退、冷启动各阶段、推理性能、安全评审、自动化测试）见 [05 测试报告](05-test-report.md)；架构和用法见 [04 架构与使用手册](04-architecture-and-operations.md)。
