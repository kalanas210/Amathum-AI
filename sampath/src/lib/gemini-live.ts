import WebSocket from "ws";
import fs from "fs";
import path from "path";
import { buildMemoryContext, loadMemory } from "./memory";

const MODEL_NAME = "gemini-3.1-flash-live-preview";

function getWsUrl(): string {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    console.error("[Gemini] FATAL: GEMINI_API_KEY environment variable is not set!");
    throw new Error("GEMINI_API_KEY environment variable is not set. Please add it to your Railway Variables.");
  }
  return `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${apiKey}`;
}

function loadSystemInstructions(): string {
  const filePath = path.join(process.cwd(), "instructions.json");
  const data = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  return data.systemInstructions;
}

function getTimeGreetingContext(): string {
  const hour = new Date().getHours();
  if (hour >= 5 && hour < 12) return "Current time context: Morning (5am-12pm)";
  if (hour >= 12 && hour < 17)
    return "Current time context: Afternoon (12pm-5pm)";
  if (hour >= 17 && hour < 20)
    return "Current time context: Evening (5pm-8pm)";
  return "Current time context: Night (8pm-5am)";
}

export interface GeminiLiveSession {
  ws: WebSocket;
  send: (audioBase64: string) => void;
  sendText: (text: string) => void;
  close: () => void;
  onAudio: (callback: (audioBase64: string) => void) => void;
  onTranscript: (callback: (text: string, role: string) => void) => void;
  onError: (callback: (error: string) => void) => void;
  onClose: (callback: () => void) => void;
}

export function createGeminiLiveSession(
  callerId?: string
): Promise<GeminiLiveSession> {
  return new Promise((resolve, reject) => {
    const wsUrl = getWsUrl();
    const ws = new WebSocket(wsUrl);

    let audioCallback: ((audioBase64: string) => void) | null = null;
    let transcriptCallback:
      | ((text: string, role: string) => void)
      | null = null;
    let errorCallback: ((error: string) => void) | null = null;
    let closeCallback: (() => void) | null = null;
    let isConfigured = false;

    ws.on("open", () => {
      console.log("[Gemini] WebSocket connected to", wsUrl.split("?")[0]);
      // Build system instruction with memory context
      let systemText = loadSystemInstructions();
      systemText += "\n\n" + getTimeGreetingContext();

      if (callerId) {
        const memory = loadMemory(callerId);
        const memoryContext = buildMemoryContext(memory);
        if (memoryContext) {
          systemText += memoryContext;
        }
      }

      // Send configuration
      const configMessage = {
        setup: {
          model: `models/${MODEL_NAME}`,
          generationConfig: {
            responseModalities: ["AUDIO"],
            speechConfig: {
              voiceConfig: {
                prebuiltVoiceConfig: {
                  voiceName: "Zephyr",
                },
              },
            },
          },
          systemInstruction: {
            parts: [{ text: systemText }],
          },
        },
      };

      console.log("[Gemini] Sending setup for model:", `models/${MODEL_NAME}`);
      ws.send(JSON.stringify(configMessage));
    });

    ws.on("message", (data: WebSocket.Data) => {
      try {
        const response = JSON.parse(data.toString());
        console.log("[Gemini] Received:", JSON.stringify(response).slice(0, 300));

        // Config setup complete
        if (response.setupComplete && !isConfigured) {
          isConfigured = true;
          resolve(session);
          return;
        }

        // Handle audio response
        if (response.serverContent?.modelTurn?.parts) {
          for (const part of response.serverContent.modelTurn.parts) {
            if (part.inlineData?.data) {
              audioCallback?.(part.inlineData.data);
            }
            if (part.text) {
              transcriptCallback?.(part.text, "agent");
            }
          }
        }

        // Handle turn complete
        if (response.serverContent?.turnComplete) {
          // Turn is complete, agent finished speaking
        }

        // Handle tool calls if any
        if (response.toolCall) {
          // For now we don't use tools, but this is where you'd handle them
        }
      } catch (err) {
        console.error("Failed to parse Gemini response:", err);
      }
    });

    ws.on("error", (err) => {
      console.error("[Gemini] WS error:", err.message);
      errorCallback?.(err.message);
      if (!isConfigured) reject(new Error(`Gemini connection failed: ${err.message}`));
    });

    ws.on("close", (code, reason) => {
      console.log("[Gemini] WebSocket closed. Code:", code, "Reason:", reason?.toString());
      closeCallback?.();
    });

    const session: GeminiLiveSession = {
      ws,
      send: (audioBase64: string) => {
        if (ws.readyState === WebSocket.OPEN) {
          const audioMessage = {
            realtimeInput: {
              audio: {
                data: audioBase64,
                mimeType: "audio/pcm;rate=16000",
              },
            },
          };
          ws.send(JSON.stringify(audioMessage));
        }
      },
      sendText: (text: string) => {
        if (ws.readyState === WebSocket.OPEN) {
          const textMessage = {
            realtimeInput: {
              text: text,
            },
          };
          ws.send(JSON.stringify(textMessage));
        }
      },
      close: () => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.close();
        }
      },
      onAudio: (cb) => {
        audioCallback = cb;
      },
      onTranscript: (cb) => {
        transcriptCallback = cb;
      },
      onError: (cb) => {
        errorCallback = cb;
      },
      onClose: (cb) => {
        closeCallback = cb;
      },
    };

    // Timeout if config doesn't complete in 10 seconds
    setTimeout(() => {
      if (!isConfigured) {
        ws.close();
        reject(new Error("Gemini Live API connection timeout"));
      }
    }, 10000);
  });
}
