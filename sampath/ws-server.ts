import { WebSocketServer, WebSocket } from "ws";
import { v4 as uuidv4 } from "uuid";
import dotenv from "dotenv";
import {
  createGeminiLiveSession,
  GeminiLiveSession,
} from "./src/lib/gemini-live";
import { loadMemory, saveMemory, createNewMemory } from "./src/lib/memory";
import type { CallerMemory } from "./src/types/index";

dotenv.config({ path: ".env.local" });

const WS_PORT = parseInt(process.env.WS_PORT || "3002", 10);

const wss = new WebSocketServer({ port: WS_PORT });

console.log(`> WebSocket server running on ws://localhost:${WS_PORT}`);

wss.on("connection", (clientWs: WebSocket) => {
  console.log("[WS] Client connected");

  let geminiSession: GeminiLiveSession | null = null;
  let callerId: string = uuidv4();
  let conversationLog: string[] = [];
  let callerName: string | undefined;
  let detectedLanguage: string | undefined;

  clientWs.on("message", async (rawData: Buffer) => {
    try {
      const message = JSON.parse(rawData.toString());

      switch (message.type) {
        case "config": {
          if (message.callerId) {
            callerId = message.callerId;
          }

          clientWs.send(
            JSON.stringify({ type: "status", status: "connecting" })
          );

          try {
            geminiSession = await createGeminiLiveSession(callerId);

            geminiSession.onAudio((audioBase64: string) => {
              if (clientWs.readyState === WebSocket.OPEN) {
                clientWs.send(
                  JSON.stringify({ type: "audio", data: audioBase64 })
                );
              }
            });

            geminiSession.onTranscript((text: string, role: string) => {
              conversationLog.push(`${role}: ${text}`);
              if (clientWs.readyState === WebSocket.OPEN) {
                clientWs.send(
                  JSON.stringify({
                    type: "transcript",
                    transcript: text,
                    role,
                  })
                );
              }
            });

            geminiSession.onError((error: string) => {
              console.error("[Gemini] Error:", error);
              if (clientWs.readyState === WebSocket.OPEN) {
                clientWs.send(
                  JSON.stringify({ type: "error", message: error })
                );
              }
            });

            geminiSession.onClose(() => {
              console.log("[Gemini] Session closed");
              if (clientWs.readyState === WebSocket.OPEN) {
                clientWs.send(
                  JSON.stringify({ type: "status", status: "ended" })
                );
              }
            });

            clientWs.send(
              JSON.stringify({
                type: "status",
                status: "connected",
                callerId,
              })
            );

            // Trigger the greeting
            geminiSession.sendText(
              "The customer has just connected to the call. Please greet them now."
            );
          } catch (err: unknown) {
            const errMsg =
              err instanceof Error ? err.message : "Unknown error";
            console.error("[Gemini] Connection failed:", errMsg);
            clientWs.send(
              JSON.stringify({
                type: "error",
                message: `Failed to connect to Gemini: ${errMsg}`,
              })
            );
          }
          break;
        }

        case "audio": {
          if (geminiSession && message.data) {
            geminiSession.send(message.data);
          }
          break;
        }

        case "save_memory": {
          try {
            let memory = loadMemory(callerId) || createNewMemory(callerId);
            if (message.name) {
              memory.name = message.name;
              callerName = message.name;
            }
            if (message.language) {
              memory.language =
                message.language as CallerMemory["language"];
              detectedLanguage = message.language;
            }
            if (message.nicNumber) {
              memory.nicNumber = message.nicNumber;
            }
            memory.previousInteractions.push({
              date: new Date().toISOString().split("T")[0],
              summary:
                message.summary ||
                `Call on ${new Date().toLocaleDateString()}`,
              language: detectedLanguage,
            });
            saveMemory(memory);
            console.log(`[Memory] Saved for caller ${callerId}`);
          } catch (err) {
            console.error("[Memory] Save failed:", err);
          }
          break;
        }

        case "end_call": {
          if (geminiSession) {
            geminiSession.close();
            geminiSession = null;
          }

          try {
            let memory = loadMemory(callerId) || createNewMemory(callerId);
            if (callerName) memory.name = callerName;
            if (detectedLanguage)
              memory.language =
                detectedLanguage as CallerMemory["language"];
            if (conversationLog.length > 0) {
              memory.previousInteractions.push({
                date: new Date().toISOString().split("T")[0],
                summary: `Call with ${conversationLog.length} exchanges`,
                language: detectedLanguage,
              });
            }
            saveMemory(memory);
          } catch (err) {
            console.error("[Memory] Auto-save failed:", err);
          }

          clientWs.send(
            JSON.stringify({ type: "status", status: "ended" })
          );
          break;
        }
      }
    } catch (err) {
      console.error("[WS] Failed to process message:", err);
    }
  });

  clientWs.on("close", () => {
    console.log("[WS] Client disconnected");
    if (geminiSession) {
      geminiSession.close();
      geminiSession = null;
    }
  });

  clientWs.on("error", (err) => {
    console.error("[WS] Client error:", err);
  });
});
