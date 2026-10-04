import { ResearchBranchWorkspace } from './ResearchBranchWorkspace'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowLeft, FlaskConical, LoaderCircle, Plus, RefreshCw } from 'lucide-react'
import { t } from '@shared/i18n'
import type { WorkspaceProfileScope } from '@shared/types'
import { LAB_PROTOCOL_IDS, parseLabRound, type LabRound, type LabAdapterId, type LabArtifact, type LabBrief, type LabCampaign, type LabCapabilities, type LabCreate, type LabDecision, type LabHistory, type LabInputs, type LabOrigin, type LabProtocol, type LabProtocolId, type LabExportSaved, type LabImportCoverage, type LabCorrection, type SourceInputs, type LabHypothesisSetInput, type LabSource } from '@shared/research-lab'
import { useLocale } from '../lib/i18n'
import { ResearchInputs, emptyInputs, fromInputs, parseInputs, sharedCorrectionSource, type InputDraft } from './ResearchInputs'
import { ResearchComparison, ResearchLimits, ResearchPlan, ResearchResult } from './ResearchResults'
import { researchText } from './ResearchLanguage'
import { SourceLink } from './IdeaResearch'
import { ResearchModels } from './ResearchModels'
import { HypothesisEditor, HypothesisSetView, editableHypothesisSet, hypothesisEvidenceOptions, validateHypothesisInput } from './ResearchHypotheses'
import './idea-lab.css'
import './research-lab.css'

interface Draft { entry: 'goal' | 'hypothesis'; brief: LabBrief; adapter: LabAdapterId; inputs: InputDraft; maxActions: number; maxRounds: number; origin?: LabOrigin; importCoverage?: LabImportCoverage; hypothesisSet?: LabHypothesisSetInput | null }
const blank = (): Draft => ({ entry: 'goal', brief: { goal: '', hypothesis: '', success_criteria: '', constraints: '' }, adapter: 'source_evidence', inputs: emptyInputs(), maxActions: 4, maxRounds: 3 })
const message = (problem: unknown) => problem instanceof Error ? problem.message : String(problem)
const readDraft = (key: string): Draft => { try { const value = JSON.parse(localStorage.getItem(key) || 'null'); if (value?.brief && value?.inputs && ['goal', 'hypothesis'].includes(value.entry) && ['source_evidence', 'paired_numeric'].includes(value.adapter)) return value } catch { /* Local drafts are optional. */ } return blank() }

export function ResearchLab({ scope, onClose, initialIdeaId }: { scope: WorkspaceProfileScope | null; onClose: () => void; initialIdeaId?: string | null }) {
  useLocale()
  const api = window.agentsDock.researchLab
  const scopeKey = JSON.stringify(scope), storageKey = `agentsdock-research-draft:${scope?.profileId ?? ''}:${scope?.serverIdentity ?? ''}`
  const [draft, setDraft] = useState<Draft>(() => readDraft(storageKey))
  const [campaign, setCampaign] = useState<LabCampaign | null>(null)
  const [items, setItems] = useState<LabCampaign[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<LabCapabilities | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const [feedback, setFeedback] = useState(''), [revisedGoal, setRevisedGoal] = useState('')
  const [revisedFields, setRevisedFields] = useState({ hypothesis: '', success_criteria: '', constraints: '' })
  const [hypothesisEdit, setHypothesisEdit] = useState<{ value: LabHypothesisSetInput | null; dirty: boolean; sources: Pick<LabSource, 'id' | 'title'>[] } | null>(null)
  const [history, setHistory] = useState<LabHistory | null>(null)
  const [artifacts, setArtifacts] = useState<Record<string, LabArtifact>>({})
  const [protocols, setProtocols] = useState<Partial<Record<LabProtocolId, LabProtocol>>>({})
  const [savedExport, setSavedExport] = useState<LabExportSaved | null>(null)
  const [correction, setCorrection] = useState<InputDraft | null>(null), [reason, setReason] = useState('')
  const [sharedCorrection, setSharedCorrection] = useState(false)
  const [statusRefreshNeeded, setStatusRefreshNeeded] = useState(false)
  const [branchBusy, setBranchBusy] = useState(false)
  const epoch = useRef(0), alive = useRef(true), currentScope = useRef(scopeKey)
  const contentRef = useRef<HTMLElement>(null)
  currentScope.current = scopeKey
  const keys = useRef(new Map<string, string>())
  const pending = useRef(false)
  const current = (version: number, owner: string) => alive.current && epoch.current === version && currentScope.current === owner
  const keyFor = (identity: string) => {
    const key = `${storageKey}:request:${identity}`
    if (!keys.current.has(key)) { let previous: string | null = null; try { previous = localStorage.getItem(key) } catch { /* optional cache */ } const value = previous || crypto.randomUUID(); keys.current.set(key, value); try { localStorage.setItem(key, value) } catch { /* keep identity in memory */ } }
    return keys.current.get(key)!
  }
  const edit = (next: Draft) => { setDraft(next); try { localStorage.setItem(storageKey, JSON.stringify(next)) } catch { /* Keep edits available in memory. */ } }
  const accept = (next: LabCampaign) => { setCampaign(previous => previous?.id === next.id && previous.revision > next.revision ? previous : next); setItems(previous => { const saved = previous.find(item => item.id === next.id); return [saved && saved.revision > next.revision ? saved : next, ...previous.filter(item => item.id !== next.id)] }); setHistory(null); setStatusRefreshNeeded(false) }
  const acceptMutation = async (next: LabCampaign, version: number, owner: string) => {
    if (!api || !scope || !current(version, owner)) return
    // Mutation acknowledgements are immutable replay receipts. Only a fresh GET
    // contains the current cross-campaign dependency projection.
    accept(next); setStatusRefreshNeeded(true)
    try { const fresh = next.branch_set && api.branches ? await api.branches.get(scope, next.id) : await api.get(scope, next.id); if (current(version, owner)) accept(fresh) }
    catch (problem) { if (current(version, owner)) setError(`${t('researchLab.savedNeedsRefresh')} ${message(problem)}`) }
  }
  const operate = async (work: () => Promise<void>) => { if (pending.current) return; pending.current = true; setBusy(true); setError(''); const version = epoch.current, owner = scopeKey; try { await work() } catch (problem) { if (current(version, owner)) setError(message(problem)) } finally { pending.current = false; if (current(version, owner)) setBusy(false) } }
  useEffect(() => { alive.current = true; return () => { alive.current = false; epoch.current++ } }, [])
  useLayoutEffect(() => { if (contentRef.current) contentRef.current.scrollTop = 0 }, [campaign?.id, initialIdeaId])
  useEffect(() => {
    const version = ++epoch.current, owner = scopeKey
    setCampaign(null); setItems([]); setDraft(readDraft(storageKey)); setHistory(null); setArtifacts({}); setProtocols({}); setSavedExport(null); setCorrection(null); setHypothesisEdit(null); setFeedback(''); setError(''); setBusy(false); setBranchBusy(false)
    if (!api || !scope) return
    Promise.all([api.list(scope), api.capabilities(scope)]).then(([page, manifest]) => { if (current(version, owner)) { setItems(page.items); setHasMore(page.has_more); setNextCursor(page.next_cursor ?? null); setCapabilities(manifest) } }).catch(problem => { if (current(version, owner)) setError(message(problem)) })
    // Import is read-only and happens only after the user's explicit Idea button.
    if (initialIdeaId) { setBusy(true); api.ideaSeed(scope, initialIdeaId).then(seed => { if (current(version, owner)) edit({ ...blank(), brief: seed.brief, origin: seed.origin, inputs: fromInputs(seed.inputs), importCoverage: seed.import_coverage }) }).catch(problem => { if (current(version, owner)) setError(message(problem)) }).finally(() => { if (current(version, owner)) setBusy(false) }) }
  }, [scopeKey, initialIdeaId, api])
  useEffect(() => {
    setHypothesisEdit(null)
    if (!campaign) return
    const key = `${storageKey}:human:${campaign.id}:${campaign.revision}`
    try { const value = JSON.parse(localStorage.getItem(key) || '{}'); setFeedback(value.feedback || ''); setRevisedGoal(value.goal || campaign.brief.goal); setRevisedFields({ hypothesis: value.hypothesis ?? campaign.brief.hypothesis, success_criteria: value.success_criteria ?? campaign.brief.success_criteria, constraints: value.constraints ?? campaign.brief.constraints }); setReason(value.reason || '') } catch { setFeedback(''); setRevisedGoal(campaign.brief.goal); setRevisedFields(campaign.brief); setReason('') }
  }, [campaign?.id, campaign?.revision, storageKey])
  const saveHuman = (patch: Record<string, unknown>) => { if (!campaign) return; try { const key = `${storageKey}:human:${campaign.id}:${campaign.revision}`; const previous = JSON.parse(localStorage.getItem(key) || '{}'); localStorage.setItem(key, JSON.stringify({ ...previous, feedback, goal: revisedGoal, ...revisedFields, reason, ...(hypothesisEdit?.dirty ? { hypothesisSet: hypothesisEdit.value } : {}), ...patch })) } catch { /* optional drafts */ } }
  const load = (id: string) => { epoch.current++; return operate(async () => { if (!api || !scope) return; const version = epoch.current, owner = scopeKey; let next = await api.get(scope, id); if (next.branch_set && api.branches) next = await api.branches.get(scope, id); if (current(version, owner)) { accept(next); setArtifacts({}); setCorrection(null) } }) }
  const create = () => operate(async () => {
    if (!api || !scope) return
    if (!draft.brief.goal.trim() || !draft.brief.success_criteria.trim() || (draft.entry === 'hypothesis' && !draft.brief.hypothesis.trim() && !draft.hypothesisSet)) throw new Error(t('researchLab.briefValidation'))
    const hypothesisSet = draft.entry === 'hypothesis' && draft.hypothesisSet ? validateHypothesisInput(draft.hypothesisSet, hypothesisEvidenceOptions(draft.adapter === 'source_evidence' ? draft.inputs.sources : [])) : undefined
    const payload = { entry: draft.entry, brief: draft.brief, adapter_id: draft.adapter, inputs: parseInputs(draft.adapter, draft.inputs), budget: { max_actions: draft.maxActions, max_rounds: draft.maxRounds }, ...(draft.origin ? { origin: draft.origin } : {}), ...(hypothesisSet ? { hypothesis_set: hypothesisSet } : {}) }
    const input: LabCreate = { ...payload, idempotency_key: keyFor(`create:${JSON.stringify(payload)}`) }
    const version = epoch.current, owner = scopeKey, next = await api.create(scope, input)
    if (current(version, owner)) { await acceptMutation(next, version, owner); try { localStorage.removeItem(storageKey) } catch { /* optional cache */ } }
  })
  const mutate = (method: 'decision' | 'run' | 'advance' | 'correctInputs', payload: Record<string, unknown> = {}) => operate(async () => {
    if (!api || !scope || !campaign) return
    const version = epoch.current, owner = scopeKey
    const input = { ...payload, expected_revision: campaign.revision, idempotency_key: keyFor(`${method}:${campaign.id}:${campaign.revision}:${JSON.stringify(payload)}`) }
    const next = method === 'decision' ? await api.decision(scope, campaign.id, input as LabDecision) : method === 'correctInputs' ? await api.correctInputs(scope, campaign.id, input as LabCorrection) : await api[method](scope, campaign.id, input)
    if (current(version, owner)) { await acceptMutation(next, version, owner); setCorrection(null); if (method === 'correctInputs') { try { localStorage.removeItem(`${storageKey}:correction:${campaign.id}:${campaign.revision}`) } catch { /* acknowledged correction */ } } }
  })
  const artifact = async (hash: string) => { if (!api || !scope || !campaign || artifacts[hash]) return; const version = epoch.current, owner = scopeKey; try { const value = await api.artifact(scope, campaign.id, hash); const omitted = campaign.omitted_rounds?.find(row => row.artifact === hash); if (omitted) { const round = parseLabRound(value.content); if (round.index !== omitted.index || round.run.id !== omitted.run_id) throw new Error(t('researchLab.wrongRound')) } if (current(version, owner)) setArtifacts(old => ({ ...old, [hash]: value })) } catch (problem) { if (current(version, owner)) setError(message(problem)) } }
  const startCorrection = () => operate(async () => { if (!api || !scope || !campaign) return; const version = epoch.current, owner = scopeKey; const value = await api.artifact(scope, campaign.id, campaign.input_artifact); if (current(version, owner)) { let saved: { draft?: InputDraft; shared?: boolean } = {}; try { saved = JSON.parse(localStorage.getItem(`${storageKey}:correction:${campaign.id}:${campaign.revision}`) || '{}') } catch { /* optional retry draft */ } setCorrection(saved.draft || fromInputs(value.content as LabInputs)); setSharedCorrection(saved.shared === true); setArtifacts(old => ({ ...old, [value.sha256]: value })) } })
  const startHypotheses = () => {
    if (!campaign) return
    let saved: { hypothesisSet?: LabHypothesisSetInput | null } = {}
    try { saved = JSON.parse(localStorage.getItem(`${storageKey}:human:${campaign.id}:${campaign.revision}`) || '{}') } catch { /* optional draft */ }
    const dirty = Object.prototype.hasOwnProperty.call(saved, 'hypothesisSet')
    const sources = Array.isArray(campaign.input_summary.sources) ? campaign.input_summary.sources.filter((source): source is Pick<LabSource, 'id' | 'title'> => !!source && typeof source.id === 'string' && typeof source.title === 'string') : []
    setHypothesisEdit({ value: dirty ? saved.hypothesisSet ?? null : campaign.hypothesis_set ? editableHypothesisSet(campaign.hypothesis_set) : null, dirty, sources })
  }
  const revise = () => {
    try {
      const hypothesisSet = hypothesisEdit?.dirty ? hypothesisEdit.value ? validateHypothesisInput(hypothesisEdit.value, hypothesisEvidenceOptions(hypothesisEdit.sources, campaign)) : null : undefined
      if (campaign?.entry === 'hypothesis' && !revisedFields.hypothesis.trim() && !(hypothesisSet === undefined ? campaign.hypothesis_set : hypothesisSet)) throw new Error(t('researchLab.briefValidation'))
      void mutate('decision', { kind: 'revise', goal: revisedGoal, ...revisedFields, feedback, ...(hypothesisSet !== undefined ? { hypothesis_set: hypothesisSet } : {}) })
    } catch (problem) { setError(message(problem)) }
  }
  const editCorrection = (next: InputDraft, shared = false) => { setCorrection(next); setSharedCorrection(shared); if (campaign) { try { localStorage.setItem(`${storageKey}:correction:${campaign.id}:${campaign.revision}`, JSON.stringify({ draft: next, shared })) } catch { /* preserve in memory */ } } }
  const readHistory = (before?: string) => operate(async () => { if (!api || !scope || !campaign) return; const version = epoch.current, owner = scopeKey, value = await api.history(scope, campaign.id, before); if (current(version, owner)) setHistory(old => ({ ...value, items: before ? [...new Map([...(old?.items ?? []), ...value.items].map(item => [item.revision, item])).values()] : value.items })) })
  const moreItems = () => operate(async () => { if (!api || !scope || !nextCursor) return; const version = epoch.current, owner = scopeKey, value = await api.list(scope, nextCursor); if (current(version, owner)) { setItems(old => [...new Map([...old, ...value.items].map(item => [item.id, item])).values()]); setHasMore(value.has_more); setNextCursor(value.next_cursor ?? null) } })
  const readProtocol = (id: LabProtocolId) => operate(async () => { if (!api || !scope || protocols[id]) return; const version = epoch.current, owner = scopeKey, value = await api.protocol(scope, id); if (current(version, owner)) setProtocols(old => ({ ...old, [id]: value })) })
  const exportCampaign = () => operate(async () => { if (!api || !scope || !campaign) return; setSavedExport(null); const version = epoch.current, owner = scopeKey, value = await api.export(scope, campaign.id); if (current(version, owner)) setSavedExport(value) })
  const reconcileDependencies = () => operate(async () => { if (!api || !scope || !campaign) return; const version = epoch.current, owner = scopeKey, next = await api.reconcileDependencies(scope, campaign.id); if (current(version, owner)) accept(next) })
  const disabled = busy || branchBusy || !api || !scope
  const canAdvance = campaign?.status === 'awaiting_next' || (campaign?.status === 'needs_input' && campaign.stop_reason === 'human_deferred')
  const latest = campaign?.rounds.at(-1)
  const dependencyBlocked = statusRefreshNeeded || campaign?.dependency_status?.run_allowed === false
  const originalSources = campaign && artifacts[campaign.input_artifact] && campaign.adapter.id === 'source_evidence' ? (artifacts[campaign.input_artifact].content as SourceInputs).sources : []
  const canShareCorrection = originalSources.some(source => source.provenance?.kind === 'idea_frozen_packet' && Number.isSafeInteger(source.provenance.decision_revision) && Number(source.provenance.decision_revision) > 0)
  const sharedSourceId = correction ? sharedCorrectionSource(originalSources, correction) : null
  const invalidated = (runId: string) => !!campaign?.claims.some(c => c.run_id === runId && c.status === 'needs_revalidation')
  return <div className="idea-lab research-lab">
    <header className="idea-lab-header"><div><h1><FlaskConical size={20} />{t('researchLab.name')}</h1><p>{t('researchLab.subtitle')}</p></div><button className="quiet-button" onClick={onClose}><ArrowLeft size={15} />{t('researchLab.back')}</button></header>
    <div className="idea-lab-layout"><aside className="idea-lab-history"><button className="quiet-button" disabled={busy || branchBusy} onClick={() => { epoch.current++; setCampaign(null); setDraft(readDraft(storageKey)); setCorrection(null); setArtifacts({}); setError('') }}><Plus size={15} />{t('researchLab.new')}</button><h2>{t('researchLab.recent')}</h2>{items.map(item => <button className={`idea-lab-history-item ${campaign?.id === item.id ? 'selected' : ''}`} disabled={busy || branchBusy} key={item.id} onClick={() => void load(item.id)}><span>{item.brief.goal}</span><small>{t(`researchLab.status.${item.status}`)}</small></button>)}{hasMore && <button className="quiet-button" disabled={busy || !nextCursor} onClick={() => void moreItems()}>{t('researchLab.moreResearch')}</button>}</aside>
    <main ref={contentRef} className="idea-lab-content" aria-busy={busy}>
      {(!api || !scope) && <p role="status">{t('researchLab.needServer')}</p>}
      {error && <div className="idea-lab-error" role="alert">{error}<button className="quiet-button" onClick={() => setError('')}>{t('researchLab.dismiss')}</button></div>}
      {busy && <p role="status"><LoaderCircle className="spin" size={15} /> {t('researchLab.working')}</p>}
      {!campaign ? <>
        <section className="idea-lab-section"><h2>{t('researchLab.start')}</h2><p>{t('researchLab.scopeNotice')}</p>
          {draft.origin && <p className="research-lab-origin">{t('researchLab.ideaOrigin')} · {draft.origin.selected_ids.length} {t('researchLab.savedDirections')}<br />{t('researchLab.originNotice')}</p>}
          {draft.importCoverage && <p className={draft.importCoverage.complete ? 'idea-lab-note' : 'research-lab-warning'}>{t('researchLab.importCoverage', { imported: draft.importCoverage.imported_sources, referenced: draft.importCoverage.referenced_sources })}{!draft.importCoverage.complete && <> {t('researchLab.importSubset')}</>}</p>}
          <label>{t('researchLab.entry')}<select disabled={disabled} value={draft.entry} onChange={e => edit({ ...draft, entry: e.target.value as Draft['entry'] })}><option value="goal">{t('researchLab.goalEntry')}</option><option value="hypothesis">{t('researchLab.hypothesisEntry')}</option></select></label>
          <label>{t('researchLab.goal')}<textarea disabled={disabled} value={draft.brief.goal} onChange={e => edit({ ...draft, brief: { ...draft.brief, goal: e.target.value } })} /></label>
          <label>{t('researchLab.hypothesis')}<textarea disabled={disabled} value={draft.brief.hypothesis} onChange={e => edit({ ...draft, brief: { ...draft.brief, hypothesis: e.target.value } })} /></label>
          {draft.entry === 'hypothesis' && <details><summary>{t('researchLab.hypotheses.editor')}</summary><HypothesisEditor value={draft.hypothesisSet ?? null} initialStatement={draft.brief.hypothesis} onChange={hypothesisSet => edit({ ...draft, hypothesisSet })} options={hypothesisEvidenceOptions(draft.adapter === 'source_evidence' ? draft.inputs.sources : [])} disabled={disabled} /></details>}
          <label>{t('researchLab.success')}<textarea disabled={disabled} value={draft.brief.success_criteria} onChange={e => edit({ ...draft, brief: { ...draft.brief, success_criteria: e.target.value } })} /></label>
          <details><summary>{t('researchLab.constraintsBudget')}</summary><label>{t('researchLab.constraints')}<textarea disabled={disabled} value={draft.brief.constraints} onChange={e => edit({ ...draft, brief: { ...draft.brief, constraints: e.target.value } })} /></label><div className="idea-lab-two"><label>{t('researchLab.maxActions')}<input type="number" min="1" max="12" disabled={disabled} value={draft.maxActions} onChange={e => edit({ ...draft, maxActions: Number(e.target.value) })} /></label><label>{t('researchLab.maxRounds')}<input type="number" min="1" max="6" disabled={disabled} value={draft.maxRounds} onChange={e => edit({ ...draft, maxRounds: Number(e.target.value) })} /></label></div></details>
        </section>
        <section className="idea-lab-section"><h2>{t('researchLab.methodInputs')}</h2><label>{t('researchLab.adapter')}<select disabled={disabled || !!draft.origin} value={draft.adapter} onChange={e => edit({ ...draft, adapter: e.target.value as LabAdapterId })}><option value="source_evidence">{t('researchLab.sourceAdapter')}</option><option value="paired_numeric">{t('researchLab.numericAdapter')}</option></select></label>
          <ResearchInputs adapter={draft.adapter} draft={draft.inputs} onChange={inputs => edit({ ...draft, inputs })} disabled={disabled} frozenSources={!!draft.origin} />
          <details><summary>{t('researchLab.applicability')}</summary><ul>{capabilities?.adapters.find(a => a.id === draft.adapter)?.limitations.map((line, i) => <li key={i}>{line}</li>)}</ul></details>
        </section><button className="primary-button" disabled={disabled} onClick={() => void create()}>{t('researchLab.create')}</button><p className="idea-lab-note">{t('researchLab.createNotice')}</p>
      </> : <>
        <section className="idea-lab-section"><div className="idea-lab-section-heading"><h2>{campaign.brief.goal}</h2><button className="quiet-button" disabled={busy || branchBusy} onClick={() => void load(campaign.id)}><RefreshCw size={14} />{t('researchLab.refresh')}</button></div><p><strong>{campaign.branch_set ? t(campaign.branch_set.root_control === 'active' ? 'researchLab.branch.rootStatus' : campaign.branch_set.root_control === 'paused' ? 'researchLab.branch.rootPaused' : 'researchLab.branch.rootStopped') : t(`researchLab.status.${campaign.status}`)}</strong> · {t('researchLab.remainingBudget', { actions: campaign.budget.remaining_actions, rounds: Math.max(0, campaign.budget.max_rounds - (campaign.total_rounds ?? campaign.rounds.length)) })}</p>
          {campaign.origin && <p className="idea-lab-note">{t('researchLab.ideaOrigin')}</p>}<details><summary>{t('researchLab.briefDetails')}</summary><p>{campaign.brief.hypothesis}</p><p>{campaign.brief.success_criteria}</p><p>{campaign.brief.constraints}</p>{!campaign.hypothesis_set && <ul>{campaign.hypotheses.map(h => <li key={h.id}>{h.statement}<br />{h.weakening_condition}</li>)}</ul>}</details>
        </section>
        {campaign.hypothesis_set && <HypothesisSetView value={campaign.hypothesis_set} status={campaign.hypothesis_set_status} artifact={campaign.hypothesis_set_artifact} onArtifact={hash => void artifact(hash)} artifacts={artifacts} />}
        {campaign.dependency_status && (campaign.dependency_status.state !== 'current' || campaign.dependency_status.pending_deliveries > 0) && <section className="research-lab-warning" role="status"><p>{t(campaign.branch_set ? 'researchLab.branch.sourceState' : campaign.dependency_status.state === 'unregistered' ? 'researchLab.dependenciesUnregistered' : dependencyBlocked ? 'researchLab.dependenciesBlocked' : 'researchLab.dependenciesHistorical')}</p>{(['unregistered', 'correction_pending'].includes(campaign.dependency_status.state) || campaign.dependency_status.pending_deliveries > 0) && <button className="quiet-button" disabled={disabled} onClick={() => void reconcileDependencies()}>{t('researchLab.syncDependencies')}</button>}{dependencyBlocked && campaign.dependency_status.state !== 'unregistered' && <button className="quiet-button" disabled={disabled} onClick={() => void startCorrection()}>{t('researchLab.correct')}</button>}<details><summary>{t('researchLab.dependencyDetails')}</summary>{[...campaign.dependency_status.pending_intents, ...campaign.dependency_status.corrections].map(item => <p key={item.id}>{item.reason}</p>)}<pre className="idea-lab-source-text">{JSON.stringify(campaign.dependency_status, null, 2)}</pre></details></section>}
        {statusRefreshNeeded && <p role="status" className="research-lab-warning">{t('researchLab.savedNeedsRefresh')}</p>}
        {api && scope && <ResearchBranchWorkspace key={`${scopeKey}:${campaign.id}`} api={api} scope={scope} campaign={campaign} disabled={busy || statusRefreshNeeded} storageKey={storageKey} onChange={accept} onBusy={setBranchBusy} />}
        {!campaign.branch_set && <>
        {api?.models && scope && <ResearchModels key={`${scopeKey}:${campaign.id}`} api={api.models} scope={scope} campaign={campaign} disabled={busy || branchBusy} blocked={statusRefreshNeeded} onUseFeedback={value => { setFeedback(value); saveHuman({ feedback: value }) }} />}
        {latest && <section className="idea-lab-section"><ResearchResult round={latest} invalidated={invalidated(latest.run.id)} onArtifact={hash => void artifact(hash)} artifacts={artifacts} /></section>}
        {!!campaign.comparisons?.length && <section className="idea-lab-section"><ResearchComparison comparison={campaign.comparisons.at(-1)!} onArtifact={hash => void artifact(hash)} artifacts={artifacts} /></section>}
        {campaign.review && <section className="idea-lab-section"><h2>{t('researchLab.review')}</h2><p>{researchText(campaign.review.reason)}</p><ResearchLimits values={campaign.review.uncertainties} alreadyShown={latest?.analysis.limitations} maximum={2} />{canAdvance && <><button className="primary-button" disabled={disabled} onClick={() => void mutate('advance')}>{t('researchLab.continue')}</button><p className="idea-lab-note">{t('researchLab.continueNotice')}</p></>}</section>}
        {canAdvance && !campaign.review && <button className="primary-button" disabled={disabled} onClick={() => void mutate('advance')}>{t('researchLab.resume')}</button>}
        {campaign.status === 'planned' && campaign.current_plan && <>
          <label>{t('researchLab.feedback')}<textarea disabled={disabled} value={feedback} onChange={e => { setFeedback(e.target.value); saveHuman({ feedback: e.target.value }) }} /></label>
          <ResearchPlan plan={campaign.current_plan} disabled={disabled} feedback={feedback} onArtifact={hash => void artifact(hash)} artifacts={artifacts} onSelect={(id, note) => void mutate('decision', { kind: 'select', selected_action_id: id, feedback: note })} />
          <div className="idea-lab-actions"><button className="primary-button" disabled={disabled || dependencyBlocked || campaign.current_plan.selection_origin !== 'human'} onClick={() => void mutate('run')}>{t('researchLab.run')}</button><button className="quiet-button" disabled={disabled} onClick={() => void mutate('decision', { kind: 'defer', feedback })}>{t('researchLab.defer')}</button></div><p className="idea-lab-note">{t(campaign.current_plan.selection_origin !== 'human' ? 'researchLab.chooseFirst' : 'researchLab.runNotice')}</p>
        </>}
        </>}
        {campaign.status !== 'stopped' && <details className="idea-lab-section"><summary>{t(campaign.branch_set ? 'researchLab.branch.rootChange' : 'researchLab.change')}</summary>{campaign.branch_set && <><p className="research-lab-warning">{t('researchLab.branch.rootChangeNotice')}</p><button className="quiet-button" disabled={disabled} onClick={() => campaign.branch_set!.root_control === 'paused' ? void mutate('advance') : void mutate('decision', { kind: 'defer', feedback })}>{t(campaign.branch_set.root_control === 'paused' ? 'researchLab.branch.resumeAll' : 'researchLab.branch.pauseAll')}</button><p className="idea-lab-note">{t('researchLab.branch.rootPauseNotice')}</p></>}<label>{t('researchLab.revisedGoal')}<textarea disabled={disabled} value={revisedGoal} onChange={e => { setRevisedGoal(e.target.value); saveHuman({ goal: e.target.value }) }} /></label>{(['hypothesis', 'success_criteria', 'constraints'] as const).map(field => <label key={field}>{t(`researchLab.revised.${field}`)}<textarea disabled={disabled} value={revisedFields[field]} onChange={e => { setRevisedFields(old => ({ ...old, [field]: e.target.value })); saveHuman({ [field]: e.target.value }) }} /></label>)}<details><summary>{t('researchLab.hypotheses.editor')}</summary>{hypothesisEdit ? <HypothesisEditor value={hypothesisEdit.value} initialStatement={revisedFields.hypothesis} options={hypothesisEvidenceOptions(hypothesisEdit.sources, campaign)} disabled={disabled} onChange={value => { setHypothesisEdit({ ...hypothesisEdit, value, dirty: true }); saveHuman({ hypothesisSet: value }) }} /> : <button className="quiet-button" disabled={disabled} onClick={() => void startHypotheses()}>{t('researchLab.hypotheses.edit')}</button>}<p className="idea-lab-note">{t('researchLab.hypotheses.saveNotice')}</p></details><label>{t('researchLab.feedback')}<textarea disabled={disabled} value={feedback} onChange={e => { setFeedback(e.target.value); saveHuman({ feedback: e.target.value }) }} /></label><div className="idea-lab-actions"><button className="quiet-button" disabled={disabled || !revisedGoal.trim() || !revisedFields.success_criteria.trim()} onClick={revise}>{t('researchLab.revise')}</button><button className="quiet-button" disabled={disabled} onClick={() => void startCorrection()}>{t('researchLab.correct')}</button><button className="quiet-button" disabled={disabled} onClick={() => void mutate('decision', { kind: 'stop', feedback })}>{t('researchLab.stop')}</button></div><p className="idea-lab-note">{t('researchLab.changeNotice')}</p></details>}
        {correction && <section className="idea-lab-section"><h2>{t('researchLab.correct')}</h2><p>{t('researchLab.correctionNotice')}</p><ResearchInputs adapter={campaign.adapter.id as LabAdapterId} draft={correction} onChange={next => editCorrection(next)} disabled={disabled} />{canShareCorrection && <div><label className="idea-lab-pick"><input type="checkbox" disabled={disabled || !sharedSourceId} checked={sharedCorrection && !!sharedSourceId} onChange={e => editCorrection(correction, e.target.checked)} />{t('researchLab.sharedCorrection')}</label><p className="idea-lab-note">{t('researchLab.sharedCorrectionNotice')}</p>{!sharedSourceId && <p>{t('researchLab.sharedCorrectionSingle')}</p>}</div>}<label>{t('researchLab.correctionReason')}<textarea disabled={disabled} value={reason} onChange={e => { setReason(e.target.value); saveHuman({ reason: e.target.value }) }} /></label><button className="primary-button" disabled={disabled || !reason.trim()} onClick={() => { try { void mutate('correctInputs', { inputs: parseInputs(campaign.adapter.id as LabAdapterId, correction), reason, ...(sharedCorrection && sharedSourceId ? { shared_source_id: sharedSourceId } : {}) }) } catch (problem) { setError(message(problem)) } }}>{t('researchLab.saveCorrection')}</button><button className="quiet-button" disabled={busy || branchBusy} onClick={() => setCorrection(null)}>{t('researchLab.dismiss')}</button></section>}
        <details className="idea-lab-section"><summary>{t('researchLab.history')}</summary>{campaign.has_more_rounds && <p>{t('researchLab.roundProjection', { shown: campaign.rounds.length, total: campaign.total_rounds ?? campaign.rounds.length })}</p>}{campaign.omitted_rounds?.map(omitted => <details key={omitted.run_id}><summary>{t('researchLab.roundResult', { round: omitted.index })}</summary><button className="quiet-button" disabled={busy || branchBusy} onClick={() => void artifact(omitted.artifact)}>{t('researchLab.loadEarlierRound')}</button>{artifacts[omitted.artifact] && <ResearchResult round={artifacts[omitted.artifact].content as LabRound} invalidated={invalidated(omitted.run_id)} onArtifact={hash => void artifact(hash)} artifacts={artifacts} />}</details>)}{(campaign.branch_set ? campaign.rounds : campaign.rounds.slice(0, -1)).map(round => <details key={round.run.id}><summary>{t('researchLab.roundResult', { round: round.index })}</summary><ResearchResult round={round} invalidated={invalidated(round.run.id)} onArtifact={hash => void artifact(hash)} artifacts={artifacts} /></details>)}{(campaign.branch_set ? campaign.comparisons : campaign.comparisons?.slice(0, -1))?.map(comparison => <details key={comparison.id}><summary>{t('researchLab.comparison')}</summary><ResearchComparison comparison={comparison} onArtifact={hash => void artifact(hash)} artifacts={artifacts} /></details>)}<button className="quiet-button" disabled={busy || branchBusy} onClick={() => void readHistory()}>{t('researchLab.loadHistory')}</button>{history?.items.map(item => <details key={item.revision}><summary>{t('researchLab.revision', { count: item.revision })} · {item.brief.goal}</summary><p>{item.brief.hypothesis}</p><p>{item.brief.success_criteria}</p><p>{item.event} · {item.at}</p>{item.event_artifact && <><button className="quiet-button" disabled={busy || branchBusy} onClick={() => void artifact(item.event_artifact!)}>{t('researchLab.loadHistoryEvent')}</button>{artifacts[item.event_artifact] && <pre className="idea-lab-source-text">{JSON.stringify(artifacts[item.event_artifact].content, null, 2)}</pre>}</>}</details>)}{history?.has_more && <button className="quiet-button" disabled={busy || !history.next_cursor} onClick={() => void readHistory(history.next_cursor!)}>{t('researchLab.moreHistory')}</button>}<details><summary>{t('researchLab.decisions')}</summary>{campaign.has_more_decisions && <p>{t('researchLab.decisionProjection', { shown: campaign.decisions.length, total: campaign.total_decisions ?? campaign.decisions.length })}</p>}<pre className="idea-lab-source-text">{JSON.stringify(campaign.decisions, null, 2)}</pre></details></details>
        <details className="idea-lab-section"><summary>{t('researchLab.protocols')}</summary><p className="idea-lab-note">{t('researchLab.protocolNotice')}</p>{LAB_PROTOCOL_IDS.map(id => <div key={id}><button className="quiet-button" disabled={disabled || !!protocols[id]} onClick={() => void readProtocol(id)}>{t(`researchLab.protocol.${id}`)}</button>{protocols[id] && <article><h3>{protocols[id]!.title}</h3><p>{protocols[id]!.content}</p><details><summary>{t('researchLab.protocolVersion')}</summary><p>{protocols[id]!.summary_version}</p><p className="idea-lab-note">{protocols[id]!.summary_sha256}</p><SourceLink url={protocols[id]!.source.url} onError={setError} /><p>{protocols[id]!.source.updated_at}</p><dl>{protocols[id]!.source.blocks.map(block => <div key={block.id}><dt>{block.id}</dt><dd className="idea-lab-note">{block.hash}</dd></div>)}</dl></details></article>}</div>)}</details>
        <details className="idea-lab-section"><summary>{t('researchLab.exportTitle')}</summary><p>{t('researchLab.exportNotice')}</p><button className="quiet-button" disabled={disabled} onClick={() => void exportCampaign()}>{t('researchLab.export')}</button>{savedExport?.campaign_id === campaign.id && <p role="status">{t('researchLab.exportSaved', { path: savedExport.path })}</p>}</details>
        <details className="idea-lab-section"><summary>{t('researchLab.technical')}</summary><pre className="idea-lab-source-text">{JSON.stringify({ adapter: campaign.adapter, origin: campaign.origin, budget: campaign.budget, stop_reason: campaign.stop_reason, events: campaign.events }, null, 2)}</pre></details>
      </>}
    </main></div>
  </div>
}
