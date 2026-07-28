import type { AuditEntry, CaseSummary, ChatHistoryMessage, NormalizedEventSummary } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`${init?.method ?? "GET"} ${path} failed: ${response.status} ${body}`);
  }
  return (await response.json()) as T;
}

export async function createCase(name: string, files: File[]): Promise<{ case_id: string }> {
  const form = new FormData();
  form.append("name", name);
  for (const file of files) form.append("files", file);
  return request("/api/cases", { method: "POST", body: form });
}

export async function getCase(caseId: string): Promise<CaseSummary> {
  return request(`/api/cases/${caseId}`);
}

export async function getEvents(
  caseId: string,
  params: { channel?: string; event_id?: number; keyword?: string; limit?: number } = {},
): Promise<{ events: NormalizedEventSummary[]; count: number }> {
  const query = new URLSearchParams();
  if (params.channel) query.set("channel", params.channel);
  if (params.event_id !== undefined) query.set("event_id", String(params.event_id));
  if (params.keyword) query.set("keyword", params.keyword);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  const qs = query.toString();
  return request(`/api/cases/${caseId}/events${qs ? `?${qs}` : ""}`);
}

export async function getAuditTrail(caseId: string): Promise<{ entries: AuditEntry[] }> {
  return request(`/api/cases/${caseId}/audit`);
}

export async function getChatHistory(
  caseId: string,
): Promise<{ messages: ChatHistoryMessage[] }> {
  return request(`/api/cases/${caseId}/chat/history`);
}

export async function generateReport(
  caseId: string,
  format: "markdown" | "pdf",
): Promise<{ format: string; content?: string; content_base64?: string }> {
  return request(`/api/cases/${caseId}/report?format=${format}`, { method: "POST" });
}

export async function reparseCase(caseId: string): Promise<{ status: string }> {
  return request(`/api/cases/${caseId}/reparse`, { method: "POST" });
}
