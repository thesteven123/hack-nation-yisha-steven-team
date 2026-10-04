import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it, vi } from 'vitest'
import { AgentServerClient } from './server-client'
import { labFixture, labSeedFixture, labExportFixture, labProtocolFixture } from '../shared/research-lab.fixture'
vi.mock('./logger', () => ({ appLog: vi.fn() }))

describe('Research Lab native transport', () => {
  it('allows only an exact read-only dependency export path at the native transport boundary', async () => {
    const requests: string[] = []
    const server = createServer((request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-dependency-token')
      expect(request.headers.origin).toBeUndefined()
      requests.push(`${request.method} ${request.url}`)
      response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ scope: 'single-campaign-sidecar' }))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-dependency-token')
    const boundary = client as unknown as { privilegedNativeRequest(path: string, init: RequestInit, timeout: number, status: number, maxBytes: number): Promise<unknown> }
    const invoke = (path: string, method = 'GET') => boundary.privilegedNativeRequest(path, { method }, 5000, 200, 1024 * 1024)
    try {
      await expect(invoke('/api/research/lab/campaign-one/dependencies/export')).resolves.toEqual({ scope: 'single-campaign-sidecar' })
      for (const path of ['/api/research/lab/campaign-one/dependencies/other', '/api/research/lab/campaign-one/dependencies/export?all=true', '/api/research/lab/campaign-one/dependencies/export/extra', '/api/research/lab/%2Fother/dependencies/export']) await expect(invoke(path)).rejects.toThrow('route')
      await expect(invoke('/api/research/lab/campaign-one/dependencies/export', 'POST')).rejects.toThrow('route')
      expect(requests).toEqual(['GET /api/research/lab/campaign-one/dependencies/export'])
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('allows bounded escaped revisions only on the approved decision route', async () => {
    const received: string[] = []
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-revision-token')
      expect(request.headers.origin).toBeUndefined()
      let body = ''; for await (const part of request) body += part
      received.push(body); response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(labFixture()))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-revision-token')
    try {
      const text = 'a\u0000'.repeat(3400), common = { expected_revision: 1, idempotency_key: 'revision-one' }
      const revision = { ...common, kind: 'revise' as const, goal: text, hypothesis: text, success_criteria: text, constraints: text, feedback: text }
      expect(Buffer.byteLength(JSON.stringify(revision))).toBeGreaterThan(64 * 1024)
      await client.researchLabDecision('campaign-one', revision)
      expect(JSON.parse(received[0])).toEqual(revision)
      await expect(client.researchLabDecision('campaign-one', { ...revision, feedback: 'x'.repeat(384 * 1024) })).rejects.toThrow()
      await expect(client.researchLabRun('campaign-one', { ...common, extra: 'x'.repeat(70 * 1024) } as typeof common)).rejects.toThrow()
      expect(received).toHaveLength(1)
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('reads only approved protocols and preserves complete export bytes up to the dedicated 16 MiB cap', async () => {
    let mode = 'valid'
    const server = createServer((request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-export-token')
      const bundle = labExportFixture()
      if (mode === 'large' || mode === 'oversize') bundle.data.artifacts.push({ sha256: 'a'.repeat(64), encoding: 'base64', data: 'A'.repeat((mode === 'large' ? 5 : 17) * 1024 * 1024) })
      if (mode === 'other') bundle.campaign_id = 'other'
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(request.url!.includes('/protocols/') ? labProtocolFixture() : bundle).replace('"wall_ms":0', '"wall_ms":1.0'))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-export-token')
    try {
      expect((await client.researchLabProtocol('planning-v0.5')).content).toBe('Freeze rules before observation.')
      await expect(client.researchLabProtocol('unsupported')).rejects.toThrow('protocol')
      expect((await client.researchLabExport('campaign-one')).json).toContain('"wall_ms":1.0')
      mode = 'large'; expect((await client.researchLabExport('campaign-one')).json.length).toBeGreaterThan(5 * 1024 * 1024)
      mode = 'other'; await expect(client.researchLabExport('campaign-one')).rejects.toThrow('Research Lab')
      mode = 'oversize'; await expect(client.researchLabExport('campaign-one')).rejects.toThrow()
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('dispatches all eleven narrow operations with native authorization and validates path segments', async () => {
    const requests: { path: string; method: string; body: string }[] = [], campaign = labFixture(), seed = labSeedFixture()
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-research-token')
      expect(request.headers.origin).toBeUndefined(); expect(request.headers['sec-fetch-mode']).toBeUndefined()
      let body = ''; for await (const part of request) body += part
      const path = request.url!; requests.push({ path, method: request.method!, body })
      const bare = path.split('?')[0]
      const output = path.endsWith('/capabilities') ? { schema_version: 1, adapters: [campaign.adapter], execution: {} } : path.includes('/idea-seed/') ? seed : path.includes('/artifacts/') ? { sha256: 'a'.repeat(64), media_type: 'application/json', content: seed.inputs } : bare.endsWith('/history') ? { items: [], has_more: false } : request.method === 'GET' && bare === '/api/research/lab' ? { items: [campaign], has_more: false } : campaign
      response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(output))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-research-token')
    try {
      await client.researchLabCapabilities(); await client.researchLabList(); await client.researchLabGet(campaign.id); await client.researchLabIdeaSeed('idea-one')
      await client.researchLabCreate({ idempotency_key: 'create-one', entry: 'goal', brief: seed.brief, adapter_id: 'source_evidence', inputs: seed.inputs, budget: { max_actions: 4, max_rounds: 3 }, origin: seed.origin })
      const common = { expected_revision: 1, idempotency_key: 'mutation-one' }
      await client.researchLabDecision(campaign.id, { ...common, kind: 'select', selected_action_id: 'action-one', feedback: 'Exact human text.' })
      await client.researchLabRun(campaign.id, common); await client.researchLabAdvance(campaign.id, common)
      await client.researchLabCorrectInputs(campaign.id, { ...common, inputs: seed.inputs, reason: 'Corrected source version' })
      await client.researchLabHistory(campaign.id); await client.researchLabArtifact(campaign.id, 'a'.repeat(64))
      expect(requests.map(r => [r.method, r.path])).toEqual([['GET', '/api/research/lab/capabilities'], ['GET', '/api/research/lab'], ['GET', '/api/research/lab/campaign-one'], ['GET', '/api/research/lab/idea-seed/idea-one'], ['POST', '/api/research/lab'], ['POST', '/api/research/lab/campaign-one/decision'], ['POST', '/api/research/lab/campaign-one/run'], ['POST', '/api/research/lab/campaign-one/continue'], ['POST', '/api/research/lab/campaign-one/correct-inputs'], ['GET', '/api/research/lab/campaign-one/history'], ['GET', `/api/research/lab/campaign-one/artifacts/${'a'.repeat(64)}`]])
      expect(JSON.parse(requests[5].body).feedback).toBe('Exact human text.')
      await expect(client.researchLabGet('../admin')).rejects.toThrow('identifier')
      await expect(client.researchLabIdeaSeed('%2fadmin')).rejects.toThrow('identifier')
      await expect(client.researchLabArtifact(campaign.id, 'bad')).rejects.toThrow('hash')
      expect(requests).toHaveLength(11)
      await client.researchLabList('opaque_cursor'); await client.researchLabHistory(campaign.id, 'history_cursor')
      expect(requests.slice(-2).map(r => r.path)).toEqual(['/api/research/lab?before=opaque_cursor&limit=50', '/api/research/lab/campaign-one/history?before=history_cursor&limit=50'])
      await expect(client.researchLabList('cursor&injected=true')).rejects.toThrow('cursor')
      expect(requests).toHaveLength(13)
      await client.researchLabReconcileDependencies(campaign.id)
      expect(requests.at(-1)).toEqual({ path: '/api/research/lab/campaign-one/dependencies/reconcile', method: 'POST', body: '{}' })
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
})
