import { useEffect, useRef, useState } from 'react'
import { t } from '@shared/i18n'
import { branchPermissionKnown, type LabBranch, type LabBranchSet } from '@shared/research-branches'
import { useLocale } from '../lib/i18n'

const blockerLabels: Record<string, string> = {
  root_paused: 'rootPaused', root_stopped: 'rootStopped', branch_paused: 'paused', branch_stopped: 'stopped',
  root_context_changed: 'contextChanged', branch_input_changed: 'inputChanged', branch_input_missing: 'inputMissing',
  input_integrity: 'inputChanged', dependency_unregistered: 'checkingSources', dependency_stale: 'inputChanged',
  required_answer: 'needsAnswer', branch_busy: 'busy', budget_exhausted: 'budgetExhausted'
}

export function branchStatusText(set: LabBranchSet, branch: LabBranch): string {
  const work = branch.owned_work
  if (work?.cancellation_requested || work?.outcome === 'cancellation_pending' || work?.outcome === 'cleanup_pending') return t('researchLab.branch.cleanup')
  if (work?.outcome === 'unknown_outcome') return t('researchLab.branch.outcomeUnknown')
  if (!branchPermissionKnown(branch)) return t('researchLab.branch.pending')
  if (set.root_control !== 'active') return t(`researchLab.branch.${set.root_control === 'paused' ? 'rootPaused' : 'rootStopped'}`)
  if (branch.control !== 'active') return t(`researchLab.branch.${branch.control}`)
  const blocker = branch.gate!.blockers[0]
  if (blocker?.code === 'dependency_pending') return t('researchLab.branch.waitingFor', { title: set.branches.find(item => item.id === blocker.branch_id)?.title ?? t('researchLab.branch.anotherBranch') })
  if (blocker) return t(`researchLab.branch.${blockerLabels[blocker.code] ?? 'pending'}`)
  if (branch.scope_status !== 'current' || !branch.current_scope) return t('researchLab.branch.pending')
  if (work) return t('researchLab.branch.busy')
  if (branch.status === 'planned' && branch.current_plan?.selection_origin === 'human') return t('researchLab.branch.readyToRun')
  if (branch.status === 'needs_input' && !branch.current_plan && branch.gate?.allowed) return t('researchLab.branch.readyToPlan')
  return t(`researchLab.branch.status.${branch.status}`)
}

/** Presentation only: selecting a card never selects an action or starts work. */
export function ResearchBranchOverview({ value, selectedId, disabled, onSelect }: { value: LabBranchSet; selectedId: string | null; disabled: boolean; onSelect: (id: string) => void }) {
  useLocale()
  return <section className="idea-lab-section research-branches">
    <h2>{t('researchLab.branch.heading')}</h2>
    <p className="idea-lab-note">{t('researchLab.branch.sharedBudget')}</p>
    <div className="idea-lab-cards">{value.branches.map(branch => <article key={branch.id} className={`idea-lab-card${selectedId === branch.id ? ' selected' : ''}`}>
      <h3>{branch.title}</h3><p>{branch.question}</p><p role="status">{branchStatusText(value, branch)}</p>
      <button className="quiet-button" aria-pressed={selectedId === branch.id} disabled={disabled} onClick={() => onSelect(branch.id)}>{t(selectedId === branch.id ? 'researchLab.branch.selected' : 'researchLab.branch.view')}</button>
    </article>)}</div>
    <p className="idea-lab-note">{t('researchLab.branch.viewOnly')}</p>
  </section>
}

/** One atomic answer request per branch, scoped to this exact question context. */
export function ResearchBranchAnswers({ branch, authorityEpoch, storageKey, disabled, onSave }: {
  branch: LabBranch; authorityEpoch: number; storageKey: string; disabled: boolean;
  onSave: (answers: { question_id: string; answer: string }[]) => Promise<void>
}) {
  useLocale()
  const key = `${storageKey}:branch-answers:${branch.id}:${branch.revision}:${authorityEpoch}:${branch.questions.map(question => question.prompt_hash).join(':')}`
  const [draft, setDraft] = useState<Record<string, string>>({}), [saving, setSaving] = useState(false), [error, setError] = useState('')
  const currentKey = useRef(key), alive = useRef(true)
  currentKey.current = key
  useEffect(() => {
    alive.current = true
    try { const value: unknown = JSON.parse(localStorage.getItem(key) || '{}'); setDraft(value && typeof value === 'object' && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).filter(([, answer]) => typeof answer === 'string')) : {}) } catch { setDraft({}) }
    setError(''); setSaving(false)
    return () => { alive.current = false }
  }, [key])
  const pending = branch.questions.filter(question => question.needs_answer === true)
  const missingState = branch.questions.some(question => question.needs_answer === undefined)
  if (!pending.length && !missingState) return null
  const update = (id: string, answer: string) => {
    const next = { ...draft, [id]: answer }; setDraft(next)
    try { localStorage.setItem(key, JSON.stringify(next)) } catch { /* Optional local draft storage. */ }
  }
  const save = async () => {
    if (disabled || saving || missingState || pending.some(question => question.required && !draft[question.id]?.trim())) return
    const answers = pending.filter(question => draft[question.id]?.trim()).map(question => ({ question_id: question.id, answer: draft[question.id] }))
    if (!answers.length) return
    if (answers.some(answer => new TextEncoder().encode(answer.answer).byteLength > 8000)) { setError(t('researchLab.branch.answerTooLong')); return }
    setSaving(true); setError('')
    try { await onSave(answers); try { localStorage.removeItem(key) } catch { /* Acknowledged save. */ } }
    catch (problem) { if (alive.current && currentKey.current === key) setError(problem instanceof Error ? problem.message : String(problem)) }
    finally { if (alive.current && currentKey.current === key) setSaving(false) }
  }
  return <section className="idea-lab-section">
    <h3>{t('researchLab.branch.answersFor', { title: branch.title })}</h3><p className="idea-lab-note">{t('researchLab.branch.answerScope')}</p>
    {missingState && <p role="status">{t('researchLab.branch.pending')}</p>}
    {pending.map(question => <div key={question.prompt_hash}>
      <label>{question.prompt}{question.required && <span className="idea-lab-note"> · {t('researchLab.branch.required')}</span>}<textarea disabled={disabled || saving} value={draft[question.id] ?? ''} onChange={event => update(question.id, event.target.value)} /></label>
      {question.answer && <details><summary>{t('researchLab.branch.previousAnswer')}</summary><p>{question.answer.answer}</p></details>}
    </div>)}
    {error && <p role="alert" className="idea-lab-error">{error}</p>}
    <button className="quiet-button" disabled={disabled || saving || missingState || !pending.some(question => draft[question.id]?.trim()) || pending.some(question => question.required && !draft[question.id]?.trim())} onClick={() => void save()}>{t(saving ? 'researchLab.branch.savingAnswers' : 'researchLab.branch.saveAnswers')}</button>
    <p className="idea-lab-note">{t('researchLab.branch.saveAnswersOnly')}</p>
  </section>
}
