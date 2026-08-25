/* Cormorant brand mark: a cormorant mid-dive — wings swept back, plunging
   down-left like the downloads it captures. Single filled silhouette so it
   stays crisp from 16px tab icon to 56px login tile. Color via `currentColor`
   (sits on the accent-gradient pill; the favicon bakes its own colors). */
export function CormorantMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden className={className}>
      <path d="M20 4c-1.8.4-3.4 1-4.8 1.8L18 3l-4.4 4.2C9.5 9.8 6.6 13.4 5 17.6c-.4 1-.2 2 .5 2.6.8.7 2 .7 2.9.1 3.6-2.5 6.4-5.9 8-9.9L18 7l2-3z" />
    </svg>
  );
}
