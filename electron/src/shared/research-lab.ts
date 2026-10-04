import type { WorkspaceProfileScope } from './types'
import { parseLabBranchSet, type LabBranchSet, type ResearchBranchesAPI } from './research-branches'

export type LabAdapterId = 'source_evidence' | 'paired_numeric'
export interface LabBrief { goal: string; hypothesis: string; success_criteria: string; constraints: string }
export type LabEvidenceSelector = { kind: 'input' } | { kind: 'source'; source_id: string } | { kind: 'observation'; run_id: string }
export type LabHypothesisEvidence = ({ kind: 'input' } | { kind: 'source'; source_id: string; source_hash: string; coverage: string; missing_sections: string[] } | { kind: 'observation'; run_id: string; input_artifact: string; brief_revision: number; run_status: string; qc_passed: boolean }) & { artifact: string }
export interface LabHypothesisCandidate { id: string; statement: string; assumptions: string[]; scope: string; supporting_evidence: LabEvidenceSelector[]; opposing_evidence: LabEvidenceSelector[]; predictions: string[]; weakening_conditions: string[] }
export interface LabHypothesisSetInput { candidates: LabHypothesisCandidate[]; open_alternative: string }
export interface LabHypothesisSet extends Omit<LabHypothesisSetInput, 'candidates'> { id: string; revision: number; brief_revision: number; introduced_at: string; origin: 'human'; post_outcome: boolean; previous_artifact: string | null; input_artifact: string; scientific_status: 'not_validated'; evidence_scope: string; visible_evidence_refs: LabHypothesisEvidence[]; candidates: (Omit<LabHypothesisCandidate, 'supporting_evidence' | 'opposing_evidence'> & { supporting_evidence: LabHypothesisEvidence[]; opposing_evidence: LabHypothesisEvidence[] })[] }
export interface LabSource { id: string; title: string; uri: string; text: string; coverage: 'full_text' | 'excerpt'; missing_sections: string[]; provenance?: Record<string, unknown> }
export interface SourceInputs { quote: string; sources: LabSource[] }
export interface NumericInputs { baseline: number[]; treatment: number[]; unit: string; minimum_effect: number }
export type LabInputs = SourceInputs | NumericInputs
export interface LabOrigin { kind: 'idea'; session_id: string; generation_id: string; selected_ids: string[]; decision_revision: number }
export interface LabImportCoverage { referenced_sources: number; imported_sources: number; omitted_sources: number; omitted_source_ids: string[]; omitted_evidence_ids: string[]; selection_policy: 'first_seen_bounded_subset'; complete: boolean; scope: 'selected_direction_source_versions_only'; limitation: string }
export interface LabSeed { origin: LabOrigin; brief: LabBrief; inputs: SourceInputs; import_coverage?: LabImportCoverage }
export interface LabManifest { id: string; version: string; title: string; task_family: string; methods: string[]; input_schema: Record<string, unknown>; observation_schema: Record<string, unknown>; applicability: string[]; limitations: string[]; required_protocols: string[]; execution: 'local_deterministic'; fresh_replicates: false }
export interface LabCapabilities { schema_version: 1; adapters: LabManifest[]; execution: Record<string, unknown> }
export interface LabAction { id: string; method: string; title: string; question: string; goal_revision: number; input_artifact: string; parameters: Record<string, unknown>; expected_learning: string; cost: { actions: number; external_requests: number; model_tokens: number }; qc_rules: string[]; interpretation_rules: string[]; stop_rules: string[]; adapter_version: string; protocol_refs: string[]; spec_artifact: string; research_mode?: string; task_packet?: Record<string, unknown>; execution_environment?: Record<string, unknown> }
export interface LabPlan { id: string; round: number; goal_revision: number; input_artifact: string; candidates: LabAction[]; selected_action_id: string; selection_origin: 'policy' | 'human'; selection_reason: string; comparison_criteria: string[]; frozen_at: string }
export interface LabReview { next_action: 'continue' | 'stop' | 'needs_input'; reason: string; next_method: string | null; uncertainties: string[]; alternatives: string[]; input_refs: string[] }
export interface LabRound {
  index: number; plan: LabPlan;
  run: { id: string; attempt_id: string; generation: number; status: 'completed' | 'failed'; spec_artifact: string; dispatch_artifact?: string; observation_artifact: string; input_artifact: string; replicate_id: null; is_independent_replicate: false; started_at: string; completed_at: string; usage: Record<string, number> };
  observation: { kind: string; data: Record<string, unknown>; coverage: Record<string, unknown> };
  qc: { passed: boolean; checks: { name: string; passed: boolean; detail: string }[] };
  analysis: { claim: string; source_support: string; inference_validity: string; limitations: string[]; input_refs: string[] };
  review: LabReview;
}
export interface LabDependencyStatus {
  state: 'unregistered' | 'current' | 'correction_pending' | 'needs_revalidation'; run_allowed: boolean; current_input_blocked: boolean;
  affected_claim_ids: string[]; pending_intents: { id: string; reason: string; affected_input_hashes: string[] }[];
  corrections: { id: string; scope: string; reason: string; affected_input_hashes: string[] }[]; pending_deliveries: number;
}
export interface LabCampaign {
  branch_set?: LabBranchSet | null;
  dependency_status?: LabDependencyStatus;
  hypothesis_set?: LabHypothesisSet | null; hypothesis_set_artifact?: string | null; hypothesis_set_status?: 'current_context' | 'needs_revalidation' | 'not_specified';
  schema_version: 1; id: string; revision: number; created_at: string; updated_at: string; entry: 'goal' | 'hypothesis'; status: 'planned' | 'awaiting_next' | 'completed' | 'needs_input' | 'stopped';
  brief: LabBrief & { revision: number; authorized_actions: string[]; stop_rules: string[] }; origin: LabOrigin | null;
  adapter: LabManifest; input_artifact: string; input_summary: Record<string, unknown>;
  hypotheses: { id: string; statement: string; status: string; origin: string; weakening_condition: string }[];
  budget: { max_actions: number; max_rounds: number; used_actions: number; reserved_actions: number; remaining_actions: number; model_tokens: number; external_requests: number; monetary_cost: number; wall_ms: number };
  current_plan: LabPlan | null; rounds: LabRound[]; decisions: Record<string, unknown>[];
  total_decisions?: number; has_more_decisions?: boolean;
  total_rounds?: number; has_more_rounds?: boolean; omitted_rounds?: { index: number; run_id: string; artifact: string }[];
  claims: { id: string; text: string; status: 'current' | 'needs_revalidation'; source_support: string; inference_validity: string; conditions: string[]; input_artifact: string; run_id: string }[];
  review: LabReview | null; stop_reason: string | null; events: { id: string; seq: number; type: string; at: string; revision: number; data: Record<string, unknown>; artifact?: string }[]; has_more_events: boolean;
  comparisons?: { id: string; run_ids: string[]; rule: string; comparability: 'same_inputs_different_analysis' | 'changed_inputs_not_like_for_like' | 'changed_goal_not_like_for_like'; eligible_for_pooled_confirmation: false; source_support: { before: string; after: string }; inference_validity: { before: string; after: string }; propositions?: string[]; changed_observation_fields: string[]; limitations: string[]; unresolved: string[]; artifact: string }[];
}
export interface LabCreate { idempotency_key: string; entry: 'goal' | 'hypothesis'; brief: LabBrief; adapter_id: LabAdapterId; inputs: LabInputs; budget: { max_actions: number; max_rounds: number }; origin?: LabOrigin; hypothesis_set?: LabHypothesisSetInput | null }
export interface LabMutation { expected_revision: number; idempotency_key: string }
export interface LabDecision extends LabMutation { kind: 'select' | 'revise' | 'defer' | 'stop'; selected_action_id?: string; goal?: string; hypothesis?: string; success_criteria?: string; constraints?: string; feedback: string; hypothesis_set?: LabHypothesisSetInput | null }
export interface LabCorrection extends LabMutation { inputs: LabInputs; reason: string; shared_source_id?: string }
export interface LabPage { items: LabCampaign[]; has_more: boolean; next_cursor?: string | null }
export interface LabHistory { items: { revision: number; at: string; event: string; event_artifact?: string; brief: LabCampaign['brief']; status: LabCampaign['status'] }[]; has_more: boolean; next_cursor?: string | null }
export interface LabArtifact { sha256: string; media_type: 'application/json'; content: unknown }
export const LAB_PROTOCOL_IDS = ['planning-v0.5', 'records-v0.5', 'execution-v0.5', 'analysis-review-v0.5', 'literature-cache-v0.5'] as const
export type LabProtocolId = typeof LAB_PROTOCOL_IDS[number]
export interface LabProtocol { id: LabProtocolId; title: string; content: string; summary_version: string; is_full_source: false; scope: string; summary_sha256: string; source: { page_id: string; url: string; sequence: number; updated_at: string; blocks: { id: string; hash: string }[] } }
export interface LabExportBundle { format: 'agentsdock-research-campaign/1'; schema_version: 1; campaign_id: string; snapshot_kind: 'sqlite_read_transaction'; data: { campaign: LabCampaign; versions: { payload: LabCampaign }[]; events: Record<string, unknown>[]; mutations: { response: LabCampaign }[]; artifacts: { sha256: string; encoding: 'base64'; data: string }[] }; manifest: { table_hashes: Record<string, string>; authentication: string }; bundle_sha256: string }
export interface LabExportSaved { path: string; campaign_id: string; bundle_sha256: string; bytes: number }
export interface ResearchLabAPI {
  branches?: ResearchBranchesAPI
  models?: import('./research-models').ResearchModelAPI
  capabilities(scope: WorkspaceProfileScope): Promise<LabCapabilities>
  list(scope: WorkspaceProfileScope, before?: string): Promise<LabPage>
  get(scope: WorkspaceProfileScope, id: string): Promise<LabCampaign>
  ideaSeed(scope: WorkspaceProfileScope, id: string): Promise<LabSeed>
  create(scope: WorkspaceProfileScope, input: LabCreate): Promise<LabCampaign>
  decision(scope: WorkspaceProfileScope, id: string, input: LabDecision): Promise<LabCampaign>
  run(scope: WorkspaceProfileScope, id: string, input: LabMutation): Promise<LabCampaign>
  advance(scope: WorkspaceProfileScope, id: string, input: LabMutation): Promise<LabCampaign>
  correctInputs(scope: WorkspaceProfileScope, id: string, input: LabCorrection): Promise<LabCampaign>
  reconcileDependencies(scope: WorkspaceProfileScope, id: string): Promise<LabCampaign>
  history(scope: WorkspaceProfileScope, id: string, before?: string): Promise<LabHistory>
  artifact(scope: WorkspaceProfileScope, id: string, hash: string): Promise<LabArtifact>
  protocol(scope: WorkspaceProfileScope, id: LabProtocolId): Promise<LabProtocol>
  export(scope: WorkspaceProfileScope, id: string): Promise<LabExportSaved | null>
}

export function labId(value: string): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new Error('Invalid research identifier.')
  return value
}
export function labHash(value: string): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error('Invalid research artifact hash.')
  return value
}
export function labCursorQuery(before?: string): string {
  if (before === undefined) return ''
  if (typeof before !== 'string' || !/^[A-Za-z0-9_-]{1,1024}$/.test(before)) throw new Error('Invalid research cursor.')
  return `?${new URLSearchParams({ before, limit: '50' })}`
}
function invalid(): never { throw new Error('Invalid Research Lab response.') }
export function labProtocolId(value: string): LabProtocolId { if (!(LAB_PROTOCOL_IDS as readonly string[]).includes(value)) throw new Error('Invalid research protocol.'); return value as LabProtocolId }
function obj(value: unknown): Record<string, any> { if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(); return value as Record<string, any> }
function texts(value: unknown): string[] { if (!Array.isArray(value) || value.some(x => typeof x !== 'string')) invalid(); return value }
function fields(value: unknown, keys: string[]): void { const row = obj(value); if (keys.some(key => typeof row[key] !== 'string')) invalid() }
function rows(value: unknown): Record<string, any>[] { if (!Array.isArray(value)) invalid(); return value.map(obj) }
function finite(value: unknown): void { if (typeof value !== 'number' || !Number.isFinite(value)) invalid() }
function brief(value: unknown): void { fields(value, ['goal', 'hypothesis', 'success_criteria', 'constraints']) }
function hypothesisEvidence(value: unknown): void {
  const row = obj(value); labHash(row.artifact)
  if (row.kind === 'source') { fields(row, ['source_id', 'source_hash', 'coverage']); labHash(row.source_hash); texts(row.missing_sections) }
  else if (row.kind === 'observation') { fields(row, ['run_id', 'run_status']); labHash(row.input_artifact); finite(row.brief_revision); if (typeof row.qc_passed !== 'boolean') invalid() }
  else if (row.kind !== 'input') invalid()
}
export function parseLabHypothesisSet(value: unknown): LabHypothesisSet {
  const row = obj(value); fields(row, ['id', 'introduced_at', 'open_alternative', 'evidence_scope']); finite(row.revision); finite(row.brief_revision); labHash(row.input_artifact)
  if (row.origin !== 'human' || row.scientific_status !== 'not_validated' || typeof row.post_outcome !== 'boolean' || !(row.previous_artifact === null || typeof row.previous_artifact === 'string')) invalid()
  if (row.previous_artifact !== null) labHash(row.previous_artifact)
  const candidates = rows(row.candidates); if (candidates.length < 1 || candidates.length > 4) invalid()
  for (const candidate of candidates) { fields(candidate, ['id', 'statement', 'scope']); for (const key of ['assumptions', 'predictions', 'weakening_conditions']) texts(candidate[key]); rows(candidate.supporting_evidence).forEach(hypothesisEvidence); rows(candidate.opposing_evidence).forEach(hypothesisEvidence) }
  rows(row.visible_evidence_refs).forEach(hypothesisEvidence)
  return row as LabHypothesisSet
}
function manifest(value: unknown): void { const row = obj(value); fields(row, ['id', 'version', 'title', 'task_family']); for (const key of ['methods', 'applicability', 'limitations', 'required_protocols']) texts(row[key]); obj(row.input_schema); obj(row.observation_schema); if (row.execution !== 'local_deterministic' || row.fresh_replicates !== false) invalid() }
export function parseLabPlan(value: unknown): LabPlan {
  const row = obj(value); fields(row, ['id', 'input_artifact', 'selected_action_id', 'selection_reason', 'frozen_at']); texts(row.comparison_criteria); finite(row.round); finite(row.goal_revision)
  if (!['policy', 'human'].includes(row.selection_origin)) invalid()
  for (const action of rows(row.candidates)) { fields(action, ['id', 'method', 'title', 'question', 'input_artifact', 'expected_learning', 'adapter_version', 'spec_artifact']); obj(action.parameters); obj(action.cost); for (const key of ['qc_rules', 'interpretation_rules', 'stop_rules', 'protocol_refs']) texts(action[key]) }
  return row as LabPlan
}
function review(value: unknown): void { const row = obj(value); fields(row, ['reason']); if (!['continue', 'stop', 'needs_input'].includes(row.next_action) || !(row.next_method === null || typeof row.next_method === 'string')) invalid(); for (const key of ['uncertainties', 'alternatives', 'input_refs']) texts(row[key]) }
export function parseLabRound(value: unknown): LabRound {
  const round = obj(value)
  finite(round.index); parseLabPlan(round.plan); fields(round.run, ['id', 'attempt_id', 'status', 'spec_artifact', 'observation_artifact', 'input_artifact', 'started_at', 'completed_at'])
  if (!['completed', 'failed'].includes(round.run.status) || round.run.is_independent_replicate !== false) invalid()
  if (round.run.dispatch_artifact !== undefined) labHash(round.run.dispatch_artifact)
  fields(round.observation, ['kind']); obj(round.observation.data); obj(round.observation.coverage)
  if (typeof obj(round.qc).passed !== 'boolean') invalid()
  for (const check of rows(round.qc.checks)) { fields(check, ['name', 'detail']); if (typeof check.passed !== 'boolean') invalid() }
  fields(round.analysis, ['claim', 'source_support', 'inference_validity']); texts(round.analysis.limitations); texts(round.analysis.input_refs); review(round.review)
  return round as LabRound
}
export function parseLabCampaign(value: unknown): LabCampaign {
  const branchValue = obj(value)
  if (branchValue.branch_set != null) parseLabBranchSet(branchValue.branch_set, branchValue.id)
  const row = obj(value); labId(row.id); brief(row.brief); manifest(row.adapter)
  if (row.hypothesis_set != null) parseLabHypothesisSet(row.hypothesis_set)
  if (row.hypothesis_set_artifact != null) labHash(row.hypothesis_set_artifact)
  if (row.hypothesis_set_status !== undefined && !['current_context', 'needs_revalidation', 'not_specified'].includes(row.hypothesis_set_status)) invalid()
  if (row.schema_version !== 1 || !Number.isSafeInteger(row.revision) || row.revision < 1 || !['goal', 'hypothesis'].includes(row.entry) || !['planned', 'awaiting_next', 'completed', 'needs_input', 'stopped'].includes(row.status)) invalid()
  fields(row, ['created_at', 'updated_at', 'input_artifact']); obj(row.input_summary); texts(row.brief.authorized_actions); texts(row.brief.stop_rules)
  for (const key of ['max_actions', 'max_rounds', 'used_actions', 'reserved_actions', 'remaining_actions', 'model_tokens', 'external_requests', 'monetary_cost', 'wall_ms']) finite(obj(row.budget)[key])
  for (const hypothesis of rows(row.hypotheses)) fields(hypothesis, ['id', 'statement', 'status', 'origin', 'weakening_condition'])
  if (row.current_plan !== null) parseLabPlan(row.current_plan)
  rows(row.rounds).forEach(parseLabRound)
  rows(row.decisions); rows(row.events)
  if (row.total_decisions !== undefined) finite(row.total_decisions)
  if (row.has_more_decisions !== undefined && typeof row.has_more_decisions !== 'boolean') invalid()
  if (row.total_rounds !== undefined) finite(row.total_rounds)
  if (row.has_more_rounds !== undefined && typeof row.has_more_rounds !== 'boolean') invalid()
  if (row.omitted_rounds !== undefined) for (const omitted of rows(row.omitted_rounds)) { finite(omitted.index); fields(omitted, ['run_id', 'artifact']); labHash(omitted.artifact) }
  for (const claim of rows(row.claims)) { fields(claim, ['id', 'text', 'source_support', 'inference_validity', 'input_artifact', 'run_id']); texts(claim.conditions); if (!['current', 'needs_revalidation'].includes(claim.status)) invalid() }
  if (row.review !== null) review(row.review)
  if (row.comparisons !== undefined) for (const comparison of rows(row.comparisons)) {
    fields(comparison, ['id', 'rule', 'artifact']); texts(comparison.run_ids); texts(comparison.limitations); texts(comparison.unresolved)
    fields(comparison.source_support, ['before', 'after']); fields(comparison.inference_validity, ['before', 'after'])
    if (comparison.propositions !== undefined) { texts(comparison.propositions); if (comparison.propositions.length !== 2) invalid() }
    if (!['same_inputs_different_analysis', 'changed_inputs_not_like_for_like', 'changed_goal_not_like_for_like'].includes(comparison.comparability) || comparison.eligible_for_pooled_confirmation !== false) invalid()
  }
  if (!(row.stop_reason === null || typeof row.stop_reason === 'string') || typeof row.has_more_events !== 'boolean') invalid()
  if (row.dependency_status !== undefined) {
    const status = obj(row.dependency_status)
    if (!['unregistered', 'current', 'correction_pending', 'needs_revalidation'].includes(status.state) || typeof status.run_allowed !== 'boolean' || typeof status.current_input_blocked !== 'boolean') invalid()
    texts(status.affected_claim_ids); finite(status.pending_deliveries)
    for (const intent of rows(status.pending_intents)) { fields(intent, ['id', 'reason']); texts(intent.affected_input_hashes) }
    for (const correction of rows(status.corrections)) { fields(correction, ['id', 'scope', 'reason']); texts(correction.affected_input_hashes) }
  }
  return row as LabCampaign
}
function pagination(row: Record<string, any>): void { if (typeof row.has_more !== 'boolean' || !(row.next_cursor === undefined || row.next_cursor === null || typeof row.next_cursor === 'string')) invalid() }
export function parseLabPage(value: unknown): LabPage { const row = obj(value); rows(row.items).forEach(parseLabCampaign); pagination(row); return row as LabPage }
export function parseLabCapabilities(value: unknown): LabCapabilities { const row = obj(value); if (row.schema_version !== 1) invalid(); rows(row.adapters).forEach(manifest); obj(row.execution); return row as LabCapabilities }
export function parseLabHistory(value: unknown): LabHistory { const row = obj(value); for (const item of rows(row.items)) { finite(item.revision); fields(item, ['at', 'event', 'status']); brief(item.brief) } pagination(row); return row as LabHistory }
export function parseLabSeed(value: unknown): LabSeed { const row = obj(value); brief(row.brief); const origin = obj(row.origin); if (origin.kind !== 'idea' || !Number.isSafeInteger(origin.decision_revision) || origin.decision_revision < 1) invalid(); fields(origin, ['session_id', 'generation_id']); texts(origin.selected_ids); fields(row.inputs, ['quote']); for (const source of rows(row.inputs.sources)) { fields(source, ['id', 'title', 'uri', 'text', 'coverage']); texts(source.missing_sections) } if (row.import_coverage !== undefined) { const coverage = obj(row.import_coverage); for (const key of ['referenced_sources', 'imported_sources', 'omitted_sources']) finite(coverage[key]); texts(coverage.omitted_source_ids); texts(coverage.omitted_evidence_ids); if (coverage.selection_policy !== 'first_seen_bounded_subset' || typeof coverage.complete !== 'boolean') invalid() } return row as LabSeed }
export function parseLabArtifact(value: unknown, hash: string): LabArtifact { const row = obj(value); if (row.sha256 !== hash || row.media_type !== 'application/json' || !('content' in row)) invalid(); return row as LabArtifact }
export function parseLabProtocol(value: unknown, id: LabProtocolId): LabProtocol {
  const row = obj(value); if (row.id !== id || row.is_full_source !== false) invalid()
  fields(row, ['title', 'content', 'summary_version', 'scope', 'summary_sha256']); labHash(row.summary_sha256)
  fields(row.source, ['page_id', 'url', 'updated_at']); finite(row.source.sequence)
  for (const block of rows(row.source.blocks)) { fields(block, ['id', 'hash']); labHash(block.hash) }
  return row as LabProtocol
}
export function parseLabExport(value: unknown, id: string): LabExportBundle {
  const row = obj(value), data = obj(row.data)
  if (row.format !== 'agentsdock-research-campaign/1' || row.schema_version !== 1 || row.campaign_id !== id || row.snapshot_kind !== 'sqlite_read_transaction' || obj(data.campaign).id !== id) invalid()
  for (const version of rows(data.versions)) if (obj(version.payload).id !== id) invalid()
  for (const mutation of rows(data.mutations)) if (obj(mutation.response).id !== id) invalid()
  rows(data.events); for (const artifact of rows(data.artifacts)) { fields(artifact, ['sha256', 'data']); labHash(artifact.sha256); if (artifact.encoding !== 'base64') invalid() }
  const manifest = obj(row.manifest); fields(manifest, ['authentication']); const hashes = obj(manifest.table_hashes)
  for (const key of ['campaign', 'versions', 'events', 'mutations', 'artifacts']) labHash(hashes[key])
  labHash(row.bundle_sha256)
  return row as LabExportBundle
}
