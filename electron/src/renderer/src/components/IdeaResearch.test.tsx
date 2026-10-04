import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import type { AgentsDockAPI } from '@shared/ipc'
import type { IdeaSession, IdeaSource, IdeaEvidence } from '@shared/idea-lab'
import { FrozenEvidenceSource, ResearchWorkspace, revealEvidence } from './IdeaResearch'

const scope = { profileId: 'isolated', profileGeneration: 1, serverIdentity: 'test-server' }
const source = (id: string, version = id): IdeaSource => ({ id, title: `Paper ${id}`, uri: 'https://example.org/shared-url', text: `Saved text ${id}`, provenance: { source_version_id: version } })
const evidence = (id: string, sourceId: string): IdeaEvidence => ({ id, source_id: sourceId, quote: `Quote ${id}`, finding: `Finding ${id}`, limitation: `Limit ${id}` })
const base: IdeaSession = {
  id: 'saved-group', revision: 1, created_at: '', updated_at: '', brief: { goal: 'A question', hypothesis: '', constraints: '', sources: [] },
  status: 'needs_input', phase: null, generation_id: 'generation-one', decision: null, error: null, usage: [], events: [],
  result: { literature: { summary: 'A synthesis', evidence: [evidence('E1', 'one')], gaps: ['Full methods missing'] } },
  research: {
    version: 2, round: 1, max_rounds: 3, readiness: 'evidence_limited', stop_reason: 'invalid_output',
    limits: { max_rounds: 3, max_model_calls: 12, max_sources: 6, max_search_queries: 9 }, used: { model_calls: 4, searches: 2, reads: 1, cache_hits: 0 },
    sources: [source('one')], papers: [{ id: 'one', title: 'Paper one', url: 'https://example.org/shared-url', status: 'read', access: 'partial', coverage: { omitted_sections: ['Appendix'] }, content_hash: 'hash-one', parser_version: 'parser-one', retrieved_at: '2026-10-03T10:00:00Z' }],
    searches: [{ query: 'a real search query', summary: 'Search snippet is not a finding', status: 'completed' }],
    activities: [{ id: 'event-one', seq: 2, at: '2026-10-03T10:00:00Z', agent: 'literature', type: 'read', summary: 'Raw English tool event' }],
    coverage_gaps: ['Full methods missing'], questions: [], rounds: [],
    agents: [{ id: 'literature', name: 'Literature Agent', role: 'Raw role', task: 'Raw provider task', status: 'idle', thread_id: 'native-thread-id' }, { id: 'idea', name: 'Idea Agent', role: 'Raw idea role', task: null, status: 'waiting_for_user' }]
  }
}
const clone = () => structuredClone(base)
const activities = vi.fn()
const renderWorkspace = (session = clone()) => render(<ResearchWorkspace session={session} scope={scope} onError={() => {}} />)

beforeEach(() => {
  setLocale('en')
  activities.mockReset().mockResolvedValue({ items: [{ id: 'earlier', seq: 1, agent: 'idea', type: 'review', summary: 'Older saved event' }], has_more: false, next_before: null })
  window.agentsDock = { ideaLab: { activities }, native: { openExternal: vi.fn() } } as unknown as AgentsDockAPI
})
afterEach(cleanup)

describe('Idea research information hierarchy', () => {
  it('summarizes only actual current-generation reuse and keeps identity details folded', () => {
    const item = clone()
    item.usage = [{ stage: 'literature', generation_id: item.generation_id, provider: { literature_cache: { version: 1, status: 'join', waited: true, avoided_model_jobs: 1, key: 'cache-key', origin: { session_id: 'old-origin' } } } }]
    renderWorkspace(item)
    expect(screen.getByText(/waited for and reused source-checked literature cards/)).toBeVisible()
    expect(screen.getByText(/Research inferences still need assessment/)).toBeVisible()
    expect(screen.getByText(/old-origin/)).not.toBeVisible()
    expect(activities).not.toHaveBeenCalled()
  })
  it('reports a reuse-check bypass without claiming inference savings', () => {
    const item = clone()
    item.usage = [{ stage: 'literature', generation_id: item.generation_id, provider: { literature_cache: { version: 1, status: 'bypass', reason: 'cache_lookup_unavailable', waited: false, avoided_model_jobs: 0 } } }]
    renderWorkspace(item)
    expect(screen.getByText(/Reuse checking was unavailable/)).toBeVisible()
    expect(screen.queryByText(/one model extraction turn avoided/)).not.toBeInTheDocument()
  })
  it('does not call a waited miss a saved task, or present earlier generation reuse as current', () => {
    const item = clone()
    item.usage = [{ stage: 'literature', generation_id: item.generation_id, provider: { literature_cache: { version: 1, status: 'miss', waited: true, avoided_model_jobs: 0 } } }]
    renderWorkspace(item)
    expect(screen.getByText(/no extraction task was saved/)).toBeVisible()
    expect(screen.queryByText(/one model extraction turn avoided/)).not.toBeInTheDocument()
    cleanup(); item.generation_id = 'new-generation'; renderWorkspace(item)
    expect(screen.queryByText(/no extraction task was saved/)).not.toBeInTheDocument()
  })

  it('shows members and existing findings while raw work, budgets and coverage remain collapsed', () => {
    renderWorkspace()
    expect(screen.getByRole('heading', { name: 'Literature Agent' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Idea Agent' })).toBeVisible()
    expect(screen.getByText('Partial text read', { selector: '.idea-lab-reading-status' })).toBeVisible()
    expect(screen.getByText('Finding E1')).toBeVisible()
    expect(screen.getByText('The generated content did not pass validation. Retrieved sources remain available.')).toBeVisible()
    for (const text of ['Raw provider task', 'Raw English tool event', 'a real search query', 'invalid_output', 'Full methods missing', 'Saved text one']) expect(screen.getByText(text)).not.toBeVisible()
    expect(screen.getByLabelText('Research limits and usage')).not.toBeVisible()
    expect(screen.getByText('Technical log').closest('details')).not.toHaveAttribute('open')
    expect(activities).not.toHaveBeenCalled()
  })

  it('loads older work only after an explicit request inside the technical log', async () => {
    renderWorkspace()
    fireEvent.click(screen.getByText('Technical log'))
    const load = screen.getByRole('button', { name: 'Inspect saved work journal' })
    expect(load).toBeVisible()
    expect(activities).not.toHaveBeenCalled()
    fireEvent.click(load)
    await waitFor(() => expect(activities).toHaveBeenCalledWith(scope, 'saved-group', undefined))
    expect(screen.getByText('Older saved event')).not.toBeVisible()
    fireEvent.click(screen.getByText(/Work and evidence handoffs/))
    expect(screen.getByText('Older saved event')).toBeVisible()
    expect(screen.getByText('Raw English tool event')).toBeVisible()
  })

  it('groups only matching source versions and retains exact packet findings and source texts', () => {
    const item = clone()
    item.research!.sources = [source('one', 'same-version'), source('two', 'same-version'), source('three', 'different-version')]
    item.research!.papers = ['one', 'two', 'three'].map(id => ({ ...base.research!.papers[0], id, title: `Paper ${id}` }))
    item.result!.literature!.evidence = [evidence('E1', 'one'), evidence('E2', 'two'), evidence('E3', 'two'), evidence('E4', 'three')]
    const view = renderWorkspace(item)
    const cards = [...view.container.querySelectorAll('.idea-lab-reading-card')]
    expect(cards).toHaveLength(2)
    expect(within(cards[0] as HTMLElement).getByText('Finding E1')).toBeVisible()
    expect(within(cards[0] as HTMLElement).getByText('Finding E2')).toBeVisible()
    expect(within(cards[0] as HTMLElement).getByText('Finding E3')).not.toBeVisible()
    expect(within(cards[0] as HTMLElement).queryByText('Finding E4')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('1 more extracted findings'))
    expect(screen.getByText('Finding E3')).toBeVisible()
    fireEvent.click(within(cards[0] as HTMLElement).getByText('Inspect source text and reading coverage'))
    expect(screen.getByText('2 reading excerpts from the same source version, not independent studies.')).toBeVisible()
    expect(view.container.querySelector('#idea-source-one')).not.toBeNull()
    expect(view.container.querySelector('#idea-source-two')).not.toBeNull()
    expect(within(cards[1] as HTMLElement).getByText('Finding E4')).toBeVisible()
  })

  it('does not infer version equivalence or transfer findings from a matching URL', () => {
    const item = clone()
    item.research!.sources = [{ ...source('other'), provenance: undefined }]
    item.result!.literature!.evidence = [evidence('E-other', 'other')]
    const view = renderWorkspace(item)
    const cards = [...view.container.querySelectorAll('.idea-lab-reading-card')]
    expect(cards).toHaveLength(2)
    expect(within(cards[0] as HTMLElement).queryByText('Finding E-other')).not.toBeInTheDocument()
    expect(within(cards[0] as HTMLElement).getByText('No extracted information has passed source checks yet.')).toBeVisible()
    expect(within(cards[1] as HTMLElement).getByText('Finding E-other')).toBeVisible()
  })

  it('does not turn search receipts or read-but-unvalidated text into evidence', () => {
    const item = clone()
    item.result = null
    renderWorkspace(item)
    expect(screen.getByText('Partial text read', { selector: '.idea-lab-reading-status' })).toBeVisible()
    expect(screen.getByText('No extracted information has passed source checks yet.')).toBeVisible()
    expect(screen.queryByText('Search snippet is not a finding')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Inspect quotation/ })).not.toBeInTheDocument()
  })

  it('keeps search-only, supplied, failed-read and legacy states distinct', () => {
    const item = clone()
    item.result = null
    item.research!.sources = []
    item.research!.papers = []
    const view = renderWorkspace(item)
    expect(screen.getByText(/Searches are recorded, but no readable source/)).toBeVisible()
    item.brief.sources = [source('pasted')]
    item.research!.papers = [{ ...base.research!.papers[0], id: 'failed', title: 'Blocked paper', status: 'unavailable', access: 'unavailable', error: 'HTTP 403' }]
    view.rerender(<ResearchWorkspace session={item} scope={scope} onError={() => {}} />)
    expect(screen.getByText('Material you supplied (not a search result)', { selector: '.idea-lab-reading-status' })).toBeVisible()
    expect(screen.getByText('Readable body text not obtained', { selector: '.idea-lab-reading-status' })).toBeVisible()
    expect(screen.queryByText('Partial text read')).not.toBeInTheDocument()
    item.research = undefined
    view.rerender(<ResearchWorkspace session={item} scope={scope} onError={() => {}} />)
    expect(screen.getByRole('heading', { name: 'Earlier result · literature search not performed' })).toBeVisible()
    expect(screen.getByText('Material you supplied (not a search result)', { selector: '.idea-lab-reading-status' })).toBeVisible()
  })

  it('opens the complete evidence path without fetching or starting work', () => {
    const view = render(<><ResearchWorkspace session={clone()} scope={scope} onError={() => {}} /><details data-testid="all-evidence"><summary>All evidence</summary><details id="idea-evidence-E1" tabIndex={-1}><summary>E1 details</summary><p>Frozen quote and limitations</p></details></details></>)
    expect(screen.getByText('Frozen quote and limitations')).not.toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Inspect quotation E1 and limitations' }))
    expect(screen.getByTestId('all-evidence')).toHaveAttribute('open')
    expect(view.container.querySelector('#idea-evidence-E1')).toHaveAttribute('open')
    expect(screen.getByText('Frozen quote and limitations')).toBeVisible()
    expect(activities).not.toHaveBeenCalled()
    revealEvidence('absent')
  })

  it('keeps Chinese reading labels and technical detail separation consistent', () => {
    setLocale('zh-CN')
    renderWorkspace()
    expect(screen.getByRole('heading', { name: '查阅的文献与信息' })).toBeVisible()
    expect(screen.getByText('已读取部分文本', { selector: '.idea-lab-reading-status' })).toBeVisible()
    expect(screen.getByText('技术日志').closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText('Raw provider task')).not.toBeVisible()
  })

  it('discards a pending activity page after this workspace unmounts', async () => {
    let finish: (value: unknown) => void = () => {}
    activities.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const view = renderWorkspace()
    fireEvent.click(screen.getByText('Technical log'))
    fireEvent.click(screen.getByRole('button', { name: 'Inspect saved work journal' }))
    view.unmount()
    await act(async () => { finish({ items: [{ id: 'late', agent: 'idea', type: 'review', summary: 'Late event' }], next_before: null }) })
    expect(screen.queryByText('Late event')).not.toBeInTheDocument()
  })

  it('describes the exact frozen model packet rather than borrowing full-text coverage from the original receipt', async () => {
    const item = evidence('E1', 'one')
    const paper = vi.fn().mockResolvedValue({
      source: { ...source('one'), text: 'Quote E1 in a clipped packet.', coverage: { kind: 'partial', model_packet_truncated: true } },
      paper: { ...base.research!.papers[0], access: 'full_text', coverage: { kind: 'full_text' } }
    })
    window.agentsDock.ideaLab!.paper = paper
    render(<FrozenEvidenceSource scope={scope} sessionId="saved-group" evidence={item} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    expect(await screen.findByText('Text used for this quotation: Partial text read')).toBeVisible()
    expect(screen.getByText(/Original reading receipt: Full text/)).not.toBeVisible()
    expect(paper).toHaveBeenCalledWith(scope, 'saved-group', 'one', undefined, undefined)
  })
  it('keeps original saving inside source details and sends only the exact historical identity after a click', async () => {
    const item = evidence('E1', 'one'), hash = 'a'.repeat(64), generationId = 'd'.repeat(32), provenanceHash = 'e'.repeat(64)
    const paper = vi.fn().mockResolvedValue({ source: { ...source('one'), text: 'Quote E1' }, paper: null, packet_hash: hash, archived: true, generation_id: generationId, provenance_hash: provenanceHash })
    const saveOriginal = vi.fn().mockResolvedValue({ status: 'saved', path: 'chosen-original.pdf', session_id: 'saved-group', source_id: 'one', source_hash: hash, generation_id: generationId, provenance_hash: provenanceHash })
    Object.assign(window.agentsDock.ideaLab!, { paper, saveOriginal })
    render(<FrozenEvidenceSource scope={scope} sessionId="saved-group" evidence={item} />)
    expect(saveOriginal).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    await screen.findByText('Paper one')
    expect(screen.getByText('Save the original file downloaded at that time…')).not.toBeVisible()
    expect(saveOriginal).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Content version'))
    fireEvent.click(screen.getByRole('button', { name: 'Save the original file downloaded at that time…' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Original file saved: chosen-original.pdf')
    expect(saveOriginal).toHaveBeenCalledWith(scope, 'saved-group', 'one', hash, generationId, provenanceHash)
    expect(window.agentsDock.native.openExternal).not.toHaveBeenCalled()
  })
  it('keeps a legacy packet readable without offering an original save when its descriptor is missing', async () => {
    const paper = vi.fn().mockResolvedValue({ source: { ...source('one'), text: 'Quote E1' }, paper: null, packet_hash: 'a'.repeat(64) })
    const saveOriginal = vi.fn().mockResolvedValue({ status: 'not_retained' })
    Object.assign(window.agentsDock.ideaLab!, { paper, saveOriginal })
    render(<FrozenEvidenceSource scope={scope} sessionId="saved-group" evidence={evidence('E1', 'one')} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    await screen.findByText('Paper one')
    fireEvent.click(screen.getByText('Content version'))
    expect(screen.queryByRole('button', { name: 'Save the original file downloaded at that time…' })).not.toBeInTheDocument()
    expect(saveOriginal).not.toHaveBeenCalled()
    expect(paper).toHaveBeenCalledTimes(1)
  })
  it('does not attach a late save result to a different source owner', async () => {
    let finish: (value: unknown) => void = () => {}
    const paper = vi.fn().mockResolvedValue({ source: { ...source('one'), text: 'Quote E1' }, paper: null, packet_hash: 'a'.repeat(64), generation_id: 'd'.repeat(32), provenance_hash: 'e'.repeat(64) })
    const saveOriginal = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    Object.assign(window.agentsDock.ideaLab!, { paper, saveOriginal })
    const view = render(<FrozenEvidenceSource scope={scope} sessionId="saved-group" evidence={evidence('E1', 'one')} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    await screen.findByText('Paper one')
    fireEvent.click(screen.getByText('Content version'))
    fireEvent.click(screen.getByRole('button', { name: 'Save the original file downloaded at that time…' }))
    view.rerender(<FrozenEvidenceSource scope={{ ...scope, serverIdentity: 'other' }} sessionId="other-group" evidence={evidence('E2', 'two')} />)
    await act(async () => { finish({ status: 'saved', path: 'previous-owner.pdf' }) })
    expect(screen.queryByText(/previous-owner.pdf/)).not.toBeInTheDocument()
    expect(screen.queryByText('Paper one')).not.toBeInTheDocument()
  })
  it('includes the generation in a paper request and discards its response after a same-source generation switch', async () => {
    let finish: (value: unknown) => void = () => {}
    const paper = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    window.agentsDock.ideaLab!.paper = paper
    const item = evidence('E1', 'one'), generation = 'd'.repeat(32)
    const view = render(<FrozenEvidenceSource scope={scope} sessionId="saved-group" generationId={generation} evidence={item} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read the source text used for this evidence' }))
    expect(paper).toHaveBeenCalledWith(scope, 'saved-group', 'one', undefined, generation)
    view.rerender(<FrozenEvidenceSource scope={scope} sessionId="saved-group" generationId={'f'.repeat(32)} evidence={item} />)
    await act(async () => { finish({ source: { ...source('one'), text: 'Quote E1' }, paper: null, generation_id: generation }) })
    expect(screen.queryByText('Paper one')).not.toBeInTheDocument()
  })

})
