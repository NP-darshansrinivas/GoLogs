import type { ConfirmationRequiredFrame } from "../api/types";

/**
 * The one interaction GoLogs is built around: nothing that mutates case
 * state or costs real CPU/RAM ever runs without an analyst explicitly
 * saying so (PRD FR-8, the permission_gate's REQUIRES_CONFIRMATION class).
 * Deliberately the most visually weighted moment in the UI — amber is
 * reserved for this single purpose everywhere else in the design system,
 * so its appearance here is unmistakable rather than one alert among many.
 */
export function ConfirmationModal({
  frame,
  onRespond,
}: {
  frame: ConfirmationRequiredFrame;
  onRespond: (approved: boolean) => void;
}): JSX.Element {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirmation-title"
    >
      <div className="mx-4 w-full max-w-md rounded-xl border border-gate/40 bg-bg-elevated shadow-gate">
        <div className="flex items-center gap-2 border-b border-gate/20 px-5 py-4">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-gate-soft text-gate">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 16 16" aria-hidden>
              <path
                d="M8 1 1 4v4c0 4 3 6.5 7 7 4-.5 7-3 7-7V4L8 1Z"
                stroke="currentColor"
                strokeWidth="1.3"
              />
              <path d="M8 6v3M8 11h.01" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
            </svg>
          </span>
          <h2 id="confirmation-title" className="font-medium text-gate">
            Confirmation required
          </h2>
        </div>

        <div className="space-y-3 px-5 py-4">
          <p className="text-sm text-text-secondary">
            GoLogs wants to run <span className="font-mono text-text-primary">{frame.tool_name}</span>.
            This{" "}
            {frame.tool_name === "report.append_finding"
              ? "will change this case's record"
              : "may take up to two minutes and use significant CPU/RAM"}
            .
          </p>
          <div className="rounded-lg bg-bg-inset px-3 py-2.5 text-sm text-text-secondary">
            {frame.reason}
          </div>
          <pre className="scrollbar-thin overflow-x-auto rounded bg-bg-inset px-3 py-2 font-mono text-xs text-text-muted">
            {JSON.stringify(frame.arguments, null, 2)}
          </pre>
        </div>

        <div className="flex gap-2 border-t border-border px-5 py-4">
          <button
            type="button"
            onClick={() => onRespond(false)}
            className="flex-1 rounded-lg border border-border px-4 py-2 text-sm font-medium text-text-secondary transition hover:border-border-strong hover:text-text-primary"
          >
            Decline
          </button>
          <button
            type="button"
            onClick={() => onRespond(true)}
            className="flex-1 rounded-lg bg-gate px-4 py-2 text-sm font-medium text-bg-primary transition hover:bg-gate-strong"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
