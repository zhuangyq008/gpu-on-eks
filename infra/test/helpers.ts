import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DeploymentConfig, parseConfig } from '../lib/config';

export const FIXTURE_PATH = path.join(__dirname, 'fixtures', 'config.malaysia.json');

/** qwenctl 对 ap-southeast-5 实际生成的配置（账号和身份已脱敏） */
export function rawFixture(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf-8')) as Record<string, unknown>;
}

export function fixture(): DeploymentConfig {
  return parseConfig(rawFixture());
}

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

/** 返回新的配置对象，不修改 base */
export function withOverrides(base: DeploymentConfig, overrides: DeepPartial<DeploymentConfig>): DeploymentConfig {
  return parseConfig(merge(base, overrides));
}

function merge(base: unknown, override: unknown): unknown {
  if (Array.isArray(override) || typeof override !== 'object' || override === null) {
    return override;
  }
  const result: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [key, value] of Object.entries(override)) {
    result[key] = value === undefined ? result[key] : merge(result[key], value);
  }
  return result;
}

export function synthNetwork(config: DeploymentConfig): Template {
  const app = new cdk.App();
  return Template.fromStack(buildStacks(app, config).network);
}

/** com.amazonaws.<region>.ecr.api（字符串或 Fn::Join）-> ecr.api */
export function serviceSuffix(serviceName: unknown): string {
  if (typeof serviceName === 'string') {
    return serviceName.split('.').slice(3).join('.');
  }
  const parts = (serviceName as { 'Fn::Join': [string, unknown[]] })['Fn::Join'][1];
  return String(parts[parts.length - 1]).replace(/^\./, '');
}

export function endpointServices(template: Template, type: 'Interface' | 'Gateway'): string[] {
  const endpoints = template.findResources('AWS::EC2::VPCEndpoint', { Properties: { VpcEndpointType: type } });
  return Object.values(endpoints)
    .map((r) => serviceSuffix((r as { Properties: { ServiceName: unknown } }).Properties.ServiceName))
    .sort();
}
