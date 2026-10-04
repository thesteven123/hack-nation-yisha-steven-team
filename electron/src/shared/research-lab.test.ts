import { describe, expect, it } from 'vitest'
import { labFixture, labRoundFixture, labSeedFixture, labProtocolFixture, labExportFixture, labHypothesisFixture } from './research-lab.fixture'
import { labHash, labId, labProtocolId, parseLabArtifact, parseLabCampaign, parseLabCapabilities, parseLabHistory, parseLabPage, parseLabSeed, parseLabProtocol, parseLabExport } from './research-lab'

describe('Research Lab contract validation', () => {
  it('retains versioned human hypotheses and rejects malformed frozen references or scientific validation claims', () => {
    const hypothesis = labHypothesisFixture()
    const campaign = { ...labFixture(), hypothesis_set: hypothesis, hypothesis_set_artifact: 'e'.repeat(64), hypothesis_set_status: 'needs_revalidation' }
    expect(parseLabCampaign(campaign).hypothesis_set).toBe(hypothesis)
    expect(parseLabCampaign(campaign).hypothesis_set_status).toBe('needs_revalidation')
    expect(() => parseLabCampaign({ ...campaign, hypothesis_set: { ...hypothesis, scientific_status: 'validated' } })).toThrow()
    hypothesis.candidates[0].supporting_evidence[0].artifact = 'wrong-hash'
    expect(() => parseLabCampaign(campaign)).toThrow()
    expect(parseLabCampaign(labFixture()).hypothesis_set).toBeUndefined()
  })

  it('preserves current dependency guards and rejects a malformed run permission', () => {
    const campaign = labFixture()
    campaign.dependency_status = { state: 'correction_pending', run_allowed: false, current_input_blocked: true, affected_claim_ids: ['claim'], pending_intents: [{ id: 'intent', reason: 'Correction awaiting its receipt', affected_input_hashes: ['a'.repeat(64)] }], corrections: [], pending_deliveries: 1 }
    expect(parseLabCampaign(campaign).dependency_status?.run_allowed).toBe(false)
    expect(() => parseLabCampaign({ ...campaign, dependency_status: { ...campaign.dependency_status, run_allowed: 'yes' } })).toThrow()
  })
  it('validates exact summary identity and keeps export records within the selected campaign', () => {
    expect(parseLabProtocol(labProtocolFixture(), 'planning-v0.5').is_full_source).toBe(false)
    expect(() => parseLabProtocol({ ...labProtocolFixture(), is_full_source: true }, 'planning-v0.5')).toThrow()
    expect(() => labProtocolId('../other')).toThrow()
    const bundle = labExportFixture()
    expect(parseLabExport(bundle, 'campaign-one')).toBe(bundle)
    expect(() => parseLabExport(bundle, 'other-campaign')).toThrow()
    bundle.data.versions[0].payload.id = 'other-campaign'
    expect(() => parseLabExport(bundle, 'campaign-one')).toThrow()
  })
  it('accepts full and list envelopes, additive fields, and precise Idea seed', () => {
    const campaign = { ...labFixture(), rounds: [labRoundFixture()], task_packet: { purpose: 'actual extension' } }
    expect(parseLabCampaign(campaign)).toBe(campaign)
    expect(parseLabPage({ items: [campaign], has_more: false }).items).toEqual([campaign])
    expect(parseLabSeed(labSeedFixture()).origin.selected_ids).toEqual(['direction-one'])
    expect(parseLabCapabilities({ schema_version: 1, adapters: [campaign.adapter], execution: {} }).adapters).toHaveLength(1)
    expect(parseLabHistory({ items: [{ revision: 1, at: '', event: '', brief: campaign.brief, status: campaign.status }], has_more: false }).items).toHaveLength(1)
  })
  it('rejects unsafe paths, nonlocal adapters, scientific replication claims, and malformed QC', () => {
    for (const id of ['../admin', '%2fadmin', '', 'x/y']) expect(() => labId(id)).toThrow()
    expect(() => labHash('x'.repeat(64))).toThrow()
    const campaign = labFixture()
    expect(() => parseLabCampaign({ ...campaign, adapter: { ...campaign.adapter, execution: 'external' } })).toThrow()
    const round = labRoundFixture()
    expect(() => parseLabCampaign({ ...campaign, rounds: [{ ...round, run: { ...round.run, is_independent_replicate: true } }] })).toThrow()
    expect(() => parseLabCampaign({ ...campaign, rounds: [{ ...round, qc: { passed: 'yes', checks: [] } }] })).toThrow()
    expect(() => parseLabCampaign({ ...campaign, status: 'science_proved' })).toThrow()
  })
  it('rejects wrong artifact identity and unknown media types', () => {
    expect(parseLabArtifact({ sha256: 'a'.repeat(64), media_type: 'application/json', content: { quote: 'test' } }, 'a'.repeat(64)).content).toEqual({ quote: 'test' })
    expect(() => parseLabArtifact({ sha256: 'b'.repeat(64), media_type: 'application/json', content: {} }, 'a'.repeat(64))).toThrow()
    expect(() => parseLabArtifact({ sha256: 'a'.repeat(64), media_type: 'text/html', content: '' }, 'a'.repeat(64))).toThrow()
  })
})
