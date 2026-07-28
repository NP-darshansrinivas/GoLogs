import { useEffect, useState } from "react";
import { getEvents } from "../api/client";
import type { NormalizedEventSummary } from "../api/types";

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * A scrollable, filterable read-out of the case's normalized Windows Event
 * Log records (`core.normalizer`'s output, queried via `evtx.query_events`
 * on the backend, here fetched over the plain REST endpoint since this
 * view doesn't need the chat's live streaming).
 */
export function TimelineView({ caseId }: { caseId: string }): JSX.Element {
  const [events, setEvents] = useState<NormalizedEventSummary[]>([]);
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(true);
  const [expandedUid, setExpandedUid] = useState<string | null>(null);

  useEffect(() => {
    if (!caseId || typeof caseId !== "string" || !caseId.trim()) {
      setEvents([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    getEvents(caseId, { keyword: keyword || undefined, limit: 100 })
      .then((data) => {
        if (!cancelled) setEvents(data.events);
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [caseId, keyword]);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-4 py-3">
        <h2 className="mb-2 text-sm font-medium text-text-primary">Timeline</h2>
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="Filter events…"
          className="w-full rounded-lg border border-border bg-bg-surface px-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent"
        />
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto">
        {loading && <div className="px-4 py-4 text-sm text-text-muted">Loading…</div>}
        {!loading && events.length === 0 && (
          <div className="px-4 py-4 text-sm text-text-muted">No events match.</div>
        )}
        {events.map((event) => {
          const isExpanded = expandedUid === event.uid;
          return (
            <button
              type="button"
              key={event.uid}
              onClick={() => setExpandedUid(isExpanded ? null : event.uid)}
              className="block w-full border-b border-border px-4 py-2.5 text-left transition hover:bg-bg-surface"
            >
              <div className="flex items-center gap-2 font-mono text-xs">
                <span className="rounded bg-bg-inset px-1.5 py-0.5 text-text-secondary">
                  {event.channel}
                </span>
                <span className="font-medium text-text-primary">{event.event_id}</span>
                <span className="ml-auto text-text-muted">{formatTime(event.time_created)}</span>
              </div>
              {isExpanded && (
                <div className="mt-2 space-y-1 font-mono text-xs text-text-secondary">
                  <div>Computer: {event.computer ?? "—"}</div>
                  <div>User SID: {event.user_sid ?? "—"}</div>
                  <div>UID: {event.uid}</div>
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
