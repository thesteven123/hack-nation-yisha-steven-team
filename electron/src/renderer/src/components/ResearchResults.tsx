import { t } from '@shared/i18n'
import type { LabArtifact, LabCampaign, LabPlan, LabRound } from '@shared/research-lab'
import { methodTitle, researchText } from './ResearchLanguage'

// Presentation only: the frozen observation and exported values stay exact.
const shown = (value: unknown): string => typeof value === 'number' ? Number(value.toPrecision(6)).toString() : Array.isArray(value) ? `[${value.map(shown).join(', ')}]` : typeof value === 'string' ? value : JSON.stringify(value)
export function ResearchLimits({ values, alreadyShown = [], maximum = 3 }: { values: string[]; alreadyShown?: string[]; maximum?: number }) {
  const unique = [...new Set(values)]
  const visible = unique.filter(value => !alreadyShown.includes(value)).slice(0, maximum)
  const remaining = unique.filter(value => !visible.includes(value))
  return <>{visible.length > 0 && <ul>{visible.map(value => <li key={value}>{researchText(value)}</li>)}</ul>}{remaining.length > 0 && <details><summary>{t('researchLab.moreLimits', { count: remaining.length })}</summary><ul>{remaining.map(value => <li key={value}>{researchText(value)}</li>)}</ul></details>}</>
}
export function ResearchPlan({ plan, disabled, feedback, onSelect, onArtifact, artifacts }: { plan: LabPlan; disabled: boolean; feedback: string; onSelect: (id: string, feedback: string) => void; onArtifact: (hash: string) => void; artifacts: Record<string, LabArtifact> }) {
  return <section className="idea-lab-section"><h2>{t('researchLab.frozenPlan', { round: plan.round })}</h2><p>{t(plan.selection_origin === 'human' ? 'researchLab.humanSelected' : 'researchLab.policySelected')}</p><p className="idea-lab-note">{t('researchLab.freezeNotice')}</p>
    {plan.candidates.some(action => action.research_mode === 'post_outcome_exploratory') && <p className="research-lab-warning">{t('researchLab.postOutcome')}</p>}
    <div className="idea-lab-cards">{plan.candidates.map(action => <article className={`idea-lab-card ${action.id === plan.selected_action_id ? 'selected' : ''}`} key={action.id}>
      <h3>{methodTitle(action.method, action.title)}</h3><p>{researchText(action.expected_learning)}</p><p className="idea-lab-note">{t('researchLab.actionCost')}</p>
      <details><summary>{t('researchLab.rules')}</summary><dl><dt>{t('researchLab.question')}</dt><dd>{action.question}</dd><dt>{t('researchLab.qc')}</dt><dd>{action.qc_rules.join('\n')}</dd><dt>{t('researchLab.limits')}</dt><dd>{action.interpretation_rules.join('\n')}</dd><dt>{t('researchLab.stopRules')}</dt><dd>{action.stop_rules.join('\n')}</dd></dl><p className="idea-lab-note">{t('researchLab.planProjection')}</p><button className="quiet-button" disabled={disabled} onClick={() => onArtifact(action.spec_artifact)}>{t('researchLab.artifact.spec_artifact')}</button>{artifacts[action.spec_artifact] && <pre className="idea-lab-source-text">{JSON.stringify(artifacts[action.spec_artifact].content, null, 2)}</pre>}</details>
      <button className="quiet-button" disabled={disabled} onClick={() => onSelect(action.id, feedback)}>{t(action.id === plan.selected_action_id && plan.selection_origin === 'human' ? 'researchLab.selected' : 'researchLab.choose')}</button>
    </article>)}</div><details><summary>{t('researchLab.planDetails')}</summary><p>{plan.selection_reason}</p><ul>{plan.comparison_criteria.map((c, i) => <li key={i}>{c}</li>)}</ul><pre className="idea-lab-source-text">{JSON.stringify(plan, null, 2)}</pre></details>
  </section>
}

export function ResearchComparison({ comparison, onArtifact, artifacts }: { comparison: NonNullable<LabCampaign['comparisons']>[number]; onArtifact: (hash: string) => void; artifacts: Record<string, LabArtifact> }) {
  const propositions = comparison.propositions
  const sameProposition = propositions?.length === 2 && propositions[0] === propositions[1]
  return <><h2>{t('researchLab.comparison')}</h2><p>{t(`researchLab.comparability.${comparison.comparability}`)}</p><p className="idea-lab-note">{t('researchLab.noPooledConfirmation')}</p>{sameProposition ? <p>{t('researchLab.sourceSupport')}: {researchText(comparison.source_support.before)} → {researchText(comparison.source_support.after)}</p> : <p>{t(propositions?.length === 2 ? 'researchLab.differentPropositions' : 'researchLab.unknownPropositions')}</p>}<details><summary>{t('researchLab.comparisonDetails')}</summary>{propositions?.map((proposition, index) => <div key={index}><strong>{t(index === 0 ? 'researchLab.beforeProposition' : 'researchLab.afterProposition')}</strong><p>{proposition}</p><p>{t('researchLab.sourceSupport')}: {researchText(index === 0 ? comparison.source_support.before : comparison.source_support.after)}</p></div>)}<p>{comparison.rule}</p><ul>{comparison.limitations.map((line, i) => <li key={i}>{researchText(line)}</li>)}</ul><ul>{comparison.unresolved.map((line, i) => <li key={i}>{researchText(line)}</li>)}</ul><button className="quiet-button" onClick={() => onArtifact(comparison.artifact)}>{t('researchLab.loadComparison')}</button>{artifacts[comparison.artifact] && <pre className="idea-lab-source-text">{JSON.stringify(artifacts[comparison.artifact].content, null, 2)}</pre>}</details></>
}

export function ResearchResult({ round, invalidated, onArtifact, artifacts }: { round: LabRound; invalidated: boolean; onArtifact: (hash: string) => void; artifacts: Record<string, LabArtifact> }) {
  const data = round.observation.data
  return <article className="research-lab-result">
    <h3>{t('researchLab.roundResult', { round: round.index })}</h3>
    {invalidated && <p role="status" className="research-lab-warning">{t('researchLab.needsRevalidation')}</p>}
    <p className={!round.qc.passed ? 'research-lab-warning' : ''}>{t(round.qc.passed ? 'researchLab.qcPassed' : 'researchLab.qcFailed')}</p>
    {'exact_match_found' in data && <><p><strong>{t(data.exact_match_found ? 'researchLab.quoteFound' : 'researchLab.quoteMissing')}</strong> · {t('researchLab.matches', { count: Number(data.match_count) })}</p><blockquote>{shown(data.quote)}</blockquote><p className="idea-lab-note">{t('researchLab.quoteLimit')}</p></>}
    {'mean_difference' in data && <dl className="research-lab-metrics">{['pairs', 'mean_difference', 'descriptive_interval', 'minimum_effect', 'sensitivity_range', 'sensitivity_mean', 'omitted_pair_index'].filter(key => key in data).map(key => <div key={key}><dt>{t(`researchLab.metric.${key}`)}</dt><dd>{shown(key === 'omitted_pair_index' ? Number(data[key]) + 1 : data[key])} {['pairs', 'omitted_pair_index'].includes(key) ? '' : shown(data.unit)}</dd></div>)}</dl>}
    {'criterion_result' in data && <p>{t(`researchLab.criterion.${data.criterion_result}`)}</p>}
    {!('exact_match_found' in data) && <p><strong>{t('researchLab.assessedClaim')}: </strong>{'mean_difference' in data ? t('researchLab.numericClaim', { threshold: shown(data.minimum_effect), unit: shown(data.unit) }) : round.analysis.claim}</p>}<div className="idea-lab-two"><p><strong>{t('researchLab.sourceSupport')}: </strong>{researchText(round.analysis.source_support)}</p><p><strong>{t('researchLab.inferenceValidity')}: </strong>{researchText(round.analysis.inference_validity)}</p></div>
    <ResearchLimits values={[...(Array.isArray(round.observation.coverage.missing_sections) ? round.observation.coverage.missing_sections.filter((value): value is string => typeof value === 'string') : []), ...round.analysis.limitations]} />
    {Array.isArray(data.contexts) && <details><summary>{t('researchLab.context')}</summary>{data.contexts.map((context, i) => <blockquote key={i}>{String((context as { text?: unknown }).text ?? '')}</blockquote>)}<p className="idea-lab-note">{t('researchLab.contextNotice')}</p></details>}
    <details><summary>{t('researchLab.evidenceDetails')}</summary><ul>{round.qc.checks.map((check, i) => <li key={i}>{check.passed ? '✓' : '×'} {check.detail}</li>)}</ul><pre className="idea-lab-source-text">{JSON.stringify(round.observation, null, 2)}</pre>
      {(['input_artifact', 'spec_artifact', 'dispatch_artifact', 'observation_artifact'] as const).map(key => round.run[key] && <div key={key}><button className="quiet-button" onClick={() => onArtifact(round.run[key]!)}>{t(`researchLab.artifact.${key}`)}</button>{artifacts[round.run[key]!] && <pre className="idea-lab-source-text">{JSON.stringify(artifacts[round.run[key]!].content, null, 2)}</pre>}</div>)}
    </details>
  </article>
}
