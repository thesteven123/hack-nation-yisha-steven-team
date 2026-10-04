import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import type { AgentsDockAPI } from '@shared/ipc'
import type { LabCampaign, ResearchLabAPI } from '@shared/research-lab'
import { labFixture, labRoundFixture, labSeedFixture, labProtocolFixture, labHypothesisFixture } from '@shared/research-lab.fixture'
import { ResearchLab } from './ResearchLab'
import { emptyInputs, parseInputs, fromInputs, sharedCorrectionSource } from './ResearchInputs'
import { ResearchLimits } from './ResearchResults'
import { branchSetFixture } from '@shared/research-branches.fixture'
const scope = { profileId: 'test', profileGeneration: 1, serverIdentity: 'test-server' }
let api: ResearchLabAPI, campaign: LabCampaign
beforeEach(() => {
  localStorage.clear(); setLocale('en'); campaign = labFixture()
  api = { trash: vi.fn(), restore: vi.fn(), reconcileDependencies: vi.fn().mockImplementation(async () => campaign), protocol: vi.fn().mockResolvedValue(labProtocolFixture()), export: vi.fn().mockResolvedValue({ path: "research-campaign.json", campaign_id: "campaign-one", bundle_sha256: "c".repeat(64), bytes: 1024 }), list: vi.fn().mockResolvedValue({ items: [], has_more: false }), capabilities: vi.fn().mockResolvedValue({ schema_version: 1, adapters: [campaign.adapter], execution: {} }), get: vi.fn().mockImplementation(async () => campaign), ideaSeed: vi.fn().mockResolvedValue(labSeedFixture()), create: vi.fn().mockResolvedValue(campaign), decision: vi.fn().mockImplementation(async (_scope, _id, input) => { campaign = { ...campaign, revision: campaign.revision + 1, status: input.kind === 'stop' ? 'stopped' : input.kind === 'defer' ? 'needs_input' : 'planned', stop_reason: input.kind === 'defer' ? 'human_deferred' : null, current_plan: input.kind === 'select' ? { ...campaign.current_plan!, selected_action_id: input.selected_action_id, selection_origin: 'human' } : campaign.current_plan }; return campaign }), run: vi.fn().mockImplementation(async () => { const round = labRoundFixture(); campaign = { ...campaign, revision: campaign.revision + 1, status: 'awaiting_next', rounds: [round], review: round.review }; return campaign }), advance: vi.fn().mockImplementation(async () => { campaign = { ...campaign, revision: campaign.revision + 1, status: 'planned', current_plan: { ...campaign.current_plan!, round: 2, selection_origin: 'policy' } }; return campaign }), correctInputs: vi.fn().mockImplementation(async () => ({ ...campaign, revision: campaign.revision + 1, status: 'planned' })), history: vi.fn().mockResolvedValue({ items: [], has_more: false }), artifact: vi.fn().mockResolvedValue({ sha256: 'a'.repeat(64), media_type: 'application/json', content: labSeedFixture().inputs }) }
  api.trash = vi.fn().mockResolvedValue({ ...campaign, revision: 2, deleted_at: '2026-10-04T13:00:00Z' })
  api.restore = vi.fn().mockResolvedValue({ ...campaign, revision: 3, deleted_at: null })
  window.agentsDock = { researchLab: api } as AgentsDockAPI
})
afterEach(cleanup)
const open = async () => { vi.mocked(api.list).mockResolvedValue({ items: [campaign], has_more: false }); render(<ResearchLab scope={scope} onClose={() => {}} />); fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ })); await screen.findByRole('button', { name: 'Run the selected frozen action' }); await waitFor(() => expect(screen.queryByText('Working on this request…')).not.toBeInTheDocument()) }
const fill = () => {
  fireEvent.change(screen.getByLabelText('Research question / goal'), { target: { value: 'Is the qualification present?' } })
  fireEvent.change(screen.getByLabelText('What would count as a useful result?'), { target: { value: 'Locate it and inspect context' } })
  fireEvent.change(screen.getByLabelText('Exact quote to check'), { target: { value: 'Only these cases.' } })
  fireEvent.change(screen.getByLabelText('Source title'), { target: { value: 'Supplied note' } })
  fireEvent.change(screen.getByLabelText('Available source text'), { target: { value: 'Only these cases. Others untested.' } })
}
describe('Research workspace', () => {
  it('confirms a deletion, closes the selected research and restores the same record', async () => {
    let deleted = false
    vi.mocked(api.list).mockImplementation(async (_scope, _before, trash) => ({ items: Boolean(trash) === deleted ? [deleted ? { ...campaign, revision: 2, deleted_at: '2026-10-04T13:00:00Z' } : campaign] : [], has_more: false }))
    vi.mocked(api.trash).mockImplementation(async () => { deleted = true; return { ...campaign, revision: 2, deleted_at: '2026-10-04T13:00:00Z' } })
    vi.mocked(api.restore).mockImplementation(async () => { deleted = false; return { ...campaign, revision: 3, deleted_at: null } })
    render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ }))
    await screen.findByRole('button', { name: 'Run the selected frozen action' })
    fireEvent.click(screen.getByRole('button', { name: /^Delete research:/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(api.trash).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /^Delete research:/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Move to Trash' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Does the supplied text include/ })).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Run the selected frozen action' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Trash' }))
    fireEvent.click(await screen.findByRole('button', { name: /^Restore research:/ }))
    await screen.findByText('Trash is empty.')
    fireEvent.click(screen.getByRole('button', { name: 'Active research' }))
    expect(await screen.findByRole('button', { name: /^Does the supplied text include/ })).toBeInTheDocument()
    expect(api.run).not.toHaveBeenCalled()
  })
  it('shows an active-role deletion failure without losing the selected research', async () => {
    await open()
    vi.mocked(api.trash).mockRejectedValue(new Error('Stop the active native role before deleting this research.'))
    fireEvent.click(screen.getByRole('button', { name: /^Delete research:/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Move to Trash' }))
    await screen.findAllByText('Stop the active native role before deleting this research.')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeInTheDocument()
  })
  it('reopens branches with their dedicated admission read and does not expose the unscoped run path', async () => {
    campaign.branch_set = branchSetFixture()
    api.branches = { get: vi.fn().mockImplementation(async () => campaign), enable: vi.fn(), plan: vi.fn(), answers: vi.fn(), decision: vi.fn(), control: vi.fn(), run: vi.fn().mockImplementation(async () => campaign) }
    await open()
    await waitFor(() => expect(api.branches!.get).toHaveBeenCalled())
    const buttons = screen.getAllByRole('button', { name: 'View this question' }); fireEvent.click(buttons[0])
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Run the selected frozen action' }))
    await waitFor(() => expect(api.branches!.run).toHaveBeenCalledWith(scope, campaign.id, 'b', expect.any(Object)))
    expect(api.run).not.toHaveBeenCalled(); expect(api.decision).not.toHaveBeenCalled(); expect(api.advance).not.toHaveBeenCalled()
  })
  it('creates hypothesis-led work with a structured set and preserves its request after an uncertain response', async () => {
    vi.mocked(api.create).mockRejectedValue(new Error('Save response unavailable'))
    render(<ResearchLab scope={scope} onClose={() => {}} />)
    await waitFor(() => expect(api.capabilities).toHaveBeenCalled())
    fill()
    expect(screen.queryByText('Structured hypotheses (optional)')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Starting point'), { target: { value: 'hypothesis' } })
    fireEvent.click(screen.getByText('Structured hypotheses (optional)'))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Record a versioned hypothesis set' }))
    fireEvent.change(screen.getByLabelText('Hypothesis statement'), { target: { value: 'The qualification is restricted.' } })
    fireEvent.change(screen.getByLabelText('Where this explanation applies'), { target: { value: 'Supplied cases only.' } })
    fireEvent.change(screen.getByLabelText('What it predicts (one per line)'), { target: { value: 'The exact quote occurs.' } })
    fireEvent.change(screen.getByLabelText('What would weaken it (one per line)'), { target: { value: 'The quote does not occur.' } })
    fireEvent.change(screen.getByLabelText('Other explanations or unknowns left open'), { target: { value: 'An omitted qualification may matter.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByText('Save response unavailable')
    const first = vi.mocked(api.create).mock.calls[0][1]
    expect(first.brief.hypothesis).toBe('')
    expect(first.hypothesis_set?.candidates[0].supporting_evidence).toEqual([])
    cleanup(); render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.create).mock.calls[1][1]).toEqual(first)
    expect(api.run).not.toHaveBeenCalled()
  })
  it('keeps a frozen set unchanged when an ordinary goal revision does not edit it', async () => {
    campaign.hypothesis_set = labHypothesisFixture(); campaign.hypothesis_set_artifact = 'e'.repeat(64)
    await open(); fireEvent.click(screen.getByText('Revise, correct inputs, or end'))
    fireEvent.change(screen.getByLabelText('Revised goal'), { target: { value: 'A refined question' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save goal and freeze a new plan' }))
    await waitFor(() => expect(api.decision).toHaveBeenCalled())
    expect(vi.mocked(api.decision).mock.calls[0][2]).not.toHaveProperty('hypothesis_set')
    expect(api.artifact).not.toHaveBeenCalled()
  })
  it('allows an explicit hypothesis-set removal from an exploratory campaign without reading source bodies', async () => {
    campaign.hypothesis_set = labHypothesisFixture(); campaign.input_summary.sources = [{ id: 'source-one', title: 'Source one' }]
    await open(); fireEvent.click(screen.getByText('Revise, correct inputs, or end'))
    fireEvent.click(screen.getByText('Structured hypotheses (optional)'))
    fireEvent.click(screen.getByRole('button', { name: 'Edit or introduce a hypothesis set' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Record a versioned hypothesis set' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save goal and freeze a new plan' }))
    await waitFor(() => expect(api.decision).toHaveBeenCalled())
    expect(vi.mocked(api.decision).mock.calls[0][2]).toMatchObject({ kind: 'revise', hypothesis_set: null })
    expect(api.artifact).not.toHaveBeenCalled(); expect(api.run).not.toHaveBeenCalled()
  })

  it('counts omitted rounds in the budget and retrieves only the explicitly selected saved round', async () => {
    campaign.total_rounds = 3; campaign.has_more_rounds = true
    campaign.rounds = [{ ...labRoundFixture(), index: 3, run: { ...labRoundFixture().run, id: 'run-three' } }]
    campaign.omitted_rounds = [{ index: 1, run_id: 'run-one', artifact: 'e'.repeat(64) }]
    const earlier = labRoundFixture(); earlier.observation.data.quote = 'Earlier exact evidence.'
    vi.mocked(api.artifact).mockResolvedValue({ sha256: 'e'.repeat(64), media_type: 'application/json', content: earlier })
    await open()
    expect(screen.getByText(/4 action slots and 0 rounds remaining/)).toBeVisible()
    expect(api.artifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Earlier results and decisions'))
    expect(screen.getByText(/Showing 1 of 3 rounds/)).toBeVisible()
    fireEvent.click(screen.getByText('Round 1 result'))
    fireEvent.click(screen.getByRole('button', { name: 'Load this earlier round' }))
    await screen.findByText('Earlier exact evidence.')
    expect(api.artifact).toHaveBeenCalledWith(scope, campaign.id, 'e'.repeat(64))
    expect(api.artifact).toHaveBeenCalledOnce()
  })
  it('keeps projected decision history readable through scoped full event artifacts', async () => {
    campaign.total_decisions = 8; campaign.has_more_decisions = true; campaign.decisions = [{ kind: 'select' }]
    vi.mocked(api.history).mockResolvedValue({ items: [{ revision: 3, at: 'saved-at', event: 'human_decision', event_artifact: 'e'.repeat(64), brief: campaign.brief, status: 'planned' }], has_more: false })
    vi.mocked(api.artifact).mockResolvedValue({ sha256: 'e'.repeat(64), media_type: 'application/json', content: { feedback: 'Exact earlier human rationale' } })
    await open(); fireEvent.click(screen.getByText('Earlier results and decisions'))
    fireEvent.click(screen.getByRole('button', { name: 'Load saved revision history' }))
    fireEvent.click(await screen.findByText(/Revision 3/))
    expect(api.artifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Read this saved change in full' }))
    await screen.findByText(/Exact earlier human rationale/)
    expect(api.artifact).toHaveBeenCalledWith(scope, campaign.id, 'e'.repeat(64))
  })

  it('keeps an acknowledged selection and fails closed when fetching current dependency status fails', async () => {
    await open()
    vi.mocked(api.get).mockRejectedValueOnce(new Error('Source status unavailable'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Choose this action' })[0])
    await screen.findByText('Saved, but the current source status could not be loaded. Refresh before running or requesting a model. Do not submit the saved change again.')
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeDisabled()
    expect(api.decision).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled())
    expect(api.decision).toHaveBeenCalledOnce()
  })
  it('shows both action and round ceilings when one action slot cannot create another round', async () => {
    campaign.rounds = [1, 2, 3].map(index => ({ ...labRoundFixture(), index, run: { ...labRoundFixture().run, id: `run-${index}` } }))
    campaign.budget.remaining_actions = 1
    await open()
    expect(screen.getByText(/1 action slots and 0 rounds remaining/)).toBeVisible()
  })
  it('requires an explicit single-source shared correction and keeps its retry identity after an uncertain response', async () => {
    const inputs = labSeedFixture().inputs
    inputs.sources[0].provenance = { kind: 'idea_frozen_packet', decision_revision: 5 }
    vi.mocked(api.artifact).mockResolvedValue({ sha256: campaign.input_artifact, media_type: 'application/json', content: inputs })
    vi.mocked(api.correctInputs).mockRejectedValue(new Error('Response lost'))
    await open(); fireEvent.click(screen.getByText('Revise, correct inputs, or end'))
    fireEvent.click(screen.getByRole('button', { name: 'Correct source text or data' }))
    const checkbox = await screen.findByRole('checkbox', { name: 'This correction also affects other research citing this source version' })
    expect(checkbox).not.toBeChecked(); expect(checkbox).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Available source text'), { target: { value: 'Corrected literal text.' } })
    expect(checkbox).toBeEnabled(); expect(checkbox).not.toBeChecked()
    fireEvent.click(checkbox)
    fireEvent.change(screen.getByLabelText('Why are these inputs being corrected?'), { target: { value: 'An exact transcription correction.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction and freeze a new plan' }))
    await screen.findByText('Response lost')
    expect(api.correctInputs).toHaveBeenCalledWith(scope, campaign.id, expect.objectContaining({ shared_source_id: 'source-one' }))
    const first = vi.mocked(api.correctInputs).mock.calls[0][2]
    cleanup()
    await open(); fireEvent.click(screen.getByText('Revise, correct inputs, or end')); fireEvent.click(screen.getByRole('button', { name: 'Correct source text or data' }))
    expect(await screen.findByRole('checkbox', { name: 'This correction also affects other research citing this source version' })).toBeChecked()
    expect(screen.getByLabelText('Available source text')).toHaveValue('Corrected literal text.')
    fireEvent.click(screen.getByRole('button', { name: 'Save correction and freeze a new plan' }))
    await waitFor(() => expect(api.correctInputs).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.correctInputs).mock.calls[1][2]).toEqual(first)
  })
  it('never selects a shared source from multiple edited packets, removed sources, or untrusted provenance', () => {
    const inputs = labSeedFixture().inputs
    inputs.sources[0].provenance = { kind: 'idea_frozen_packet', decision_revision: 5 }
    inputs.sources.push({ ...inputs.sources[0], id: 'second' })
    const draft = fromInputs(structuredClone(inputs)); draft.sources[0].text += 'edit'
    expect(sharedCorrectionSource(inputs.sources, draft)).toBe('source-one')
    draft.sources[1].coverage = 'full_text'; expect(sharedCorrectionSource(inputs.sources, draft)).toBeNull(); draft.sources[1].coverage = 'excerpt'
    draft.sources[1].text += 'edit'; expect(sharedCorrectionSource(inputs.sources, draft)).toBeNull()
    draft.sources.pop(); expect(sharedCorrectionSource(inputs.sources, draft)).toBeNull()
    inputs.sources.pop(); delete inputs.sources[0].provenance; expect(sharedCorrectionSource(inputs.sources, draft)).toBeNull()
  })
  it('synchronizes dependency receipts explicitly and blocks execution until run_allowed becomes true', async () => {
    campaign.current_plan!.selection_origin = 'human'
    campaign.dependency_status = { state: 'unregistered', run_allowed: false, current_input_blocked: true, affected_claim_ids: [], pending_intents: [], corrections: [], pending_deliveries: 0 }
    vi.mocked(api.reconcileDependencies).mockImplementation(async () => ({ ...campaign, dependency_status: { ...campaign.dependency_status!, state: 'current', run_allowed: true, current_input_blocked: false } }))
    await open()
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeDisabled()
    expect(api.reconcileDependencies).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Synchronize source status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled())
    expect(api.reconcileDependencies).toHaveBeenCalledWith(scope, campaign.id)
    expect(api.run).not.toHaveBeenCalled()
  })

  it('discloses bounded Idea source imports and keeps decision revision without adding unknown create fields', async () => {
    const seed = labSeedFixture()
    seed.import_coverage = { referenced_sources: 6, imported_sources: 5, omitted_sources: 1, omitted_source_ids: ['sixth'], omitted_evidence_ids: ['E6'], selection_policy: 'first_seen_bounded_subset', complete: false, scope: 'selected_direction_source_versions_only', limitation: 'Some sources were omitted.' }
    vi.mocked(api.ideaSeed).mockResolvedValue(seed)
    render(<ResearchLab scope={scope} initialIdeaId="idea-one" onClose={() => {}} />)
    await screen.findByText(/Imported 5 of 6 referenced sources/)
    expect(screen.getByText(/Omitted sources and counterevidence were not assessed/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await waitFor(() => expect(api.create).toHaveBeenCalled())
    const input = vi.mocked(api.create).mock.calls[0][1]
    expect(input.origin?.decision_revision).toBe(5)
    expect(input).not.toHaveProperty('import_coverage'); expect(input).not.toHaveProperty('importCoverage')
  })
  it('shows at most three literal limitations, translates fixed coverage text, and keeps the rest inspectable', () => {
    setLocale('zh-CN')
    const values = ['Text extraction only; figures, image tables, equation layout and scanned text were not visually inspected.', 'Only the declared text spans entered this task; omitted sections require a targeted follow-up read.', 'Quote occurrence does not validate a scientific claim', 'A specific human-authored limitation.']
    render(<ResearchLimits values={values} />)
    expect(screen.getAllByRole('listitem').filter(node => !node.closest('details'))).toHaveLength(3)
    expect(screen.getByText('仅使用已标明的文本片段；被省略的部分仍需定向补读。')).toBeVisible()
    expect(screen.getByText('A specific human-authored limitation.')).not.toBeVisible()
    fireEvent.click(screen.getByText('其余限制（1）'))
    expect(screen.getByText('A specific human-authored limitation.')).toBeVisible()
  })
  it('reads a versioned protocol and saves only the open campaign after explicit clicks', async () => {
    await open()
    expect(api.protocol).not.toHaveBeenCalled(); expect(api.export).not.toHaveBeenCalled()
    expect(screen.getByText('Frozen rules and protocol summaries').closest('details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Frozen rules and protocol summaries'))
    fireEvent.click(screen.getByRole('button', { name: 'Read planning summary v0.5' }))
    await screen.findByText('Freeze rules before observation.')
    expect(api.protocol).toHaveBeenCalledWith(scope, 'planning-v0.5')
    expect(api.protocol).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByText('Export this research')); fireEvent.click(screen.getByRole('button', { name: 'Save core research JSON…' }))
    await screen.findByText('Saved to research-campaign.json')
    expect(api.export).toHaveBeenCalledWith(scope, 'campaign-one')
    vi.mocked(api.export).mockResolvedValueOnce(null)
    fireEvent.click(screen.getByRole('button', { name: 'Save core research JSON…' }))
    await waitFor(() => expect(screen.queryByText('Saved to research-campaign.json')).not.toBeInTheDocument())
  })
  it('resets scroll after navigation but preserves position across same-campaign refresh', async () => {
    render(<ResearchLab scope={scope} onClose={() => {}} />); fill()
    const content = screen.getByRole('main'); content.scrollTop = 700
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByText('Round 1: frozen plan')
    expect(content.scrollTop).toBe(0)
    content.scrollTop = 240; fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText('Working on this request…')).not.toBeInTheDocument())
    expect(content.scrollTop).toBe(240)
    fireEvent.click(screen.getByRole('button', { name: 'New research' })); expect(content.scrollTop).toBe(0)
  })
  it('creates a frozen plan only, records a real selection, runs once, and plans the next round without executing', async () => {
    render(<ResearchLab scope={scope} onClose={() => {}} />); await waitFor(() => expect(api.list).toHaveBeenCalledOnce())
    expect(api.create).not.toHaveBeenCalled(); expect(api.run).not.toHaveBeenCalled()
    fill(); fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByText('Round 1: frozen plan'); expect(api.run).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeDisabled()
    fireEvent.change(screen.getAllByLabelText('Your decision rationale / feedback')[0], { target: { value: 'Use the second supplied source first.' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Choose this action' })[1])
    await waitFor(() => expect(api.decision).toHaveBeenCalledWith(scope, campaign.id, expect.objectContaining({ kind: 'select', selected_action_id: 'action-two', feedback: 'Use the second supplied source first.' })))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Run the selected frozen action' }))
    await screen.findByText('The exact quote occurs in this supplied text.')
    expect(screen.getByText('Cannot determine')).toBeInTheDocument()
    expect(api.advance).not.toHaveBeenCalled(); expect(api.artifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the next plan' }))
    await screen.findByText('Round 2: frozen plan'); expect(api.run).toHaveBeenCalledOnce(); expect(api.advance).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeDisabled()
  })
  it('requires hypothesis for hypothesis entry and validates paired numeric data', async () => {
    render(<ResearchLab scope={scope} onClose={() => {}} />); fill()
    fireEvent.change(screen.getByLabelText('Starting point'), { target: { value: 'hypothesis' } })
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByRole('alert'); expect(api.create).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Hypothesis summary (optional)'), { target: { value: 'The difference exceeds one unit.' } })
    fireEvent.change(screen.getByLabelText('Executable method family'), { target: { value: 'paired_numeric' } })
    fireEvent.change(screen.getByLabelText('Baseline values'), { target: { value: '1, 2, 3, 4' } })
    fireEvent.change(screen.getByLabelText('Comparison values (paired in order)'), { target: { value: '3, 4, 5, 6' } })
    fireEvent.change(screen.getByLabelText('Measurement unit'), { target: { value: 'points' } })
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(scope, expect.objectContaining({ entry: 'hypothesis', adapter_id: 'paired_numeric', inputs: { baseline: [1, 2, 3, 4], treatment: [3, 4, 5, 6], unit: 'points', minimum_effect: 0 } })))
    expect(() => parseInputs('paired_numeric', { ...emptyInputs(), baseline: '1 2', treatment: '3 Infinity', unit: 'x' })).toThrow()
  })
  it('retains exact idempotency after a lost create acknowledgement', async () => {
    vi.mocked(api.create).mockRejectedValueOnce(new Error('Connection lost after request')).mockResolvedValueOnce(campaign)
    render(<ResearchLab scope={scope} onClose={() => {}} />); fill(); fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByText('Connection lost after request'); fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await screen.findByText('Round 1: frozen plan')
    expect(vi.mocked(api.create).mock.calls[0][1]).toEqual(vi.mocked(api.create).mock.calls[1][1])
  })
  it('imports only the server-validated saved Idea choice and does not execute it', async () => {
    render(<ResearchLab scope={scope} initialIdeaId="idea-one" onClose={() => {}} />)
    await waitFor(() => expect(screen.getByLabelText('Research question / goal')).toHaveValue('Saved direction question'))
    expect(api.ideaSeed).toHaveBeenCalledWith(scope, 'idea-one'); expect(api.create).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Available source text')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Research question / goal'), { target: { value: 'My revised question' } })
    fireEvent.click(screen.getByRole('button', { name: 'Freeze the first plan' }))
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(scope, expect.objectContaining({ origin: labSeedFixture().origin, inputs: labSeedFixture().inputs, brief: expect.objectContaining({ goal: 'My revised question' }) })))
    expect(api.run).not.toHaveBeenCalled()
  })
  it('preserves late response ownership when the server changes', async () => {
    let resolve!: (value: LabCampaign) => void
    vi.mocked(api.list).mockResolvedValue({ items: [campaign], has_more: false }); vi.mocked(api.get).mockReturnValue(new Promise(r => { resolve = r }))
    const view = render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ }))
    view.rerender(<ResearchLab scope={{ ...scope, serverIdentity: 'other-server' }} onClose={() => {}} />)
    await act(async () => resolve(campaign))
    expect(screen.queryByText('Round 1: frozen plan')).not.toBeInTheDocument()
  })
  it('loads frozen input on explicit correction and sends exact reason; retains old claims as invalidated', async () => {
    const round = labRoundFixture(); campaign = { ...campaign, status: 'awaiting_next', rounds: [round], review: round.review, claims: [{ id: 'claim-one', text: round.analysis.claim, status: 'needs_revalidation', source_support: 'supported', inference_validity: 'cannot_determine', conditions: [], input_artifact: 'a'.repeat(64), run_id: round.run.id }] }
    vi.mocked(api.list).mockResolvedValue({ items: [campaign], has_more: false })
    render(<ResearchLab scope={scope} onClose={() => {}} />); fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ }))
    await screen.findByText('The goal or inputs supporting this conclusion changed. It needs revalidation.')
    expect(api.artifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Revise, correct inputs, or end')); fireEvent.click(screen.getByRole('button', { name: 'Correct source text or data' }))
    await screen.findByLabelText('Why are these inputs being corrected?')
    fireEvent.change(screen.getByLabelText('Available source text'), { target: { value: 'Only these cases. Full context now supplied.' } })
    fireEvent.change(screen.getByLabelText('Why are these inputs being corrected?'), { target: { value: 'Earlier excerpt omitted the next sentence.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction and freeze a new plan' }))
    await waitFor(() => expect(api.correctInputs).toHaveBeenCalledWith(scope, campaign.id, expect.objectContaining({ reason: 'Earlier excerpt omitted the next sentence.', inputs: expect.objectContaining({ sources: [expect.objectContaining({ text: 'Only these cases. Full context now supplied.' })] }) })))
    expect(api.run).not.toHaveBeenCalled()
  })
  it('records defer, resumes the same plan, and stops without model calls or polling', async () => {
    await open(); fireEvent.click(screen.getByRole('button', { name: 'Defer this plan' }))
    await screen.findByRole('button', { name: 'Resume the deferred plan' })
    fireEvent.click(screen.getByRole('button', { name: 'Resume the deferred plan' }))
    await screen.findByText('Round 2: frozen plan')
    fireEvent.click(screen.getByText('Revise, correct inputs, or end')); fireEvent.click(screen.getByRole('button', { name: 'End this research' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Run the selected frozen action' })).not.toBeInTheDocument())
    expect(api.run).not.toHaveBeenCalled(); expect(api.get).toHaveBeenCalledTimes(4)
    expect(api.decision).toHaveBeenLastCalledWith(scope, campaign.id, expect.objectContaining({ kind: 'stop' }))
  })
  it('does not misstate a failed QC result as accepted scientific support', async () => {
    const round = labRoundFixture(); round.qc.passed = false; round.analysis.source_support = 'cannot_determine'
    campaign = { ...campaign, status: 'needs_input', rounds: [round], review: { ...round.review, next_action: 'needs_input' } }
    vi.mocked(api.list).mockResolvedValue({ items: [campaign], has_more: false }); render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ }))
    await screen.findByText('Quality checks failed. This result cannot support the research claim.')
    expect(screen.queryByRole('button', { name: 'Freeze the next plan' })).not.toBeInTheDocument()
    expect(screen.getByText('Technical records').closest('details')).not.toHaveAttribute('open')
  })
  it('loads earlier campaigns and history only on request and deduplicates page boundaries', async () => {
    const older = { ...campaign, id: 'older-research', brief: { ...campaign.brief, goal: 'An older question' } }
    vi.mocked(api.list).mockResolvedValueOnce({ items: [campaign], has_more: true, next_cursor: 'more_campaigns' }).mockResolvedValueOnce({ items: [campaign, older], has_more: false, next_cursor: null })
    vi.mocked(api.history).mockResolvedValueOnce({ items: [{ revision: 4, at: '', event: 'first', brief: campaign.brief, status: 'planned' }], has_more: true, next_cursor: 'older_revisions' }).mockResolvedValueOnce({ items: [{ revision: 4, at: '', event: 'first', brief: campaign.brief, status: 'planned' }, { revision: 2, at: '', event: 'older', brief: campaign.brief, status: 'planned' }], has_more: false })
    render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more research' }))
    await screen.findByRole('button', { name: /^An older question/ }); expect(api.list).toHaveBeenLastCalledWith(scope, 'more_campaigns')
    expect(screen.getAllByRole('button', { name: /^Does the supplied text include/ })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: /^Does the supplied text include/ }))
    await screen.findByRole('button', { name: 'Run the selected frozen action' }); expect(api.history).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Earlier results and decisions')); fireEvent.click(screen.getByRole('button', { name: 'Load saved revision history' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Load earlier revisions' }))
    await screen.findByText(/Revision 2/); expect(screen.getAllByText(/Revision 4/)).toHaveLength(1)
    expect(api.history).toHaveBeenLastCalledWith(scope, campaign.id, 'older_revisions')
  })
  it('marks post-observation planning and distinguishes changed inputs from independent confirmation', async () => {
    campaign.current_plan!.candidates[0].research_mode = 'post_outcome_exploratory'
    campaign.comparisons = [{ id: 'comparison-one', run_ids: ['run-one', 'run-two'], rule: 'Changed input hashes', comparability: 'changed_inputs_not_like_for_like', eligible_for_pooled_confirmation: false, source_support: { before: 'supported', after: 'cannot_determine' }, inference_validity: { before: 'cannot_determine', after: 'cannot_determine' }, changed_observation_fields: ['match_count'], limitations: ['Different inputs'], unresolved: ['No new independent evidence'], artifact: 'e'.repeat(64) }]
    await open()
    expect(screen.getByText(/This plan was revised after observing results/)).toBeInTheDocument()
    expect(screen.getByText('Inputs changed. Differences are not a like-for-like effect comparison.')).toBeInTheDocument()
    expect(screen.getByText('These analyses do not combine into independent confirmation.')).toBeInTheDocument()
    expect(api.artifact).not.toHaveBeenCalled()
  })
  it('retains explicit revised hypothesis and criterion without automatically running', async () => {
    await open(); fireEvent.click(screen.getByText('Revise, correct inputs, or end'))
    fireEvent.change(screen.getByLabelText('Revised hypothesis'), { target: { value: 'The result applies only to this subgroup.' } })
    fireEvent.change(screen.getByLabelText('Revised useful-result criterion'), { target: { value: 'Locate the subgroup qualification.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save goal and freeze a new plan' }))
    await waitFor(() => expect(api.decision).toHaveBeenCalledWith(scope, campaign.id, expect.objectContaining({ kind: 'revise', hypothesis: 'The result applies only to this subgroup.', success_criteria: 'Locate the subgroup qualification.' })))
    expect(api.run).not.toHaveBeenCalled()
  })
  it('labels a numeric proposition as assessed, not proved, when the interval is below threshold', async () => {
    const round = labRoundFixture(); round.observation = { kind: 'paired_summary', data: { pairs: 4, mean_difference: -2, descriptive_interval: [-3, -1], minimum_effect: 1, unit: 'points', criterion_result: 'upper_bound_below_threshold' }, coverage: { kind: 'supplied_pairs' } }; round.analysis.source_support = 'contradicted'; round.analysis.claim = 'The mean exceeds 1 points.'
    campaign = { ...campaign, status: 'awaiting_next', rounds: [round], review: round.review }
    vi.mocked(api.list).mockResolvedValue({ items: [campaign], has_more: false }); render(<ResearchLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /^Does the supplied text include/ }))
    await screen.findByText("The descriptive interval's upper bound is below the frozen threshold.")
    expect(screen.getByText('Proposition being assessed:')).toBeInTheDocument()
    expect(screen.getByText('Contradicted under this rule')).toBeInTheDocument()
    expect(screen.queryByText('The mean exceeds 1 points.')).not.toBeInTheDocument()
  })
})
