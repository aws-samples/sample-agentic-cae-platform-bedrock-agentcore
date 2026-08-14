import * as cdk from 'aws-cdk-lib/core';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { Construct } from 'constructs';

/**
 * Environment tier for the platform deployment.
 */
export type PlatformEnvironment = 'dev' | 'staging' | 'prod';

export interface CaePlatformStackProps extends cdk.StackProps {
  environment: PlatformEnvironment;
}

/**
 * CAE Platform CDK Stack — Agentic CAE Platform AWS
 * resources: auth, storage, APIs, compute, AgentCore, and knowledge base.
 */
export class CaePlatformStack extends cdk.Stack {
  public readonly platformEnvironment: PlatformEnvironment;

  constructor(scope: Construct, id: string, props: CaePlatformStackProps) {
    super(scope, id, props);
    this.platformEnvironment = props.environment;

    // ──────────────────────────────────────────────────────────────────────
    // 1. COGNITO AUTH (task 2.1)
    // ──────────────────────────────────────────────────────────────────────
    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: true,
      autoVerify: { email: true },
      signInAliases: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Override the default VerificationMessageTemplate to use code-based verification
    const cfnUserPool = userPool.node.defaultChild as cognito.CfnUserPool;
    cfnUserPool.addPropertyOverride('VerificationMessageTemplate', {
      DefaultEmailOption: 'CONFIRM_WITH_CODE',
    });

    const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool,
      authFlows: { userSrp: true, userPassword: true },
      accessTokenValidity: cdk.Duration.minutes(60),
      refreshTokenValidity: cdk.Duration.minutes(43200), // 30 days
    });

    new cdk.CfnOutput(this, 'CognitoUserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'CognitoAppClientId', { value: userPoolClient.userPoolClientId });

    // ──────────────────────────────────────────────────────────────────────
    // 2. S3 BUCKETS
    // ──────────────────────────────────────────────────────────────────────
    const drawingsBucket = new s3.Bucket(this, 'DrawingsBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const frontendBucket = new s3.Bucket(this, 'FrontendBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const modelsBucket = new s3.Bucket(this, 'ModelsBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ──────────────────────────────────────────────────────────────────────
    // 3. DYNAMODB TABLES
    // ──────────────────────────────────────────────────────────────────────
    const sessionsTable = new dynamodb.Table(this, 'SessionsTable', {
      partitionKey: { name: 'sessionId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'expiresAt',
    });

    const connectionsTable = new dynamodb.Table(this, 'ConnectionsTable', {
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'expiresAt',
    });
    connectionsTable.addGlobalSecondaryIndex({
      indexName: 'userId-index',
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
    });

    const optimizationHistoryTable = new dynamodb.Table(this, 'OptimizationHistoryTable', {
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sessionId#timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'expiresAt',
    });

    // ──────────────────────────────────────────────────────────────────────
    // 4. CLOUDFRONT DISTRIBUTION
    // ──────────────────────────────────────────────────────────────────────
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(frontendBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      additionalBehaviors: {
        'models/*': {
          origin: origins.S3BucketOrigin.withOriginAccessControl(modelsBucket),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        },
      },
      defaultRootObject: 'index.html',
    });

    new cdk.CfnOutput(this, 'CloudFrontDomainName', { value: distribution.distributionDomainName });

    // ──────────────────────────────────────────────────────────────────────
    // 5. HTTP API — Upload & Sessions (task 4.2)
    // ──────────────────────────────────────────────────────────────────────
    const httpApi = new apigatewayv2.CfnApi(this, 'HttpApi', {
      name: `cae-platform-http-${props.environment}`,
      protocolType: 'HTTP',
    });

    const jwtAuthorizer = new apigatewayv2.CfnAuthorizer(this, 'JwtAuthorizer', {
      apiId: httpApi.ref,
      authorizerType: 'JWT',
      name: 'CognitoJwtAuthorizer',
      identitySource: ['$request.header.Authorization'],
      jwtConfiguration: {
        audience: [userPoolClient.userPoolClientId],
        issuer: `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`,
      },
    });

    // Upload Lambda
    const uploadHandler = new lambda.Function(this, 'UploadHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../tools/upload'),
      reservedConcurrentExecutions: 30,
      timeout: cdk.Duration.seconds(60),
      environment: {
        DRAWINGS_BUCKET: drawingsBucket.bucketName,
        SESSIONS_TABLE: sessionsTable.tableName,
      },
    });
    drawingsBucket.grantPut(uploadHandler);
    sessionsTable.grantReadWriteData(uploadHandler);

    // Sessions Lambda
    const sessionsHandler = new lambda.Function(this, 'SessionsHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../tools/sessions'),
      timeout: cdk.Duration.seconds(30),
      environment: {
        SESSIONS_TABLE: sessionsTable.tableName,
        OPTIMIZATION_HISTORY_TABLE: optimizationHistoryTable.tableName,
      },
    });
    sessionsTable.grantReadWriteData(sessionsHandler);
    optimizationHistoryTable.grantReadWriteData(sessionsHandler);

    // HTTP API integration & routes
    const uploadIntegration = new apigatewayv2.CfnIntegration(this, 'UploadIntegration', {
      apiId: httpApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: uploadHandler.functionArn,
      payloadFormatVersion: '2.0',
    });
    uploadHandler.addPermission('HttpApiInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${httpApi.ref}/*/*`,
    });

    new apigatewayv2.CfnRoute(this, 'UploadRoute', {
      apiId: httpApi.ref,
      routeKey: 'POST /api/upload',
      authorizationType: 'JWT',
      authorizerId: jwtAuthorizer.ref,
      target: `integrations/${uploadIntegration.ref}`,
    });

    new apigatewayv2.CfnRoute(this, 'UploadConfirmRoute', {
      apiId: httpApi.ref,
      routeKey: 'POST /api/upload/confirm',
      authorizationType: 'JWT',
      authorizerId: jwtAuthorizer.ref,
      target: `integrations/${uploadIntegration.ref}`,
    });

    const sessionsIntegration = new apigatewayv2.CfnIntegration(this, 'SessionsIntegration', {
      apiId: httpApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: sessionsHandler.functionArn,
      payloadFormatVersion: '2.0',
    });
    sessionsHandler.addPermission('HttpApiInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${httpApi.ref}/*/*`,
    });

    new apigatewayv2.CfnRoute(this, 'SessionsRoute', {
      apiId: httpApi.ref,
      routeKey: 'GET /api/sessions',
      authorizationType: 'JWT',
      authorizerId: jwtAuthorizer.ref,
      target: `integrations/${sessionsIntegration.ref}`,
    });

    const httpStage = new apigatewayv2.CfnStage(this, 'HttpApiStage', {
      apiId: httpApi.ref,
      stageName: '$default',
      autoDeploy: true,
    });

    new cdk.CfnOutput(this, 'UploadApiEndpoint', {
      value: `https://${httpApi.ref}.execute-api.${this.region}.amazonaws.com`,
    });

    // ──────────────────────────────────────────────────────────────────────
    // 6. AGENTCORE GATEWAY (task 6.1) — 7 MCP tool targets
    // ──────────────────────────────────────────────────────────────────────

    // Gateway tool target Lambdas
    const toolTargetNames = [
      { id: 'KbRetrieval', asset: '../tools/kb_retrieval', handler: 'handler.handler' },
      { id: 'VisionZoom', asset: '../tools/vision_zoom', handler: 'handler.handler' },
      { id: 'PdfAnnotations', asset: '../tools/pdf_annotations', handler: 'handler.handler' },
      { id: 'Compliance', asset: '../tools/compliance', handler: 'handler.handler' },
      { id: 'Optimizer', asset: '../tools/optimizer', handler: 'handler.handler' },
      { id: 'CadqueryGen', asset: '../tools/cadquery_gen', handler: 'handler.handler' },
      { id: 'ConstraintValidator', asset: '../tools/constraint_validator', handler: 'handler.handler' },
    ];

    const toolTargetLambdas: lambda.Function[] = [];
    for (const target of toolTargetNames) {
      const fn = new lambda.Function(this, `${target.id}ToolTarget`, {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: target.handler,
        code: lambda.Code.fromAsset(target.asset),
        timeout: cdk.Duration.seconds(120),
      });
      toolTargetLambdas.push(fn);
    }

    // Grant tool target Lambdas their required permissions
    // KbRetrieval: Bedrock KB retrieve + S3 read
    const kbRetrievalFn = toolTargetLambdas[0];
    drawingsBucket.grantRead(kbRetrievalFn);

    // VisionZoom: S3 read for drawings
    const visionZoomFn = toolTargetLambdas[1];
    drawingsBucket.grantRead(visionZoomFn);

    // PdfAnnotations: S3 read for drawings
    const pdfAnnotationsFn = toolTargetLambdas[2];
    drawingsBucket.grantRead(pdfAnnotationsFn);

    // Compliance: S3 read for drawings + Bedrock invoke
    const complianceFn = toolTargetLambdas[3];
    drawingsBucket.grantRead(complianceFn);

    // Optimizer: DynamoDB write for optimization results
    const optimizerToolFn = toolTargetLambdas[4];
    optimizationHistoryTable.grantWriteData(optimizerToolFn);

    // CadqueryGen: S3 write for models
    const cadqueryGenFn = toolTargetLambdas[5];
    modelsBucket.grantReadWrite(cadqueryGenFn);

    // ConstraintValidator: S3 read for drawings
    const constraintValidatorFn = toolTargetLambdas[6];
    drawingsBucket.grantRead(constraintValidatorFn);

    // Gateway execution role
    const gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    });
    const gatewayPolicy = new iam.Policy(this, 'GatewayExecutionPolicy', {
      statements: [
        new iam.PolicyStatement({
          actions: ['lambda:InvokeFunction'],
          resources: toolTargetLambdas.map(fn => fn.functionArn),
        }),
      ],
    });
    gatewayRole.attachInlinePolicy(gatewayPolicy);

    // AgentCore Gateway (L1 CFN construct)
    const gateway = new cdk.CfnResource(this, 'AgentCoreGateway', {
      type: 'AWS::BedrockAgentCore::Gateway',
      properties: {
        Name: 'cae-platform-gateway',
        ProtocolType: 'MCP',
        AuthorizerType: 'AWS_IAM',
        RoleArn: gatewayRole.roleArn,
        ProtocolConfiguration: {
          Mcp: {
            SearchType: 'SEMANTIC',
            Instructions: 'Engineering drawing analysis and design optimization platform',
          },
        },
      },
    });

    // 7 Gateway Targets
    const toolNames = ['retrieve', 'zoom_and_read', 'annotations', 'compliance', 'optimize', 'generate_3d', 'validate_constraints'];
    for (let i = 0; i < toolTargetLambdas.length; i++) {
      new cdk.CfnResource(this, `GatewayTarget${toolTargetNames[i].id}`, {
        type: 'AWS::BedrockAgentCore::GatewayTarget',
        properties: {
          GatewayIdentifier: gateway.getAtt('GatewayId'),
          Name: toolNames[i],
          TargetConfiguration: {
            LambdaTargetConfiguration: {
              LambdaArn: toolTargetLambdas[i].functionArn,
            },
          },
        },
      });
    }

    new cdk.CfnOutput(this, 'AgentCoreGatewayId', { value: gateway.getAtt('GatewayId').toString() });
    new cdk.CfnOutput(this, 'AgentCoreGatewayUrl', { value: gateway.getAtt('GatewayUrl').toString() });

    // ──────────────────────────────────────────────────────────────────────
    // 7. AGENTCORE MEMORY (task 7.1)
    // ──────────────────────────────────────────────────────────────────────
    const memoryRole = new iam.Role(this, 'MemoryExecutionRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    });
    const memoryPolicy = new iam.Policy(this, 'MemoryExecutionPolicy', {
      statements: [
        new iam.PolicyStatement({
          actions: ['bedrock:InvokeModel'],
          resources: [
            `arn:aws:bedrock:${this.region}::foundation-model/*`,
          ],
        }),
      ],
    });
    memoryRole.attachInlinePolicy(memoryPolicy);

    const memory = new cdk.CfnResource(this, 'AgentCoreMemory', {
      type: 'AWS::BedrockAgentCore::Memory',
      properties: {
        Name: 'cae_platform_memory',
        EventExpiryDuration: 90,
        RoleArn: memoryRole.roleArn,
        MemoryStrategies: [
          {
            SemanticMemoryStrategy: {
              Name: 'drawing_entities',
              Description: 'Semantic index of extracted drawing entities and specifications',
              Namespace: 'drawings',
              Model: `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
              ContentType: 'TEXT',
            },
          },
          {
            UserPreferenceMemoryStrategy: {
              Name: 'optimization_preferences',
              Description: 'User optimization preferences and constraints',
              Namespace: 'preferences',
            },
          },
        ],
      },
    });

    new cdk.CfnOutput(this, 'AgentCoreMemoryId', { value: memory.getAtt('MemoryId').toString() });
    new cdk.CfnOutput(this, 'AgentCoreMemoryArn', { value: memory.getAtt('Arn').toString() });

    // ──────────────────────────────────────────────────────────────────────
    // 8. AGENTCORE RUNTIMES + IDENTITY (task 8.8)
    // ──────────────────────────────────────────────────────────────────────
    const runtimeConfigs = [
      { id: 'Orchestrator', name: 'cae_platform_orchestrator', key: 'orchestrator' },
      { id: 'Extraction', name: 'cae_platform_extraction', key: 'extraction' },
      { id: 'Qa', name: 'cae_platform_qa', key: 'qa' },
      { id: 'Optimizer', name: 'cae_platform_optimizer', key: 'optimizer' },
    ];

    const runtimeResources: cdk.CfnResource[] = [];
    const runtimeRoles: iam.Role[] = [];

    for (const config of runtimeConfigs) {
      const runtimeRole = new iam.Role(this, `${config.id}RuntimeRole`, {
        roleName: `cae-platform-agent-${config.key}-${props.environment}`,
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      });
      runtimeRoles.push(runtimeRole);

      const runtime = new cdk.CfnResource(this, `${config.id}Runtime`, {
        type: 'AWS::BedrockAgentCore::Runtime',
        properties: {
          AgentRuntimeName: config.name,
          ProtocolConfiguration: 'HTTP',
          RoleArn: runtimeRole.roleArn,
          AgentRuntimeArtifact: {
            ContainerConfiguration: {
              ContainerUri: `${this.account}.dkr.ecr.${this.region}.amazonaws.com/cae-platform-${config.key}:latest`,
            },
          },
          NetworkConfiguration: { NetworkMode: 'PUBLIC' },
          LifecycleConfiguration: {
            IdleRuntimeSessionTimeout: 900,
            MaxLifetime: 28800,
          },
        },
      });
      runtimeResources.push(runtime);

      new cdk.CfnOutput(this, `${config.id}RuntimeId`, {
        value: runtime.getAtt('AgentRuntimeId').toString(),
      });
      new cdk.CfnOutput(this, `${config.id}RuntimeArn`, {
        value: runtime.getAtt('AgentRuntimeArn').toString(),
      });
      // The DEFAULT endpoint is auto-created by the AgentCore service.
      new cdk.CfnOutput(this, `${config.id}RuntimeEndpointArn`, {
        value: cdk.Fn.join('', [runtime.getAtt('AgentRuntimeArn').toString(), '/runtime-endpoint/DEFAULT']),
      });
    }

    // Per-runtime IAM policies
    const [orchestratorRole, extractionRole, qaRole, optimizerRole] = runtimeRoles;
    const [orchestratorRuntime, extractionRuntime, qaRuntime, optimizerRuntime] = runtimeResources;

    // Base policy for all runtimes: InvokeModel, InvokeGateway, ECR auth
    for (let i = 0; i < runtimeRoles.length; i++) {
      const role = runtimeRoles[i];
      const rt = runtimeResources[i];

      new iam.Policy(this, `${runtimeConfigs[i].id}RuntimeRoleDefaultPolicy`, {
        roles: [role],
        statements: [
          new iam.PolicyStatement({
            actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
            resources: [`arn:aws:bedrock:${this.region}::foundation-model/*`],
          }),
          new iam.PolicyStatement({
            actions: ['bedrock-agentcore:InvokeGateway'],
            resources: [gateway.getAtt('Arn').toString()],
          }),
          new iam.PolicyStatement({
            actions: ['ecr:GetAuthorizationToken'],
            resources: ['*'],
          }),
          new iam.PolicyStatement({
            actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'],
            resources: [`arn:aws:ecr:${this.region}:${this.account}:repository/cae-platform-${runtimeConfigs[i].key}`],
          }),
          // Orchestrator needs inter-agent invoke; others get self-only
          ...(i === 0
            ? [
                new iam.PolicyStatement({
                  actions: ['bedrock-agentcore:InvokeAgentRuntime'],
                  resources: [
                    extractionRuntime.getAtt('AgentRuntimeArn').toString(),
                    cdk.Fn.join('', [extractionRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
                    qaRuntime.getAtt('AgentRuntimeArn').toString(),
                    cdk.Fn.join('', [qaRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
                    optimizerRuntime.getAtt('AgentRuntimeArn').toString(),
                    cdk.Fn.join('', [optimizerRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
                  ],
                }),
              ]
            : []),
        ],
      });
    }

    // Upload handler needs to invoke the orchestrator runtime for extraction
    uploadHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [
          orchestratorRuntime.getAtt('AgentRuntimeArn').toString(),
          cdk.Fn.join('', [orchestratorRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
        ],
      }),
    );

    // Workload Identity + OAuth2 Credential Provider (AgentCore Identity)
    const workloadIdentity = new cdk.CfnResource(this, 'WorkloadIdentity', {
      type: 'AWS::BedrockAgentCore::WorkloadIdentity',
      properties: {
        Name: 'cae-platform-agents',
      },
    });

    const credentialProvider = new cdk.CfnResource(this, 'OAuth2CredentialProvider', {
      type: 'AWS::BedrockAgentCore::OAuth2CredentialProvider',
      properties: {
        Name: 'sagemaker-access',
        WorkloadIdentityArn: workloadIdentity.getAtt('WorkloadIdentityArn'),
        CredentialProviderVendor: 'CustomOauth2',
        Oauth2ProviderConfigInput: {
          CustomOauth2ProviderConfig: {
            OauthDiscoveryUrl: `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}/.well-known/openid-configuration`,
          },
        },
      },
    });

    new cdk.CfnOutput(this, 'AgentWorkloadIdentityArn', {
      value: workloadIdentity.getAtt('WorkloadIdentityArn').toString(),
    });
    new cdk.CfnOutput(this, 'SagemakerCredentialProviderArn', {
      value: credentialProvider.getAtt('CredentialProviderArn').toString(),
    });

    // ──────────────────────────────────────────────────────────────────────
    // 9. NEPTUNE ANALYTICS + KNOWLEDGE BASE (task 11.1)
    // ──────────────────────────────────────────────────────────────────────
    const neptuneGraph = new cdk.CfnResource(this, 'NeptuneGraph', {
      type: 'AWS::NeptuneGraph::Graph',
      properties: {
        GraphName: `cae-platform-graph-${props.environment}`,
        ProvisionedMemory: 16,
        PublicConnectivity: false,
        ReplicaCount: 0,
        DeletionProtection: false,
        VectorSearchConfiguration: {
          VectorSearchDimension: 1024,
        },
      },
    });

    const kbRole = new iam.Role(this, 'KnowledgeBaseRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com'),
    });
    const kbPolicy = new iam.Policy(this, 'KnowledgeBasePolicy', {
      roles: [kbRole],
      statements: [
        new iam.PolicyStatement({
          actions: ['neptune-graph:ReadDataViaQuery', 'neptune-graph:WriteDataViaQuery', 'neptune-graph:DeleteDataViaQuery'],
          resources: [neptuneGraph.getAtt('GraphArn').toString()],
        }),
        new iam.PolicyStatement({
          actions: ['s3:GetObject', 's3:ListBucket'],
          resources: [drawingsBucket.bucketArn, `${drawingsBucket.bucketArn}/*`],
        }),
        new iam.PolicyStatement({
          actions: ['bedrock:InvokeModel'],
          resources: [`arn:aws:bedrock:${this.region}::foundation-model/*`],
        }),
      ],
    });

    const knowledgeBase = new cdk.CfnResource(this, 'KnowledgeBase', {
      type: 'AWS::Bedrock::KnowledgeBase',
      properties: {
        Name: `cae-platform-kb-${props.environment}`,
        RoleArn: kbRole.roleArn,
        KnowledgeBaseConfiguration: {
          Type: 'VECTOR',
          VectorKnowledgeBaseConfiguration: {
            EmbeddingModelArn: `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
            EmbeddingModelConfiguration: {
              BedrockEmbeddingModelConfiguration: {
                Dimensions: 1024,
              },
            },
          },
        },
        StorageConfiguration: {
          Type: 'NEPTUNE_ANALYTICS',
          NeptuneAnalyticsConfiguration: {
            GraphArn: neptuneGraph.getAtt('GraphArn').toString(),
            FieldMapping: {
              TextField: 'text',
              MetadataField: 'metadata',
            },
          },
        },
      },
    });
    knowledgeBase.addDependency(neptuneGraph as unknown as cdk.CfnResource);

    const dataSource = new cdk.CfnResource(this, 'KnowledgeBaseDataSource', {
      type: 'AWS::Bedrock::DataSource',
      properties: {
        KnowledgeBaseId: knowledgeBase.getAtt('KnowledgeBaseId'),
        Name: `cae-platform-drawings-ds-${props.environment}`,
        DataDeletionPolicy: 'DELETE',
        DataSourceConfiguration: {
          Type: 'S3',
          S3Configuration: {
            BucketArn: drawingsBucket.bucketArn,
            InclusionPrefixes: ['ground-truth/'],
          },
        },
        VectorIngestionConfiguration: {
          ParsingConfiguration: {
            ParsingStrategy: 'BEDROCK_FOUNDATION_MODEL',
            BedrockFoundationModelConfiguration: {
              ModelArn: `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0`,
            },
          },
          ContextEnrichmentConfiguration: {
            Type: 'BEDROCK_FOUNDATION_MODEL',
            BedrockFoundationModelConfiguration: {
              ModelArn: `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0`,
              EnrichmentStrategyConfiguration: {
                Method: 'CHUNK_ENTITY_EXTRACTION',
              },
            },
          },
        },
      },
    });

    new cdk.CfnOutput(this, 'NeptuneGraphId', { value: neptuneGraph.getAtt('GraphId').toString() });
    new cdk.CfnOutput(this, 'KnowledgeBaseId', { value: knowledgeBase.getAtt('KnowledgeBaseId').toString() });

    // ──────────────────────────────────────────────────────────────────────
    // 10. WEBSOCKET API (task 10.1)
    // ──────────────────────────────────────────────────────────────────────
    const wsApi = new apigatewayv2.CfnApi(this, 'WebSocketApi', {
      name: `cae-platform-ws-${props.environment}`,
      protocolType: 'WEBSOCKET',
      routeSelectionExpression: '$request.body.action',
    });

    // WebSocket handler Lambdas
    const wsConnectHandler = new lambda.Function(this, 'WebSocketConnectHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.connect_handler',
      code: lambda.Code.fromAsset('../tools/websocket'),
      timeout: cdk.Duration.seconds(10),
      environment: {
        CONNECTIONS_TABLE: connectionsTable.tableName,
      },
    });
    connectionsTable.grantReadWriteData(wsConnectHandler);

    const wsDisconnectHandler = new lambda.Function(this, 'WebSocketDisconnectHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.disconnect_handler',
      code: lambda.Code.fromAsset('../tools/websocket'),
      timeout: cdk.Duration.seconds(10),
      environment: {
        CONNECTIONS_TABLE: connectionsTable.tableName,
      },
    });
    connectionsTable.grantReadWriteData(wsDisconnectHandler);

    const wsMessageHandler = new lambda.Function(this, 'WebSocketMessageHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.message_handler',
      code: lambda.Code.fromAsset('../tools/websocket'),
      timeout: cdk.Duration.seconds(300),
      environment: {
        CONNECTIONS_TABLE: connectionsTable.tableName,
        ORCHESTRATOR_RUNTIME_ARN: orchestratorRuntime.getAtt('AgentRuntimeArn').toString(),
        EXTRACTION_RUNTIME_ARN: extractionRuntime.getAtt('AgentRuntimeArn').toString(),
        QA_RUNTIME_ARN: qaRuntime.getAtt('AgentRuntimeArn').toString(),
        OPTIMIZER_RUNTIME_ARN: optimizerRuntime.getAtt('AgentRuntimeArn').toString(),
        OPTIMIZATION_HISTORY_TABLE: optimizationHistoryTable.tableName,
      },
    });
    connectionsTable.grantReadWriteData(wsMessageHandler);
    optimizationHistoryTable.grantWriteData(wsMessageHandler);

    // Grant WebSocket message handler the ability to invoke all runtimes
    // and manage connections
    wsMessageHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [
          orchestratorRuntime.getAtt('AgentRuntimeArn').toString(),
          cdk.Fn.join('', [orchestratorRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
          extractionRuntime.getAtt('AgentRuntimeArn').toString(),
          cdk.Fn.join('', [extractionRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
          qaRuntime.getAtt('AgentRuntimeArn').toString(),
          cdk.Fn.join('', [qaRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
          optimizerRuntime.getAtt('AgentRuntimeArn').toString(),
          cdk.Fn.join('', [optimizerRuntime.getAtt('AgentRuntimeArn').toString(), '/*']),
        ],
      }),
    );
    wsMessageHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['execute-api:ManageConnections'],
        resources: [
          `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/${props.environment}/POST/@connections/*`,
        ],
      }),
    );

    // WebSocket authorizer Lambda
    const wsAuthorizerHandler = new lambda.Function(this, 'WebSocketAuthorizerHandler', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.authorizer_handler',
      code: lambda.Code.fromAsset('../tools/websocket'),
      timeout: cdk.Duration.seconds(10),
      environment: {
        USER_POOL_ID: userPool.userPoolId,
        APP_CLIENT_ID: userPoolClient.userPoolClientId,
      },
    });

    const wsAuthorizer = new apigatewayv2.CfnAuthorizer(this, 'WebSocketAuthorizer', {
      apiId: wsApi.ref,
      authorizerType: 'REQUEST',
      name: 'CognitoWebSocketAuthorizer',
      identitySource: ['route.request.querystring.token'],
      authorizerUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${wsAuthorizerHandler.functionArn}/invocations`,
    });

    wsAuthorizerHandler.addPermission('WsApiAuthorizerInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*`,
    });

    // WebSocket integrations
    const wsConnectIntegration = new apigatewayv2.CfnIntegration(this, 'WsConnectIntegration', {
      apiId: wsApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${wsConnectHandler.functionArn}/invocations`,
    });
    wsConnectHandler.addPermission('WsApiConnectInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*`,
    });

    const wsDisconnectIntegration = new apigatewayv2.CfnIntegration(this, 'WsDisconnectIntegration', {
      apiId: wsApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${wsDisconnectHandler.functionArn}/invocations`,
    });
    wsDisconnectHandler.addPermission('WsApiDisconnectInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*`,
    });

    const wsMessageIntegration = new apigatewayv2.CfnIntegration(this, 'WsMessageIntegration', {
      apiId: wsApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${wsMessageHandler.functionArn}/invocations`,
    });
    wsMessageHandler.addPermission('WsApiMessageInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*`,
    });

    // WebSocket routes
    new apigatewayv2.CfnRoute(this, 'WsConnectRoute', {
      apiId: wsApi.ref,
      routeKey: '$connect',
      authorizationType: 'CUSTOM',
      authorizerId: wsAuthorizer.ref,
      target: `integrations/${wsConnectIntegration.ref}`,
    });

    new apigatewayv2.CfnRoute(this, 'WsDisconnectRoute', {
      apiId: wsApi.ref,
      routeKey: '$disconnect',
      target: `integrations/${wsDisconnectIntegration.ref}`,
    });

    new apigatewayv2.CfnRoute(this, 'WsDefaultRoute', {
      apiId: wsApi.ref,
      routeKey: '$default',
      target: `integrations/${wsMessageIntegration.ref}`,
    });

    new apigatewayv2.CfnRoute(this, 'WsSendMessageRoute', {
      apiId: wsApi.ref,
      routeKey: 'sendmessage',
      target: `integrations/${wsMessageIntegration.ref}`,
    });

    new apigatewayv2.CfnStage(this, 'WsApiStage', {
      apiId: wsApi.ref,
      stageName: props.environment,
      autoDeploy: true,
    });

    new cdk.CfnOutput(this, 'WebSocketEndpoint', {
      value: `wss://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/${props.environment}`,
    });
  }
}
