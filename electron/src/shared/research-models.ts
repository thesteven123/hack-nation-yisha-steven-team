import type { WorkspaceProfileScope } from './types'
import { labBranchId } from './research-branches'

export const RESEARCH_ROLES = ['planner', 'analyst', 'reviewer'] as const
export type ResearchRole = typeof RESEARCH_ROLES[number]
interface Interpretation { layer: 'model_interpretation'; machine_records_modified: false; scientific_validation: 'not_established' }
export interface PlannerOutput extends Interpretation { status: 'recommendation' | 'needs_input' | 'needs_method'; selected_action_id: string | null; reason: string; candidates: { action_id: string; applicability: 'applicable' | 'not_applicable' | 'requires_input'; goal_relevance: string; expected_learning: string; limitations: string[]; rejected_reason: string }[]; missing_inputs: string[]; questions: string[]; scope_note: string }
export interface AnalystOutput extends Interpretation { status: 'interpreted' | 'needs_input' | 'not_applicable'; summary: string; findings: { id: string; run_id: string; field_paths: string[]; interpretation: string; limitations: string[] }[]; hypothesis_assessment: { status: 'untested' | 'bounded_evidence_only' | 'not_applicable'; reason: string }; missing_inputs: string[]; machine_fact_refs: { finding_id: string; run_id: string; field_path: string; value: unknown; qc_passed: boolean }[] }
export interface ReviewerOutput extends Interpretation { status: 'reviewed' | 'needs_input' | 'not_applicable'; summary: string; critiques: { claim_id: string; run_id: string; assessment: 'adequate_within_scope' | 'needs_correction' | 'needs_more_evidence' | 'not_applicable'; source_support_assessment: 'bounded_machine_evidence' | 'cannot_determine'; inference_assessment: 'not_assessed' | 'missing_premise' | 'possible_error' | 'cannot_determine'; concern: string; suggested_check: string }[]; next_action: 'freeze_next_plan' | 'ask_human' | 'stop' | 'needs_method'; next_action_reason: string; unresolved: string[]; questions: string[] }
export type ResearchModelOutput = PlannerOutput | AnalystOutput | ReviewerOutput
export interface ResearchModelJob {
  branch_id?: string | null; branch_scope?: Record<string, unknown> | null; scope_status?: 'current' | 'stale' | 'unknown';
  id: string; campaign_id: string; campaign_revision: number; brief_revision: number; role: ResearchRole;
  status: 'planned' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'stale'; created_at: string; updated_at: string;
  packet_ref: string; output_ref: string | null; raw_output_ref: string | null; output: ResearchModelOutput | null;
  usage: { available?: boolean; complete?: boolean; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; monetary_cost?: number | null; [key: string]: unknown };
  provider: Record<string, unknown> | null; context: Record<string, unknown>; error: { code: string; message: string } | null;
  events: Record<string, unknown>[]; has_more_events: boolean; attempt_count: number; owner: string | null;
}
export interface ResearchModelPage { items: ResearchModelJob[]; has_more?: boolean; next_before?: number | null; quota: { limit_jobs: number; used: number; reserved: number; resolved_model?: string | null } }
export type ResearchModelCreate = { idempotency_key: string; role: ResearchRole } & ({ expected_revision: number; branch_id?: never } | { branch_id: string; expected_branch_revision: number; expected_authority_epoch: number; expected_revision?: never })
export interface ResearchModelArtifact { sha256: string; content: unknown }
export interface ResearchModelAPI {
  list(scope: WorkspaceProfileScope, campaign: string, before?: number, branch?: string): Promise<ResearchModelPage>
  create(scope: WorkspaceProfileScope, campaign: string, input: ResearchModelCreate): Promise<ResearchModelJob>
  get(scope: WorkspaceProfileScope, campaign: string, job: string): Promise<ResearchModelJob>
  start(scope: WorkspaceProfileScope, campaign: string, job: string): Promise<ResearchModelJob>
  wait(scope: WorkspaceProfileScope, campaign: string, job: string): Promise<ResearchModelJob>
  cancel(scope: WorkspaceProfileScope, campaign: string, job: string): Promise<ResearchModelJob>
  artifact(scope: WorkspaceProfileScope, campaign: string, job: string, hash: string): Promise<ResearchModelArtifact>
}
export const researchJobActive = (job: ResearchModelJob) => job.status === 'running' || job.status === 'planned'
function invalid(): never { throw new Error('Invalid Research model job response.') }
function object(value: unknown): Record<string, any> { if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(); return value as Record<string, any> }
function strings(value: unknown): void { if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) invalid() }
function fields(value: unknown, names: string[]): void { const row = object(value); if (names.some(key => typeof row[key] !== 'string')) invalid() }
function rows(value: unknown): Record<string, any>[] { if (!Array.isArray(value)) invalid(); return value.map(object) }
function output(value: unknown, role: ResearchRole): void {
  const row = object(value)
  if (row.layer !== 'model_interpretation' || row.machine_records_modified !== false || row.scientific_validation !== 'not_established') invalid()
  if (role === 'planner') {
    if (!['recommendation', 'needs_input', 'needs_method'].includes(row.status) || !(row.selected_action_id === null || typeof row.selected_action_id === 'string')) invalid()
    fields(row, ['reason', 'scope_note']); strings(row.missing_inputs); strings(row.questions)
    for (const candidate of rows(row.candidates)) { fields(candidate, ['action_id', 'goal_relevance', 'expected_learning', 'rejected_reason']); strings(candidate.limitations); if (!['applicable', 'not_applicable', 'requires_input'].includes(candidate.applicability)) invalid() }
  } else if (role === 'analyst') {
    if (!['interpreted', 'needs_input', 'not_applicable'].includes(row.status)) invalid()
    fields(row, ['summary']); fields(row.hypothesis_assessment, ['status', 'reason']); strings(row.missing_inputs)
    for (const finding of rows(row.findings)) { fields(finding, ['id', 'run_id', 'interpretation']); strings(finding.field_paths); strings(finding.limitations) }
    for (const fact of rows(row.machine_fact_refs)) { fields(fact, ['finding_id', 'run_id', 'field_path']); if (!('value' in fact) || typeof fact.qc_passed !== 'boolean') invalid() }
  } else {
    if (!['reviewed', 'needs_input', 'not_applicable'].includes(row.status) || !['freeze_next_plan', 'ask_human', 'stop', 'needs_method'].includes(row.next_action)) invalid()
    fields(row, ['summary', 'next_action_reason']); strings(row.unresolved); strings(row.questions)
    for (const critique of rows(row.critiques)) fields(critique, ['claim_id', 'run_id', 'assessment', 'source_support_assessment', 'inference_assessment', 'concern', 'suggested_check'])
  }
}
export function parseResearchJob(value: unknown, campaign: string, jobId?: string, branchId?: string): ResearchModelJob {
  const row = object(value)
  if (row.branch_id != null) labBranchId(row.branch_id)
  if (row.branch_scope != null) object(row.branch_scope)
  if (row.scope_status !== undefined && !['current', 'stale', 'unknown'].includes(row.scope_status) || branchId !== undefined && row.branch_id !== branchId) invalid()
  if (row.campaign_id !== campaign || jobId !== undefined && row.id !== jobId || !RESEARCH_ROLES.includes(row.role) || !['planned', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'stale'].includes(row.status) || !Number.isSafeInteger(row.campaign_revision) || !Number.isSafeInteger(row.brief_revision)) invalid()
  fields(row, ['id', 'created_at', 'updated_at', 'packet_ref']); object(row.usage); object(row.context); rows(row.events)
  for (const key of ['output_ref', 'raw_output_ref', 'owner']) if (!(row[key] === null || typeof row[key] === 'string')) invalid()
  if (row.error !== null) fields(row.error, ['code', 'message'])
  if (row.provider !== null) object(row.provider)
  if (row.output !== null) { if (row.status !== 'completed') invalid(); output(row.output, row.role) }
  if (row.status === 'completed' && row.output === null || typeof row.has_more_events !== 'boolean' || !Number.isSafeInteger(row.attempt_count)) invalid()
  return row as ResearchModelJob
}
export function parseResearchModelPage(value: unknown, campaign: string, branchId?: string): ResearchModelPage { const row = object(value); rows(row.items).forEach(job => parseResearchJob(job, campaign, undefined, branchId)); const quota = object(row.quota); for (const key of ['limit_jobs', 'used', 'reserved']) if (!Number.isSafeInteger(quota[key]) || quota[key] < 0) invalid(); if (!(row.has_more === undefined || typeof row.has_more === 'boolean') || !(row.next_before === undefined || row.next_before === null || Number.isSafeInteger(row.next_before) && row.next_before > 0)) invalid(); return row as ResearchModelPage }
export function parseResearchModelArtifact(value: unknown, hash: string): ResearchModelArtifact { const row = object(value); if (row.sha256 !== hash || !('content' in row)) invalid(); return row as ResearchModelArtifact }
