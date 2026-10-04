import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentsDockAPI } from '@shared/ipc'
import type { IdeaLabAPI, IdeaSession, IdeaResearch } from '@shared/idea-lab'
import { setLocale } from '@shared/i18n'
import { IdeaLab } from './IdeaLab'

const scope = { profileId: 'isolated', profileGeneration: 1, serverIdentity: 'isolated-server' }
const draft: IdeaSession = {
  id: 'idea-one', revision: 1, created_at: '2026-10-03T00:00:00Z', updated_at: '2026-10-03T00:00:00Z',
  brief: { goal: 'Which evidence should we collect?', hypothesis: '', constraints: '', sources: [] }, status: 'draft', phase: null,
  generation_id: null, result: null, decision: null, error: null, usage: [], events: []
}
const direction = { id: 'direction-one', title: 'Check missing context', question: 'Does omitted context change the result?', nearest_work: 'Only supplied notes', evidence_ids: ['E1'], counterevidence: ['No measured outcomes'], value: 'Test a specific ambiguity', uncertainty: 'Effect is unknown', minimal_action: 'Compare six pairs', expected_learning: 'Whether scope matters', feasibility: 'One day', cost_risk: 'Low cost; synthetic data only' }
const complete: IdeaSession = { ...draft, status: 'completed', revision: 5, generation_id: 'run-one', result: {
  literature: { summary: 'Supplied notes only', evidence: [{ id: 'E1', source_id: 'source-one', source_hash: 'a'.repeat(64), quote: 'Missing context', finding: 'A possible gap', limitation: 'No full paper', conditions: 'Only the six pilot cases', assumptions: ['Labels are reliable'], interpretation: 'Generalization remains untested' }], gaps: ['Expert validation missing'] },
  ideas: { directions: [direction, { ...direction, id: 'direction-two', title: 'Check wording' }], open_alternative: 'Collect more evidence first' },
  review: { recommendation_id: direction.id, reason: 'Addresses a clear ambiguity', critiques: [{ direction_id: direction.id, concerns: ['Small synthetic set'], test_before_commit: 'Check labels' }], missing_evidence: ['Measured outcomes'], comparison_summary: 'Prefer a bounded test.' }
} }
const researched: IdeaResearch = {
  version: 2, round: 2, max_rounds: 3, readiness: 'evidence_limited', stop_reason: 'Full methods remain unavailable.',
  limits: { max_rounds: 3, max_model_calls: 12, max_sources: 12, max_search_queries: 12 },
  used: { model_calls: 8, searches: 4, reads: 2, cache_hits: 1 },
  sources: [{ id: 'source-one', title: 'Retrieved article', uri: 'https://example.org/paper', text: 'Retrieved text. Missing context. Methods omitted.' }],
  papers: [{ id: 'source-one', title: 'Retrieved article', url: 'https://example.org/paper', access: 'abstract', status: 'read', coverage: 'Abstract only; methods omitted', content_hash: 'abc', parser_version: 'reader-2', cache_hit: false }],
  searches: [{ query: 'context omissions review methods', status: 'completed' }],
  activities: [{ id: 'event-one', at: '2026-10-03T10:00:00Z', agent: 'literature', type: 'evidence_handoff', summary: 'Read source and handed evidence to Idea Agent', thread_id: 'native-thread-1', turn_id: 'native-turn-1' }],
  rounds: [{ round: 1, disposition: 'retrieve_more', coverage_gaps: ['Methods unavailable'] }],
  questions: [{ id: 'q1', question: 'Which population matters?', why: 'It changes the comparison source.', required: true, options: ['Researchers', 'Students'] }],
  agents: [{ id: 'literature', name: 'Literature Agent', role: 'Read papers', status: 'idle', task: null }, { id: 'idea', name: 'Idea Agent', role: 'Propose directions', status: 'waiting_for_user', task: null }]
}
let api: IdeaLabAPI

beforeEach(() => {
  localStorage.clear()
  setLocale('en')
  api = {
    list: vi.fn().mockResolvedValue({ items: [], has_more: false }), get: vi.fn().mockResolvedValue(complete),
    saveOriginal: vi.fn(),
    paper: vi.fn().mockResolvedValue({ source: { id: 'source-one', title: 'Frozen source', uri: 'https://example.org/paper', text: 'Missing context from the frozen earlier version.' }, paper: null, revision: 3, archived: true, generation_id: complete.generation_id }),
    activities: vi.fn().mockResolvedValue({ items: [], has_more: false, next_before: null }),
    history: vi.fn().mockResolvedValue({ items: [complete], has_more: false }), create: vi.fn().mockResolvedValue(draft),
    generate: vi.fn().mockResolvedValue({ ...draft, status: 'running', phase: 'literature', generation_id: 'run-one', revision: 2 }),
    followup: vi.fn().mockResolvedValue({ ...complete, status: 'draft', revision: 6 }),
    cancel: vi.fn().mockResolvedValue({ ...draft, status: 'cancelled', generation_id: 'run-one', revision: 4 }),
    decision: vi.fn().mockImplementation(async (_scope, _id, input) => ({ ...complete, revision: 6, decision: { ...input, at: '2026-10-03T00:01:00Z' } }))
  }
  window.agentsDock = { ideaLab: api } as AgentsDockAPI
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('Idea Lab user workflow', () => {
  it('keeps direction tradeoffs and long reviewer critiques collapsed while preserving citations', async () => {
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    await screen.findAllByText('Evidence and tradeoffs')
    expect(screen.getAllByText('Evidence and tradeoffs').every(node => !node.closest('details')?.open)).toBe(true)
    expect(screen.getByText('Check labels')).not.toBeVisible()
    expect(screen.getByText('Detailed critiques and evidence requests').closest('details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getAllByRole('button', { name: 'E1' })[0])
    expect(document.getElementById('idea-evidence-E1')).toHaveAttribute('open')
  })
  it('requires an explicit generate click and does not poll while idle', async () => {
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    await waitFor(() => expect(api.list).toHaveBeenCalledOnce())
    fireEvent.click(screen.getByRole('button', { name: 'Fill synthetic example' }))
    expect((screen.getByLabelText('Research goal') as HTMLTextAreaElement).value).toContain('evidence excerpts')
    expect(api.create).not.toHaveBeenCalled(); expect(api.generate).not.toHaveBeenCalled()
    vi.useFakeTimers()
    await act(async () => { vi.advanceTimersByTime(10000) })
    expect(api.get).not.toHaveBeenCalled(); expect(api.list).toHaveBeenCalledOnce()
    vi.useRealTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Start literature research' }))
    await waitFor(() => expect(api.generate).toHaveBeenCalledOnce())
    expect(api.create).toHaveBeenCalledWith(scope, expect.objectContaining({ brief: expect.objectContaining({ sources: [expect.objectContaining({ title: expect.stringContaining('Synthetic') })] }) }))
    expect(screen.getByRole('button', { name: 'Stop this generation' })).toBeInTheDocument()
  })

  it('shows evidence/critique and saves actual selection, feedback, revision and history', async () => {
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    await screen.findAllByText('Expert validation missing')
    expect(screen.getByText('Check labels')).toBeInTheDocument()
    expect(screen.getByText(/Only the six pilot cases/)).toBeInTheDocument(); expect(screen.getByText('Labels are reliable')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Your feedback and follow-up request'), { target: { value: 'Keep this to six synthetic pairs.' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Choose this direction' })[0])
    await waitFor(() => expect(api.decision).toHaveBeenCalledWith(scope, complete.id, { expected_revision: 5, kind: 'select', selected_id: direction.id, feedback: 'Keep this to six synthetic pairs.' }))
    await screen.findByRole('button', { name: 'Selected direction' })
    fireEvent.click(screen.getByRole('button', { name: 'Previous versions and decisions' }))
    await waitFor(() => expect(api.history).toHaveBeenCalledWith(scope, complete.id))
  })

  it('resumes ownership only after the user explicitly opens a saved running group', async () => {
    const external = { ...draft, status: 'running' as const, generation_id: 'elsewhere', phase: 'review' as const }
    vi.mocked(api.list).mockResolvedValue({ items: [external], has_more: false }); vi.mocked(api.get).mockResolvedValue(external)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    const open = await screen.findByRole('button', { name: /Which evidence should we collect/ })
    expect(api.get).not.toHaveBeenCalled(); expect(api.cancel).not.toHaveBeenCalled()
    fireEvent.click(open)
    await screen.findByRole('button', { name: 'Stop this generation' })
    fireEvent.click(screen.getByRole('button', { name: 'Stop this generation' }))
    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(scope, draft.id, { expected_revision: external.revision }))
  })

  it('rejects a late response after the selected server changes', async () => {
    let resolveOld: (value: { items: IdeaSession[]; has_more: boolean }) => void = () => {}
    vi.mocked(api.list).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const view = render(<IdeaLab scope={scope} onClose={() => {}} />)
    view.rerender(<IdeaLab scope={{ ...scope, profileId: 'other', profileGeneration: 2, serverIdentity: 'other-server' }} onClose={() => {}} />)
    await act(async () => { resolveOld({ items: [complete], has_more: false }) })
    expect(screen.queryByRole('button', { name: /Which evidence should we collect/ })).not.toBeInTheDocument()
  })

  it('keeps partial evidence visible when generation fails', async () => {
    const partial = { ...complete, status: 'failed' as const, error: 'Review provider unavailable', result: { literature: complete.result!.literature, ideas: null, review: null } }
    vi.mocked(api.list).mockResolvedValue({ items: [partial], has_more: false }); vi.mocked(api.get).mockResolvedValue(partial)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    await screen.findByText('Review provider unavailable')
    expect(screen.getAllByText('Expert validation missing').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Start literature research' })).toBeEnabled()
  })

  it('validates source UTF-8 bytes before creating a group', async () => {
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fill synthetic example' }))
    fireEvent.change(screen.getByLabelText(/Pasted source text/), { target: { value: '文'.repeat(10001) } })
    fireEvent.click(screen.getByRole('button', { name: 'Start literature research' }))
    await screen.findByText(/Source 1 exceeds 30000 UTF-8 bytes/)
    expect(api.create).not.toHaveBeenCalled(); expect(api.generate).not.toHaveBeenCalled()
  })

  it('does not stop a generation that replaced the selected one', async () => {
    const run = { ...draft, status: 'running' as const, generation_id: 'owned-run', phase: 'review' as const }
    vi.mocked(api.list).mockResolvedValue({ items: [run], has_more: false })
    vi.mocked(api.get).mockResolvedValueOnce(run).mockResolvedValue({ ...run, generation_id: 'replacement-run', revision: 9 })
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Stop this generation' }))
    await screen.findByText(/This saved group is running elsewhere/)
    expect(api.cancel).not.toHaveBeenCalled()
  })
  it('distinguishes completed legacy output from researched readiness and exposes real source context', async () => {
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    const view = render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    await screen.findByRole('heading', { name: 'Earlier result · literature search not performed' })
    expect(screen.queryByRole('heading', { name: 'Evidence is ready for a direction choice' })).not.toBeInTheDocument()
    view.unmount()
    const item = { ...complete, research: researched }
    vi.mocked(api.list).mockResolvedValue({ items: [item], has_more: false }); vi.mocked(api.get).mockResolvedValue(item)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    await screen.findByRole('heading', { name: 'More evidence is needed' })
    expect(screen.getByText('Full methods remain unavailable.')).toBeInTheDocument()
    expect(screen.getByText('context omissions review methods')).toBeInTheDocument()
    expect(screen.getByText('Read source and handed evidence to Idea Agent')).toBeInTheDocument()
    expect(screen.getByText('Abstract only; methods omitted')).toBeInTheDocument()
    expect(screen.getAllByText('Retrieved text. Missing context. Methods omitted.').length).toBeGreaterThan(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'E1' })[0])
    expect(document.getElementById('idea-evidence-E1')).toHaveAttribute('open')
  })

  it('requires current answers and explicitly continues with exact feedback, answers and revision', async () => {
    const item = { ...complete, status: 'needs_input' as const, research: researched }
    vi.mocked(api.list).mockResolvedValue({ items: [item], has_more: false }); vi.mocked(api.get).mockResolvedValue(item)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    const continueButton = await screen.findByRole('button', { name: 'Search and read more' })
    expect(continueButton).toBeDisabled(); expect(api.followup).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText(/Which population matters/), { target: { value: 'Early-career researchers; keep this exact wording.' } })
    fireEvent.change(screen.getByLabelText('Your feedback and follow-up request'), { target: { value: 'Find the omitted methods, not another abstract.' } })
    fireEvent.click(continueButton)
    await waitFor(() => expect(api.followup).toHaveBeenCalledWith(scope, item.id, expect.objectContaining({ expected_revision: 5, mode: 'research', feedback: 'Find the omitted methods, not another abstract.', answers: [{ question_id: 'q1', answer: 'Early-career researchers; keep this exact wording.' }] })))
    await waitFor(() => expect(api.generate).toHaveBeenCalledWith(scope, item.id, expect.objectContaining({ expected_revision: 6 })))
    expect(api.followup).toHaveBeenCalledOnce(); expect(api.generate).toHaveBeenCalledOnce()
  })

  it('reuses the follow-up key after an unknown acknowledgement instead of creating new work', async () => {
    const item = { ...complete, research: { ...researched, questions: [] } }
    vi.mocked(api.list).mockResolvedValue({ items: [item], has_more: false }); vi.mocked(api.get).mockResolvedValue(item)
    vi.mocked(api.followup).mockRejectedValueOnce(new Error('Connection interrupted'))
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    fireEvent.change(await screen.findByLabelText('Your feedback and follow-up request'), { target: { value: 'Read missing methods.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search and read more' }))
    await screen.findByText('Connection interrupted')
    fireEvent.click(screen.getByRole('button', { name: 'Search and read more' }))
    await waitFor(() => expect(api.followup).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.followup).mock.calls[0][2]).toEqual(vi.mocked(api.followup).mock.calls[1][2])
    await waitFor(() => expect(api.generate).toHaveBeenCalledOnce())
  })

  it('combines checked directions only with an explicit resulting goal and retains actual feedback', async () => {
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    const combine = await screen.findByRole('button', { name: 'Combine checked directions' })
    expect(combine).toBeDisabled()
    for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox)
    expect(combine).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Updated or combined goal (optional; required to combine)'), { target: { value: 'Compare wording and missing context jointly.' } })
    fireEvent.change(screen.getByLabelText('Your feedback and follow-up request'), { target: { value: 'Keep both mechanisms separate in analysis.' } })
    fireEvent.click(combine)
    await waitFor(() => expect(api.decision).toHaveBeenCalledWith(scope, complete.id, { expected_revision: 5, kind: 'combine', selected_id: null, selected_ids: ['direction-one', 'direction-two'], goal: 'Compare wording and missing context jointly.', feedback: 'Keep both mechanisms separate in analysis.' }))
    expect(api.generate).not.toHaveBeenCalled()
  })

  it('restores unsent brief and answer drafts after leaving and reopening the workspace', async () => {
    const view = render(<IdeaLab scope={scope} onClose={() => {}} />)
    await waitFor(() => expect(api.list).toHaveBeenCalledOnce())
    fireEvent.change(screen.getByLabelText('Research goal'), { target: { value: 'Preserve my unsubmitted goal.' } })
    view.unmount()
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByLabelText('Research goal')).toHaveValue('Preserve my unsubmitted goal.'))
    expect(api.create).not.toHaveBeenCalled()
  })

  it('keeps new briefs open but makes a running group’s members and persistent Stop control visible above the collapsed form', async () => {
    const run = { ...complete, status: 'running' as const, phase: 'searching' as const, research: { ...researched, readiness: 'researching' as const } }
    vi.mocked(api.list).mockResolvedValue({ items: [run], has_more: false }); vi.mocked(api.get).mockResolvedValue(run)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    expect(screen.getByLabelText('Research goal')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    const expand = await screen.findByRole('button', { name: 'Show brief details' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByLabelText('Research goal')).not.toBeVisible()
    expect(screen.getByRole('heading', { name: 'Literature Agent' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Idea Agent' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stop this generation' }).closest('.idea-lab-header')).not.toBeNull()
    fireEvent.click(expand)
    expect(screen.getByLabelText('Research goal')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stop this generation' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Hide brief details' }))
    expect(screen.getByLabelText('Research goal')).not.toBeVisible()
  })

  it('does not apply an old answer when a new generation reuses the same question ID', async () => {
    const old = { ...complete, status: 'needs_input' as const, research: researched }
    vi.mocked(api.list).mockResolvedValue({ items: [old], has_more: false }); vi.mocked(api.get).mockResolvedValue(old)
    const view = render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    fireEvent.change(await screen.findByLabelText(/Which population matters/), { target: { value: 'Researchers' } })
    view.unmount()
    const changed = { ...old, generation_id: 'new-generation', research: { ...researched, questions: [{ ...researched.questions[0], question: 'Which outcome matters?' }] } }
    vi.mocked(api.list).mockResolvedValue({ items: [changed], has_more: false }); vi.mocked(api.get).mockResolvedValue(changed)
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    expect(await screen.findByLabelText(/Which outcome matters/)).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Search and read more' })).toBeDisabled()
    expect(api.followup).not.toHaveBeenCalled()
  })

  it('loads a historical frozen source only on explicit request and keeps the saved evidence inspectable', async () => {
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Previous versions and decisions' }))
    const version = await screen.findByText(/Version 5 · Which evidence/)
    expect(api.paper).not.toHaveBeenCalled()
    fireEvent.click(version)
    const evidenceSummary = screen.getByText(/Saved evidence E1/)
    fireEvent.click(evidenceSummary)
    const evidence = evidenceSummary.closest('details')!
    expect(within(evidence).getByText('Missing context')).toBeVisible()
    expect(within(evidence).getByText(/Only the six pilot cases/)).toBeVisible()
    expect(within(evidence).getByText('Labels are reliable')).toBeVisible()
    expect(within(evidence).getByText(/Generalization remains untested/)).toBeVisible()
    expect(api.paper).not.toHaveBeenCalled()
    fireEvent.click(within(evidence).getByRole('button', { name: 'Read the source text used for this evidence' }))
    await waitFor(() => expect(api.paper).toHaveBeenCalledWith(scope, complete.id, 'source-one', 'a'.repeat(64), complete.generation_id))
    expect(await within(evidence).findByText('Missing context from the frozen earlier version.')).toBeVisible()
    expect(within(evidence).getByText(/Source preserved from an earlier research version/)).toBeVisible()
    expect(api.paper).toHaveBeenCalledOnce(); expect(api.generate).not.toHaveBeenCalled()
  })

  it('discards a historical source response when the displayed server changes', async () => {
    let resolvePaper: (value: any) => void = () => {}
    vi.mocked(api.paper).mockImplementation(() => new Promise(resolve => { resolvePaper = resolve }))
    vi.mocked(api.list).mockResolvedValue({ items: [complete], has_more: false })
    const view = render(<IdeaLab scope={scope} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Which evidence should we collect/ }))
    fireEvent.click((await screen.findAllByRole('button', { name: 'E1' }))[0])
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    await waitFor(() => expect(api.paper).toHaveBeenCalledOnce())
    vi.mocked(api.list).mockResolvedValue({ items: [], has_more: false })
    view.rerender(<IdeaLab scope={{ ...scope, serverIdentity: 'another', profileGeneration: 2 }} onClose={() => {}} />)
    await act(async () => { resolvePaper({ source: { id: 'source-one', title: 'Late source', uri: 'https://example.org/old', text: 'Missing context: late old-server text.' }, paper: null }) })
    expect(screen.queryByText('Missing context: late old-server text.')).not.toBeInTheDocument()
  })

})
