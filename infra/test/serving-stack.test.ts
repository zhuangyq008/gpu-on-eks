import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DeploymentConfig } from '../lib/config';
import { loadVersions } from '../lib/versions';
import { fixture, withOverrides } from './helpers';

function synth(config: DeploymentConfig = fixture()): { template: Template; eksId: string; servingDeps: string[] } {
  const stacks = buildStacks(new cdk.App(), config, loadVersions());
  return {
    template: Template.fromStack(stacks.serving),
    eksId: stacks.eks.stackName,
    servingDeps: stacks.serving.dependencies.map((s) => s.stackName),
  };
}

type PolicyStatement = { Action: string | string[]; Resource: unknown };

function statementsFor(template: Template, roleLogicalIdPrefix: string): PolicyStatement[] {
  const policies = Object.values(template.findResources('AWS::IAM::Policy')).filter((p) =>
    JSON.stringify((p as { Properties: { Roles: unknown } }).Properties.Roles).includes(roleLogicalIdPrefix),
  );
  return policies.flatMap(
    (p) => (p as { Properties: { PolicyDocument: { Statement: PolicyStatement[] } } }).Properties.PolicyDocument.Statement,
  );
}

function actions(statements: PolicyStatement[]): string[] {
  return statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
}

describe('推理服务栈', () => {
  const { template, eksId, servingDeps } = synth();

  test('模型桶：禁止公开访问、强制 TLS、加密、删栈时保留', () => {
    template.hasResource('AWS::S3::Bucket', {
      Properties: {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }],
        },
        OwnershipControls: { Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] },
        VersioningConfiguration: { Status: 'Enabled' },
        LifecycleConfiguration: {
          Rules: [Match.objectLike({ NoncurrentVersionExpiration: { NoncurrentDays: 7 }, Status: 'Enabled' })],
        },
      },
      DeletionPolicy: 'Retain',
    });
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        ]),
      },
    });
  });

  test('vLLM 和同步 Job 的 Pod Identity 关联到 qwenctl 生成的命名', () => {
    template.hasResourceProperties('AWS::EKS::PodIdentityAssociation', {
      ClusterName: 'qwen-vl',
      Namespace: 'qwen-vl',
      ServiceAccount: 'vllm',
    });
    template.hasResourceProperties('AWS::EKS::PodIdentityAssociation', { ServiceAccount: 'model-sync' });
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: 'pods.eks.amazonaws.com' },
            Condition: {
              StringEquals: {
                'aws:RequestTag/eks-cluster-name': 'qwen-vl',
                'aws:RequestTag/kubernetes-namespace': 'qwen-vl',
                'aws:RequestTag/kubernetes-service-account': 'vllm',
              },
            },
          }),
        ],
      },
    });
  });

  test('vLLM 只读 models/*；同步 Job 能写但不能删', () => {
    const vllm = actions(statementsFor(template, 'VllmRole'));
    expect(vllm).toEqual(expect.arrayContaining(['s3:GetObject*', 's3:List*']));
    expect(vllm.some((a) => a.startsWith('s3:Put') || a.startsWith('s3:Delete'))).toBe(false);
    const sync = actions(statementsFor(template, 'ModelSyncRole'));
    expect(sync).toEqual(expect.arrayContaining(['s3:PutObject', 's3:GetObject*']));
    expect(sync.some((a) => a.startsWith('s3:Delete'))).toBe(false);
    expect(JSON.stringify(statementsFor(template, 'VllmRole'))).toContain('/models/*');
  });

  test('依赖 EKS 栈（Pod Identity 关联需要集群先存在），输出桶名', () => {
    expect(servingDeps).toContain(eksId);
    template.hasOutput('ModelBucketName', {});
  });
});

describe('推理服务栈可选项', () => {
  test('客户 CMK 加密模型桶，并启用 Bucket Key', () => {
    const base = fixture();
    const kmsKey = 'arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555';
    const { template } = synth(
      withOverrides(base, { advanced: { compliance: { ...base.advanced.compliance, kms_key_arn: kmsKey } } }),
    );
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: 'aws:kms', KMSMasterKeyID: kmsKey }, BucketKeyEnabled: true },
        ],
      },
    });
  });

  test('serving 命名不合法时拒绝', () => {
    expect(() => withOverrides(fixture(), { serving: { namespace: 'Bad_NS' } })).toThrow(/serving\.namespace/);
    expect(() => withOverrides(fixture(), { serving: { modelPrefix: 'models' } })).toThrow(/serving\.modelPrefix/);
    expect(() => withOverrides(fixture(), { serving: { namespace: 'kube-system' } })).toThrow(/kube- 开头/);
    expect(() => withOverrides(fixture(), { serving: { namespace: 'a'.repeat(64) } })).toThrow(/serving\.namespace/);
  });
});
