import { useEffect, useRef } from "react";

interface Props {
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Native <dialog>: Escape-to-close, focus trap and inertness come from the
 *  platform, so this file only styles it. Mount it conditionally — it opens
 *  itself. */
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = "Delete",
  onConfirm,
  onCancel,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  return (
    <dialog
      ref={ref}
      onClose={onCancel}
      // A click landing on the dialog box itself is a click on the backdrop;
      // inner content stops at its own subtree.
      onClick={(e) => e.target === ref.current && onCancel()}
      className="w-[min(26rem,calc(100vw-2rem))] rounded-2xl border border-line bg-surface-2 p-0 text-ink shadow-2xl shadow-black/50 backdrop:bg-black/70"
    >
      <div className="p-5 sm:p-6">
        <div className="flex gap-3">
          <span
            aria-hidden
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-bad/40 bg-bad/15 text-base text-bad"
          >
            !
          </span>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">{title}</h2>
            <div className="mt-1 text-sm break-words text-ink-dim">{message}</div>
          </div>
        </div>
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            onClick={onCancel}
            className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-4 py-2.5 text-sm text-ink-dim"
          >
            Cancel
          </button>
          <button
            autoFocus
            onClick={onConfirm}
            className="min-h-[44px] cursor-pointer rounded-xl bg-bad px-4 py-2.5 text-sm font-semibold text-white transition-[filter,transform] hover:brightness-110 active:scale-[0.98]"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
