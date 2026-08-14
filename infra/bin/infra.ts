#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { CaePlatformStack, PlatformEnvironment } from '../lib/cae-platform-stack';

const app = new cdk.App();

const rawEnvironment = app.node.tryGetContext('environment') ?? process.env.PLATFORM_ENVIRONMENT ?? 'dev';
const validEnvironments: PlatformEnvironment[] = ['dev', 'staging', 'prod'];

if (!validEnvironments.includes(rawEnvironment)) {
  throw new Error(
    `Invalid environment "${rawEnvironment}". Expected one of: ${validEnvironments.join(', ')}. ` +
      'Set via "cdk deploy -c environment=<env>" or the PLATFORM_ENVIRONMENT env var.'
  );
}
const environment = rawEnvironment as PlatformEnvironment;

new CaePlatformStack(app, `CaePlatformStack-${environment}`, {
  environment,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
