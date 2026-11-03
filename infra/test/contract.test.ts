/**
 * 契约测试：让 Python 端（qwenctl）真实生成配置，再交给 zod 校验和 CDK synth。
 * 两边任何一边改了字段，这里会先失败。
 */
import { execFileSync } from 'child_process';
import * as path from 'path';
import { parseConfig } from '../lib/config';
import { endpointServices, synthNetwork } from './helpers';

const CLI_DIR = path.resolve(__dirname, '..', '..', 'cli');

function generate(mode: 'new' | 'existing'): unknown {
  const stdout = execFileSync('python3', ['-m', 'tests.sample_config', mode], {
    cwd: CLI_DIR,
    encoding: 'utf-8',
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  return JSON.parse(stdout);
}

describe('qwenctl → CDK 契约', () => {
  test('新建 VPC 的配置能通过校验并 synth', () => {
    const config = parseConfig(generate('new'));
    expect(config.network.mode).toBe('new');
    const template = synthNetwork(config);
    template.resourceCountIs('AWS::EC2::VPC', 1);
    expect(endpointServices(template, 'Interface')).toEqual(['ecr.api', 'ecr.dkr', 'eks-auth', 'sqs', 'sts']);
  });

  test('现有 VPC 的配置能通过校验并 synth（默认不建接口终端节点）', () => {
    const config = parseConfig(generate('existing'));
    expect(config.network.privateSubnets.map((s) => s.zoneId)).toEqual(['apse5-az1', 'apse5-az2']);
    const template = synthNetwork(config);
    template.resourceCountIs('AWS::EC2::VPC', 0);
    expect(endpointServices(template, 'Interface')).toEqual([]);
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      VpcEndpointType: 'Gateway',
      RouteTableIds: ['rtb-0b0000000000000b2'],
    });
  });

  test('未校验的高级配置字段原样保留，给后续的栈使用', () => {
    const config = parseConfig(generate('new')) as unknown as { advanced: Record<string, unknown> };
    expect(Object.keys(config.advanced)).toEqual(
      expect.arrayContaining(['serving', 'scaling', 'capacity', 'image', 'cdk']),
    );
  });
});
