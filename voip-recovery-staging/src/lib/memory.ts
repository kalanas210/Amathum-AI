import fs from "fs";
import path from "path";
import { CallerMemory } from "../types";

const MEMORY_DIR = path.join(process.cwd(), "memory");

function ensureMemoryDir() {
  if (!fs.existsSync(MEMORY_DIR)) {
    fs.mkdirSync(MEMORY_DIR, { recursive: true });
  }
}

export function loadMemory(callerId: string): CallerMemory | null {
  ensureMemoryDir();
  const filePath = path.join(MEMORY_DIR, `${callerId}.json`);
  if (!fs.existsSync(filePath)) return null;
  const data = fs.readFileSync(filePath, "utf-8");
  return JSON.parse(data) as CallerMemory;
}

export function saveMemory(memory: CallerMemory): void {
  ensureMemoryDir();
  memory.updatedAt = new Date().toISOString();
  const filePath = path.join(MEMORY_DIR, `${memory.callerId}.json`);
  fs.writeFileSync(filePath, JSON.stringify(memory, null, 2), "utf-8");
}

export function createNewMemory(callerId: string): CallerMemory {
  return {
    callerId,
    previousInteractions: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

export function buildMemoryContext(memory: CallerMemory | null): string {
  if (!memory || memory.previousInteractions.length === 0) return "";

  let context = "\n\n## CALLER MEMORY FROM PREVIOUS CALLS\n";

  if (memory.name) {
    context += `- Caller's name: ${memory.name}\n`;
  }
  if (memory.language) {
    const langMap = { si: "Sinhala", ta: "Tamil", en: "English" };
    context += `- Preferred language: ${langMap[memory.language]}\n`;
  }
  if (memory.nicNumber) {
    context += `- NIC number on file: ${memory.nicNumber}\n`;
  }

  context += "\nPrevious interactions:\n";
  for (const interaction of memory.previousInteractions.slice(-5)) {
    context += `- ${interaction.date}: ${interaction.summary}\n`;
  }

  context +=
    "\nUse this information naturally. Greet them by name if known. Reference previous issues if relevant.";
  return context;
}

export function listAllCallerIds(): string[] {
  ensureMemoryDir();
  return fs
    .readdirSync(MEMORY_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.replace(".json", ""));
}
