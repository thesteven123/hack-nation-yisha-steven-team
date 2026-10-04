import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowLeft, Check, FlaskConical, LoaderCircle, Plus, RefreshCw, Square, X } from 'lucide-react'
import { t } from '@shared/i18n'
import type { IdeaBrief, IdeaDecisionInput, IdeaDirection, IdeaSession, IdeaSource, IdeaFollowupInput } from '@shared/idea-lab'
import type { WorkspaceProfileScope } from '@shared/types'
import { useLocale } from '../lib/i18n'
import { ResearchWorkspace, revealEvidence, SourceLink, HistoricalEvidence, FrozenEvidenceSource } from './IdeaResearch'
import './idea-lab.css'

const blank = (): IdeaBrief => ({ goal: '', hypothesis: '', constraints: '', sources: [] })
const newId = () => crypto.randomUUID()
const readDraft = (key: string): IdeaBrief => {
  try { const value = JSON.parse(localStorage.getItem(key) || 'null'); if (value && typeof value.goal === 'string' && typeof value.hypothesis === 'string' && typeof value.constraints === 'string' && Array.isArray(value.sources)) return value } catch { /* Keep the editor usable when local storage is unavailable. */ }
  return blank()
}
const failure = (error: unknown) => error instanceof Error ? error.message : String(error)
const DEMO: IdeaBrief = {
  goal: 'Which small experiment would help us understand whether showing evidence excerpts improves review accuracy?',
  hypothesis: 'Short source excerpts may help reviewers identify unsupported claims, but could also hide missing context.',
  constraints: 'A one-day pilot with six synthetic claim/source pairs. No human-subject recruitment or external data collection.',
  sources: [{ id: 'pilot-note', title: 'Synthetic pilot note (not a published study)', uri: 'fixture://idea-lab/pilot-note',
    text: 'This is a fictional practice dataset, not research evidence. In six made-up claim/source pairs, two claims omit a boundary condition and one reverses causality. The source excerpts vary from one to four sentences. No measured reviewer outcomes are available. Full papers and expert ground truth are missing.' }]
}

export function IdeaLab({ scope, onClose }: { scope: WorkspaceProfileScope | null; onClose: () => void }) {
  useLocale()
  const [briefExpanded, setBriefExpanded] = useState(false)
  const [brief, setBrief] = useState<IdeaBrief>(blank)
  const [items, setItems] = useState<IdeaSession[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [session, setSession] = useState<IdeaSession | null>(null)
  const [past, setPast] = useState<IdeaSession[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [feedback, setFeedback] = useState('')
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [nextGoal, setNextGoal] = useState('')
  const alive = useRef(true)
  const contentRef = useRef<HTMLDivElement>(null)
  const viewEpoch = useRef(0)
  const listEpoch = useRef(0)
  const owned = useRef(new Map<string, string>())
  const createKey = useRef<string | null>(null)
  const followupKeys = useRef(new Map<string, string>())
  const generationKeys = useRef(new Map<string, string>())
  const current = useRef(session)
  current.current = session
  const api = window.agentsDock.ideaLab
  const scopeKey = scope ? JSON.stringify(scope) : ''
  const draftKey = `agentsdock-idea-draft:${scope?.profileId ?? ''}:${scope?.serverIdentity ?? ''}`
  const humanContext = JSON.stringify([session?.generation_id, session?.decision_card?.options_version, session?.research?.questions ?? session?.result?.review?.questions ?? [], session?.result?.ideas?.directions.map(direction => [direction.id, direction.question]) ?? []])
  const humanKey = `${draftKey}:human:${session?.id ?? 'new'}:${humanContext}`
  const scopeRef = useRef(scopeKey)
  scopeRef.current = scopeKey
  const running = session?.status === 'running'
  const ownsRun = !!session?.generation_id && owned.current.get(session.id) === session.generation_id
  const formLocked = busy || session !== null
  const isCurrent = (epoch: number, key: string) => alive.current && epoch === viewEpoch.current && key === scopeRef.current

  useEffect(() => { setBriefExpanded(false) }, [session?.id])
  useLayoutEffect(() => { if (contentRef.current) contentRef.current.scrollTop = 0 }, [session?.id])
  useEffect(() => { alive.current = true; return () => { alive.current = false; viewEpoch.current += 1 } }, [])
  const refreshList = useCallback(async () => {
    if (!api || !scope) return
    const epoch = ++listEpoch.current
    try {
      const page = await api.list(scope)
      if (!alive.current || scopeKey !== scopeRef.current || epoch !== listEpoch.current) return
      setItems(page.items); setHasMore(page.has_more)
    } catch (problem) { if (alive.current && scopeKey === scopeRef.current) setError(failure(problem)) }
  }, [api, scopeKey])
  useEffect(() => {
    viewEpoch.current += 1; listEpoch.current += 1
    owned.current.clear(); setSession(null); setBrief(readDraft(draftKey)); setPast([]); setItems([]); setFeedback(''); setAnswers({}); setSelectedIds([]); setNextGoal(''); setError(''); setBusy(false)
    void refreshList()
  }, [scopeKey, refreshList])

  useEffect(() => {
    try { const value = JSON.parse(localStorage.getItem(humanKey) || '{}'); setFeedback(value.feedback || ''); setAnswers(value.answers || {}); setSelectedIds(value.selectedIds || []); setNextGoal(value.nextGoal || '') } catch { setFeedback(''); setAnswers({}); setSelectedIds([]); setNextGoal('') }
  }, [humanKey])
  const saveHuman = (patch: Record<string, unknown>) => {
    const value = { feedback, answers, selectedIds, nextGoal, ...patch }
    try { localStorage.setItem(humanKey, JSON.stringify(value)) } catch { /* A storage failure must not block a decision. */ }
  }
  const clearHuman = () => { setFeedback(''); setAnswers({}); setSelectedIds([]); setNextGoal(''); try { localStorage.removeItem(humanKey) } catch { /* optional draft storage */ } }
  const accept = (value: IdeaSession) => {
    setSession(value); setBrief(value.brief)
    setItems(previous => [value, ...previous.filter(item => item.id !== value.id)].slice(0, 50))
  }

  // Poll only a generation explicitly started or reopened by the user, and
  // only while that exact server/session/generation is selected. No idle poll.
  useEffect(() => {
    if (!api || !scope || !session || !running || !ownsRun) return
    const epoch = viewEpoch.current, key = scopeKey, id = session.id, generation = session.generation_id
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await api.get(scope, id)
        if (disposed || !isCurrent(epoch, key) || current.current?.id !== id || next.generation_id !== generation) return
        if (next.revision >= (current.current?.revision ?? 0)) accept(next)
        if (next.status === 'running') timer = setTimeout(() => void poll(), 1200)
      } catch (problem) {
        if (!disposed && isCurrent(epoch, key)) setError(failure(problem))
        // A failed read requires an explicit Refresh; do not retry indefinitely.
      }
    }
    timer = setTimeout(() => void poll(), 800)
    return () => { disposed = true; clearTimeout(timer) }
  }, [scopeKey, session?.id, session?.generation_id, running, ownsRun, api, viewEpoch.current])

  const newBrief = () => {
    viewEpoch.current += 1; setSession(null); setBrief(readDraft(draftKey)); setFeedback(''); setPast([]); setHistoryOpen(false); setError('')
    createKey.current = null
  }
  const editBrief = (next: IdeaBrief) => { setBrief(next); createKey.current = null; try { localStorage.setItem(draftKey, JSON.stringify(next)) } catch { /* optional draft storage */ } }
  const editSource = (index: number, patch: Partial<IdeaSource>) => editBrief({ ...brief, sources: brief.sources.map((source, i) => i === index ? { ...source, ...patch } : source) })
  const load = async (id: string) => {
    if (!api || !scope || busy) return
    const epoch = ++viewEpoch.current, key = scopeKey
    setBusy(true); setError(''); setHistoryOpen(false); setPast([])
    try {
      const next = await api.get(scope, id)
      if (isCurrent(epoch, key)) {
        if (next.status === 'running' && next.generation_id) owned.current.set(next.id, next.generation_id)
        accept(next)
      }
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const generate = async () => {
    if (!api || !scope || busy || running || !brief.goal.trim()) return
    const epoch = viewEpoch.current, key = scopeKey
    setBusy(true); setError('')
    try {
      let draft = session
      if (!draft) {
        const bounded = (value: string, label: string, limit: number) => {
          if (new TextEncoder().encode(value).byteLength > limit) throw new Error(t('ideaLab.inputLimit', { field: label, limit }))
        }
        bounded(brief.goal, t('ideaLab.goal'), 8000)
        bounded(brief.hypothesis, t('ideaLab.hypothesis'), 8000)
        bounded(brief.constraints, t('ideaLab.constraints'), 8000)
        bounded(brief.success_criteria ?? '', t('ideaLab.successCriteria'), 8000)
        bounded(brief.preferences ?? '', t('ideaLab.preferences'), 8000)
        brief.sources.forEach((source, index) => {
          bounded(source.text, `${t('ideaLab.source')} ${index + 1}`, 30000)
          bounded(source.title, t('ideaLab.sourceTitle'), 1000)
          bounded(source.uri, t('ideaLab.sourceUri'), 4096)
        })
        createKey.current ??= newId()
        const submitted = { ...brief, sources: brief.sources.map((source, index) => ({ ...source, uri: source.uri.trim() || `supplied:source-${index + 1}` })) }
        draft = await api.create(scope, { idempotency_key: createKey.current, brief: submitted })
        if (!isCurrent(epoch, key)) return
        accept(draft)
        try { localStorage.removeItem(draftKey) } catch { /* Persisted server brief is authoritative. */ }
      }
      const requestId = `${draft.id}:${draft.revision}`
      if (!generationKeys.current.has(requestId)) generationKeys.current.set(requestId, newId())
      const next = await api.generate(scope, draft.id, { expected_revision: draft.revision, idempotency_key: generationKeys.current.get(requestId)! })
      if (!isCurrent(epoch, key)) return
      if (next.generation_id) owned.current.set(next.id, next.generation_id)
      accept(next)
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const stop = async () => {
    if (!api || !scope || !session || !ownsRun || !running || busy) return
    const epoch = viewEpoch.current, key = scopeKey, id = session.id, generation = session.generation_id
    setBusy(true); setError('')
    try {
      const latest = await api.get(scope, id)
      if (!isCurrent(epoch, key) || latest.generation_id !== generation || latest.status !== 'running') {
        if (isCurrent(epoch, key)) accept(latest)
        return
      }
      const next = await api.cancel(scope, id, { expected_revision: latest.revision })
      if (isCurrent(epoch, key)) accept(next)
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const decide = async (kind: IdeaDecisionInput['kind'], selectedId: string | null = null) => {
    if (!api || !scope || !session || busy || running || kind === 'revise' && !feedback.trim()) return
    const epoch = viewEpoch.current, key = scopeKey
    setBusy(true); setError('')
    try {
      if (new TextEncoder().encode(feedback).byteLength > 8000) throw new Error(t('ideaLab.inputLimit', { field: t('ideaLab.feedback'), limit: 8000 }))
      const submittedAnswers = (session.research?.questions ?? []).filter(question => answers[question.id]?.trim()).map(question => ({ question_id: question.id, answer: answers[question.id] }))
      const next = await api.decision(scope, session.id, { expected_revision: session.revision, kind, selected_id: selectedId, feedback, ...(submittedAnswers.length ? { answers: submittedAnswers } : {}), ...(kind === 'combine' ? { selected_ids: selectedIds, goal: nextGoal } : kind === 'reject' && nextGoal.trim() ? { goal: nextGoal } : {}) })
      if (isCurrent(epoch, key)) { accept(next); setHistoryOpen(false); setPast([]) }
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const continueResearch = async (mode: IdeaFollowupInput['mode']) => {
    if (!api || !scope || !session || busy || running) return
    const epoch = viewEpoch.current, key = scopeKey
    setBusy(true); setError('')
    try {
      const questions = session.research?.questions ?? session.result?.review?.questions ?? []
      if (questions.some(question => question.required && !answers[question.id]?.trim())) throw new Error(t('ideaLab.answerRequired'))
      const input = { expected_revision: session.revision, feedback, mode,
        ...(nextGoal.trim() ? { goal: nextGoal } : {}),
        ...(selectedIds.length ? { selected_ids: selectedIds } : session.decision?.selected_ids?.length ? { selected_ids: session.decision.selected_ids } : session.decision?.selected_id ? { selected_ids: [session.decision.selected_id] } : {}),
        answers: questions.filter(question => answers[question.id]?.trim()).map(question => ({ question_id: question.id, answer: answers[question.id] })) }
      for (const value of [feedback, nextGoal, ...Object.values(answers)]) if (new TextEncoder().encode(value).byteLength > 8000) throw new Error(t('ideaLab.inputLimit', { field: t('ideaLab.feedback'), limit: 8000 }))
      const request = JSON.stringify({ id: session.id, ...input })
      if (!followupKeys.current.has(request)) followupKeys.current.set(request, newId())
      const draft = await api.followup(scope, session.id, { ...input, idempotency_key: followupKeys.current.get(request)! })
      if (!isCurrent(epoch, key)) return
      accept(draft)
      const dispatch = `${draft.id}:${draft.revision}`
      if (!generationKeys.current.has(dispatch)) generationKeys.current.set(dispatch, newId())
      const next = await api.generate(scope, draft.id, { expected_revision: draft.revision, idempotency_key: generationKeys.current.get(dispatch)! })
      if (!isCurrent(epoch, key)) return
      if (next.generation_id) owned.current.set(next.id, next.generation_id)
      accept(next); clearHuman()
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const history = async () => {
    if (!api || !scope || !session || busy) return
    if (historyOpen) { setHistoryOpen(false); return }
    const epoch = viewEpoch.current, key = scopeKey
    setBusy(true)
    try {
      const page = await api.history(scope, session.id)
      if (isCurrent(epoch, key)) { setPast(page.items); setHistoryOpen(true) }
    } catch (problem) { if (isCurrent(epoch, key)) setError(failure(problem)) }
    finally { if (isCurrent(epoch, key)) setBusy(false) }
  }
  const result = session?.result
  const sourceMap = new Map([...brief.sources, ...(session?.research?.sources ?? [])].map(source => [source.id, source]))
  const questions = session?.research?.questions ?? result?.review?.questions ?? []
  const missingAnswer = questions.some(question => question.required && !answers[question.id]?.trim())
  const selectedDirection = result?.ideas?.directions.find(direction => direction.id === session?.decision?.selected_id)
  const staleChoices = !!session?.decision_card && session.decision_card.brief_revision !== session.brief_revision
  const directions = result?.ideas?.directions ?? []

  return <section className="idea-lab" aria-label={t('ideaLab.name')}>
    <header className="idea-lab-header">
      <div><h1><FlaskConical size={21} />{t('ideaLab.name')}</h1><p>{t('ideaLab.intro')}</p></div>
      <div className="idea-lab-header-actions">{running && ownsRun && <button className="quiet-button" disabled={busy} onClick={() => void stop()}><Square size={13} />{t('ideaLab.stop')}</button>}<button className="quiet-button" onClick={onClose}><ArrowLeft size={15} />{t('ideaLab.back')}</button></div>
    </header>
    {!scope || !api ? <p className="idea-lab-empty">{t('ideaLab.connectFirst')}</p> : <div className="idea-lab-layout">
      <aside className="idea-lab-history" aria-label={t('ideaLab.recent')}>
        <div className="idea-lab-actions"><button className="quiet-button" disabled={busy} onClick={newBrief}><Plus size={14} />{t('ideaLab.new')}</button>
          <button className="icon-button" aria-label={t('ideaLab.refreshHistory')} disabled={busy} onClick={() => void refreshList()}><RefreshCw size={14} /></button></div>
        <h2>{t('ideaLab.recent')}</h2>
        {!items.length && <p>{t('ideaLab.noHistory')}</p>}
        {items.map(item => <button key={item.id} className={`idea-lab-history-item ${session?.id === item.id ? 'selected' : ''}`} disabled={busy} onClick={() => void load(item.id)}>
          <span>{item.brief.goal}</span><small>{t(`ideaLab.readiness.${item.research?.readiness ?? 'legacy_unsearched'}`)}</small>
        </button>)}
        {hasMore && <p>{t('ideaLab.recentOnly')}</p>}
      </aside>
      <div ref={contentRef} className="idea-lab-content">
        {error && <div role="alert" className="idea-lab-error">{error}<button className="icon-button" aria-label={t('ideaLab.dismiss')} onClick={() => setError('')}><X size={14} /></button></div>}
        <section className="idea-lab-section">
          <div className="idea-lab-section-heading"><h2>{t('ideaLab.brief')}</h2>{!session && <button className="quiet-button" disabled={busy} onClick={() => editBrief(structuredClone(DEMO))}>{t('ideaLab.demo')}</button>}{session && <button className="quiet-button" aria-expanded={briefExpanded} aria-controls="idea-brief-fields" onClick={() => setBriefExpanded(value => !value)}>{t(briefExpanded ? 'ideaLab.collapseBrief' : 'ideaLab.expandBrief')}</button>}</div>
          {session && <p className="idea-lab-goal-summary" title={brief.goal}>{brief.goal}</p>}
          <div id="idea-brief-fields" hidden={!!session && !briefExpanded}>
          <label>{t('ideaLab.goal')}<textarea aria-label={t('ideaLab.goal')} rows={3} maxLength={8000} value={brief.goal} disabled={formLocked} onChange={event => editBrief({ ...brief, goal: event.target.value })} /></label>
          <div className="idea-lab-two"><label>{t('ideaLab.hypothesis')}<textarea rows={2} maxLength={8000} value={brief.hypothesis} disabled={formLocked} onChange={event => editBrief({ ...brief, hypothesis: event.target.value })} /></label>
            <label>{t('ideaLab.constraints')}<textarea rows={2} maxLength={8000} value={brief.constraints} disabled={formLocked} onChange={event => editBrief({ ...brief, constraints: event.target.value })} /></label></div>
          <div className="idea-lab-two"><label>{t('ideaLab.successCriteria')}<textarea rows={2} value={brief.success_criteria ?? ''} disabled={formLocked} maxLength={8000} onChange={event => editBrief({ ...brief, success_criteria: event.target.value })} /></label><label>{t('ideaLab.preferences')}<textarea rows={2} value={brief.preferences ?? ''} disabled={formLocked} maxLength={8000} onChange={event => editBrief({ ...brief, preferences: event.target.value })} /></label></div>
          <div className="idea-lab-section-heading"><h3>{t('ideaLab.sources')}</h3>{!session && <button className="quiet-button" disabled={busy || brief.sources.length >= 5} onClick={() => editBrief({ ...brief, sources: [...brief.sources, { id: newId(), title: '', uri: '', text: '' }] })}><Plus size={14} />{t('ideaLab.addSource')}</button>}</div>
          <p className="idea-lab-note">{t('ideaLab.sourceScope')}</p>
          {brief.sources.map((source, index) => <details className="idea-lab-source" key={source.id} open={!session}>
            <summary>{source.title || `${t('ideaLab.source')} ${index + 1}`}</summary>
            <div className="idea-lab-two"><label>{t('ideaLab.sourceTitle')}<input value={source.title} maxLength={500} disabled={formLocked} onChange={event => editSource(index, { title: event.target.value })} /></label>
              <label>{t('ideaLab.sourceUri')}<input value={source.uri} maxLength={2000} disabled={formLocked} onChange={event => editSource(index, { uri: event.target.value })} /></label></div>
            <label>{t('ideaLab.sourceText')}<textarea rows={4} maxLength={30000} value={source.text} disabled={formLocked} onChange={event => editSource(index, { text: event.target.value })} /><small className="idea-lab-note">{new TextEncoder().encode(source.text).byteLength.toLocaleString()} / 30,000 UTF-8 bytes</small></label>
            {!session && <button className="quiet-button" disabled={busy} onClick={() => editBrief({ ...brief, sources: brief.sources.filter((_, i) => i !== index) })}>{t('ideaLab.remove')}</button>}
          </details>)}
          {!brief.sources.length && <p className="idea-lab-note">{t('ideaLab.noSources')}</p>}
          </div>
          <div className="idea-lab-actions">
            {(!session || ['draft', 'failed', 'cancelled', 'interrupted'].includes(session.status)) && <button className="primary-button" disabled={busy || !brief.goal.trim() || brief.sources.some(source => !source.title.trim() || !source.text.trim())} onClick={() => void generate()}>{busy ? <LoaderCircle className="spin" size={15} /> : <FlaskConical size={15} />}{t('ideaLab.generate')}</button>}
            {session && <button className="quiet-button" disabled={busy} onClick={() => void load(session.id)}><RefreshCw size={14} />{t('ideaLab.refreshResult')}</button>}
          </div>
          {!running && (!session || session.status !== 'completed') && <p className="idea-lab-note">{t('ideaLab.modelNotice')}</p>}
          {session && <div role="status" className="idea-lab-progress" aria-live="polite">
            {(['searching', 'reading', 'literature', 'ideas', 'review'] as const).map(phase => <span key={phase} className={session.phase === phase && running ? 'active' : ''}>{(phase === 'searching' ? !!session.research?.searches.length : phase === 'reading' ? !!session.research?.sources?.length : result?.[phase]) ? <Check size={14} /> : session.phase === phase && running ? <LoaderCircle className="spin" size={14} /> : null}{t(`ideaLab.phase.${phase}`)}</span>)}
            <strong>{t(`ideaLab.status.${session.status}`)}</strong>
          </div>}
          {session?.error && <p role="alert" className="idea-lab-error">{session.error}</p>}
          {running && !ownsRun && <p className="idea-lab-note">{t('ideaLab.existingRun')}</p>}
        </section>
        {session && <ResearchWorkspace key={`${scopeKey}:${session.id}:${session.generation_id}`} session={session} scope={scope} onError={setError} />}
        {result?.literature && <details className="idea-lab-section" key={`evidence:${scopeKey}:${session?.id}:${session?.generation_id}`}><summary>{t('ideaLab.evidence')}</summary><p>{result.literature.summary}</p>
          <TextList title={t('ideaLab.gaps')} values={result.literature.gaps} />
          {result.literature.evidence.map(evidence => <details key={evidence.id} className="idea-lab-source" id={`idea-evidence-${evidence.id}`} tabIndex={-1}><summary>{evidence.id} · {sourceMap.get(evidence.source_id)?.title ?? evidence.source_id}</summary>
            <blockquote>{evidence.quote}</blockquote><p>{evidence.finding}</p><p className="idea-lab-note">{t('ideaLab.sourceLinked')} · {t('ideaLab.inferenceNotEstablished')}</p>{evidence.location && <p>{evidence.location}</p>}<p className="idea-lab-note">{evidence.limitation}</p>{evidence.conditions && <p><strong>{t('ideaLab.conditions')}: </strong>{evidence.conditions}</p>}<TextList title={t('ideaLab.assumptions')} values={evidence.assumptions ?? []} />{evidence.interpretation && <p><strong>{t('ideaLab.interpretation')}: </strong>{evidence.interpretation}</p>}{evidence.span && <p className="idea-lab-note">{t('ideaLab.sourceLocation')}: {evidence.span.start}–{evidence.span.end}</p>}
            {sourceMap.get(evidence.source_id) && <><SourceLink url={sourceMap.get(evidence.source_id)!.uri} onError={setError} /></>}
            <FrozenEvidenceSource key={`${scopeKey}:${session!.id}:${session!.generation_id ?? ''}:${evidence.source_id}:${evidence.source_hash ?? ''}`} scope={scope} sessionId={session!.id} generationId={session!.generation_id ?? undefined} evidence={evidence} />
          </details>)}
        </details>}
        {directions.length > 0 && <section className="idea-lab-section"><h2>{t('ideaLab.directions')}</h2>
          <p className="idea-lab-note">{t('ideaLab.proposalsNotice')}</p>
          <div className="idea-lab-cards">{directions.map(direction => <DirectionCard key={direction.id} direction={direction} selected={session?.decision?.selected_id === direction.id}
            recommended={result?.review?.recommendation_id === direction.id} disabled={busy || !!running || !result?.review || missingAnswer || staleChoices} onSelect={() => void decide('select', direction.id)} checked={selectedIds.includes(direction.id)} onCheck={() => { const next = selectedIds.includes(direction.id) ? selectedIds.filter(id => id !== direction.id) : [...selectedIds, direction.id]; setSelectedIds(next); saveHuman({ selectedIds: next }) }} />)}</div>
          <p>{result?.ideas?.open_alternative}</p>
        </section>}
        {result?.review && <section className="idea-lab-section"><h2>{t('ideaLab.review')}</h2><p>{result.review.comparison_summary}</p>
          <p><strong>{t('ideaLab.recommendation')}: </strong>{directions.find(direction => direction.id === result.review?.recommendation_id)?.title ?? result.review.recommendation_id} — {result.review.reason}</p>
          <details><summary>{t('ideaLab.reviewDetails')}</summary>{result.review.critiques.map(critique => <div className="idea-lab-critique" key={critique.direction_id}><h3>{directions.find(direction => direction.id === critique.direction_id)?.title ?? critique.direction_id}</h3><TextList values={critique.concerns} /><p><strong>{t('ideaLab.beforeCommit')}: </strong>{critique.test_before_commit}</p></div>)}
          <TextList title={t('ideaLab.gaps')} values={result.review.missing_evidence} />
          <TextList title={t('ideaLab.requestedSearches')} values={result.review.followup_queries ?? []} /></details>
          {result.review.disposition && <p>{t(`ideaLab.disposition.${result.review.disposition}`)}</p>}
        </section>}
        {session && <section className="idea-lab-section"><h2>{t('ideaLab.direction')}</h2>{session.decision_card && <><p>{session.decision_card.choice_needed === 'Choose, combine, reject, refine, or defer these research directions.' ? t('ideaLab.choiceNeeded') : session.decision_card.choice_needed}</p><p><strong>{t('ideaLab.recommendation')}: </strong>{session.decision_card.options.find(option => option.id === session.decision_card?.recommendation_id)?.title ?? ''} · {session.decision_card.reason}</p><details><summary>{t('ideaLab.choiceProvenance')}</summary><p>{t('ideaLab.briefVersion')} {session.decision_card.brief_revision} · {t('ideaLab.optionsVersion')} {session.decision_card.options_version}</p><p className="idea-lab-note">{session.decision_card.evidence_versions.join(' · ')}</p></details></>}
          {session.decision && <div className="idea-lab-decision"><strong>{t(`ideaLab.decision.${session.decision.kind}`)}</strong>{selectedDirection && <p>{selectedDirection.title}</p>}<p>{session.decision.feedback || t('ideaLab.noFeedback')}</p>{session.decision.answers?.map(answer => <p key={answer.question_id}>{answer.answer}</p>)}<small>{session.decision.at}</small>{!running && ['select', 'combine'].includes(session.decision.kind) && <div><button className="quiet-button" disabled={busy} onClick={() => window.dispatchEvent(new CustomEvent('agentsdock:open-research-lab', { detail: { ideaId: session.id } }))}>{t('researchLab.importChoice')}</button><p className="idea-lab-note">{t('researchLab.importNotice')}</p></div>}</div>}
          {questions.length > 0 && <fieldset className="idea-lab-questions"><legend>{t('ideaLab.questions')}</legend>{questions.map(question => <div key={question.id}><label>{question.question} {question.required ? `(${t('ideaLab.required')})` : `(${t('ideaLab.optional')})`}<textarea rows={2} maxLength={8000} disabled={busy || !!running} value={answers[question.id] ?? ''} onChange={event => { const next = { ...answers, [question.id]: event.target.value }; setAnswers(next); saveHuman({ answers: next }) }} /></label><p className="idea-lab-note">{question.why}</p><div className="idea-lab-actions">{question.options.map(option => <button className="quiet-button" key={option} disabled={busy || !!running} onClick={() => { const next = { ...answers, [question.id]: option }; setAnswers(next); saveHuman({ answers: next }) }}>{option}</button>)}</div></div>)}</fieldset>}
          <label>{t('ideaLab.feedback')}<textarea rows={3} value={feedback} disabled={busy || !!running} maxLength={8000} onChange={event => { setFeedback(event.target.value); saveHuman({ feedback: event.target.value }) }} /></label>
          <label>{t('ideaLab.nextGoal')}<textarea rows={2} maxLength={8000} value={nextGoal} disabled={busy || !!running} onChange={event => { setNextGoal(event.target.value); saveHuman({ nextGoal: event.target.value }) }} /></label>
          <p className="idea-lab-note">{t('ideaLab.decisionNotice')}</p>
          <div className="idea-lab-actions"><button className="quiet-button" disabled={busy || !!running || !feedback.trim() || !result?.ideas} onClick={() => void decide('reject')}>{t('ideaLab.reject')}</button>
            <button className="quiet-button" disabled={busy || !!running || selectedIds.length < 2 || !nextGoal.trim() || missingAnswer || staleChoices} onClick={() => void decide('combine')}>{t('ideaLab.combine')}</button>
            <button className="quiet-button" disabled={busy || !!running} onClick={() => void decide('defer')}>{t('ideaLab.defer')}</button>
            <button className="quiet-button" disabled={busy} onClick={() => void history()}>{t('ideaLab.pastDecisions')}</button></div>
          {!running && session.status !== 'draft' && <div className="idea-lab-continuation"><h3>{t('ideaLab.continueTitle')}</h3><p className="idea-lab-note">{t('ideaLab.continueNotice')}</p>{missingAnswer && <p>{t('ideaLab.answerRequired')}</p>}<div className="idea-lab-actions"><button className="primary-button" disabled={busy || missingAnswer} onClick={() => void continueResearch('research')}>{t('ideaLab.continueResearch')}</button><button className="quiet-button" disabled={busy || missingAnswer || !directions.length} onClick={() => void continueResearch('refine')}>{t('ideaLab.refine')}</button><button className="quiet-button" disabled={busy || missingAnswer || !(selectedIds.length || session.decision?.selected_ids?.length || session.decision?.selected_id)} onClick={() => void continueResearch('selected')}>{t('ideaLab.researchSelected')}</button></div></div>}
          {historyOpen && <div className="idea-lab-past">{!past.length && <p>{t('ideaLab.noPast')}</p>}{past.map((previous, index) => <details key={`${previous.revision}:${index}`}><summary>{t('ideaLab.revision')} {previous.revision} · {previous.brief.goal}</summary>
            <p>{previous.decision ? `${t(`ideaLab.decision.${previous.decision.kind}`)} · ${previous.decision.feedback || t('ideaLab.noFeedback')}` : t('ideaLab.noFeedback')}</p>
            {previous.decision?.answers?.map(answer => <p key={answer.question_id}>{answer.answer}</p>)}
            <HistoricalEvidence key={`${scopeKey}:${previous.id}:${previous.revision}`} previous={previous} scope={scope} />
            {previous.result?.ideas?.directions.map(direction => <p key={direction.id}><strong>{direction.title}: </strong>{direction.question}</p>)}
          </details>)}</div>}
        </section>}
      </div>
    </div>}
  </section>
}

function TextList({ title, values }: { title?: string; values: string[] }) {
  if (!values.length) return null
  return <div>{title && <h3>{title}</h3>}<ul>{values.map((value, index) => <li key={index}>{value}</li>)}</ul></div>
}

function DirectionCard({ direction, selected, recommended, disabled, onSelect, checked, onCheck }: { direction: IdeaDirection; selected: boolean; recommended: boolean; disabled: boolean; onSelect: () => void; checked: boolean; onCheck: () => void }) {
  return <article className={`idea-lab-card ${selected ? 'selected' : ''}`}>
    <h3>{direction.title}</h3>{direction.grounding && <p className="idea-lab-note">{t(`ideaLab.grounding.${direction.grounding}`)}</p>}{recommended && <span className="idea-lab-badge">{t('ideaLab.recommended')}</span>}
    <dl>{(['question', 'uncertainty', 'minimal_action'] as const).map(field => <div key={field}><dt>{t(`ideaLab.field.${field}`)}</dt><dd>{direction[field]}</dd></div>)}</dl>
    <p className="idea-lab-note">{t('ideaLab.evidenceRefs')}: {direction.evidence_ids.length ? direction.evidence_ids.map(id => <button className="idea-lab-citation" key={id} onClick={() => revealEvidence(id)}>{id}</button>) : t('ideaLab.noEvidenceRefs')}</p>
    <details><summary>{t('ideaLab.directionTradeoffs')}</summary><dl>{(['nearest_work', 'value', 'expected_learning', 'feasibility', 'cost_risk'] as const).map(field => <div key={field}><dt>{t(`ideaLab.field.${field}`)}</dt><dd>{direction[field]}</dd></div>)}</dl><TextList title={t('ideaLab.counterevidence')} values={direction.counterevidence} /></details>
    <label className="idea-lab-pick"><input type="checkbox" checked={checked} disabled={disabled} onChange={onCheck} />{t('ideaLab.includeDirection')}</label>
    <button className={selected ? 'quiet-button' : 'primary-button'} disabled={disabled || selected} onClick={onSelect}>{selected ? t('ideaLab.selected') : t('ideaLab.choose')}</button>
  </article>
}
