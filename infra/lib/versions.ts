/**
 * 读取仓库根目录的 versions.json（与 qwenctl 共用的锁定版本）。
 */
import * as fs from 'fs';
import * as path from 'path';
import { z } from 'zod';

/** 上游 IAM 策略原文的锁定信息：原文放在 infra/lib/policies/，synth 时按 sha256 校验 */
const policyLockSchema = z.object({
  version: z.string().regex(/^v\d+\.\d+\.\d+$/),
  source: z.string().startsWith('https://'),
  sha256: z.string().regex(/^[0-9a-f]{64}$/, '应该是 64 位小写十六进制'),
});

const versionsSchema = z.object({
  kubernetes: z.string().regex(/^1\.\d+$/),
  bottlerocket: z.string().regex(/^v\d+\.\d+\.\d+$/),
  eksAddons: z.record(z.string(), z.string().min(1)),
  tools: z.record(z.string(), z.unknown()),
  charts: z.record(z.string(), z.object({ repository: z.string(), version: z.string() })),
  // 旧版 versions.json 没有这一段；_comment 等其余键忽略
  policies: z.looseObject({ 'aws-load-balancer-controller': policyLockSchema.optional() }).optional(),
});

export type Versions = z.infer<typeof versionsSchema>;
export type PolicyLock = z.infer<typeof policyLockSchema>;

/** 交付包里 infra/ 和 versions.json 的相对位置固定；编译后的 dist/lib 也向上找 */
export function loadVersions(start: string = __dirname): Versions {
  let dir = start;
  for (let i = 0; i < 5; i += 1) {
    const candidate = path.join(dir, 'versions.json');
    if (fs.existsSync(candidate)) {
      return versionsSchema.parse(JSON.parse(fs.readFileSync(candidate, 'utf-8')));
    }
    dir = path.dirname(dir);
  }
  throw new Error(`找不到 versions.json（从 ${start} 向上查找了 5 层）`);
}

/** EKS 核心 add-on：创建节点组之前装 DaemonSet 类的，之后装需要节点才能 Ready 的 */
export const ADDONS_BEFORE_NODES = ['vpc-cni', 'kube-proxy', 'eks-pod-identity-agent'] as const;
export const ADDONS_AFTER_NODES = ['coredns', 'eks-node-monitoring-agent'] as const;

/** 取 add-on 版本；versions.json 里没有时报错（不能让 EKS 自己选版本） */
export function addonVersion(versions: Versions, name: string): string {
  const version = versions.eksAddons[name];
  if (!version) {
    throw new Error(`versions.json 里缺少 EKS add-on ${name} 的版本`);
  }
  return version;
}
