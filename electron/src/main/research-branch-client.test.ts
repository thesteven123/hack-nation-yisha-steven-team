import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it, vi } from 'vitest'
import { AgentServerClient } from './server-client'
import { labFixture } from '../shared/research-lab.fixture'
import { branchSetFixture } from '../shared/research-branches.fixture'
import { modelJobFixture } from '../shared/research-models.fixture'
vi.mock('./logger', () => ({ appLog: vi.fn() }))
describe('Branch native transport', () => {
  it('pins exact branch operations, bounded escaped answers and scoped model pages without broadening neighbours', async () => {
    const requests: { path: string; method: string; body: string }[] = []
    const snapshot = { ...labFixture(), branch_set: branchSetFixture() }
    let wrong = false
    const server = createServer(async (request, response) => {
      expect(request.headers['x-agentsdock-token']).toBe('synthetic-branches')
      expect(request.headers.origin).toBeUndefined()
      let body = ''; for await (const chunk of request) body += chunk
      const url = request.url!; requests.push({ path: url, method: request.method!, body })
      const job = { ...modelJobFixture(), branch_id: wrong ? 'other' : 'b', branch_scope: { sha256: 'b'.repeat(64) }, scope_status: 'current' }
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(url.includes('/model-jobs') ? request.method === 'GET' ? { items: [job], quota: { used: 0, reserved: 1, limit_jobs: 6 }, has_more: false, next_before: null } : job : snapshot))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const client = new AgentServerClient(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, 'synthetic-branches')
    const boundary = client as unknown as { privilegedNativeRequest(path: string, init: RequestInit, timeout: number, status: number, cap: number): Promise<unknown> }
    const request = (path: string, method = 'GET') => boundary.privilegedNativeRequest(path, { method }, 2000, 200, 4 * 1024 * 1024)
    try {
      const common = { expected_branch_revision: 1, expected_authority_epoch: 2, idempotency_key: 'exact-one' }
      await client.researchBranchesGet(snapshot.id)
      await client.researchBranchesEnable(snapshot.id, { expected_revision: 1, idempotency_key: 'enable', branches: [] })
      await client.researchBranchesPlan(snapshot.id, 'b', common)
      const answers = ['a', 'b', 'c'].map(question_id => ({ question_id, answer: '\u0000'.repeat(7000) }))
      expect(Buffer.byteLength(JSON.stringify(answers))).toBeGreaterThan(64 * 1024)
      await client.researchBranchesAnswers(snapshot.id, 'b', { ...common, answers })
      await client.researchBranchesDecision(snapshot.id, 'b', { ...common, selected_action_id: 'action-one', feedback: '  exact human text\n' })
      await client.researchBranchesControl(snapshot.id, 'b', { ...common, operation: 'pause', feedback: '' })
      await client.researchBranchesRun(snapshot.id, 'b', common)
      expect(requests.slice(0, 7).map(row => [row.method, row.path])).toEqual([['GET', `/api/research/lab/${snapshot.id}/branches`], ['POST', `/api/research/lab/${snapshot.id}/branches`], ...['plan', 'answers', 'decision', 'control', 'run'].map(op => ['POST', `/api/research/lab/${snapshot.id}/branches/b/${op}`])])
      expect(JSON.parse(requests[3].body).answers).toEqual(answers)
      expect(JSON.parse(requests[4].body).feedback).toBe('  exact human text\n')
      await expect(client.researchBranchesAnswers(snapshot.id, 'b', { ...common, answers: [{ question_id: 'q', answer: 'x'.repeat(384 * 1024) }] })).rejects.toThrow()
      await expect(client.researchLabRun(snapshot.id, { expected_revision: 1, idempotency_key: 'x'.repeat(70 * 1024) })).rejects.toThrow()
      for (const path of ['/branches/b/answers?all=true', '/branches/b/answers/extra', '/branches/b/artifacts', '/branches/%2Fother/run', '/branches/bb/run?branch_id=b']) await expect(request(`/api/research/lab/${snapshot.id}${path}`, 'POST')).rejects.toThrow('route')
      await expect(request(`/api/research/lab/${snapshot.id}/branches/b/run`)).rejects.toThrow('route')
      await expect(client.researchBranchesRun(snapshot.id, '../b', common)).rejects.toThrow('identifier')
      expect(requests).toHaveLength(7)
      await client.researchModelList(snapshot.id, undefined, 'b'); await client.researchModelList(snapshot.id, 12, 'b')
      await client.researchModelCreate(snapshot.id, { role: 'planner', branch_id: 'b', expected_branch_revision: 1, expected_authority_epoch: 2, idempotency_key: 'explicit-native' })
      expect(requests.slice(7).map(row => row.path)).toEqual([`/api/research/lab/${snapshot.id}/model-jobs?branch_id=b`, `/api/research/lab/${snapshot.id}/model-jobs?branch_id=b&before=12&limit=50`, `/api/research/lab/${snapshot.id}/model-jobs`])
      expect(JSON.parse(requests[9].body)).not.toHaveProperty('expected_revision')
      wrong = true
      await expect(client.researchModelList(snapshot.id, undefined, 'b')).rejects.toThrow('Research model')
      await expect(client.researchModelCreate(snapshot.id, { role: 'planner', branch_id: 'b', expected_branch_revision: 1, expected_authority_epoch: 2, idempotency_key: 'same' })).rejects.toThrow('Research model')
    } finally { client.dispose(); await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() }) }
  })
})
