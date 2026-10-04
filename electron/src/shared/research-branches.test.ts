import { describe, expect, it } from 'vitest'
import { branchMayRun, branchResult, parseLabBranchSet } from './research-branches'
import { branchSetFixture } from './research-branches.fixture'
import { labFixture, labRoundFixture } from './research-lab.fixture'
import { parseLabCampaign } from './research-lab'

describe('Research branch projection boundaries', () => {
  it('keeps an independent branch eligible while A awaits an answer and C awaits its fixed result', () => {
    const set = parseLabBranchSet(branchSetFixture(), 'campaign-one')
    expect(set.branches.map(branch => branchMayRun(set, branch))).toEqual([false, true, false])
    const campaign = { ...labFixture(), revision: 99, branch_set: set }
    expect(parseLabCampaign(campaign).branch_set).toEqual(set)
    expect(branchMayRun(set, set.branches[1])).toBe(true)
  })
  it('treats omitted, unknown or inconsistent admission state as unavailable rather than deriving permission', () => {
    for (const patch of [{ gate: undefined }, { scope_status: undefined }, { scope_status: 'unknown' },
      { status: 'unrecognized-new-status' }, { current_scope: null }, { gate: { allowed: true, blockers: [{ code: 'required_answer' }], dependency_refs: [] } }]) {
      const set = branchSetFixture(), branch = { ...set.branches[1], ...patch }
      const parsed = parseLabBranchSet({ ...set, branches: [branch] }, 'campaign-one')
      expect(branchMayRun(parsed, parsed.branches[0])).toBe(false)
    }
  })
  it('never silently binds an unrelated last global run to a branch', () => {
    const campaign = labFixture(), set = branchSetFixture(), selected = set.branches[1], old = labRoundFixture(), unrelated = { ...labRoundFixture(), run: { ...labRoundFixture().run, id: 'unrelated-later-run' } }
    campaign.rounds = [old, unrelated]
    selected.latest_result = { run_id: old.run.id, round_artifact: 'e'.repeat(64), observation_artifact: old.run.observation_artifact, input_artifact: old.run.input_artifact, status: 'completed', qc_passed: true, root_context_hash: selected.root_context_hash, context_hash: 'f'.repeat(64), current: true }
    expect(branchResult(campaign, selected)?.run.id).toBe(old.run.id)
    campaign.rounds = [unrelated]
    expect(branchResult(campaign, selected)).toBeUndefined()
  })
  it('rejects cross-campaign ownership, executable dispatch flags and malformed questions', () => {
    const set = branchSetFixture()
    expect(() => parseLabBranchSet(set, 'another-campaign')).toThrow()
    expect(() => parseLabBranchSet({ ...set, automatic_dispatch: true }, 'campaign-one')).toThrow()
    expect(() => parseLabBranchSet({ ...set, branches: [...set.branches, set.branches[0]] }, 'campaign-one')).toThrow()
    set.branches[0].questions[0].prompt_hash = '../file'
    expect(() => parseLabBranchSet(set, 'campaign-one')).toThrow()
  })
})
