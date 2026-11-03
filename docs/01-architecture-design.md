# PoC 方案设计：EKS 上弹性部署 Qwen3.5-35B-A3B 图片分析服务（低冷启动）

> 版本：v0.3（草案） | 日期：2026-11-03 | 目标 Region：**ap-southeast-5（马来西亚）**；§2.2 的 us-east-1 数据作为对照（部署时使用 CloudShell 所在的 region）
> 状态：待确认（见 §13 待确认问题）

---

## 0. 一页结论

| 维度 | 设计决策 |
|---|---|
| 模型 | `Qwen/Qwen3.5-35B-A3B-FP8`（37.5 GB）；同架构的 `Qwen3.6-35B-A3B-FP8` 作为 PoC 对比项，基础设施零改动 |
| 部署单元 | 主力机型 **1 副本 = 1 GPU = 1 节点**，不做 TP，扩容粒度最小、启动链路最短；只有回退机型 g6.12xlarge 用 TP=2（§2.3） |
| GPU | 用 **GPU profile** 按 region 自动选择（§2.3）。马来西亚：主选 **g7e.2xlarge**（RTX PRO 6000 96GB，**只有 apse5-az2 有货**），回退 **g6.12xlarge**（4×L4，TP=2，每节点 2 副本，需 PoC 验证）。us-east-1：g7e + g6e。PoC 按「满足 SLA 时的单位吞吐成本」定 |
| 推理引擎 | vLLM（固定版本，当前 stable v0.32.0），关闭 thinking，限制图片像素和数量 |
| 弹性 | KEDA（vLLM 队列/并发指标 + SQS 积压 + cron）→ HPA → Karpenter（多机型、多 AZ，ODCR 兜底） |
| 冷启动 | 默认链路 **10~25 min → 目标 ≤3 min**（新节点）/ **≤90 s**（复用保温节点） |
| 削峰 | 同步走网关做负载感知路由 + 背压；能异步的走 SQS；可选溢出到 Bedrock `qwen3-vl-235b-a22b` |
| 可用区 | **2 个 AZ**，部署时按 GPU 有没有货自动选择（马来西亚实测结果：apse5-az1 + apse5-az2） |
| 非 GPU 业务 | 集群同时承载客户的普通业务：系统节点（托管节点组，带污点）+ 通用 CPU 节点池（Karpenter，不带污点）+ GPU 节点池（带 `nvidia.com/gpu` 污点），三者隔离（§3.1） |
| 交付 | 客户在 CloudShell（或跳板机）运行 `qwenctl`，回答 4 个问题；AWS 资源走 CDK → CloudFormation，集群内组件由客户端 helm/kubectl 安装（见 [03](03-deployment-parameters.md)） |

**核心思路**：冷启动不可能为 0，所以分四层处理——
① 常驻副本承接谷值 + 预留 buffer；② 用领先指标提前扩容，把冷启动藏在 buffer 里；③ 把每一段冷启动链路压到最短；④ 用队列/溢出兜住超出 buffer 的突发。

---

## 1. 需求与假设

| 项 | 假设（待客户确认） |
|---|---|
| 场景 | 制造业图片分析：质检缺陷描述、铭牌/单据识别、产品图打标，输出结构化 JSON |
| 请求形态 | 单请求 1~4 张图，单图 ≤ 2 MP；输入约 1~4K tokens，输出 ≤ 1K tokens |
| SLA | 同步：p95 E2E ≤ 10 s；异步：分钟级完成 |
| 流量 | 谷值约 1~2 副本的负载，峰值是谷值的 5~10 倍，突增可能在分钟级发生，日内有一定周期性 |
| 合规 | 数据留在客户 AWS 账号内；如启用 Bedrock 溢出，需客户书面接受 |

---

## 2. 模型与 GPU 选型

### 2.1 模型关键事实（来自 HF config）

| 属性 | 值 | 对部署的影响 |
|---|---|---|
| 架构 | `Qwen3_5MoeForConditionalGeneration`，256 experts / 8 routed，**激活参数约 3B** | decode 很快，单卡就够，不需要 TP |
| 注意力 | 40 层，其中 **10 层 full attention + 30 层 linear attention**，KV heads = 2，head_dim = 256 | KV cache 只有约 **20 KB/token**（BF16），显存压力小 |
| 视觉 | 原生多模态（early fusion），patch 16 + 2×2 merge，**约 32×32 像素对应 1 个 token** | 1024×1024 的图约 1K tokens，必须限制像素 |
| 权重 | BF16 71.9 GB / **FP8 37.5 GB** / GPTQ-Int4 24.4 GB | FP8 单卡能放下，冷启动要加载的数据量最小、精度损失也小 |
| MTP | 内置 1 层 MTP | 低并发时可开 speculative decoding 降 TPOT（PoC 评估） |

KV 估算：10 层 × 2(K/V) × 2 heads × 256 × 2 B ≈ 20 KB/token；linear attention 每条序列还有一份固定状态，约 30~60 MB。一个 4K token 的请求大约占 140 MB。

### 2.2 GPU 候选（us-east-1 实测价格）

| 实例 | GPU | 显存 | 网络 | On-Demand $/h | FP8 放下后剩余显存 | 可用 AZ |
|---|---|---|---|---|---|---|
| **g7e.2xlarge** | RTX PRO 6000 (Blackwell) | 96 GB | 50 Gbps | **3.363** | ~50 GB（并发充裕） | 1a/1c/1d |
| g6e.2xlarge | L40S (Ada) | 48 GB | ≤20 Gbps | 2.242（RI 1yr NU 1.413） | ~6 GB（约 40 路 4K 请求） | 1a/1b/1c/1d |
| g6e.4xlarge | L40S | 48 GB | 20 Gbps | 3.004 | 同上，CPU/内存翻倍 | 1a/1b/1c/1d |
| p5.4xlarge | H100 | 80 GB | 100 Gbps | 6.880 | ~35 GB | 全部 6 AZ |

### 2.3 马来西亚（ap-southeast-5）实测（2026-11-03）

| 实例 | GPU | On-Demand $/h | 有货的 AZ（AZ ID） | 定位 |
|---|---|---|---|---|
| **g7e.2xlarge** | RTX PRO 6000 96GB | **5.762**（us-east-1 的 1.7 倍） | **仅 apse5-az2** | 主力，TP=1 |
| g6.12xlarge | 4× L4 24GB（共 96GB） | 5.797（每副本 ≈ 2.90） | apse5-az1、apse5-az2 | 回退，TP=2，每节点 2 副本 |
| g6.2xlarge / gr6 | 1× L4 24GB | 1.231 / 1.939 | apse5-az1、apse5-az2 | 放不下 FP8 权重（37.5 GB），不用 |
| g6e、p5、p4d | — | — | 不提供 | — |

- **g7e 只在一个 AZ**：主力副本没有 AZ 级高可用，az2 出现 ICE 或故障时由 g6.12xlarge 在两个 AZ 兜底。
- **g6.12xlarge TP=2 的风险**：每卡 24 GB，放下一半权重（约 18.8 GB）后 KV 余量只有每卡 1~2 GB，并发能力和 L4 上 FP8 MoE kernel 的性能都要 PoC 实测。KV 不够时的备选是 GPTQ-Int4（24.4 GB）+ TP=2，代价是精度。
- 其他服务：AMP、X-Ray、CodeBuild、CloudShell、EKS 1.34~1.37 都可用；**AMG 不可用**（新加坡/悉尼/东京有）；**Bedrock 没有 Qwen3-VL**，溢出选项不可用。
- 配额（我方账号）：G/VT 768 vCPU，标准实例 1152 vCPU。客户账号以环境检查结果为准。

**GPU profile**（锁定在 `cli/qwenctl/catalog.py`）：每种机型对应一套部署参数，环境检查时按 region 的供货情况自动选择，优先级 g7e > g6e > g6-tp2。

| profile | 机型 | TP | 每节点副本数 | NodePool weight |
|---|---|---|---|---|
| g7e-1gpu | g7e.2xlarge / 4xlarge | 1 | 1 | 100 |
| g6e-1gpu | g6e.2xlarge / 4xlarge | 1 | 1 | 50 |
| g6-tp2 | g6.12xlarge | 2 | 2 | 10 |

> 「可用 AZ」一列是在我方账号里查到的。同一个 AZ 名在不同账号里对应的物理 AZ 不一样，客户部署时会在客户账号里按 AZ ID 重新查询（见 [03 §4](03-deployment-parameters.md#4-az-选择固定-2-个)）。

**选型理由**
- **g7e 主选**：显存大，KV 不受限，可以把 `max-num-seqs` 开大；Blackwell 原生 FP8；**50 Gbps 网络让 S3 流式加载 37.5 GB 权重只需约 10~20 s**，直接缩短冷启动。
- **g6e 备选**：便宜约 33%，AZ 覆盖更广，用来分散 ICE（容量不足）风险。注意：L40S（SM89）上 block-FP8 MoE kernel 的性能需要 PoC 实测。
- **不选多卡实例（如 g6e.12xlarge TP=4）**：一次扩容就是 4 卡，粒度太粗；权重加载和 NCCL 初始化都会拉长冷启动。
- **不选 g6e.xlarge**：只有 32 GB 内存，加载 37.5 GB 权重风险大。

> 最终判据：PoC 压测出每种实例在 SLA 下的最大并发 `C_sla` 和吞吐，算「$/千张图」，取最低者。

---

## 3. 总体架构

```
                      ┌──────────────────────── AWS us-east-1 / VPC (2 AZ，自动选择) ─────────────────────────┐
  同步客户端 ──HTTPS──►│ WAF ─► ALB ─► Envoy AI Gateway (CPU, Graviton, ≥2 pods)                                 │
                      │                 │  • API Key/JWT 鉴权、限流、请求体/图片大小校验                            │
                      │                 │  • InferencePool + EPP：按队列长度/KV 使用率选后端                         │
                      │                 │  • 背压：所有后端排队超阈值 → 429 或溢出到 Bedrock（可选）                  │
                      │                 ▼                                                                        │
  异步客户端 ─► S3(图片) │        ┌─────────────────────────────────────────┐        ┌───────────────────────────┐  │
     └──► SQS ────────►│ Worker │  vLLM Pod × N  (1 GPU/Pod/Node)          │        │ Amazon Bedrock (可选溢出)  │  │
          (KEDA SQS)   │ (CPU)──►  Qwen3.5-35B-A3B-FP8                     │        │ qwen.qwen3-vl-235b-a22b   │  │
                      │        │  Bottlerocket NVIDIA / Karpenter NodePool │        └───────────────────────────┘  │
                      │        └──────▲───────────────┬──────────────────┘                                       │
                      │   权重流式加载 │               │ /metrics (5s)                                            │
                      │  (Run:ai      │               ▼                                                          │
                      │   Streamer)   │   Prometheus(集群内) ──remote_write──► AMP ──► AMG                         │
                      │               │               │                                                          │
                      │  S3 模型桶 ────┘               ▼                                                          │
                      │  (Gateway Endpoint)   KEDA ScaledObject ─► HPA ─► Pending Pod ─► Karpenter ─► EC2 GPU      │
                      │                       (队列/并发 + SQS 积压 + cron)        (g7e 优先 → g6e 回退，ODCR 基线)│
                      │  ECR (vLLM 镜像) + EBS Snapshot（预缓存镜像的 Bottlerocket 数据卷）                         │
                      └─────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 节点分层（GPU 推理和客户业务共用一个集群）

| 节点类型 | 管理方式 | 机型 | 污点 | 运行什么 |
|---|---|---|---|---|
| 系统节点 | EKS 托管节点组，固定 2 台 | m7g.large（Graviton，按 AZ 供货自动选） | `CriticalAddonsOnly` | Karpenter、CoreDNS、KEDA、Prometheus、ADOT 等插件 |
| 通用 CPU 节点 | Karpenter NodePool | 默认 x86、按需实例，上限 200 vCPU（高级配置可改） | 无 | **客户的非 GPU 业务**、网关数据面、异步 Worker |
| GPU 节点 | Karpenter NodePool，每个 GPU profile 一个 | 见 §2.3 | `nvidia.com/gpu` | 只有 vLLM（带对应 toleration） |

- 通用节点池默认不放 arm64：客户的镜像不一定是多架构的，被调度到 Graviton 上会直接启动失败。确认镜像支持 arm64 后，可以在高级配置里加上 `arm64`。
- 客户业务不需要任何额外设置就会落在通用节点池；GPU 节点只接受带 toleration 的 Pod，不会被普通业务占用。

**组件选型理由**

| 组件 | 作用 | 为什么选它 |
|---|---|---|
| EKS 1.36 + 自管 Karpenter 1.15 | GPU 节点秒级决策、多机型回退 | 相比 Cluster Autoscaler + MNG 少一层 ASG，启动更快；相比 EKS Auto Mode，可以自定义 Bottlerocket 数据卷快照（镜像预缓存），这是冷启动的关键一环 |
| Bottlerocket NVIDIA | GPU 节点 OS | 自带驱动和 device plugin，启动快；支持数据卷用 EBS 快照预置镜像 |
| vLLM | 推理引擎 | 官方推荐、支持 Qwen3.5 VL；原生支持 Run:ai Model Streamer 从 S3 直接加载；暴露的队列/KV 指标适合驱动扩缩容 |
| KEDA | 扩缩容控制器 | 一个 ScaledObject 里可以组合 Prometheus、SQS、cron 多种触发器，HPA 原生做不到 |
| Envoy AI Gateway + Gateway API Inference Extension | 入口路由 | 负载感知路由（LLM 请求时长差异大，Round-Robin 会打偏）；新 Pod 就绪后 1~2 s 就接流量，避开 ALB 注册目标的 30~90 s；原生支持 Bedrock 后端，方便做溢出 |
| SQS + Worker | 异步削峰 | 能接受分钟级延迟的流量（批量质检、单据）不必为冷启动付出 buffer 成本 |
| S3 + Run:ai Model Streamer | 权重分发 | S3 的总吞吐随节点数线性扩展，没有共享文件系统的吞吐瓶颈；不用预置 FSx |
| 集群内 Prometheus + AMP | 指标 | KEDA 查集群内 Prometheus（5 s 抓取，延迟最低）；AMP 负责长期存储和 Grafana 展示 |

---

## 4. 冷启动优化（核心）

### 4.1 冷启动链路拆解与时间预算

| # | 阶段 | 默认做法耗时（估） | 优化手段 | 优化后目标 |
|---|---|---|---|---|
| 1 | **扩容决策** | HPA 按 CPU/GPU 利用率，有 stabilization 窗口：1~5 min | KEDA 10 s 轮询 vLLM 排队/并发指标；scaleUp stabilization = 0 | 15~30 s |
| 2 | **节点就绪** | CA + MNG：2~4 min | Karpenter 直接调 EC2 Fleet；Bottlerocket NVIDIA 变体 | 45~75 s |
| 3 | **镜像拉取**（vLLM 镜像 10 GB+） | 从 Docker Hub 拉取并解压：2~5 min | 镜像同步到 ECR；Bottlerocket 数据卷挂载**预缓存镜像的 EBS 快照**（备选：SOCI parallel pull） | 10~30 s |
| 4 | **权重加载**（37.5 GB） | 从 HF 下载再加载：3~10 min | 权重放同 Region S3 + VPC Gateway Endpoint；vLLM `--load-format runai_streamer` 并发流式读入显存 | 15~40 s |
| 5 | **Engine 初始化** | torch.compile + 全量 CUDA graph：1~3 min | 预热的 compile/Triton/FlashInfer 缓存放 S3，initContainer 同步；限制 `--max-cudagraph-capture-size`；`video: 0` 减少 profiling | 20~40 s |
| 6 | **接入流量** | ALB IP target 注册 + 健康检查：30~90 s | ALB 只指向网关（稳定目标）；网关的 EPP 监听 Pod readiness | 1~5 s |
| | **合计（新节点）** | **10~25 min** | | **≈ 2~3 min** |
| | **合计（保温节点，跳过 2/3）** | | | **≈ 45~90 s** |

> 表中「优化后」是工程估算，PoC 第 2 周逐段实测（见 §10 指标）。

### 4.2 各项落地细节

**(1) 镜像预缓存：Bottlerocket 数据卷快照**
- 用临时 Bottlerocket 实例 `ctr pull` vLLM 镜像（ECR，固定 tag），把 `/dev/xvdb` 打成 EBS Snapshot，写进 Karpenter `EC2NodeClass.blockDeviceMappings[].ebs.snapshotID`。
- 每次升级 vLLM 都通过 CI 重建快照（参考 `awslabs/ai-on-eks` → `guidance/container-startup-time/14-prefecthing-images-on-br`）。
- 注意：从快照恢复的卷是 lazy load，首次读要从 S3 拉块。容器启动只读用到的文件，影响可控；PoC 实测后再决定是否需要 EBS 卷初始化速率或 FSR。

**(2) 权重：S3 + Run:ai Model Streamer**
- `s3://<model-bucket>/qwen3.5-35b-a3b-fp8/`（safetensors + config/tokenizer），设 `HF_HUB_OFFLINE=1`，启动过程完全不访问外网。
- 流式加载的并发数 `concurrency` 调到 16~32，把实例网络带宽吃满。g7e（50 Gbps）理论约 6 GB/s，g6e.2xlarge（≤20 Gbps，突发带宽）约 2.5 GB/s。
- 不推荐把权重也打进 EBS 快照：lazy load 首读慢，而且 2xlarge 实例的 EBS 上限只有 625 MB/s，37.5 GB 要 60 s 以上，比 S3 流式慢。

**(3) Engine 初始化缓存**
- CI 在每种 GPU 架构上各启动一次，把 `VLLM_CACHE_ROOT`（含 torch.compile 产物）、Triton、FlashInfer 的缓存上传到 `s3://.../vllm-cache/<vllm版本>/<参数hash>/`。
- initContainer 每次启动时 `aws s3 sync` 到 emptyDir（几百 MB 级）。两种 GPU 架构的缓存都同步下来，vLLM 会按 hash 自动命中。
- `--max-cudagraph-capture-size 128`：覆盖常见 batch 即可，减少 capture 时间。

**(4) 保温：靠 Karpenter `consolidateAfter`，不用 placeholder Pod**
- 缩容后空节点保留 10 min（`consolidationPolicy: WhenEmpty`）。这段时间里再次扩容可以跳过节点启动和镜像拉取，约 45~90 s 就能服务。
- **为什么不用「占位 Pod 预热节点」**：占位 Pod 占着的 GPU 节点和一个在线副本花一样的钱，不如直接把 `minReplicas` +1，至少它还在接流量。
- **为什么不用 ASG Warm Pool（Stopped 实例）**：Karpenter 不支持；从 Stopped 启动 GPU 实例同样可能 ICE；相对 Karpenter + 快照只省约 30~60 s，引入的运维复杂度不值得。

**(5) 容量可获得性 = 冷启动的隐藏变量**
- 拿不到 GPU 时，冷启动就是无穷大。对策：
  - 基线副本用 **ODCR（On-Demand Capacity Reservation）**，Karpenter 通过 `capacityReservationSelectorTerms` 优先使用（capacity-type `reserved`）。
  - NodePool 设置多机型、多 AZ：g7e 的 NodePool `weight: 100`，g6e 的 `weight: 50`，ICE 时自动回退。
  - G 系列 Spot 配额只有 **64 vCPU**（实测），PoC 不依赖 Spot；生产如需 Spot 承接突发，要先提配额，并接受 2 min 中断通知 ≈ 一次冷启动的时间。

---

## 5. 弹性伸缩设计

### 5.1 分层容量模型

| 层 | 承接什么 | 响应时间 | 成本 |
|---|---|---|---|
| L0 常驻 | 谷值流量 + buffer，`minReplicas ≥ 2`（跨 AZ 高可用） | 0 | ODCR + Savings Plan / RI |
| L1 保温节点 | 缩容后 10 min 内的回弹 | 45~90 s | 空节点的保温时间 |
| L2 冷扩容 | 持续上升的流量 | 2~3 min | On-Demand 按量 |
| L3 缓冲/溢出 | 冷启动窗口内超出 buffer 的突发 | 立即 | SQS 近乎为 0 / Bedrock 按 token 计费 |
| 计划扩容 | 已知的日内周期（上班时段、排产批次） | 提前 | 同 L2 |

### 5.2 buffer 怎么定（冷启动和成本的关系）

记 `g` 为在一个冷启动窗口 `T_cold` 内流量可能上涨的最大比例，则：

```
目标利用率 U_target ≤ 1 / (1 + g)
```

- 例：`T_cold = 3 min`，3 分钟内流量最多涨 50% → `U_target ≤ 0.67`，即按 `C_sla` 的 65% 设扩容阈值。
- 冷启动越短，`g` 越小，buffer 越小，常驻副本越少。**压缩冷启动本身就在省钱**，这是 §4 的经济价值所在。
- `g` 从生产的历史指标里取（PoC 阶段用压测的流量模型模拟）。

### 5.3 扩缩容信号

| 触发器 | 指标 | 阈值（单副本） | 作用 |
|---|---|---|---|
| Prometheus（主） | `vllm:num_requests_running + vllm:num_requests_waiting` | `0.65 × C_sla` | 按负载等比扩容 |
| Prometheus（领先） | `vllm:num_requests_waiting` 1 min 最大值 | 4 | 一出现排队就扩，抢在 SLA 被打破之前 |
| Prometheus（显存） | `vllm:kv_cache_usage_perc` | 0.8 | 防止长上下文/多图请求把 KV 打满 |
| SQS | `ApproximateNumberOfMessagesVisible` | 单副本吞吐 × 目标排空时间 | 异步通道 |
| Cron | 时间窗 | 已知的峰值时段 | 提前把 min 抬高 |

HPA 行为（扩得激进、缩得保守，因为每次冷启动都很贵）：
- **scaleUp**：stabilization 0；每 30 s 最多扩 100% 或 +4 个 Pod，取两者较大值。
- **scaleDown**：stabilization 900 s；每 5 min 最多缩 1 个 Pod。
- 优雅下线：`preStop sleep 15` 让网关先摘流量；`terminationGracePeriodSeconds: 180` 让在途请求跑完。

> 不用 GPU 利用率做指标：vLLM 在低负载时 GPU util 也可能很高，反映不出真实的饱和度。

---

## 6. 流量接入与削峰

1. **同步通道**：WAF → ALB → Envoy AI Gateway → InferencePool（EPP 按排队/KV 选 Pod）→ vLLM。
   - 简化选项（团队小、先跑通）：ALB `least_outstanding_requests` 算法直接指向 vLLM Service。代价是新 Pod 接流量要多等 30~90 s 的注册时间。
2. **异步通道**：客户端用预签名 URL 把图片传到 S3 → 发 SQS → Worker（CPU，KEDA SQS 扩缩）调网关 → 结果写 DynamoDB/S3，通过 EventBridge/SNS 回调。Worker 的并发上限本身就是天然的背压。
3. **背压**：所有后端排队都超阈值时，网关返回 `429 + Retry-After`，不让请求在 vLLM 里无限堆积导致整体超时。
4. **Bedrock 溢出（可选，需客户确认）**：只在冷启动窗口内、本地队列超阈值时才溢出到 `qwen.qwen3-vl-235b-a22b`。
   - 风险：两个模型不同，输出不一致。需要用同一套 prompt 和 JSON Schema 做回归评测，并在响应头里标记实际使用的模型。

---

## 7. 关键配置示例（PoC 起点，参数以实测为准）

### 7.1 vLLM 容器

```yaml
containers:
  - name: vllm
    image: <acct>.dkr.ecr.us-east-1.amazonaws.com/vllm-openai:v0.32.0   # 固定版本，与 EBS 快照一致
    command: ["vllm", "serve"]
    args:
      - s3://<model-bucket>/qwen3.5-35b-a3b-fp8/
      - --load-format=runai_streamer
      - --model-loader-extra-config={"concurrency":32}
      - --served-model-name=qwen-vl
      - --max-model-len=32768                      # 图片分析够用；thinking 已关闭
      - --gpu-memory-utilization=0.92
      - --limit-mm-per-prompt={"image":4,"video":0}
      - --mm-processor-cache-type=shm
      - --reasoning-parser=qwen3
      - --default-chat-template-kwargs={"enable_thinking":false}
      - --max-cudagraph-capture-size=128
      - --allowed-media-domains=<bucket>.s3.us-east-1.amazonaws.com   # 防 SSRF
      # 图片像素上限：--mm-processor-kwargs（字段名以 vLLM Qwen3.5 recipe 为准）
    env:
      - {name: VLLM_CACHE_ROOT, value: /cache/vllm}
      - {name: HF_HUB_OFFLINE, value: "1"}
      - {name: AWS_REGION, value: us-east-1}
    resources:
      limits: {nvidia.com/gpu: 1}
    startupProbe:   {httpGet: {path: /health, port: 8000}, periodSeconds: 5, failureThreshold: 120}
    readinessProbe: {httpGet: {path: /health, port: 8000}, periodSeconds: 5}
    lifecycle:
      preStop: {exec: {command: ["sleep", "15"]}}
```

### 7.2 Karpenter

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata: {name: gpu-bottlerocket}
spec:
  amiSelectorTerms: [{alias: bottlerocket@<pinned-version>}]   # GPU 机型自动选用 NVIDIA 变体
  role: <KarpenterNodeRole>
  subnetSelectorTerms:        [{id: <subnet-az1>}, {id: <subnet-az2>}]   # 直接用子网 ID，不给客户已有的子网打 tag
  securityGroupSelectorTerms: [{tags: {karpenter.sh/discovery: qwen-vl}}]
  capacityReservationSelectorTerms: [{tags: {purpose: qwen-vl-baseline}}]
  metadataOptions: {httpTokens: required, httpPutResponseHopLimit: 1}
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs: {volumeSize: 4Gi, volumeType: gp3, encrypted: true}
    - deviceName: /dev/xvdb
      ebs: {volumeSize: 150Gi, volumeType: gp3, iops: 16000, throughput: 1000,
            encrypted: true, snapshotID: snap-<prefetched-vllm-image>}
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: {name: gpu-g7e}
spec:
  weight: 100                        # g6e NodePool 同结构，weight: 50
  template:
    spec:
      nodeClassRef: {group: karpenter.k8s.aws, kind: EC2NodeClass, name: gpu-bottlerocket}
      taints: [{key: nvidia.com/gpu, effect: NoSchedule}]
      expireAfter: 720h
      requirements:
        - {key: node.kubernetes.io/instance-type, operator: In, values: [g7e.2xlarge, g7e.4xlarge]}
        - {key: karpenter.sh/capacity-type, operator: In, values: [reserved, on-demand]}
        - {key: topology.k8s.aws/zone-id, operator: In, values: [<az-id-1>, <az-id-2>]}   # 部署时自动探测，见 03 §4
  disruption:
    consolidationPolicy: WhenEmpty   # 绝不为了「装箱」驱逐正在推理的 Pod
    consolidateAfter: 10m            # = L1 保温窗口
  limits: {nvidia.com/gpu: "16"}
```

### 7.3 KEDA

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: qwen-vl}
spec:
  scaleTargetRef: {name: qwen-vl-vllm}
  minReplicaCount: 2
  maxReplicaCount: 16
  pollingInterval: 10
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 0
          selectPolicy: Max
          policies: [{type: Percent, value: 100, periodSeconds: 30}, {type: Pods, value: 4, periodSeconds: 30}]
        scaleDown:
          stabilizationWindowSeconds: 900
          policies: [{type: Pods, value: 1, periodSeconds: 300}]
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus-operated.monitoring:9090
        query: sum(vllm:num_requests_running{model_name="qwen-vl"}) + sum(vllm:num_requests_waiting{model_name="qwen-vl"})
        threshold: "<0.65 x C_sla>"
    - type: prometheus
      metadata:
        serverAddress: http://prometheus-operated.monitoring:9090
        query: max_over_time(sum(vllm:num_requests_waiting{model_name="qwen-vl"})[1m:5s])
        threshold: "4"
    - type: cron        # 默认关闭；时区按客户业务所在地设置（出海客户不一定是 Asia/Shanghai）
      metadata: {timezone: Asia/Shanghai, start: "0 8 * * 1-5", end: "0 20 * * 1-5", desiredReplicas: "4"}
```

---

## 8. 安全设计

| 项 | 措施 |
|---|---|
| 入口 | WAF（速率限制、请求体大小）+ 网关 API Key/JWT 鉴权；ALB 只开 443，强制 TLS |
| 输入校验 | 限制图片数量、单图大小和分辨率、MIME 类型；`--allowed-media-domains` 白名单或只收 base64，防 SSRF |
| IAM | EKS Pod Identity 按角色拆分：vLLM 只有模型桶前缀的 `s3:GetObject/ListBucket`；Worker 只能读写自己的 SQS 队列和结果表；网关只有 `bedrock:InvokeModel`，限定到具体 model ARN。全部禁止 `*` |
| 网络 | 节点放私有子网；S3 用 Gateway Endpoint，ECR/STS/SQS/Bedrock 用 Interface Endpoint；Security Group 只放行网关 → vLLM:8000 |
| 数据 | S3 模型桶和图片桶开启 Block Public Access + SSE-KMS；EBS 加密；图片桶设生命周期，自动删除过期图片 |
| 节点 | Bottlerocket（只读根文件系统）；IMDSv2，hop limit 1 |

---

## 9. 可观测性

> 完整设计见 [02-observability-design.md](02-observability-design.md)，包括冷启动 15 段时间线、coldstart-tracer、指标、日志、trace 和告警。以下是摘要。

- **推理指标**：TTFT、TPOT、E2E p50/p95/p99，排队数，KV 使用率，每秒处理图片数（vLLM `/metrics`）。
- **GPU 指标**：DCGM Exporter（利用率、显存、温度、XID 错误）。
- **冷启动分段**：Karpenter NodeClaim 生命周期指标（launch → registered → initialized）+ Pod 事件（Scheduled → Pulled → Started → Ready）+ vLLM 启动日志里的「权重加载耗时」「compile 耗时」。在 Grafana 里做成一个分段瀑布面板，这是 PoC 的核心交付物之一。
- **扩缩容**：KEDA/HPA 期望副本数 vs 实际副本数 vs Pending Pod 数，一眼看出扩容卡在哪一段。

---

## 10. PoC 计划（4 周）

| 周 | 内容 | 交付物 |
|---|---|---|
| W1 | VPC/EKS/Karpenter/KEDA/观测栈（CDK TypeScript，可以参考 `awslabs/ai-on-eks` 的 inference-ready-cluster）+ `qwenctl` 交互式部署；权重上传 S3；在 g7e 和 g6e 上分别做单副本压测 | 基础环境；`C_sla`、吞吐、$/千张图 |
| W2 | 冷启动优化：EBS 快照流水线、Run:ai Streamer、compile 缓存；逐段测出「默认 vs 优化」 | 冷启动分段对比报告 |
| W3 | 弹性：KEDA 策略、网关、负载感知路由；跑阶跃、尖刺、锯齿三种流量模型的压测并调参 | 扩缩容曲线 + SLA 达标率 |
| W4 | 异步通道 +（可选）Bedrock 溢出；Qwen3.6 对比评测；Demo 和交付文档 | Demo、方案 PPT、成本模型 |

**建议的成功标准**
1. 新节点冷启动（扩容决策到开始服务）P90 ≤ 3 min；保温节点 ≤ 90 s。
2. 1 min 内流量阶跃到 3 倍时，同步请求 p95 ≤ SLA，错误率（不含有意返回的 429）≤ 1%。
3. 流量回落后 20 min 内缩回 `minReplicas`；GPU 成本相比「按峰值固定部署」下降 ≥ 50%。

---

## 11. 成本估算（GPU 部分，730 h/月）

**场景假设**：谷值常驻 2 副本；工作日峰值时段 10 h/天，平均额外 4 副本（峰值最高 10 副本）。

| 方案 | 常驻 2 副本 | 弹性部分（1,200 副本·h/月） | GPU 月成本 | 对比按峰值固定 10 副本 |
|---|---|---|---|---|
| g7e.2xlarge 全 On-Demand | $4,910 | $4,036 | **$8,946** | $24,551 → 省 64% |
| g7e.2xlarge 常驻部分用 Compute Savings Plan | 按折扣率另算 | $4,036 | 以 Pricing Calculator 为准 | — |
| g6e.2xlarge 全 On-Demand | $3,273 | $2,690 | **$5,964** | $16,367 → 省 64% |
| g6e.2xlarge 常驻部分用 RI 1yr No Upfront | $2,062 | $2,690 | **$4,753** | — |

- 注意：g6e 单副本吞吐预计低于 g7e，相同流量下需要更多副本。**必须在 PoC 压测后按「同等吞吐」重新比较**，上表只是单价层面的比较。
- 其他费用约 $300~500/月：EKS 控制面 $73、Graviton 系统节点、ALB/WAF、AMP/AMG、Interface Endpoints、EBS 快照、S3。Bedrock 溢出按 token 计费，只在突发时产生。
- **PoC 预算**：约 900 GPU·h（g7e 600 h + g6e 300 h）≈ **$2.7K**，加上其他费用，合计约 $3~3.5K。
- **马来西亚（ap-southeast-5）**：g7e.2xlarge $5.762/h，常驻 2 副本 ≈ **$8,412/月**（是 us-east-1 的 1.7 倍）；g6.12xlarge 每副本 ≈ $2.90/h。PoC 预算按 900 GPU·h 估算约 **$4.6~5.2K**（GPU 部分），需要按 PoC 实际的机型组合重算。

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| us-east-1 GPU 容量不足（ICE） | 扩不出来，冷启动无上限 | ODCR 保基线；g7e/g6e 两种机型 × 2 个 AZ，优先选两种机型都有货的 AZ；必要时增加 p5.4xlarge 作为第三档回退 |
| L40S 上 block-FP8 MoE kernel 性能不达预期 | g6e 性价比下降 | PoC 对比 FP8 和 GPTQ-Int4；或者 g7e 单选 |
| vLLM 版本对 Qwen3.5 hybrid 架构的支持（prefix caching 在 hybrid 模型上仍是 experimental） | 稳定性 | 固定版本 + 回归压测；prefix caching 视实测再开 |
| EBS 快照 lazy load 导致首读慢 | 镜像阶段比预期慢 | 实测；备选 SOCI parallel pull，或卷初始化速率/FSR |
| Bedrock 溢出模型不一致 | 输出格式和质量有差异 | 默认关闭；开启前做回归评测并在响应中标记来源 |
| 马来西亚 g7e 只在 apse5-az2 | 主力副本没有 AZ 级高可用；az2 ICE 时扩容只能靠回退机型 | g6.12xlarge 在两个 AZ 兜底；常驻副本优先用 ODCR 锁定 az2 的 g7e 容量 |
| g6.12xlarge TP=2 的 KV 余量小 | 回退时并发能力低 | PoC 实测；不够时改用 GPTQ-Int4 + TP=2 |
| 马来西亚没有 AMG | Grafana 模式要跨 region 或自建 | 默认 CloudWatch 模式（02 §1.2）；Grafana 方案待定（§13 第 8 条） |
| 突发超过「buffer + 溢出」能力 | 部分请求 429 | 客户端重试 + 退避；可异步的流量强制走 SQS |

---

## 13. 待确认问题（影响设计分支）

1. **延迟 SLA 和同步/异步占比**：能走异步的比例越高，常驻 buffer 越小。
2. **流量画像**：峰值 QPS、峰谷比、突增速度（每分钟涨多少）、是否有日/周周期。决定 `minReplicas`、`maxReplicas` 和 buffer 系数 `g`。
3. **图片规格**：分辨率、每请求图数、输出长度。决定单副本 `C_sla` 和像素上限。
4. **Bedrock 溢出**是否可接受（不同模型、数据出 EKS 但仍在账号内）。
5. ~~**IaC 选型**~~ → **已定：CDK TypeScript**（2026-11-03）。CloudShell 预装了 `cdk`，但没有 `terraform`；CloudFormation 的状态存在服务端，适合在客户 CloudShell 上交付（见 [03 §1](03-deployment-parameters.md)）。
6. 是否需要同时评估 **Qwen3.6-35B-A3B**（同架构，2026-04 发布）。
7. ~~**马来西亚的 GPU 策略**~~ → **已定（2026-11-03）**：g7e.2xlarge（仅 az2）为主 + g6.12xlarge（TP=2）在两个 AZ 兜底。
8. **马来西亚的 Grafana 方案**：**首次交付用 CloudWatch 模式提供完整可观测性（2026-11-03 确认），Grafana 之后再建**。届时再选：在新加坡建 AMG、跨 region 查询马来西亚的 AMP/CloudWatch（只有查询结果出境），或者在集群内自建 Grafana（数据不出境，但要自己管登录）。
