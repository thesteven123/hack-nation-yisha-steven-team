import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it, vi } from 'vitest'
import { AgentServerClient } from './server-client'
import { modelJobFixture } from '../shared/research-models.fixture'
vi.mock('./logger', () => ({ appLog: vi.fn() }))
describe('Research model native transport', () => {
  it('scopes all seven routes, read-only waits, paginated history and cancellation to a campaign/job', async () => {
    const requests: { method: string; path: string; body: string }[] = []
    let wrong = false
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-model-token'); expect(request.headers.origin).toBeUndefined()
      let body = ''; for await (const part of request) body += part
      const path = request.url!; requests.push({ method: request.method!, path, body })
      const job = modelJobFixture(); if (wrong) job.campaign_id = 'wrong-campaign'
      const result = path.includes('/artifacts/') ? { sha256: 'a'.repeat(64), content: { frozen: 'packet' } } : request.method === 'GET' && /model-jobs(?:\?|$)/.test(path) ? { items: [job], has_more: false, next_before: null, quota: { limit_jobs: 6, used: 1, reserved: 0 } } : job
      response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(result))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-model-token')
    try {
      await client.researchModelList('campaign-one'); await client.researchModelList('campaign-one', 18)
      await client.researchModelCreate('campaign-one', { role: 'planner', expected_revision: 1, idempotency_key: 'explicit-key' })
      await client.researchModelGet('campaign-one', 'job-planner'); await client.researchModelWait('campaign-one', 'job-planner')
      await client.researchModelStart('campaign-one', 'job-planner'); await client.researchModelCancel('campaign-one', 'job-planner')
      await client.researchModelArtifact('campaign-one', 'job-planner', 'a'.repeat(64))
      expect(requests.map(r => [r.method, r.path])).toEqual([
        ['GET', '/api/research/lab/campaign-one/model-jobs'], ['GET', '/api/research/lab/campaign-one/model-jobs?before=18&limit=50'], ['POST', '/api/research/lab/campaign-one/model-jobs'],
        ['GET', '/api/research/lab/campaign-one/model-jobs/job-planner'], ['GET', '/api/research/lab/campaign-one/model-jobs/job-planner/wait'], ['POST', '/api/research/lab/campaign-one/model-jobs/job-planner/start'],
        ['POST', '/api/research/lab/campaign-one/model-jobs/job-planner/cancel'], ['GET', `/api/research/lab/campaign-one/model-jobs/job-planner/artifacts/${'a'.repeat(64)}`]
      ])
      expect(JSON.parse(requests[2].body)).toEqual({ role: 'planner', expected_revision: 1, idempotency_key: 'explicit-key' }); expect(requests[4].body).toBe('')
      await expect(client.researchModelWait('campaign-one', '../job')).rejects.toThrow('identifier')
      await expect(client.researchModelList('campaign-one', -1)).rejects.toThrow('cursor')
      await expect(client.researchModelArtifact('other/../campaign', 'job-planner', 'a'.repeat(64))).rejects.toThrow('identifier')
      expect(requests).toHaveLength(8)
      wrong = true; await expect(client.researchModelGet('campaign-one', 'job-planner')).rejects.toThrow('Research model')
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
})
