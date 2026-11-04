import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as eks from 'aws-cdk-lib/aws-eks';
import { buildStacks } from '../lib/app';
import { DeploymentConfig } from '../lib/config';
import { LBC_POLICY_NAME, PolicyDocumentJson, Statement, loadUpstreamPolicy, policiesDir } from '../lib/lbc-policy';
import { LoadBalancerController, RATE_LIMIT_PER_5_MIN } from '../lib/load-balancer-controller';
import { Versions, loadVersions } from '../lib/versions';
import { fixture, withOverrides } from './helpers';

const VERSIONS = loadVersions();
const LOCK = VERSIONS.policies?.[LBC_POLICY_NAME];
const POLICY_FILE = path.join(policiesDir(), `${LBC_POLICY_NAME}-${LOCK?.version}.json`);

function synthEks(config: DeploymentConfig = fixture()): Template {
  return Template.fromStack(buildStacks(new cdk.App(), config, VERSIONS).eks);
}

function upstream(): PolicyDocumentJson {
  return JSON.parse(fs.readFileSync(POLICY_FILE, 'utf-8')) as PolicyDocumentJson;
}

/** 控制器角色的内联策略语句 */
function lbcStatements(template: Template): Statement[] {
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((p) => JSON.stringify((p as { Properties: { Roles: unknown } }).Properties.Roles).includes('LoadBalancerControllerRole'))
    .flatMap((p) => (p as { Properties: { PolicyDocument: { Statement: Statement[] } } }).Properties.PolicyDocument.Statement);
}

/** 用指定的策略目录在一个最小的栈里创建控制器资源（模拟 synth） */
function synthWith(dir: string, versions: Versions = VERSIONS): void {
  const stack = new cdk.Stack(new cdk.App(), 'Test', { env: { account: '123456789012', region: 'ap-southeast-5' } });
  const cluster = new eks.CfnCluster(stack, 'Cluster', {
    roleArn: 'arn:aws:iam::123456789012:role/eks',
    resourcesVpcConfig: { subnetIds: ['subnet-0aaa1111'] },
  });
  new LoadBalancerController(stack, 'Lbc', { clusterName: 'qwen-vl', cluster, vpcId: 'vpc-0abc1234', versions, policiesDir: dir });
}

interface WafRule {
  Name: string;
  Priority: number;
  Action?: unknown;
  OverrideAction?: unknown;
  Statement: Record<string, { Name?: string; RuleActionOverrides?: unknown; Limit?: number }>;
  VisibilityConfig: { MetricName: string; CloudWatchMetricsEnabled: boolean; SampledRequestsEnabled: boolean };
}

describe('Load Balancer Controller：ingress 关闭', () => {
  const template = synthEks(withOverrides(fixture(), { addons: { ingress: { enabled: false } } }));

  test('没有控制器角色、没有 WAF、没有 WebAclArn 输出', () => {
    expect(
      template.findResources('AWS::EKS::PodIdentityAssociation', {
        Properties: { ServiceAccount: 'aws-load-balancer-controller' },
      }),
    ).toEqual({});
    expect(lbcStatements(template)).toEqual([]);
    template.resourceCountIs('AWS::WAFv2::WebACL', 0);
    expect(Object.keys(template.findOutputs('WebAclArn'))).toEqual([]);
    template.hasOutput('VpcId', {});
  });
});

describe('Load Balancer Controller：ingress 开启', () => {
  const template = synthEks();
  const acl = Object.values(template.findResources('AWS::WAFv2::WebACL'))[0] as { Properties: { Rules: WafRule[] } };
  const rules = [...acl.Properties.Rules].sort((a, b) => a.Priority - b.Priority);
  const groupOf = (r: WafRule): string | undefined => r.Statement.ManagedRuleGroupStatement?.Name;

  test('Pod Identity 关联到 kube-system/aws-load-balancer-controller，信任策略限定本集群', () => {
    template.hasResourceProperties('AWS::EKS::PodIdentityAssociation', {
      ClusterName: { Ref: 'Cluster' },
      Namespace: 'kube-system',
      ServiceAccount: 'aws-load-balancer-controller',
    });
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'pods.eks.amazonaws.com' },
            Condition: {
              StringEquals: {
                'aws:RequestTag/eks-cluster-name': 'qwen-vl',
                'aws:RequestTag/kubernetes-namespace': 'kube-system',
                'aws:RequestTag/kubernetes-service-account': 'aws-load-balancer-controller',
              },
            },
          }),
        ],
      },
    });
    // 策略内容的收紧规则见 lbc-policy.test.ts；这里只确认附加在控制器角色上，而且不是空的
    expect(lbcStatements(template).length).toBeGreaterThan(10);
  });

  test('WAF：REGIONAL、默认放行、5 条规则', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'REGIONAL',
      DefaultAction: { Allow: {} },
      VisibilityConfig: { CloudWatchMetricsEnabled: true, SampledRequestsEnabled: true, MetricName: 'qwen-vl-web-acl' },
    });
    expect(rules.map((r) => groupOf(r) ?? r.Name)).toEqual([
      'AWSManagedRulesAmazonIpReputationList',
      'AWSManagedRulesSQLiRuleSet',
      'RateLimitPerIp',
      'AWSManagedRulesCommonRuleSet',
      'AWSManagedRulesKnownBadInputsRuleSet',
    ]);
    expect(new Set(rules.map((r) => r.Priority)).size).toBe(rules.length);
  });

  test('SQLi 规则组排在 IP 信誉之后、限速之前，规则组自己的动作不覆盖', () => {
    const priority = (name: string): number => rules.find((r) => (groupOf(r) ?? r.Name) === name)?.Priority ?? -1;
    const sqli = priority('AWSManagedRulesSQLiRuleSet');
    expect(sqli).toBeGreaterThan(priority('AWSManagedRulesAmazonIpReputationList'));
    expect(sqli).toBeLessThan(priority('RateLimitPerIp'));
    const rule = rules.find((r) => groupOf(r) === 'AWSManagedRulesSQLiRuleSet');
    expect(rule?.OverrideAction).toEqual({ None: {} });
    expect(rule?.Statement.ManagedRuleGroupStatement.RuleActionOverrides).toBeUndefined();
  });

  test('CommonRuleSet 只把 SizeRestrictions_BODY 改成计数，其余规则组不覆盖', () => {
    for (const rule of rules.filter((r) => groupOf(r) !== undefined)) {
      const overrides = rule.Statement.ManagedRuleGroupStatement.RuleActionOverrides;
      if (groupOf(rule) === 'AWSManagedRulesCommonRuleSet') {
        expect(overrides).toEqual([{ Name: 'SizeRestrictions_BODY', ActionToUse: { Count: {} } }]);
      } else {
        expect(overrides).toBeUndefined();
      }
    }
  });

  test('每条规则都开了指标和采样，指标名带集群名前缀', () => {
    for (const rule of rules) {
      expect(rule.VisibilityConfig).toEqual({
        CloudWatchMetricsEnabled: true,
        SampledRequestsEnabled: true,
        MetricName: expect.stringMatching(/^qwen-vl-/),
      });
    }
  });

  test('WAF 限速：按来源 IP，5 分钟 2000 次，超过就 Block', () => {
    const rate = rules.find((r) => r.Name === 'RateLimitPerIp');
    expect(rate?.Action).toEqual({ Block: {} });
    expect(rate?.Statement.RateBasedStatement).toEqual({
      Limit: RATE_LIMIT_PER_5_MIN,
      EvaluationWindowSec: 300,
      AggregateKeyType: 'IP',
    });
    expect(RATE_LIMIT_PER_5_MIN).toBe(2000);
  });

  test('输出 WebAclArn（客户在 Ingress 注解里引用）', () => {
    const aclId = Object.keys(template.findResources('AWS::WAFv2::WebACL'))[0];
    template.hasOutput('WebAclArn', { Value: { 'Fn::GetAtt': [aclId, 'Arn'] } });
  });
});

describe('上游策略的锁定和校验', () => {
  test('策略文件的 sha256 与 versions.json 一致', () => {
    const actual = crypto.createHash('sha256').update(fs.readFileSync(POLICY_FILE)).digest('hex');
    expect(LOCK?.sha256).toBe(actual);
  });

  test('文件被篡改时 synth 报错', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lbc-policy-'));
    const tampered = upstream().Statement.slice(1);
    fs.writeFileSync(path.join(dir, path.basename(POLICY_FILE)), JSON.stringify({ Version: '2012-10-17', Statement: tampered }));
    expect(() => synthWith(dir)).toThrow(/sha256 是 [0-9a-f]{64}，与 versions.json 锁定的/);
  });

  test('原文没被改时 synth 正常', () => {
    expect(() => synthWith(policiesDir())).not.toThrow();
  });

  test('文件不存在、versions.json 没有锁定信息时报错', () => {
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'lbc-policy-'));
    expect(() => synthWith(empty)).toThrow(/无法读取上游 IAM 策略/);
    const legacy = { ...VERSIONS, policies: undefined };
    expect(() => synthWith(policiesDir(), legacy)).toThrow(/缺少 policies\.aws-load-balancer-controller/);
  });

  test.each([
    ['没有 Statement', JSON.stringify({ Version: '2012-10-17', Statement: [] }), /没有 Statement/],
    ['旧版本的策略语法', JSON.stringify({ Version: '2008-10-17', Statement: [{}] }), /不是 2012-10-17 版本/],
    ['不是 JSON', '{not json', /不是合法的 JSON/],
  ])('哈希一致但内容不对时报错：%s', (_label, content, message) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lbc-policy-'));
    fs.writeFileSync(path.join(dir, `${LBC_POLICY_NAME}-v0.0.1.json`), content);
    const sha256 = crypto.createHash('sha256').update(content).digest('hex');
    const lock = { version: 'v0.0.1', source: 'https://example.com', sha256 };
    expect(() => loadUpstreamPolicy(LBC_POLICY_NAME, lock, dir)).toThrow(message);
  });

  test('编译后从 dist/lib 运行时也能找到 lib/policies；都找不到时报错', () => {
    const infra = path.resolve(__dirname, '..');
    expect(policiesDir(path.join(infra, 'dist', 'lib'))).toBe(path.join(infra, 'lib', 'policies'));
    expect(() => policiesDir(os.tmpdir())).toThrow(/找不到上游 IAM 策略目录/);
  });
});
