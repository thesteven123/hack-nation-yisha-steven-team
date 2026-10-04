import { createHash } from 'node:crypto'
import type { IdeaOriginalUnavailable } from '../shared/idea-lab'

export const MAX_IDEA_ORIGINAL_BYTES = 10 * 1024 * 1024
export interface IdeaOriginalReady {
  status: 'retained'; session_id: string; source_id: string; source_hash: string; generation_id: string; provenance_hash: string
  content_hash: string; bytes: number; mime: string; fetch_id: string; body: Buffer
}

/** Original file bytes never enter renderer state or an HTML/PDF preview. */
export function parseIdeaOriginal(value: unknown, sessionId: string, sourceId: string, packetHash: string, generationId: string, provenanceHash: string): IdeaOriginalReady | IdeaOriginalUnavailable {
  const bad = () => { throw new Error('IDEA_ORIGINAL_INVALID') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return bad()
  const row = value as Record<string, unknown>
  if (row.version !== 1 || row.session_id !== sessionId || row.source_id !== sourceId || row.source_hash !== packetHash
    || !/^[a-f0-9]{32}$/.test(generationId) || !/^[a-f0-9]{64}$/.test(provenanceHash)
    || row.generation_id !== generationId || row.provenance_hash !== provenanceHash) return bad()
  if (row.status === 'not_retained' || row.status === 'missing' || row.status === 'corrupt') {
    if ('body_base64' in row) return bad()
    return { status: row.status, session_id: sessionId, source_id: sourceId, source_hash: packetHash, generation_id: generationId, provenance_hash: provenanceHash }
  }
  if (row.status !== 'retained' || row.verified !== true
    || typeof row.content_hash !== 'string' || !/^[a-f0-9]{64}$/.test(row.content_hash)
    || typeof row.fetch_id !== 'string' || !/^[a-f0-9]{32}$/.test(row.fetch_id)
    || typeof row.mime !== 'string' || Buffer.byteLength(row.mime, 'utf8') > 1024 || /[\x00-\x1f\x7f]/.test(row.mime)
    || !Number.isSafeInteger(row.bytes) || (row.bytes as number) < 1 || (row.bytes as number) > MAX_IDEA_ORIGINAL_BYTES
    || typeof row.body_base64 !== 'string' || row.body_base64.length > Math.ceil(MAX_IDEA_ORIGINAL_BYTES / 3) * 4) return bad()
  // Re-encoding rejects noncanonical padding, whitespace and Buffer's permissive
  // base64 decoder behavior without an unbounded recursive regular expression.
  const body = Buffer.from(row.body_base64 as string, 'base64')
  if (body.toString('base64') !== row.body_base64 || body.length !== row.bytes
    || createHash('sha256').update(body).digest('hex') !== row.content_hash) return bad()
  return { status: 'retained', session_id: sessionId, source_id: sourceId, source_hash: packetHash, generation_id: generationId, provenance_hash: provenanceHash,
    content_hash: row.content_hash as string, bytes: body.length, mime: row.mime as string,
    fetch_id: row.fetch_id as string, body }
}

export function ideaOriginalFilename(sourceId: string, mime: string): string {
  const extension = ({ 'application/pdf': 'pdf', 'text/html': 'html', 'application/xhtml+xml': 'html', 'text/plain': 'txt' } as Record<string, string>)[mime.split(';')[0].trim().toLowerCase()] ?? 'bin'
  return `source-${sourceId.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 48) || 'document'}.${extension}`
}
