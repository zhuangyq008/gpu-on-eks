/**
 * EKS 栈：集群、系统节点组、核心 add-on、访问授权、Karpenter 的 AWS 侧资源。
 *
 * 全部用 CloudFormation 原生资源（AWS::EKS::*），不用 CDK 的 kubectl Lambda 自定义资源。
 * 集群内的组件（Karpenter、KEDA 等 Helm chart）由执行 qwenctl 的客户端安装（docs/03 §1）。
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { DeploymentConfig } from './config';
import { KarpenterIam } from './karpenter-iam';
import { ADDONS_AFTER_NODES, ADDONS_BEFORE_NODES, Versions } from './versions';

export const SYSTEM_NODE_LABEL = 'qwenctl/node-role';
const CLUSTER_ADMIN_POLICY = 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy';
// audit：谁用集群管理员权限做了什么（安全审计）
const CONTROL_PLANE_LOG_TYPES = ['api', 'audit', 'authenticator', 'scheduler'];

export interface EksStackProps extends cdk.StackProps {
  readonly config: DeploymentConfig;
  readonly versions: Versions;
  readonly vpc: ec2.IVpc;
}

export class EksStack extends cdk.Stack {
  public readonly cluster: eks.CfnCluster;
  public readonly karpenter: KarpenterIam;

  constructor(scope: Construct, id: string, props: EksStackProps) {
    super(scope, id, props);
    const { config, versions, vpc } = props;
    const privateSubnetIds = vpc.privateSubnets.map((s) => s.subnetId);

    // EKS 会往 /aws/eks/<name>/cluster 写控制面日志；先建好它才能设置保留期（默认永不过期）
    const controlPlaneLogs = new logs.LogGroup(this, 'ControlPlaneLogs', {
      logGroupName: `/aws/eks/${config.name}/cluster`,
      retention: config.advanced.observability.log_retention_days.access as logs.RetentionDays,
      // 删掉重建同名集群时，保留的旧日志组会让创建失败
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.cluster = createCluster(this, config, versions, privateSubnetIds);
    this.cluster.node.addDependency(controlPlaneLogs);
    addAdminAccess(this, this.cluster, config);
    const before = addAddons(this, this.cluster, versions, ADDONS_BEFORE_NODES);
    const nodegroup = createSystemNodegroup(this, this.cluster, config, privateSubnetIds);
    before.forEach((addon) => nodegroup.addResourceDependency(addon, '节点需要 VPC CNI 等 DaemonSet 才能 Ready'));
    addAddons(this, this.cluster, versions, ADDONS_AFTER_NODES).forEach((addon) =>
      addon.addResourceDependency(nodegroup, 'CoreDNS 等需要节点才能 Ready'),
    );
    this.karpenter = new KarpenterIam(this, 'Karpenter', { clusterName: config.name, cluster: this.cluster });

    const outputs: Record<string, string> = {
      ClusterName: this.cluster.ref,
      ClusterSecurityGroupId: this.cluster.attrClusterSecurityGroupId,
      KarpenterNodeRoleName: this.karpenter.nodeRole.roleName,
      KarpenterInterruptionQueueName: this.karpenter.interruptionQueue.queueName,
      PrivateSubnetIds: privateSubnetIds.join(','),
    };
    for (const [key, value] of Object.entries(outputs)) {
      new cdk.CfnOutput(this, key, { value });
    }
  }
}

function createCluster(
  scope: Construct,
  config: DeploymentConfig,
  versions: Versions,
  subnetIds: string[],
): eks.CfnCluster {
  const role = new iam.Role(scope, 'ClusterRole', {
    assumedBy: new iam.ServicePrincipal('eks.amazonaws.com'),
    managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEKSClusterPolicy')],
  });
  const kmsKeyArn = config.advanced.compliance.kms_key_arn;
  return new eks.CfnCluster(scope, 'Cluster', {
    name: config.name,
    version: versions.kubernetes,
    roleArn: role.roleArn,
    resourcesVpcConfig: {
      subnetIds,
      endpointPrivateAccess: true,
      // 默认同时开公网端点（IAM 认证），CloudShell 才能执行 kubectl/helm；private 时需要在 VPC 内运行 qwenctl
      endpointPublicAccess: config.advanced.network.eks_endpoint === 'public-and-private',
    },
    accessConfig: {
      authenticationMode: 'API',
      // 不让 CloudFormation 执行角色自动成为集群管理员，管理员只按 access entry 显式授予
      bootstrapClusterCreatorAdminPermissions: false,
    },
    // 核心 add-on 由下面的 AWS::EKS::Addon 托管，版本锁定在 versions.json
    bootstrapSelfManagedAddons: false,
    logging: { clusterLogging: { enabledTypes: CONTROL_PLANE_LOG_TYPES.map((type) => ({ type })) } },
    encryptionConfig: kmsKeyArn ? [{ provider: { keyArn: kmsKeyArn }, resources: ['secrets'] }] : undefined,
  });
}

function addAdminAccess(scope: Construct, cluster: eks.CfnCluster, config: DeploymentConfig): void {
  const admins = [config.adminPrincipalArn, ...config.advanced.compliance.extra_admin_role_arns];
  [...new Set(admins)].forEach((principalArn, index) => {
    new eks.CfnAccessEntry(scope, `AdminAccess${index}`, {
      clusterName: cluster.ref,
      principalArn,
      type: 'STANDARD',
      accessPolicies: [{ policyArn: CLUSTER_ADMIN_POLICY, accessScope: { type: 'cluster' } }],
    });
  });
}

function addAddons(
  scope: Construct,
  cluster: eks.CfnCluster,
  versions: Versions,
  names: readonly string[],
): eks.CfnAddon[] {
  return names.map((name) => {
    const version = versions.eksAddons[name];
    if (!version) {
      throw new Error(`versions.json 里缺少 EKS add-on ${name} 的版本`);
    }
    return new eks.CfnAddon(scope, `Addon-${name}`, {
      clusterName: cluster.ref,
      addonName: name,
      addonVersion: version,
      resolveConflicts: 'OVERWRITE',
    });
  });
}

function createSystemNodegroup(
  scope: Construct,
  cluster: eks.CfnCluster,
  config: DeploymentConfig,
  subnetIds: string[],
): eks.CfnNodegroup {
  const role = new iam.Role(scope, 'SystemNodeRole', {
    assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
    managedPolicies: [
      'AmazonEKSWorkerNodePolicy',
      'AmazonEKS_CNI_Policy',
      'AmazonEC2ContainerRegistryPullOnly',
      'AmazonSSMManagedInstanceCore',
    ].map((name) => iam.ManagedPolicy.fromAwsManagedPolicyName(name)),
  });
  const launchTemplate = systemLaunchTemplate(scope, config.name);
  return new eks.CfnNodegroup(scope, 'SystemNodegroup', {
    clusterName: cluster.ref,
    nodeRole: role.roleArn,
    launchTemplate: { id: launchTemplate.ref, version: launchTemplate.attrLatestVersionNumber },
    subnets: subnetIds,
    amiType: 'BOTTLEROCKET_ARM_64',
    capacityType: 'ON_DEMAND',
    instanceTypes: [config.systemNodes.instanceType],
    scalingConfig: { minSize: 2, desiredSize: 2, maxSize: 3 },
    updateConfig: { maxUnavailable: 1 },
    labels: { [SYSTEM_NODE_LABEL]: 'system' },
    // 只跑 Karpenter 和插件；客户业务落在 Karpenter 的通用节点池
    taints: [{ key: 'CriticalAddonsOnly', value: 'true', effect: 'NO_SCHEDULE' }],
  });
}

/** 和 Karpenter 节点一致：IMDSv2 + hop limit 1（Pod 拿不到节点角色凭证），EBS 加密 */
function systemLaunchTemplate(scope: Construct, name: string): ec2.CfnLaunchTemplate {
  const tags = [{ key: 'Name', value: `${name}-system` }];
  return new ec2.CfnLaunchTemplate(scope, 'SystemNodeLaunchTemplate', {
    launchTemplateData: {
      metadataOptions: { httpEndpoint: 'enabled', httpTokens: 'required', httpPutResponseHopLimit: 1 },
      // Bottlerocket：xvda 是只读的系统盘，xvdb 放容器镜像和数据
      blockDeviceMappings: [
        { deviceName: '/dev/xvda', ebs: { volumeSize: 4, volumeType: 'gp3', encrypted: true, deleteOnTermination: true } },
        { deviceName: '/dev/xvdb', ebs: { volumeSize: 50, volumeType: 'gp3', encrypted: true, deleteOnTermination: true } },
      ],
      tagSpecifications: [
        { resourceType: 'instance', tags },
        { resourceType: 'volume', tags },
      ],
    },
  });
}
