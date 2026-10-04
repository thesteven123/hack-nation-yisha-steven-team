import * as Dialog from '@radix-ui/react-dialog'
import { t } from '@shared/i18n'

export function ResearchTrashDialog({ goal, busy, error, onCancel, onConfirm }: {
  goal: string | null; busy: boolean; error: string; onCancel: () => void; onConfirm: () => void
}) {
  return <Dialog.Root open={goal !== null} onOpenChange={open => { if (!open && !busy) onCancel() }}>
    <Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="form-dialog research-trash-dialog"
      onEscapeKeyDown={event => { if (busy) event.preventDefault() }}
      onInteractOutside={event => { if (busy) event.preventDefault() }}>
      <header><Dialog.Title>{t('researchTrash.title')}</Dialog.Title></header>
      <div className="form-dialog-body"><Dialog.Description>{t('researchTrash.description')}</Dialog.Description>
        <p className="research-trash-goal">{goal}</p>
        {error && <p role="alert">{error}</p>}
        <div className="idea-lab-actions"><Dialog.Close asChild><button className="quiet-button" disabled={busy}>{t('researchTrash.cancel')}</button></Dialog.Close>
          <button className="primary-button" disabled={busy} onClick={onConfirm}>{t(busy ? 'researchTrash.deleting' : 'researchTrash.confirm')}</button></div>
      </div>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}
