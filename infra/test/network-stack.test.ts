import { Match } from 'aws-cdk-lib/assertions';
import { endpointClientCidrs } from '../lib/network-stack';
import { endpointServices, fixture, synthNetwork, withOverrides } from './helpers';

const EXISTING_VPC = {
  mode: 'existing' as const,
  cidr: '10.0.0.0/16',
  secondaryCidrs: ['100.64.0.0/16'],
  vpcId: 'vpc-0abc1234',
  privateSubnets: [
    { subnetId: 'subnet-0aaa1111', zoneId: 'apse5-az1', cidr: '10.0.32.0/19', routeTableId: 'rtb-0ccc3333' },
    { subnetId: 'subnet-0bbb2222', zoneId: 'apse5-az2', cidr: '10.0.64.0/19', routeTableId: 'rtb-0ddd4444' },
  ],
  publicSubnets: [],
  endpoints: { interface: [], s3Gateway: true, s3RouteTableIds: ['rtb-0ddd4444'] },
};

describe('新建 VPC', () => {
  const template = synthNetwork(fixture());

  test('在选中的 2 个 AZ 里各建一个公有子网和一个私有子网', () => {
    template.resourceCountIs('AWS::EC2::VPC', 1);
    template.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.100.0.0/16',
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
    const zones = Object.values(template.findResources('AWS::EC2::Subnet')).map(
      (r) => (r as { Properties: { AvailabilityZone: string } }).Properties.AvailabilityZone,
    );
    expect(zones.sort()).toEqual(['ap-southeast-5a', 'ap-southeast-5a', 'ap-southeast-5b', 'ap-southeast-5b']);
  });

  test('公有子网不自动分配公网 IP，并带上 ELB 发现用的 tag', () => {
    template.hasResourceProperties('AWS::EC2::Subnet', {
      MapPublicIpOnLaunch: false,
      Tags: Match.arrayWith([{ Key: 'kubernetes.io/role/elb', Value: '1' }]),
    });
    template.hasResourceProperties('AWS::EC2::Subnet', {
      CidrBlock: Match.stringLikeRegexp('/19$'),
      Tags: Match.arrayWith([{ Key: 'kubernetes.io/role/internal-elb', Value: '1' }]),
    });
  });

  test('默认 1 个 NAT；Flow Logs 记录全部流量，保留 30 天，删栈时保留日志', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
    template.hasResourceProperties('AWS::EC2::FlowLog', { TrafficType: 'ALL', LogDestinationType: 'cloud-watch-logs' });
    template.hasResource('AWS::Logs::LogGroup', {
      Properties: { RetentionInDays: 30 },
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  test('按 CLI 的计划创建 S3 网关终端节点和接口终端节点，全部启用私有 DNS', () => {
    expect(endpointServices(template, 'Gateway')).toEqual(['s3']);
    expect(endpointServices(template, 'Interface')).toEqual(['ecr.api', 'ecr.dkr', 'eks-auth', 'sqs', 'sts']);
    const interfaces = template.findResources('AWS::EC2::VPCEndpoint', { Properties: { VpcEndpointType: 'Interface' } });
    for (const resource of Object.values(interfaces)) {
      expect((resource as { Properties: { PrivateDnsEnabled: boolean } }).Properties.PrivateDnsEnabled).toBe(true);
    }
  });

  test('接口终端节点只放行 VPC 内的 HTTPS', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: 'VPC interface endpoints: HTTPS from inside the VPC only',
      SecurityGroupIngress: [{ CidrIp: '10.100.0.0/16', FromPort: 443, ToPort: 443, IpProtocol: 'tcp', Description: Match.anyValue() }],
    });
  });

  test('打上部署名 tag', () => {
    template.hasResourceProperties('AWS::EC2::VPC', {
      Tags: Match.arrayWith([{ Key: 'qwenctl:deployment', Value: 'qwen-vl' }]),
    });
  });
});

describe('可选项', () => {
  test('2 个 NAT、合规 tag、客户 CMK 加密 Flow Logs', () => {
    const base = fixture();
    const kmsKey = 'arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555';
    const config = withOverrides(base, {
      advanced: {
        network: { ...base.advanced.network, nat_gateways: 2 },
        compliance: { ...base.advanced.compliance, tags: { CostCenter: 'mfg-poc' }, kms_key_arn: kmsKey },
      },
    });
    const template = synthNetwork(config);
    template.resourceCountIs('AWS::EC2::NatGateway', 2);
    template.hasResourceProperties('AWS::EC2::VPC', { Tags: Match.arrayWith([{ Key: 'CostCenter', Value: 'mfg-poc' }]) });
    template.hasResourceProperties('AWS::Logs::LogGroup', { KmsKeyId: kmsKey });
  });

  test('计划里没有接口终端节点时不建安全组', () => {
    const config = withOverrides(fixture(), {
      network: { endpoints: { interface: [], s3Gateway: true, s3RouteTableIds: [] } },
    });
    const template = synthNetwork(config);
    expect(endpointServices(template, 'Interface')).toEqual([]);
    expect(endpointServices(template, 'Gateway')).toEqual(['s3']);
    template.resourcePropertiesCountIs(
      'AWS::EC2::SecurityGroup',
      { GroupDescription: 'VPC interface endpoints: HTTPS from inside the VPC only' },
      0,
    );
  });
});

describe('现有 VPC', () => {
  const template = synthNetwork(withOverrides(fixture(), { network: EXISTING_VPC }));

  test('不创建 VPC、子网、NAT 和 Flow Logs', () => {
    template.resourceCountIs('AWS::EC2::VPC', 0);
    template.resourceCountIs('AWS::EC2::Subnet', 0);
    template.resourceCountIs('AWS::EC2::NatGateway', 0);
    template.resourceCountIs('AWS::EC2::FlowLog', 0);
  });

  test('S3 网关终端节点只加到还没有覆盖的路由表；默认不建接口终端节点', () => {
    expect(endpointServices(template, 'Interface')).toEqual([]);
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      VpcEndpointType: 'Gateway',
      VpcId: 'vpc-0abc1234',
      RouteTableIds: ['rtb-0ddd4444'],
    });
  });

  test('客户要求创建接口终端节点时，安全组放行整个 VPC（主 CIDR + 附加 CIDR）', () => {
    const config = withOverrides(fixture(), {
      network: { ...EXISTING_VPC, endpoints: { ...EXISTING_VPC.endpoints, interface: ['ecr.api', 'ecr.dkr'] } },
    });
    expect(endpointClientCidrs(config)).toEqual(['10.0.0.0/16', '100.64.0.0/16']);
    synthNetwork(config).hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: [
        Match.objectLike({ CidrIp: '10.0.0.0/16', FromPort: 443 }),
        Match.objectLike({ CidrIp: '100.64.0.0/16', FromPort: 443 }),
      ],
    });
  });

  test('所有路由表都已覆盖时不建 S3 网关终端节点', () => {
    const config = withOverrides(fixture(), {
      network: { ...EXISTING_VPC, endpoints: { interface: [], s3Gateway: false, s3RouteTableIds: [] } },
    });
    expect(endpointServices(synthNetwork(config), 'Gateway')).toEqual([]);
  });
});
