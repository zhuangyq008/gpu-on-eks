#!/usr/bin/env node
/**
 * 入口：cdk synth/deploy -c configFile=<qwenctl 生成的 JSON>
 * （也可以用环境变量 QWENCTL_CONFIG 指定）
 */
import * as cdk from 'aws-cdk-lib';
import { buildStacks } from '../lib/app';
import { loadConfig } from '../lib/config';

const app = new cdk.App();
const configPath: unknown = app.node.tryGetContext('configFile') ?? process.env.QWENCTL_CONFIG;
if (typeof configPath !== 'string' || configPath.length === 0) {
  throw new Error('缺少部署配置：用 -c configFile=<path> 或环境变量 QWENCTL_CONFIG 指定 qwenctl 生成的 JSON');
}
buildStacks(app, loadConfig(configPath));
