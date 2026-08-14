import * as cdk from 'aws-cdk-lib/core';
import { Template } from 'aws-cdk-lib/assertions';
import { CaePlatformStack } from '../lib/cae-platform-stack';

/**
 * Task 15.2 — CDK infrastructure tests.
 *
 * These tests lock down the finalized `CaePlatformStack` (task 15.1):
 *  - A snapshot of the full `cdk synth` output guards against unintended
 *    template drift.
 *  - Resource-count assertions verify completeness (Requirement 10.1).
 *  - DynamoDB billing-mode + removal-policy assertions (Requirement 10.4).
 *  - A programmatic scan asserting NO IAM policy statement uses a bare "*"
 *    resource ARN — the core least-privilege guarantee of Requirement 10.3.
 *  - S3 removal / auto-delete assertions for clean teardown (Requirement 10.4).
 *
 * A single deterministic stack instance is used (fixed account/region) so the
 * synthesized template — and therefore the snapshot — is stable across runs.
 */

/** Fixed environment so the synthesized template is deterministic. */
const TEST_ENV = { account: '123456789012', region: 'us-east-1' } as const;

function synth(): Template {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'CdkInfraTestStack', {
    environment: 'dev',
    env: { ...TEST_ENV },
  });
  return Template.fromStack(stack);
}

/**
 * Walks every IAM policy statement in the template — both standalone
 * `AWS::IAM::Policy` resources and inline `Policies` on `AWS::IAM::Role`
 * resources — and returns the list of statements whose `Resource` is (or
 * contains) the bare "*" wildcard.
 *
 * Partial-wildcard ARNs (e.g. `arn:aws:bedrock:...:foundation-model/*`) are the
 * accepted least-privilege pattern and are intentionally NOT flagged: only the
 * exact bare `"*"` value is a Requirement 10.3 violation.
 */
function findBareWildcardResources(template: Template): string[] {
  const offenders: string[] = [];

  const scanStatements = (statements: any, where: string): void => {
    if (!Array.isArray(statements)) {
      return;
    }
    for (const statement of statements) {
      const resource = statement?.Resource;
      const resources = Array.isArray(resource) ? resource : [resource];
      if (!resources.includes('*')) {
        continue;
      }
      // Permitted exception: ecr:GetAuthorizationToken has no resource-level
      // permissions in IAM, so it must use "*". It is added by the AgentCore
      // runtime roles to pull their container images from ECR. Every other
      // action must be scoped to concrete ARNs (Requirement 10.3).
      const actions = Array.isArray(statement?.Action)
        ? statement.Action
        : [statement?.Action];
      const isEcrAuthTokenOnly =
        actions.length > 0 &&
        actions.every((a: string) => a === 'ecr:GetAuthorizationToken');
      if (isEcrAuthTokenOnly) {
        continue;
      }
      const action = JSON.stringify(statement?.Action);
      offenders.push(`${where} (Action: ${action})`);
    }
  };

  // Standalone managed policies.
  const managedPolicies = template.findResources('AWS::IAM::Policy');
  for (const [logicalId, policy] of Object.entries<any>(managedPolicies)) {
    scanStatements(
      policy.Properties?.PolicyDocument?.Statement,
      `AWS::IAM::Policy ${logicalId}`,
    );
  }

  // Inline policies attached directly to roles.
  const roles = template.findResources('AWS::IAM::Role');
  for (const [logicalId, role] of Object.entries<any>(roles)) {
    for (const inline of role.Properties?.Policies ?? []) {
      scanStatements(
        inline.PolicyDocument?.Statement,
        `AWS::IAM::Role ${logicalId} inline policy "${inline.PolicyName}"`,
      );
    }
  }

  return offenders;
}

describe('CaePlatformStack CDK infrastructure tests (task 15.2)', () => {
  describe('cdk synth snapshot', () => {
    test('synthesized template matches the committed snapshot', () => {
      // Locks the full `cdk synth` output so any unintended template drift is
      // surfaced in review. Regenerate intentionally with `jest -u` when a
      // stack change is expected.
      const template = synth();
      expect(template.toJSON()).toMatchSnapshot();
    });
  });

  describe('resource-count completeness (Requirement 10.1)', () => {
    const template = synth();

    // Actual synthesized counts (verified against `cdk synth` output).
    const EXPECTED_COUNTS: Record<string, number> = {
      // Auth.
      'AWS::Cognito::UserPool': 1,
      'AWS::Cognito::UserPoolClient': 1,
      // Storage + CDN: drawings / frontend / models buckets + one CloudFront.
      'AWS::S3::Bucket': 3,
      'AWS::CloudFront::Distribution': 1,
      // Session + connection tracking + optimization history tables.
      'AWS::DynamoDB::Table': 3,
      // HTTP upload API + WebSocket API.
      'AWS::ApiGatewayV2::Api': 2,
      // AgentCore Gateway + 7 Lambda tool targets.
      'AWS::BedrockAgentCore::Gateway': 1,
      'AWS::BedrockAgentCore::GatewayTarget': 7,
      // AgentCore Memory.
      'AWS::BedrockAgentCore::Memory': 1,
      // 4 AgentCore Runtimes. The DEFAULT endpoint is auto-created by the
      // service, so no explicit RuntimeEndpoint resource is declared.
      'AWS::BedrockAgentCore::Runtime': 4,
      'AWS::BedrockAgentCore::RuntimeEndpoint': 0,
      // AgentCore Identity: workload identity + OAuth2 credential provider.
      'AWS::BedrockAgentCore::WorkloadIdentity': 1,
      'AWS::BedrockAgentCore::OAuth2CredentialProvider': 1,
      // Knowledge Base: Neptune Analytics graph + Bedrock KB + S3 data source.
      'AWS::NeptuneGraph::Graph': 1,
      'AWS::Bedrock::KnowledgeBase': 1,
      'AWS::Bedrock::DataSource': 1,
      // Lambda functions: upload (1) + sessions (1) + 7 gateway tool targets
      // + 4 WebSocket handlers (connect/disconnect/message/authorizer) + 1
      // CDK-generated S3 auto-delete-objects custom-resource provider = 14.
      'AWS::Lambda::Function': 14,
    };

    for (const [resourceType, count] of Object.entries(EXPECTED_COUNTS)) {
      test(`synthesizes exactly ${count} ${resourceType}`, () => {
        template.resourceCountIs(resourceType, count);
      });
    }

    test('splits the two ApiGatewayV2 APIs into one HTTP and one WEBSOCKET', () => {
      template.resourcePropertiesCountIs('AWS::ApiGatewayV2::Api', { ProtocolType: 'HTTP' }, 1);
      template.resourcePropertiesCountIs(
        'AWS::ApiGatewayV2::Api',
        { ProtocolType: 'WEBSOCKET' },
        1,
      );
    });
  });

  describe('DynamoDB billing mode + removal policy (Requirements 10.4, 13.1)', () => {
    const template = synth();

    test('all tables use PAY_PER_REQUEST billing', () => {
      template.resourcePropertiesCountIs(
        'AWS::DynamoDB::Table',
        { BillingMode: 'PAY_PER_REQUEST' },
        3,
      );
    });

    test('all tables destroy cleanly (DeletionPolicy + UpdateReplacePolicy = Delete)', () => {
      // RemovalPolicy.DESTROY manifests as Delete on both CloudFormation
      // policy attributes so `cdk destroy` leaves zero tables behind.
      const tables = template.findResources('AWS::DynamoDB::Table');
      const tableEntries = Object.entries<any>(tables);
      expect(tableEntries.length).toBe(3);
      for (const [logicalId, table] of tableEntries) {
        expect(table.DeletionPolicy).toBe('Delete');
        expect(table.UpdateReplacePolicy).toBe('Delete');
        expect(table.Properties.BillingMode).toBe('PAY_PER_REQUEST');
        // Sanity: PAY_PER_REQUEST tables must not declare provisioned
        // throughput.
        expect(table.Properties.ProvisionedThroughput).toBeUndefined();
        void logicalId;
      }
    });

    test('the connections table exposes the userId-index GSI', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        GlobalSecondaryIndexes: [
          {
            IndexName: 'userId-index',
            KeySchema: [{ AttributeName: 'userId', KeyType: 'HASH' }],
          },
        ],
      });
    });
  });

  describe('least-privilege IAM — no wildcard resource ARNs (Requirement 10.3)', () => {
    const template = synth();

    test('no IAM policy or inline role policy statement uses a bare "*" resource', () => {
      const offenders = findBareWildcardResources(template);
      // A non-empty list is a real Requirement 10.3 defect: name the offending
      // policy/statement(s) so the regression is easy to locate.
      expect(offenders).toEqual([]);
    });

    test('the template does contain IAM policies to scan (guards the scan itself)', () => {
      // Guards against the wildcard scan passing vacuously if policies stop
      // being emitted. The stack synthesizes 19 managed IAM policies.
      const managedPolicies = template.findResources('AWS::IAM::Policy');
      expect(Object.keys(managedPolicies).length).toBeGreaterThanOrEqual(1);
      template.resourceCountIs('AWS::IAM::Policy', 19);
      template.resourceCountIs('AWS::IAM::Role', 21);
    });
  });

  describe('S3 clean teardown (Requirement 10.4)', () => {
    const template = synth();

    test('every bucket has DeletionPolicy + UpdateReplacePolicy = Delete', () => {
      const buckets = template.findResources('AWS::S3::Bucket');
      const bucketEntries = Object.entries<any>(buckets);
      expect(bucketEntries.length).toBe(3);
      for (const [, bucket] of bucketEntries) {
        expect(bucket.DeletionPolicy).toBe('Delete');
        expect(bucket.UpdateReplacePolicy).toBe('Delete');
      }
    });

    test('registers an S3 auto-delete-objects custom resource per bucket', () => {
      // `autoDeleteObjects: true` empties each bucket on delete so
      // `cdk destroy` can remove the (non-empty) buckets cleanly.
      template.resourceCountIs('Custom::S3AutoDeleteObjects', 3);
    });
  });
});
