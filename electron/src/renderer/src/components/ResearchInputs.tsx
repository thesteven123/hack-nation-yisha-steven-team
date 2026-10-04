import { t } from '@shared/i18n'
import type { LabAdapterId, LabInputs, SourceInputs } from '@shared/research-lab'

export interface InputDraft { quote: string; sources: SourceInputs['sources']; baseline: string; treatment: string; unit: string; minimumEffect: string }
export const emptyInputs = (): InputDraft => ({ quote: '', sources: [{ id: 'source-1', title: '', uri: '', text: '', coverage: 'excerpt', missing_sections: [] }], baseline: '', treatment: '', unit: '', minimumEffect: '0' })
export function sharedCorrectionSource(original: SourceInputs['sources'], draft: InputDraft): string | null {
  // Shared invalidation is meaningful only for one explicitly edited frozen
  // source. Changed source sets and multiple edits must be corrected separately.
  if (original.length !== draft.sources.length || original.some(source => !draft.sources.some(next => next.id === source.id))) return null
  const changed = original.filter(source => {
    const next = draft.sources.find(next => next.id === source.id)!
    return (['title', 'uri', 'text', 'coverage', 'missing_sections'] as const).some(key => JSON.stringify(next[key]) !== JSON.stringify(source[key]))
  })
  if (changed.length !== 1) return null
  const source = changed[0], provenance = source.provenance
  return draft.sources.find(next => next.id === source.id)!.text !== source.text && provenance?.kind === 'idea_frozen_packet' && Number.isSafeInteger(provenance.decision_revision) && Number(provenance.decision_revision) > 0 ? source.id : null
}
export function fromInputs(inputs: LabInputs): InputDraft {
  return 'sources' in inputs ? { ...emptyInputs(), ...inputs } : { ...emptyInputs(), baseline: inputs.baseline.join(', '), treatment: inputs.treatment.join(', '), unit: inputs.unit, minimumEffect: String(inputs.minimum_effect) }
}
const bytes = (value: string) => new TextEncoder().encode(value).length
export function parseInputs(adapter: LabAdapterId, draft: InputDraft): LabInputs {
  if (adapter === 'source_evidence') {
    if (!draft.quote.trim() || bytes(draft.quote) > 16384 || draft.sources.length < 1 || draft.sources.length > 5 || draft.sources.some(s => !s.title.trim() || !s.text.trim()) || draft.sources.reduce((sum, s) => sum + bytes(s.text), 0) > 153600) throw new Error(t('researchLab.sourceValidation'))
    return { quote: draft.quote, sources: draft.sources.map(s => ({ ...s, uri: s.uri.trim() || `supplied:${s.id}` })) }
  }
  const numbers = (text: string) => text.trim().split(/[\s,;，；]+/).filter(Boolean).map(Number)
  const baseline = numbers(draft.baseline), treatment = numbers(draft.treatment), minimum = Number(draft.minimumEffect)
  if (baseline.length < 2 || baseline.length > 10000 || baseline.length !== treatment.length || [...baseline, ...treatment, minimum].some(n => !Number.isFinite(n) || Math.abs(n) > 1e12) || !draft.minimumEffect.trim() || minimum < 0 || !draft.unit.trim() || bytes(draft.unit) > 128) throw new Error(t('researchLab.numericValidation'))
  return { baseline, treatment, unit: draft.unit, minimum_effect: minimum }
}

export function ResearchInputs({ adapter, draft, onChange, disabled, frozenSources = false }: { adapter: LabAdapterId; draft: InputDraft; onChange: (next: InputDraft) => void; disabled: boolean; frozenSources?: boolean }) {
  if (adapter === 'paired_numeric') return <>
    <p className="idea-lab-note">{t('researchLab.numericNotice')}</p>
    <div className="idea-lab-two"><label>{t('researchLab.baseline')}<textarea disabled={disabled} value={draft.baseline} onChange={e => onChange({ ...draft, baseline: e.target.value })} /></label><label>{t('researchLab.treatment')}<textarea disabled={disabled} value={draft.treatment} onChange={e => onChange({ ...draft, treatment: e.target.value })} /></label></div>
    <div className="idea-lab-two"><label>{t('researchLab.unit')}<input disabled={disabled} value={draft.unit} onChange={e => onChange({ ...draft, unit: e.target.value })} /></label><label>{t('researchLab.minimumEffect')}<input type="number" min="0" disabled={disabled} value={draft.minimumEffect} onChange={e => onChange({ ...draft, minimumEffect: e.target.value })} /></label></div>
  </>
  const edit = (index: number, patch: Partial<SourceInputs['sources'][number]>) => onChange({ ...draft, sources: draft.sources.map((s, i) => {
    if (i !== index) return s
    const next = { ...s, ...patch }
    // The old version remains in its immutable input artifact. A human-edited
    // packet must not carry the original reader's trusted version receipt.
    if (Object.entries(patch).some(([key, value]) => s[key as keyof typeof s] !== value)) delete next.provenance
    return next
  }) })
  return <>
    <p className="idea-lab-note">{t('researchLab.sourceNotice')}</p>
    <label>{t('researchLab.quote')}<textarea disabled={disabled} value={draft.quote} onChange={e => onChange({ ...draft, quote: e.target.value })} /></label>
    {draft.sources.map((source, index) => <details className="idea-lab-source" key={source.id} open={frozenSources ? undefined : true}><summary>{source.title || `${t('researchLab.source')} ${index + 1}`}</summary>
      <label>{t('researchLab.sourceTitle')}<input disabled={disabled || frozenSources} value={source.title} onChange={e => edit(index, { title: e.target.value })} /></label>
      <label>{t('researchLab.sourceUri')}<input disabled={disabled || frozenSources} value={source.uri} onChange={e => edit(index, { uri: e.target.value })} /></label>
      <label>{t('researchLab.sourceText')}<textarea rows={5} disabled={disabled || frozenSources} value={source.text} onChange={e => edit(index, { text: e.target.value })} /></label>
      <label>{t('researchLab.coverage')}<select disabled={disabled || frozenSources} value={source.coverage} onChange={e => edit(index, { coverage: e.target.value as 'excerpt' | 'full_text' })}><option value="excerpt">{t('researchLab.excerpt')}</option><option value="full_text">{t('researchLab.fullText')}</option></select></label>
      <label>{t('researchLab.missingSections')}<input disabled={disabled || frozenSources} value={source.missing_sections.join('; ')} onChange={e => edit(index, { missing_sections: e.target.value.split(';').map(x => x.trim()).filter(Boolean) })} /></label>
      {!frozenSources && draft.sources.length > 1 && <button className="quiet-button" disabled={disabled} onClick={() => onChange({ ...draft, sources: draft.sources.filter((_, i) => i !== index) })}>{t('researchLab.removeSource')}</button>}
    </details>)}
    {!frozenSources && draft.sources.length < 5 && <button className="quiet-button" disabled={disabled} onClick={() => onChange({ ...draft, sources: [...draft.sources, { ...emptyInputs().sources[0], id: crypto.randomUUID() }] })}>{t('researchLab.addSource')}</button>}
  </>
}
