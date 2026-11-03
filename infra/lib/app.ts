/**
 * 根据部署配置组装 CDK App。栈按依赖顺序加入：network → eks → （后续：gpu、observability）。
 */
import * as cdk from 'aws-cdk-lib';
import { DeploymentConfig } from './config';
import { EksStack } from './eks-stack';
import { NetworkStack } from './network-stack';
import { Versions, loadVersions } from './versions';

export interface QwenVlStacks {
  readonly network: NetworkStack;
  readonly eks: EksStack;
}

export function stackNames(name: string): { network: string; eks: string } {
  return { network: `${name}-network`, eks: `${name}-eks` };
}

export function buildStacks(app: cdk.App, config: DeploymentConfig, versions: Versions = loadVersions()): QwenVlStacks {
  const env: cdk.Environment = { account: config.account, region: config.region };
  const names = stackNames(config.name);
  const network = new NetworkStack(app, names.network, {
    env,
    config,
    description: `Qwen-VL on EKS (${config.name}): VPC and endpoints`,
  });
  const eks = new EksStack(app, names.eks, {
    env,
    config,
    versions,
    vpc: network.vpc,
    description: `Qwen-VL on EKS (${config.name}): EKS cluster, system nodes, Karpenter IAM`,
  });
  applyTags(app, config);
  return { network, eks };
}

function applyTags(app: cdk.App, config: DeploymentConfig): void {
  cdk.Tags.of(app).add('qwenctl:deployment', config.name);
  for (const [key, value] of Object.entries(config.advanced.compliance.tags)) {
    cdk.Tags.of(app).add(key, value);
  }
}
