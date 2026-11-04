/**
 * EKS Pod Identity 角色：信任策略只允许指定集群里指定 namespace / ServiceAccount 使用。
 * 条件用的是 Pod Identity 的会话 tag（docs.aws.amazon.com/eks/latest/userguide/pod-id-abac.html）。
 */
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export function podIdentityPrincipal(clusterName: string, namespace: string, serviceAccount: string): iam.IPrincipal {
  return new iam.PrincipalWithConditions(new iam.ServicePrincipal('pods.eks.amazonaws.com'), {
    StringEquals: {
      'aws:RequestTag/eks-cluster-name': clusterName,
      'aws:RequestTag/kubernetes-namespace': namespace,
      'aws:RequestTag/kubernetes-service-account': serviceAccount,
    },
  }).withSessionTags();
}

export interface PodIdentityRoleProps {
  readonly clusterName: string;
  readonly namespace: string;
  readonly serviceAccount: string;
  readonly description: string;
  /** 和集群在同一个栈里时传入：关联用 cluster.ref，保证集群先创建（跨栈时由栈依赖保证） */
  readonly cluster?: eks.CfnCluster;
}

/** 创建角色并关联到 ServiceAccount */
export function podIdentityRole(scope: Construct, id: string, props: PodIdentityRoleProps): iam.Role {
  const role = new iam.Role(scope, id, {
    description: props.description,
    assumedBy: podIdentityPrincipal(props.clusterName, props.namespace, props.serviceAccount),
  });
  new eks.CfnPodIdentityAssociation(scope, `${id}Association`, {
    clusterName: props.cluster?.ref ?? props.clusterName,
    namespace: props.namespace,
    serviceAccount: props.serviceAccount,
    roleArn: role.roleArn,
  });
  return role;
}
