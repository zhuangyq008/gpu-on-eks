/**
 * 推理服务栈：模型权重桶 + vLLM / 权重同步 Job 的 Pod Identity 角色。
 *
 * - vLLM 用 Run:ai Model Streamer 从这个桶流式加载权重（经 S3 网关终端节点，不走 NAT）
 * - vLLM 只读 models/*；同步 Job 可读写 models/* 但不能删除
 * - 桶开启版本控制：同步 Job 有覆盖写权限，权重被替换也能回滚（同步时逐文件校验 HF 的哈希）
 * - 桶删栈时保留（权重 37.5 GB，误删要重新下载），由 qwenctl destroy 确认后清理
 */
import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { DeploymentConfig } from './config';
import { podIdentityRole } from './pod-identity';

export interface ServingStackProps extends cdk.StackProps {
  readonly config: DeploymentConfig;
}

export class ServingStack extends cdk.Stack {
  public readonly modelBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: ServingStackProps) {
    super(scope, id, props);
    const { config } = props;
    const serving = config.serving;
    const kmsKeyArn = config.advanced.compliance.kms_key_arn;
    const encryptionKey = kmsKeyArn ? kms.Key.fromKeyArn(this, 'BucketKey', kmsKeyArn) : undefined;

    this.modelBucket = new s3.Bucket(this, 'ModelBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: encryptionKey ? s3.BucketEncryption.KMS : s3.BucketEncryption.S3_MANAGED,
      encryptionKey,
      bucketKeyEnabled: encryptionKey !== undefined,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      // 权重被覆盖时可以回滚；旧版本 7 天后清理，控制存储费用
      versioned: true,
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(1), noncurrentVersionExpiration: cdk.Duration.days(7) },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const keys = `${serving.modelPrefix}*`;
    const vllm = podIdentityRole(this, 'VllmRole', {
      clusterName: config.name,
      namespace: serving.namespace,
      serviceAccount: serving.vllmServiceAccount,
      description: `vLLM of ${config.name}: read model weights`,
    });
    this.modelBucket.grantRead(vllm, keys);

    const sync = podIdentityRole(this, 'ModelSyncRole', {
      clusterName: config.name,
      namespace: serving.namespace,
      serviceAccount: serving.syncServiceAccount,
      description: `Model sync job of ${config.name}: upload model weights`,
    });
    this.modelBucket.grantRead(sync, keys);
    this.modelBucket.grantPut(sync, keys);

    new cdk.CfnOutput(this, 'ModelBucketName', { value: this.modelBucket.bucketName });
  }
}
