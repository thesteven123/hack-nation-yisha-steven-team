import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import type { ResearchModelAPI, ResearchModelJob } from '@shared/research-models'
import { modelJobFixture } from '@shared/research-models.fixture'
import { labFixture, labRoundFixture } from '@shared/research-lab.fixture'
import { ResearchModels } from './ResearchModels'
import { branchFixture, branchSetFixture } from '@shared/research-branches.fixture'

const scope = { profileId: 'research', profileGeneration: 1, serverIdentity: 'isolated-server' }
const quota = { limit_jobs: 6, used: 0, reserved: 0 }
let api: ResearchModelAPI
beforeEach(() => {
  localStorage.clear(); setLocale('en')
  api = { list: vi.fn().mockResolvedValue({ items: [], has_more: false, next_before: null, quota }), create: vi.fn().mockResolvedValue(modelJobFixture()), get: vi.fn().mockResolvedValue(modelJobFixture()), start: vi.fn().mockResolvedValue(modelJobFixture()), wait: vi.fn().mockReturnValue(new Promise(() => {})), cancel: vi.fn().mockResolvedValue(modelJobFixture('planner', 'cancelled')), artifact: vi.fn().mockResolvedValue({ sha256: 'a'.repeat(64), content: { frozen: 'packet' } }) }
})
afterEach(cleanup)
describe('Research model members', () => {
  it('releases only the matching pending state after a branch context changes during submission', async () => {
    const branch = branchFixture('b'), campaign = { ...labFixture(), branch_set: branchSetFixture() }
    let resolveOld!: (value: ResearchModelJob) => void
    vi.mocked(api.create).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve })).mockRejectedValueOnce(new Error('Current request remains distinct'))
    const view = render(<ResearchModels api={api} scope={scope} campaign={campaign} branch={branch} authorityEpoch={1} disabled={false} onUseFeedback={() => {}} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' }))
    await waitFor(() => expect(api.create).toHaveBeenCalledOnce())
    view.rerender(<ResearchModels api={api} scope={scope} campaign={campaign} branch={{ ...branch, revision: 2 }} authorityEpoch={1} disabled={false} onUseFeedback={() => {}} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeEnabled())
    await act(async () => resolveOld({ ...modelJobFixture(), branch_id: 'b', scope_status: 'current' }))
    expect(api.wait).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' }))
    await screen.findByText('Current request remains distinct')
    expect(vi.mocked(api.create).mock.calls[1][2]).toEqual(expect.objectContaining({ expected_branch_revision: 2 }))
  })
  it('uses server branch scope rather than root revision, and never runs from a recommendation', async () => {
    const branch = branchFixture('b'), campaign = { ...labFixture(), revision: 99, branch_set: branchSetFixture() }
    campaign.dependency_status = { state: 'needs_revalidation', run_allowed: false, current_input_blocked: true, affected_claim_ids: [], pending_intents: [], corrections: [], pending_deliveries: 0 }
    const job = { ...modelJobFixture('planner', 'completed'), branch_id: 'b', branch_scope: { sha256: 'b'.repeat(64) }, scope_status: 'current' as const }
    vi.mocked(api.list).mockResolvedValue({ items: [job], quota })
    const onUse = vi.fn()
    const view = render(<ResearchModels api={api} scope={scope} campaign={campaign} branch={branch} authorityEpoch={7} disabled={false} onUseFeedback={onUse} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy recommendation into my feedback' }))
    expect(onUse).toHaveBeenCalledOnce(); expect(api.create).not.toHaveBeenCalled()
    expect(api.list).toHaveBeenCalledWith(scope, campaign.id, undefined, 'b')
    expect(screen.queryByText(/For an earlier research revision/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask Analyst (model call)' })).toBeDisabled()
    vi.mocked(api.create).mockRejectedValue(new Error('Acknowledgement lost'))
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' }))
    await screen.findByText('Acknowledgement lost')
    expect(vi.mocked(api.create).mock.calls[0][2]).toEqual(expect.objectContaining({ branch_id: 'b', expected_branch_revision: 1, expected_authority_epoch: 7 }))
    expect(vi.mocked(api.create).mock.calls[0][2]).not.toHaveProperty('expected_revision')
    view.rerender(<ResearchModels api={api} scope={scope} campaign={campaign} branch={{ ...branch, scope_status: 'unknown' }} authorityEpoch={7} disabled={false} onUseFeedback={onUse} />)
    expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Copy recommendation into my feedback' })).not.toBeInTheDocument()
  })
  it('rejects branch advice with unknown service scope and lets a prepared own job be cancelled', async () => {
    const job = { ...modelJobFixture('planner', 'planned'), branch_id: 'b', scope_status: 'unknown' as const }
    vi.mocked(api.list).mockResolvedValue({ items: [job], quota: { ...quota, reserved: 1 } })
    const campaign = { ...labFixture(), branch_set: branchSetFixture() }, branch = branchFixture('b')
    render(<ResearchModels api={api} scope={scope} campaign={campaign} branch={branch} authorityEpoch={1} disabled={false} onUseFeedback={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Start prepared task (model call)' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Stop this member task' })).toBeEnabled()
    expect(api.create).not.toHaveBeenCalled(); expect(api.start).not.toHaveBeenCalled(); expect(api.wait).not.toHaveBeenCalled()
  })
  it('blocks new jobs and invalidates existing model advice when shared source correction changes no core revision', async () => {
    const job = modelJobFixture('planner', 'completed'), campaign = labFixture()
    campaign.dependency_status = { state: 'needs_revalidation', run_allowed: false, current_input_blocked: true, affected_claim_ids: [], pending_intents: [], corrections: [], pending_deliveries: 0 }
    vi.mocked(api.list).mockResolvedValue({ items: [job], quota: { ...quota, used: 1 } })
    render(<ResearchModels api={api} scope={scope} campaign={campaign} disabled={false} onUseFeedback={() => {}} />)
    await screen.findByText('The source was corrected; this model interpretation needs reassessment.')
    expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Use recommendation as decision feedback' })).not.toBeInTheDocument()
    expect(api.create).not.toHaveBeenCalled()
  })

  it('does not call a model or resume waiting on open; analyst/reviewer require current observations', async () => {
    const job = modelJobFixture(); vi.mocked(api.list).mockResolvedValue({ items: [job], quota: { ...quota, used: 1 } })
    const campaign = labFixture(); campaign.rounds = [{ ...labRoundFixture(), plan: { ...labRoundFixture().plan, goal_revision: 99 } }]
    render(<ResearchModels api={api} scope={scope} campaign={campaign} disabled={false} onUseFeedback={() => {}} />)
    await screen.findByRole('button', { name: 'View progress' })
    expect(api.wait).not.toHaveBeenCalled(); expect(api.create).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Ask Analyst (model call)' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Ask Reviewer (model call)' })).toBeDisabled()
    expect(screen.getByText(/native-private-thread/)).not.toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'View progress' }))
    await waitFor(() => expect(api.wait).toHaveBeenCalledOnce())
    expect(api.wait).toHaveBeenCalledWith(scope, campaign.id, job.id)
  })
  it('starts once after a click and waits for each server long wait before issuing another', async () => {
    const resolvers: ((value: ResearchModelJob) => void)[] = []
    vi.mocked(api.wait).mockImplementation(() => new Promise(resolve => resolvers.push(resolve)))
    const onUse = vi.fn()
    render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={onUse} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' }))
    await waitFor(() => expect(api.wait).toHaveBeenCalledOnce())
    expect(api.create).toHaveBeenCalledOnce(); expect(api.get).not.toHaveBeenCalled()
    await act(async () => resolvers[0](modelJobFixture()))
    await waitFor(() => expect(api.wait).toHaveBeenCalledTimes(2))
    const complete = modelJobFixture('planner', 'completed')
    vi.mocked(api.list).mockResolvedValue({ items: [complete], quota: { ...quota, used: 1 } })
    await act(async () => resolvers[1](complete))
    await screen.findByText('Inspect the qualification in the first source before interpreting it.')
    expect(api.wait).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole('button', { name: 'Copy recommendation into my feedback' }))
    expect(onUse).toHaveBeenCalledWith('Inspect the qualification in the first source before interpreting it.')
    expect(api.create).toHaveBeenCalledOnce(); expect(api.artifact).not.toHaveBeenCalled()
  })
  it('ignores a long-wait response after unmount and starts no additional waits', async () => {
    let resolve!: (value: ResearchModelJob) => void
    vi.mocked(api.list).mockResolvedValue({ items: [modelJobFixture()], quota: { ...quota, used: 1 } })
    vi.mocked(api.wait).mockReturnValue(new Promise(r => { resolve = r }))
    const view = render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'View progress' })); await waitFor(() => expect(api.wait).toHaveBeenCalledOnce())
    view.unmount(); await act(async () => resolve(modelJobFixture()))
    expect(api.wait).toHaveBeenCalledOnce()
  })
  it('shows unsupported methods and fences a recommendation from an older campaign revision', async () => {
    const job = modelJobFixture('planner', 'completed'); if (job.output && 'selected_action_id' in job.output) { job.output.status = 'needs_method'; job.output.selected_action_id = null }
    vi.mocked(api.list).mockResolvedValue({ items: [job], quota: { ...quota, used: 1 } })
    const view = render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={() => {}} />)
    await screen.findByText(/The current executable methods cannot answer this goal/)
    expect(screen.queryByRole('button', { name: 'Copy recommendation into my feedback' })).not.toBeInTheDocument()
    view.rerender(<ResearchModels api={api} scope={scope} campaign={{ ...labFixture(), revision: 2 }} disabled={false} onUseFeedback={() => {}} />)
    expect(screen.getByText(/For an earlier research revision/)).toBeInTheDocument()
    expect(api.list).toHaveBeenCalledOnce()
  })
  it('cancels only this job and ignores a late running response after cleanup completes', async () => {
    let resolve!: (value: ResearchModelJob) => void
    vi.mocked(api.list).mockResolvedValue({ items: [modelJobFixture()], quota: { ...quota, used: 1 } })
    vi.mocked(api.wait).mockReturnValue(new Promise(r => { resolve = r }))
    render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'View progress' })); await waitFor(() => expect(api.wait).toHaveBeenCalledOnce())
    vi.mocked(api.list).mockResolvedValue({ items: [modelJobFixture('planner', 'cancelled')], quota: { ...quota, used: 1 } })
    fireEvent.click(screen.getByRole('button', { name: 'Stop this member task' }))
    await screen.findByText('Cancelled after cleanup')
    await act(async () => resolve(modelJobFixture()))
    expect(screen.getByText('Cancelled after cleanup')).toBeInTheDocument()
    expect(api.cancel).toHaveBeenCalledWith(scope, 'campaign-one', 'job-planner'); expect(api.wait).toHaveBeenCalledOnce()
  })
  it('can explicitly start a prepared identity without preparing a second job', async () => {
    const planned = modelJobFixture('planner', 'planned')
    vi.mocked(api.list).mockResolvedValue({ items: [planned], quota: { ...quota, reserved: 1 } })
    render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Start prepared task (model call)' }))
    await waitFor(() => expect(api.wait).toHaveBeenCalledOnce())
    expect(api.start).toHaveBeenCalledWith(scope, 'campaign-one', 'job-planner'); expect(api.create).not.toHaveBeenCalled()
  })
  it('reuses the same identity when the create acknowledgement is lost', async () => {
    vi.mocked(api.create).mockRejectedValueOnce(new Error('Response lost')).mockResolvedValueOnce(modelJobFixture())
    render(<ResearchModels api={api} scope={scope} campaign={labFixture()} disabled={false} onUseFeedback={() => {}} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ask Planner (model call)' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' })); await screen.findByText('Response lost')
    fireEvent.click(screen.getByRole('button', { name: 'Ask Planner (model call)' })); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.create).mock.calls[0][2]).toEqual(vi.mocked(api.create).mock.calls[1][2])
  })
  it('shows model usage as unknown and links analyst interpretations to returned machine facts', async () => {
    const job = modelJobFixture('analyst', 'completed'), campaign = labFixture(); campaign.rounds = [labRoundFixture()]
    vi.mocked(api.list).mockResolvedValue({ items: [job], quota: { ...quota, used: 1 } })
    render(<ResearchModels api={api} scope={scope} campaign={campaign} disabled={false} onUseFeedback={() => {}} />)
    await screen.findByText('The observation locates the quote but does not test the broad hypothesis.')
    expect(screen.getAllByText(/Model token\/cost usage unknown or incomplete/).length).toBeGreaterThan(0)
    expect(screen.getByText('data.match_count · Quality checks passed within this method\'s scope.')).not.toBeVisible()
    fireEvent.click(screen.getByText('Interpretation details and evidence links'))
    fireEvent.click(screen.getByText('Inspect referenced machine values'))
    expect(screen.getByText('data.match_count · Quality checks passed within this method\'s scope.')).toBeVisible()
    expect(api.artifact).not.toHaveBeenCalled()
  })
})
