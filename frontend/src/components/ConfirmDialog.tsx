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
      className="w-[min(26rem,calc(100vw-2rem))] rounded-xl border border-zinc-800 bg-zinc-900 p-0 text-zinc-100 shadow-2xl backdrop:bg-black/70"
    >
      <div className="p-5">
        <div className="flex gap-3">
          <span
            aria-hidden
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-red-950 text-lg text-red-400"
          >
            !
          </span>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">{title}</h2>
            <div className="mt-1 text-sm break-words text-zinc-400">{message}</div>
          </div>
        </div>
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            onClick={onCancel}
            className="rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
          >
            Cancel
          </button>
          <button
            autoFocus
            onClick={onConfirm}
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
