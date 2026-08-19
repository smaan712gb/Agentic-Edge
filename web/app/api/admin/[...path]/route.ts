/**
 * Server-side admin proxy — supplies the operator's admin token so the
 * emergency halt needs no setup at the machine running the stack.
 *
 * WHY THIS EXISTS
 * The kill switch used to read ADMIN_API_TOKEN from per-origin localStorage.
 * That made losing it routine: clearing site data, a fresh profile, or simply
 * opening the dashboard on http://127.0.0.1:3001 instead of localhost:3001
 * (a different origin, therefore a different store) left the operator with no
 * working halt and no visible state. An emergency control that depends on the
 * browser having been set up beforehand is not an emergency control.
 *
 * WHY IT IS SAFE
 * Injection happens ONLY when ADMIN_TOKEN_AUTOINJECT=1, which start-all.ps1
 * sets in the same breath as binding Next to 127.0.0.1. The loopback bind is
 * the real security boundary — a socket that only accepts local connections
 * cannot be reached from the network at all — and tying the flag to that one
 * launch path means a dashboard started any other way (on all interfaces)
 * does NOT auto-inject. Client IP is deliberately not sniffed: Host and
 * X-Forwarded-For are caller-controlled and would be security theatre here.
 *
 * A caller that sends its own X-Admin-Token always wins, so nothing about the
 * existing token flow changes; and with the flag off this file is a plain
 * pass-through, leaving the backend to authenticate exactly as before.
 */

import { NextRequest, NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

let cachedToken: string | null | undefined;

/** Token from the process env, falling back to the repo-root .env. */
async function adminToken(): Promise<string | null> {
  if (cachedToken !== undefined) return cachedToken;
  const fromEnv = process.env.ADMIN_API_TOKEN?.trim();
  if (fromEnv) return (cachedToken = fromEnv);
  // start-all.ps1 exports it, but a hand-started `npm run dev` would not.
  try {
    const raw = await fs.readFile(path.resolve(process.cwd(), "..", ".env"), "utf8");
    const line = raw.split(/\r?\n/).find((l) => l.startsWith("ADMIN_API_TOKEN="));
    const v = line?.slice("ADMIN_API_TOKEN=".length).trim();
    return (cachedToken = v || null);
  } catch {
    return (cachedToken = null);
  }
}

async function forward(req: NextRequest, segments: string[]): Promise<NextResponse> {
  const target = `${API_BASE}/api/admin/${segments.join("/")}${req.nextUrl.search}`;

  const headers = new Headers();
  const ct = req.headers.get("content-type");
  if (ct) headers.set("content-type", ct);

  // The caller's own token always takes precedence.
  const supplied = req.headers.get("x-admin-token");
  if (supplied) {
    headers.set("x-admin-token", supplied);
  } else if (process.env.ADMIN_TOKEN_AUTOINJECT === "1") {
    const tok = await adminToken();
    if (tok) headers.set("x-admin-token", tok);
  }

  const body = req.method === "GET" || req.method === "HEAD"
    ? undefined
    : await req.text();

  try {
    const res = await fetch(target, { method: req.method, headers, body, cache: "no-store" });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") || "application/json" },
    });
  } catch (e: unknown) {
    // The backend being down must read as backend-down, not as auth failure.
    return NextResponse.json(
      { detail: `admin proxy: backend unreachable (${e instanceof Error ? e.message : String(e)})` },
      { status: 502 },
    );
  }
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).path);
}

export async function POST(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).path);
}

export async function DELETE(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).path);
}
