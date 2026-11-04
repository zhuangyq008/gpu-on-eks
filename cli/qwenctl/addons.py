"""集群插件的 Helm values：集群内 Prometheus（kube-prometheus-stack 精简版）和 KEDA。

都跑在系统节点组上（带 CriticalAddonsOnly 污点），不依赖通用节点池扩容。
Prometheus 只做 KEDA 的扩缩容查询：保留 6 小时、不对外写；可观测性（CloudWatch 模式）另行接入（docs/02 §1.2）。

扩缩容指标决定 GPU 副本数（也就是成本），所以 Prometheus 只读两个可信命名空间里的监控对象：
推理服务命名空间（vLLM 的 PodMonitor）和 monitoring（kube-state-metrics 等自带的 ServiceMonitor）。
客户业务命名空间里的 PodMonitor / 规则不会被加载，没法伪造 vllm:* 或 kube_pod_* 指标。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

PROMETHEUS_NAMESPACE = "monitoring"
PROMETHEUS_RELEASE = "kps"
PROMETHEUS_URL = f"http://{PROMETHEUS_RELEASE}-prometheus.{PROMETHEUS_NAMESPACE}.svc:9090"
KEDA_NAMESPACE = "keda"


def _on_system_nodes() -> Dict[str, Any]:
    """每次返回新对象：同一个 dict 嵌进多处 values，下游原地修改会互相影响。"""
    return {
        "nodeSelector": {"qwenctl/node-role": "system"},
        "tolerations": [{"key": "CriticalAddonsOnly", "operator": "Exists"}],
    }


def namespace_manifest(name: str) -> Dict[str, Any]:
    """强制 Pod Security baseline（禁止特权、hostPath、host 网络等），对 restricted 只告警。"""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                "pod-security.kubernetes.io/enforce": "baseline",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        },
    }


def _only(*namespaces: str) -> Dict[str, Any]:
    return {
        "matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": list(namespaces)}
        ]
    }


def prometheus_values(serving_namespace: str) -> Dict[str, Any]:
    return {
        "fullnameOverride": PROMETHEUS_RELEASE,
        "grafana": {"enabled": False},
        "alertmanager": {"enabled": False},
        "defaultRules": {"create": False},
        # 只保留 KEDA 要用的：vLLM 指标（PodMonitor）+ kube-state-metrics（Pod 状态）。
        # 控制面组件在 EKS 上抓不到；kubelet/cAdvisor、API server、CoreDNS 序列多、占内存；
        # node-exporter 要 hostNetwork + hostPID 跑在每个节点（包括客户业务节点），都不需要
        "kubeControllerManager": {"enabled": False},
        "kubeScheduler": {"enabled": False},
        "kubeEtcd": {"enabled": False},
        "kubeProxy": {"enabled": False},
        "kubelet": {"enabled": False},
        "kubeApiServer": {"enabled": False},
        "coreDns": {"enabled": False},
        "nodeExporter": {"enabled": False},
        "prometheusOperator": {
            **_on_system_nodes(),
            "admissionWebhooks": {"patch": _on_system_nodes()},
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
        },
        "kube-state-metrics": {
            **_on_system_nodes(),
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        },
        "prometheus": {
            "prometheusSpec": {
                **_on_system_nodes(),
                "retention": "6h",
                "scrapeInterval": "15s",
                "evaluationInterval": "30s",
                # 不要求 release 标签（vLLM 的 PodMonitor 不是这个 chart 建的），但只看可信命名空间
                "serviceMonitorSelectorNilUsesHelmValues": False,
                "podMonitorSelectorNilUsesHelmValues": False,
                "podMonitorNamespaceSelector": _only(serving_namespace),
                "serviceMonitorNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "ruleNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "probeNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "scrapeConfigNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "resources": {"requests": {"cpu": "300m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}},
            }
        },
    }


def keda_values() -> Dict[str, Any]:
    return {
        **_on_system_nodes(),
        # 只用 Prometheus 触发器、不用 TriggerAuthentication：不需要读全集群的 Secret
        "permissions": {
            "operator": {"restrict": {"secret": True}},
            "metricServer": {"restrict": {"secret": True}},
        },
        "resources": {
            "operator": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "metricServer": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "webhooks": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        },
    }


# ---- 客户 Web 应用用到的插件 ----
# metrics-server、EBS CSI 驱动、ExternalDNS 是 EKS 托管 add-on（CloudFormation 管理，见 infra/lib）；
# 这里只放客户端用 kubectl / helm 安装的部分。

DEFAULT_STORAGE_CLASS = "gp3"
LBC_RELEASE = "aws-load-balancer-controller"
LBC_NAMESPACE = "kube-system"
# 和 CDK 里 Pod Identity 关联的 ServiceAccount 一致（infra/lib/load-balancer-controller.ts）
LBC_SERVICE_ACCOUNT = "aws-load-balancer-controller"


def gp3_storage_class(kms_key_arn: Any = None) -> Dict[str, Any]:
    """默认 StorageClass：gp3、加密、可扩容。等 Pod 调度后再建卷，卷和 Pod 落在同一个 AZ。

    EBS CSI 托管 add-on 自带的默认 StorageClass 不显式要求加密，所以不用它。
    StorageClass 的参数不能修改：以后改 CMK 需要先删掉它（已经建好的卷不受影响）。"""
    parameters = {"type": "gp3", "encrypted": "true", **({"kmsKeyId": kms_key_arn} if kms_key_arn else {})}
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {
            "name": DEFAULT_STORAGE_CLASS,
            "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
        },
        "provisioner": "ebs.csi.aws.com",
        "parameters": parameters,
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
    }


# 不允许用 alb IngressClass 发布的命名空间：系统组件，以及没有认证的推理服务和 Prometheus。
# 否则能在这些命名空间建 Ingress 的人，可以把它们直接发布到公网
SYSTEM_NAMESPACES = (
    "kube-system",
    "kube-public",
    "kube-node-lease",
    PROMETHEUS_NAMESPACE,
    KEDA_NAMESPACE,
    "external-dns",
)


def load_balancer_controller_values(
    cluster: str, region: str, vpc_id: str, protected_namespaces: Tuple[str, ...] = ()
) -> Dict[str, Any]:
    """AWS Load Balancer Controller：客户 Web 应用的 ALB（Ingress）和 NLB（Service）。

    节点的 IMDS hop limit 是 1，控制器拿不到实例元数据，region 和 vpcId 要显式传。"""
    blocked = sorted({*SYSTEM_NAMESPACES, *protected_namespaces})
    return {
        **_on_system_nodes(),
        "clusterName": cluster,
        "region": region,
        "vpcId": vpc_id,
        "replicaCount": 2,
        "serviceAccount": {"create": True, "name": LBC_SERVICE_ACCOUNT},
        # 这个 webhook 默认 failurePolicy=Fail、对所有命名空间的所有 Service 生效：
        # 控制器不可用时，集群里任何 Service 都建不了。关掉它，代价是 NLB 要在 Service 上
        # 显式写 loadBalancerClass: service.k8s.aws/nlb（ALB Ingress 不受影响）
        "enableServiceMutatorWebhook": False,
        # WAF Classic（waf-regional）在马来西亚等较新的 region 没有端点；WAF 用 WAFv2（enableWafv2 默认开启）
        "enableWaf": False,
        # Shield Advanced 需要单独订阅，不用；对应的 IAM 权限也已删掉
        "enableShield": False,
        # HTTPS 监听器默认只允许 TLS 1.2 / 1.3 的安全套件；直接转发到 Pod（不经过 NodePort）
        "defaultSSLPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
        "defaultTargetType": "ip",
        "ingressClassParams": {
            "create": True,
            "spec": {
                "namespaceSelector": {
                    "matchExpressions": [
                        {"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": blocked}
                    ]
                }
            },
        },
        "podDisruptionBudget": {"maxUnavailable": 1},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
    }


# ---- 准入策略（ValidatingAdmissionPolicy：在 API server 内部求值，不依赖任何 webhook 或控制器在线） ----
LB_CLASS_POLICY = "qwenctl-lb-service-class"
PUBLIC_ALB_WAF_POLICY = "qwenctl-public-alb-waf"
WAF_ANNOTATION = "alb.ingress.kubernetes.io/wafv2-acl-arn"
SCHEME_ANNOTATION = "alb.ingress.kubernetes.io/scheme"


def _policy(name: str, group: str, resource: str, validations: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": name},
        "spec": {
            "failurePolicy": "Fail",
            # 正在删除的对象不检查：策略生效（或收紧）之前建的不合规对象，删除时控制器要 UPDATE 摘掉 finalizer，
            # 拒绝的话对象会一直卡在 Terminating，负载均衡器也删不掉
            "matchConditions": [
                {"name": "not-being-deleted", "expression": "!has(object.metadata.deletionTimestamp)"}
            ],
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": [group],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": [resource],
                    }
                ]
            },
            "validations": [{"expression": e, "message": m} for e, m in validations],
        },
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": name},
        "spec": {"policyName": name, "validationActions": ["Deny"]},
    }
    return [policy, binding]


ADMISSION_POLICIES = (LB_CLASS_POLICY, PUBLIC_ALB_WAF_POLICY)
LEGACY_CLASS_ANNOTATION = "kubernetes.io/ingress.class"


def _annotation(key: str) -> str:
    return f"(has(object.metadata.annotations) && '{key}' in object.metadata.annotations)"


def admission_policies(
    region: str, account: str, protected_namespaces: Tuple[str, ...] = ()
) -> List[Dict[str, Any]]:
    """装了 Load Balancer Controller 之后的护栏（防误操作，不是防恶意）：

    Service：
    1. type=LoadBalancer 必须写 loadBalancerClass：没写就会落到 EKS 自带的旧控制器，建出默认对公网开放、
       没有安全组也挂不了 WAF 的 Classic LB（控制器的 Service webhook 我们关掉了）。
    2. 系统、推理、监控命名空间里不能建 type=LoadBalancer：推理服务和 Prometheus 没有认证。
    Ingress：
    3. 不能用旧的 kubernetes.io/ingress.class: alb 注解：它绕过 IngressClassParams 的命名空间限制。
    4. 公网（internet-facing）Ingress 必须挂本账号、本 region 的 WAFv2 WebACL（注解值要是 WebACL ARN，空值、假值都不行）。
       准入策略查不到 WebACL 是否真的存在：ARN 写错时，见 docs/04 §5.8。
    """
    if not (re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region) and re.fullmatch(r"\d{12}", account)):
        # 两个值会拼进 CEL 表达式和正则
        raise ValueError(f"region 或账号不合法：{region!r} / {account!r}")
    blocked = sorted({*SYSTEM_NAMESPACES, *protected_namespaces})
    namespaces = "[" + ", ".join(f"'{n}'" for n in blocked) + "]"
    waf_arn = f"^arn:aws[a-z-]*:wafv2:{region}:{account}:regional/webacl/[A-Za-z0-9_-]+/[0-9a-f-]+$"
    service = _policy(
        LB_CLASS_POLICY,
        "",
        "services",
        [
            (
                "object.spec.type != 'LoadBalancer' || has(object.spec.loadBalancerClass)",
                (
                    "type=LoadBalancer 的 Service 必须写 spec.loadBalancerClass: service.k8s.aws/nlb（由 AWS Load "
                    "Balancer Controller 创建 NLB）；不写会建出默认对公网开放的 Classic LB"
                ),
            ),
            (
                f"object.spec.type != 'LoadBalancer' || !(namespaceObject.metadata.name in {namespaces})",
                f"这些命名空间里不能创建 type=LoadBalancer 的 Service（推理服务和监控没有认证）：{', '.join(blocked)}",
            ),
        ],
    )
    internet_facing = (
        f"{_annotation(SCHEME_ANNOTATION)} && "
        f"object.metadata.annotations['{SCHEME_ANNOTATION}'] == 'internet-facing'"
    )
    ingress = _policy(
        PUBLIC_ALB_WAF_POLICY,
        "networking.k8s.io",
        "ingresses",
        [
            (
                (
                    f"!{_annotation(LEGACY_CLASS_ANNOTATION)} || "
                    f"object.metadata.annotations['{LEGACY_CLASS_ANNOTATION}'] != 'alb'"
                ),
                f"请用 spec.ingressClassName: alb，不要用旧的 {LEGACY_CLASS_ANNOTATION} 注解",
            ),
            (
                (
                    f"!({internet_facing}) || ({_annotation(WAF_ANNOTATION)} && "
                    f"object.metadata.annotations['{WAF_ANNOTATION}'].matches('{waf_arn}'))"
                ),
                (
                    f"公网（internet-facing）ALB 必须挂 WAF：注解 {WAF_ANNOTATION} 的值要是本账号、本 region（{region}）的 "
                    "WAFv2 WebACL ARN（部署完成时打印的那个）"
                ),
            ),
        ],
    )
    return service + ingress
