/**
 * Karpenter 的 AWS 侧资源：节点角色、控制器角色（EKS Pod Identity）、中断队列和 EventBridge 规则。
 * 移植自 Karpenter v1.15.1 官方 cloudformation.yaml（getting-started），策略内容保持一致；
 * 区别：角色和策略名由 CloudFormation 生成（IAM 是全局的，固定名字在多 region 部署时会冲突）。
 */
import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { podIdentityPrincipal } from './pod-identity';

export const KARPENTER_NAMESPACE = 'kube-system';
export const KARPENTER_SERVICE_ACCOUNT = 'karpenter';

export interface KarpenterIamProps {
  readonly clusterName: string;
  /** 用 CfnCluster 的 ref 建立依赖，保证集群先创建 */
  readonly cluster: eks.CfnCluster;
}

type Statement = Record<string, unknown>;

export class KarpenterIam extends Construct {
  public readonly nodeRole: iam.Role;
  public readonly controllerRole: iam.Role;
  public readonly interruptionQueue: sqs.Queue;

  constructor(scope: Construct, id: string, props: KarpenterIamProps) {
    super(scope, id);
    const { clusterName, cluster } = props;
    const stack = cdk.Stack.of(this);
    const ctx: PolicyContext = { partition: stack.partition, region: stack.region, account: stack.account, clusterName };

    this.nodeRole = new iam.Role(this, 'NodeRole', {
      description: `Karpenter-managed nodes of EKS cluster ${clusterName}`,
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        'AmazonEKS_CNI_Policy',
        'AmazonEKSWorkerNodePolicy',
        'AmazonEC2ContainerRegistryPullOnly',
        'AmazonSSMManagedInstanceCore',
      ].map((name) => iam.ManagedPolicy.fromAwsManagedPolicyName(name)),
    });

    this.interruptionQueue = this.createInterruptionQueue();

    this.controllerRole = new iam.Role(this, 'ControllerRole', {
      description: `Karpenter controller of EKS cluster ${clusterName} (EKS Pod Identity)`,
      // 只允许本集群 kube-system/karpenter 这个 ServiceAccount 使用（Pod Identity 会带上这些会话 tag）
      assumedBy: podIdentityPrincipal(clusterName, KARPENTER_NAMESPACE, KARPENTER_SERVICE_ACCOUNT),
    });
    const documents: Record<string, Statement[]> = {
      NodeLifecycle: nodeLifecycleStatements(ctx),
      IamIntegration: iamIntegrationStatements(ctx, this.nodeRole.roleArn),
      EksIntegration: eksIntegrationStatements(ctx),
      Interruption: interruptionStatements(this.interruptionQueue.queueArn),
      ZonalShift: zonalShiftStatements(ctx),
      ResourceDiscovery: resourceDiscoveryStatements(ctx),
    };
    for (const [name, statements] of Object.entries(documents)) {
      new iam.ManagedPolicy(this, `${name}Policy`, {
        description: `Karpenter controller ${name} (${clusterName})`,
        document: iam.PolicyDocument.fromJson({ Version: '2012-10-17', Statement: statements }),
        roles: [this.controllerRole],
      });
    }

    new eks.CfnPodIdentityAssociation(this, 'PodIdentity', {
      clusterName: cluster.ref,
      namespace: KARPENTER_NAMESPACE,
      serviceAccount: KARPENTER_SERVICE_ACCOUNT,
      roleArn: this.controllerRole.roleArn,
    });
    // Karpenter 拉起的节点用这个角色加入集群（Bottlerocket 也用 EC2_LINUX 类型）
    new eks.CfnAccessEntry(this, 'NodeAccessEntry', {
      clusterName: cluster.ref,
      principalArn: this.nodeRole.roleArn,
      type: 'EC2_LINUX',
    });
  }

  private createInterruptionQueue(): sqs.Queue {
    const queue = new sqs.Queue(this, 'InterruptionQueue', {
      retentionPeriod: cdk.Duration.minutes(5),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
    });
    const rules: Record<string, events.EventPattern> = {
      ScheduledChange: { source: ['aws.health'], detailType: ['AWS Health Event'] },
      SpotInterruption: { source: ['aws.ec2'], detailType: ['EC2 Spot Instance Interruption Warning'] },
      Rebalance: { source: ['aws.ec2'], detailType: ['EC2 Instance Rebalance Recommendation'] },
      InstanceStateChange: { source: ['aws.ec2'], detailType: ['EC2 Instance State-change Notification'] },
      CapacityReservationInterruption: {
        source: ['aws.ec2'],
        detailType: ['EC2 Capacity Reservation Instance Interruption Warning'],
      },
    };
    for (const [name, eventPattern] of Object.entries(rules)) {
      new events.Rule(this, `${name}Rule`, { eventPattern, targets: [new targets.SqsQueue(queue)] });
    }
    return queue;
  }
}

interface PolicyContext {
  readonly partition: string;
  readonly region: string;
  readonly account: string;
  readonly clusterName: string;
}

const clusterTag = (c: PolicyContext): string => `kubernetes.io/cluster/${c.clusterName}`;

function nodeLifecycleStatements(c: PolicyContext): Statement[] {
  const arn = (type: string, withAccount = true): string =>
    `arn:${c.partition}:ec2:${c.region}:${withAccount ? '*' : ''}:${type}/*`;
  const created = ['fleet', 'instance', 'volume', 'network-interface', 'launch-template', 'spot-instances-request'].map(
    (t) => arn(t),
  );
  const requestTags = {
    StringEquals: { [`aws:RequestTag/${clusterTag(c)}`]: 'owned', 'aws:RequestTag/eks:eks-cluster-name': c.clusterName },
    StringLike: { 'aws:RequestTag/karpenter.sh/nodepool': '*' },
  };
  const owned = {
    StringEquals: { [`aws:ResourceTag/${clusterTag(c)}`]: 'owned' },
    StringLike: { 'aws:ResourceTag/karpenter.sh/nodepool': '*' },
  };
  return [
    {
      Sid: 'AllowScopedEC2InstanceAccessActions',
      Effect: 'Allow',
      Resource: [
        arn('image', false),
        arn('snapshot', false),
        arn('security-group'),
        arn('subnet'),
        arn('capacity-reservation'),
        arn('placement-group'),
      ],
      Action: ['ec2:RunInstances', 'ec2:CreateFleet'],
    },
    {
      Sid: 'AllowScopedEC2LaunchTemplateAccessActions',
      Effect: 'Allow',
      Resource: arn('launch-template'),
      Action: ['ec2:RunInstances', 'ec2:CreateFleet'],
      Condition: owned,
    },
    {
      Sid: 'AllowScopedEC2InstanceActionsWithTags',
      Effect: 'Allow',
      Resource: created,
      Action: ['ec2:RunInstances', 'ec2:CreateFleet', 'ec2:CreateLaunchTemplate'],
      Condition: requestTags,
    },
    {
      Sid: 'AllowScopedResourceCreationTagging',
      Effect: 'Allow',
      Resource: created,
      Action: 'ec2:CreateTags',
      Condition: {
        StringEquals: {
          ...requestTags.StringEquals,
          'ec2:CreateAction': ['RunInstances', 'CreateFleet', 'CreateLaunchTemplate'],
        },
        StringLike: requestTags.StringLike,
      },
    },
    {
      Sid: 'AllowScopedResourceTagging',
      Effect: 'Allow',
      Resource: arn('instance'),
      Action: 'ec2:CreateTags',
      Condition: {
        ...owned,
        StringEqualsIfExists: { 'aws:RequestTag/eks:eks-cluster-name': c.clusterName },
        'ForAllValues:StringEquals': { 'aws:TagKeys': ['eks:eks-cluster-name', 'karpenter.sh/nodeclaim', 'Name'] },
      },
    },
    {
      Sid: 'AllowScopedDeletion',
      Effect: 'Allow',
      Resource: [arn('instance'), arn('launch-template')],
      Action: ['ec2:TerminateInstances', 'ec2:DeleteLaunchTemplate'],
      Condition: owned,
    },
  ];
}

function iamIntegrationStatements(c: PolicyContext, nodeRoleArn: string): Statement[] {
  const profiles = `arn:${c.partition}:iam::${c.account}:instance-profile/*`;
  const regionTag = 'topology.kubernetes.io/region';
  return [
    {
      Sid: 'AllowPassingInstanceRole',
      Effect: 'Allow',
      Resource: nodeRoleArn,
      Action: 'iam:PassRole',
      Condition: { StringEquals: { 'iam:PassedToService': ['ec2.amazonaws.com', 'ec2.amazonaws.com.cn'] } },
    },
    {
      Sid: 'AllowScopedInstanceProfileCreationActions',
      Effect: 'Allow',
      Resource: profiles,
      Action: ['iam:CreateInstanceProfile'],
      Condition: {
        StringEquals: {
          [`aws:RequestTag/${clusterTag(c)}`]: 'owned',
          'aws:RequestTag/eks:eks-cluster-name': c.clusterName,
          [`aws:RequestTag/${regionTag}`]: c.region,
        },
        StringLike: { 'aws:RequestTag/karpenter.k8s.aws/ec2nodeclass': '*' },
      },
    },
    {
      Sid: 'AllowScopedInstanceProfileTagActions',
      Effect: 'Allow',
      Resource: profiles,
      Action: ['iam:TagInstanceProfile'],
      Condition: {
        StringEquals: {
          [`aws:ResourceTag/${clusterTag(c)}`]: 'owned',
          [`aws:ResourceTag/${regionTag}`]: c.region,
          [`aws:RequestTag/${clusterTag(c)}`]: 'owned',
          'aws:RequestTag/eks:eks-cluster-name': c.clusterName,
          [`aws:RequestTag/${regionTag}`]: c.region,
        },
        StringLike: {
          'aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass': '*',
          'aws:RequestTag/karpenter.k8s.aws/ec2nodeclass': '*',
        },
      },
    },
    {
      Sid: 'AllowScopedInstanceProfileActions',
      Effect: 'Allow',
      Resource: profiles,
      Action: ['iam:AddRoleToInstanceProfile', 'iam:RemoveRoleFromInstanceProfile', 'iam:DeleteInstanceProfile'],
      Condition: {
        StringEquals: { [`aws:ResourceTag/${clusterTag(c)}`]: 'owned', [`aws:ResourceTag/${regionTag}`]: c.region },
        StringLike: { 'aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass': '*' },
      },
    },
  ];
}

function clusterArn(c: PolicyContext): string {
  return `arn:${c.partition}:eks:${c.region}:${c.account}:cluster/${c.clusterName}`;
}

function eksIntegrationStatements(c: PolicyContext): Statement[] {
  return [{ Sid: 'AllowAPIServerEndpointDiscovery', Effect: 'Allow', Resource: clusterArn(c), Action: 'eks:DescribeCluster' }];
}

function interruptionStatements(queueArn: string): Statement[] {
  return [
    {
      Sid: 'AllowInterruptionQueueActions',
      Effect: 'Allow',
      Resource: queueArn,
      Action: ['sqs:DeleteMessage', 'sqs:GetQueueUrl', 'sqs:ReceiveMessage'],
    },
  ];
}

function zonalShiftStatements(c: PolicyContext): Statement[] {
  return [
    {
      Sid: 'AllowZonalShiftStatusReadOnly',
      Effect: 'Allow',
      Resource: '*',
      Action: ['arc-zonal-shift:GetManagedResource'],
      Condition: { StringEquals: { 'arc-zonal-shift:ResourceIdentifier': clusterArn(c) } },
    },
  ];
}

function resourceDiscoveryStatements(c: PolicyContext): Statement[] {
  return [
    {
      Sid: 'AllowRegionalReadActions',
      Effect: 'Allow',
      Resource: '*',
      Action: [
        'ec2:DescribeCapacityReservations',
        'ec2:DescribeImages',
        'ec2:DescribeInstances',
        'ec2:DescribeInstanceStatus',
        'ec2:DescribeInstanceTypeOfferings',
        'ec2:DescribeInstanceTypes',
        'ec2:DescribeLaunchTemplates',
        'ec2:DescribePlacementGroups',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeSpotPriceHistory',
        'ec2:DescribeSubnets',
      ],
      Condition: { StringEquals: { 'aws:RequestedRegion': c.region } },
    },
    {
      Sid: 'AllowSSMReadActions',
      Effect: 'Allow',
      Resource: `arn:${c.partition}:ssm:${c.region}::parameter/aws/service/*`,
      Action: 'ssm:GetParameter',
    },
    { Sid: 'AllowPricingReadActions', Effect: 'Allow', Resource: '*', Action: 'pricing:GetProducts' },
    { Sid: 'AllowUnscopedInstanceProfileListAction', Effect: 'Allow', Resource: '*', Action: 'iam:ListInstanceProfiles' },
    {
      Sid: 'AllowInstanceProfileReadActions',
      Effect: 'Allow',
      Resource: `arn:${c.partition}:iam::${c.account}:instance-profile/*`,
      Action: 'iam:GetInstanceProfile',
    },
  ];
}
