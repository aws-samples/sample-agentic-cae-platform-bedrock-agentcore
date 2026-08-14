import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { CaePlatformStack } from '../lib/cae-platform-stack';

describe('CaePlatformStack skeleton', () => {
  test('synthesizes successfully with a dev environment', () => {
    const app = new cdk.App();
    const stack = new CaePlatformStack(app, 'TestStack', { environment: 'dev' });

    const template = Template.fromStack(stack);

    // Cognito auth resources exist as of task 2.1.
    template.resourceCountIs('AWS::Cognito::UserPool', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
  });

  test('exposes the configured environment on the stack instance', () => {
    const app = new cdk.App();
    const stack = new CaePlatformStack(app, 'TestStack', { environment: 'staging' });

    expect(stack.platformEnvironment).toBe('staging');
  });

  test('accepts explicit account/region via StackProps', () => {
    const app = new cdk.App();
    const stack = new CaePlatformStack(app, 'TestStack', {
      environment: 'prod',
      env: { account: '123456789012', region: 'us-west-2' },
    });

    expect(stack.account).toBe('123456789012');
    expect(stack.region).toBe('us-west-2');
  });

  test('exports the Cognito User Pool ID and App Client ID as stack outputs', () => {
    const app = new cdk.App();
    const stack = new CaePlatformStack(app, 'TestStack', { environment: 'dev' });

    const template = Template.fromStack(stack);
    // Requirement 9: connection parameters surfaced for the frontend/backends.
    template.hasOutput('CognitoUserPoolId', {});
    template.hasOutput('CognitoAppClientId', {});
  });
});

describe('CaePlatformStack Cognito auth (task 2.1)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'AuthStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('enforces the required password policy (Requirement 9.4)', () => {
    // 8-char minimum with all four character classes required. Cognito does
    // not support a configurable maximum length (hard platform cap of 256).
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: {
          MinimumLength: 8,
          RequireUppercase: true,
          RequireLowercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
        },
      },
    });
  });

  test('configures email OTP (code) verification (Requirement 9.4)', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AutoVerifiedAttributes: ['email'],
      VerificationMessageTemplate: {
        DefaultEmailOption: 'CONFIRM_WITH_CODE',
      },
    });
  });

  test('issues 60-minute access tokens and 30-day refresh tokens (Requirement 9.2)', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AccessTokenValidity: 60,
      // 30 days expressed in minutes: 30 * 24 * 60 = 43200.
      RefreshTokenValidity: 43200,
      TokenValidityUnits: {
        AccessToken: 'minutes',
        RefreshToken: 'minutes',
      },
    });
  });
});

describe('CaePlatformStack Drawing Ingestion upload service (task 4.2)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'UploadStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates the Python upload Lambda pointing at the tools/upload handler', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'python3.13',
      Handler: 'handler.handler',
    });
  });

  test('reserves concurrency to support >=10 concurrent uploads (Requirement 1.4)', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.handler',
      // Reserved concurrency floor gives >=3x the 10-upload requirement.
      ReservedConcurrentExecutions: 30,
    });
  });

  test('injects the drawings bucket and sessions table as env vars', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.handler',
      Environment: {
        Variables: {
          DRAWINGS_BUCKET: { Ref: Match.anyValue() },
          SESSIONS_TABLE: { Ref: Match.anyValue() },
        },
      },
    });
  });

  test('creates an HTTP API with POST /api/upload and /api/upload/confirm routes', () => {
    // Exactly one HTTP-protocol API (the WebSocket API added in task 10.1 is
    // a separate WEBSOCKET-protocol API).
    template.resourcePropertiesCountIs('AWS::ApiGatewayV2::Api', { ProtocolType: 'HTTP' }, 1);
    template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
      RouteKey: 'POST /api/upload',
    });
    template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
      RouteKey: 'POST /api/upload/confirm',
    });
  });

  test('configures a Cognito JWT authorizer on the HTTP API (Requirement 9)', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'JWT',
    });
    // Both upload routes must require JWT authorization. Scope to the HTTP
    // upload routes so the separate WebSocket routes (task 10.1) are excluded.
    const routes = template.findResources('AWS::ApiGatewayV2::Route');
    const uploadRoutes = Object.values(routes).filter((route: any) =>
      String(route.Properties.RouteKey).startsWith('POST /api/upload'),
    );
    expect(uploadRoutes.length).toBe(2);
    for (const route of uploadRoutes) {
      expect(route.Properties.AuthorizationType).toBe('JWT');
    }
  });

  test('grants least-privilege access with no wildcard resource ARNs (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const uploadPolicy = Object.values(policies).find((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('s3:PutObject'),
    ) as any;
    expect(uploadPolicy).toBeDefined();
    for (const statement of uploadPolicy.Properties.PolicyDocument.Statement) {
      // No statement may use a bare "*" wildcard resource ARN.
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      expect(resources).not.toContain('*');
    }
  });

  test('exports the upload HTTP API endpoint as a stack output (Requirement 10.2)', () => {
    template.hasOutput('UploadApiEndpoint', {});
  });
});

describe('CaePlatformStack AgentCore Memory (task 7.1)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'MemoryStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates cae-platform-memory with 90-day event expiry (Requirements 13.1, 13.5)', () => {
    template.resourceCountIs('AWS::BedrockAgentCore::Memory', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::Memory', {
      Name: 'cae_platform_memory',
      EventExpiryDuration: 90,
    });
  });

  test('configures semantic + user-preference strategies (Requirements 4.4, 5.5)', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Memory', {
      MemoryStrategies: Match.arrayWith([
        Match.objectLike({
          SemanticMemoryStrategy: Match.objectLike({ Name: 'drawing_entities' }),
        }),
        Match.objectLike({
          UserPreferenceMemoryStrategy: Match.objectLike({
            Name: 'optimization_preferences',
          }),
        }),
      ]),
    });
  });

  test('creates a memory execution role assumed by the AgentCore service', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
          }),
        ]),
      },
    });
  });

  test('grants InvokeModel with no bare wildcard resource ARN (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const memoryPolicy = Object.values(policies).find((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('bedrock:InvokeModel'),
    ) as any;
    expect(memoryPolicy).toBeDefined();
    for (const statement of memoryPolicy.Properties.PolicyDocument.Statement) {
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      expect(resources).not.toContain('*');
    }
  });

  test('exports the memory id and ARN as stack outputs (Requirement 10.2)', () => {
    template.hasOutput('AgentCoreMemoryId', {});
    template.hasOutput('AgentCoreMemoryArn', {});
  });
});

describe('CaePlatformStack AgentCore Gateway (task 6.1)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'GatewayStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates cae-platform-gateway with MCP protocol + semantic search (Requirement 7.1)', () => {
    template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      Name: 'cae-platform-gateway',
      ProtocolType: 'MCP',
      ProtocolConfiguration: {
        Mcp: Match.objectLike({
          SearchType: 'SEMANTIC',
          Instructions: Match.stringLikeRegexp('[Ee]ngineering drawing analysis'),
        }),
      },
    });
  });

  test('configures the AWS_IAM authorizer (Requirement 7.1)', () => {
    template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      AuthorizerType: 'AWS_IAM',
    });
  });

  test('creates a gateway execution role assumed by the AgentCore service', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
          }),
        ]),
      },
    });
  });

  test('grants InvokeFunction with no bare wildcard resource ARN (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const gatewayPolicy = Object.values(policies).find((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('lambda:InvokeFunction'),
    ) as any;
    expect(gatewayPolicy).toBeDefined();
    for (const statement of gatewayPolicy.Properties.PolicyDocument.Statement) {
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      expect(resources).not.toContain('*');
    }
  });

  test('exports the gateway id and URL as stack outputs (Requirement 10.2)', () => {
    template.hasOutput('AgentCoreGatewayId', {});
    template.hasOutput('AgentCoreGatewayUrl', {});
  });
});

describe('CaePlatformStack Neptune Analytics + Knowledge Base (task 11.1)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'KnowledgeBaseStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates a Neptune Analytics graph with a 1024-dim vector index (Requirement 10.1)', () => {
    template.resourceCountIs('AWS::NeptuneGraph::Graph', 1);
    template.hasResourceProperties('AWS::NeptuneGraph::Graph', {
      VectorSearchConfiguration: { VectorSearchDimension: 1024 },
      // Destroyable for clean teardown (Requirement 10.4).
      DeletionProtection: false,
    });
  });

  test('creates a GraphRAG Knowledge Base backed by Neptune Analytics (Requirements 4.1, 10.1)', () => {
    template.resourceCountIs('AWS::Bedrock::KnowledgeBase', 1);
    // Selecting the NEPTUNE_ANALYTICS store is what makes this GraphRAG.
    template.hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      Name: 'cae-platform-kb-dev',
      KnowledgeBaseConfiguration: Match.objectLike({
        Type: 'VECTOR',
        VectorKnowledgeBaseConfiguration: Match.objectLike({
          EmbeddingModelConfiguration: {
            BedrockEmbeddingModelConfiguration: { Dimensions: 1024 },
          },
        }),
      }),
      StorageConfiguration: Match.objectLike({
        Type: 'NEPTUNE_ANALYTICS',
        NeptuneAnalyticsConfiguration: Match.objectLike({
          FieldMapping: { TextField: 'text', MetadataField: 'metadata' },
        }),
      }),
    });
  });

  test('creates an S3 data source over the drawings bucket with the FM Parser (Requirement 2.4)', () => {
    template.resourceCountIs('AWS::Bedrock::DataSource', 1);
    template.hasResourceProperties('AWS::Bedrock::DataSource', {
      DataDeletionPolicy: 'DELETE',
      DataSourceConfiguration: Match.objectLike({
        Type: 'S3',
        S3Configuration: Match.objectLike({
          InclusionPrefixes: ['ground-truth/'],
        }),
      }),
      VectorIngestionConfiguration: Match.objectLike({
        ParsingConfiguration: Match.objectLike({
          ParsingStrategy: 'BEDROCK_FOUNDATION_MODEL',
        }),
        // Neptune Analytics (GraphRAG) KBs require context enrichment so the
        // ingestion pipeline extracts entities from each chunk into the graph.
        ContextEnrichmentConfiguration: Match.objectLike({
          Type: 'BEDROCK_FOUNDATION_MODEL',
          BedrockFoundationModelConfiguration: Match.objectLike({
            EnrichmentStrategyConfiguration: {
              Method: 'CHUNK_ENTITY_EXTRACTION',
            },
          }),
        }),
      }),
    });
  });

  test('creates a KB service role assumed by the Bedrock service', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: { Service: 'bedrock.amazonaws.com' },
          }),
        ]),
      },
    });
  });

  test('grants KB access with no bare wildcard resource ARNs (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const kbPolicy = Object.values(policies).find((p: any) =>
      JSON.stringify(p.Properties.PolicyDocument).includes('neptune-graph:ReadDataViaQuery'),
    ) as any;
    expect(kbPolicy).toBeDefined();
    for (const statement of kbPolicy.Properties.PolicyDocument.Statement) {
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      expect(resources).not.toContain('*');
    }
  });

  test('exports the Neptune graph id and Knowledge Base id as stack outputs (Requirement 10.2)', () => {
    template.hasOutput('NeptuneGraphId', {});
    template.hasOutput('KnowledgeBaseId', {});
  });
});

describe('CaePlatformStack AgentCore Runtimes + Identity (task 8.8)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'AgentRuntimeStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  const RUNTIME_NAMES = [
    'cae_platform_orchestrator',
    'cae_platform_extraction',
    'cae_platform_qa',
    'cae_platform_optimizer',
  ];

  test('registers all four AgentCore Runtimes (Requirement 10.1)', () => {
    template.resourceCountIs('AWS::BedrockAgentCore::Runtime', 4);
    // The DEFAULT endpoint is auto-created by the AgentCore service when the
    // runtime is created, so no explicit CfnRuntimeEndpoint is declared
    // (declaring a DEFAULT endpoint races the service -> 409 AlreadyExists).
    template.resourceCountIs('AWS::BedrockAgentCore::RuntimeEndpoint', 0);

    for (const name of RUNTIME_NAMES) {
      template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
        AgentRuntimeName: name,
        // Container-image deploy (ECR) + HTTP protocol per agentcore.json.
        ProtocolConfiguration: 'HTTP',
        AgentRuntimeArtifact: Match.objectLike({
          ContainerConfiguration: Match.objectLike({
            ContainerUri: Match.anyValue(),
          }),
        }),
        NetworkConfiguration: { NetworkMode: 'PUBLIC' },
        // 900s idle / 28800s max lifetime from agentcore.json.
        LifecycleConfiguration: {
          IdleRuntimeSessionTimeout: 900,
          MaxLifetime: 28800,
        },
      });
    }
  });

  test('exposes a derived DEFAULT endpoint ARN output for every runtime', () => {
    // The DEFAULT endpoint is auto-created by the service; its ARN is derived
    // from the runtime ARN (<runtimeArn>/runtime-endpoint/DEFAULT) and surfaced
    // as a stack output rather than an explicit CfnRuntimeEndpoint resource.
    for (const idBase of ['Orchestrator', 'Extraction', 'Qa', 'Optimizer']) {
      template.hasOutput(`${idBase}RuntimeEndpointArn`, {
        Value: Match.objectLike({
          'Fn::Join': Match.arrayWith([
            Match.arrayWith([Match.stringLikeRegexp('runtime-endpoint/DEFAULT')]),
          ]),
        }),
      });
    }
  });

  test('gives each runtime a dedicated execution role assumed by the AgentCore service', () => {
    // Four per-runtime execution roles (Requirement 10.3, per-runtime scope).
    for (const key of ['orchestrator', 'extraction', 'qa', 'optimizer']) {
      template.hasResourceProperties('AWS::IAM::Role', {
        RoleName: `cae-platform-agent-${key}-dev`,
        AssumeRolePolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sts:AssumeRole',
              Principal: { Service: 'bedrock-agentcore.amazonaws.com' },
            }),
          ]),
        },
      });
    }
  });

  test('scopes runtime IAM policies to concrete ARNs with no bare wildcard resource (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // Every agent runtime role policy must invoke a model and the gateway.
    const agentPolicies = Object.values(policies).filter((p: any) => {
      const doc = JSON.stringify(p.Properties.PolicyDocument);
      return (
        doc.includes('bedrock:InvokeModel') &&
        doc.includes('bedrock-agentcore:InvokeGateway')
      );
    }) as any[];
    // orchestrator, extraction, qa, optimizer.
    expect(agentPolicies.length).toBe(4);
    for (const policy of agentPolicies) {
      for (const statement of policy.Properties.PolicyDocument.Statement) {
        const resources = Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource];
        const actions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        // The only permitted "*" resource is ecr:GetAuthorizationToken, whose
        // API does not support resource-level permissions (the runtime role
        // pulls its container image from ECR). Every other statement must be
        // scoped to concrete ARNs (Requirement 10.3).
        const isEcrAuthTokenOnly =
          actions.length > 0 &&
          actions.every((a: string) => a === 'ecr:GetAuthorizationToken');
        if (isEcrAuthTokenOnly) {
          continue;
        }
        expect(resources).not.toContain('*');
      }
    }
  });

  test('grants the orchestrator inter-agent invoke on the specialized runtimes only (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // The Orchestrator's own policy is attached to OrchestratorRuntimeRole;
    // scope to that specific policy rather than "any policy mentioning
    // InvokeAgentRuntime" since the UploadHandler also gets a (much smaller)
    // InvokeAgentRuntime grant to trigger extraction (Requirement 2.4).
    const invokePolicy = Object.entries(policies).find(
      ([logicalId, p]: [string, any]) =>
        logicalId.startsWith('OrchestratorRuntimeRoleDefaultPolicy') &&
        JSON.stringify(p.Properties.PolicyDocument).includes(
          'bedrock-agentcore:InvokeAgentRuntime',
        ),
    )?.[1] as any;
    expect(invokePolicy).toBeDefined();
    const invokeStmt = invokePolicy.Properties.PolicyDocument.Statement.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes(
        'bedrock-agentcore:InvokeAgentRuntime',
      ),
    );
    const resources = Array.isArray(invokeStmt.Resource)
      ? invokeStmt.Resource
      : [invokeStmt.Resource];
    expect(resources).not.toContain('*');
    // Three specialized runtime ARNs (+ their sub-resource wildcards) => 6.
    expect(resources.length).toBe(6);
  });

  test('grants the upload handler invoke on the orchestrator runtime only, to trigger extraction (Requirement 2.4)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const uploadPolicy = Object.entries(policies).find(
      ([logicalId, p]: [string, any]) =>
        logicalId.startsWith('UploadHandlerServiceRoleDefaultPolicy') &&
        JSON.stringify(p.Properties.PolicyDocument).includes(
          'bedrock-agentcore:InvokeAgentRuntime',
        ),
    )?.[1] as any;
    expect(uploadPolicy).toBeDefined();
    const invokeStmt = uploadPolicy.Properties.PolicyDocument.Statement.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes(
        'bedrock-agentcore:InvokeAgentRuntime',
      ),
    );
    const resources = Array.isArray(invokeStmt.Resource)
      ? invokeStmt.Resource
      : [invokeStmt.Resource];
    expect(resources).not.toContain('*');
    // Orchestrator runtime ARN + its sub-resource wildcard => 2.
    expect(resources.length).toBe(2);
  });

  test('sets up the cae-platform-agents workload identity (AgentCore Identity)', () => {
    template.resourceCountIs('AWS::BedrockAgentCore::WorkloadIdentity', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::WorkloadIdentity', {
      Name: 'cae-platform-agents',
    });
  });

  test('sets up the sagemaker-access OAuth2 credential provider (AgentCore Identity)', () => {
    template.resourceCountIs('AWS::BedrockAgentCore::OAuth2CredentialProvider', 1);
    template.hasResourceProperties('AWS::BedrockAgentCore::OAuth2CredentialProvider', {
      Name: 'sagemaker-access',
      CredentialProviderVendor: 'CustomOauth2',
    });
  });

  test('exports runtime ids/ARNs, endpoint ARNs, and identity ARNs as stack outputs (Requirement 10.2)', () => {
    template.hasOutput('OrchestratorRuntimeId', {});
    template.hasOutput('OrchestratorRuntimeArn', {});
    template.hasOutput('OrchestratorRuntimeEndpointArn', {});
    template.hasOutput('ExtractionRuntimeId', {});
    template.hasOutput('QaRuntimeArn', {});
    template.hasOutput('OptimizerRuntimeEndpointArn', {});
    template.hasOutput('AgentWorkloadIdentityArn', {});
    template.hasOutput('SagemakerCredentialProviderArn', {});
  });
});

describe('CaePlatformStack WebSocket Gateway (task 10.1)', () => {
  const app = new cdk.App();
  const stack = new CaePlatformStack(app, 'WebSocketStack', { environment: 'dev' });
  const template = Template.fromStack(stack);

  test('creates a WebSocket API with the $request.body.action route selection (Requirement 12.1)', () => {
    template.resourcePropertiesCountIs('AWS::ApiGatewayV2::Api', { ProtocolType: 'WEBSOCKET' }, 1);
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      ProtocolType: 'WEBSOCKET',
      RouteSelectionExpression: '$request.body.action',
    });
  });

  test('defines the $connect, $disconnect, $default, and sendmessage routes', () => {
    for (const routeKey of ['$connect', '$disconnect', '$default', 'sendmessage']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
  });

  test('protects the $connect route with a Lambda (Cognito JWT) authorizer (Requirement 12.6)', () => {
    // A REQUEST Lambda authorizer is the supported way to enforce Cognito auth
    // on a WebSocket $connect (no native Cognito authorizer for WebSocket APIs).
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'REQUEST',
      IdentitySource: ['route.request.querystring.token'],
    });
    // The $connect route must reference an authorizer and use CUSTOM auth.
    const routes = template.findResources('AWS::ApiGatewayV2::Route');
    const connectRoute = Object.values(routes).find(
      (r: any) => r.Properties.RouteKey === '$connect',
    ) as any;
    expect(connectRoute).toBeDefined();
    expect(connectRoute.Properties.AuthorizationType).toBe('CUSTOM');
    expect(connectRoute.Properties.AuthorizerId).toBeDefined();
  });

  test('creates an auto-deploying WebSocket stage', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      StageName: 'dev',
      AutoDeploy: true,
    });
  });

  test('creates connect/disconnect/message/authorizer Python Lambdas', () => {
    for (const handler of [
      'handler.connect_handler',
      'handler.disconnect_handler',
      'handler.message_handler',
      'handler.authorizer_handler',
    ]) {
      template.hasResourceProperties('AWS::Lambda::Function', {
        Runtime: 'python3.13',
        Handler: handler,
      });
    }
  });

  test('injects the connections table and runtime ARNs into the message router', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'handler.message_handler',
      Environment: {
        Variables: Match.objectLike({
          CONNECTIONS_TABLE: Match.anyValue(),
          ORCHESTRATOR_RUNTIME_ARN: Match.anyValue(),
          EXTRACTION_RUNTIME_ARN: Match.anyValue(),
          QA_RUNTIME_ARN: Match.anyValue(),
          OPTIMIZER_RUNTIME_ARN: Match.anyValue(),
        }),
      },
    });
  });

  test('scopes the message router IAM to concrete ARNs with no bare wildcard (Requirement 10.3)', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    // The message router policy is the one that both invokes agent runtimes
    // and manages WebSocket connections.
    const wsPolicy = Object.values(policies).find((p: any) => {
      const doc = JSON.stringify(p.Properties.PolicyDocument);
      return (
        doc.includes('bedrock-agentcore:InvokeAgentRuntime') &&
        doc.includes('execute-api:ManageConnections')
      );
    }) as any;
    expect(wsPolicy).toBeDefined();
    for (const statement of wsPolicy.Properties.PolicyDocument.Statement) {
      const resources = Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource];
      // No statement may use a bare "*" wildcard resource ARN.
      expect(resources).not.toContain('*');
    }
  });

  test('grants InvokeAgentRuntime on all four concrete runtime ARNs', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const wsPolicy = Object.values(policies).find((p: any) => {
      const doc = JSON.stringify(p.Properties.PolicyDocument);
      return (
        doc.includes('bedrock-agentcore:InvokeAgentRuntime') &&
        doc.includes('execute-api:ManageConnections')
      );
    }) as any;
    expect(wsPolicy).toBeDefined();
    const invokeStmt = wsPolicy.Properties.PolicyDocument.Statement.find((s: any) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).includes(
        'bedrock-agentcore:InvokeAgentRuntime',
      ),
    );
    const resources = Array.isArray(invokeStmt.Resource)
      ? invokeStmt.Resource
      : [invokeStmt.Resource];
    // Four runtime ARNs (+ their sub-resource wildcards) => 8 entries.
    expect(resources.length).toBe(8);
  });

  test('exports the WebSocket endpoint as a stack output (Requirement 10.2)', () => {
    template.hasOutput('WebSocketEndpoint', {});
  });
});
