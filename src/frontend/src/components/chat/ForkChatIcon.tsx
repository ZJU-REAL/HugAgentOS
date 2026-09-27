/** Branch from the current path, matching the two conversation entry points. */
export function ForkChatIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 12h7c4 0 5-3 9-8M14 4h6v6M11 12c4 0 5 3 9 8M14 20h6v-6" />
    </svg>
  );
}
