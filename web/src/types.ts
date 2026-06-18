import { z } from 'zod';

export const PromptMetadataSchema = z.object({
  prompt_id: z.string(),
  prompt_version: z.string(),
  prompt_status: z.string(),
  prompt_description: z.string(),
});

export const ModelMetadataSchema = z.object({
  model_id: z.string(),
  model_version: z.string(),
  model_status: z.string(),
  model_provider: z.string(),
  model_name: z.string(),
  model_description: z.string(),
  runtime_provider: z.string().optional(),
  runtime_model_name: z.string().optional(),
  runtime_overridden: z.boolean().optional(),
});

export const TraceSummarySchema = z.object({
  alert_id: z.string().nullable(),
  trace_id: z.string().nullable(),
  headline: z.string().nullable(),
  final_status: z.string().nullable(),
  created_at: z.string().nullable(),
  run_duration_ms: z.number().nullable(),
});

export const TraceStepSchema = z.object({
  node_name: z.string(),
  started_at: z.string(),
  ended_at: z.string(),
  duration_ms: z.number(),
  decision: z.string().optional(),
  error: z.string().optional(),
});

export const TraceDetailSchema = z.object({
  alert_id: z.string().nullable(),
  trace_id: z.string().nullable(),
  headline: z.string().nullable(),
  final_status: z.string().nullable(),
  created_at: z.string().nullable(),
  run_duration_ms: z.number().nullable(),
  tool_decision: z.string().nullable(),
  supplier: z.string().nullable(),
  risk_type: z.string().nullable(),
  risk_level: z.string().nullable(),
  change_type: z.string().nullable(),
  trace_steps: z.array(TraceStepSchema),
  prompt_metadata: z.array(PromptMetadataSchema).optional(),
  model_metadata: z.array(ModelMetadataSchema).optional(),
});

export const ExplanationResponseSchema = z.object({
  explanation: z.string(),
});

export type TraceSummary = z.infer<typeof TraceSummarySchema>;
export type TraceStep = z.infer<typeof TraceStepSchema>;
export type TraceDetail = z.infer<typeof TraceDetailSchema>;
export type PromptMetadata = z.infer<typeof PromptMetadataSchema>;
export type ModelMetadata = z.infer<typeof ModelMetadataSchema>;
