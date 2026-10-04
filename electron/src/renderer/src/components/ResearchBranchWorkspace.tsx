import { useEffect, useRef, useState } from 'react'
import { t } from '@shared/i18n'
import { branchMayRun, branchPermissionKnown, branchResult, type BranchProposal, type LabBranch } from '@shared/research-branches'
import { parseLabRound, type LabArtifact, type LabCampaign, type ResearchLabAPI } from '@shared/research-lab'
import type { WorkspaceProfileScope } from '@shared/types'
import { ResearchBranchAnswers, ResearchBranchOverview } from './ResearchBranches'
import { ResearchBranchEditor, branchSources } from './ResearchBranchEditor'
import { ResearchModels } from './ResearchModels'
import { ResearchLimits, ResearchPlan, ResearchResult } from './ResearchResults'
import { methodTitle, researchText } from './ResearchLanguage'

/** Branch responses carry saved records. Only a dedicated GET supplies current admission. */
export function ResearchBranchWorkspace({ api, scope, campaign, disabled, storageKey, onChange, onBusy }: { api: ResearchLabAPI; scope: WorkspaceProfileScope; campaign: LabCampaign; disabled: boolean; storageKey: string; onChange: (campaign: LabCampaign) => void; onBusy: (busy: boolean) => void }) {
  const [selected, setSelected] = useState<string | null>(campaign.branch_set?.branches[0]?.id ?? null)
  const [pending, setPending] = useState(false), [needsRefresh, setNeedsRefresh] = useState(!!campaign.branch_set), [error, setError] = useState('')
  const [feedback, setFeedback] = useState(''), [artifacts, setArtifacts] = useState<Record<string, LabArtifact>>({})
  const [changes, setChanges] = useState<{ title: string; question: string; success_criterion: string; methods: string[]; source_ids?: string[] } | null>(null)
  const owner = JSON.stringify([scope, campaign.id]), ownerRef = useRef(owner), alive = useRef(true), lock = useRef(false), requestEpoch = useRef(0), readSequence = useRef(0)
  const set = campaign.branch_set, branch = set?.branches.find(item => item.id === selected) ?? set?.branches[0]
  ownerRef.current = owner
  const current = (token: number) => alive.current && ownerRef.current === owner && requestEpoch.current === token
  const feedbackKey = `${storageKey}:branch-feedback:${campaign.id}:${branch?.id}:${branch?.revision}:${set?.authority_epoch}`
  const saveFeedback = (value: string) => { setFeedback(value); try { localStorage.setItem(feedbackKey, value) } catch { /* optional draft */ } }
  useEffect(() => { setChanges(null); try { setFeedback(localStorage.getItem(feedbackKey) || '') } catch { setFeedback('') } }, [feedbackKey])
  const keyFor = (identity: string) => { const key = `${storageKey}:branch-request:${campaign.id}:${identity}`; try { const previous = localStorage.getItem(key); if (previous) return previous; const value = crypto.randomUUID(); localStorage.setItem(key, value); return value } catch { if (!requestKeys.current.has(key)) requestKeys.current.set(key, crypto.randomUUID()); return requestKeys.current.get(key)! } }
  const requestKeys = useRef(new Map<string, string>())
  const read = async (token: number) => { if (!api.branches) return; const request = ++readSequence.current; const fresh = await api.branches.get(scope, campaign.id); if (current(token) && request === readSequence.current) { onChange(fresh); setNeedsRefresh(false) } }
  useEffect(() => {
    alive.current = true; const token = ++requestEpoch.current
    if (campaign.branch_set && api.branches) { setNeedsRefresh(true); void read(token).catch(problem => { if (current(token)) setError(String(problem instanceof Error ? problem.message : problem)) }) }
    return () => { alive.current = false; requestEpoch.current++ }
  }, [owner, api.branches])
  useEffect(() => { onBusy(pending); return () => onBusy(false) }, [pending, onBusy])
  const refresh = async () => { if (lock.current || !api.branches) return; const token = ++requestEpoch.current; setError(''); setNeedsRefresh(true); try { await read(token) } catch (problem) { if (current(token)) setError(String(problem instanceof Error ? problem.message : problem)) } }
  const commit = async (work: () => Promise<LabCampaign>) => {
    if (lock.current || disabled || !api.branches) throw new Error(t('researchLab.working'))
    lock.current = true; setPending(true); setError(''); const token = ++requestEpoch.current
    try {
      const saved = await work()
      if (!current(token)) return
      onChange(saved); setNeedsRefresh(true)
      try { await read(token) } catch (problem) { if (current(token)) setError(`${t('researchLab.savedNeedsRefresh')} ${problem instanceof Error ? problem.message : String(problem)}`) }
    } catch (problem) { if (current(token)) setError(problem instanceof Error ? problem.message : String(problem)); throw problem }
    finally { lock.current = false; if (current(token)) setPending(false) }
  }
  const enable = (values: BranchProposal[]) => commit(() => api.branches!.enable(scope, campaign.id, { expected_revision: campaign.revision, idempotency_key: keyFor(`enable:${campaign.revision}:${JSON.stringify(values)}`), branches: values }))
  const mutate = (target: LabBranch, method: 'answers' | 'decision' | 'control' | 'plan' | 'run', payload: Record<string, unknown> = {}) => {
    const common = { expected_branch_revision: target.revision, expected_authority_epoch: set!.authority_epoch, idempotency_key: keyFor(`${target.id}:${target.revision}:${set!.authority_epoch}:${method}:${JSON.stringify(payload)}`) }
    return commit(() => {
      const branches = api.branches!
      if (method === 'answers') return branches.answers(scope, campaign.id, target.id, { ...common, answers: payload.answers as { question_id: string; answer: string }[] })
      if (method === 'decision') return branches.decision(scope, campaign.id, target.id, { ...common, selected_action_id: String(payload.selected_action_id), feedback: String(payload.feedback) })
      if (method === 'control') return branches.control(scope, campaign.id, target.id, { ...common, operation: payload.operation as 'pause' | 'resume' | 'stop', feedback: String(payload.feedback) })
      if (method === 'plan') return branches.plan(scope, campaign.id, target.id, { ...common, ...(payload.changes ? { changes: payload.changes as NonNullable<typeof changes> } : {}) })
      return branches.run(scope, campaign.id, target.id, common)
    })
  }
  const click = (work: Promise<void>) => { void work.catch(() => { /* commit already presents the error; keep exact request identity */ }) }
  const artifact = async (hash: string, target?: LabBranch) => {
    if (artifacts[hash]) return
    const token = requestEpoch.current
    try { const value = await api.artifact(scope, campaign.id, hash); if (target?.latest_result?.round_artifact === hash) { const round = parseLabRound(value.content); if (round.run.id !== target.latest_result.run_id) throw new Error(t('researchLab.wrongRound')) } if (current(token)) setArtifacts(previous => ({ ...previous, [hash]: value })) }
    catch (problem) { if (current(token)) setError(problem instanceof Error ? problem.message : String(problem)) }
  }
  if (!api.branches) return set ? <p className="research-lab-warning">{t('researchLab.branch.pending')}</p> : null
  if (!set || !branch) return <ResearchBranchEditor key={`${campaign.id}:${campaign.brief.revision}:${campaign.input_artifact}`} campaign={campaign} storageKey={storageKey} disabled={disabled || pending || campaign.status === 'stopped'} onEnable={enable} />
  const blocked = disabled || pending
  const known = !needsRefresh && branchPermissionKnown(branch)
  const ready = known && branch.scope_status === 'current' && !!branch.current_scope && branch.gate?.allowed === true && branch.gate.blockers.length === 0
  const exhausted = campaign.budget.remaining_actions <= 0 || (campaign.total_rounds ?? campaign.rounds.length) >= campaign.budget.max_rounds
  const latest = branchResult(campaign, branch) ?? (branch.latest_result && artifacts[branch.latest_result.round_artifact] ? parseLabRound(artifacts[branch.latest_result.round_artifact].content) : undefined)
  const controlAvailable = set.root_control !== 'stopped' && branch.control !== 'stopped'
  return <>
    <ResearchBranchOverview value={set} selectedId={branch.id} disabled={blocked} onSelect={id => { setSelected(id); setChanges(null) }} />
    {error && <p role="alert" className="idea-lab-error">{error}</p>}
    {needsRefresh && <p className="research-lab-warning">{t('researchLab.savedNeedsRefresh')}</p>}
    <button className="quiet-button" disabled={blocked} onClick={() => void refresh()}>{t('researchLab.branch.refresh')}</button>
    {set.branches.map(item => <ResearchBranchAnswers key={item.id} branch={item} authorityEpoch={set.authority_epoch} storageKey={`${storageKey}:${campaign.id}`} disabled={blocked || needsRefresh || set.root_control === 'stopped' || item.control === 'stopped'} onSave={answers => mutate(item, 'answers', { answers })} />)}
    <section className="idea-lab-section"><h2>{branch.title}</h2><p>{branch.question}</p><p className="idea-lab-note">{branch.success_criterion}</p>
      {branch.post_outcome && <p className="idea-lab-note">{t('researchLab.branch.postOutcome')}</p>}
      {latest && <ResearchResult round={latest} invalidated={branch.latest_result?.current !== true} onArtifact={hash => void artifact(hash)} artifacts={artifacts} />}
      {branch.latest_result && !latest && <button className="quiet-button" disabled={blocked} onClick={() => void artifact(branch.latest_result!.round_artifact, branch)}>{t('researchLab.loadEarlierRound')}</button>}
      {latest && <><p>{researchText(latest.review.reason)}</p><ResearchLimits values={latest.review.uncertainties} alreadyShown={latest.analysis.limitations} maximum={2} /></>}
      {api.models && <ResearchModels key={`${owner}:${branch.id}`} api={api.models} scope={scope} campaign={campaign} branch={branch} authorityEpoch={set.authority_epoch} disabled={blocked} blocked={needsRefresh} onStateChanged={() => read(requestEpoch.current)} onUseFeedback={saveFeedback} />}
      <label>{t('researchLab.feedback')}<textarea disabled={blocked} value={feedback} onChange={event => saveFeedback(event.target.value)} /></label>
      {branch.current_plan && <ResearchPlan plan={branch.current_plan} disabled={blocked || !ready || branch.control !== 'active' || set.root_control !== 'active'} feedback={feedback} onArtifact={hash => void artifact(hash)} artifacts={artifacts} onSelect={(id, note) => click(mutate(branch, 'decision', { selected_action_id: id, feedback: note }))} />}
      <div className="idea-lab-actions">
        {(!branch.current_plan || branch.status === 'awaiting_next' || branch.status === 'completed') && <button className="quiet-button" disabled={blocked || !ready || exhausted || !controlAvailable} onClick={() => click(mutate(branch, 'plan'))}>{t('researchLab.branch.prepare')}</button>}
        {branch.current_plan && <button className="primary-button" disabled={blocked || needsRefresh || exhausted || !branchMayRun(set, branch)} onClick={() => click(mutate(branch, 'run'))}>{t('researchLab.run')}</button>}
        {controlAvailable && <button className="quiet-button" disabled={blocked || needsRefresh} onClick={() => click(mutate(branch, 'control', { operation: branch.control === 'paused' ? 'resume' : 'pause', feedback }))}>{t(branch.control === 'paused' ? 'researchLab.branch.resume' : 'researchLab.branch.pause')}</button>}
      </div><p className="idea-lab-note">{t('researchLab.branch.explicitRun')}</p>
      {controlAvailable && <details><summary>{t('researchLab.branch.change')}</summary><p>{t('researchLab.branch.changeNotice')}</p>
        {changes ? <><label>{t('researchLab.branch.title')}<input disabled={blocked} value={changes.title} onChange={event => setChanges({ ...changes, title: event.target.value })} /></label><label>{t('researchLab.branch.question')}<textarea disabled={blocked} value={changes.question} onChange={event => setChanges({ ...changes, question: event.target.value })} /></label><label>{t('researchLab.branch.criterion')}<textarea disabled={blocked} value={changes.success_criterion} onChange={event => setChanges({ ...changes, success_criterion: event.target.value })} /></label>{campaign.brief.authorized_actions.map(method => <label className="idea-lab-pick" key={method}><input type="checkbox" disabled={blocked} checked={changes.methods.includes(method)} onChange={event => setChanges({ ...changes, methods: event.target.checked ? [...changes.methods, method] : changes.methods.filter(item => item !== method) })} />{methodTitle(method, method)}</label>)}{campaign.adapter.id === 'source_evidence' && branchSources(campaign).map(source => <label className="idea-lab-pick" key={source.id}><input type="checkbox" disabled={blocked} checked={changes.source_ids?.includes(source.id) ?? false} onChange={event => setChanges({ ...changes, source_ids: event.target.checked ? [...(changes.source_ids ?? []), source.id] : changes.source_ids?.filter(id => id !== source.id) })} />{source.title}</label>)}<button className="quiet-button" disabled={blocked || needsRefresh || !changes.title.trim() || !changes.question.trim() || !changes.success_criterion.trim() || !changes.methods.length || campaign.adapter.id === 'source_evidence' && !changes.source_ids?.length} onClick={() => click(mutate(branch, 'plan', { changes }))}>{t('researchLab.branch.savePlan')}</button></> : <button className="quiet-button" disabled={blocked || needsRefresh} onClick={() => setChanges({ title: branch.title, question: branch.question, success_criterion: branch.success_criterion, methods: [...branch.methods], ...(branch.source_ids ? { source_ids: [...branch.source_ids] } : {}) })}>{t('researchLab.branch.edit')}</button>}
        <button className="quiet-button" disabled={blocked || needsRefresh} onClick={() => click(mutate(branch, 'control', { operation: 'stop', feedback }))}>{t('researchLab.branch.stop')}</button><p className="idea-lab-note">{t('researchLab.branch.pauseNotice')}</p>
      </details>}
      <details><summary>{t('researchLab.technical')}</summary><pre className="idea-lab-source-text">{JSON.stringify(branch, null, 2)}</pre></details>
    </section>
    {!!set.historical_unassigned_run_ids.length && <p className="idea-lab-note">{t('researchLab.branch.legacyRuns')}</p>}
  </>
}
