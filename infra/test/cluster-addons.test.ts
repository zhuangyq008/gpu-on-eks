import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { EBS_CSI_MANAGED_POLICY } from '../lib/cluster-addons';
import { DeploymentConfig } from '../lib/config';
import { loadVersions } from '../lib/versions';
import { fixture, withOverrides } from './helpers';

const VERSIONS = loadVersions();
const KMS_KEY = 'arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555';
const ZONES = [
  { id: 'Z0123456789ABCDEFGHIJ', name: 'example.com' },
  { id: 'Z9876543210ZYXWVUTSRQ', name: 'shop.example.net' },
];

function synthEks(config: DeploymentConfig = fixture()): Template {
  return Template.fromStack(buildStacks(new cdk.App(), config, VERSIONS).eks);
}

interface AddonResource {
  Properties: {
    AddonName: string;
    AddonVersion: string;
    ConfigurationValues?: string;
    PodIdentityAssociations?: { ServiceAccount: string; RoleArn: { 'Fn::GetAtt': [string, string] } }[];
  };
  DependsOn?: string[];
}

function addon(template: Template, name: string): AddonResource | undefined {
  return Object.values(template.findResources('AWS::EKS::Addon')).find(
    (r) => (r as AddonResource).Properties.AddonName === name,
  ) as AddonResource | undefined;
}

function values(res: AddonResource | undefined): Record<string, unknown> {
  return JSON.parse(res?.Properties.ConfigurationValues ?? '{}') as Record<string, unknown>;
}

type PolicyStatement = { Sid?: string; Action: string | string[]; Resource: unknown; Condition?: unknown };

/** 某个角色的内联策略语句（按角色逻辑 ID 前缀找） */
function inlineStatements(template: Template, rolePrefix: string): PolicyStatement[] {
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((p) => JSON.stringify((p as { Properties: { Roles: unknown } }).Properties.Roles).includes(rolePrefix))
    .flatMap((p) => (p as { Properties: { PolicyDocument: { Statement: PolicyStatement[] } } }).Properties.PolicyDocument.Statement);
}

function nodegroupId(template: Template): string {
  return Object.keys(template.findResources('AWS::EKS::Nodegroup'))[0];
}

const SYSTEM_PLACEMENT = {
  nodeSelector: { 'qwenctl/node-role': 'system' },
  tolerations: [{ key: 'CriticalAddonsOnly', operator: 'Exists' }],
};

describe('必装插件：metrics-server 和 EBS CSI', () => {
  const template = synthEks();

  test.each(['metrics-server', 'aws-ebs-csi-driver'])('%s：版本取自 versions.json，依赖系统节点组', (name) => {
    const res = addon(template, name);
    expect(res).toBeDefined();
    expect(res?.Properties.AddonVersion).toBe(VERSIONS.eksAddons[name]);
    expect(res?.DependsOn).toContain(nodegroupId(template));
  });

  test('metrics-server：2 副本跑在系统节点上，带 PDB', () => {
    expect(values(addon(template, 'metrics-server'))).toEqual({
      replicas: 2,
      ...SYSTEM_PLACEMENT,
      podDisruptionBudget: { enabled: true, maxUnavailable: 1 },
    });
  });

  test('EBS CSI：controller 在系统节点上，node DaemonSet 保持默认，不建 add-on 自带的默认 StorageClass', () => {
    const config = values(addon(template, 'aws-ebs-csi-driver'));
    expect(config).toEqual({ controller: SYSTEM_PLACEMENT, defaultStorageClass: { enabled: false } });
    expect(config).not.toHaveProperty('node');
  });

  test('EBS CSI 角色：托管策略 V2，Pod Identity 关联到 kube-system/ebs-csi-controller-sa', () => {
    const roles = template.findResources('AWS::IAM::Role', {
      Properties: {
        ManagedPolicyArns: [{ 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:iam::aws:policy/${EBS_CSI_MANAGED_POLICY}`]] }],
      },
    });
    const roleIds = Object.keys(roles);
    expect(roleIds).toHaveLength(1);
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
                'aws:RequestTag/kubernetes-service-account': 'ebs-csi-controller-sa',
              },
            },
          }),
        ],
      },
      ManagedPolicyArns: Match.anyValue(),
    });
    const res = addon(template, 'aws-ebs-csi-driver');
    expect(res?.Properties.PodIdentityAssociations).toEqual([
      { ServiceAccount: 'ebs-csi-controller-sa', RoleArn: { 'Fn::GetAtt': [roleIds[0], 'Arn'] } },
    ]);
    expect(res?.DependsOn).toContain(roleIds[0]);
  });

  test('没有客户 CMK 时，EBS CSI 角色没有 KMS 权限', () => {
    const statements = inlineStatements(template, 'ClusterAddonsEbsCsiRole');
    expect(JSON.stringify(statements)).not.toContain('kms:');
  });

  test('有客户 CMK 时，EBS CSI 角色只对这把 key 有 KMS 权限，授权限定给 AWS 资源', () => {
    const base = fixture();
    const template = synthEks(
      withOverrides(base, { advanced: { compliance: { ...base.advanced.compliance, kms_key_arn: KMS_KEY } } }),
    );
    const statements = inlineStatements(template, 'ClusterAddonsEbsCsiRole');
    expect(statements).toEqual([
      {
        Sid: 'CmkGrantsForEbs',
        Effect: 'Allow',
        Action: ['kms:CreateGrant', 'kms:ListGrants', 'kms:RevokeGrant'],
        Resource: KMS_KEY,
        Condition: { Bool: { 'kms:GrantIsForAWSResource': 'true' } },
      },
      {
        Sid: 'CmkCryptoForEbs',
        Effect: 'Allow',
        Action: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        Resource: KMS_KEY,
      },
    ]);
    // add-on 等 KMS 策略附加好再装
    const policyId = Object.keys(template.findResources('AWS::IAM::Policy')).find((k) =>
      k.startsWith('ClusterAddonsEbsCsiRoleDefaultPolicy'),
    );
    expect(addon(template, 'aws-ebs-csi-driver')?.DependsOn).toContain(policyId);
  });

  test('versions.json 缺少 metrics-server 版本时报错', () => {
    const eksAddons = Object.fromEntries(Object.entries(VERSIONS.eksAddons).filter(([k]) => k !== 'metrics-server'));
    expect(() => buildStacks(new cdk.App(), fixture(), { ...VERSIONS, eksAddons })).toThrow(
      /缺少 EKS add-on metrics-server/,
    );
  });
});

describe('ExternalDNS（可选）', () => {
  test('默认关闭：没有 add-on，也没有 Route 53 权限', () => {
    const template = synthEks();
    expect(addon(template, 'external-dns')).toBeUndefined();
    expect(JSON.stringify(template.toJSON())).not.toContain('route53:');
  });

  describe('开启时', () => {
    const template = synthEks(
      withOverrides(fixture(), { addons: { ingress: { enabled: true }, externalDns: { enabled: true, hostedZones: ZONES } } }),
    );
    const zoneArns = ZONES.map((z) => ({
      'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:route53:::hostedzone/${z.id}`]],
    }));

    test('add-on 版本锁定，依赖系统节点组，Pod Identity 关联到 external-dns/external-dns', () => {
      const res = addon(template, 'external-dns');
      expect(res?.Properties.AddonVersion).toBe(VERSIONS.eksAddons['external-dns']);
      expect(res?.DependsOn).toContain(nodegroupId(template));
      expect(res?.Properties.PodIdentityAssociations?.map((a) => a.ServiceAccount)).toEqual(['external-dns']);
      template.hasResourceProperties('AWS::IAM::Role', {
        AssumeRolePolicyDocument: {
          Statement: [
            Match.objectLike({
              Condition: {
                StringEquals: {
                  'aws:RequestTag/eks-cluster-name': 'qwen-vl',
                  'aws:RequestTag/kubernetes-namespace': 'external-dns',
                  'aws:RequestTag/kubernetes-service-account': 'external-dns',
                },
              },
            }),
          ],
        },
      });
    });

    test('配置：只管列出的区域，只增不删，所有者标记是「集群名-region」，跑在系统节点上', () => {
      expect(values(addon(template, 'external-dns'))).toEqual({
        domainFilters: ['example.com', 'shop.example.net'],
        extraArgs: ['--zone-id-filter=Z0123456789ABCDEFGHIJ', '--zone-id-filter=Z9876543210ZYXWVUTSRQ'],
        managedRecordTypes: ['A', 'AAAA', 'CNAME'],
        policy: 'upsert-only',
        txtOwnerId: 'qwen-vl-ap-southeast-5',
        sources: ['ingress', 'service'],
        ...SYSTEM_PLACEMENT,
      });
    });

    test('改记录：只覆盖列出的区域，只能改 A / AAAA / CNAME / TXT，只能 CREATE / UPSERT；只有 ListHostedZones 对 *', () => {
      const statements = inlineStatements(template, 'ClusterAddonsExternalDnsRole');
      expect(statements).toEqual([
        {
          Sid: 'ChangeRecordsInListedZones',
          Effect: 'Allow',
          Action: 'route53:ChangeResourceRecordSets',
          Resource: zoneArns,
          Condition: {
            'ForAllValues:StringEquals': {
              'route53:ChangeResourceRecordSetsRecordTypes': ['A', 'AAAA', 'CNAME', 'TXT'],
              'route53:ChangeResourceRecordSetsActions': ['CREATE', 'UPSERT'],
            },
            Null: {
              'route53:ChangeResourceRecordSetsRecordTypes': 'false',
              'route53:ChangeResourceRecordSetsActions': 'false',
            },
          },
        },
        {
          Sid: 'ReadRecordsInListedZones',
          Effect: 'Allow',
          Action: ['route53:ListResourceRecordSets', 'route53:ListTagsForResources'],
          Resource: zoneArns,
        },
        { Sid: 'ListHostedZones', Effect: 'Allow', Action: 'route53:ListHostedZones', Resource: '*' },
      ]);
    });

    test('IAM 允许的记录类型 = ExternalDNS 管理的类型 + TXT registry 记录（两边不一致会在运行时 AccessDenied）', () => {
      const managed = values(addon(template, 'external-dns')).managedRecordTypes as string[];
      const change = inlineStatements(template, 'ClusterAddonsExternalDnsRole').find(
        (s) => s.Sid === 'ChangeRecordsInListedZones',
      ) as { Condition: Record<string, Record<string, string[]>> };
      const allowed = change.Condition['ForAllValues:StringEquals']['route53:ChangeResourceRecordSetsRecordTypes'];
      expect([...allowed].sort()).toEqual([...managed, 'TXT'].sort());
      // NS / MX / SOA 等记录（域名委派、邮件）不能改
      for (const type of ['NS', 'MX', 'SOA', 'SRV', 'CAA']) {
        expect(allowed).not.toContain(type);
      }
    });

    test('IAM 层强制只增不删：和 add-on 的 policy: upsert-only 一致，不允许 DELETE', () => {
      const change = inlineStatements(template, 'ClusterAddonsExternalDnsRole').find(
        (s) => s.Sid === 'ChangeRecordsInListedZones',
      ) as { Condition: Record<string, Record<string, string[] | string>> };
      expect(values(addon(template, 'external-dns')).policy).toBe('upsert-only');
      const actions = change.Condition['ForAllValues:StringEquals']['route53:ChangeResourceRecordSetsActions'];
      expect(actions).toEqual(['CREATE', 'UPSERT']);
      expect(actions).not.toContain('DELETE');
      expect(change.Condition.Null['route53:ChangeResourceRecordSetsActions']).toBe('false');
    });

    test('不同 region 的同名集群：TXT 所有者标记不同', () => {
      const other = synthEks(
        withOverrides(fixture(), {
          region: 'ap-southeast-1',
          addons: { ingress: { enabled: true }, externalDns: { enabled: true, hostedZones: ZONES } },
        }),
      );
      expect(values(addon(other, 'external-dns')).txtOwnerId).toBe('qwen-vl-ap-southeast-1');
      expect(values(addon(template, 'external-dns')).txtOwnerId).toBe('qwen-vl-ap-southeast-5');
    });

    test('同名的公有 / 私有区域：domainFilters 去重，zone-id-filter 各一个', () => {
      const twins = [
        { id: 'Z0123456789ABCDEFGHIJ', name: 'example.com' },
        { id: 'Z00000000000000000001', name: 'example.com' },
      ];
      const t = synthEks(
        withOverrides(fixture(), { addons: { ingress: { enabled: true }, externalDns: { enabled: true, hostedZones: twins } } }),
      );
      const config = values(addon(t, 'external-dns'));
      expect(config.domainFilters).toEqual(['example.com']);
      expect(config.extraArgs).toEqual(twins.map((z) => `--zone-id-filter=${z.id}`));
    });
  });
});
