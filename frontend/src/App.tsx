import { useState } from "react";
import { createCase, getCase } from "./api/client";
import { useChat } from "./hooks/useChat";
import { ChatPanel } from "./components/ChatPanel";
import { TimelineView } from "./components/TimelineView";
import { ConfirmationModal } from "./components/ConfirmationModal";
import type { CaseSummary } from "./api/types";

function CaseIntake({ onReady }: { onReady: (caseId: string) => void }): JSX.Element {
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [existingId, setExistingId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCreate(): Promise<void> {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const { case_id } = await createCase(name, files);
      onReady(case_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create case");
    } finally {
      setBusy(false);
    }
  }

  async function handleOpenExisting(): Promise<void> {
    const trimmedId = existingId.trim();
    if (!trimmedId) return;
    setBusy(true);
    setError(null);
    try {
      await getCase(trimmedId);
      onReady(trimmedId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Case not found");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <h1 className="font-mono text-2xl font-semibold tracking-tight text-text-primary">
            GoLogs
          </h1>
          <p className="mt-1 text-sm text-text-muted">Interrogate Your Evidence</p>
        </div>

        <div className="rounded-xl border border-border bg-bg-surface p-5">
          <h2 className="mb-3 text-sm font-medium text-text-primary">New case</h2>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Case name"
            className="mb-2 w-full rounded-lg border border-border bg-bg-inset px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent"
          />
          <input
            type="file"
            multiple
            accept=".evtx,.raw,.mem,.dmp"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            className="mb-3 w-full text-xs text-text-secondary file:mr-3 file:rounded-lg file:border-0 file:bg-bg-inset file:px-3 file:py-1.5 file:text-xs file:text-text-secondary"
          />
          <button
            type="button"
            onClick={handleCreate}
            disabled={busy || !name.trim()}
            className="w-full rounded-lg bg-accent px-4 py-2 text-sm font-medium text-bg-primary transition disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Creating…" : "Create case"}
          </button>
        </div>

        <div className="my-4 flex items-center gap-3 text-xs text-text-muted">
          <div className="h-px flex-1 bg-border" />
          or
          <div className="h-px flex-1 bg-border" />
        </div>

        <div className="flex gap-2">
          <input
            value={existingId}
            onChange={(e) => setExistingId(e.target.value)}
            placeholder="Existing case ID"
            className="flex-1 rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent"
          />
          <button
            type="button"
            onClick={handleOpenExisting}
            disabled={busy || !existingId.trim()}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-text-secondary transition hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            Open
          </button>
        </div>

        {error && <p className="mt-3 text-xs text-danger">{error}</p>}
      </div>
    </div>
  );
}

function Workspace({
  caseId,
  caseSummary,
  onOpenIntake,
}: {
  caseId: string;
  caseSummary: CaseSummary;
  onOpenIntake: () => void;
}): JSX.Element {
  const { transcript, status, pendingConfirmation, sendMessage, respondToConfirmation } =
    useChat(caseId);

  return (
    <div className="flex h-screen w-full flex-col min-h-0 overflow-hidden bg-bg-primary">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2.5 shrink-0">
        <span className="font-mono text-sm font-semibold text-text-primary">GoLogs</span>
        <span className="text-xs text-text-muted">/</span>
        <span className="text-sm text-text-secondary">{caseSummary.name}</span>
        <span className="ml-auto rounded bg-bg-inset px-2 py-0.5 font-mono text-xs text-text-muted">
          {caseSummary.event_count} events
        </span>
        <button
          type="button"
          onClick={onOpenIntake}
          className="rounded border border-border bg-bg-surface px-2.5 py-1 text-xs font-medium text-text-secondary hover:text-text-primary transition-colors"
        >
          Switch Case
        </button>
      </header>

      <div className="flex flex-1 min-h-0 w-full flex-col lg:grid lg:grid-cols-[2fr_1fr] overflow-hidden">
        <div className="flex flex-col flex-1 min-h-0 min-w-0 h-full border-b border-border lg:border-b-0 lg:border-r">
          <ChatPanel transcript={transcript} status={status} onSend={sendMessage} caseId={caseId} />
        </div>
        <div className="flex flex-col flex-1 min-h-0 min-w-0 h-full">
          <TimelineView caseId={caseId} />
        </div>
      </div>

      {pendingConfirmation && (
        <ConfirmationModal frame={pendingConfirmation} onRespond={respondToConfirmation} />
      )}
    </div>
  );
}

export default function App(): JSX.Element {
  const [caseId, setCaseId] = useState<string | null>(null);
  const [caseSummary, setCaseSummary] = useState<CaseSummary | null>(null);
  const [showIntakeModal, setShowIntakeModal] = useState<boolean>(false);

  async function handleReady(id: string): Promise<void> {
    const summary = await getCase(id);
    setCaseSummary(summary);
    setCaseId(id);
    setShowIntakeModal(false);
  }

  if (!caseId || !caseSummary) {
    return <CaseIntake onReady={handleReady} />;
  }

  return (
    <div className="relative h-screen w-full min-h-0 flex flex-col overflow-hidden bg-bg-primary">
      <Workspace
        caseId={caseId}
        caseSummary={caseSummary}
        onOpenIntake={() => setShowIntakeModal(true)}
      />

      {showIntakeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg-primary/80 backdrop-blur-sm p-4">
          <div className="relative w-full max-w-md">
            <button
              type="button"
              onClick={() => setShowIntakeModal(false)}
              className="absolute -top-3 -right-3 rounded-full bg-bg-elevated p-1.5 text-text-muted hover:text-text-primary"
              title="Close"
            >
              ✕
            </button>
            <CaseIntake onReady={handleReady} />
          </div>
        </div>
      )}
    </div>
  );
}
