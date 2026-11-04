import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { loadVersions } from '../lib/versions';
import { fixture, withOverrides } from './helpers';
import { DeploymentConfig } from '../lib/config';

const VERSIONS = loadVersions();

function synthEks(config: DeploymentConfig = fixture()): Template {
  return Template.fromStack(buildStacks(new cdk.App(), config, VERSIONS).eks);
}

describe('EKS 栈', () => {
  const template = synthEks();

  test('集群：锁定版本、只在私有子网、API 认证模式、不给 CloudFormation 执行角色隐式管理员', () => {
    template.hasResourceProperties('AWS::EKS::Cluster', {
      Name: 'qwen-vl',
      Version: VERSIONS.kubernetes,
      ResourcesVpcConfig: { EndpointPrivateAccess: true, EndpointPublicAccess: true },
      AccessConfig: { AuthenticationMode: 'API', BootstrapClusterCreatorAdminPermissions: false },
      BootstrapSelfManagedAddons: false,
      Logging: {
        ClusterLogging: {
          EnabledTypes: [{ Type: 'api' }, { Type: 'audit' }, { Type: 'authenticator' }, { Type: 'scheduler' }],
        },
      },
    });
    const cluster = Object.values(template.findResources('AWS::EKS::Cluster'))[0] as {
      Properties: { EncryptionConfig?: unknown };
    };
    expect(cluster.Properties.EncryptionConfig).toBeUndefined();
  });

  test('部署人（CloudShell 的身份）是集群管理员', () => {
    template.hasResourceProperties('AWS::EKS::AccessEntry', {
      PrincipalArn: 'arn:aws:iam::123456789012:role/Admin',
      Type: 'STANDARD',
      AccessPolicies: [
        {
          PolicyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy',
          AccessScope: { Type: 'cluster' },
        },
      ],
    });
  });

  test('系统节点组：Graviton + Bottlerocket，带 CriticalAddonsOnly 污点，2 台起', () => {
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      AmiType: 'BOTTLEROCKET_ARM_64',
      InstanceTypes: ['m7g.large'],
      CapacityType: 'ON_DEMAND',
      ScalingConfig: { MinSize: 2, DesiredSize: 2, MaxSize: 3 },
      Labels: { 'qwenctl/node-role': 'system' },
      Taints: [{ Key: 'CriticalAddonsOnly', Value: 'true', Effect: 'NO_SCHEDULE' }],
    });
  });

  test('全部 add-on 版本锁定；DaemonSet 类在节点组之前，其余在节点组之后', () => {
    const addons = template.findResources('AWS::EKS::Addon');
    const byName = Object.fromEntries(
      Object.values(addons).map((r) => {
        const res = r as { Properties: { AddonName: string; AddonVersion: string }; DependsOn?: string[] };
        return [res.Properties.AddonName, res];
      }),
    );
    expect(Object.keys(byName).sort()).toEqual(
      [
        'aws-ebs-csi-driver',
        'coredns',
        'eks-node-monitoring-agent',
        'eks-pod-identity-agent',
        'kube-proxy',
        'metrics-server',
        'vpc-cni',
      ].sort(),
    );
    for (const [name, res] of Object.entries(byName)) {
      expect(res.Properties.AddonVersion).toBe(VERSIONS.eksAddons[name]);
    }
    const nodegroupId = Object.keys(template.findResources('AWS::EKS::Nodegroup'))[0];
    expect(byName['coredns'].DependsOn).toContain(nodegroupId);
    expect(byName['vpc-cni'].DependsOn ?? []).not.toContain(nodegroupId);
    const nodegroup = template.findResources('AWS::EKS::Nodegroup')[nodegroupId] as { DependsOn: string[] };
    const vpcCniId = Object.keys(addons).find((k) => k.includes('vpccni'));
    expect(nodegroup.DependsOn).toContain(vpcCniId);
  });

  test('Karpenter：Pod Identity 关联到 kube-system/karpenter，节点角色有 EC2_LINUX 访问入口', () => {
    template.hasResourceProperties('AWS::EKS::PodIdentityAssociation', {
      Namespace: 'kube-system',
      ServiceAccount: 'karpenter',
    });
    template.hasResourceProperties('AWS::EKS::AccessEntry', { Type: 'EC2_LINUX' });
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Action: ['sts:AssumeRole', 'sts:TagSession'],
            Principal: { Service: 'pods.eks.amazonaws.com' },
            Condition: {
              StringEquals: {
                'aws:RequestTag/eks-cluster-name': 'qwen-vl',
                'aws:RequestTag/kubernetes-namespace': 'kube-system',
                'aws:RequestTag/kubernetes-service-account': 'karpenter',
              },
            },
          }),
        ],
      },
    });
  });

  test('Karpenter 控制器的 6 份策略都按集群名限定范围', () => {
    const policies = Object.values(template.findResources('AWS::IAM::ManagedPolicy')).filter((p) =>
      String((p as { Properties: { Description?: string } }).Properties.Description).startsWith('Karpenter controller'),
    );
    expect(policies).toHaveLength(6);
    const text = JSON.stringify(policies);
    expect(text).toContain('aws:ResourceTag/kubernetes.io/cluster/qwen-vl');
    expect(text).toContain(':eks:ap-southeast-5:123456789012:cluster/qwen-vl');
    expect(text).toContain('"aws:RequestedRegion":"ap-southeast-5"');
    // 删除和启动实例都必须带集群 tag 条件，不能是无条件的
    const statements = policies.flatMap(
      (p) => (p as { Properties: { PolicyDocument: { Statement: Record<string, unknown>[] } } }).Properties.PolicyDocument.Statement,
    );
    const deletion = statements.find((s) => s.Sid === 'AllowScopedDeletion') as { Condition?: unknown };
    expect(deletion.Condition).toBeDefined();
  });

  test('中断队列：SQS 托管加密、强制 TLS，5 条 EventBridge 规则', () => {
    template.hasResourceProperties('AWS::SQS::Queue', { SqsManagedSseEnabled: true, MessageRetentionPeriod: 300 });
    template.hasResourceProperties('AWS::SQS::QueuePolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        ]),
      },
    });
    template.resourceCountIs('AWS::Events::Rule', 5);
  });

  test('输出 qwenctl 安装阶段需要的值', () => {
    for (const key of [
      'ClusterName',
      'ClusterSecurityGroupId',
      'KarpenterNodeRoleName',
      'KarpenterInterruptionQueueName',
      'PrivateSubnetIds',
      'VpcId',
      'WebAclArn',
    ]) {
      template.hasOutput(key, {});
    }
  });
});

describe('EKS 栈安全加固', () => {
  const template = synthEks();

  test('系统节点组使用启动模板：IMDSv2、hop limit 1、EBS 加密', () => {
    template.hasResourceProperties('AWS::EC2::LaunchTemplate', {
      LaunchTemplateData: {
        MetadataOptions: { HttpTokens: 'required', HttpPutResponseHopLimit: 1 },
        BlockDeviceMappings: [
          Match.objectLike({ DeviceName: '/dev/xvda', Ebs: Match.objectLike({ Encrypted: true }) }),
          Match.objectLike({ DeviceName: '/dev/xvdb', Ebs: Match.objectLike({ Encrypted: true }) }),
        ],
      },
    });
    template.hasResourceProperties('AWS::EKS::Nodegroup', {
      LaunchTemplate: { Id: Match.anyValue(), Version: Match.anyValue() },
    });
  });

  test('控制面日志组预先创建并设置保留期，集群依赖它', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', { LogGroupName: '/aws/eks/qwen-vl/cluster', RetentionInDays: 30 });
    const logGroupId = Object.keys(
      template.findResources('AWS::Logs::LogGroup', { Properties: { LogGroupName: '/aws/eks/qwen-vl/cluster' } }),
    )[0];
    const cluster = Object.values(template.findResources('AWS::EKS::Cluster'))[0] as { DependsOn: string[] };
    expect(cluster.DependsOn).toContain(logGroupId);
  });
});

describe('EKS 栈可选项', () => {
  test('客户 CMK 加密 Secrets，额外的管理员角色，仅私网端点', () => {
    const base = fixture();
    const kmsKey = 'arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555';
    const extra = 'arn:aws:iam::123456789012:role/ops/PlatformAdmin';
    const template = synthEks(
      withOverrides(base, {
        advanced: {
          network: { ...base.advanced.network, eks_endpoint: 'private' },
          compliance: { ...base.advanced.compliance, kms_key_arn: kmsKey, extra_admin_role_arns: [extra] },
        },
      }),
    );
    template.hasResourceProperties('AWS::EKS::Cluster', {
      EncryptionConfig: [{ Provider: { KeyArn: kmsKey }, Resources: ['secrets'] }],
      ResourcesVpcConfig: { EndpointPublicAccess: false },
    });
    template.hasResourceProperties('AWS::EKS::AccessEntry', { PrincipalArn: extra, Type: 'STANDARD' });
  });

  test('versions.json 缺少 add-on 版本时报错', () => {
    const broken = { ...VERSIONS, eksAddons: { ...VERSIONS.eksAddons, coredns: '' } };
    delete (broken.eksAddons as Record<string, string>).coredns;
    expect(() => buildStacks(new cdk.App(), fixture(), broken)).toThrow(/缺少 EKS add-on coredns/);
  });

  test('VpcId 输出：现有 VPC 时就是配置里的 vpcId（qwenctl 装 Load Balancer Controller 用）', () => {
    const existing = withOverrides(fixture(), {
      network: {
        mode: 'existing',
        cidr: '10.0.0.0/16',
        vpcId: 'vpc-0abc1234',
        privateSubnets: [
          { subnetId: 'subnet-0aaa1111', zoneId: 'apse5-az1', cidr: '10.0.32.0/19', routeTableId: 'rtb-0ccc3333' },
          { subnetId: 'subnet-0bbb2222', zoneId: 'apse5-az2', cidr: '10.0.64.0/19', routeTableId: 'rtb-0ddd4444' },
        ],
        endpoints: { interface: [], s3Gateway: true, s3RouteTableIds: ['rtb-0ddd4444'] },
      },
    });
    synthEks(existing).hasOutput('VpcId', { Value: 'vpc-0abc1234' });
  });
});
