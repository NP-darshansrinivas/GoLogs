import { useCallback, useEffect, useRef, useState } from "react";
import { getChatHistory } from "../api/client";
import type {
  ClientFrame,
  ConfirmationRequiredFrame,
  ServerFrame,
  ToolCallFrame,
  ToolResultFrame,
  TranscriptItem,
} from "../api/types";

export type ConnectionStatus = "connecting" | "open" | "closed" | "error";

let nextId = 0;
function makeId(): string {
  nextId += 1;
  return `item-${nextId}`;
}

interface UseChatResult {
  transcript: TranscriptItem[];
  status: ConnectionStatus;
  pendingConfirmation: ConfirmationRequiredFrame | null;
  sendMessage: (content: string) => void;
  respondToConfirmation: (approved: boolean) => void;
}

/**
 * Owns the live WebSocket connection to `/ws/cases/{caseId}/chat` and
 * turns the raw server-frame stream into a renderable transcript.
 * Includes auto-reconnect, message queueing, and chat history hydration.
 */
export function useChat(caseId: string): UseChatResult {
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [pendingConfirmation, setPendingConfirmation] =
    useState<ConfirmationRequiredFrame | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const streamingMessageIdRef = useRef<string | null>(null);
  const pendingToolCallIdRef = useRef<Map<string, string>>(new Map());
  const queuedMessagesRef = useRef<ClientFrame[]>([]);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hydrate chat history on caseId change
  useEffect(() => {
    if (!caseId || typeof caseId !== "string" || !caseId.trim()) return;

    let cancelled = false;
    getChatHistory(caseId)
      .then((data) => {
        if (cancelled) return;
        if (data.messages && data.messages.length > 0) {
          const items: TranscriptItem[] = data.messages.map((m) => ({
            kind: "message",
            role: m.role as "user" | "assistant",
            content: m.content,
            id: makeId(),
          }));
          setTranscript(items);
        }
      })
      .catch(() => {
        // Ignored, will populate on live messages
      });

    return () => {
      cancelled = true;
    };
  }, [caseId]);

  useEffect(() => {
    if (!caseId || typeof caseId !== "string" || !caseId.trim()) {
      setStatus("closed");
      return;
    }

    let isDisposed = false;

    function connect() {
      if (isDisposed) return;

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(
        `${protocol}//${window.location.host}/ws/cases/${encodeURIComponent(caseId)}/chat`,
      );
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (isDisposed) {
          ws.close();
          return;
        }
        setStatus("open");
        // Flush any queued messages
        while (queuedMessagesRef.current.length > 0) {
          const msg = queuedMessagesRef.current.shift();
          if (msg) ws.send(JSON.stringify(msg));
        }
      };

      ws.onclose = () => {
        if (isDisposed) return;
        setStatus("closed");
        wsRef.current = null;
        // Schedule auto-reconnect
        if (!reconnectTimerRef.current) {
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            connect();
          }, 1500);
        }
      };

      ws.onerror = () => {
        if (isDisposed) return;
        setStatus("error");
      };

      ws.onmessage = (event: MessageEvent<string>) => {
        try {
          const frame = JSON.parse(event.data) as ServerFrame;
          handleFrame(frame);
        } catch {
          // Ignore malformed JSON
        }
      };
    }

    function handleFrame(frame: ServerFrame): void {
      switch (frame.type) {
        case "token": {
          appendToken(frame.content);
          break;
        }
        case "tool_call": {
          startToolCall(frame);
          break;
        }
        case "tool_result": {
          completeToolCall(frame.tool_name, frame);
          break;
        }
        case "confirmation_required": {
          setPendingConfirmation(frame);
          break;
        }
        case "done": {
          streamingMessageIdRef.current = null;
          break;
        }
        case "error": {
          setTranscript((prev) => [
            ...prev,
            { kind: "message", role: "assistant", content: `⚠ ${frame.message}`, id: makeId() },
          ]);
          break;
        }
      }
    }

    function appendToken(content: string): void {
      setTranscript((prev) => {
        const currentId = streamingMessageIdRef.current;
        if (currentId) {
          return prev.map((item) =>
            item.kind === "message" && item.id === currentId
              ? { ...item, content: item.content + content }
              : item,
          );
        }
        const id = makeId();
        streamingMessageIdRef.current = id;
        return [...prev, { kind: "message", role: "assistant", content, id }];
      });
    }

    function startToolCall(call: ToolCallFrame): void {
      const id = makeId();
      pendingToolCallIdRef.current.set(call.tool_name, id);
      setTranscript((prev) => [...prev, { kind: "tool_call", call, result: null, id }]);
    }

    function completeToolCall(toolName: string, result: ToolResultFrame): void {
      const id = pendingToolCallIdRef.current.get(toolName);
      pendingToolCallIdRef.current.delete(toolName);
      setTranscript((prev) =>
        prev.map((item) =>
          item.kind === "tool_call" && item.id === id ? { ...item, result } : item,
        ),
      );
    }

    connect();

    return () => {
      isDisposed = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [caseId]);

  const send = useCallback((frame: ClientFrame) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(frame));
    } else {
      queuedMessagesRef.current.push(frame);
    }
  }, []);

  const sendMessage = useCallback(
    (content: string) => {
      if (!content.trim()) return;
      setTranscript((prev) => [...prev, { kind: "message", role: "user", content, id: makeId() }]);
      send({ type: "user_message", content });
    },
    [send],
  );

  const respondToConfirmation = useCallback(
    (approved: boolean) => {
      if (!pendingConfirmation) return;
      setTranscript((prev) => [
        ...prev,
        { kind: "confirmation", frame: pendingConfirmation, resolved: true, id: makeId() },
      ]);
      send({ type: "confirm", approved });
      setPendingConfirmation(null);
    },
    [pendingConfirmation, send],
  );

  return { transcript, status, pendingConfirmation, sendMessage, respondToConfirmation };
}
