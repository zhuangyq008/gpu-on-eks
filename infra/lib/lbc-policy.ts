/**
 * AWS Load Balancer Controller 的 IAM 策略：读取上游原文（infra/lib/policies/），按 versions.json 的 sha256 校验，
 * 再按本集群收紧。除了 (f) 明确列出的服务，不删 Action：控制器缺权限要到运行时创建负载均衡器才会报错。
 *
 * 收紧规则（依据见各函数注释）：
 *  (a) 资源 ARN 里 region / account 的通配换成本栈的 partition / region / account
 *  (b) Resource "*" 的语句加 aws:RequestedRegion；全局服务（IAM）的 Action 拆出来，不加
 *  (c) 上游那条无条件的安全组入站规则修改，限定到本集群 VPC 的安全组（ec2:Vpc）
 *  (d) elbv2.k8s.aws/cluster tag 条件从「有这个 tag」改成「tag 值是本集群名」
 *  (e) 创建安全组只能建在本集群 VPC 里
 *  (f) 删掉 WAF Classic（waf-regional）和 Shield 的 Action：qwenctl 安装时关掉了这两个功能
 * 上游结构变了（升级策略版本时）找不到要收紧的语句会直接报错，强制重新审查。
 */
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { PolicyLock } from './versions';

export const LBC_POLICY_NAME = 'aws-load-balancer-controller';

export type Condition = Readonly<Record<string, Readonly<Record<string, unknown>>>>;

export interface Statement {
  readonly Sid?: string;
  readonly Effect: string;
  readonly Action: string | readonly string[];
  readonly Resource: string | readonly string[];
  readonly Condition?: Condition;
}

export interface PolicyDocumentJson {
  readonly Version: string;
  readonly Statement: readonly Statement[];
}

/** 收紧时用到的本栈信息；值可以是 CDK token（例如 Aws.PARTITION、跨栈引用的 vpcId） */
export interface PolicyScope {
  readonly partition: string;
  readonly region: string;
  readonly account: string;
  readonly vpcId: string;
  /** 控制器的 --cluster-name，也就是它打在资源上的 elbv2.k8s.aws/cluster tag 的值 */
  readonly clusterName: string;
}

/**
 * (f) 删掉的服务。控制器只有在对应开关打开时才调用它们（v3.6.2 pkg/deploy/stack_deployer.go：
 * `if d.addonsConfig.WAFEnabled && ...`、`if d.addonsConfig.ShieldEnabled`），
 * qwenctl 的 Helm values 设了 enableWaf: false、enableShield: false。
 * 要恢复其中任何一个功能，必须同时改这里和 Helm values，否则控制器会在运行时 AccessDenied。
 * WAF Classic 在 ap-southeast-5 没有端点（waf-regional.ap-southeast-5.amazonaws.com 解析不到）；WAFv2 保留。
 */
export const REMOVED_SERVICES: readonly string[] = ['waf-regional', 'shield'];

/**
 * aws:RequestedRegion 对这些服务不是集群所在 region，加上就会拒绝：
 * - IAM 只有一个端点，「IAM calls are always made to the us-east-1 Region」
 *   （IAM 用户指南 reference_policies_condition-keys.html#condition-keys-requestedregion）
 * - Shield Advanced 在所有商业 region 的端点都是 shield.us-east-1.amazonaws.com（General Reference shield.html）。
 *   Shield 的 Action 已经被 (f) 删掉，留在这里是为了以后恢复时不会被加上 RequestedRegion
 */
const GLOBAL_SERVICES: readonly string[] = ['iam', 'shield'];
const SG_INGRESS_ACTIONS = ['ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress'];
const CREATE_SG_ACTIONS = ['ec2:CreateSecurityGroup'];
const CLUSTER_TAG_KEYS = ['aws:ResourceTag/elbv2.k8s.aws/cluster', 'aws:RequestTag/elbv2.k8s.aws/cluster'];

/** jest 直接跑 lib/*.ts，cdk 跑编译后的 dist/lib/*.js；策略原文只在 lib/policies 里（随 git archive 进交付包） */
export function policiesDir(start: string = __dirname): string {
  const candidates = [path.join(start, 'policies'), path.resolve(start, '..', '..', 'lib', 'policies')];
  const found = candidates.find((dir) => fs.existsSync(dir));
  if (!found) {
    throw new Error(`找不到上游 IAM 策略目录（查找了 ${candidates.join('、')}），交付包可能不完整`);
  }
  return found;
}

/** 读取并校验上游策略原文：sha256 必须和 versions.json 锁定的一致（防止文件被改、或升级时漏改锁定值） */
export function loadUpstreamPolicy(name: string, lock: PolicyLock, dir: string = policiesDir()): PolicyDocumentJson {
  const file = path.join(dir, `${name}-${lock.version}.json`);
  let content: Buffer;
  try {
    content = fs.readFileSync(file);
  } catch (error: unknown) {
    throw new Error(`无法读取上游 IAM 策略 ${file}：${error instanceof Error ? error.message : String(error)}`);
  }
  const actual = crypto.createHash('sha256').update(content).digest('hex');
  if (actual !== lock.sha256) {
    throw new Error(
      `上游 IAM 策略 ${file} 的 sha256 是 ${actual}，与 versions.json 锁定的 ${lock.sha256} 不一致` +
        `（文件被改过，或者升级时没有同步更新 versions.json；来源：${lock.source}）`,
    );
  }
  let doc: PolicyDocumentJson;
  try {
    doc = JSON.parse(content.toString('utf-8')) as PolicyDocumentJson;
  } catch (error: unknown) {
    throw new Error(`上游 IAM 策略 ${file} 不是合法的 JSON：${error instanceof Error ? error.message : String(error)}`);
  }
  if (doc.Version !== '2012-10-17' || !Array.isArray(doc.Statement) || doc.Statement.length === 0) {
    throw new Error(`上游 IAM 策略 ${file} 不是 2012-10-17 版本的策略文档，或者没有 Statement`);
  }
  return doc;
}

/** 返回收紧后的新策略文档，不修改 doc */
export function tightenPolicy(doc: PolicyDocumentJson, scope: PolicyScope): PolicyDocumentJson {
  const prepared = doc.Statement.flatMap(dropRemovedServices)
    .map((s) => scopeArns(s, scope))
    .map((s) => bindClusterTag(s, scope.clusterName));
  assertClusterTagBound(prepared);
  const sgIngress = uniqueIndex(prepared, SG_INGRESS_ACTIONS, '无条件的安全组入站规则');
  const createSg = uniqueIndex(prepared, CREATE_SG_ACTIONS, '无条件的创建安全组');
  const statements = prepared.flatMap((s, i) => {
    if (i === sgIngress) return [restrictToVpc(s, scope)];
    if (i === createSg) return [createInVpcOnly(s, scope)];
    return pinRegion(s, scope.region);
  });
  assertNoWildcardArns(statements);
  return { Version: doc.Version, Statement: statements };
}

const asArray = (value: string | readonly string[]): readonly string[] => (typeof value === 'string' ? [value] : value);
const isWildcardResource = (s: Statement): boolean => {
  const resources = asArray(s.Resource);
  return resources.length === 1 && resources[0] === '*';
};
const serviceOf = (action: string): string => action.split(':')[0];
const ec2Arn = (scope: PolicyScope, resource: string): string =>
  `arn:${scope.partition}:ec2:${scope.region}:${scope.account}:${resource}`;

/** (f) 去掉被删服务的 Action；整条都是被删服务的语句直接去掉 */
function dropRemovedServices(s: Statement): Statement[] {
  const kept = asArray(s.Action).filter((a) => !REMOVED_SERVICES.includes(serviceOf(a)));
  if (kept.length === 0) return [];
  return [kept.length === asArray(s.Action).length ? s : { ...s, Action: kept }];
}

/**
 * (a) 上游 ARN 写成 arn:aws:ec2:*:*:security-group/... 和 arn:aws:elasticloadbalancing:*:*:targetgroup/... 等，
 * 等于允许操作任意 region、任意账号的资源（跨账号还要对方授权，但不应该由我们放开）。控制器只管本集群
 * 所在 region、本账号的负载均衡器和安全组；partition 也不写死，中国区 / GovCloud 同样适用。
 * ARN 格式见 Service Authorization Reference：EC2 security-group 为
 * arn:${Partition}:ec2:${Region}:${Account}:security-group/${SecurityGroupId}，ELBv2 同理。
 */
function scopeArns(s: Statement, scope: PolicyScope): Statement {
  if (isWildcardResource(s)) return s;
  const resources = asArray(s.Resource).map((arn) =>
    arn.replace(
      /^arn:aws:([a-z0-9-]+):\*:\*:/,
      (_match, service: string) => `arn:${scope.partition}:${service}:${scope.region}:${scope.account}:`,
    ),
  );
  return { ...s, Resource: typeof s.Resource === 'string' ? resources[0] : resources };
}

/**
 * (d) 上游用 Null = false 只检查「有 elbv2.k8s.aws/cluster tag」，同一账号里别的集群（或任何人手动打上这个 tag）
 * 的负载均衡器、目标组、安全组都能被改、被删。控制器打的 tag 值就是 --cluster-name
 * （v3.6.2 pkg/deploy/tracking/provider.go StackTags：TagKeyK8sCluster: p.clusterName；
 * 后端安全组见 pkg/networking/backend_sg_provider.go），所以改成 StringEquals = 本集群名。
 * StringEquals 已经隐含「key 必须存在」：IAM 用户指南 reference_policies_elements_condition_operators.html，
 * 「If the key that you specify in a policy condition is not present in the request context, the values do not
 * match and the condition is false」。
 * Null = true（请求里不能带这个 tag，防止把别人的资源改成「属于本集群」）保持上游原样。
 */
function bindClusterTag(s: Statement, clusterName: string): Statement {
  const nullCondition = s.Condition?.Null;
  if (!nullCondition) return s;
  const bound = CLUSTER_TAG_KEYS.filter((key) => nullCondition[key] === 'false');
  if (bound.length === 0) return s;
  const remainingNull = Object.fromEntries(Object.entries(nullCondition).filter(([key]) => !bound.includes(key)));
  const others: Condition = Object.fromEntries(Object.entries(s.Condition ?? {}).filter(([op]) => op !== 'Null'));
  const withoutNull: Condition = Object.keys(remainingNull).length > 0 ? { ...others, Null: remainingNull } : others;
  const condition = bound.reduce<Condition>(
    (acc, key) => withCondition(acc, 'StringEquals', key, clusterName),
    withoutNull,
  );
  return { ...s, Condition: condition };
}

/**
 * (b) ec2:Describe*、elasticloadbalancing:Create* 等操作不支持（或上游没用）资源级限制，只能写 Resource "*"。
 * 加 aws:RequestedRegion = 本 region：凭证泄露时不能拿去别的 region 建负载均衡器、改安全组。
 * 同一条语句里混有全局服务的 Action（上游把 iam:GetServerCertificate 和 acm、wafv2 写在一起）时拆成两条：
 * 全局服务那部分保持上游原样，否则控制器读 IAM 服务器证书会被拒绝。
 * iam:CreateServiceLinkedRole 那条整条都是 IAM，原样保留。
 */
function pinRegion(s: Statement, region: string): Statement[] {
  if (!isWildcardResource(s)) return [s];
  const actions = asArray(s.Action);
  const global = actions.filter((a) => GLOBAL_SERVICES.includes(serviceOf(a)));
  if (global.length === actions.length) return [s];
  const regional = actions.filter((a) => !global.includes(a));
  const pinned: Statement = {
    ...s,
    Action: regional,
    Condition: withCondition(s.Condition, 'StringEquals', 'aws:RequestedRegion', region),
  };
  return global.length > 0 ? [pinned, { ...s, Action: global }] : [pinned];
}

/**
 * (c) 上游有一条对 "*"、没有任何条件的 ec2:AuthorizeSecurityGroupIngress / RevokeSecurityGroupIngress
 * （控制器要给节点安全组加放行 ALB 的规则），等于能改账号里任意安全组。
 * Service Authorization Reference（EC2）：这两个操作的 security-group 资源支持条件键 ec2:Vpc（ARN 类型）；
 * 写法照 EC2 用户指南 iam-policies-ec2-console.html「Work with security groups」的官方示例：
 * Resource 用 security-group ARN + ArnEquals ec2:Vpc = VPC ARN。
 * 另一个可选资源 security-group-rule「only enforced when the API request includes TagSpecifications」，控制器不带。
 */
function restrictToVpc(s: Statement, scope: PolicyScope): Statement {
  return {
    ...s,
    Resource: ec2Arn(scope, 'security-group/*'),
    Condition: { ArnEquals: { 'ec2:Vpc': ec2Arn(scope, `vpc/${scope.vpcId}`) } },
  };
}

/**
 * (e) 上游 ec2:CreateSecurityGroup 对 "*" 无条件，能在账号里任意 VPC 建安全组。
 * Service Authorization Reference（EC2，list_ec2.html）：CreateSecurityGroup 的资源是 security-group*（必需）
 * 和 vpc（请求里带 VpcId 时参与授权）。Resource 同时列出两者：新安全组的 ARN 匹配 security-group/*，
 * 目标 VPC 必须是本集群 VPC。控制器建安全组时总是带 VpcId（v3.6.2 pkg/deploy/ec2/security_group_manager.go、
 * pkg/networking/backend_sg_provider.go 的 CreateSecurityGroupInput 都设了 VpcId）。
 * Resource 不再是 "*"，不需要再加 RequestedRegion（ARN 已经固定了 region）。
 */
function createInVpcOnly(s: Statement, scope: PolicyScope): Statement {
  return { ...s, Resource: [ec2Arn(scope, 'security-group/*'), ec2Arn(scope, `vpc/${scope.vpcId}`)] };
}

/** 找唯一一条 Action 正好是 actions、Resource "*"、没有条件的语句；不是正好一条时报错 */
function uniqueIndex(statements: readonly Statement[], actions: readonly string[], label: string): number {
  const wanted = [...actions].sort().join();
  const matches = statements
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => s.Condition === undefined && isWildcardResource(s) && [...asArray(s.Action)].sort().join() === wanted);
  if (matches.length !== 1) {
    throw new Error(`上游 LBC 策略里${label}语句有 ${matches.length} 条（应为 1 条）：策略结构变了，请重新审查收紧规则`);
  }
  return matches[0].i;
}

/** (d) 的兜底：不能再有只检查 cluster tag 存在的条件，并且至少收紧了一条（上游改了 tag 写法时会在这里发现） */
function assertClusterTagBound(statements: readonly Statement[]): void {
  const leftover = statements.filter((s) => CLUSTER_TAG_KEYS.some((key) => s.Condition?.Null?.[key] === 'false'));
  const bound = statements.filter((s) => CLUSTER_TAG_KEYS.some((key) => s.Condition?.StringEquals?.[key] !== undefined));
  if (leftover.length > 0 || bound.length === 0) {
    throw new Error('上游 LBC 策略的 elbv2.k8s.aws/cluster tag 条件写法变了，请重新审查收紧规则 (d)');
  }
}

/** 合并条件，返回新对象；同一个键上游已有值时报错（不能悄悄覆盖上游的限制） */
function withCondition(condition: Condition | undefined, operator: string, key: string, value: string): Condition {
  const existing = condition?.[operator] ?? {};
  if (key in existing) {
    throw new Error(`上游语句已有条件 ${operator} ${key}，请人工审查后再收紧`);
  }
  return { ...condition, [operator]: { ...existing, [key]: value } };
}

/** 兜底：收紧后不能再有 region 或 account 是通配的 ARN（上游新增了别的服务的 ARN 时会在这里发现） */
function assertNoWildcardArns(statements: readonly Statement[]): void {
  const leftovers = statements
    .flatMap((s) => asArray(s.Resource))
    .filter((arn) => arn.startsWith('arn:'))
    .filter((arn) => {
      const [, , , region, account] = arn.split(':');
      return region === '*' || account === '*';
    });
  if (leftovers.length > 0) {
    throw new Error(`收紧后仍有通配 region / account 的 ARN：${leftovers.join(', ')}`);
  }
}
