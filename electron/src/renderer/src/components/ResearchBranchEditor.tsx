import { useState } from 'react'
import { t } from '@shared/i18n'
import type { LabCampaign } from '@shared/research-lab'
import type { BranchProposal } from '@shared/research-branches'
import { methodTitle } from './ResearchLanguage'

export const branchSources = (campaign: LabCampaign): { id: string; title: string }[] => Array.isArray(campaign.input_summary.sources) ? campaign.input_summary.sources.filter((source): source is { id: string; title: string } => !!source && typeof source.id === 'string' && typeof source.title === 'string') : []
const bytes = (text: string) => new TextEncoder().encode(text).length
export function validateBranches(values: BranchProposal[], campaign: LabCampaign): BranchProposal[] {
  const sources = new Set(branchSources(campaign).map(source => source.id)), ids = new Set(values.map(value => value.id))
  if (!values.length || values.length > 3 || ids.size !== values.length || values.reduce((n, value) => n + value.questions.length, 0) > 3) throw new Error(t('researchLab.branch.invalidDraft'))
  for (const value of values) {
    if (!value.title.trim() || bytes(value.title) > 200 || !value.question.trim() || bytes(value.question) > 4000 || !value.success_criterion.trim() || bytes(value.success_criterion) > 4000 || !value.methods.length || value.methods.some(method => !campaign.brief.authorized_actions.includes(method))) throw new Error(t('researchLab.branch.invalidDraft'))
    if (campaign.adapter.id === 'source_evidence' && (!value.source_ids?.length || value.source_ids.some(id => !sources.has(id)))) throw new Error(t('researchLab.branch.invalidDraft'))
    if (value.depends_on.some(item => item.branch_id === value.id || !ids.has(item.branch_id)) || value.questions.some(question => !question.prompt.trim() || bytes(question.prompt) > 4000)) throw new Error(t('researchLab.branch.invalidDraft'))
  }
  const visit = (id: string, seen: Set<string>) => { if (seen.has(id)) throw new Error(t('researchLab.branch.cycle')); for (const dependency of values.find(value => value.id === id)!.depends_on) visit(dependency.branch_id, new Set([...seen, id])) }
  values.forEach(value => visit(value.id, new Set()))
  return values
}

export function ResearchBranchEditor({ campaign, storageKey, disabled, onEnable }: { campaign: LabCampaign; storageKey: string; disabled: boolean; onEnable: (branches: BranchProposal[]) => Promise<void> }) {
  const key = `${storageKey}:branch-proposals:${campaign.id}:${campaign.brief.revision}:${campaign.input_artifact}`
  const sources = branchSources(campaign)
  const blank = (): BranchProposal => ({ id: `branch_${crypto.randomUUID().replaceAll('-', '')}`, title: '', question: '', success_criterion: '', methods: [...campaign.brief.authorized_actions], ...(campaign.adapter.id === 'source_evidence' ? { source_ids: sources.map(source => source.id) } : {}), depends_on: [], questions: [] })
  const [values, setValues] = useState<BranchProposal[]>(() => { try { const saved = JSON.parse(localStorage.getItem(key) || 'null'); if (Array.isArray(saved) && saved.length > 0 && saved.length <= 3) return saved } catch { /* optional draft */ } return [blank()] })
  const [error, setError] = useState('')
  const update = (next: BranchProposal[]) => { setValues(next); try { localStorage.setItem(key, JSON.stringify(next)) } catch { /* optional draft */ } }
  const edit = (index: number, patch: Partial<BranchProposal>) => update(values.map((value, i) => i === index ? { ...value, ...patch } : value))
  const totalQuestions = values.reduce((n, value) => n + value.questions.length, 0)
  const save = async () => { setError(''); try { await onEnable(validateBranches(values, campaign)); localStorage.removeItem(key) } catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)) } }
  return <details className="idea-lab-section"><summary>{t('researchLab.branch.enableTitle')}</summary>
    <p>{t('researchLab.branch.enableNotice')}</p>
    {(campaign.total_rounds ?? campaign.rounds.length) > 0 && <p className="research-lab-warning">{t('researchLab.branch.postOutcome')}</p>}
    {values.map((value, index) => <fieldset key={value.id} disabled={disabled}><legend>{t('researchLab.branch.number', { number: index + 1 })}</legend>
      <label>{t('researchLab.branch.title')}<input value={value.title} onChange={event => edit(index, { title: event.target.value })} /></label>
      <label>{t('researchLab.branch.question')}<textarea value={value.question} onChange={event => edit(index, { question: event.target.value })} /></label>
      <label>{t('researchLab.branch.criterion')}<textarea value={value.success_criterion} onChange={event => edit(index, { success_criterion: event.target.value })} /></label>
      <details><summary>{t('researchLab.branch.scopeOptions')}</summary>
        <p>{t('researchLab.branch.methods')}</p>{campaign.brief.authorized_actions.map(method => <label className="idea-lab-pick" key={method}><input type="checkbox" checked={value.methods.includes(method)} onChange={event => edit(index, { methods: event.target.checked ? [...value.methods, method] : value.methods.filter(item => item !== method) })} />{methodTitle(method, method)}</label>)}
        {campaign.adapter.id === 'source_evidence' && <><p>{t('researchLab.branch.sources')}</p>{sources.map(source => <label className="idea-lab-pick" key={source.id}><input type="checkbox" checked={value.source_ids?.includes(source.id) ?? false} onChange={event => edit(index, { source_ids: event.target.checked ? [...(value.source_ids ?? []), source.id] : value.source_ids?.filter(id => id !== source.id) })} />{source.title}</label>)}</>}
        {campaign.adapter.id === 'paired_numeric' && <p className="idea-lab-note">{t('researchLab.branch.sameData')}</p>}
        <p>{t('researchLab.branch.depends')}</p>{values.filter(other => other.id !== value.id).map(other => <div key={other.id}><label className="idea-lab-pick"><input type="checkbox" checked={value.depends_on.some(item => item.branch_id === other.id)} onChange={event => edit(index, { depends_on: event.target.checked ? [...value.depends_on, { branch_id: other.id, require: 'qc_passed' }] : value.depends_on.filter(item => item.branch_id !== other.id) })} />{other.title || t('researchLab.branch.number', { number: values.indexOf(other) + 1 })}</label>{value.depends_on.some(item => item.branch_id === other.id) && <label>{t('researchLab.branch.dependencyCondition')}<select value={value.depends_on.find(item => item.branch_id === other.id)!.require} onChange={event => edit(index, { depends_on: value.depends_on.map(item => item.branch_id === other.id ? { ...item, require: event.target.value as 'completed' | 'qc_passed' } : item) })}><option value="qc_passed">{t('researchLab.branch.requireQc')}</option><option value="completed">{t('researchLab.branch.requireCompleted')}</option></select></label>}</div>)}
        <p>{t('researchLab.branch.questionsNotice')}</p>{value.questions.map((question, q) => <div key={question.id}><label>{t('researchLab.branch.prompt')}<textarea value={question.prompt} onChange={event => edit(index, { questions: value.questions.map((item, n) => n === q ? { ...item, prompt: event.target.value } : item) })} /></label><label className="idea-lab-pick"><input type="checkbox" checked={question.required} onChange={event => edit(index, { questions: value.questions.map((item, n) => n === q ? { ...item, required: event.target.checked } : item) })} />{t('researchLab.branch.required')}</label><button className="quiet-button" onClick={() => edit(index, { questions: value.questions.filter((_, n) => n !== q) })}>{t('researchLab.branch.removeQuestion')}</button></div>)}
        <button className="quiet-button" disabled={disabled || totalQuestions >= 3} onClick={() => edit(index, { questions: [...value.questions, { id: `question_${crypto.randomUUID().replaceAll('-', '')}`, prompt: '', required: true }] })}>{t('researchLab.branch.addQuestion')}</button>
      </details>
      {values.length > 1 && <button className="quiet-button" onClick={() => update(values.filter((_, i) => i !== index).map(other => ({ ...other, depends_on: other.depends_on.filter(item => item.branch_id !== value.id) })))}>{t('researchLab.branch.remove')}</button>}
    </fieldset>)}
    <div className="idea-lab-actions"><button className="quiet-button" disabled={disabled || values.length >= 3} onClick={() => update([...values, blank()])}>{t('researchLab.branch.add')}</button><button className="primary-button" disabled={disabled} onClick={() => void save()}>{t('researchLab.branch.enable')}</button></div>
    {error && <p className="idea-lab-error" role="alert">{error}</p>}
  </details>
}
