import type { WorkspaceProfileScope } from './types'

export interface IdeaSource { id: string; title: string; uri: string; text: string; provenance?: Record<string, unknown> }
export interface IdeaLiteratureCache { version: 1; status: 'miss' | 'hit' | 'join' | 'bypass'; reason: string | null; key: string | null; scope: string | null; request_id: string; waited: boolean; stale_rejected: string | null; artifact_hash: string | null; origin: null | { session_id: string; generation_id: string; round: number; attempt_id: string; packet_digest: string }; avoided_model_jobs: 0 | 1 }
export interface IdeaQuestion { id: string; question: string; why: string; required: boolean; options: string[] }
export interface IdeaActivity { id?: string; seq?: number; at?: string; time?: string; agent: string; type: string; summary: string; query?: string; url?: string; source_id?: string; thread_id?: string; turn_id?: string; round?: number; generation_id?: string }
export interface IdeaPaper { id: string; title: string; url: string; retrieved_at?: string; content_hash?: string; parser_version?: string; access: string; coverage: string | string[] | Record<string, unknown>; cache_hit?: boolean; status: string; error?: string | null }
export interface IdeaResearch {
  version: number; round: number; max_rounds: number
  readiness: 'not_started' | 'researching' | 'ready_for_choice' | 'evidence_limited' | 'needs_input' | 'failed' | 'legacy_unsearched'
  stop_reason: string | null
  limits: { max_rounds: number; max_model_calls: number; max_sources: number; max_search_queries: number }
  used: { model_calls: number; searches: number; reads: number; cache_hits: number }
  papers: IdeaPaper[]; searches: Record<string, unknown>[]; activities: IdeaActivity[]
  activities_omitted?: number; coverage_gaps?: string[]; sources?: IdeaSource[]; rounds: Record<string, unknown>[]; questions: IdeaQuestion[]
  agents: { id: string; name: string; role: string; status: string; task: string | null; thread_id?: string; turn_id?: string }[]
}
export interface IdeaAnswer { question_id: string; answer: string }
export interface IdeaFollowupInput { expected_revision: number; idempotency_key: string; feedback: string; mode: 'research' | 'refine' | 'selected'; goal?: string; constraints?: string; selected_ids?: string[]; answers?: IdeaAnswer[] }
export interface IdeaBrief { goal: string; hypothesis: string; constraints: string; sources: IdeaSource[]; success_criteria?: string; preferences?: string }
export interface IdeaEvidence { id: string; source_id: string; quote: string; finding: string; limitation: string; source_support?: string; inference_validity?: string; location?: string; source_hash?: string; conditions?: string; assumptions?: string[]; interpretation?: string; span?: { start: number; end: number; coordinate: string }; coverage?: Record<string, unknown> }
export interface IdeaDirection {
  grounding?: 'source_linked' | 'provisional'; id: string; title: string; question: string; nearest_work: string; evidence_ids: string[]
  counterevidence: string[]; value: string; uncertainty: string; minimal_action: string
  expected_learning: string; feasibility: string; cost_risk: string
}
export interface IdeaResult {
  literature?: { summary: string; evidence: IdeaEvidence[]; gaps: string[] } | null
  ideas?: { directions: IdeaDirection[]; open_alternative: string } | null
  review?: { recommendation_id: string; reason: string; critiques: { direction_id: string; concerns: string[]; test_before_commit: string }[]; missing_evidence: string[]; comparison_summary: string; disposition?: 'ready' | 'retrieve_more' | 'revise_ideas' | 'needs_input'; followup_queries?: string[]; questions?: IdeaQuestion[] } | null
}
export interface IdeaDecisionInput {
  expected_revision: number; kind: 'select' | 'revise' | 'defer' | 'reject' | 'combine'; selected_id: string | null; feedback: string; selected_ids?: string[]; goal?: string; answers?: IdeaAnswer[]
}
export interface IdeaSession {
  deleted_at?: string | null
  id: string; revision: number; created_at: string; updated_at: string; brief: IdeaBrief
  status: 'draft' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'needs_input'
  phase: 'searching' | 'reading' | 'literature' | 'ideas' | 'review' | null; generation_id: string | null
  result: IdeaResult | null; decision: { kind: 'select' | 'revise' | 'defer' | 'reject' | 'combine' | 'followup'; selected_id?: string | null; mode?: string; answers?: IdeaAnswer[]; feedback: string; selected_ids?: string[]; goal?: string; at: string } | null
  brief_revision?: number
  decision_card?: { id: string; brief_revision: number; generation_id: string; options_version: number; evidence_versions: string[]; choice_needed: string; recommendation_id: string | null; reason: string; options: IdeaDirection[]; next_actions: { direction_id: string; action: string }[]; questions: IdeaQuestion[] } | null
  research?: IdeaResearch
  error: string | null; usage: Record<string, unknown>[]; events: Record<string, unknown>[]
}
export interface IdeaPaperResult { source: IdeaSource; paper: IdeaPaper | null; revision?: number; archived?: boolean; generation_id?: string | null; round?: number; packet_hash?: string; provenance_hash?: string }
export interface IdeaActivityPage { items: IdeaActivity[]; has_more: boolean; next_before: number | null }
export interface IdeaPage { items: IdeaSession[]; has_more: boolean }
export interface IdeaCreateInput { idempotency_key: string; brief: IdeaBrief }
export interface IdeaGenerateInput { idempotency_key: string; expected_revision: number }
export interface IdeaOriginalUnavailable { status: 'not_retained' | 'missing' | 'corrupt'; session_id?: string; source_id?: string; source_hash?: string; generation_id?: string; provenance_hash?: string }
export type IdeaOriginalSaveResult = IdeaOriginalUnavailable | { status: 'saved'; path: string; session_id: string; source_id: string; source_hash: string; generation_id: string; provenance_hash: string; content_hash: string; bytes: number; fetch_id: string }
export interface IdeaLabAPI {
  list(scope: WorkspaceProfileScope, trashed?: boolean): Promise<IdeaPage>
  trash(scope: WorkspaceProfileScope, id: string, input: { expected_revision: number }): Promise<IdeaSession>
  restore(scope: WorkspaceProfileScope, id: string, input: { expected_revision: number }): Promise<IdeaSession>
  get(scope: WorkspaceProfileScope, id: string): Promise<IdeaSession>
  paper(scope: WorkspaceProfileScope, id: string, sourceId: string, sourceHash?: string, generationId?: string): Promise<IdeaPaperResult>
  saveOriginal(scope: WorkspaceProfileScope, id: string, sourceId: string, sourceHash: string, generationId: string, provenanceHash: string): Promise<IdeaOriginalSaveResult | null>
  activities(scope: WorkspaceProfileScope, id: string, before?: number): Promise<IdeaActivityPage>
  history(scope: WorkspaceProfileScope, id: string): Promise<IdeaPage>
  create(scope: WorkspaceProfileScope, input: IdeaCreateInput): Promise<IdeaSession>
  generate(scope: WorkspaceProfileScope, id: string, input: IdeaGenerateInput): Promise<IdeaSession>
  cancel(scope: WorkspaceProfileScope, id: string, input: { expected_revision: number }): Promise<IdeaSession>
  followup(scope: WorkspaceProfileScope, id: string, input: IdeaFollowupInput): Promise<IdeaSession>
  decision(scope: WorkspaceProfileScope, id: string, input: IdeaDecisionInput): Promise<IdeaSession>
}

export function ideaSessionId(value: string): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new Error('Invalid Idea Lab session identifier.')
  return value
}

function object(value: unknown): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Idea Lab response.')
  return value as Record<string, any>
}
function strings(value: unknown): void {
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) throw new Error('Invalid Idea Lab text list.')
}
function fields(value: unknown, keys: string[]): void {
  const row = object(value)
  if (keys.some(key => typeof row[key] !== 'string')) throw new Error('Invalid Idea Lab text field.')
}
function rows(value: unknown): Record<string, any>[] {
  if (!Array.isArray(value)) throw new Error('Invalid Idea Lab list.')
  return value.map(object)
}

/** Parse returned data before it enters native UI state; never execute source text. */
export function parseIdeaSession(value: unknown): IdeaSession {
  const row = object(value)
  if (row.deleted_at !== undefined && row.deleted_at !== null && (typeof row.deleted_at !== 'string' || !row.deleted_at)) throw new Error('Invalid Idea Lab trash state.')
  ideaSessionId(row.id)
  if (!Number.isSafeInteger(row.revision) || row.revision < 0
    || !['draft', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'needs_input'].includes(row.status)
    || ![null, 'searching', 'reading', 'literature', 'ideas', 'review'].includes(row.phase)
    || !(row.generation_id === null || typeof row.generation_id === 'string')
    || !(row.error === null || typeof row.error === 'string')) throw new Error('Invalid Idea Lab session state.')
  fields(row, ['created_at', 'updated_at'])
  fields(row.brief, ['goal', 'hypothesis', 'constraints'])
  rows(row.brief.sources).forEach(source => fields(source, ['id', 'title', 'uri', 'text']))
  rows(row.usage); rows(row.events)
  if (row.decision !== null) {
    fields(row.decision, ['kind', 'feedback', 'at'])
    if (!['select', 'revise', 'defer', 'reject', 'combine', 'followup'].includes(row.decision.kind)
      || !(row.decision.selected_id === undefined || row.decision.selected_id === null || typeof row.decision.selected_id === 'string')) throw new Error('Invalid Idea Lab decision.')
  }
  if (row.result !== null) {
    const result = object(row.result)
    if (result.literature) {
      fields(result.literature, ['summary']); strings(result.literature.gaps)
      rows(result.literature.evidence).forEach(item => { fields(item, ['id', 'source_id', 'quote', 'finding', 'limitation']); if (item.conditions !== undefined) fields(item, ['conditions']); if (item.interpretation !== undefined) fields(item, ['interpretation']); if (item.assumptions !== undefined) strings(item.assumptions) })
    }
    if (result.ideas) {
      fields(result.ideas, ['open_alternative'])
      rows(result.ideas.directions).forEach(item => {
        fields(item, ['id', 'title', 'question', 'nearest_work', 'value', 'uncertainty', 'minimal_action', 'expected_learning', 'feasibility', 'cost_risk'])
        strings(item.evidence_ids); strings(item.counterevidence)
      })
    }
    if (result.review) {
      fields(result.review, ['recommendation_id', 'reason', 'comparison_summary']); strings(result.review.missing_evidence)
      if (result.review.followup_queries !== undefined) strings(result.review.followup_queries)
      if (result.review.questions !== undefined) rows(result.review.questions).forEach(question => { fields(question, ['id', 'question', 'why']); strings(question.options); if (typeof question.required !== 'boolean') throw new Error('Invalid Idea Lab question.') })
      rows(result.review.critiques).forEach(item => { fields(item, ['direction_id', 'test_before_commit']); strings(item.concerns) })
    }
  }
  if (row.research !== undefined) {
    const research = object(row.research)
    if (!['not_started', 'researching', 'ready_for_choice', 'evidence_limited', 'needs_input', 'failed', 'legacy_unsearched'].includes(research.readiness)) throw new Error('Invalid Idea Lab readiness.')
    for (const key of ['version', 'round', 'max_rounds']) if (!Number.isSafeInteger(research[key]) || research[key] < 0) throw new Error('Invalid Idea Lab budget.')
    if (research.stop_reason !== null && typeof research.stop_reason !== 'string') throw new Error('Invalid Idea Lab stop reason.')
    const limits = object(research.limits), used = object(research.used)
    for (const key of ['max_rounds', 'max_model_calls', 'max_sources', 'max_search_queries']) if (!Number.isSafeInteger(limits[key]) || limits[key] < 0) throw new Error('Invalid Idea Lab budget.')
    for (const key of ['model_calls', 'searches', 'reads', 'cache_hits']) if (!Number.isSafeInteger(used[key]) || used[key] < 0) throw new Error('Invalid Idea Lab usage.')
    rows(research.papers).forEach(paper => fields(paper, ['id', 'title', 'url', 'access', 'status']))
    rows(research.searches); rows(research.rounds)
    rows(research.activities).forEach(event => fields(event, ['agent', 'type', 'summary']))
    rows(research.agents).forEach(agent => { fields(agent, ['id', 'name', 'role', 'status']); if (agent.task !== null && typeof agent.task !== 'string') throw new Error('Invalid Idea Lab task.') })
    rows(research.questions).forEach(question => { fields(question, ['id', 'question', 'why']); strings(question.options); if (typeof question.required !== 'boolean') throw new Error('Invalid Idea Lab question.') })
    if (research.sources !== undefined) rows(research.sources).forEach(source => fields(source, ['id', 'title', 'uri', 'text']))
  }
  if (row.decision_card != null) { fields(row.decision_card, ['id', 'generation_id', 'choice_needed', 'reason']); strings(row.decision_card.evidence_versions); rows(row.decision_card.questions); rows(row.decision_card.options); rows(row.decision_card.next_actions) }
  return row as IdeaSession
}

export function parseIdeaPage(value: unknown): IdeaPage {
  const row = object(value)
  if (typeof row.has_more !== 'boolean') throw new Error('Invalid Idea Lab pagination.')
  return { items: rows(row.items).map(parseIdeaSession), has_more: row.has_more }
}

export function parseIdeaActivities(value: unknown): IdeaActivityPage {
  const row = object(value)
  if (typeof row.has_more !== 'boolean' || !(row.next_before === null || Number.isSafeInteger(row.next_before) && row.next_before > 0)) throw new Error('Invalid Idea Lab activity page.')
  rows(row.items).forEach(event => { fields(event, ['id', 'at', 'agent', 'type', 'summary']); if (!Number.isSafeInteger(event.seq) || event.seq < 1) throw new Error('Invalid Idea Lab activity cursor.') })
  return row as IdeaActivityPage
}

export function ideaSourceSegment(value: string): string {
  if (typeof value !== 'string' || !value || new TextEncoder().encode(value).byteLength > 256 || /[\\/?#%\u0000-\u0020]/.test(value) || value === '.' || value === '..') throw new Error('Invalid Idea Lab source identifier.')
  return encodeURIComponent(value)
}
export function parseIdeaPaper(value: unknown, expectedId: string, expectedHash?: string, expectedGeneration?: string): IdeaPaperResult {
  const row = object(value)
  fields(row.source, ['id', 'title', 'uri', 'text'])
  if (row.source.id !== expectedId || (expectedHash !== undefined && row.packet_hash !== expectedHash)) throw new Error('The returned source does not match this frozen evidence version.')
  if (expectedGeneration !== undefined && row.generation_id !== expectedGeneration) throw new Error('The returned source does not match this frozen generation.')
  if (row.generation_id !== undefined && row.generation_id !== null && (typeof row.generation_id !== 'string' || !/^[a-f0-9]{32}$/.test(row.generation_id)) || row.provenance_hash !== undefined && (typeof row.provenance_hash !== 'string' || !/^[a-f0-9]{64}$/.test(row.provenance_hash))) throw new Error('Invalid Idea Lab source provenance.')
  if (row.paper !== null) { fields(row.paper, ['id', 'title', 'url', 'access', 'status']); if (row.paper.id !== expectedId) throw new Error('Invalid Idea Lab reading receipt.') }
  if (row.archived !== undefined && typeof row.archived !== 'boolean' || row.revision !== undefined && (!Number.isSafeInteger(row.revision) || row.revision < 0)) throw new Error('Invalid Idea Lab source version.')
  return row as IdeaPaperResult
}
