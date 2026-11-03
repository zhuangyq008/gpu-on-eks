/**
 * 网络栈：新建 VPC，或导入客户现有 VPC；按 qwenctl 算好的计划创建终端节点。
 *
 * 现有 VPC 只做加法：不打 tag、不改路由、不加 Flow Logs。建哪些终端节点由 CLI 决定
 * （cli/qwenctl/endpoints.py：它能查到客户账号里已有的终端节点和各 AZ 的服务可用性），
 * 这里不再重复判断。
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { DeploymentConfig } from './config';

const PUBLIC_SUBNET_MASK = 24;
const PRIVATE_SUBNET_MASK = 19;

export interface NetworkStackProps extends cdk.StackProps {
  readonly config: DeploymentConfig;
}

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.IVpc;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);
    const { config } = props;
    this.vpc = config.network.mode === 'new' ? createVpc(this, config) : importVpc(this, config);
    addS3GatewayEndpoint(this.vpc, config);
    addInterfaceEndpoints(this, this.vpc, config);

    new cdk.CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
    new cdk.CfnOutput(this, 'PrivateSubnetIds', {
      value: this.vpc.privateSubnets.map((s) => s.subnetId).join(','),
    });
  }
}

/** 接口终端节点安全组放行的网段：整个 VPC（主 CIDR + 附加 CIDR）。
 *  私有 DNS 对整个 VPC 生效，只放行我们的子网会拦住 VPC 里的其他主机。 */
export function endpointClientCidrs(config: DeploymentConfig): string[] {
  return [config.network.cidr, ...config.network.secondaryCidrs];
}

function createVpc(scope: Construct, config: DeploymentConfig): ec2.Vpc {
  const kmsKeyArn = config.advanced.compliance.kms_key_arn;
  const flowLogGroup = new logs.LogGroup(scope, 'VpcFlowLogs', {
    retention: config.advanced.observability.log_retention_days.access as logs.RetentionDays,
    // 客户的 CMK 需要在密钥策略里允许 logs.<region>.amazonaws.com 使用
    encryptionKey: kmsKeyArn ? kms.Key.fromKeyArn(scope, 'LogKey', kmsKeyArn) : undefined,
    // Flow Logs 是安全审计证据：删栈时保留，由 qwenctl destroy 询问后再删
    removalPolicy: cdk.RemovalPolicy.RETAIN,
  });
  const vpc = new ec2.Vpc(scope, 'Vpc', {
    ipAddresses: ec2.IpAddresses.cidr(config.network.cidr),
    availabilityZones: config.zones.map((z) => z.zoneName),
    natGateways: config.advanced.network.nat_gateways,
    subnetConfiguration: [
      { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: PUBLIC_SUBNET_MASK, mapPublicIpOnLaunch: false },
      { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: PRIVATE_SUBNET_MASK },
    ],
    restrictDefaultSecurityGroup: true,
    flowLogs: {
      all: {
        destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
        trafficType: ec2.FlowLogTrafficType.ALL,
      },
    },
  });
  // AWS Load Balancer Controller 按这两个 tag 选子网
  vpc.publicSubnets.forEach((s) => cdk.Tags.of(s).add('kubernetes.io/role/elb', '1'));
  vpc.privateSubnets.forEach((s) => cdk.Tags.of(s).add('kubernetes.io/role/internal-elb', '1'));
  return vpc;
}

function importVpc(scope: Construct, config: DeploymentConfig): ec2.IVpc {
  const n = config.network;
  if (!n.vpcId) {
    throw new Error('使用现有 VPC 时必须提供 vpcId');
  }
  // config.ts 已校验 privateSubnets 与 zones 一一对应、顺序一致
  return ec2.Vpc.fromVpcAttributes(scope, 'Vpc', {
    vpcId: n.vpcId,
    vpcCidrBlock: n.cidr,
    availabilityZones: config.zones.map((z) => z.zoneName),
    privateSubnetIds: n.privateSubnets.map((s) => s.subnetId),
    privateSubnetIpv4CidrBlocks: n.privateSubnets.map((s) => s.cidr),
    privateSubnetRouteTableIds: n.privateSubnets.map((s) => s.routeTableId),
    publicSubnetIds: n.publicSubnets.length > 0 ? n.publicSubnets.map((s) => s.subnetId) : undefined,
  });
}

function addS3GatewayEndpoint(vpc: ec2.IVpc, config: DeploymentConfig): void {
  const plan = config.network.endpoints;
  if (!plan.s3Gateway) {
    return;
  }
  // 权重（37.5 GB）和编译缓存都从 S3 读，走网关终端节点不经过 NAT
  const wanted = new Set(plan.s3RouteTableIds);
  const subnets =
    config.network.mode === 'new'
      ? vpc.privateSubnets
      : vpc.privateSubnets.filter((s) => wanted.has(s.routeTable.routeTableId));
  vpc.addGatewayEndpoint('S3Endpoint', {
    service: ec2.GatewayVpcEndpointAwsService.S3,
    subnets: [{ subnets }],
  });
}

function addInterfaceEndpoints(scope: Construct, vpc: ec2.IVpc, config: DeploymentConfig): void {
  const services = config.network.endpoints.interface;
  if (services.length === 0) {
    return;
  }
  // 终端节点只接受入站 HTTPS，本身不发起连接；allowAllOutbound=false 时 CDK 会加一条占位的出站规则
  const securityGroup = new ec2.SecurityGroup(scope, 'EndpointSecurityGroup', {
    vpc,
    description: 'VPC interface endpoints: HTTPS from inside the VPC only',
    allowAllOutbound: false,
  });
  endpointClientCidrs(config).forEach((cidr) => {
    securityGroup.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(443), `HTTPS from ${cidr}`);
  });
  for (const service of services) {
    vpc.addInterfaceEndpoint(`Endpoint-${service.replace(/\./g, '-')}`, {
      service: new ec2.InterfaceVpcEndpointAwsService(service),
      subnets: { subnets: vpc.privateSubnets },
      securityGroups: [securityGroup],
      open: false,
      privateDnsEnabled: true,
    });
  }
}
