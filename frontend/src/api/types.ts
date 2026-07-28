// Mirrors the backend's real contracts exactly:
// - REST shapes: backend/app/api/routes_cases.py, routes_chat.py
// - WS frame shapes: backend/app/api/ws_chat.py's `_event_to_frame`,
//   which is a 1:1 serialization of orchestrator/tool_call_loop.py's
//   typed ConversationEvent union.

export type RiskClass = "AUTO_APPROVE" | "REQUIRES_CONFIRMATION" | "DENY";

export interface CaseSummary {
  case_id: string;
  name: string;
  status: string;
  created_at: string | null;
  event_count: number;
}

export interface NormalizedEventSummary {
  uid: string;
  channel: string;
  event_id: number;
  time_created: string | null;
  computer: string | null;
  user_sid: string | null;
}

export interface AuditEntry {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk_class: RiskClass;
  approval_status: string;
  called_at: string;
}

export interface ChatHistoryMessage {
  role: string;
  content: string;
  created_at: string;
}

// --- WebSocket frames: server -> client ---

export interface TokenFrame {
  type: "token";
  content: string;
}

export interface ToolCallFrame {
  type: "tool_call";
  tool_name: string;
  arguments: Record<string, unknown>;
  risk_class: RiskClass;
}

export interface ToolResultFrame {
  type: "tool_result";
  tool_name: string;
  result: Record<string, unknown>;
  injection_flagged: boolean;
}

export interface ConfirmationRequiredFrame {
  type: "confirmation_required";
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
}

export interface DoneFrame {
  type: "done";
  stopped_for_confirmation: boolean;
}

export interface ErrorFrame {
  type: "error";
  message: string;
}

export type ServerFrame =
  | TokenFrame
  | ToolCallFrame
  | ToolResultFrame
  | ConfirmationRequiredFrame
  | DoneFrame
  | ErrorFrame;

// --- WebSocket frames: client -> server ---

export interface UserMessageFrame {
  type: "user_message";
  content: string;
}

export interface ConfirmFrame {
  type: "confirm";
  approved: boolean;
}

export type ClientFrame = UserMessageFrame | ConfirmFrame;

// --- Conversation transcript item, as rendered in ChatPanel ---

export type TranscriptItem =
  | { kind: "message"; role: "user" | "assistant"; content: string; id: string }
  | { kind: "tool_call"; call: ToolCallFrame; result: ToolResultFrame | null; id: string }
  | { kind: "confirmation"; frame: ConfirmationRequiredFrame; resolved: boolean; id: string };
