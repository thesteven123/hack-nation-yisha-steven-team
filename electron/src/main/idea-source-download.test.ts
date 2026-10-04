import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ideaOriginalFilename, MAX_IDEA_ORIGINAL_BYTES, parseIdeaOriginal } from './idea-source-download'

const packetHash = 'a'.repeat(64), generationId = 'd'.repeat(32), provenanceHash = 'e'.repeat(64)
function envelope(body = Buffer.from('<html><script>doNotExecute()</script>\r\n原文</html>')) {
  return { version: 1, status: 'retained', session_id: 'idea', source_id: 'source', source_hash: packetHash, generation_id: generationId, provenance_hash: provenanceHash,
    content_hash: createHash('sha256').update(body).digest('hex'), bytes: body.length, mime: 'text/html; charset=utf-8',
    fetch_id: 'b'.repeat(32), verified: true, body_base64: body.toString('base64') }
}
const parse = (value: unknown) => parseIdeaOriginal(value, 'idea', 'source', packetHash, generationId, provenanceHash)

describe('Idea original file verification', () => {
  it('preserves exact bytes rather than rendering HTML or using the packet hash for file integrity', () => {
    const input = envelope(), result = parse(input)
    expect(result.status).toBe('retained')
    if (result.status !== 'retained') throw new Error('Expected original')
    expect(result.body.toString()).toBe('<html><script>doNotExecute()</script>\r\n原文</html>')
    expect(result.content_hash).not.toBe(result.source_hash)
    expect(result.body.toString('base64')).toBe(input.body_base64)
  })
  it('never accepts another session, source packet, fetch metadata, malformed base64 or damaged bytes', () => {
    const valid = envelope()
    for (const change of [{ session_id: 'other' }, { source_id: 'other' }, { source_hash: 'c'.repeat(64) },
      { generation_id: 'f'.repeat(32) }, { provenance_hash: 'f'.repeat(64) }, { generation_id: undefined }, { provenance_hash: undefined }, { verified: false }, { fetch_id: '../path' }, { mime: 'text/html\nX-injected' },
      { bytes: valid.bytes + 1 }, { content_hash: packetHash }, { body_base64: valid.body_base64 + '\n' },
      { body_base64: '!!!' }, { body_base64: valid.body_base64.replace(/.$/, '') }]) {
      expect(() => parse({ ...valid, ...change })).toThrow('IDEA_ORIGINAL_INVALID')
    }
  })
  it('reports unavailable original versions without bytes and rejects a hidden body on those statuses', () => {
    for (const status of ['not_retained', 'missing', 'corrupt']) {
      const unavailable = { version: 1, session_id: 'idea', source_id: 'source', source_hash: packetHash, generation_id: generationId, provenance_hash: provenanceHash, status }
      expect(parse(unavailable)).toEqual({ status, session_id: 'idea', source_id: 'source', source_hash: packetHash, generation_id: generationId, provenance_hash: provenanceHash })
      expect(() => parse({ ...unavailable, body_base64: '' })).toThrow('IDEA_ORIGINAL_INVALID')
    }
  })
  it('accepts the exact bounded maximum and rejects oversized input before decoding', () => {
    const maximum = envelope(Buffer.alloc(MAX_IDEA_ORIGINAL_BYTES, 37))
    const result = parse(maximum)
    expect(result.status === 'retained' && result.body.length).toBe(MAX_IDEA_ORIGINAL_BYTES)
    expect(() => parse({ ...maximum, bytes: MAX_IDEA_ORIGINAL_BYTES + 1 })).toThrow('IDEA_ORIGINAL_INVALID')
    expect(() => parse({ ...maximum, body_base64: maximum.body_base64 + 'AAAA' })).toThrow('IDEA_ORIGINAL_INVALID')
  })
  it('derives a safe suggested filename from the source identity and a fixed MIME extension mapping', () => {
    expect(ideaOriginalFilename('CON:../\\unsafe', 'text/html; charset=utf-8')).toBe('source-CON_____unsafe.html')
    expect(ideaOriginalFilename('paper', 'application/pdf')).toBe('source-paper.pdf')
    expect(ideaOriginalFilename('paper', 'application/x-msdownload')).toBe('source-paper.bin')
  })
})
