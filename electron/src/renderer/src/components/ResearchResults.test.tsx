import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { setLocale } from '@shared/i18n'
import type { LabCampaign } from '@shared/research-lab'
import { labRoundFixture } from '@shared/research-lab.fixture'
import { ResearchComparison, ResearchResult } from './ResearchResults'

beforeEach(() => setLocale('en'))
afterEach(cleanup)
const comparison = (): NonNullable<LabCampaign['comparisons']>[number] => ({ id: 'comparison', run_ids: ['a', 'b'], rule: 'Compare fixed records.', comparability: 'same_inputs_different_analysis', eligible_for_pooled_confirmation: false, source_support: { before: 'supported', after: 'cannot_determine' }, inference_validity: { before: 'cannot_determine', after: 'cannot_determine' }, changed_observation_fields: [], limitations: [], unresolved: [], artifact: 'a'.repeat(64) })
it('does not imply a support downgrade when the two recorded propositions differ', () => {
  const value = { ...comparison(), propositions: ['The mean exceeds the threshold.', 'The result is insensitive to omitting one pair.'] }
  render(<ResearchComparison comparison={value} onArtifact={() => {}} artifacts={{}} />)
  expect(screen.getByText(/different propositions/)).toBeVisible()
  expect(screen.queryByText(/→/)).not.toBeInTheDocument()
  expect(screen.getByText(value.propositions[0])).not.toBeVisible()
  expect(screen.getByText(value.propositions[1])).not.toBeVisible()
})
it('shows a support transition only for identical recorded propositions', () => {
  render(<ResearchComparison comparison={{ ...comparison(), propositions: ['Same question', 'Same question'] }} onArtifact={() => {}} artifacts={{}} />)
  expect(screen.getByText(/→/)).toBeVisible()
})
it('formats displayed numeric ranges but preserves exact raw observation values', () => {
  const round = labRoundFixture()
  round.observation = { kind: 'extreme_sensitivity', coverage: {}, data: { pairs: 4, mean_difference: -0.5, descriptive_interval: [-7.359999999999999, 6.359999999999999], minimum_effect: 0, unit: 'points', sensitivity_mean: 3 } }
  const before = JSON.stringify(round.observation)
  render(<ResearchResult round={round} invalidated onArtifact={() => {}} artifacts={{}} />)
  expect(screen.getByText('[-7.36, 6.36] points')).toBeVisible()
  expect(screen.getByText(/The goal or inputs supporting this conclusion changed/)).toBeVisible()
  expect(screen.getByText(/7.359999999999999/)).not.toBeVisible()
  expect(JSON.stringify(round.observation)).toBe(before)
})
it('loads the actual dispatched human decision only on an explicit scoped artifact click', () => {
  const round = labRoundFixture(), onArtifact = vi.fn()
  round.run.dispatch_artifact = 'e'.repeat(64)
  render(<ResearchResult round={round} invalidated={false} onArtifact={onArtifact} artifacts={{}} />)
  expect(onArtifact).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('Observation, QC and frozen artifacts'))
  fireEvent.click(screen.getByRole('button', { name: 'Read the dispatched task and actual human decision' }))
  expect(onArtifact).toHaveBeenCalledWith('e'.repeat(64))
})
