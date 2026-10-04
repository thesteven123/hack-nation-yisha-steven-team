import type { LabBranch } from '@shared/research-branches'
import { useEffect, useRef, useState } from 'react'
import { t } from '@shared/i18n'
import type { LabCampaign } from '@shared/research-lab'
import type { WorkspaceProfileScope } from '@shared/types'
import { RESEARCH_ROLES, researchJobActive, type AnalystOutput, type PlannerOutput, type ResearchModelAPI, type ResearchModelArtifact, type ResearchModelJob, type ResearchModelPage, type ResearchRole, type ReviewerOutput } from '@shared/research-models'
import { useLocale } from '../lib/i18n'
import { ResearchLimits } from './ResearchResults'
import { methodTitle } from './ResearchLanguage'

const problemText = (error: unknown) => error instanceof Error ? error.message : String(error)
const summary = (job: ResearchModelJob) => job.output ? 'reason' in job.output ? job.output.reason : job.output.summary : ''
const mergeJobs = (old: ResearchModelJob[], incoming: ResearchModelJob[]) => {
  const byId = new Map(old.map(job => [job.id, job]))
  for (const job of incoming) { const previous = byId.get(job.id); if (!previous || !(!researchJobActive(previous) && researchJobActive(job) || previous.updated_at > job.updated_at)) byId.set(job.id, job) }
  return [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
}

export function ResearchModels({ api, scope, campaign, disabled, blocked = false, branch, authorityEpoch, onStateChanged, onUseFeedback }: { api: ResearchModelAPI; scope: WorkspaceProfileScope; campaign: LabCampaign; disabled: boolean; blocked?: boolean; branch?: LabBranch; authorityEpoch?: number; onStateChanged?: () => Promise<void>; onUseFeedback: (value: string) => void }) {
  useLocale()
  const [jobs, setJobs] = useState<ResearchModelJob[]>([])
  const [quota, setQuota] = useState<ResearchModelPage['quota'] | null>(null)
  const [error, setError] = useState(''), [pending, setPending] = useState(false), [watching, setWatching] = useState<string | null>(null)
  const [artifacts, setArtifacts] = useState<Record<string, ResearchModelArtifact>>({})
  const branchContext = branch ? JSON.stringify([branch.revision, authorityEpoch, branch.root_context_hash, branch.input_artifact, branch.gate?.dependency_refs]) : ''
  const owner = JSON.stringify([scope, campaign.id, branch?.id, branchContext]), ownerRef = useRef(owner), alive = useRef(true), generation = useRef(0), watchToken = useRef(0), mutation = useRef(false)
  const [copied, setCopied] = useState(false)
  const [hasMore, setHasMore] = useState(false), [nextBefore, setNextBefore] = useState<number | null>(null)
  ownerRef.current = owner
  const current = (epoch: number) => alive.current && ownerRef.current === owner && generation.current === epoch
  const storagePrefix = `agentsdock-research-model:${scope.profileId}:${scope.serverIdentity}:${campaign.id}:${branch?.id ?? 'root'}`
  const requestKeys = useRef(new Map<string, string>())
  const listRequest = useRef(0)
  const currentResult = branch ? branch.latest_result?.current === true && branch.latest_result.status === 'completed' : campaign.rounds.some(round => round.run.input_artifact === campaign.input_artifact && round.plan.goal_revision === campaign.brief.revision)
  const active = jobs.find(researchJobActive)
  const quotaFull = !!quota && quota.used + quota.reserved >= quota.limit_jobs
  const executionBlocked = blocked || (branch ? branch.scope_status === undefined || branch.scope_status === 'unknown' || branch.control !== 'active' || campaign.branch_set?.root_control !== 'active' || !branch.gate || branch.gate.blockers.some(blocker => blocker.code !== 'branch_busy') : campaign.dependency_status?.run_allowed === false)
  const mayCreate = !branch || branch.scope_status === 'current' && !!branch.current_scope && branch.gate?.allowed === true && !!authorityEpoch
  const jobCurrent = (job: ResearchModelJob) => branch ? job.branch_id === branch.id && job.scope_status === 'current' : job.campaign_revision === campaign.revision
  const usageComplete = !hasMore && jobs.length > 0 && jobs.every(job => job.usage.complete === true && typeof job.usage.total_tokens === 'number')
  const upsert = (job: ResearchModelJob) => setJobs(previous => {
    const existing = previous.find(row => row.id === job.id)
    if (existing && (!researchJobActive(existing) && researchJobActive(job) || existing.updated_at > job.updated_at)) return previous
    return [job, ...previous.filter(row => row.id !== job.id)].sort((a, b) => b.created_at.localeCompare(a.created_at))
  })
  const refresh = async (epoch: number, before?: number) => {
    const request = ++listRequest.current
    const page = await api.list(scope, campaign.id, before, branch?.id)
    if (current(epoch) && request === listRequest.current) { setJobs(old => mergeJobs(old, page.items)); setQuota(page.quota); setHasMore(page.has_more ?? false); setNextBefore(page.next_before ?? null) }
  }
  useEffect(() => {
    alive.current = true; const epoch = ++generation.current
    mutation.current = false; setPending(false); setJobs([]); setQuota(null); setError(''); setWatching(null); setArtifacts({}); setCopied(false); setHasMore(false); setNextBefore(null); watchToken.current++
    void refresh(epoch).catch(error => { if (current(epoch)) setError(problemText(error)) })
    return () => { alive.current = false; generation.current++; watchToken.current++ }
  }, [owner, api])

  // A read on opening the workspace never starts/resumes provider work. Only a
  // create click or explicit View progress starts these server-held long waits.
  const watch = async (job: ResearchModelJob) => {
    if (job.status !== 'running') return
    const epoch = generation.current, token = ++watchToken.current
    setWatching(job.id); setError('')
    try {
      let latest = job
      while (latest.status === 'running' && current(epoch) && token === watchToken.current) {
        latest = await api.wait(scope, campaign.id, job.id)
        if (!current(epoch) || token !== watchToken.current) return
        upsert(latest)
      }
      if (current(epoch) && token === watchToken.current) { await refresh(epoch); if (current(epoch)) await onStateChanged?.() }
    } catch (error) { if (current(epoch) && token === watchToken.current) setError(problemText(error)) }
    finally { if (current(epoch) && token === watchToken.current) setWatching(null) }
  }
  const start = async (role: ResearchRole) => {
    if (mutation.current || disabled || executionBlocked || !mayCreate || active || quotaFull || !quota || role !== 'planner' && !currentResult) return
    mutation.current = true; setPending(true); setError(''); setCopied(false)
    const epoch = generation.current, identity = `${storagePrefix}:${branch ? `${branch.revision}:${authorityEpoch}` : campaign.revision}:${role}`
    let key = requestKeys.current.get(identity)
    if (!key) { try { key = localStorage.getItem(identity) || undefined } catch { /* retain in memory */ } key ||= crypto.randomUUID(); requestKeys.current.set(identity, key); try { localStorage.setItem(identity, key) } catch { /* optional retry identity cache */ } }
    try {
      const job = await api.create(scope, campaign.id, branch ? { role, branch_id: branch.id, expected_branch_revision: branch.revision, expected_authority_epoch: authorityEpoch!, idempotency_key: key } : { role, expected_revision: campaign.revision, idempotency_key: key })
      requestKeys.current.delete(identity); try { localStorage.removeItem(identity) } catch { /* acknowledged request */ }
      if (!current(epoch)) return
      upsert(job)
      await refresh(epoch)
      if (current(epoch)) await onStateChanged?.()
      if (current(epoch)) void watch(job)
    } catch (error) { if (current(epoch)) setError(problemText(error)) }
    finally { if (current(epoch)) { mutation.current = false; setPending(false) } }
  }
  const cancel = async (job: ResearchModelJob) => {
    if (mutation.current || disabled) return
    mutation.current = true; const epoch = generation.current
    watchToken.current++; setWatching(null); setPending(true); setError('')
    try { const next = await api.cancel(scope, campaign.id, job.id); if (current(epoch)) { upsert(next); await refresh(epoch); if (current(epoch)) await onStateChanged?.() } }
    catch (error) { if (current(epoch)) setError(problemText(error)) }
    finally { if (current(epoch)) { mutation.current = false; setPending(false) } }
  }
  const startPrepared = async (job: ResearchModelJob) => {
    if (mutation.current || disabled || executionBlocked || !jobCurrent(job) || job.status !== 'planned') return
    mutation.current = true; const epoch = generation.current; setPending(true); setError('')
    try { const next = await api.start(scope, campaign.id, job.id); if (current(epoch)) { upsert(next); await refresh(epoch); if (current(epoch)) await onStateChanged?.(); if (current(epoch)) void watch(next) } }
    catch (error) { if (current(epoch)) setError(problemText(error)) }
    finally { if (current(epoch)) { mutation.current = false; setPending(false) } }
  }
  const refreshManually = () => { const epoch = generation.current; setError(''); void refresh(epoch).catch(error => { if (current(epoch)) setError(problemText(error)) }) }
  const loadEarlier = async () => { if (!nextBefore || pending) return; const epoch = generation.current; setPending(true); try { await refresh(epoch, nextBefore) } catch (error) { if (current(epoch)) setError(problemText(error)) } finally { if (current(epoch)) setPending(false) } }
  const readArtifact = async (job: ResearchModelJob, hash: string) => {
    if (artifacts[hash]) return
    const epoch = generation.current
    try { const value = await api.artifact(scope, campaign.id, job.id, hash); if (current(epoch)) setArtifacts(old => ({ ...old, [hash]: value })) }
    catch (error) { if (current(epoch)) setError(problemText(error)) }
  }
  const view = (job: ResearchModelJob, historical = false) => {
    const oldVersion = !jobCurrent(job)
    const sourceInvalidated = branch ? job.scope_status === 'stale' : campaign.dependency_status?.run_allowed === false && campaign.dependency_status.state !== 'unregistered'
    const planner = job.role === 'planner' ? job.output as PlannerOutput | null : null
    const reviewer = job.role === 'reviewer' ? job.output as ReviewerOutput | null : null
    const needsMethod = planner?.status === 'needs_method' || reviewer?.next_action === 'needs_method'
    return <div key={job.id} className="research-model-job">
      <p className="idea-lab-note">{t(`researchLab.model.status.${job.status}`)}{oldVersion && <> · {t('researchLab.model.oldVersion')}</>}</p>
      {needsMethod && <p className="research-lab-warning">{t('researchLab.model.needsMethod')}</p>}
      {sourceInvalidated && job.output && <p className="research-lab-warning">{t('researchLab.model.sourceInvalidated')}</p>}
      {job.output && <><ModelSummary value={summary(job)} />{!historical && planner?.status === 'recommendation' && !oldVersion && !executionBlocked && <><p className="idea-lab-note">{t('researchLab.model.recommendationOnly')}</p><button className="quiet-button" disabled={disabled || pending} onClick={() => { onUseFeedback(planner.reason); setCopied(true) }}>{t('researchLab.model.useFeedback')}</button></>}</>}
      {job.error && <p role="status">{job.error.message}</p>}
      {researchJobActive(job) && <div className="idea-lab-actions">{job.status === 'planned' ? <button className="quiet-button" disabled={pending || disabled || executionBlocked || oldVersion} onClick={() => void startPrepared(job)}>{t('researchLab.model.startPrepared')}</button> : <button className="quiet-button" disabled={pending || disabled || watching === job.id} onClick={() => void watch(job)}>{t(watching === job.id ? 'researchLab.model.waiting' : 'researchLab.model.viewProgress')}</button>}<button className="quiet-button" disabled={pending || disabled} onClick={() => void cancel(job)}>{t('researchLab.model.cancel')}</button></div>}
      {job.output && <ModelInterpretation job={job} campaign={campaign} branch={branch} />}
      <details><summary>{t('researchLab.model.technical')}</summary><p>{t('researchLab.model.usage')}: {job.usage.complete === true && typeof job.usage.total_tokens === 'number' ? t('researchLab.model.tokens', { count: job.usage.total_tokens }) : t('researchLab.model.unknownUsage')}</p><pre className="idea-lab-source-text">{JSON.stringify({ id: job.id, campaign_revision: job.campaign_revision, brief_revision: job.brief_revision, provider: job.provider, usage: job.usage, context: job.context, events: job.events, has_more_events: job.has_more_events }, null, 2)}</pre>
        {(['packet_ref', 'output_ref', 'raw_output_ref'] as const).map(key => job[key] && <div key={key}><button className="quiet-button" disabled={pending} onClick={() => void readArtifact(job, job[key]!)}>{t(`researchLab.model.${key}`)}</button>{artifacts[job[key]!] && <pre className="idea-lab-source-text">{JSON.stringify(artifacts[job[key]!].content, null, 2)}</pre>}</div>)}
      </details>
    </div>
  }
  return <section className="idea-lab-section research-models">
    <h2>{t('researchLab.model.members')}</h2><p className="idea-lab-note">{t('researchLab.model.boundary')}</p>
    {branch && <p className="idea-lab-note">{t('researchLab.branch.modelScope')}</p>}
    {quota && <p className="idea-lab-note">{t('researchLab.model.quota', { used: quota.used, reserved: quota.reserved, limit: quota.limit_jobs })} · {jobs.length ? usageComplete ? t('researchLab.model.tokens', { count: jobs.reduce((sum, job) => sum + (job.usage.total_tokens ?? 0), 0) }) : t('researchLab.model.unknownUsage') : t('researchLab.model.notCalled')}</p>}
    {error && <div className="idea-lab-error" role="alert">{error}<button className="quiet-button" disabled={pending} onClick={refreshManually}>{t('researchLab.refresh')}</button></div>}
    {copied && <p role="status">{t('researchLab.model.feedbackCopied')}</p>}
    <div className="research-model-cards">{RESEARCH_ROLES.map(role => {
      const latest = jobs.find(job => job.role === role)
      return <article className="idea-lab-card" key={role}><h3>{t(`researchLab.model.role.${role}`)}</h3><p className="idea-lab-note">{t(`researchLab.model.purpose.${role}`)}</p>{latest ? view(latest) : <p>{t('researchLab.model.idle')}</p>}
        <button className="quiet-button" disabled={disabled || executionBlocked || !mayCreate || pending || !!active || quotaFull || !quota || campaign.status === 'stopped' || role !== 'planner' && !currentResult} onClick={() => void start(role)}>{t(`researchLab.model.request.${role}`)}</button>
        {role !== 'planner' && !currentResult && <p className="idea-lab-note">{t('researchLab.model.resultRequired')}</p>}
      </article>
    })}</div>
    {quotaFull && <p>{t('researchLab.model.quotaExhausted')}</p>}
    {(hasMore || jobs.some((job, i) => jobs.findIndex(other => other.role === job.role) !== i)) && <details><summary>{t('researchLab.model.past')}</summary>{jobs.filter((job, i) => jobs.findIndex(other => other.role === job.role) !== i).map(job => <details key={job.id}><summary>{t(`researchLab.model.role.${job.role}`)} · {t(`researchLab.model.status.${job.status}`)}</summary>{view(job, true)}</details>)}{hasMore && <button className="quiet-button" disabled={pending || !nextBefore} onClick={() => void loadEarlier()}>{t('researchLab.moreHistory')}</button>}</details>}
  </section>
}

function ModelSummary({ value }: { value: string }) {
  const characters = Array.from(value)
  return <><p>{characters.length > 240 ? `${characters.slice(0, 240).join('')}…` : value}</p>{characters.length > 240 && <details><summary>{t('researchLab.model.fullSummary')}</summary><p>{value}</p></details>}</>
}

function ModelInterpretation({ job, campaign, branch }: { job: ResearchModelJob; campaign: LabCampaign; branch?: LabBranch }) {
  const output = job.output!
  const questions = 'questions' in output ? output.questions : []
  const missing = 'missing_inputs' in output ? output.missing_inputs : []
  return <>
    {questions.length > 0 && <div className="research-model-questions"><strong>{t('researchLab.model.questions')}</strong><ul>{questions.map((question, i) => <li key={i}>{question}</li>)}</ul><p className="idea-lab-note">{t('researchLab.model.answerNotice')}</p></div>}
    {missing.length > 0 && <ResearchLimits values={missing} maximum={2} />}
    <details><summary>{t('researchLab.model.interpretation')}</summary><p className="idea-lab-note">{t('researchLab.model.interpretationNotice')}</p>
      {job.role === 'planner' && <>{(output as PlannerOutput).candidates.map(candidate => { const action = (branch ? branch.current_plan : campaign.current_plan)?.candidates.find(a => a.id === candidate.action_id); return <div key={candidate.action_id}><h4>{action ? methodTitle(action.method, action.title) : t('researchLab.model.earlierCandidate')}</h4><p>{t(`researchLab.model.applicability.${candidate.applicability}`)}</p><p>{candidate.goal_relevance}</p><p>{candidate.expected_learning}</p>{candidate.rejected_reason && <p>{candidate.rejected_reason}</p>}<ResearchLimits values={candidate.limitations} /></div> })}<p>{(output as PlannerOutput).scope_note}</p></>}
      {job.role === 'analyst' && <><p>{(output as AnalystOutput).hypothesis_assessment.reason}</p>{(output as AnalystOutput).findings.map(finding => <div key={finding.id}><p>{finding.interpretation}</p><ResearchLimits values={finding.limitations} /><details><summary>{t('researchLab.model.machineRefs')}</summary>{(output as AnalystOutput).machine_fact_refs.filter(fact => fact.finding_id === finding.id).map((fact, i) => <div key={i}><p>{fact.field_path} · {t(fact.qc_passed ? 'researchLab.qcPassed' : 'researchLab.qcFailed')}</p><pre className="idea-lab-source-text">{JSON.stringify(fact.value, null, 2)}</pre></div>)}</details></div>)}</>}
      {job.role === 'reviewer' && <><p>{t(`researchLab.model.next.${(output as ReviewerOutput).next_action}`)}: {(output as ReviewerOutput).next_action_reason}</p>{(output as ReviewerOutput).critiques.map((critique, i) => <div key={i}><p>{critique.concern}</p><p><strong>{t('researchLab.model.suggestedCheck')}: </strong>{critique.suggested_check}</p><details><summary>{t('researchLab.model.assessmentRefs')}</summary><pre className="idea-lab-source-text">{JSON.stringify({ claim_id: critique.claim_id, run_id: critique.run_id, source_support_assessment: critique.source_support_assessment, inference_assessment: critique.inference_assessment, assessment: critique.assessment }, null, 2)}</pre></details></div>)}<ResearchLimits values={(output as ReviewerOutput).unresolved} /></>}
    </details>
  </>
}
