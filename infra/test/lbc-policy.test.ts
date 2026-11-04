import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { LBC_POLICY_NAME, PolicyDocumentJson, Statement, policiesDir, tightenPolicy } from '../lib/lbc-policy';
import { INLINE_POLICY_LIMIT } from '../lib/load-balancer-controller';
import { loadVersions } from '../lib/versions';
import { fixture } from './helpers';

const VERSIONS = loadVersions();
const LOCK = VERSIONS.policies?.[LBC_POLICY_NAME];
const POLICY_FILE = path.join(policiesDir(), `${LBC_POLICY_NAME}-${LOCK?.version}.json`);
const SCOPE = {
  partition: 'aws',
  region: 'ap-southeast-5',
  account: '123456789012',
  vpcId: 'vpc-0abc1234',
  clusterName: 'qwen-vl',
};
const RESOURCE_TAG = 'aws:ResourceTag/elbv2.k8s.aws/cluster';
const REQUEST_TAG = 'aws:RequestTag/elbv2.k8s.aws/cluster';
/** (f) 明确删掉的 Action：WAF Classic 和 Shield（qwenctl 的 Helm values 关掉了 enableWaf / enableShield） */
const REMOVED_ACTIONS = [
  'waf-regional:GetWebACL',
  'waf-regional:GetWebACLForResource',
  'waf-regional:AssociateWebACL',
  'waf-regional:DisassociateWebACL',
  'shield:GetSubscriptionState',
  'shield:DescribeProtection',
  'shield:CreateProtection',
  'shield:DeleteProtection',
];

function upstream(): PolicyDocumentJson {
  return JSON.parse(fs.readFileSync(POLICY_FILE, 'utf-8')) as PolicyDocumentJson;
}

const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : [v]);
const actionsOf = (s: Statement): string[] => asArray(s.Action) as string[];
const isIamOnly = (s: Statement): boolean => actionsOf(s).every((a) => a.startsWith('iam:'));
const hasAction = (action: string) => (s: Statement): boolean => actionsOf(s).includes(action);

describe('LBC 策略收紧（纯函数，按字面值的 scope）', () => {
  const tightened = tightenPolicy(upstream(), SCOPE).Statement;

  test('(a) 不再有 :*:*: 形式的 ARN，ARN 用本 partition / region / 账号', () => {
    expect(JSON.stringify(upstream())).toContain(':*:*:');
    const text = JSON.stringify(tightened);
    expect(text).not.toContain(':*:*:');
    expect(text).toContain('arn:aws:elasticloadbalancing:ap-southeast-5:123456789012:targetgroup/*/*');
  });

  test('(b) Resource "*" 的语句都带 RequestedRegion，只有 IAM 的语句例外', () => {
    const wildcard = tightened.filter((s) => s.Resource === '*');
    const exempt = wildcard.filter(isIamOnly);
    expect(exempt.flatMap(actionsOf).sort()).toEqual(
      ['iam:CreateServiceLinkedRole', 'iam:GetServerCertificate', 'iam:ListServerCertificates'].sort(),
    );
    exempt.forEach((s) => expect(JSON.stringify(s.Condition ?? {})).not.toContain('aws:RequestedRegion'));
    for (const s of wildcard.filter((s) => !isIamOnly(s))) {
      expect(s.Condition?.StringEquals?.['aws:RequestedRegion']).toBe('ap-southeast-5');
    }
    // 上游把 acm / wafv2 和 iam:GetServerCertificate 写在同一条：拆开后两半合起来 = 原来那条去掉 (f) 删的 Action
    const mixed = upstream().Statement.find(hasAction('iam:GetServerCertificate')) as Statement;
    const halves = wildcard.filter((s) => actionsOf(s).some((a) => actionsOf(mixed).includes(a)));
    expect(halves).toHaveLength(2);
    expect(halves.flatMap(actionsOf).sort()).toEqual(actionsOf(mixed).filter((a) => !REMOVED_ACTIONS.includes(a)).sort());
    // IAM 创建服务关联角色那条保持上游原样
    expect(tightened).toContainEqual(upstream().Statement[0]);
  });

  test('(c) 修改安全组入站规则：只能改本集群 VPC 里的安全组', () => {
    expect(tightened).toContainEqual({
      Effect: 'Allow',
      Action: ['ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress'],
      Resource: 'arn:aws:ec2:ap-southeast-5:123456789012:security-group/*',
      Condition: { ArnEquals: { 'ec2:Vpc': 'arn:aws:ec2:ap-southeast-5:123456789012:vpc/vpc-0abc1234' } },
    });
    const sgIngress = tightened.filter(hasAction('ec2:AuthorizeSecurityGroupIngress'));
    expect(sgIngress).toHaveLength(2);
    sgIngress.forEach((s) => expect(s.Condition).toBeDefined());
  });

  test('(d) elbv2.k8s.aws/cluster 的 Null=false 全部改成 StringEquals = 集群名，Null=true 保持原样', () => {
    // 收紧后不能再有「只检查 tag 存在」的条件
    for (const s of tightened) {
      expect(s.Condition?.Null?.[RESOURCE_TAG]).not.toBe('false');
      expect(s.Condition?.Null?.[REQUEST_TAG]).not.toBe('false');
    }
    // 逐条对照上游：每个 Null=false 的 tag 键都变成了 StringEquals = qwen-vl；Null=true 原样保留
    const upstreamTagged = upstream().Statement.filter((s) => s.Condition?.Null);
    expect(upstreamTagged.length).toBeGreaterThan(0);
    for (const before of upstreamTagged) {
      const after = tightened.filter((s) => actionsOf(s).some((a) => actionsOf(before).includes(a)) && s.Condition?.StringEquals);
      const nulls = before.Condition?.Null as Record<string, string>;
      for (const [key, value] of Object.entries(nulls)) {
        const match = after.find((s) =>
          value === 'false' ? s.Condition?.StringEquals?.[key] === 'qwen-vl' : s.Condition?.Null?.[key] === 'true',
        );
        expect({ actions: actionsOf(before), key, found: match !== undefined }).toEqual({
          actions: actionsOf(before),
          key,
          found: true,
        });
      }
    }
    const boundResource = tightened.filter((s) => s.Condition?.StringEquals?.[RESOURCE_TAG] === 'qwen-vl');
    const boundRequest = tightened.filter((s) => s.Condition?.StringEquals?.[REQUEST_TAG] === 'qwen-vl');
    expect(boundResource.length).toBeGreaterThanOrEqual(4);
    expect(boundRequest.length).toBeGreaterThanOrEqual(3);
  });

  test('(d) 删除安全组、删除负载均衡器都要求 tag 值是本集群名', () => {
    for (const action of ['ec2:DeleteSecurityGroup', 'elasticloadbalancing:DeleteLoadBalancer', 'elasticloadbalancing:DeleteTargetGroup']) {
      const s = tightened.find(hasAction(action));
      expect(s?.Condition?.StringEquals?.[RESOURCE_TAG]).toBe('qwen-vl');
    }
    const create = tightened.find(hasAction('elasticloadbalancing:CreateLoadBalancer'));
    expect(create?.Condition).toEqual({
      StringEquals: { [REQUEST_TAG]: 'qwen-vl', 'aws:RequestedRegion': 'ap-southeast-5' },
    });
  });

  test('(e) 创建安全组：Resource 是本 VPC + security-group/*，没有别的条件', () => {
    const create = tightened.filter(hasAction('ec2:CreateSecurityGroup'));
    expect(create).toEqual([
      {
        Effect: 'Allow',
        Action: ['ec2:CreateSecurityGroup'],
        Resource: [
          'arn:aws:ec2:ap-southeast-5:123456789012:security-group/*',
          'arn:aws:ec2:ap-southeast-5:123456789012:vpc/vpc-0abc1234',
        ],
      },
    ]);
  });

  test('(f) 只删掉 WAF Classic 和 Shield 的 Action，其余上游 Action 一个不少', () => {
    const before = new Set(upstream().Statement.flatMap(actionsOf));
    const after = new Set(tightened.flatMap(actionsOf));
    const removed = [...before].filter((a) => !after.has(a));
    expect(removed.sort()).toEqual([...REMOVED_ACTIONS].sort());
    expect([...after].filter((a) => !before.has(a))).toEqual([]);
    expect(JSON.stringify(tightened)).not.toMatch(/waf-regional:|shield:/);
    // WAFv2 保留（客户用 wafv2-acl-arn 注解挂 WebACL）
    expect(after.has('wafv2:AssociateWebACL')).toBe(true);
  });

  test('上游 Resource "*" 语句涉及的服务是已审查过的集合（新增服务时要判断是不是全局服务、要不要删）', () => {
    const services = new Set(
      upstream()
        .Statement.filter((s) => s.Resource === '*')
        .flatMap(actionsOf)
        .map((a) => a.split(':')[0]),
    );
    // iam 是全局服务（不加 RequestedRegion）；waf-regional、shield 被 (f) 删掉；其余都是 region 级服务
    expect([...services].sort()).toEqual(
      ['acm', 'cognito-idp', 'ec2', 'elasticloadbalancing', 'iam', 'shield', 'waf-regional', 'wafv2'].sort(),
    );
  });

  test('收紧后的策略在角色内联策略的大小限制内（按最长的 partition 估算）', () => {
    const doc = tightenPolicy(upstream(), {
      ...SCOPE,
      partition: 'aws-us-gov',
      vpcId: 'vpc-0123456789abcdef0',
      clusterName: 'a'.repeat(20),
    });
    expect(JSON.stringify(doc).length).toBeLessThan(INLINE_POLICY_LIMIT);
  });

  test('不修改传入的文档', () => {
    const base = upstream();
    const snapshot = JSON.stringify(base);
    tightenPolicy(base, SCOPE);
    expect(JSON.stringify(base)).toBe(snapshot);
  });
});

describe('LBC 策略收紧（合成后的模板：partition、vpcId 是 CDK token）', () => {
  const template = Template.fromStack(buildStacks(new cdk.App(), fixture(), VERSIONS).eks);
  const join = (...parts: unknown[]) => ({ 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ...parts]] });
  const vpcImport = { 'Fn::ImportValue': Match.stringLikeRegexp('^qwen-vl-network:ExportsOutputRefVpc') };

  test('(c) 和 (e) 用的是本栈的 partition 和跨栈引用的 VPC', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          {
            Effect: 'Allow',
            Action: ['ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress'],
            Resource: join(':ec2:ap-southeast-5:123456789012:security-group/*'),
            Condition: { ArnEquals: { 'ec2:Vpc': join(':ec2:ap-southeast-5:123456789012:vpc/', vpcImport) } },
          },
          {
            Effect: 'Allow',
            Action: 'ec2:CreateSecurityGroup',
            Resource: [
              join(':ec2:ap-southeast-5:123456789012:security-group/*'),
              join(':ec2:ap-southeast-5:123456789012:vpc/', vpcImport),
            ],
          },
        ]),
      },
    });
  });

  test('(d) tag 条件的值是配置里的集群名', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['ec2:DeleteSecurityGroup']),
            Condition: { StringEquals: { [RESOURCE_TAG]: 'qwen-vl', 'aws:RequestedRegion': 'ap-southeast-5' } },
          }),
        ]),
      },
    });
  });
});

describe('收紧规则遇到上游结构变化时报错（升级策略版本时强制重新审查）', () => {
  const base = upstream();
  const replace = (pred: (s: Statement) => boolean, next: (s: Statement) => Statement[]): PolicyDocumentJson => ({
    ...base,
    Statement: base.Statement.flatMap((s) => (pred(s) ? next(s) : [s])),
  });
  const unconditioned = (action: string) => (s: Statement): boolean => s.Condition === undefined && hasAction(action)(s);

  test('找不到无条件的安全组入站规则语句', () => {
    const doc = replace(unconditioned('ec2:AuthorizeSecurityGroupIngress'), () => []);
    expect(() => tightenPolicy(doc, SCOPE)).toThrow(/无条件的安全组入站规则语句有 0 条/);
  });

  test('找不到无条件的创建安全组语句', () => {
    const doc = replace(unconditioned('ec2:CreateSecurityGroup'), () => []);
    expect(() => tightenPolicy(doc, SCOPE)).toThrow(/无条件的创建安全组语句有 0 条/);
  });

  test('cluster tag 条件换了写法（例如改成 StringLike）', () => {
    const doc = replace(
      (s) => s.Condition?.Null !== undefined,
      (s) => [{ ...s, Condition: { StringLike: { [RESOURCE_TAG]: '*' } } }],
    );
    expect(() => tightenPolicy(doc, SCOPE)).toThrow(/elbv2\.k8s\.aws\/cluster tag 条件写法变了/);
  });

  test('上游语句已经有同名条件时不覆盖', () => {
    const extra: Statement = {
      Effect: 'Allow',
      Action: 'ec2:DescribeVpcs',
      Resource: '*',
      Condition: { StringEquals: { 'aws:RequestedRegion': 'us-east-1' } },
    };
    expect(() => tightenPolicy({ ...base, Statement: [...base.Statement, extra] }, SCOPE)).toThrow(
      /已有条件 StringEquals aws:RequestedRegion/,
    );
  });

  test('出现没处理的通配 ARN', () => {
    const extra: Statement = { Effect: 'Allow', Action: 'ec2:DeleteTags', Resource: ['arn:aws:ec2:*:123456789012:volume/*'] };
    expect(() => tightenPolicy({ ...base, Statement: [...base.Statement, extra] }, SCOPE)).toThrow(/仍有通配/);
  });

  test('整条都是被删服务的语句会被去掉', () => {
    const extra: Statement = { Effect: 'Allow', Action: ['shield:ListProtections'], Resource: '*' };
    const doc = tightenPolicy({ ...base, Statement: [...base.Statement, extra] }, SCOPE);
    expect(JSON.stringify(doc)).not.toContain('shield:');
  });
});
