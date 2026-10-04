import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import type { LabCampaign, ResearchLabAPI } from '@shared/research-lab'
import type { ResearchBranchesAPI } from '@shared/research-branches'
import { branchSetFixture } from '@shared/research-branches.fixture'
import { labFixture, labRoundFixture } from '@shared/research-lab.fixture'
import { ResearchBranchWorkspace } from './ResearchBranchWorkspace'
import { validateBranches } from './ResearchBranchEditor'
const scope = { profileId: 'isolated', profileGeneration: 1, serverIdentity: 'synthetic' }
let snapshot: LabCampaign, branchApi: ResearchBranchesAPI, api: ResearchLabAPI
beforeEach(() => {
  setLocale('en'); localStorage.clear(); snapshot = { ...labFixture(), branch_set: branchSetFixture() }
  branchApi = { get: vi.fn().mockImplementation(async () => snapshot), enable: vi.fn(), plan: vi.fn(), answers: vi.fn(), decision: vi.fn(), control: vi.fn(), run: vi.fn() }
  api = { branches: branchApi, artifact: vi.fn() } as unknown as ResearchLabAPI
})
afterEach(cleanup)
function Harness() { const [value, setValue] = useState(snapshot); return <ResearchBranchWorkspace api={api} scope={scope} campaign={value} storageKey="test" disabled={false} onChange={setValue} onBusy={() => {}} /> }
const ready = async () => { await waitFor(() => expect(branchApi.get).toHaveBeenCalledOnce()); await waitFor(() => expect(screen.queryByText('Saved. Refresh the current source state before running or requesting another member.')).not.toBeInTheDocument()) }
const selectB = () => fireEvent.click(within(screen.getAllByRole('article').find(row => within(row).queryByText('Question b'))!).getByRole('button'))
describe('Branch workspace', () => {
  it('keeps B available when A needs an answer and C waits, and switching is view-only', async () => {
    snapshot.dependency_status = { state: 'needs_revalidation', run_allowed: false, current_input_blocked: true, affected_claim_ids: [], pending_intents: [], corrections: [], pending_deliveries: 0 }
    render(<Harness />); await ready(); selectB()
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled()
    expect(screen.getByText('Which supplied scope should we use?')).toBeVisible()
    expect(branchApi.run).not.toHaveBeenCalled(); expect(branchApi.decision).not.toHaveBeenCalled(); expect(branchApi.plan).not.toHaveBeenCalled()
  })
  it('treats an acknowledged save with failed projection refresh as saved and failclosed, never re-POSTs it', async () => {
    const saved = structuredClone(snapshot); saved.branch_set!.branches[1].scope_status = 'unknown'; saved.branch_set!.branches[1].gate!.allowed = false
    vi.mocked(branchApi.run).mockResolvedValue(saved)
    render(<Harness />); await ready(); selectB()
    vi.mocked(branchApi.get).mockRejectedValueOnce(new Error('Projection unavailable'))
    fireEvent.click(screen.getByRole('button', { name: 'Run the selected frozen action' }))
    await screen.findByText(/Projection unavailable/)
    expect(branchApi.run).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeDisabled()
    expect(branchApi.run).toHaveBeenCalledWith(scope, snapshot.id, 'b', expect.objectContaining({ expected_branch_revision: 1, expected_authority_epoch: 1 }))
    vi.mocked(branchApi.get).mockResolvedValue(snapshot)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh branch status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run the selected frozen action' })).toBeEnabled())
    expect(branchApi.run).toHaveBeenCalledOnce()
  })
  it('retries an unknown mutation response with its same identity, and answers preserve whitespace without executing', async () => {
    vi.mocked(branchApi.answers).mockRejectedValue(new Error('Save response lost'))
    render(<Harness />); await ready()
    fireEvent.change(screen.getByLabelText(/Which supplied scope/), { target: { value: '  explicit scope\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save answers' }))
    await screen.findAllByText('Save response lost')
    fireEvent.click(screen.getByRole('button', { name: 'Save answers' }))
    await waitFor(() => expect(branchApi.answers).toHaveBeenCalledTimes(2))
    expect(vi.mocked(branchApi.answers).mock.calls[0][3]).toEqual(vi.mocked(branchApi.answers).mock.calls[1][3])
    expect(vi.mocked(branchApi.answers).mock.calls[0][3].answers).toEqual([{ question_id: 'scope', answer: '  explicit scope\n' }])
    expect(branchApi.run).not.toHaveBeenCalled(); expect(branchApi.plan).not.toHaveBeenCalled()
  })
  it('loads an omitted result by its branch run reference, never another branch’s newest round', async () => {
    const older = labRoundFixture(), newer = structuredClone(older); newer.run.id = 'other-run'; newer.analysis.claim = 'Other branch observation'
    snapshot.rounds = [newer]; snapshot.branch_set!.branches[1].latest_result = { run_id: older.run.id, round_artifact: 'c'.repeat(64), observation_artifact: 'd'.repeat(64), input_artifact: 'a'.repeat(64), status: 'completed', qc_passed: true, root_context_hash: 'b'.repeat(64), context_hash: 'b'.repeat(64), current: true }
    vi.mocked(api.artifact).mockResolvedValue({ sha256: 'c'.repeat(64), media_type: 'application/json', content: older })
    render(<Harness />); await ready(); selectB()
    expect(screen.queryByText('Other branch observation')).not.toBeInTheDocument(); expect(api.artifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Load this earlier round' }))
    await waitFor(() => expect(api.artifact).toHaveBeenCalledWith(scope, snapshot.id, 'c'.repeat(64)))
    await screen.findByText(String(older.observation.data.quote)); expect(screen.queryByText('Other branch observation')).not.toBeInTheDocument()
  })
  it('validates explicitly supplied methods, sources and non-cyclic dependencies', () => {
    snapshot.input_summary.sources = [{ id: 'source-one', title: 'Source one' }]
    const proposal = { id: 'a', title: 'A', question: 'Question?', success_criterion: 'Useful result', methods: [...snapshot.brief.authorized_actions], source_ids: ['source-one'], depends_on: [], questions: [] }
    expect(validateBranches([proposal], snapshot)).toEqual([proposal])
    expect(() => validateBranches([{ ...proposal, methods: ['execute_arbitrary_code'] }], snapshot)).toThrow()
    expect(() => validateBranches([{ ...proposal, source_ids: ['not-supplied'] }], snapshot)).toThrow()
    expect(() => validateBranches([{ ...proposal, depends_on: [{ branch_id: 'b', require: 'qc_passed' }] }, { ...proposal, id: 'b', depends_on: [{ branch_id: 'a', require: 'completed' }] }], snapshot)).toThrow('cycle')
  })
})
