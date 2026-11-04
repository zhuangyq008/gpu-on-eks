# 测试报告：EKS 上弹性部署 Qwen3.5-35B-A3B（PoC 第一阶段）

> 版本：v1.0 | 日期：2026-11-04 | 代码版本：`main` @ b9afd5a 及之后 | 测试 region：ap-southeast-5（马来西亚）
> 关联：[04 架构与使用手册](04-architecture-and-operations.md)（实现说明）、[01 方案设计](01-architecture-design.md)（设计目标）

---

## 0. 结论摘要

| 验证项 | 目标 | 结果 | 结论 |
|---|---|---|---|
| CloudShell 一键部署 | 回答 4 个问题即可部署，断线可续跑 | 首次部署约 17 分钟（网络、EKS、Karpenter）；11 个阶段都验证过 `--resume` / `--update` | ✅ 通过 |
| 非 GPU 业务共用集群 | 客户普通 Pod 不做任何设置就能运行 | Pod 从 apply 到 Ready 37 s（Karpenter 新拉 CPU 节点），删除业务后 130 s 回收节点 | ✅ 通过 |
| 主力 GPU 没货时自动回退 | 不需要人工干预 | g7e 整个测试期间都没有容量（ICE）。KEDA 自动扩出 g6.12xlarge 回退副本，Karpenter 41 s 拉起节点 | ✅ 通过 |
| 新节点冷启动 | ≤ 3 分钟 | 基线 6 分 02 秒 → **优化后 3 分 38 秒**（复测 3 分 34 秒） | ⚠️ 改善 40%，离目标还差约 40 s |
| 推理正确性 | 图片 → 结构化 JSON | 设备铭牌 7 个字段全部识别正确 | ✅ 通过 |
| 推理性能（g6.12xlarge TP=2） | — | 铭牌图片 → JSON 1.16 s；8 路并发 TTFT p50 0.46 s，输出 314 tok/s | 作为回退机型可用 |
| 安全 | 没有遗留 CRITICAL/HIGH | 每次部署前都做安全评审（3 个阶段、5 次），另有 2 轮代码评审；没有 CRITICAL，HIGH 全部修复 | ✅ 通过 |
| 自动化测试 | 覆盖率 ≥ 80% | Python 349 个，覆盖率 98%（Python 3.12；3.9 下跑 342 个，权重同步脚本的测试需要 huggingface_hub，所以跳过）；CDK 52 个 | ✅ 通过 |

**这一阶段没有测到的内容**（详见 §10）：
- g7e 实机：一直 ICE，Blackwell 驱动和性能都没测到；
- 按负载扩缩容的压测：还没有测过 `C_sla`，也没有测过扩缩容曲线；
- 网关和 API Key：未实现，**服务目前没有认证，只能在集群内访问**；
- CloudWatch 可观测性：未实现；
- `destroy` 等运维命令：未实现。

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| Region / AZ | ap-southeast-5；apse5-az1 + apse5-az2（部署时按 GPU 供货自动选出） |
| 网络 | 新建 VPC 10.100.0.0/16；1 个 NAT；S3 网关终端节点 + ecr.api、ecr.dkr、sts、eks-auth、sqs 接口终端节点 |
| EKS | 1.36；系统节点组 2 × m7g.large（Bottlerocket，arm64） |
| GPU 节点 OS | Bottlerocket v1.68.1 NVIDIA 变体（驱动 580.178.04 / CUDA 13.0） |
| 组件版本 | Karpenter 1.15.1、KEDA 2.21.0、kube-prometheus-stack 92.17.1、kubectl v1.36.6、helm v3.22.1 |
| 推理 | vLLM v0.32.0（镜像按 digest 锁定，从 Docker Hub 拉取）；模型 `Qwen/Qwen3.5-35B-A3B-FP8`（revision 锁定，37.5 GB） |
| GPU profile | g7e-1gpu（g7e.2xlarge，只在 apse5-az2 有）为主力；g6-tp2（g6.12xlarge，4×L4，TP=2）为回退 |
| 约束 | 为控制测试成本，**整个测试期间同时运行的 GPU 节点不超过 2 台**（配额本身更大） |
| 执行端 | 一台 us-east-1 的开发机（等同于客户的 CloudShell）。性能数据都在集群内测量，原因见 §7 |

---

## 2. 测试方法

- **部署**：`./qwenctl deploy --yes --approve`（全部用默认值）。之后每次改动都用 `--update` / `--resume` 在同一个环境上验证。
- **冷启动计时**：时间戳都取自 Kubernetes 和 vLLM 自己的记录，精度为秒：
  - Karpenter NodeClaim 的 `Launched` / `Registered` / `Ready` 状态时间；
  - Pod 的 `PodScheduled` / `Initialized` / `Ready` 状态时间；
  - kubelet 的 `Pulled` 事件（里面带拉取耗时）；
  - vLLM 启动日志里的 `Model loading took`、`torch.compile took`、`init engine ... took`。
- **新节点**：每轮测试前先删掉 GPU 节点，确保从「没有节点、没有镜像」开始。
- **推理**：用 [`e2e/infer_test.py`](../e2e/infer_test.py) 在 vLLM 容器里直接请求本地端口（`kubectl exec`），测纯文本、图片 → JSON、8 路并发三项。
- **代码质量**：pytest（Python 3.9 / 3.12）、jest（CDK）；配置契约测试会让 Python 端真实生成配置，再交给 CDK 校验和 synth。

---

## 3. 部署测试

| 阶段 | 结果 | 耗时 |
|---|---|---|
| cdk bootstrap | ✅ 第一次失败，修复后用 `--resume` 续跑成功（见 §9 问题 1） | ~1 分钟 |
| 网络栈 | ✅ VPC、2 个 AZ 的子网、NAT、终端节点、Flow Logs | ~3 分钟 |
| EKS 栈 | ✅ 集群、2 台系统节点（已核实 IMDSv2 hop limit 1、EBS 加密）、5 个 add-on 全部 ACTIVE | ~11 分钟 |
| Karpenter + 节点池 | ✅ chart 的 sha256 校验通过；2 个控制器副本分布在两个 AZ；2 个 EC2NodeClass 和 3 个 NodePool 都 Ready | ~2 分钟 |
| **小计（集群可用）** | | **~17 分钟** |
| Prometheus + KEDA | ✅（等 Prometheus 就绪后才装 KEDA） | ~5 分钟 |
| 权重同步（HF → S3，一次性） | ✅ 37.5 GB / 27 个文件，418 s（约 90 MB/s），**逐文件哈希校验全部通过** | ~7 分钟 |
| 推理服务（首个副本就绪 + 保存编译缓存） | ✅ | 5~6 分钟（新节点） |

**可重复执行**：`--update` 先后在同一环境上跑了 10 多次。CloudFormation 没有变更时直接跳过；已经同步过的权重直接跳过（不再拉起 CPU 节点、拉 9 GB 镜像）；正在服务的副本不会重启。

---

## 4. 非 GPU 业务（通用 CPU 节点池）

部署 2 个 nginx 副本，不带任何 nodeSelector 或 toleration。

| 项 | 结果 |
|---|---|
| 扩容 | Pod 从 apply 到 Ready **37 s**。Karpenter 选了 c7i-flex.large（x86、按需、apse5-az2）：创建 → 启动 3 s → 注册 9 s → 就绪 18 s |
| 缩容 | 删除业务后 **130 s** 回收节点（空闲 1 分钟，再加上驱逐和终止） |
| 隔离 | 业务 Pod 不会被调度到 GPU 节点（GPU 节点带 `nvidia.com/gpu` 污点）和系统节点（带 `CriticalAddonsOnly` 污点） |

---

## 5. GPU 节点与自动回退

### 5.1 容量（ICE）

- 整个测试期间（11-03 至 11-04），apse5-az2 的 g7e.2xlarge 一直返回 `InsufficientInstanceCapacity`。马来西亚只有这一个 AZ 提供 g7e，没有地方可换。
- 所以本报告的 GPU 数据全部来自回退机型 g6.12xlarge。**g7e（Blackwell）的驱动兼容性和推理性能都还没有实测。** 驱动是 R580，官方支持 Blackwell。

### 5.2 KEDA 自动扩出回退副本

场景：主力 g7e 的 Pod 已经调度不上 24 分钟，回退 g6-tp2 的副本数为 0。装好 Prometheus 和 KEDA 之后，没有任何人工操作。

| 时间（UTC） | 事件 | 间隔 |
|---|---|---|
| 02:47:31 | qwenctl 创建 ScaledObject | — |
| 02:47:32~51 | KEDA 的查询失败（Prometheus 刚启动）。主力 ScaledObject 连续失败后启用 fallback（KEDA 日志：`Suppressing error, using fallback metrics`，`behavior=currentReplicasIfHigher`）：保持当前副本数，**没有误缩容** | — |
| 02:48:02 | KEDA 把 vllm-g6-tp2 从 0 扩到 1，触发条件是「主力 Pod 调度不上超过 180 s」的个数为 1 | 31 s |
| 02:48:03 → 02:48:44 | Karpenter 创建 NodeClaim，g6.12xlarge 启动、注册、Ready | 41 s |
| 02:48:44 → 02:54:04 | 拉镜像、加载权重、引擎初始化（当时还没做冷启动优化，见 §6） | 5 分 20 秒 |

- 新部署的情况下，还要加上主力 Pod 被判定为「调度不上」的 180 s，以及指标抓取和 KEDA 轮询（约 30 s）。按优化后的冷启动 3 分 38 秒算，主力没货时**约 7 分钟后有回退副本在服务**。
- 重跑 `--update` 不会打断回退副本，3 次复测的 Pod 都没有重启。
- 回退副本在主力恢复后保留 10 分钟（`cooldownPeriod: 600`），避免主力还没就绪就断流。这一段**还没有实测**：g7e 一直没有货。

---

## 6. 冷启动

### 6.1 新节点冷启动（扩容触发 → 新节点上的副本 Ready）

同一个 Pod 模板，每轮都从没有节点开始；镜像都从 Docker Hub 拉取。起点是副本数变成 1 的时刻（KEDA 扩容，或者测试时用注解设定）。各段按时间戳计算，精度 1 秒，相加等于合计（±1 s）。

| 阶段 | 基线 | 第 1 步：NVMe + SOCI 并行拉取 | 第 2 步：+ S3 编译缓存 |
|---|---|---|---|
| 副本数变化 → GPU 节点就绪（Karpenter 决策 + EC2 启动 + 注册） | 42 s | 38 s | 37 s |
| 拉镜像（9.2 GB） | 89 s | 54 s | 52 s |
| 恢复编译缓存（initContainer）+ 启动 vLLM 容器 | — | — | 9 s（恢复 3 s：34 MB、4,457 个文件） |
| 进程启动 + 权重（S3 流式加载约 37 s） | 93 s | 93 s | 84 s |
| 引擎初始化 + API 启动 → Ready | 139 s | 134 s | **36 s** |
| └ 其中引擎初始化（`init engine`） | 126 s | 124 s | 25 s |
| └ 其中 torch.compile / 首次 profiling（kernel JIT） | 31 s / 60 s | 31 s / 59 s | **0.5 s / 0.7 s** |
| **合计** | **6 分 02 秒** | **5 分 19 秒** | **3 分 38 秒**（复测 3 分 34 秒） |

- **第 1 步**：GPU 节点的容器镜像、kubelet、Pod 临时存储都放到本地 NVMe 上（Karpenter `instanceStorePolicy: RAID0`）；Bottlerocket SOCI 开并行下载和解压。拉镜像快了 40%。EBS 数据盘也从 150 GiB / 1000 MB/s 降到 30 GiB gp3 默认规格。
- **第 2 步**：首个副本冷编译出来的 torch.compile / Triton 缓存由 qwenctl 存到 S3，之后新起的副本先恢复它再启动。引擎初始化从 2 分多钟降到 25 s。
- 「首次 profiling 60 s」以前以为花在 CUDA graph 或 KV cache 上，实测几乎全是 Qwen3.5 GDN / MoE 的 Triton kernel 即时编译。

### 6.2 其他启动路径

| 路径 | 实测 | 说明 |
|---|---|---|
| 未优化基线：节点已在，镜像从 Docker Hub 拉、权重从 HF 下载 | 7 分 17 秒 | 11-03。HF 下载 137 s，改成 S3 流式加载后 37 s |
| 保温节点（节点和镜像都在，权重从 S3 加载，无编译缓存） | 3 分 50 秒 | 11-04 |
| 同一个 Pod 内重启容器（编译缓存还在） | 2 分 07 秒 | 等同于「保温节点 + 编译缓存」 |

### 6.3 离目标还差多少

新节点目标 ≤ 3 分钟，还差约 40 s。剩下的大头：
- 进程启动约 47 s，其中多模态预处理预热 16 s；
- 拉镜像 52 s：可以改从区域内的 ECR 拉，或者把镜像预缓存到 EBS 快照（要实测快照懒加载的影响）；
- 权重 37 s，g6.12xlarge 上约 1 GB/s。g7e 网络是 50 Gbps，预计会更快，待实测。

---

## 7. 推理功能与性能（g6.12xlarge，TP=2）

配置：`max-model-len=8192`，`max-num-seqs=16`，每请求最多 2 张图（g6-tp2 profile 的上限），`gpu-memory-utilization=0.92`，请求里关闭 thinking。

| 测试 | 结果 |
|---|---|
| 纯文本（输出 46 token） | 0.55 s |
| 设备铭牌图片 → JSON（7 个字段） | **1.16 s**，全部正确（制造商、型号、序列号、功率、电压、频率、质检结果） |
| 8 路并发（每路 1 张图，输出 128 token） | TTFT p50 0.46 s / max 0.61 s；墙钟 3.3 s；输出 **314 tok/s** |
| 单路长输出（400 token，手工 curl 测，不在验收脚本里） | 约 13 ms/token |
| KV cache 容量 | 每卡 1.29 GiB = 74,898 token；8K 上下文下最大并发约 9 路 |

- **测量方式要注意**：经 `kubectl port-forward` 从 us-east-1 测，会多出 0.6~1.8 s 的跨 region 开销（纯文本 1.23 s、8 路并发 TTFT 2.1 s）。早先记录的「铭牌图片 2.1 s」也包含这部分开销，实际是 1.16 s。上表的数据都是在集群内测的。
- 已排除的干扰：GPU 没有降频（2040 MHz，功耗 60 W / 上限 72 W）；Prometheus 每 5 秒抓一次指标只要 6 ms；g6.12xlarge 上所有 GPU 对都不支持 P2P（custom allreduce 一直是关的），每台节点都一样。
- g6-tp2 用默认参数（32K 上下文、128 并发、4 张图）会在分配 KV cache 时启动失败，所以这个 profile 带了更小的参数上限（见 §9 问题 4）。

---

## 8. 安全评审

每次部署和推送前都由独立的安全评审检查（IAM、密钥、认证、输入校验与注入、S3 公开访问、CORS、供应链）。

| 轮次 | 范围 | CRITICAL / HIGH | MEDIUM / LOW | 处理结果 |
|---|---|---|---|---|
| 1（11-03） | 部署工具、CDK、Karpenter | 0 / 0 | 5 / 5 | 全部修复 |
| 2（11-04） | 弹性伸缩（Prometheus、KEDA） | 0 / 0 | 3 / 8 | 修复 10 个；1 个 LOW 接受（副本数交接遇到冲突时不重试，会安全失败） |
| 3（11-04） | 冷启动（编译缓存、RAID0、SOCI） | 0 / 0 | 2 / 6 | 修复 7 个；1 个 LOW 留到生产加固（桶策略显式拒绝其他主体写缓存前缀） |
| 代码评审 ×2 | 弹性伸缩、冷启动 | 0 / 3（弹性伸缩 1 个、冷启动 2 个） | 若干 | HIGH 和 MEDIUM 全部修复 |

重点修复：
- Prometheus 只加载两个可信命名空间里的监控对象，客户业务命名空间伪造不了决定 GPU 副本数的指标；
- KEDA 不能读全集群的 Secret；
- 编译缓存里有生成的代码，vLLM 只读、只由部署者写入；key 按配置计算；上传和恢复都校验压缩包；
- 重跑部署不会重置副本数；
- 本地文件只有本人可读写。

**已知例外，交付生产前要处理**（见 [04 §7](04-architecture-and-operations.md#7-已知限制与后续计划)）：
- vLLM 没有认证，只在集群内可访问；
- 没有 NetworkPolicy；
- vLLM 没有限制可拉取的图片域名（SSRF）；
- vLLM 容器以 root 运行；
- chart 里的镜像用的是 tag，没有用 digest；
- EKS API 端点默认公网 + 私网（受 IAM 保护；CloudShell 需要公网端点）；
- 编译缓存的剩余窗口：被保存缓存的副本在就绪后的几秒里已经开始接流量；
- GPU 节点的镜像和临时存储在本地 NVMe 上，由 Nitro 硬件加密，不是客户的 CMK。

---

## 9. 测试中发现并已修复的问题

| # | 问题 | 发现方式 | 修复 |
|---|---|---|---|
| 1 | `cdk bootstrap` 在 `infra/` 目录下会执行 app，但没有传部署配置 | 真实部署 | bootstrap 也传 `-c configFile` |
| 2 | 本机残留的 public.ecr.aws 登录已过期，`helm pull` 返回 403 | 真实部署 | helm 使用隔离的 registry 配置 |
| 3 | 权重同步 Pod 启动很慢：通用节点的 gp3 默认只有 125 MB/s，解压 9 GB 镜像很慢 | 真实部署 | 已同步过就跳过 Job（S3 完成标记） |
| 4 | g6-tp2 用默认参数时 KV cache 不够，vLLM 启动失败 | 真实部署 | 每个 profile 带参数上限（8K / 16 并发 / 2 张图） |
| 5 | 旧版本保存的配置缺少新增的配置项，`--update` 报错 | 真实部署 | 迁移配置时合并默认值，客户已填的值优先 |
| 6 | 同步 Job 的 Pod 模板不可变，模板变了 apply 被拒绝 | 真实部署 | Job 名包含整个模板的哈希 |
| 7 | Prometheus 还没就绪，KEDA 就开始查询 | 真实部署 | 等 Prometheus 可用后再装 KEDA |
| 8 | 重跑 `--update` 会把回退副本从 0 重置成 1，白白拉起一台 GPU 节点 | 安全评审，集群上复现 | `replicas` 交给单独的 field manager 持有 |
| 9 | 回退用「Pending 超过 180 s」判断，拉镜像期间 Pod 也是 Pending，主力正常扩容也会误触发回退 | 代码评审 | 改为「调度不上」（`kube_pod_status_unschedulable`） |
| 10 | 有 3 个以上 profile 时，所有回退会同时扩出 | 代码评审 | 回退按顺序接力 |
| 11 | 只带 `replicas` 的部分对象包在 List 里 apply，被 kubectl 客户端校验拒绝 | 真实部署 | 改为逐个对象 apply |
| 12 | 编译缓存：Pod 的 annotation 可以被改，部署者的凭证可能被引导去写桶里的任意位置 | 安全评审 | key 按配置计算，profile 按 Pod 名识别 |
| 13 | 恢复缓存时网络中断，会留下半个缓存目录 | 代码评审 | 任何失败都清空目录，然后冷编译 |
| 14 | 打包缓存时写盘失败，不回收子进程、不删临时文件 | 代码评审 | 统一清理 |
| 15 | 性能数据被误判为 decode 变慢 | 测试复核 | 改为在集群内测量（§7） |

---

## 10. 未覆盖的测试与风险

| 项 | 状态 | 风险 / 下一步 |
|---|---|---|
| g7e（主力机型）实机 | ❌ 一直 ICE | 驱动兼容性、TP=1 性能、`C_sla` 都未知。建议用 ODCR 锁定 az2 的 g7e 容量后再测 |
| 按负载扩缩容（压测） | ❌ | 扩缩容阈值 `C_sla` 目前是占位值（g7e 32、g6-tp2 8）。要做阶跃、尖刺、锯齿三种流量模型的压测，再用实测值覆盖 |
| 主力恢复后回退缩回 0 | ❌ | 设计是冷却 10 分钟，但 g7e 一直没货，无法触发 |
| 网关 + API Key + NetworkPolicy | ❌ 未实现 | **交付生产前必须完成**。目前只有集群内 ClusterIP |
| 可观测性（CloudWatch 模式） | ❌ 未实现 | 当前能用的手段见 [04 §6](04-architecture-and-operations.md#6-可观测性如何使用) |
| `status` / `test` / `scale` / `destroy` 命令 | ❌ 未实现 | 临时做法见 04 §5 |
| 镜像来源 | ⚠️ Docker Hub 匿名拉取 | 有限流风险，NAT 出口 IP 可能和其他业务共用。计划改为同步到客户 ECR |
| 图片像素上限 `max_image_pixels` | ⚠️ 尚未传给 vLLM | 大图会占满上下文。网关阶段统一处理 |
| 单 NAT | ⚠️ | 对 PoC 足够；生产建议 `nat_gateways: 2` |
| 容量预留（ODCR） | ⚠️ Karpenter 不会主动指定或优先使用 | EC2NodeClass 还没有配置容量预留选择器；open 类型 ODCR 是否被 EC2 自动套用未验证。买 ODCR 之前要先补上 |
| 回退期间的请求上限和负载 | ⚠️ | 主力和回退共用一个 Service，客户端要按最小上限发请求（8K、2 张图）；回退期间扩缩容仍按主力的 `C_sla` 计算（[04 §4.4](04-architecture-and-operations.md#44-主力机型没货时自动回退接力)） |
| 回退所需配额 | ⚠️ | 副本上限按主力机型（8 vCPU/副本）计算，回退 g6-tp2 每副本 24 vCPU，打满时需要 3 倍配额 |
| 清理（`destroy`）和换人操作 | ⚠️ 未实测 | [04 §5.1、§5.6](04-architecture-and-operations.md#56-清理环境destroy-命令上线前的临时做法) 的临时步骤还没有完整执行过 |
| 部署工具的已知问题 | ⚠️ | 环境检查会检查用不到的 CodeBuild / SSM 权限；同名部署检测无效；部分提示和实际不一致（[04 §7](04-architecture-and-operations.md#7-已知限制与后续计划)） |
| 跨 AZ 高可用 | ⚠️ g7e 只在 apse5-az2 | 主力副本没有 AZ 级冗余，靠 g6-tp2 在两个 AZ 兜底 |

---

## 附录 A：复现方法

```bash
# 推理验收（在集群内测，结果才准确）。全部通过时最后一行是「结果：通过」，退出码 0
POD=$(kubectl -n qwen-vl get pod -l app=qwen-vl -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{"\n"}{end}' | head -1)
kubectl -n qwen-vl exec -i "$POD" -c vllm -- python3 - < e2e/infer_test.py

# 一次冷启动的时间线（Pod、NodeClaim、镜像、vLLM 各阶段）
kubectl -n qwen-vl get pod "$POD" -o jsonpath='{.metadata.creationTimestamp}{"\n"}{range .status.conditions[*]}{.type} {.lastTransitionTime}{"\n"}{end}'
kubectl get nodeclaims -o wide
kubectl -n qwen-vl get events --field-selector involvedObject.name=$POD,reason=Pulled   # 事件只保留约 1 小时
kubectl -n qwen-vl logs "$POD" -c restore-compile-cache
kubectl -n qwen-vl logs "$POD" -c vllm | grep -E "Model loading took|torch.compile took|Initial profiling|init engine"
```

更多观测手段见 [04 §6](04-architecture-and-operations.md#6-可观测性如何使用)。
