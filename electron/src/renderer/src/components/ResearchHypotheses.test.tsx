import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import { labFixture, labHypothesisFixture, labRoundFixture, labSeedFixture } from '@shared/research-lab.fixture'
import { HypothesisEditor, HypothesisSetView, editableHypothesisSet, hypothesisEvidenceOptions, newHypothesisSet, validateHypothesisInput } from './ResearchHypotheses'

beforeEach(() => setLocale('en'))
afterEach(cleanup)
describe('Human-authored hypothesis sets', () => {
  it('does not invent predictions, evidence or alternatives when enabled', () => {
    const changed = vi.fn()
    render(<HypothesisEditor value={null} onChange={changed} options={[]} disabled={false} initialStatement="My existing summary" />)
    expect(changed).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Record a versioned hypothesis set' }))
    const value = changed.mock.calls[0][0]
    expect(value.candidates[0]).toMatchObject({ statement: 'My existing summary', predictions: [''], weakening_conditions: [''], supporting_evidence: [], opposing_evidence: [] })
    expect(value.open_alternative).toBe('')
  })
  it('offers only actual source and observation identities, including projected earlier rounds', () => {
    const campaign = labFixture(); campaign.rounds = [labRoundFixture()]; campaign.omitted_rounds = [{ index: 2, run_id: 'earlier-run', artifact: 'd'.repeat(64) }]
    const options = hypothesisEvidenceOptions(labSeedFixture().inputs.sources, campaign)
    expect(options.map(option => option.value)).toEqual([{ kind: 'input' }, { kind: 'source', source_id: 'source-one' }, { kind: 'observation', run_id: 'run-one' }, { kind: 'observation', run_id: 'earlier-run' }])
    const input = editableHypothesisSet(labHypothesisFixture())
    expect(input.candidates[0].supporting_evidence).toEqual([{ kind: 'source', source_id: 'source-one' }])
    expect(JSON.stringify(input)).not.toContain('artifact')
    expect(validateHypothesisInput(input, options).candidates).toHaveLength(1)
  })
  it('rejects missing predictions, out-of-scope references and oversized UTF-8 input', () => {
    const options = hypothesisEvidenceOptions(labSeedFixture().inputs.sources)
    expect(() => validateHypothesisInput(newHypothesisSet('A hypothesis'), options)).toThrow()
    const input = editableHypothesisSet(labHypothesisFixture())
    input.candidates[0].supporting_evidence = [{ kind: 'observation', run_id: 'other-campaign-run' }]
    expect(() => validateHypothesisInput(input, options)).toThrow()
    input.candidates[0].supporting_evidence = []; input.candidates[0].statement = '文'.repeat(1334)
    expect(() => validateHypothesisInput(input, options)).toThrow()
  })
  it('supports parallel alternatives without choosing an explanation for the user', () => {
    const value = editableHypothesisSet(labHypothesisFixture()), changed = vi.fn()
    render(<HypothesisEditor value={value} onChange={changed} options={hypothesisEvidenceOptions(labSeedFixture().inputs.sources)} disabled={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add an alternative explanation' }))
    const next = changed.mock.calls[0][0]
    expect(next.candidates).toHaveLength(2)
    expect(next.candidates[0]).toEqual(value.candidates[0]); expect(next.candidates[1].statement).toBe('')
    expect(next.open_alternative).toEqual(value.open_alternative)
  })
  it('keeps details folded and reads frozen records only after an explicit click', () => {
    const value = labHypothesisFixture(), read = vi.fn(); value.post_outcome = true; value.previous_artifact = 'f'.repeat(64)
    render(<HypothesisSetView value={value} status="needs_revalidation" artifact={'e'.repeat(64)} onArtifact={read} artifacts={{}} />)
    expect(screen.getByText(/goal or inputs changed/)).toBeVisible()
    expect(screen.getByText(/introduced or revised after observations/)).toBeVisible()
    screen.getAllByText(value.candidates[0].statement).forEach(node => expect(node).not.toBeVisible())
    expect(read).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Hypothesis set · version 1 · 1 explanations'))
    fireEvent.click(screen.getByText('Version and frozen record'))
    fireEvent.click(screen.getByRole('button', { name: 'Read this frozen hypothesis set' }))
    expect(read).toHaveBeenCalledExactlyOnceWith('e'.repeat(64))
    fireEvent.click(screen.getByRole('button', { name: 'Read the previous hypothesis version' }))
    expect(read).toHaveBeenLastCalledWith('f'.repeat(64))
  })
})
