import { describe, expect, it } from 'vitest'
import fixtures from './research-branches.backend.fixture.json'
import { parseLabCampaign } from './research-lab'
import { branchResult } from './research-branches'
describe('Actual staged backend branch snapshots', () => {
  it.each(Object.entries(fixtures))('parses %s without rewriting saved records', (name, value) => {
    const campaign = parseLabCampaign(value)
    expect(campaign).toBe(value)
    expect(campaign.branch_set!.branches).toHaveLength(name === 'numeric_enabled' ? 1 : 3)
    for (const branch of campaign.branch_set!.branches) if (branch.latest_result) expect(branchResult(campaign, branch)?.run.id).toBe(branch.latest_result.run_id)
  })
})
