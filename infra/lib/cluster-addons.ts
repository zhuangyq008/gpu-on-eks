/**
 * 客户 Web 应用需要的集群插件（EKS 托管 add-on）：
 * - metrics-server（HPA 用）、EBS CSI 驱动（PVC 用）：始终安装
 * - ExternalDNS：addons.externalDns.enabled 时安装，只能改配置里列出的 Route 53 托管区域
 *
 * configurationValues 的字段按 `aws eks describe-addon-configuration` 返回的 schema 核对过（版本见 versions.json），
 * 这些 schema 都是 additionalProperties: false，写了不存在的字段 EKS 会拒绝创建 add-on。
 * 加密的 gp3 默认 StorageClass 由 qwenctl 创建（cli/qwenctl/addons.py），这里不开 add-on 自带的 defaultStorageClass。
 */
import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { DeploymentConfig } from './config';
import { podIdentityPrincipal } from './pod-identity';
import { Versions, addonVersion } from './versions';

// add-on 的 podIdentityConfiguration / EKS 文档里的 namespace 和 ServiceAccount（Pod Identity 信任策略要用）
export const EBS_CSI_NAMESPACE = 'kube-system';
export const EBS_CSI_SERVICE_ACCOUNT = 'ebs-csi-controller-sa';
export const EXTERNAL_DNS_NAMESPACE = 'external-dns';
export const EXTERNAL_DNS_SERVICE_ACCOUNT = 'external-dns';
/** EKS 文档推荐的 EBS CSI 托管策略：只能管理带 ebs.csi.aws.com/cluster=true tag 的卷和快照 */
export const EBS_CSI_MANAGED_POLICY = 'AmazonEBSCSIDriverPolicyV2';
/**
 * ExternalDNS 管理的记录类型：显式写进 add-on 配置（v0.24.0 的默认值也是 A、AAAA、CNAME），
 * 以后 add-on 改了默认值也不会去写 IAM 不允许的类型。ALB / NLB 的 alias 记录是 A / AAAA。
 */
export const EXTERNAL_DNS_MANAGED_RECORD_TYPES: readonly string[] = ['A', 'AAAA', 'CNAME'];
/** IAM 允许改的记录类型：上面几种 + TXT registry 的所有权记录（registry 写的是 TXT 类型） */
export const EXTERNAL_DNS_IAM_RECORD_TYPES: readonly string[] = [...EXTERNAL_DNS_MANAGED_RECORD_TYPES, 'TXT'];
/**
 * IAM 只允许 CREATE / UPSERT，和 policy: upsert-only 一起在 IAM 层强制「只增不删」。
 * 代价：v0.24.0 的 AWS provider 遇到「记录类型变了、A 记录在 alias 和非 alias 之间切换、set identifier 或
 * 路由策略变了」这几种更新时会发 DELETE + CREATE（provider/aws/aws.go requiresDeleteCreate），这类变更会被拒绝，
 * 需要人工在 Route 53 里处理；同一批里的其他记录不受影响（失败的批次会按记录拆开重试）。
 */
export const EXTERNAL_DNS_IAM_CHANGE_ACTIONS: readonly string[] = ['CREATE', 'UPSERT'];

/** 让 Deployment 类插件落在系统节点组上 */
export interface SystemPlacement {
  readonly nodeSelector: Readonly<Record<string, string>>;
  readonly tolerations: ReadonlyArray<Readonly<Record<string, string>>>;
}

export interface ClusterAddonsProps {
  readonly config: DeploymentConfig;
  readonly versions: Versions;
  readonly cluster: eks.CfnCluster;
  /** 插件的 Pod 要调度到系统节点上才能 Ready */
  readonly nodegroup: eks.CfnNodegroup;
  readonly placement: SystemPlacement;
}

export class ClusterAddons extends Construct {
  public readonly ebsCsiRole: iam.Role;
  public readonly externalDnsRole?: iam.Role;

  constructor(scope: Construct, id: string, props: ClusterAddonsProps) {
    super(scope, id);
    const { config, placement } = props;

    this.addon(props, 'metrics-server', {
      replicas: 2,
      ...placement,
      podDisruptionBudget: { enabled: true, maxUnavailable: 1 },
    });

    this.ebsCsiRole = this.ebsCsiDriverRole(config);
    const ebs = this.addon(
      props,
      'aws-ebs-csi-driver',
      {
        // 只改 controller；node DaemonSet 保持默认（tolerateAllTaints: true），GPU 节点上也能挂卷
        controller: { ...placement },
        defaultStorageClass: { enabled: false },
      },
      [{ serviceAccount: EBS_CSI_SERVICE_ACCOUNT, roleArn: this.ebsCsiRole.roleArn }],
    );
    // 角色的 KMS 内联策略是单独的资源：先附加好再装 add-on
    ebs.node.addDependency(this.ebsCsiRole);

    if (config.addons.externalDns.enabled) {
      this.externalDnsRole = this.externalDnsIamRole(config);
      const dns = this.addon(
        props,
        'external-dns',
        externalDnsValues(config, placement),
        [{ serviceAccount: EXTERNAL_DNS_SERVICE_ACCOUNT, roleArn: this.externalDnsRole.roleArn }],
      );
      dns.node.addDependency(this.externalDnsRole);
    }
  }

  private addon(
    props: ClusterAddonsProps,
    name: string,
    values: Record<string, unknown>,
    podIdentityAssociations?: eks.CfnAddon.PodIdentityAssociationProperty[],
  ): eks.CfnAddon {
    const addon = new eks.CfnAddon(this, `Addon-${name}`, {
      clusterName: props.cluster.ref,
      addonName: name,
      addonVersion: addonVersion(props.versions, name),
      resolveConflicts: 'OVERWRITE',
      configurationValues: JSON.stringify(values),
      // add-on 自己创建 Pod Identity 关联（namespace 由 add-on 决定），删 add-on 时一起删
      podIdentityAssociations,
    });
    addon.addResourceDependency(props.nodegroup, '插件的 Pod 需要系统节点才能 Ready');
    return addon;
  }

  private ebsCsiDriverRole(config: DeploymentConfig): iam.Role {
    const role = new iam.Role(this, 'EbsCsiRole', {
      description: `EBS CSI driver of EKS cluster ${config.name} (EKS Pod Identity)`,
      assumedBy: podIdentityPrincipal(config.name, EBS_CSI_NAMESPACE, EBS_CSI_SERVICE_ACCOUNT),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(EBS_CSI_MANAGED_POLICY)],
    });
    const kmsKeyArn = config.advanced.compliance.kms_key_arn;
    if (kmsKeyArn) {
      // 客户 CMK 加密的卷：EKS 文档 ebs-csi.html 给出的 KMS 权限（授权只允许给 AWS 资源使用）
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CmkGrantsForEbs',
          actions: ['kms:CreateGrant', 'kms:ListGrants', 'kms:RevokeGrant'],
          resources: [kmsKeyArn],
          conditions: { Bool: { 'kms:GrantIsForAWSResource': 'true' } },
        }),
      );
      role.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CmkCryptoForEbs',
          actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
          resources: [kmsKeyArn],
        }),
      );
    }
    return role;
  }

  /**
   * 托管区域级别的权限（Service Authorization Reference，Route 53）：
   * ChangeResourceRecordSets、ListResourceRecordSets、ListTagsForResources 支持 hostedzone 资源；
   * ListHostedZones 不支持资源级限制，只能给 "*"（只读，列出区域名和 ID）。
   * 没有用 add-on 推荐的 AmazonRoute53FullAccess：那等于能改账号里所有域名。
   *
   * 改记录还限定了记录类型：route53:ChangeResourceRecordSetsRecordTypes 是多值键（ArrayOfString），
   * 写法照 Route 53 开发者指南 specifying-rrset-conditions.html「limit access to creating and editing only
   * the specified types of DNS records」示例用 ForAllValues:StringEquals；凭证泄露时也改不了 NS / MX / SOA 等记录。
   * 同样用 route53:ChangeResourceRecordSetsActions（多值键，取值 CREATE / UPSERT / DELETE）禁止 DELETE。
   * 再加 Null = false：ForAllValues 在请求里没有这个键时也返回 true，IAM 用户指南
   * reference_policies_condition-single-vs-multi-valued-context-keys.html 要求 Allow 语句同时检查键存在。
   */
  private externalDnsIamRole(config: DeploymentConfig): iam.Role {
    const partition = cdk.Stack.of(this).partition;
    const zoneArns = config.addons.externalDns.hostedZones.map(
      (z) => `arn:${partition}:route53:::hostedzone/${z.id}`,
    );
    const role = new iam.Role(this, 'ExternalDnsRole', {
      description: `ExternalDNS of EKS cluster ${config.name} (EKS Pod Identity), listed hosted zones only`,
      assumedBy: podIdentityPrincipal(config.name, EXTERNAL_DNS_NAMESPACE, EXTERNAL_DNS_SERVICE_ACCOUNT),
    });
    const recordTypes = 'route53:ChangeResourceRecordSetsRecordTypes';
    const changeActions = 'route53:ChangeResourceRecordSetsActions';
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ChangeRecordsInListedZones',
        actions: ['route53:ChangeResourceRecordSets'],
        resources: zoneArns,
        conditions: {
          'ForAllValues:StringEquals': {
            [recordTypes]: EXTERNAL_DNS_IAM_RECORD_TYPES,
            [changeActions]: EXTERNAL_DNS_IAM_CHANGE_ACTIONS,
          },
          Null: { [recordTypes]: 'false', [changeActions]: 'false' },
        },
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadRecordsInListedZones',
        actions: ['route53:ListResourceRecordSets', 'route53:ListTagsForResources'],
        resources: zoneArns,
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({ sid: 'ListHostedZones', actions: ['route53:ListHostedZones'], resources: ['*'] }),
    );
    return role;
  }
}

/**
 * domainFilters 是按域名后缀匹配：账号里同名的私有区域、或 sub.example.com 这样的子区域也会被选中，
 * 而 IAM 只放行列出的区域 ID，ExternalDNS 读这些区域时 AccessDenied 会让整轮同步失败。
 * 所以再用 --zone-id-filter 把范围收窄到和 IAM 一致（schema 没有单独的字段，走 extraArgs）。
 */
export function externalDnsValues(config: DeploymentConfig, placement: SystemPlacement): Record<string, unknown> {
  const zones = config.addons.externalDns.hostedZones;
  return {
    domainFilters: [...new Set(zones.map((z) => z.name))],
    extraArgs: zones.map((z) => `--zone-id-filter=${z.id}`),
    managedRecordTypes: EXTERNAL_DNS_MANAGED_RECORD_TYPES,
    // 只增改不删：误删 Ingress 不会连带删掉 DNS 记录
    policy: 'upsert-only',
    // TXT 记录里的所有者标记：只管理本集群创建的记录。带上 region：同一账号不同 region 里有同名集群时不会互相认领
    txtOwnerId: `${config.name}-${config.region}`,
    sources: ['ingress', 'service'],
    ...placement,
  };
}
