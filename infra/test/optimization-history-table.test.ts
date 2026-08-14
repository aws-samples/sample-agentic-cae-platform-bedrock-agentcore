import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { CaePlatformStack } from '../lib/cae-platform-stack';

/**
 * Task 1.2 — Unit tests for OptimizationHistoryTable CDK provisioning.
 *
 * Validates Requirements 6.1–6.5:
 *  - 6.1: Table provisioned with correct key schema (userId PK, sessionId#timestamp SK)
 *  - 6.2: On-demand capacity mode (PAY_PER_REQUEST)
 *  - 6.3: RemovalPolicy.DESTROY for clean teardown
 *  - 6.4: Sessions Lambda and WebSocket message handler have read/write permissions
 *  - 6.5: Table name passed as OPTIMIZATION_HISTORY_TABLE env var to both Lambdas
 */
describe('CaePlatformStack OptimizationHistoryTable (task 1.2)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'OptHistoryStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates the OptimizationHistoryTable with correct key schema (Requirement 6.1)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'userId', KeyType: 'HASH' },
        { AttributeName: 'sessionId#timestamp', KeyType: 'RANGE' },
      ],
      AttributeDefinitions: Match.arrayWith([
        { AttributeName: 'userId', AttributeType: 'S' },
        { AttributeName: 'sessionId#timestamp', AttributeType: 'S' },
      ]),
    });
  });

  test('uses PAY_PER_REQUEST billing mode (Requirement 6.2)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'userId', KeyType: 'HASH' },
        { AttributeName: 'sessionId#timestamp', KeyType: 'RANGE' },
      ],
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('enables TTL on the expiresAt attribute (Requirement 6.1)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'userId', KeyType: 'HASH' },
        { AttributeName: 'sessionId#timestamp', KeyType: 'RANGE' },
      ],
      TimeToLiveSpecification: {
        AttributeName: 'expiresAt',
        Enabled: true,
      },
    });
  });

  test('applies DESTROY removal policy for clean teardown (Requirement 6.3)', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    // Find the OptimizationHistoryTable by its key schema
    const historyTable = Object.values(tables).find((t: any) =>
      t.Properties.KeySchema?.some(
        (k: any) => k.AttributeName === 'sessionId#timestamp',
      ),
    ) as any;
    expect(historyTable).toBeDefined();
    expect(historyTable.DeletionPolicy).toBe('Delete');
    expect(historyTable.UpdateReplacePolicy).toBe('Delete');
  });

  test('grants the Sessions Lambda read/write access to the table (Requirement 6.4)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // The Sessions handler policy includes dynamodb actions scoped to the
    // OptimizationHistoryTable ARN (grantReadWriteData).
    const sessionsPolicy = Object.entries(policies).find(
      ([logicalId, p]: [string, any]) =>
        logicalId.includes('SessionsHandler') &&
        JSON.stringify(p.Properties.PolicyDocument).includes('dynamodb:PutItem') &&
        JSON.stringify(p.Properties.PolicyDocument).includes('dynamodb:Query'),
    );
    expect(sessionsPolicy).toBeDefined();
  });

  test('grants the WebSocket message handler write access to the table (Requirement 6.4)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // The WebSocket message handler policy includes dynamodb:PutItem scoped
    // to the OptimizationHistoryTable ARN (grantWriteData).
    const wsPolicy = Object.entries(policies).find(
      ([logicalId, p]: [string, any]) =>
        logicalId.includes('WebSocketMessageHandler') &&
        JSON.stringify(p.Properties.PolicyDocument).includes('dynamodb:PutItem'),
    );
    expect(wsPolicy).toBeDefined();
  });

  test('passes OPTIMIZATION_HISTORY_TABLE env var to the Sessions Lambda (Requirement 6.5)', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.handler',
      Environment: {
        Variables: Match.objectLike({
          OPTIMIZATION_HISTORY_TABLE: Match.anyValue(),
          SESSIONS_TABLE: Match.anyValue(),
        }),
      },
    });
  });

  test('passes OPTIMIZATION_HISTORY_TABLE env var to the WebSocket message handler (Requirement 6.5)', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.message_handler',
      Environment: {
        Variables: Match.objectLike({
          OPTIMIZATION_HISTORY_TABLE: Match.anyValue(),
        }),
      },
    });
  });

  test('IAM policy grants are scoped to table ARN with no bare wildcard (Requirement 6.4)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // Find all policies that reference dynamodb actions and verify none use bare "*"
    const dynamoPolicies = Object.values(policies).filter((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('dynamodb:'),
    );
    expect(dynamoPolicies.length).toBeGreaterThan(0);
    for (const policy of dynamoPolicies as any[]) {
      for (const statement of policy.Properties.PolicyDocument.Statement) {
        const actions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        // Only check statements with DynamoDB actions
        if (!actions.some((a: string) => a.startsWith('dynamodb:'))) {
          continue;
        }
        const resources = Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource];
        expect(resources).not.toContain('*');
      }
    }
  });
});
