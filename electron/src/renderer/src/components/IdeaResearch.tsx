import { useEffect, useRef, useState } from 'react'
import type { WorkspaceProfileScope } from '@shared/types'
import { t } from '@shared/i18n'
import type { IdeaActivity, IdeaSession, IdeaSource, IdeaEvidence, IdeaPaper, IdeaPaperResult, IdeaLiteratureCache } from '@shared/idea-lab'
import { ExternalLink, Search, BookOpen } from 'lucide-react'

export function revealEvidence(id: string) {
  const element = document.getElementById(`idea-evidence-${id}`)
  for (let parent: HTMLElement | null = element; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLDetailsElement) parent.open = true
  }
  element?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  element?.focus({ preventScroll: true })
}
export function SourceLink({ url, onError }: { url: string; onError: (message: string) => void }) {
  let safe = false
  try { const parsed = new URL(url); safe = ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password } catch { /* A supplied label is not a link. */ }
  return safe ? <button className="idea-lab-link" onClick={() => void window.agentsDock.native.openExternal(url).catch(error => onError(String(error)))}><ExternalLink size={12} />{url}</button> : <span className="idea-lab-note">{url}</span>
}
const describe = (value: unknown): string => typeof value === 'string' ? value : value == null ? '' : Array.isArray(value) ? value.map(describe).join(' · ') : typeof value === 'object' ? Object.entries(value).map(([key, item]) => `${key}: ${describe(item)}`).join(' · ') : String(value)

type ReadingEntry = { id: string; paper?: IdeaPaper; source?: IdeaSource; supplied: boolean }
type ReadingGroup = { key: string; entries: ReadingEntry[]; evidence: IdeaEvidence[] }

function readingGroups(session: IdeaSession): ReadingGroup[] {
  const sources = new Map([...(session.brief.sources ?? []), ...(session.research?.sources ?? [])].map(source => [source.id, source]))
  const suppliedIds = new Set(session.brief.sources.map(source => source.id))
  const entries = new Map<string, ReadingEntry>()
  for (const paper of session.research?.papers ?? []) entries.set(paper.id, { id: paper.id, paper, source: sources.get(paper.id), supplied: suppliedIds.has(paper.id) })
  for (const source of sources.values()) if (!entries.has(source.id)) entries.set(source.id, { id: source.id, source, supplied: suppliedIds.has(source.id) })
  const evidence = session.result?.literature?.evidence ?? []
  for (const item of evidence) if (!entries.has(item.source_id)) entries.set(item.source_id, { id: item.source_id, supplied: false })
  const groups = new Map<string, ReadingGroup>()
  for (const entry of entries.values()) {
    // Only a recorded parsed-source version can join packets. A shared URL or
    // title does not establish that their text, coverage, or findings match.
    const version = entry.source?.provenance?.source_version_id ?? (entry.paper && 'source_version_id' in entry.paper ? entry.paper.source_version_id : undefined)
    const key = typeof version === 'string' && version ? `version:${version}` : `source:${entry.id}`
    if (!groups.has(key)) groups.set(key, { key, entries: [], evidence: [] })
    groups.get(key)!.entries.push(entry)
    groups.get(key)!.evidence.push(...evidence.filter(item => item.source_id === entry.id))
  }
  return [...groups.values()]
}

function readingTitle(entries: ReadingEntry[]): string {
  const titles = entries.flatMap(entry => [entry.paper?.title, entry.source?.title])
  const title = titles.find(value => value && value.trim().length <= 180 && !/^https?:\/\//i.test(value.trim()) && !/[\r\n]/.test(value))
  if (title) return title
  const uri = entries.map(entry => entry.paper?.url ?? entry.source?.uri).find(Boolean)
  try {
    const url = new URL(uri!)
    if (['https:', 'http:'].includes(url.protocol)) return `${url.hostname}${url.pathname !== '/' ? ` · ${decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() ?? '').slice(0, 60)}` : ''}`
  } catch { /* A supplied source may only have a label. */ }
  return t('ideaLab.savedSource')
}

function readingStatus(entry: ReadingEntry): string {
  if (entry.paper?.status === 'read') {
    const access = entry.paper.access
    return t(`ideaLab.reading.${['full_text', 'abstract', 'partial'].includes(access) ? access : 'text'}`)
  }
  if (entry.paper?.error || entry.paper?.status === 'unavailable' || entry.paper?.access === 'unavailable') return t('ideaLab.reading.unavailable')
  if (entry.paper) return t('ideaLab.reading.pending')
  if (entry.supplied) return t('ideaLab.reading.supplied')
  return t(entry.source ? 'ideaLab.reading.saved' : 'ideaLab.reading.missing')
}

function Findings({ evidence }: { evidence: IdeaEvidence[] }) {
  return <ul className="idea-lab-findings">{evidence.map(item => <li key={item.id}><button className="idea-lab-citation" onClick={() => revealEvidence(item.id)} aria-label={t('ideaLab.inspectEvidence', { id: item.id })}>{item.id}</button> <span>{item.finding}</span></li>)}</ul>
}

function literatureReuse(session: IdeaSession): IdeaLiteratureCache | null {
  if (!session.generation_id) return null
  const latest = session.usage.filter(item => item.stage === 'literature' && item.generation_id === session.generation_id).at(-1)
  const provider = latest?.provider
  if (!provider || typeof provider !== 'object') return null
  const value = (provider as Record<string, unknown>).literature_cache as IdeaLiteratureCache | undefined
  if (!value || value.version !== 1 || !['miss', 'hit', 'join', 'bypass'].includes(value.status) || typeof value.waited !== 'boolean') return null
  if (value.avoided_model_jobs !== (['miss', 'bypass'].includes(value.status) ? 0 : 1) || (value.status === 'join' && !value.waited)) return null
  return value
}

export function ResearchWorkspace({ session, scope, onError }: { session: IdeaSession; scope: WorkspaceProfileScope; onError: (message: string) => void }) {
  const research = session.research
  const [earlier, setEarlier] = useState<IdeaActivity[]>([])
  const [before, setBefore] = useState<number | null | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const loadActivities = async () => {
    if (loading || !window.agentsDock.ideaLab) return
    setLoading(true)
    try {
      const page = await window.agentsDock.ideaLab.activities(scope, session.id, before ?? undefined)
      if (!alive.current) return
      setEarlier(previous => [...previous, ...page.items]); setBefore(page.next_before)
    } catch (error) { if (alive.current) onError(String(error)) }
    finally { if (alive.current) setLoading(false) }
  }
  const allActivities = [...new Map([...earlier, ...(research?.activities ?? [])].map(event => [event.id, event])).values()].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  const readiness = research?.readiness ?? 'legacy_unsearched'
  const stopReason = research?.stop_reason
  const localizedStop = stopReason ? t(`ideaLab.stopReason.${stopReason}`) : ''
  const stopExplanation = stopReason && localizedStop !== `ideaLab.stopReason.${stopReason}` ? localizedStop : t(`ideaLab.readinessNote.${readiness}`)
  const groups = readingGroups(session)
  const reuse = literatureReuse(session)
  const gaps = [...new Set([...(research?.coverage_gaps ?? []), ...(session.result?.literature?.gaps ?? [])].filter(Boolean))]
  return <>
    <section className={`idea-lab-section idea-lab-readiness ${readiness}`} aria-label={t('ideaLab.researchState')}>
      <h2>{t(`ideaLab.readiness.${readiness}`)}</h2>
      <p>{stopExplanation}</p>
      <p className="idea-lab-note">{t('ideaLab.readinessLimit')}</p>
    </section>
    {research && <section className="idea-lab-section idea-lab-member-section" aria-label={t('ideaLab.members')}><h2>{t('ideaLab.members')}</h2>
        <div className="idea-lab-members">{research.agents.map(agent => <article key={agent.id}>
          <h3>{agent.id === 'literature' ? <BookOpen size={16} /> : <Search size={16} />}{agent.name}</h3>
          <span className="idea-lab-badge">{t(`ideaLab.memberStatus.${agent.status}`) === `ideaLab.memberStatus.${agent.status}` ? t('ideaLab.memberStatus.unknown') : t(`ideaLab.memberStatus.${agent.status}`)}</span>
        </article>)}</div>
      </section>}
    <section className="idea-lab-section idea-lab-reading" aria-label={t('ideaLab.readingLibrary')}><h2>{t('ideaLab.readingLibrary')}</h2>
      <p className="idea-lab-note">{t('ideaLab.readingNotice')}</p>
      {reuse && <p className="idea-lab-note">{t(`ideaLab.literatureReuse.${reuse.status}`)} {t('ideaLab.literatureReuse.limit')}</p>}
      {!groups.length && <p>{t(research?.searches.length ? 'ideaLab.searchWithoutReading' : 'ideaLab.noRetrievedPapers')}</p>}
      {groups.map(group => <article className="idea-lab-reading-card" key={group.key}>
        <h3>{readingTitle(group.entries)}</h3>
        <p className="idea-lab-reading-status">{[...new Set(group.entries.map(readingStatus))].join(' · ')}</p>
        {group.evidence.length ? <><Findings evidence={group.evidence.slice(0, 2)} />{group.evidence.length > 2 && <details className="idea-lab-reading-more"><summary>{t('ideaLab.moreFindings', { count: group.evidence.length - 2 })}</summary><Findings evidence={group.evidence.slice(2)} /></details>}</> : <p className="idea-lab-note">{t('ideaLab.noExtractedInformation')}</p>}
        <details className="idea-lab-reading-details"><summary>{t('ideaLab.sourceDetails')}</summary>
          {group.entries.length > 1 && <p className="idea-lab-note">{t('ideaLab.sameSourcePackets', { count: group.entries.length })}</p>}
          {group.entries.map(entry => <div className="idea-lab-source-packet" key={entry.id} id={`idea-source-${entry.id}`}>
            <p>{readingStatus(entry)}</p>
            {(entry.paper?.url || entry.source?.uri) && <SourceLink url={entry.paper?.url ?? entry.source!.uri} onError={onError} />}
            {entry.paper?.error && <p className="idea-lab-note">{entry.paper.error}</p>}
            {entry.source ? <SourceText source={entry.source} /> : <p className="idea-lab-note">{t('ideaLab.noSourceText')}</p>}
            <details><summary>{t('ideaLab.provenance')}</summary>
              <p>{entry.paper?.title ?? entry.source?.title ?? entry.id}</p>
              {entry.paper && <><p>{describe(entry.paper.coverage)}</p><p className="idea-lab-note">{entry.paper.retrieved_at} · {entry.paper.cache_hit ? t('ideaLab.reused') : t('ideaLab.freshRead')}</p><dl className="idea-lab-provenance"><dt>{t('ideaLab.sourceVersion')}</dt><dd>{entry.paper.content_hash || t('ideaLab.notAvailable')}</dd><dt>{t('ideaLab.parser')}</dt><dd>{entry.paper.parser_version || t('ideaLab.notAvailable')}</dd></dl></>}
            </details>
          </div>)}
        </details>
      </article>)}
      {gaps.length > 0 && <details className="idea-lab-coverage-gaps"><summary>{t('ideaLab.coverageGaps')}</summary><ul>{gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></details>}
    </section>
    {research && <details className="idea-lab-section idea-lab-technical-log"><summary>{t('ideaLab.technicalLog')}</summary>
      <p className="idea-lab-note">{t('ideaLab.reviewContext')}</p>
      {reuse && <details><summary>{t('ideaLab.literatureReuse.details')}</summary><p>{t('ideaLab.literatureReuse.scope')}</p><pre className="idea-lab-source-text">{JSON.stringify(reuse, null, 2)}</pre></details>}
      <div className="idea-lab-budget" aria-label={t('ideaLab.budget')}>
        <span>{t('ideaLab.round')} {research.round} / {research.max_rounds}</span>
        <span>{t('ideaLab.modelCalls')} {research.used.model_calls} / {research.limits.max_model_calls}</span>
        <span>{t('ideaLab.searchCount')} {research.used.searches} / {research.limits.max_search_queries}</span>
        <span>{t('ideaLab.readCount')} {research.used.reads} / {research.limits.max_sources}</span>
        <span>{t('ideaLab.cacheHits')} {research.used.cache_hits}</span>
      </div>
      {stopReason && <p className="idea-lab-note">{stopReason}</p>}
      {research.agents.map(agent => <details key={agent.id}><summary>{agent.name} · {t('ideaLab.jobReceipt')}</summary><p>{agent.role}</p><p>{agent.task}</p><p className="idea-lab-note">{agent.status} · {agent.thread_id} · {agent.turn_id}</p></details>)}
      <ActivityLog activities={allActivities} onError={onError} />
      {before !== null && <button className="quiet-button" disabled={loading} onClick={() => void loadActivities()}>{t(earlier.length ? 'ideaLab.olderActivities' : 'ideaLab.fullActivities')}</button>}
        <details><summary>{t('ideaLab.searchHistory')} ({research.searches.length})</summary>
          {research.searches.length === 0 && <p>{t('ideaLab.noSearches')}</p>}
          {research.searches.map((search, index) => <div className="idea-lab-activity" key={index}><strong>{describe(search.query ?? search.queries ?? search.summary ?? search.type)}</strong>
            {typeof search.url === 'string' && <SourceLink url={String(search.url)} onError={onError} />}<p className="idea-lab-note">{describe(search.at ?? search.searched_at ?? search.timestamp)} {describe(search.status ?? search.error)}</p>
          </div>)}
        </details>
      {research.rounds.length > 0 && <details><summary>{t('ideaLab.roundHistory')}</summary>{research.rounds.map((round, index) => <details key={index} className="idea-lab-source"><summary>{t('ideaLab.round')} {describe(round.round ?? index + 1)} · {describe(round.disposition)}</summary>
        <p>{describe((round.literature as Record<string, unknown> | undefined)?.summary)}</p>
        <p>{describe(round.coverage_gaps)}</p>
        <p>{describe((round.review as Record<string, unknown> | undefined)?.comparison_summary)}</p>
        <p>{describe((round.review as Record<string, unknown> | undefined)?.reason)}</p>
      </details>)}</details>}
    </details>}
  </>
}
export function SourceText({ source }: { source: IdeaSource }) {
  return <details><summary>{t('ideaLab.readSource')}</summary><pre className="idea-lab-source-text">{source.text}</pre></details>
}
function ActivityLog({ activities, onError }: { activities: IdeaActivity[]; onError: (message: string) => void }) {
  return <details className="idea-lab-activity-log"><summary>{t('ideaLab.activity')} ({activities.length})</summary>
    {!activities.length && <p>{t('ideaLab.noActivities')}</p>}
    <ol>{activities.map((event, index) => <li key={event.id ?? index}>
      <small>{event.at ?? event.time} · {event.agent === 'literature' ? t('ideaLab.literatureAgent') : t('ideaLab.ideaAgent')}{event.round ? ` · ${t('ideaLab.round')} ${event.round}` : ''}</small>
      <p>{event.summary}</p>{event.query && <blockquote>{event.query}</blockquote>}{event.url && <SourceLink url={event.url} onError={onError} />}
      {(event.thread_id || event.turn_id) && <details><summary>{t('ideaLab.jobReceipt')}</summary><p className="idea-lab-note">{event.thread_id}<br />{event.turn_id}</p></details>}
    </li>)}</ol>
  </details>
}


export function HistoricalEvidence({ previous, scope }: { previous: IdeaSession; scope: WorkspaceProfileScope }) {
  const literature = previous.result?.literature
  if (!literature) return null
  return <div className="idea-lab-historical-evidence"><h3>{t('ideaLab.evidence')}</h3><p>{literature.summary}</p>
    {literature.gaps.length > 0 && <ul>{literature.gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul>}
    {literature.evidence.map(evidence => <details className="idea-lab-source" key={evidence.id}><summary>{t('ideaLab.historicalEvidence')} {evidence.id} · {previous.research?.papers.find(paper => paper.id === evidence.source_id)?.title ?? evidence.source_id}</summary>
      <blockquote>{evidence.quote}</blockquote><p>{evidence.finding}</p><p className="idea-lab-note">{evidence.limitation}</p>
      {evidence.conditions && <p><strong>{t('ideaLab.conditions')}: </strong>{evidence.conditions}</p>}
      {!!evidence.assumptions?.length && <><h3>{t('ideaLab.assumptions')}</h3><ul>{evidence.assumptions.map((value, index) => <li key={index}>{value}</li>)}</ul></>}
      {evidence.interpretation && <p><strong>{t('ideaLab.interpretation')}: </strong>{evidence.interpretation}</p>}
      {evidence.span && <p className="idea-lab-note">{t('ideaLab.sourceLocation')}: {evidence.span.start}–{evidence.span.end}</p>}
      <p className="idea-lab-note">{t('ideaLab.sourceLinked')} · {t('ideaLab.inferenceNotEstablished')}</p>
      <FrozenEvidenceSource key={`${JSON.stringify(scope)}:${previous.id}:${previous.generation_id ?? ''}:${evidence.source_id}:${evidence.source_hash ?? ''}`} scope={scope} sessionId={previous.id} generationId={previous.generation_id ?? undefined} evidence={evidence} />
    </details>)}
  </div>
}

export function FrozenEvidenceSource({ scope, sessionId, generationId, evidence }: { scope: WorkspaceProfileScope; sessionId: string; generationId?: string; evidence: IdeaEvidence }) {
  const [savedRecord, setSaved] = useState<{ owner: string; result: IdeaPaperResult } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveNotice, setSaveNotice] = useState('')
  const alive = useRef(true)
  const owner = JSON.stringify([scope, sessionId, generationId, evidence.source_id, evidence.source_hash])
  const saved = savedRecord?.owner === owner ? savedRecord.result : null
  const currentOwner = useRef(owner)
  currentOwner.current = owner
  useEffect(() => {
    alive.current = true; setSaved(null); setLoading(false); setSaving(false); setError(''); setSaveNotice('')
    return () => { alive.current = false }
  }, [owner])
  const coverage = saved && 'coverage' in saved.source ? saved.source.coverage : evidence.coverage
  const coverageInfo = coverage && typeof coverage === 'object' && !Array.isArray(coverage) ? coverage as Record<string, unknown> : undefined
  const coverageKind = coverageInfo?.model_packet_truncated ? 'partial' : coverageInfo?.kind
  const coverageLabel = t(`ideaLab.reading.${typeof coverageKind === 'string' && ['partial', 'full_text', 'abstract'].includes(coverageKind) ? coverageKind : 'text'}`)
  const load = async () => {
    if (loading || !window.agentsDock.ideaLab) return
    setLoading(true); setError('')
    try {
      const result = await window.agentsDock.ideaLab.paper(scope, sessionId, evidence.source_id, evidence.source_hash, generationId)
      if (!alive.current || currentOwner.current !== owner) return
      if (generationId !== undefined && result.generation_id !== generationId) throw new Error(t('ideaLab.quoteMismatch'))
      if (!result.source.text.includes(evidence.quote)) throw new Error(t('ideaLab.quoteMismatch'))
      setSaved({ owner, result })
    } catch (problem) { if (alive.current && currentOwner.current === owner) setError(problem instanceof Error ? problem.message : String(problem)) }
    finally { if (alive.current && currentOwner.current === owner) setLoading(false) }
  }
  const packetHash = saved?.packet_hash
  const descriptor = saved && typeof saved.generation_id === 'string' && /^[a-f0-9]{32}$/.test(saved.generation_id)
    && typeof saved.provenance_hash === 'string' && /^[a-f0-9]{64}$/.test(saved.provenance_hash)
    ? { generationId: saved.generation_id, provenanceHash: saved.provenance_hash } : null
  const saveOriginal = async () => {
    if (saving || !saved || !packetHash || !descriptor || !window.agentsDock.ideaLab?.saveOriginal) return
    setSaving(true); setSaveNotice(''); setError('')
    try {
      const result = await window.agentsDock.ideaLab.saveOriginal(scope, sessionId, evidence.source_id, packetHash, descriptor.generationId, descriptor.provenanceHash)
      if (!alive.current || currentOwner.current !== owner || !result) return
      if (result.status === 'saved' && (result.session_id !== sessionId || result.source_id !== evidence.source_id || result.source_hash !== packetHash || result.generation_id !== descriptor.generationId || result.provenance_hash !== descriptor.provenanceHash)) throw new Error('IDEA_ORIGINAL_INVALID')
      if (result.status === 'saved') setSaveNotice(`${t('ideaLab.original.saved')} ${result.path}`)
      else if (result.status === 'corrupt' || result.status === 'missing') setError(t(`ideaLab.original.${result.status}`))
      else setSaveNotice(t('ideaLab.original.not_retained'))
    } catch (problem) {
      if (alive.current && currentOwner.current === owner) setError(t(String(problem).includes('IDEA_ORIGINAL_INVALID') ? 'ideaLab.original.invalid' : 'ideaLab.original.failed'))
    } finally { if (alive.current && currentOwner.current === owner) setSaving(false) }
  }
  return <div className="idea-lab-frozen-source">
    {!saved && <button className="quiet-button" disabled={loading} onClick={() => void load()}>{t(loading ? 'ideaLab.loadingFrozenSource' : 'ideaLab.loadFrozenSource')}</button>}
    {error && <p role="alert" className="idea-lab-error">{error}</p>}
    {saved && <><h3>{saved.source.title}</h3><SourceLink url={saved.source.uri} onError={setError} />
      <p className="idea-lab-note">{saved.archived ? t('ideaLab.archivedSource') : t('ideaLab.frozenSource')}{saved.revision !== undefined ? ` · ${t('ideaLab.revision')} ${saved.revision}` : ''}</p>
      <p>{t('ideaLab.frozenCoverage')}: {coverageLabel}</p>
      <pre className="idea-lab-source-text">{saved.source.text}</pre>
      <details><summary>{t('ideaLab.sourceVersion')}</summary>{!!coverage && <p>{describe(coverage)}</p>}{saved.packet_hash && <p className="idea-lab-note">{saved.packet_hash}</p>}{saved.paper && <p>{t('ideaLab.originalReadCoverage')}: {t(`ideaLab.access.${saved.paper.access}`)} · {describe(saved.paper.coverage)}</p>}
        <p className="idea-lab-note">{t('ideaLab.original.scope')}</p>
        {packetHash && descriptor ? <button className="quiet-button" disabled={saving} onClick={() => void saveOriginal()}>{t(saving ? 'ideaLab.original.saving' : 'ideaLab.original.save')}</button> : <p>{t('ideaLab.original.noVersion')}</p>}
        {saveNotice && <p role="status" className="idea-lab-note">{saveNotice}</p>}
      </details>
    </>}
  </div>
}
