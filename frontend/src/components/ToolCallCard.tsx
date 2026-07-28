import { useState } from "react";
import type { RiskClass, ToolCallFrame, ToolResultFrame } from "../api/types";

const RISK_STYLES: Record<RiskClass, { label: string; dot: string; text: string }> = {
  AUTO_APPROVE: { label: "Auto-approved", dot: "bg-accent", text: "text-accent" },
  REQUIRES_CONFIRMATION: { label: "Confirmed by analyst", dot: "bg-gate", text: "text-gate" },
  DENY: { label: "Denied", dot: "bg-danger", text: "text-danger" },
};

function JsonBlock({ data }: { data: unknown }): JSX.Element {
  return (
    <pre className="scrollbar-thin overflow-x-auto rounded bg-bg-inset px-3 py-2 font-mono text-xs text-text-secondary">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

/**
 * Renders one tool call end-to-end: arguments, risk classification, and
 * (once available) its result — including an explicit flag when the
 * injection scanner found something suspicious in the returned evidence
 * (PRD §11.4). Every field the model saw is visible here; nothing about a
 * tool call happens off-screen (FR-6).
 */
export function ToolCallCard({
  call,
  result,
}: {
  call: ToolCallFrame;
  result: ToolResultFrame | null;
}): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const risk = RISK_STYLES[call.risk_class];
  const isError = result && "error" in result.result;

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    const dataToCopy = result ? result.result : call.arguments;
    const jsonString = typeof dataToCopy === "string" ? dataToCopy : JSON.stringify(dataToCopy, null, 2);
    navigator.clipboard.writeText(jsonString);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="my-2 max-w-xl rounded-lg border border-border bg-bg-surface">
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((e) => !e)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((prev) => !prev);
          }
        }}
        className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left select-none"
      >
        <span className="font-mono text-xs uppercase tracking-wide text-text-muted">
          Exhibit
        </span>
        <span className="font-mono text-sm font-medium text-text-primary">{call.tool_name}</span>
        <span className={`ml-auto flex items-center gap-1.5 text-xs ${risk.text}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${risk.dot}`} aria-hidden />
          {risk.label}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-medium transition-colors ${
            copied
              ? "border-success/40 bg-success-soft text-success"
              : "border-border bg-bg-inset text-text-secondary hover:border-border-strong hover:text-text-primary"
          }`}
          title="Copy raw JSON result"
        >
          {copied ? "Copied!" : "Copy JSON"}
        </button>
        {result && (
          <span
            className={`rounded px-1.5 py-0.5 text-xs font-medium ${
              isError ? "bg-danger-soft text-danger" : "bg-success-soft text-success"
            }`}
          >
            {isError ? "error" : "done"}
          </span>
        )}
        {!result && call.risk_class !== "DENY" && (
          <span className="text-xs text-text-muted">running…</span>
        )}
        <svg
          className={`h-3.5 w-3.5 text-text-muted transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 12 12"
          aria-hidden
        >
          <path d="M2.5 4.5 6 8l3.5-3.5" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </div>

      {result?.injection_flagged && (
        <div className="mx-3 mb-2 rounded border border-danger/40 bg-danger-soft px-2.5 py-1.5 text-xs text-danger">
          This evidence contained text formatted to look like an instruction. GoLogs treated it
          as data, not a command — flagged fields are marked in the raw result below.
        </div>
      )}

      {expanded && (
        <div className="space-y-2 border-t border-border px-3 py-2.5">
          <div>
            <div className="mb-1 text-xs font-medium text-text-secondary">Arguments</div>
            <JsonBlock data={call.arguments} />
          </div>
          {result && (
            <div>
              <div className="mb-1 text-xs font-medium text-text-secondary">Result</div>
              <JsonBlock data={result.result} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
