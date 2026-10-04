import { describe, expect, it } from 'vitest'
import { modelJobFixture } from './research-models.fixture'
import { parseResearchJob, parseResearchModelArtifact, parseResearchModelPage } from './research-models'
describe('Research role interpretation envelopes', () => {
  it('accepts each bounded role and current unknown usage without treating null as zero', () => {
    for (const role of ['planner', 'analyst', 'reviewer'] as const) expect(parseResearchJob(modelJobFixture(role, 'completed'), 'campaign-one').usage.total_tokens).toBeNull()
    expect(parseResearchModelPage({ items: [modelJobFixture()], has_more: true, next_before: 8, quota: { limit_jobs: 6, used: 1, reserved: 0 } }, 'campaign-one').next_before).toBe(8)
  })
  it('rejects another campaign/job, changed machine records, unvalidated output, and invalid references', () => {
    expect(() => parseResearchJob(modelJobFixture(), 'other')).toThrow()
    expect(() => parseResearchJob(modelJobFixture(), 'campaign-one', 'other-job')).toThrow()
    const job = modelJobFixture('planner', 'completed')
    expect(() => parseResearchJob({ ...job, output: { ...job.output, machine_records_modified: true } }, 'campaign-one')).toThrow()
    expect(() => parseResearchJob({ ...job, status: 'failed' }, 'campaign-one')).toThrow()
    expect(() => parseResearchJob({ ...job, output: null }, 'campaign-one')).toThrow()
    expect(() => parseResearchModelArtifact({ sha256: 'other', content: {} }, 'a'.repeat(64))).toThrow()
  })
})
