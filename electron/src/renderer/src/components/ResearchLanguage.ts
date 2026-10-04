import { t } from '@shared/i18n'

// Translate only known deterministic adapter statements. Unknown server text is
// retained verbatim rather than guessed or replaced with a stronger claim.
const statements: Record<string, string> = {
  'Text extraction only; figures, image tables, equation layout and scanned text were not visually inspected.': 'pdfTextOnly',
  'Only the declared text spans entered this task; omitted sections require a targeted follow-up read.': 'declaredSpansOnly',
  'Only the exact frozen Idea text packet is available; original completeness is unverified.': 'frozenPacketOnly',
  'Original completeness unverified': 'originalUnknown',
  'HTML text only; figures, interactive elements, linked supplements and inaccessible sections were not read.': 'htmlTextOnly',
  'Plain text resource; completeness and linked materials were not verified.': 'plainTextOnly',
  'Completed computation failed support-eligibility QC; no support claim accepted': 'numericQcReview',
  'Supply at least four valid pairs': 'fourPairs',
  'The selected sensitivity analysis is complete; independent evidence is still required for confirmation': 'sensitivityReviewed',
  'The descriptive lower bound exceeds the threshold; test whether each leave-one-pair-out mean retains that direction': 'leaveOneOutNext',
  'The threshold is not clearly exceeded; inspect dependence on the most extreme pair before revising the hypothesis': 'sensitivityNext',
  'Quality checks failed; the result is excluded from support': 'qcReview',
  'Input or method repair required': 'repairRequired',
  'Quote and bounded surrounding context have been inspected': 'contextReviewed',
  'The quote was found; inspect surrounding qualifications before any interpretation': 'inspectContextNext',
  'No match in this supplied source; check the next supplied source if available': 'inspectOtherNext',
  'Scientific meaning and inference remain unassessed': 'scientificUnknown',
  'Missing accessible text does not establish absence': 'missingNotAbsence',
  'The resource ceiling was reached; remaining uncertainty is not resolved': 'budgetStopped',
  'Checks supplied text only; does not search the network': 'suppliedOnly',
  'Quote occurrence does not validate a scientific claim': 'occurrenceOnly',
  'Context keywords are inspection aids, not semantic or inference judgments': 'contextKeywords',
  'Coverage gaps remain unresolved': 'coverageUnresolved',
  'Descriptive analysis of supplied pairs; no causal identification': 'descriptiveOnly',
  'Normal approximation is not a calibrated interval without sampling assumptions': 'intervalLimit',
  'Sensitivity uses the same fixed dataset, not independent replication': 'sameDataset',
  'No hidden test set or population representativeness is assumed': 'sampleLimit',
  'Locate exact supplied evidence and expose missing context; assess no scientific implication': 'sourceLearning',
  'Compare a descriptive effect against the frozen minimum and expose dependence on observations': 'numericLearning',
  'supported': 'supportYes', 'cannot_determine': 'supportUnknown', 'weakened': 'supportWeakened', 'contradicted': 'supportContradicted',
}
export function researchText(value: string): string { return statements[value] ? t(`researchLab.text.${statements[value]}`) : value }
export function methodTitle(method: string, original: string): string {
  if (method === 'exact_quote') return `${t('researchLab.method.exact_quote')}${original.startsWith('Check exact quote: ') ? `: ${original.slice(19)}` : ''}`
  if (method === 'source_context') return `${t('researchLab.method.source_context')}${original.startsWith('Inspect surrounding context: ') ? `: ${original.slice(29)}` : ''}`
  return ['paired_summary', 'leave_one_out', 'extreme_sensitivity'].includes(method) ? t(`researchLab.method.${method}`) : original
}
