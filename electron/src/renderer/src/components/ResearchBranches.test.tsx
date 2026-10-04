import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import { branchSetFixture } from '@shared/research-branches.fixture'
import { ResearchBranchAnswers, ResearchBranchOverview } from './ResearchBranches'

beforeEach(() => { setLocale('en'); localStorage.clear() })
afterEach(cleanup)
describe('Compact research branch UI', () => {
  it('shows human-readable blocking reasons and selecting a view only calls the view callback', () => {
    const value = branchSetFixture(), select = vi.fn()
    render(<ResearchBranchOverview value={value} selectedId={null} disabled={false} onSelect={select} />)
    expect(screen.getByText('Waiting for your answer.')).toBeVisible()
    expect(screen.getByText('Waiting for the result of Question a.')).toBeVisible()
    expect(screen.queryByText('dependency_pending')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'View this question' })).toHaveLength(3)
    fireEvent.click(screen.getAllByRole('button', { name: 'View this question' })[1])
    expect(select).toHaveBeenCalledExactlyOnceWith('b')
    expect(screen.getByText(/All questions share/)).toBeVisible()
  })
  it('displays cancellation as unfinished cleanup instead of stopped', () => {
    const value = branchSetFixture()
    value.branches[1].owned_work = { id: 'native-job', kind: 'native_role', cancellation_requested: true, outcome: 'cancellation_pending', scope: {} }
    render(<ResearchBranchOverview value={value} selectedId="b" disabled={false} onSelect={() => {}} />)
    expect(screen.getByText('Cancellation is pending cleanup; it has not stopped yet.')).toBeVisible()
  })
  it('submits all answers for a branch in one explicit request without changing their words', async () => {
    const branch = branchSetFixture().branches[0], save = vi.fn().mockResolvedValue(undefined)
    branch.questions.push({ ...branch.questions[0], id: 'second', prompt: 'What should remain excluded?', prompt_hash: 'e'.repeat(64) })
    render(<ResearchBranchAnswers branch={branch} authorityEpoch={1} storageKey="server-one:campaign-one" disabled={false} onSave={save} />)
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: '  Only the supplied source.\nKeep the original words.' } })
    expect(screen.getByRole('button', { name: 'Save answers' })).toBeDisabled()
    fireEvent.change(screen.getAllByRole('textbox')[1], { target: { value: 'No web retrieval.' } })
    expect(save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Save answers' }))
    await act(async () => {})
    expect(save).toHaveBeenCalledExactlyOnceWith([{ question_id: 'scope', answer: '  Only the supplied source.\nKeep the original words.' }, { question_id: 'second', answer: 'No web retrieval.' }])
    expect(screen.getByText(/does not choose or run an action/)).toBeVisible()
  })
  it('does not reuse a prior question draft or show its late failure after the question context changes', async () => {
    const branch = branchSetFixture().branches[0]
    let reject: (error: Error) => void = () => {}
    const save = vi.fn().mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
    const view = render(<ResearchBranchAnswers branch={branch} authorityEpoch={1} storageKey="owner" disabled={false} onSave={save} />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Old answer.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save answers' }))
    const next = { ...branch, revision: 2, questions: [{ ...branch.questions[0], prompt: 'A different question using the same ID?', prompt_hash: 'f'.repeat(64) }] }
    view.rerender(<ResearchBranchAnswers branch={next} authorityEpoch={1} storageKey="owner" disabled={false} onSave={save} />)
    expect(screen.getByRole('textbox')).toHaveValue('')
    await act(async () => reject(new Error('Late old failure')))
    expect(screen.queryByText('Late old failure')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save answers' })).toBeDisabled()
  })
})
