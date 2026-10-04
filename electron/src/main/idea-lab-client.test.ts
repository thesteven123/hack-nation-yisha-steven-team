import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it, vi } from 'vitest'
import { AgentServerClient } from './server-client'
import type { IdeaSession } from '../shared/idea-lab'

vi.mock('./logger', () => ({ appLog: vi.fn() }))
const frozenSource = { id: 'source-one', title: 'Saved source', uri: 'https://example.org/paper', text: 'The frozen evidence text.' }
const generationId = 'd'.repeat(32), provenanceHash = 'e'.repeat(64)
const frozenHash = createHash('sha256').update(frozenSource.text).digest('hex')
const draft: IdeaSession = {
  id: 'idea-one', revision: 1, created_at: '2026-10-03T00:00:00Z', updated_at: '2026-10-03T00:00:00Z',
  brief: { goal: 'What should we test?', hypothesis: '', constraints: '', sources: [] }, status: 'draft', phase: null,
  generation_id: null, result: null, decision: null, error: null, usage: [], events: []
}

describe('Idea Lab native transport', () => {
  it('fetches an exact historical original through the bounded native-only route without rendering it', async () => {
    const bytes = Buffer.alloc(3 * 1024 * 1024, 65), contentHash = createHash('sha256').update(bytes).digest('hex')
    const received: string[] = []
    const server = createServer((request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-token')
      expect(request.headers.origin).toBeUndefined()
      received.push(request.url!)
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify({ version: 1, status: 'retained', session_id: 'idea-one', source_id: 'source-one', source_hash: frozenHash, generation_id: generationId, provenance_hash: provenanceHash,
        content_hash: contentHash, mime: 'text/plain', fetch_id: 'b'.repeat(32), verified: true, bytes: bytes.length, body_base64: bytes.toString('base64') }))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    const narrow = client as unknown as { privilegedNativeRequest(path: string, init?: { method?: string }): Promise<unknown> }
    try {
      const result = await client.ideaLabOriginal('idea-one', 'source-one', frozenHash, generationId, provenanceHash)
      expect(result.status === 'retained' && result.body.equals(bytes)).toBe(true)
      const raw = '/api/research/ideas/idea-one/papers/source-one/raw'
      for (const path of [raw, `${raw}?source_hash=${frozenHash}&extra=1`, `${raw}?source_hash=${frozenHash}&source_hash=${frozenHash}`,
        `${raw}/?source_hash=${frozenHash}`, `/api/research/ideas/idea-one/papers/%252F/raw?source_hash=${frozenHash}`,
        `/api/research/ideas/idea-one/papers/%2F/raw?source_hash=${frozenHash}`]) await expect(narrow.privilegedNativeRequest(path)).rejects.toThrow()
      await expect(narrow.privilegedNativeRequest(`${raw}?source_hash=${frozenHash}&generation_id=${generationId}&provenance_hash=${provenanceHash}`, { method: 'POST' })).rejects.toThrow()
      await expect(client.ideaLabOriginal('idea-one', 'source-one', '', generationId, provenanceHash)).rejects.toThrow('version')
      expect(received).toEqual([`${raw}?source_hash=${frozenHash}&generation_id=${generationId}&provenance_hash=${provenanceHash}`])
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('rejects an original-file JSON response above its dedicated 16 MiB cap', async () => {
    const server = createServer((_request, response) => {
      response.setHeader('Content-Type', 'application/json')
      response.setHeader('Content-Length', 16 * 1024 * 1024 + 1)
      response.end('{}')
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    try { await expect(client.ideaLabOriginal('idea-one', 'source-one', frozenHash, generationId, provenanceHash)).rejects.toThrow(/large|limit/) }
    finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('uses authenticated Node HTTP without browser headers for all ten narrow operations', async () => {
    const received: { path: string; method: string; body: string }[] = []
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-token')
      expect(request.headers.origin).toBeUndefined()
      expect(request.headers['sec-fetch-mode']).toBeUndefined()
      let body = ''
      for await (const part of request) body += part
      received.push({ path: request.url!, method: request.method!, body })
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(request.url?.includes('/papers/') ? { source: frozenSource, paper: null, archived: true, revision: 3, packet_hash: frozenHash, generation_id: generationId, provenance_hash: provenanceHash } : request.url?.includes('/activities') ? { items: [], has_more: false, next_before: null } : request.method === 'GET' && (request.url === '/api/research/ideas' || request.url?.endsWith('/history'))
        ? { items: [draft], has_more: false } : draft))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    try {
      expect((await client.ideaLabList()).items[0].id).toBe(draft.id)
      await client.ideaLabGet(draft.id)
      await client.ideaLabHistory(draft.id)
      await client.ideaLabCreate({ idempotency_key: 'create-key', brief: draft.brief })
      await client.ideaLabGenerate(draft.id, { expected_revision: 1, idempotency_key: 'run-key' })
      await client.ideaLabCancel(draft.id, { expected_revision: 2 })
      await client.ideaLabDecision(draft.id, { expected_revision: 3, kind: 'defer', selected_id: null, feedback: 'Need better evidence' })
      await client.ideaLabFollowup(draft.id, { expected_revision: 4, idempotency_key: 'followup-key', feedback: 'Read the methods.', mode: 'research', answers: [{ question_id: 'q1', answer: 'Researchers' }] })
      await client.ideaLabActivities(draft.id, 12)
      expect((await client.ideaLabPaper(draft.id, frozenSource.id, frozenHash, generationId)).source.text).toBe(frozenSource.text)
      expect(received.map(item => [item.method, item.path])).toEqual([
        ['GET', '/api/research/ideas'], ['GET', '/api/research/ideas/idea-one'], ['GET', '/api/research/ideas/idea-one/history'],
        ['POST', '/api/research/ideas'], ['POST', '/api/research/ideas/idea-one/generate'],
        ['POST', '/api/research/ideas/idea-one/cancel'], ['POST', '/api/research/ideas/idea-one/decision'], ['POST', '/api/research/ideas/idea-one/followup'], ['GET', '/api/research/ideas/idea-one/activities?before=12'], ['GET', `/api/research/ideas/idea-one/papers/source-one?source_hash=${frozenHash}&generation_id=${generationId}`]
      ])
      expect(JSON.parse(received[6].body).feedback).toBe('Need better evidence')
      await expect(client.ideaLabGet('../admin/restart')).rejects.toThrow('identifier')
      await expect(client.ideaLabActivities(draft.id, -1)).rejects.toThrow('cursor')
      await expect(client.ideaLabPaper(draft.id, '../admin')).rejects.toThrow('identifier')
      await expect(client.ideaLabPaper(draft.id, '%2fadmin')).rejects.toThrow('identifier')
      await expect(client.ideaLabPaper(draft.id, frozenSource.id, 'invalid')).rejects.toThrow('version')
      expect(received).toHaveLength(10)
      expect(JSON.parse(received[7].body).answers).toEqual([{ question_id: 'q1', answer: 'Researchers' }])
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })

  it('rejects malformed result envelopes instead of exposing them as successful ideas', async () => {
    const server = createServer((_request, response) => { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ ...draft, result: { ideas: { directions: [{ title: 7 }], open_alternative: '' } } })) })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    try { await expect(client.ideaLabGet(draft.id)).rejects.toThrow('Idea Lab') }
    finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
  it('rejects a source body that does not match the requested frozen text hash', async () => {
    const server = createServer((_request, response) => { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ source: { ...frozenSource, text: 'A changed source body.' }, paper: null, packet_hash: frozenHash, generation_id: generationId, provenance_hash: provenanceHash })) })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    try { await expect(client.ideaLabPaper(draft.id, frozenSource.id, frozenHash)).rejects.toThrow('frozen evidence version') }
    finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })

  it('rejects a paper from another generation and an original with a changed descriptor before accepting bytes', async () => {
    const bytes = Buffer.from('same original'), server = createServer((request, response) => {
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(request.url?.includes('/raw?')
        ? { version: 1, status: 'retained', session_id: 'idea-one', source_id: 'source-one', source_hash: frozenHash, generation_id: generationId, provenance_hash: 'f'.repeat(64), content_hash: createHash('sha256').update(bytes).digest('hex'), mime: 'text/plain', fetch_id: 'b'.repeat(32), verified: true, bytes: bytes.length, body_base64: bytes.toString('base64') }
        : { source: frozenSource, paper: null, packet_hash: frozenHash, generation_id: 'f'.repeat(32), provenance_hash: provenanceHash }))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-token')
    try {
      await expect(client.ideaLabPaper('idea-one', 'source-one', frozenHash, generationId)).rejects.toThrow('generation')
      await expect(client.ideaLabOriginal('idea-one', 'source-one', frozenHash, generationId, provenanceHash)).rejects.toThrow('IDEA_ORIGINAL_INVALID')
      await expect(client.ideaLabOriginal('idea-one', 'source-one', frozenHash, '', provenanceHash)).rejects.toThrow('provenance')
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })

})
