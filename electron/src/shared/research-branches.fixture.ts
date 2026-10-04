import type { LabBranch, LabBranchSet } from './research-branches'
import { labFixture } from './research-lab.fixture'

export const branchFixture = (id = 'branch-b'): LabBranch => ({
  id, revision: 1, title: `Question ${id}`, question: 'Does this supplied source contain the quote?', success_criterion: 'Record the occurrence and limits.',
  methods: ['exact_quote'], source_ids: ['source-one'], input_artifact: 'a'.repeat(64), root_context_hash: 'b'.repeat(64),
  control: 'active', status: 'planned', current_plan: { ...labFixture().current_plan!, selection_origin: 'human' },
  latest_result: null, questions: [], depends_on: [], gate: { allowed: true, blockers: [], dependency_refs: [] },
  current_scope: { sha256: 'c'.repeat(64) }, scope_status: 'current', owned_work: null, post_outcome: false
})
export const branchSetFixture = (): LabBranchSet => {
  const a = branchFixture('a'), b = branchFixture('b'), c = branchFixture('c')
  a.questions = [{ id: 'scope', prompt: 'Which supplied scope should we use?', required: true, prompt_hash: 'd'.repeat(64), needs_answer: true, answer: null }]
  a.gate = { allowed: false, blockers: [{ code: 'required_answer', question_id: 'scope' }], dependency_refs: [] }; a.scope_status = 'blocked'; a.current_scope = null
  c.depends_on = [{ branch_id: 'a', require: 'qc_passed' }]
  c.gate = { allowed: false, blockers: [{ code: 'dependency_pending', branch_id: 'a' }], dependency_refs: [] }; c.scope_status = 'blocked'; c.current_scope = null
  return { schema_version: 1, campaign_id: 'campaign-one', authority_epoch: 1, root_control: 'active', branches: [a, b, c], historical_unassigned_run_ids: [], automatic_dispatch: false }
}
