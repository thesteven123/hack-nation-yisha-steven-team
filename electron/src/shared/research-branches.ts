import type { LabCampaign, LabMutation, LabPlan } from './research-lab'
import { labHash, labId, parseLabPlan } from './research-lab'
import type { WorkspaceProfileScope } from './types'

export const BRANCH_STATUSES = ['planned', 'awaiting_next', 'completed', 'needs_input', 'paused', 'stopped'] as const
export type BranchStatus = typeof BRANCH_STATUSES[number]
export interface BranchQuestion { id: string; prompt: string; required: boolean; prompt_hash: string; needs_answer?: boolean; answer: null | { answer: string; artifact: string; prompt_hash: string; question_context_hash: string; [key: string]: unknown } }
export interface BranchDependency { branch_id: string; require: 'completed' | 'qc_passed' }
export interface BranchProposal {
  id: string; title: string; question: string; success_criterion: string; methods: string[];
  source_ids?: string[]; depends_on: BranchDependency[]; questions: { id: string; prompt: string; required: boolean }[]
}
export interface BranchGate { allowed: boolean; blockers: { code: string; branch_id?: string; question_id?: string }[]; dependency_refs: Record<string, unknown>[] }
export interface BranchResult { run_id: string; round_artifact: string; observation_artifact: string; input_artifact: string; status: 'completed' | 'failed'; qc_passed: boolean; root_context_hash: string; context_hash: string; current?: boolean }
export interface LabBranch {
  id: string; revision: number; title: string; question: string; success_criterion: string; methods: string[]; source_ids: string[] | null;
  input_artifact: string; root_context_hash: string; control: 'active' | 'paused' | 'stopped';
  status?: string; current_plan?: LabPlan | null; latest_result: BranchResult | null; questions: BranchQuestion[];
  depends_on: BranchDependency[]; gate?: BranchGate; current_scope?: Record<string, unknown> | null; scope_status?: 'current' | 'blocked' | 'unknown';
  owned_work: null | { id: string; kind: 'local_action' | 'native_role'; cancellation_requested: boolean; outcome: string; scope: Record<string, unknown> };
  post_outcome: boolean;
}
export interface LabBranchSet { schema_version: 1; campaign_id: string; authority_epoch: number; root_control: 'active' | 'paused' | 'stopped'; branches: LabBranch[]; historical_unassigned_run_ids: string[]; automatic_dispatch: false }
export interface BranchMutation { expected_branch_revision: number; expected_authority_epoch: number; idempotency_key: string }
export interface BranchEnable extends LabMutation { branches: BranchProposal[] }
export interface BranchPlanInput extends BranchMutation { changes?: Partial<Pick<BranchProposal, 'title' | 'question' | 'success_criterion' | 'methods' | 'source_ids'>> }
export interface BranchAnswersInput extends BranchMutation { answers: { question_id: string; answer: string }[] }
export interface BranchDecisionInput extends BranchMutation { selected_action_id: string; feedback: string }
export interface BranchControlInput extends BranchMutation { operation: 'pause' | 'resume' | 'stop'; feedback: string }
export interface ResearchBranchesAPI {
  get(scope: WorkspaceProfileScope, campaign: string): Promise<LabCampaign>
  enable(scope: WorkspaceProfileScope, campaign: string, input: BranchEnable): Promise<LabCampaign>
  plan(scope: WorkspaceProfileScope, campaign: string, branch: string, input: BranchPlanInput): Promise<LabCampaign>
  answers(scope: WorkspaceProfileScope, campaign: string, branch: string, input: BranchAnswersInput): Promise<LabCampaign>
  decision(scope: WorkspaceProfileScope, campaign: string, branch: string, input: BranchDecisionInput): Promise<LabCampaign>
  control(scope: WorkspaceProfileScope, campaign: string, branch: string, input: BranchControlInput): Promise<LabCampaign>
  run(scope: WorkspaceProfileScope, campaign: string, branch: string, input: BranchMutation): Promise<LabCampaign>
}

function invalid(): never { throw new Error('Invalid research branch response.') }
function row(value: unknown): Record<string, any> { if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(); return value as Record<string, any> }
function strings(value: unknown): string[] { if (!Array.isArray(value) || value.some(x => typeof x !== 'string')) invalid(); return value }
function records(value: unknown): Record<string, any>[] { if (!Array.isArray(value)) invalid(); return value.map(row) }
function fields(value: Record<string, any>, keys: string[]) { if (keys.some(key => typeof value[key] !== 'string')) invalid() }
function revision(value: unknown) { if (!Number.isSafeInteger(value) || Number(value) < 1) invalid() }
export function labBranchId(value: string): string { if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value)) throw new Error('Invalid research branch identifier.'); return value }

/** Missing admission projections remain missing. They never become permission. */
export function parseLabBranchSet(value: unknown, campaignId: string): LabBranchSet {
  const set = row(value)
  if (set.schema_version !== 1 || set.campaign_id !== campaignId || set.automatic_dispatch !== false || !['active', 'paused', 'stopped'].includes(set.root_control)) invalid()
  revision(set.authority_epoch); strings(set.historical_unassigned_run_ids)
  const branches = records(set.branches)
  if (!branches.length || branches.length > 3 || new Set(branches.map(branch => branch.id)).size !== branches.length) invalid()
  let questions = 0
  for (const branch of branches) {
    labBranchId(branch.id); revision(branch.revision); fields(branch, ['title', 'question', 'success_criterion', 'input_artifact', 'root_context_hash'])
    labHash(branch.input_artifact); labHash(branch.root_context_hash); strings(branch.methods)
    if (branch.source_ids !== null) strings(branch.source_ids)
    if (!['active', 'paused', 'stopped'].includes(branch.control) || typeof branch.post_outcome !== 'boolean') invalid()
    if (branch.status !== undefined && typeof branch.status !== 'string') invalid()
    if (branch.scope_status !== undefined && !['current', 'blocked', 'unknown'].includes(branch.scope_status)) invalid()
    if (branch.current_scope != null) row(branch.current_scope)
    if (branch.current_plan != null) parseLabPlan(branch.current_plan)
    for (const dependency of records(branch.depends_on)) { labBranchId(dependency.branch_id); if (!['completed', 'qc_passed'].includes(dependency.require)) invalid() }
    if (branch.gate !== undefined) {
      const gate = row(branch.gate); if (typeof gate.allowed !== 'boolean') invalid()
      records(gate.blockers).forEach(blocker => fields(blocker, ['code'])); records(gate.dependency_refs)
    }
    const prompts = records(branch.questions); questions += prompts.length
    if (new Set(prompts.map(prompt => prompt.id)).size !== prompts.length) invalid()
    for (const prompt of prompts) {
      labBranchId(prompt.id); fields(prompt, ['prompt', 'prompt_hash']); labHash(prompt.prompt_hash)
      if (typeof prompt.required !== 'boolean' || prompt.needs_answer !== undefined && typeof prompt.needs_answer !== 'boolean') invalid()
      if (prompt.answer !== null) { const answer = row(prompt.answer); fields(answer, ['answer', 'artifact', 'prompt_hash', 'question_context_hash']); labHash(answer.artifact); labHash(answer.prompt_hash); labHash(answer.question_context_hash) }
    }
    if (branch.latest_result !== null) {
      const result = row(branch.latest_result); labId(result.run_id)
      for (const key of ['round_artifact', 'observation_artifact', 'input_artifact', 'root_context_hash', 'context_hash']) labHash(result[key])
      if (!['completed', 'failed'].includes(result.status) || typeof result.qc_passed !== 'boolean' || result.current !== undefined && typeof result.current !== 'boolean') invalid()
    }
    if (branch.owned_work !== null) {
      const work = row(branch.owned_work); fields(work, ['id', 'outcome']); row(work.scope)
      if (!['local_action', 'native_role'].includes(work.kind) || typeof work.cancellation_requested !== 'boolean') invalid()
    }
  }
  if (questions > 3) invalid()
  return set as LabBranchSet
}

export function branchPermissionKnown(branch: LabBranch): boolean {
  return branch.status !== undefined && (BRANCH_STATUSES as readonly string[]).includes(branch.status)
    && branch.scope_status !== undefined && branch.scope_status !== 'unknown' && branch.gate !== undefined
}
export function branchMayRun(set: LabBranchSet, branch: LabBranch): boolean {
  return branchPermissionKnown(branch) && set.root_control === 'active' && branch.control === 'active'
    && branch.scope_status === 'current' && branch.current_scope != null && branch.gate?.allowed === true
    && branch.gate.blockers.length === 0 && branch.owned_work === null && branch.status === 'planned'
    && branch.current_plan?.selection_origin === 'human'
}
export function branchResult(campaign: LabCampaign, branch: LabBranch) {
  return branch.latest_result ? campaign.rounds.find(round => round.run.id === branch.latest_result!.run_id) : undefined
}
