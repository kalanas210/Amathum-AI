import { createServer } from "http";
import next from "next";
import { WebSocketServer, WebSocket } from "ws";
import { v4 as uuidv4 } from "uuid";
import {
  createGeminiLiveSession,
  GeminiLiveSession,
} from "./src/lib/gemini-live";
import { loadMemory, saveMemory, createNewMemory } from "./src/lib/memory";
import type { CallerMemory } from "./src/types/index";

const dev = process.env.NODE_ENV !== "production";
const app = next({ dev });
const handle = app.getRequestHandler();
const PORT = parseInt(process.env.PORT || "3001", 10);

app.prepare().then(() => {
  const server = createServer((req, res) => {
    handle(req, res);
  });

  const wss = new WebSocketServer({ server, path: "/api/voice" });

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
            // Client sends config with optional callerId
            if (message.callerId) {
              callerId = message.callerId;
            }

            // Send status
            clientWs.send(
              JSON.stringify({ type: "status", status: "connecting" })
            );

            try {
              geminiSession = await createGeminiLiveSession(callerId);

              // Handle audio from Gemini → forward to client
              geminiSession.onAudio((audioBase64: string) => {
                if (clientWs.readyState === WebSocket.OPEN) {
                  clientWs.send(
                    JSON.stringify({ type: "audio", data: audioBase64 })
                  );
                }
              });

              // Handle transcripts from Gemini
              geminiSession.onTranscript(
                (text: string, role: string) => {
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
                }
              );

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

              // Notify client we're connected
              clientWs.send(
                JSON.stringify({
                  type: "status",
                  status: "connected",
                  callerId,
                })
              );

              // Send initial text to trigger greeting
              geminiSession.sendText(
                "The customer has just connected to the call. Please greet them now."
              );
            } catch (err) {
              const errMsg = err instanceof Error ? err.message : String(err);
              console.error("[Gemini] Connection failed:", errMsg);
              clientWs.send(
                JSON.stringify({
                  type: "error",
                  message: `Gemini error: ${errMsg}`,
                })
              );
            }
            break;
          }

          case "audio": {
            // Client sends audio chunk → forward to Gemini
            if (geminiSession && message.data) {
              geminiSession.send(message.data);
            }
            break;
          }

          case "save_memory": {
            // Client requests memory save with extracted info
            try {
              let memory =
                loadMemory(callerId) || createNewMemory(callerId);
              if (message.name) {
                memory.name = message.name;
                callerName = message.name;
              }
              if (message.language) {
                memory.language = message.language as CallerMemory["language"];
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
            // Clean shutdown
            if (geminiSession) {
              geminiSession.close();
              geminiSession = null;
            }

            // Auto-save basic memory
            try {
              let memory =
                loadMemory(callerId) || createNewMemory(callerId);
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

  server.listen(PORT, () => {
    console.log(`> Server running on http://localhost:${PORT}`);
    console.log(`> WebSocket available at ws://localhost:${PORT}/api/voice`);
  });
});
