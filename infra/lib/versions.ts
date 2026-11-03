/**
 * 读取仓库根目录的 versions.json（与 qwenctl 共用的锁定版本）。
 */
import * as fs from 'fs';
import * as path from 'path';
import { z } from 'zod';

const versionsSchema = z.object({
  kubernetes: z.string().regex(/^1\.\d+$/),
  bottlerocket: z.string().regex(/^v\d+\.\d+\.\d+$/),
  eksAddons: z.record(z.string(), z.string().min(1)),
  tools: z.record(z.string(), z.unknown()),
  charts: z.record(z.string(), z.object({ repository: z.string(), version: z.string() })),
});

export type Versions = z.infer<typeof versionsSchema>;

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
