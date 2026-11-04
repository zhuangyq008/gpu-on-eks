/**
 * 部署配置契约：qwenctl（cli/qwenctl/models.py）生成的 JSON。
 * CDK 只信任通过这里校验的配置；契约测试见 test/contract.test.ts。
 */
import * as fs from "fs";
import { z } from "zod";

export const SCHEMA_VERSION = 1;

/** CloudWatch Logs 只接受这些保留天数 */
export const LOG_RETENTION_DAYS = [
  1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827,
  2192, 2557, 2922, 3288, 3653,
] as const;

const retentionDays = z
  .number()
  .int()
  .refine((d) => (LOG_RETENTION_DAYS as readonly number[]).includes(d), {
    message: "不是 CloudWatch 支持的保留天数",
  });

/** 解析 IPv4 CIDR；不合法时返回 undefined */
export function parseCidr(value: string): { prefix: number } | undefined {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(
    value,
  );
  if (!match) return undefined;
  const octets = match.slice(1, 5).map(Number);
  const prefix = Number(match[5]);
  if (octets.some((o) => o > 255) || prefix > 32) return undefined;
  return { prefix };
}

const cidr = z
  .string()
  .refine((v) => parseCidr(v) !== undefined, {
    message: "不是合法的 IPv4 CIDR",
  });
const zoneId = z.string().regex(/^[a-z0-9]+-az\d+$/, "不是合法的 AZ ID");
const subnetId = z.string().regex(/^subnet-[0-9a-f]+$/, "不是合法的子网 ID");
const serviceShortName = z
  .string()
  .regex(/^[a-z0-9][a-z0-9.-]*$/, "应该是短服务名（例如 ecr.api）");

const zoneSchema = z.object({ zoneId, zoneName: z.string().min(1) });

const profileSchema = z.object({
  name: z.string().min(1),
  instanceTypes: z.array(z.string().min(1)).min(1),
  tensorParallel: z.number().int().positive(),
  replicasPerNode: z.number().int().positive(),
  nodepoolWeight: z.number().int().min(1).max(100),
  experimental: z.boolean(),
  zoneIds: z.array(zoneId).min(1),
  podCpu: z.string().optional(),
  podMemory: z.string().optional(),
});

const k8sName = z
  .string()
  .max(63)
  .regex(/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/, "不是合法的 K8s 名称")
  .refine((n) => !n.startsWith("kube-"), {
    message: "不能使用 kube- 开头的系统命名空间",
  });

/** 推理服务的命名：由 qwenctl 生成，CDK 用来建 Pod Identity 关联和限定 S3 前缀 */
const servingSchema = z
  .object({
    namespace: k8sName,
    vllmServiceAccount: k8sName,
    syncServiceAccount: k8sName,
    modelPrefix: z
      .string()
      .regex(
        /^[a-z0-9][a-z0-9._-]*\/$/,
        "应该是以 / 结尾的 S3 前缀，例如 models/",
      ),
    compileCachePrefix: z
      .string()
      .regex(
        /^[a-z0-9][a-z0-9._-]*\/$/,
        "应该是以 / 结尾的 S3 前缀，例如 compile-cache/",
      ),
  })
  .refine(
    (s) =>
      !s.modelPrefix.startsWith(s.compileCachePrefix) &&
      !s.compileCachePrefix.startsWith(s.modelPrefix),
    {
      // 同步 Job 能写模型前缀：包含了缓存前缀，就等于能往编译缓存里写代码
      message: "modelPrefix 和 compileCachePrefix 不能互相包含",
      path: ["compileCachePrefix"],
    },
  );

const networkSchema = z.object({
  mode: z.enum(["new", "existing"]),
  cidr,
  secondaryCidrs: z.array(cidr),
  vpcId: z
    .string()
    .regex(/^vpc-[0-9a-f]+$/)
    .nullable(),
  privateSubnets: z.array(
    z.object({
      subnetId,
      zoneId,
      cidr,
      routeTableId: z.string().regex(/^rtb-[0-9a-f]+$/, "不是合法的路由表 ID"),
    }),
  ),
  publicSubnets: z.array(z.object({ subnetId, zoneId })),
  endpoints: z.object({
    interface: z.array(serviceShortName),
    s3Gateway: z.boolean(),
    s3RouteTableIds: z.array(z.string().regex(/^rtb-[0-9a-f]+$/)),
  }),
});

/** 只校验 CDK 用到的高级配置；其余字段原样保留（后续的栈会用到） */
const advancedSchema = z.looseObject({
  network: z.looseObject({
    nat_gateways: z.union([z.literal(1), z.literal(2)]),
    eks_endpoint: z.enum(["public-and-private", "private"]),
    interface_endpoints: z.enum(["auto", "on", "off"]),
  }),
  observability: z.looseObject({
    amp: z.enum(["auto", "on", "off"]),
    log_retention_days: z.object({
      app: retentionDays,
      access: retentionDays,
      coldstart: retentionDays,
    }),
  }),
  compliance: z.looseObject({
    kms_key_arn: z
      .string()
      .regex(/^arn:aws[a-z-]*:kms:/, "不是 KMS 密钥 ARN")
      .nullable(),
    tags: z.record(z.string(), z.string()),
    extra_admin_role_arns: z.array(
      z.string().regex(/^arn:aws[a-z-]*:iam::\d{12}:role\//),
    ),
  }),
  general_nodes: z.looseObject({
    arch: z.array(z.enum(["amd64", "arm64"])).min(1),
    capacity_types: z.array(z.enum(["on-demand", "spot"])).min(1),
    cpu_limit: z.number().int().positive(),
  }),
});

type Issue = (path: (string | number)[], message: string) => void;

export const configSchema = z
  .object({
    schemaVersion: z.literal(SCHEMA_VERSION),
    name: z.string().regex(/^[a-z][a-z0-9-]{1,18}[a-z0-9]$/),
    account: z.string().regex(/^\d{12}$/),
    region: z.string().regex(/^[a-z]{2}(-[a-z]+)+-\d$/),
    adminPrincipalArn: z
      .string()
      .regex(
        /^arn:aws[a-z-]*:iam::\d{12}:(role|user)\//,
        "必须是 IAM 角色或用户 ARN",
      ),
    zones: z.array(zoneSchema).length(2),
    network: networkSchema,
    access: z.object({
      mode: z.enum(["internal", "public"]),
      certificateArn: z
        .string()
        .regex(/^arn:aws[a-z-]*:acm:/, "不是 ACM 证书 ARN")
        .nullable(),
      allowedCidrs: z.array(cidr),
      openToInternet: z.boolean(),
    }),
    gpu: z.object({
      minReplicas: z.number().int().positive(),
      maxReplicas: z.number().int().positive(),
      profiles: z.array(profileSchema).min(1),
    }),
    systemNodes: z.object({ instanceType: z.string().min(1) }),
    features: z.object({
      asyncChannel: z.boolean(),
      bedrockOverflow: z.boolean(),
      alertEmail: z.string().nullable(),
    }),
    observability: z.object({ mode: z.enum(["cloudwatch", "grafana"]) }),
    serving: servingSchema,
    advanced: advancedSchema,
  })
  .superRefine((cfg, ctx) => {
    const issue: Issue = (path, message) =>
      ctx.addIssue({ code: "custom", path, message });
    if (cfg.gpu.maxReplicas < cfg.gpu.minReplicas) {
      issue(["gpu", "maxReplicas"], "不能小于 minReplicas");
    }
    checkAccess(cfg.access, issue);
    if (cfg.network.mode === "new") {
      checkNewNetwork(cfg.network, issue);
    } else {
      checkExistingNetwork(cfg.network, cfg.zones, cfg.access.mode, issue);
    }
  });

export type DeploymentConfig = z.infer<typeof configSchema>;
type Network = DeploymentConfig["network"];

function checkAccess(access: DeploymentConfig["access"], issue: Issue): void {
  if (access.mode !== "public") return;
  if (!access.certificateArn)
    issue(["access", "certificateArn"], "公网访问必须提供 ACM 证书");
  if (access.allowedCidrs.length === 0 && !access.openToInternet) {
    issue(
      ["access", "allowedCidrs"],
      "公网访问必须限定来源 CIDR，或者明确确认 openToInternet",
    );
  }
}

function checkNewNetwork(n: Network, issue: Issue): void {
  const prefix = parseCidr(n.cidr)?.prefix ?? 0;
  if (prefix < 16 || prefix > 17) {
    issue(
      ["network", "cidr"],
      "新建 VPC 必须是 /16 或 /17（2 个 /19 私有子网 + 2 个 /24 公有子网）",
    );
  }
  if (n.privateSubnets.length > 0 || n.publicSubnets.length > 0 || n.vpcId) {
    issue(["network"], "新建 VPC 时不应该带 vpcId 或子网");
  }
}

function checkExistingNetwork(
  n: Network,
  zones: DeploymentConfig["zones"],
  accessMode: string,
  issue: Issue,
): void {
  const expected = zones.map((z) => z.zoneId).join(",");
  if (!n.vpcId) issue(["network", "vpcId"], "使用现有 VPC 时必须提供");
  if (n.privateSubnets.map((s) => s.zoneId).join(",") !== expected) {
    issue(
      ["network", "privateSubnets"],
      `必须每个 AZ 一个，并且顺序与 zones 一致（${expected}）`,
    );
  }
  if (
    accessMode === "public" &&
    n.publicSubnets.map((s) => s.zoneId).join(",") !== expected
  ) {
    issue(
      ["network", "publicSubnets"],
      `公网访问需要每个 AZ 一个公有子网（${expected}）`,
    );
  }
  const ours = new Set(n.privateSubnets.map((s) => s.routeTableId));
  const stray = n.endpoints.s3RouteTableIds.filter((rt) => !ours.has(rt));
  if (stray.length > 0) {
    issue(
      ["network", "endpoints", "s3RouteTableIds"],
      `只能是所选私有子网的路由表：${stray.join(", ")}`,
    );
  }
}

export function parseConfig(raw: unknown): DeploymentConfig {
  const result = configSchema.safeParse(raw);
  if (!result.success) {
    const lines = result.error.issues.map(
      (i) => `  - ${i.path.join(".") || "(根)"}: ${i.message}`,
    );
    throw new Error(`部署配置不合法：\n${lines.join("\n")}`);
  }
  return result.data;
}

export function loadConfig(path: string): DeploymentConfig {
  let text: string;
  try {
    text = fs.readFileSync(path, "utf-8");
  } catch (error: unknown) {
    throw new Error(
      `无法读取部署配置 ${path}：${error instanceof Error ? error.message : String(error)}`,
    );
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (error: unknown) {
    throw new Error(
      `部署配置 ${path} 不是合法的 JSON：${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parseConfig(raw);
}
