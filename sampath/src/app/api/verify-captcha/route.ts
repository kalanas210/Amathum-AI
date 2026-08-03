import { NextRequest, NextResponse } from "next/server";

export async function POST(req: NextRequest) {
  try {
    const { token } = await req.json();

    if (!token) {
      return NextResponse.json({ success: false, error: "Missing token" }, { status: 400 });
    }

    const secret = process.env.TURNSTILE_SECRET_KEY;
    if (!secret) {
      console.error("[Turnstile] TURNSTILE_SECRET_KEY is not set");
      return NextResponse.json({ success: false, error: "Server misconfigured" }, { status: 500 });
    }

    const formData = new URLSearchParams();
    formData.append("secret", secret);
    formData.append("response", token);
    // Optionally append remoteip from request
    const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "";
    if (ip) formData.append("remoteip", ip);

    const cfRes = await fetch(
      "https://challenges.cloudflare.com/turnstile/v0/siteverify",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData.toString(),
      }
    );

    const data = await cfRes.json();

    if (data.success) {
      return NextResponse.json({ success: true });
    }

    return NextResponse.json(
      { success: false, error: data["error-codes"]?.join(", ") ?? "Verification failed" },
      { status: 403 }
    );
  } catch (err) {
    console.error("[Turnstile] Verification error:", err);
    return NextResponse.json({ success: false, error: "Server error" }, { status: 500 });
  }
}
