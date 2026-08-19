// Token-injecting proxy for /api/trade-intents/* — see lib/adminProxy.ts for why this
// exists and why it is safe. next.config.mjs must EXCLUDE this prefix from the
// blanket backend rewrite: Next applies afterFiles rewrites before dynamic
// routes, so a catch-all handler cannot otherwise win and the request would be
// forwarded to FastAPI without ever reaching here.
import { NextRequest } from "next/server";
import { forwardWithToken } from "@/lib/adminProxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return forwardWithToken(req, "trade-intents", (await ctx.params).path);
}

export async function POST(req: NextRequest, ctx: Ctx) {
  return forwardWithToken(req, "trade-intents", (await ctx.params).path);
}

export async function DELETE(req: NextRequest, ctx: Ctx) {
  return forwardWithToken(req, "trade-intents", (await ctx.params).path);
}
