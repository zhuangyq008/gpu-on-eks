import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as logs from 'aws-cdk-lib/aws-logs';
import { LOG_RETENTION_DAYS, loadConfig, parseCidr, parseConfig } from '../lib/config';
import { FIXTURE_PATH, fixture, rawFixture, withOverrides } from './helpers';

const EXISTING = {
  mode: 'existing' as const,
  cidr: '10.0.0.0/16',
  vpcId: 'vpc-0abc1234',
  privateSubnets: [
    { subnetId: 'subnet-0aaa1111', zoneId: 'apse5-az1', cidr: '10.0.32.0/19', routeTableId: 'rtb-0ccc3333' },
    { subnetId: 'subnet-0bbb2222', zoneId: 'apse5-az2', cidr: '10.0.64.0/19', routeTableId: 'rtb-0ddd4444' },
  ],
  endpoints: { interface: [], s3Gateway: true, s3RouteTableIds: ['rtb-0ddd4444'] },
};

describe('部署配置校验', () => {
  test('qwenctl 生成的马来西亚配置可以通过校验', () => {
    const config = loadConfig(FIXTURE_PATH);
    expect(config.region).toBe('ap-southeast-5');
    expect(config.zones.map((z) => z.zoneId)).toEqual(['apse5-az1', 'apse5-az2']);
    expect(config.gpu.profiles.map((p) => p.name)).toEqual(['g7e-1gpu', 'g6-tp2']);
  });

  test.each([
    ['schemaVersion', { schemaVersion: 2 }, 'schemaVersion'],
    ['zones 数量', { zones: [{ zoneId: 'apse5-az1', zoneName: 'b' }] }, 'zones'],
    ['账号格式', { account: '12345' }, 'account'],
    ['管理员必须是 IAM 角色/用户', { adminPrincipalArn: 'arn:aws:sts::123456789012:assumed-role/A/s' }, 'adminPrincipalArn'],
  ])('拒绝错误的 %s', (_label, override, field) => {
    expect(() => parseConfig({ ...rawFixture(), ...override })).toThrow(field);
  });

  test.each(['10.0.0.0', '999.1.1.1/16', '10.0.0.0/33'])('拒绝不合法的 CIDR %s', (value) => {
    const raw = rawFixture();
    const network = { ...(raw.network as object), cidr: value };
    expect(() => parseConfig({ ...raw, network })).toThrow(/network\.cidr: 不是合法的 IPv4 CIDR/);
  });

  test('parseCidr', () => {
    expect(parseCidr('10.100.0.0/16')).toEqual({ prefix: 16 });
    expect(parseCidr('256.0.0.0/8')).toBeUndefined();
  });

  test('新建 VPC 必须是 /16 或 /17', () => {
    expect(() => withOverrides(fixture(), { network: { cidr: '10.0.0.0/18' } })).toThrow(/\/16 或 \/17/);
    expect(() => withOverrides(fixture(), { network: { cidr: '10.0.0.0/17' } })).not.toThrow();
  });

  test('新建 VPC 不应该带子网', () => {
    expect(() => withOverrides(fixture(), { network: { vpcId: 'vpc-0abc' } })).toThrow(/不应该带 vpcId/);
  });

  test('maxReplicas 不能小于 minReplicas', () => {
    expect(() => withOverrides(fixture(), { gpu: { minReplicas: 4, maxReplicas: 2 } })).toThrow(/gpu\.maxReplicas/);
  });

  test('公网访问必须有证书，并且要么限定来源，要么明确确认对公网开放', () => {
    expect(() => withOverrides(fixture(), { access: { mode: 'public' } })).toThrow(/access\.certificateArn/);
    const cert = 'arn:aws:acm:ap-southeast-5:123456789012:certificate/abc';
    expect(() => withOverrides(fixture(), { access: { mode: 'public', certificateArn: cert } })).toThrow(
      /access\.allowedCidrs/,
    );
    expect(() =>
      withOverrides(fixture(), { access: { mode: 'public', certificateArn: cert, openToInternet: true } }),
    ).not.toThrow();
  });

  test('现有 VPC：私有子网必须与 zones 一一对应且顺序一致', () => {
    expect(() => withOverrides(fixture(), { network: EXISTING })).not.toThrow();
    const swapped = { ...EXISTING, privateSubnets: [...EXISTING.privateSubnets].reverse() };
    expect(() => withOverrides(fixture(), { network: swapped })).toThrow(/network\.privateSubnets: 必须每个 AZ 一个/);
    expect(() => withOverrides(fixture(), { network: { ...EXISTING, vpcId: null } })).toThrow(/network\.vpcId/);
  });

  test('现有 VPC：公网访问需要公有子网；S3 路由表只能是所选子网的', () => {
    const cert = 'arn:aws:acm:ap-southeast-5:123456789012:certificate/abc';
    const bad = () =>
      withOverrides(fixture(), {
        network: { ...EXISTING, endpoints: { ...EXISTING.endpoints, s3RouteTableIds: ['rtb-0eee5555'] } },
        access: { mode: 'public', certificateArn: cert, allowedCidrs: ['203.0.113.0/24'] },
      });
    expect(bad).toThrow(/network\.publicSubnets/);
    expect(bad).toThrow(/s3RouteTableIds: 只能是所选私有子网的路由表：rtb-0eee5555/);
  });

  test('终端节点必须是短服务名', () => {
    const endpoints = { interface: ['com.amazonaws.ap-southeast-5.sts'], s3Gateway: true, s3RouteTableIds: [] };
    expect(() => withOverrides(fixture(), { network: { endpoints } })).not.toThrow();
    const upper = { ...endpoints, interface: ['STS'] };
    expect(() => withOverrides(fixture(), { network: { endpoints: upper } })).toThrow(/短服务名/);
  });

  test('日志保留天数必须是 CloudWatch 支持的值，并且都在 CDK 的枚举里', () => {
    const raw = rawFixture() as { advanced: { observability: Record<string, unknown> } };
    const advanced = {
      ...raw.advanced,
      observability: { ...raw.advanced.observability, log_retention_days: { app: 10, access: 30, coldstart: 90 } },
    };
    expect(() => parseConfig({ ...raw, advanced })).toThrow(/保留天数/);
    const enumValues = new Set(Object.values(logs.RetentionDays).filter((v) => typeof v === 'number'));
    expect(LOG_RETENTION_DAYS.filter((d) => !enumValues.has(d))).toEqual([]);
  });

  test('读不到文件、不是 JSON 时给出清楚的错误', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qwenctl-'));
    const broken = path.join(dir, 'broken.json');
    fs.writeFileSync(broken, '{not json');
    expect(() => loadConfig(path.join(dir, 'missing.json'))).toThrow(/无法读取部署配置/);
    expect(() => loadConfig(broken)).toThrow(/不是合法的 JSON/);
  });
});
