import { NextResponse } from "next/server";

/** Tiny response helpers shared by every route handler. */
export function json(data: unknown, status = 200): NextResponse {
  return NextResponse.json(data, { status });
}

export function notFound(): NextResponse {
  return NextResponse.json({ error: "not found" }, { status: 404 });
}

export function badRequest(error: string): NextResponse {
  return NextResponse.json({ error }, { status: 400 });
}
