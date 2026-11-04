/**
 * AWS Load Balancer Controller 的 AWS 侧资源（addons.ingress.enabled 时创建）：
 * - Pod Identity 角色：策略以上游 iam_policy.json 为基础按本集群收紧（lbc-policy.ts）
 * - WAF WebACL：客户在 Ingress 上加注解 alb.ingress.kubernetes.io/wafv2-acl-arn 挂载，这里不做关联
 *
 * 控制器本身由 qwenctl 用 Helm 安装（cli/qwenctl/addons.py），ServiceAccount 名字要和这里一致。
 */
import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';
import { LBC_POLICY_NAME, loadUpstreamPolicy, tightenPolicy } from './lbc-policy';
import { podIdentityRole } from './pod-identity';
import { PolicyLock, Versions } from './versions';

export const LBC_NAMESPACE = 'kube-system';
export const LBC_SERVICE_ACCOUNT = 'aws-load-balancer-controller';

/** IAM 角色内联策略合计字符数上限（不含空白） */
export const INLINE_POLICY_LIMIT = 10240;
/** 按来源 IP 限速：5 分钟窗口内超过这个请求数就 Block */
export const RATE_LIMIT_PER_5_MIN = 2000;
const RATE_LIMIT_WINDOW_SEC = 300;

export interface LoadBalancerControllerProps {
  readonly clusterName: string;
  /** 用 CfnCluster 的 ref 建立依赖，保证集群先创建 */
  readonly cluster: eks.CfnCluster;
  /** 安全组入站规则只允许改这个 VPC 里的（策略收紧规则 c） */
  readonly vpcId: string;
  readonly versions: Versions;
  /** 上游策略原文所在目录，测试用；默认 infra/lib/policies */
  readonly policiesDir?: string;
}

export class LoadBalancerController extends Construct {
  public readonly role: iam.Role;
  public readonly webAcl: wafv2.CfnWebACL;

  constructor(scope: Construct, id: string, props: LoadBalancerControllerProps) {
    super(scope, id);
    const { clusterName } = props;
    const stack = cdk.Stack.of(this);
    const lock = policyLock(props.versions);
    const upstream = loadUpstreamPolicy(LBC_POLICY_NAME, lock, props.policiesDir);
    const document = tightenPolicy(upstream, {
      partition: stack.partition,
      region: stack.region,
      account: stack.account,
      vpcId: props.vpcId,
      clusterName,
    });

    this.role = podIdentityRole(this, 'Role', {
      clusterName,
      cluster: props.cluster,
      namespace: LBC_NAMESPACE,
      serviceAccount: LBC_SERVICE_ACCOUNT,
      description: `AWS Load Balancer Controller of EKS cluster ${clusterName} (EKS Pod Identity)`,
    });
    // 用内联策略：收紧后的大小接近托管策略 6144 的上限；角色内联策略合计上限 10240（测试里有检查）
    new iam.Policy(this, 'Policy', {
      document: iam.PolicyDocument.fromJson(document),
      roles: [this.role],
    });
    this.webAcl = createWebAcl(this, clusterName);
  }
}

function policyLock(versions: Versions): PolicyLock {
  const lock = versions.policies?.[LBC_POLICY_NAME];
  if (!lock) {
    throw new Error(`versions.json 里缺少 policies.${LBC_POLICY_NAME}（开启 addons.ingress 时需要）`);
  }
  return lock;
}

type Rule = wafv2.CfnWebACL.RuleProperty;
type RuleActionOverride = wafv2.CfnWebACL.RuleActionOverrideProperty;

/**
 * REGIONAL WebACL，默认放行。规则按顺序：先丢掉已知恶意来源和 SQL 注入，再限速，最后做通用的请求内容检查。
 * WCU（describe-managed-rule-group 的 Capacity）：IpReputation 25 + SQLi 200 + 限速 2 + CommonRuleSet 700
 * + KnownBadInputs 200 = 1127，在 1500 的基础额度内（`aws wafv2 check-capacity` 核对过）。
 *
 * 请求体检查的限制：ALB 关联的 WebACL 只检查请求体的前 8 KB，超过 8 KB 的部分 WAF 看不到。
 * 超过 8 KB 的请求体，WAF 只检查前 8 KB，应用自己必须做校验（类型、大小、内容）。
 */
function createWebAcl(scope: Construct, clusterName: string): wafv2.CfnWebACL {
  // 指标名带集群名前缀：同一账号里多个集群的 WAF 指标不会混在一起
  const visibility = (name: string): wafv2.CfnWebACL.VisibilityConfigProperty => ({
    cloudWatchMetricsEnabled: true,
    sampledRequestsEnabled: true,
    metricName: `${clusterName}-${name}`,
  });
  const managed = (priority: number, group: string, metric: string, overrides?: RuleActionOverride[]): Rule => ({
    name: `AWS-${group}`,
    priority,
    overrideAction: { none: {} },
    statement: { managedRuleGroupStatement: { vendorName: 'AWS', name: group, ruleActionOverrides: overrides } },
    visibilityConfig: visibility(metric),
  });
  return new wafv2.CfnWebACL(scope, 'WebAcl', {
    name: `${clusterName}-ingress`,
    description: `Web ACL for ALB Ingress of EKS cluster ${clusterName}`,
    scope: 'REGIONAL',
    defaultAction: { allow: {} },
    visibilityConfig: visibility('web-acl'),
    rules: [
      managed(0, 'AWSManagedRulesAmazonIpReputationList', 'ip-reputation'),
      managed(1, 'AWSManagedRulesSQLiRuleSet', 'sqli'),
      {
        name: 'RateLimitPerIp',
        priority: 2,
        action: { block: {} },
        statement: {
          rateBasedStatement: {
            limit: RATE_LIMIT_PER_5_MIN,
            evaluationWindowSec: RATE_LIMIT_WINDOW_SEC,
            aggregateKeyType: 'IP',
          },
        },
        visibilityConfig: visibility('rate-limit-per-ip'),
      },
      // 客户的 Web 应用要上传图片：SizeRestrictions_BODY 会拦截超过 8 KB 的请求体，改成只计数。
      // 超过 8 KB 的请求体，WAF 只检查前 8 KB，应用自己必须做校验
      managed(3, 'AWSManagedRulesCommonRuleSet', 'common', [
        { name: 'SizeRestrictions_BODY', actionToUse: { count: {} } },
      ]),
      managed(4, 'AWSManagedRulesKnownBadInputsRuleSet', 'known-bad-inputs'),
    ],
  });
}
