#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { SesAnalyticsStack } from './ses-analytics-stack';
import { loadConfig } from './config';

const app = new cdk.App();
const config = loadConfig(app.node.tryGetContext('config') ?? {
  projectName: app.node.tryGetContext('projectName'),
  stage: app.node.tryGetContext('stage'),
  sesConfigurationSetName: app.node.tryGetContext('sesConfigurationSetName'),
  modelId: app.node.tryGetContext('modelId'),
  enableGateway: app.node.tryGetContext('enableGateway'),
  enableRuntime: app.node.tryGetContext('enableRuntime'),
  memoryEventExpiryDays: app.node.tryGetContext('memoryEventExpiryDays'),
});

new SesAnalyticsStack(app, `${config.projectName}-${config.stage}`, {
  config,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
});

// Enable cdk-nag AWS Solutions checks — findings print during cdk synth
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
