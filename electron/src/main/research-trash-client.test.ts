import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it, vi } from 'vitest'
import { AgentServerClient } from './server-client'
import { labFixture } from '../shared/research-lab.fixture'
import type { IdeaSession } from '../shared/idea-lab'
vi.mock('./logger', () => ({ appLog: vi.fn() }))
const idea: IdeaSession = { id: 'idea-one', revision: 1, created_at: '2026-10-04T13:00:00Z', updated_at: '2026-10-04T13:00:00Z', brief: { goal: 'Fixture', hypothesis: '', constraints: '', sources: [] }, status: 'draft', phase: null, generation_id: null, result: null, decision: null, error: null, usage: [], events: [] }

describe('Research trash native transport', () => {
  it('uses authorized, bounded routes for both labs and rejects another record or trash state', async () => {
    const requests: string[] = []
    let responseMode = 'valid'
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-trash-token')
      expect(request.headers.origin).toBeUndefined(); expect(request.headers['sec-fetch-mode']).toBeUndefined()
      requests.push(`${request.method} ${request.url}`)
      let text = ''; for await (const chunk of request) text += chunk
      if (request.method === 'POST') expect(JSON.parse(text)).toMatchObject({ expected_revision: 1 })
      const source = request.url!.includes('/ideas/') ? idea : labFixture()
      const deleted_at = request.url!.endsWith('/restore') ? null : '2026-10-04T13:00:00Z'
      const item = { ...source, deleted_at: responseMode === 'wrong-state' ? null : deleted_at, ...(responseMode === 'wrong-id' ? { id: 'other' } : {}) }
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(request.method === 'GET' ? { items: [item], has_more: false } : item))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-trash-token')
    const mutation = { expected_revision: 1, idempotency_key: 'trash-fixture' }
    const narrow = client as unknown as { privilegedNativeRequest(path: string, init?: { method: string }): Promise<unknown> }
    try {
      await client.ideaLabList(true); await client.researchLabList(undefined, true)
      await client.ideaLabTrash(idea.id, { expected_revision: 1 }); await client.ideaLabTrash(idea.id, { expected_revision: 1 }, true)
      await client.researchLabTrash('campaign-one', mutation); await client.researchLabTrash('campaign-one', mutation, true)
      expect(requests).toEqual(['GET /api/research/ideas/trash', 'GET /api/research/lab/trash', 'POST /api/research/ideas/idea-one/trash', 'POST /api/research/ideas/idea-one/restore', 'POST /api/research/lab/campaign-one/trash', 'POST /api/research/lab/campaign-one/restore'])
      for (const path of ['/api/research/ideas/trash', '/api/research/lab/trash']) await expect(narrow.privilegedNativeRequest(path, { method: 'POST' })).rejects.toThrow('route')
      for (const path of ['/api/research/ideas/idea-one/trash', '/api/research/lab/campaign-one/restore']) {
        await expect(narrow.privilegedNativeRequest(path)).rejects.toThrow('route')
        await expect(narrow.privilegedNativeRequest(`${path}?all=true`, { method: 'POST' })).rejects.toThrow('route')
      }
      responseMode = 'wrong-id'; await expect(client.ideaLabTrash(idea.id, { expected_revision: 1 })).rejects.toThrow('trash response')
      responseMode = 'wrong-state'; await expect(client.researchLabTrash('campaign-one', mutation)).rejects.toThrow('trash response')
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
})
