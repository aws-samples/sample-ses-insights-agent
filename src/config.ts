export interface AppConfig {
  readonly projectName: string;
  readonly stage: string;
  readonly sesConfigurationSetName: string;
  /** Set to false to use an existing SES Configuration Set instead of creating a new one. */
  readonly createConfigurationSet: boolean;
  readonly modelId: string;
  readonly enableGateway: boolean;
  readonly enableRuntime: boolean;
  /** Firehose buffer interval in seconds (1–900). Defaults to 60 for dev, 300 for prod. */
  readonly firehoseBufferIntervalSeconds?: number;
  /** Firehose buffer size in MB (1–128). Defaults to 64. */
  readonly firehoseBufferSizeMBs?: number;
  /** Optional Amazon Bedrock Guardrail ID for input/output content filtering. */
  readonly guardrailId?: string;
  /** Guardrail version (required if guardrailId is set). Defaults to 'DRAFT'. */
  readonly guardrailVersion?: string;
}

const DEFAULTS: AppConfig = {
  projectName: 'ses-analytics',
  stage: 'dev',
  sesConfigurationSetName: 'ses-analytics-config-set',
  createConfigurationSet: true,
  modelId: 'us.anthropic.claude-sonnet-4-20250514-v1:0',
  enableGateway: true,
  enableRuntime: true,
};

export function loadConfig(ctx: Record<string, unknown> | undefined): AppConfig {
  if (!ctx) return { ...DEFAULTS };
  return {
    projectName: (ctx.projectName as string) ?? DEFAULTS.projectName,
    stage: (ctx.stage as string) ?? DEFAULTS.stage,
    sesConfigurationSetName: (ctx.sesConfigurationSetName as string) ?? DEFAULTS.sesConfigurationSetName,
    createConfigurationSet: (ctx.createConfigurationSet as boolean) ?? DEFAULTS.createConfigurationSet,
    modelId: (ctx.modelId as string) ?? DEFAULTS.modelId,
    enableGateway: (ctx.enableGateway as boolean) ?? DEFAULTS.enableGateway,
    enableRuntime: (ctx.enableRuntime as boolean) ?? DEFAULTS.enableRuntime,
    firehoseBufferIntervalSeconds: ctx.firehoseBufferIntervalSeconds as number | undefined,
    firehoseBufferSizeMBs: ctx.firehoseBufferSizeMBs as number | undefined,
    guardrailId: ctx.guardrailId as string | undefined,
    guardrailVersion: ctx.guardrailVersion as string | undefined,
  };
}
