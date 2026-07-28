import { useEffect, useRef, useState } from "react";
import type { ConnectionStatus } from "../hooks/useChat";
import type { TranscriptItem } from "../api/types";
import { ToolCallCard } from "./ToolCallCard";
import { ExportReportButton } from './ExportReportButton';

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting…",
  open: "Connected",
  closed: "Disconnected",
  error: "Connection error",
};

function ChatMessage({ content, isUser }: { content: unknown; isUser: boolean }): JSX.Element {
  const displayContent =
    typeof content === "string"
      ? content
      : typeof content === "object" && content !== null
      ? JSON.stringify(content)
      : String(content ?? "");

  const handleDownload = () => {
    if (!displayContent) return;
    const blob = new Blob([displayContent], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "gologs_report.md";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className={`group relative flex items-start gap-1.5 ${isUser ? "flex-row-reverse justify-start" : "justify-start"}`}>
      <div
        className={`max-w-xl rounded-lg px-3.5 py-2.5 text-sm leading-relaxed ${
          isUser
            ? "bg-accent-soft text-text-primary"
            : "border border-border bg-bg-surface text-text-primary"
        }`}
      >
        {displayContent || <span className="text-text-muted">…</span>}
      </div>
      {displayContent && (
        <button
          type="button"
          onClick={handleDownload}
          title="Download message as markdown"
          aria-label="Download message"
          className="mt-1.5 rounded p-1 text-text-muted transition-colors hover:bg-bg-elevated hover:text-text-primary"
        >
          <svg
            className="h-3.5 w-3.5"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"
            />
          </svg>
        </button>
      )}
    </div>
  );
}

function TranscriptRow({ item }: { item: TranscriptItem }): JSX.Element | null {
  if (item.kind === "message") {
    return <ChatMessage content={item.content} isUser={item.role === "user"} />;
  }
  if (item.kind === "tool_call") {
    return <ToolCallCard call={item.call} result={item.result} />;
  }
  if (item.kind === "confirmation") {
    return (
      <div className="my-1 flex items-center gap-2 text-xs text-text-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-gate" aria-hidden />
        Confirmation for <span className="font-mono">{item.frame.tool_name}</span> resolved
      </div>
    );
  }
  return null;
}

export function ChatPanel({
  transcript,
  status,
  onSend,
  caseId,
}: {
  transcript: TranscriptItem[];
  status: ConnectionStatus;
  onSend: (content: string) => void;
  caseId: string;
}): JSX.Element {
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [transcript]);

  function submit(): void {
    if (!draft.trim() || status !== "open") return;
    onSend(draft);
    setDraft("");
  }

  return (
    <div className="flex flex-col h-full min-h-0 w-full flex-1 bg-bg-primary">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3 shrink-0">
        <h2 className="text-sm font-medium text-text-primary">Investigation</h2>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-text-muted">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              status === "open" ? "bg-success" : status === "error" ? "bg-danger" : "bg-text-muted"
            }`}
            aria-hidden
          />
          {STATUS_LABEL[status]}
        </span>
        <div className="ml-4">
          <ExportReportButton caseId={caseId} />
        </div>
      </div>

      <div ref={scrollRef} className="scrollbar-thin flex-1 min-h-0 space-y-3 overflow-y-auto px-4 py-4">
        {transcript.length === 0 && (
          <div className="mt-8 text-center text-sm text-text-muted">
            Ask GoLogs about this case's evidence to begin.
          </div>
        )}
        {transcript.map((item) => (
          <TranscriptRow key={item.id} item={item} />
        ))}
      </div>

      <div className="border-t border-border px-4 py-3 shrink-0 bg-bg-primary">
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Any suspicious logons in the Security log?"
            rows={1}
            className="scrollbar-thin max-h-32 flex-1 resize-none rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent"
          />
          <button
            type="button"
            onClick={submit}
            disabled={status !== "open" || !draft.trim()}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-bg-primary transition disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
