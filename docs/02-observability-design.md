# 可观测性设计：冷启动全链路时间线 + 运行期指标/日志/追踪

> 版本：v0.3 | 日期：2026-11-03 | 关联：[01-architecture-design.md](01-architecture-design.md)、[03-deployment-parameters.md](03-deployment-parameters.md)
> 状态：**技术栈已确认采用混合方案**（见 §1.1）；展示层根据客户是否启用 IdC 自动选择（见 §1.2，2026-11-03 确认）
> 指标名和日志格式已对照 **Karpenter（main 文档）、kube-state-metrics、kubelet（master）、vLLM v0.32.0 源码** 核实；升级版本后需要重新核对。

---

## 0. 要回答的三类问题

| 问题 | 例子 | 手段 |
|---|---|---|
| **一次扩容慢在哪一段？** | 这次 4 分钟，是 EC2 拿不到容量，还是权重加载慢？ | 单次冷启动的**瀑布图**（trace） |
| **冷启动整体表现和趋势** | 冷启动 P90 是多少？g7e 和 g6e 谁更快？编译缓存命中率多少？ | 分阶段的**分布指标**（histogram） |
| **运行期是否健康** | SLA 是否达标？扩容跟不跟得上？GPU 有没有坏？每千张图花多少钱？ | 指标 + 日志 + 请求级 trace，三者能互相关联跳转 |

---

## 1. 技术栈

```
              ┌──────────────── EKS ────────────────────────────────────────────┐
 vLLM /metrics ┐                                                                 │
 DCGM exporter ┤                                                                 │
 Karpenter     ┼─► Prometheus（集群内，5s 抓取，保留 6h）──remote_write──► AMP ──┐ │
 KEDA / KSM    ┤        └──► KEDA 扩缩容查询（低延迟）                            │ │
 node-exporter ┤                                                                  │ │
 coldstart-tracer ┘                                                               │ │
                                                                                 ▼ │
 容器 stdout ───► Fluent Bit ───────────────────────────► CloudWatch Logs ──► AMG (Grafana)
 K8s Events ───► kubernetes-event-exporter ──────────────►      ▲              ▲ │
 EKS 控制面（scheduler / audit）──────────────────────────────────┘              │ │
                                                                                │ │
 vLLM OTel spans ┐                                                              │ │
 Envoy spans     ┼─► ADOT Collector（tail sampling）──► AWS X-Ray ───────────────┘ │
 coldstart-tracer┘                                                                │
              └─────────────────────────────────────────────────────────────────┘
```

> 上图是 Grafana 模式。CloudWatch 模式下不部署 AMP 和 AMG，面板改用 CloudWatch Dashboards，其余部分不变（见 §1.2）。

| 信号 | 采集 | 存储 | 为什么这么选 |
|---|---|---|---|
| 指标 | 集群内 Prometheus（kube-prometheus-stack） | 全量指标进 AMP（Grafana 模式）；关键指标白名单同时写入 CloudWatch Metrics，给告警用 | KEDA 查集群内 Prometheus，扩容延迟最低；AMP 托管长期存储，免运维 |
| 日志 | Fluent Bit（`amazon-cloudwatch-observability` add-on） | CloudWatch Logs | 托管服务；Logs Insights 能直接查询 JSON 字段，冷启动记录的统计不需要再建 ES |
| K8s Events | `kubernetes-event-exporter` | CloudWatch Logs | **EKS 的 Events 只保留 1 小时**，`Pulled`、`Nominated`、`FailedScheduling` 这类证据过后就查不到了 |
| Trace | ADOT Collector | X-Ray | vLLM 原生支持 OTLP；X-Ray 免运维，Grafana 有现成数据源 |
| 冷启动时间线 | **自研 `coldstart-tracer`**（§3） | 同时写指标、日志、trace | 需要把 Pod、NodeClaim、Node、Events、容器日志五个来源关联起来，现成工具做不到 |
| 展示 | Amazon Managed Grafana，或 CloudWatch Dashboards（§1.2） | — | AMG 能同时接 AMP、CloudWatch、X-Ray 三个数据源，在一个面板里互相跳转 |

### 1.1 选型决策记录：混合方案（2026-11-03 确认）

**决策**：日志和 trace 用 CloudWatch 体系，指标用 Prometheus 体系。展示层根据客户是否启用 IdC，在 AMG 和 CloudWatch Dashboards 之间选择（§1.2）。

| 信号 | 服务 | 属于 CloudWatch 体系 |
|---|---|---|
| 日志（容器日志、K8s Events、EKS 控制面、冷启动 JSON 明细） | CloudWatch Logs + Logs Insights | ✅ |
| Trace（请求级 trace、冷启动瀑布图） | AWS X-Ray | ✅ |
| 指标（vLLM、GPU、Karpenter、KEDA、tracer 的分段耗时） | 集群内 Prometheus + AMP（Grafana 模式）；关键指标白名单同时写入 CloudWatch Metrics | 部分 |
| 告警 | CloudWatch Alarms → SNS（两种模式都一样） | ✅ |
| 面板 | AMG（接 AMP、CloudWatch、X-Ray 三个数据源），或 CloudWatch Dashboards | 视模式而定 |

**全量指标不放 CloudWatch Metrics 的原因**（只有告警需要的关键指标写入 CloudWatch，见 §1.2）
1. **扩缩容时效**：KEDA 要按 5~10 s 的粒度查询 vLLM 排队数。CloudWatch 指标从写入到能查询有延迟，会让扩容决策慢几十秒以上，和压缩冷启动的目标相冲突。
2. **原生格式**：vLLM、DCGM、Karpenter、KEDA 输出的都是 Prometheus 格式。冷启动分段耗时、TTFT 这些 histogram 要用 PromQL 的 `histogram_quantile` 算分位数，放在 Prometheus 里最自然。
3. **成本**：转成 CloudWatch 自定义指标后，按 `phase × 机型 × AZ × 缓存命中` 等维度组合计费，序列一多就会比 AMP 贵。

### 1.2 展示层两种模式（2026-11-03 确认）

客户账号不一定启用了 IAM Identity Center，而 AMG 登录依赖 IdC 或 SAML IdP；**马来西亚（ap-southeast-5）也没有 AMG**。所以设计成**采集层在两种模式下完全一样，只有展示层不同**，部署时根据是否启用 IdC 自动选择：

| 组件 | CloudWatch 模式（未启用 IdC 时默认） | Grafana 模式 |
|---|---|---|
| 集群内 Prometheus（KEDA 扩缩容用） | ✅ | ✅ |
| 日志 → CloudWatch Logs；trace → X-Ray；coldstart-tracer | ✅ | ✅ |
| 关键指标 → CloudWatch Metrics（白名单约 30~50 个） | ✅ | ✅（保留，给告警用） |
| 告警：CloudWatch Alarms → SNS | ✅ | ✅ |
| 全量指标 → AMP | 不部署（设置 `observability.amp: on` 可以提前开启） | ✅ |
| 面板 | CloudWatch Dashboards | AMG（CloudWatch Dashboards 也保留） |

**关键指标怎么进 CloudWatch**：ADOT Collector 本来就部署了（用于 trace），给它加一条 metrics pipeline：从集群内 Prometheus 的 `/federate` 接口拉取白名单指标，再通过 `awsemf` exporter 写入 CloudWatch。这样不会重复抓取各个 target。维度只细到机型和 AZ，不细到 Pod：Pod 名一直在变，按 Pod 拆维度会让指标数量和费用一直涨。

**GPU 指标**：关闭 `amazon-cloudwatch-observability` add-on 自带的 GPU 采集，统一用 §8 里的 DCGM Exporter，避免一个节点上跑两个 DCGM 实例。

**告警两种模式都放在 CloudWatch Alarms**，所以切换时不用迁移告警。§7 的条件用 PromQL 写是为了好读，实际落地方式如下：
- **计数和水位类**（KEDA 错误、Karpenter 启动失败、Pending Pod、排队数、KV 使用率、XID、副本数顶格）：用上面的白名单指标。
- **分位数和比例类**（冷启动 P90、E2E P95、Bedrock 溢出比例）：用 metric filter 从 JSON 日志（coldstart-tracer 的记录、网关的 access log）里逐条取值生成指标，CloudWatch 可以直接对这些指标算 p90/p95。SLA 告警以网关 access log 里的 E2E 延迟为准；TTFT 在面板里看。

**冷启动分析**：各阶段的 P90 和最近几次的明细都用 Logs Insights 查 tracer 的 JSON 记录（§3.2 有示例），两种模式通用。单次冷启动的瀑布图在 X-Ray 控制台里也能看。

**从 CloudWatch 切到 Grafana**：运行 `qwenctl observability grafana [--auth idc|saml]`。这一步只往上加组件，不重建集群，也不中断推理：
1. 前提：客户已经启用 IdC，或者有 SAML IdP（Entra ID、Okta 等，AMG 原生支持）。
   - 本 region 没有 AMG（例如马来西亚）时，两种做法待定：在新加坡建 AMG，跨 region 查询本 region 的 AMP 和 CloudWatch（只有查询结果出境）；或者在集群内自建 Grafana（数据不出境，但登录要自己管）。
2. 创建 AMP workspace，给 Prometheus 的 IAM 角色加上 AMP 写权限，打开 remote_write。Prometheus 的 Pod Identity 关联**在部署第一天就建好**（先不授予权限），所以切换时不用重建 Pod。remote_write 配置由 operator 热加载，这一点要在 PoC 中验证。
3. 创建 AMG workspace，接入 AMP、CloudWatch、X-Ray 三个数据源。
4. 导入仓库里的 Grafana 面板 JSON（`dashboards/grafana/`），分配管理员。

**切换的代价**
- AMP 里的历史数据从切换那一刻才开始有。切换前的数据不会丢：关键指标、冷启动明细、日志和 trace 都在 CloudWatch 和 X-Ray 里，AMG 通过 CloudWatch 数据源一样能看。
- 要维护两套面板：CloudWatch Dashboards 定义在 CDK 里，Grafana 面板是仓库里的 JSON 文件。
- 如果确定以后会切，第一天就设置 `observability.amp: on`（约 $100~200/月），切换后 PromQL 的历史数据就是完整的。

---

## 2. 冷启动全链路时间线（核心）

### 2.1 阶段定义与数据来源

一次「新节点冷启动」被拆成下面 15 个阶段，每段都有确定的起止事件。复用保温节点时，B~F 段为 0。

| # | 阶段 | 起点 → 终点 | 数据来源（字段/日志） |
|---|---|---|---|
| A | **扩容决策** | 指标越过阈值 → Pod 被创建 | Prometheus 查询越线时刻；HPA `SuccessfulRescale` Event；`pod.metadata.creationTimestamp` |
| B | **调度决策** | Pod 创建 → Karpenter 提名并创建 NodeClaim | Pod Event `Nominated`（Karpenter）；`nodeclaim.metadata.creationTimestamp` |
| C | **EC2 启动** | NodeClaim 创建 → `Launched=True` | `nodeclaim.status.conditions[Launched].lastTransitionTime`；拿到实例 ID、机型、AZ、容量类型 |
| D | **节点引导** | `Launched` → `Registered=True` | 覆盖 EC2 开机、Bottlerocket 启动、kubelet 注册；对应 `node.metadata.creationTimestamp` |
| E | **节点初始化** | `Registered` → `Initialized=True` | Karpenter 判定 `Initialized` 的条件：Node Ready + startup taint 已移除 + **`nvidia.com/gpu` 资源已注册**。所以这一段包含 CNI 和 GPU device plugin 的就绪时间 |
| F | **Pod 绑定** | `Initialized` → `PodScheduled=True` | `pod.status.conditions[PodScheduled].lastTransitionTime` |
| G | **沙箱/网络** | 绑定 → `PodReadyToStartContainers=True` | 包含 Pod 沙箱创建和 VPC CNI 分配 IP |
| H | **镜像** | `Pulling` → `Pulled` | Pod Event 的 message 里带「拉取耗时 + 镜像大小」，或者 `already present on machine`（说明 EBS 快照预缓存命中） |
| I | **Init 容器** | → `Initialized=True` | 也就是 compile 缓存从 S3 同步的耗时 |
| J | **容器启动** | → `containerStatuses[vllm].state.running.startedAt` | EBS 快照 lazy load 的代价会体现在这一段和 K 段 |
| K | **权重加载** | `Starting to load model ...` → `Model loading took X GiB memory and Y seconds` | vLLM 日志；用 37.5 GB 除以 Y 得到实际流式吞吐 |
| L | **编译** | 缓存命中：`Directly load the compiled graph(s) ... from the cache, took Xs`<br>缓存未命中：`Compiling a graph for compile range ... takes Xs` | vLLM 日志；**同时得到 `cache_hit` 标签** |
| M | **Engine 初始化** | → `init engine (profile, create kv cache, warmup model) took Xs`；其中 `Graph capturing finished in X secs` | vLLM 日志 |
| N | **API 就绪** | `Starting vLLM server on ...` → Pod `Ready=True` | readiness probe 的周期也算在这一段 |
| O | **接入首个请求** | Pod Ready → 第一个打到该 Pod 的请求 | Envoy access log 的 `upstream_host` 等于 Pod IP 的第一条 |

> 精度：K8s 的 condition 和 Event 时间戳精度是 **1 秒**；容器日志用 kubelet 的 `timestamps=true` 前缀，精度是纳秒。对于分钟级的冷启动，这个精度足够。

### 2.2 瀑布图效果（示意，数字非实测）

```
Pod qwen-vl-7d9f-x2k │ g7e.2xlarge │ us-east-1c │ on-demand │ path=cold │ compile_cache=hit │ total=168s
             0s        30s       60s       90s       120s      150s      180s
             |─────────|─────────|─────────|─────────|─────────|─────────|
A 扩容决策    ███                                                      12s
B 调度决策       █                                                      3s
C EC2 启动        ██                                                    9s
D 节点引导          ███████                                            28s
E 节点初始化               ████                                        14s  (CNI + GPU plugin)
F Pod 绑定                     █                                        2s
G 沙箱/网络                     █                                       2s
H 镜像                           █                                      4s  (already present)
I Init 容器                       █                                     4s  (cache sync)
K 权重加载                          █████                              18s  (2.1 GiB/s)
L 编译(命中)                             ██                             7s
M Engine 初始化                            ███                         11s  (CUDA graph)
N API 就绪                                    ██                        6s
O 首个请求                                      █                       3s
```

### 2.3 现成指标做交叉验证（零开发，W1 就能用）

自研 tracer 上线之前，先用组件自带的指标看整体分布；tracer 上线后，再用这些指标核对它的结果。

| 指标 | 含义 | 对应阶段 |
|---|---|---|
| `karpenter_pods_scheduling_decision_duration_seconds` | Karpenter 看到 Pod 后多久做出调度决策 | B |
| `karpenter_pods_provisioning_bound_duration_seconds` | 需要新建节点的 Pod：从「可调度」到绑定 | B~F |
| `karpenter_pods_provisioning_startup_duration_seconds` | 需要新建节点的 Pod：从「可调度」到 Running | B~J |
| `karpenter_pods_bound_duration_seconds` / `karpenter_pods_startup_duration_seconds` | 所有 Pod：从创建到绑定 / 到 Running | — |
| `operator_nodeclaim_status_condition_transition_seconds` | NodeClaim 某个 condition 停留在某状态多久后才发生跳变 | C / D / E |
| `kubelet_image_pull_duration_seconds{image_size_in_bytes}` | 镜像拉取耗时 | H |
| `kubelet_pod_start_sli_duration_seconds` | Pod 启动耗时，**不含镜像拉取和 init 容器** | G~J |
| `kubelet_pod_start_total_duration_seconds` | Pod 启动总耗时 | G~J |
| KSM：`kube_pod_created`、`kube_pod_status_scheduled_time`、`kube_pod_status_initialized_time`、`kube_pod_status_container_ready_time`、`kube_pod_status_ready_time` | Pod 各个 condition 的时间戳 | A~N 的粗粒度 |

**这些指标的局限**：它们只给聚合分布，没法拆出单次冷启动的明细；没有 vLLM 内部阶段（K/L/M）；也很难在 PromQL 里把 Pod、NodeClaim、机型关联起来。所以需要 §3 的 tracer。

---

## 3. coldstart-tracer（自研，Python，约 300~400 行）

### 3.1 工作方式

```
watch Pods(label app=qwen-vl) ──┐
watch NodeClaims ───────────────┼─► 每个 Pod 一个状态机 ──► Pod 首次 Ready 或超时（15 min）
watch Events(ns) ───────────────┘          │
                                           ├─ 读取 Node / NodeClaim 的 conditions 和 labels
                                           ├─ 读取容器日志 (timestamps=true)，用正则抽取 K/L/M/N
                                           └─ 判定 path：NodeClaim 晚于 Pod 创建 → cold，否则 warm
                                                         │
               ┌─────────────────────────────────────────┼──────────────────────────┐
               ▼                                         ▼                          ▼
     stdout JSON 记录                          /metrics (Prometheus)        OTLP spans（真实起止时间）
     → CloudWatch Logs                         coldstart_phase_seconds      → ADOT → X-Ray（瀑布图）
     （明细查询、90 天趋势）                     （分布、告警）
```

- **卡住也要记录**：超过 15 min 仍未 Ready 时，输出 `status=stuck`，记下最后到达的阶段和原因。原因从 Event 里取，例如 `FailedScheduling`、Karpenter 的容量不足错误、`ImagePullBackOff`、`CrashLoopBackOff`。**ICE 问题主要靠它来定位。**
- **权限**：只读 RBAC（pods、pods/log、events、nodes、nodeclaims 的 get/list/watch），不需要任何 AWS 权限。日志走 stdout，trace 由 ADOT 负责上报到 X-Ray。
- **日志正则跟着 vLLM 版本走**：每个版本保存一份真实启动日志作为测试夹具，升级 vLLM 时跑单测，防止正则静默失效。

### 3.2 输出

**JSON 记录（CloudWatch Logs）**
```json
{"log_type":"coldstart","status":"ready","pod":"qwen-vl-7d9f-x2k","path":"cold",
 "node":"i-0abc...","nodeclaim":"gpu-g7e-x8k2p","nodepool":"gpu-g7e",
 "instance_type":"g7e.2xlarge","zone":"us-east-1c","capacity_type":"on-demand",
 "vllm_version":"v0.32.0","image_cached":true,"compile_cache_hit":true,
 "weight_gib":34.9,"weight_load_gibps":2.1,"total_s":168,
 "phases":{"decision_s":12,"karpenter_s":3,"ec2_launch_s":9,"node_boot_s":28,"node_init_s":14,
           "bind_s":2,"sandbox_s":2,"image_s":4,"init_container_s":4,"weight_load_s":18,
           "compile_s":7,"engine_init_s":11,"api_ready_s":6,"first_request_s":3}}
```

**Prometheus 指标**
- `coldstart_phase_seconds{phase, path, instance_type, capacity_type, zone, compile_cache_hit}`（histogram）
- `coldstart_total_seconds{path, instance_type}`（histogram）
- `coldstart_stuck_total{last_phase, reason}`（counter）

**常用查询**
```promql
# 各阶段 P90（冷节点，最近 7 天）
histogram_quantile(0.9, sum by (le, phase) (increase(coldstart_phase_seconds_bucket{path="cold"}[7d])))
```
```
# CloudWatch Logs Insights：按机型统计
fields total_s, phases.ec2_launch_s, phases.node_boot_s, phases.weight_load_s
| filter log_type = "coldstart" and status = "ready"
| stats pct(total_s,50) as p50, pct(total_s,90) as p90, avg(phases.weight_load_s) as load_avg, count() as n
  by path, instance_type
```

---

## 4. 运行期指标

### 4.1 分层指标

| 层 | 关键指标 | 用途 |
|---|---|---|
| **网关/业务** | QPS、4xx/5xx/**429** 比例、E2E 延迟、Bedrock 溢出比例、每秒处理图片数、JSON 解析失败率（Worker 自定义指标） | SLA 和用户体验 |
| **vLLM** | `vllm:time_to_first_token_seconds`、`vllm:inter_token_latency_seconds`、`vllm:e2e_request_latency_seconds`、`vllm:request_queue_time_seconds`、`vllm:request_prefill_time_seconds`、`vllm:request_decode_time_seconds` | 延迟拆解：慢在排队、prefill（图片编码）还是 decode |
| | `vllm:num_requests_running`、`vllm:num_requests_waiting`、`vllm:num_requests_waiting_by_reason`、`vllm:kv_cache_usage_perc`、`vllm:num_preemptions_total` | 饱和度（同时也是扩缩容信号） |
| | `vllm:prompt_tokens_total`、`vllm:generation_tokens_total`、`vllm:request_success_total{finished_reason}`、`vllm:mm_cache_hits/queries` | 吞吐、截断比例、多模态缓存效果 |
| **GPU（DCGM）** | `DCGM_FI_PROF_SM_ACTIVE`、`DCGM_FI_PROF_DRAM_ACTIVE`、`DCGM_FI_DEV_FB_USED`、`DCGM_FI_DEV_POWER_USAGE`、`DCGM_FI_DEV_GPU_TEMP`、**`DCGM_FI_DEV_XID_ERRORS`** | 真实算力利用率（`GPU_UTIL` 会虚高，不要看它）；硬件故障 |
| **弹性链路** | KEDA：`keda_scaler_metrics_value`、**`keda_scaler_errors_total`**；KSM：HPA desired/current、Deployment ready replicas | 扩容决策是否正确、是否「失明」 |
| | Karpenter：`karpenter_pods_state`、`karpenter_scheduler_queue_depth`、**`karpenter_cloudprovider_instance_launch_failures_total`**、`karpenter_cloudprovider_errors_total`、`karpenter_cloudprovider_instance_type_offering_available`、`karpenter_nodepools_usage` / `karpenter_nodepools_limit` | ICE、配额、NodePool 上限 |
| **节点** | node-exporter 网络接收吞吐（验证 S3 流式加载有没有吃满带宽）、EBS 读吞吐（验证快照 lazy load） | 冷启动优化的佐证 |
| **成本** | `karpenter_nodepools_cost_total`（Karpenter 估算的小时价，**不是账单口径**）÷ 每小时处理图片数 | 实时的 **$/千张图** |

### 4.2 面板

> 两种模式都提供下面 5 个面板。CloudWatch 模式下，依赖 PromQL 的面板改用白名单指标和 Logs Insights 来做，多维下钻的能力会弱一些。

| 面板 | 内容 |
|---|---|
| ① 冷启动总览 | cold/warm 两条路径的 P50/P90；各阶段 P50 堆叠柱（按机型）；编译缓存命中率；最近 20 次冷启动明细表（Logs Insights），每行可跳转到 X-Ray 瀑布图 |
| ② 弹性链路 | 同一张时序图叠加：需求（running+waiting）、容量（ready 副本数 × C_sla）、desired/current/ready 副本数、Pending Pod 数、各状态 NodeClaim 数，并用 HPA 事件做注解。**一眼看出扩容卡在哪一层** |
| ③ 推理 SLO | TTFT / E2E 的 P50/P95/P99、排队时间、错误率、429 比例、溢出比例 |
| ④ GPU 健康 | 每张卡的 SM/DRAM 活跃度、显存、功耗、温度、XID |
| ⑤ 成本 | GPU 小时成本（按机型、容量类型拆分）、$/千张图、副本小时数 |

---

## 5. 日志

### 5.1 日志源

| 来源 | 内容 | 采集 → 目标 | 保留期 |
|---|---|---|---|
| vLLM | 启动阶段日志、引擎统计、错误 | stdout → Fluent Bit → CW `/application` | 14 天 |
| Envoy AI Gateway | JSON access log：`x-request-id`、路由、`upstream_host`、状态码、耗时、response flags、是否溢出 | 同上 | 30 天 |
| Worker（异步） | 结构化 JSON：request_id、S3 key、耗时、结果状态 | 同上 | 30 天 |
| coldstart-tracer | 冷启动 JSON 记录（量很小） | 同上，或通过路由单独写一个 log group | **90 天**（趋势分析） |
| Karpenter / KEDA | 机型选择、ICE、扩缩容决策（JSON） | 同上 | 30 天 |
| K8s Events | Nominated / Pulled / FailedScheduling / Killing 等 | event-exporter → CW | 30 天 |
| EKS 控制面 | scheduler（必开）、audit（可选，量大） | EKS control plane logging | 30 天 |
| 节点（Bottlerocket） | kubelet、containerd 的 journal | add-on 的 dataplane 日志（**需验证 Bottlerocket 支持**）；临时排查用 `logdog` | 7 天 |
| GPU 故障 | XID、驱动错误 | EKS Node Monitoring Agent 把它转成 Node condition/Event，并联动 node auto repair | — |

### 5.2 日志原则

- **不记录 prompt 和图片**：生产环境不打开 `--enable-log-requests`。base64 图片既大又可能含敏感信息，日志里只保留 request_id 和元数据。
- **结构化**：可以用 `VLLM_LOGGING_CONFIG_PATH` 给 vLLM 配置 JSON formatter（镜像里需要装 `python-json-logger`）。网关和 Worker 统一输出 JSON。
- **request_id 全链路透传**：网关生成 `x-request-id`；vLLM 开启 `--enable-request-id-headers` 后会回写这个头；Worker 和网关日志用同一个 ID。

---

## 6. 请求级 Trace

- vLLM：加 `--otlp-traces-endpoint=http://adot-collector:4317`。每个请求生成一个 span，属性里带 `gen_ai.latency.time_in_queue`、`time_to_first_token`、`time_in_model_prefill` 等。需要更细的引擎内部耗时可以加 `--collect-detailed-traces`，**但有性能开销，只在压测时开**。
- Envoy：开启 OTel tracing，透传 `traceparent`。一条 trace 串起「网关 → vLLM Pod」；发生溢出时则是「网关 → Bedrock」。
- ADOT Collector 用 **tail sampling**：错误和慢请求（超过 SLA）100% 保留，其余按 5% 采样。
- 跳转路径：在 Grafana 的 P95 突刺处查 Logs Insights 找到 request_id，再到 X-Ray 看这条 trace，最后通过 `upstream_host` 定位到具体 Pod 和节点。

---

## 7. 告警

> 两种模式下告警都由 CloudWatch Alarms 实现，通知走 SNS。下表的条件用 PromQL 写是为了好读，具体怎么落地见 §1.2。

| 告警 | 条件（初值，PoC 中调整） | 级别 | 说明 |
|---|---|---|---|
| 扩容失明 | `increase(keda_scaler_errors_total[5m]) > 0` | P1 | Prometheus 挂掉时 KEDA 拿不到指标；同时配置 ScaledObject 的 `fallback.replicas` 兜底 |
| 扩不出节点 | Pending Pod 超过 5 min，或 `karpenter_cloudprovider_instance_launch_failures_total` 增长 | P1 | 通常是 ICE 或配额问题；tracer 的 stuck 记录会给出原因 |
| 冷启动劣化 | 冷节点 `coldstart_total_seconds` 1h 内 P90 > 180s | P2 | 对照阶段分布，看是哪一段退化了 |
| 容量顶格 | desired == maxReplicas 且 `num_requests_waiting > 0` 持续 3 min | P1 | 需要提高 maxReplicas、NodePool limits 或配额 |
| SLA 违约 | TTFT 或 E2E 的 P95 > SLA 持续 5 min | P1 | — |
| KV 饱和 | `vllm:kv_cache_usage_perc > 0.9` 持续 5 min，或 preemption 增长 | P2 | 检查图片像素上限和 max-num-seqs |
| GPU 故障 | `DCGM_FI_DEV_XID_ERRORS > 0` | P1 | 摘除节点，交给 node auto repair |
| 溢出过多 | Bedrock 溢出比例 > 5%（15 min） | P2 | 说明 buffer 或冷启动不达标 |

---

## 8. 组件清单与成本

| 组件 | 部署方式 |
|---|---|
| kube-prometheus-stack（Prometheus 本地保留 6h 并 remote_write 到 AMP，含 KSM 和 node-exporter） | Helm |
| DCGM Exporter（只跑在 GPU 节点，带 GPU taint 的 toleration） | Helm |
| amazon-cloudwatch-observability（Fluent Bit；关闭自带的 GPU 采集） | EKS add-on |
| ADOT Collector（OTLP → X-Ray，tail sampling；Prometheus federate → CloudWatch EMF） | EKS add-on |
| kubernetes-event-exporter | Helm |
| coldstart-tracer | 自研 Deployment（1 副本，跑在 CPU 节点） |
| CloudWatch Dashboards、Alarms、metric filters、SNS（两种模式都有） | IaC |
| AMP workspace、AMG workspace（只在 Grafana 模式；IdC 或 SAML 登录） | IaC / `qwenctl observability grafana` |
| EKS 控制面日志（scheduler） | IaC |

**成本估算（量级）**
- CloudWatch 关键指标：白名单约 30~50 个指标，维度只细到机型和 AZ，约 **$30~100/月**。
- AMP（只在 Grafana 模式）：按样本数计费。远端只写需要的指标（用 relabel 删掉不用的 series），控制在约 1 万活跃序列、15s 间隔，约 **$100~200/月**。
- CloudWatch Logs：关掉请求日志后，日志量主要是 access log，PoC 期间预计 **< $50/月**。
- X-Ray：采样后可以忽略不计。AMG：$9/编辑者/月。

---

## 9. 纳入 PoC 计划

| 时间 | 交付 |
|---|---|
| **W1（压测前必须就绪）** | 指标栈 + 日志 + event-exporter + 面板 ②③④；用 §2.3 的现成指标拿到**未优化的冷启动基线** |
| **W2 前半周** | coldstart-tracer + 面板 ① + X-Ray 瀑布图。每做一项优化（快照、streamer、编译缓存）都拿 tracer 数据对比 |
| W3 | 请求级 trace + 告警；跑压测时开 `--collect-detailed-traces` 分析延迟构成 |
| W4 | 冷启动「默认 vs 优化」分段对比报告（直接从 tracer 数据生成），作为 Demo 素材 |
