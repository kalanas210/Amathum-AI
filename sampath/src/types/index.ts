export interface CallerMemory {
  callerId: string;
  name?: string;
  language?: "si" | "ta" | "en";
  nicNumber?: string;
  accountNumber?: string;
  previousInteractions: {
    date: string;
    summary: string;
    language?: string;
  }[];
  createdAt: string;
  updatedAt: string;
}

export interface ConversationTurn {
  role: "user" | "agent";
  text?: string;
  timestamp: string;
}

export type CallStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "listening"
  | "speaking"
  | "error"
  | "ended";

export interface WebSocketMessage {
  type:
    | "audio"
    | "config"
    | "end_call"
    | "status"
    | "error"
    | "transcript"
    | "save_memory";
  data?: string; // base64 audio or JSON string
  callerId?: string;
  status?: CallStatus;
  message?: string;
  transcript?: string;
}
