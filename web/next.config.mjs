/** @type {import('next').NextConfig} */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

const nextConfig = {
  async rewrites() {
    return [
      // Everything EXCEPT the token-guarded prefixes proxies straight to FastAPI.
      //
      // /api/admin/* and /api/trade-intents/* are deliberately left for the
      // Next server to handle, so their route handlers can attach the
      // operator's admin token on a loopback-bound dashboard — giving the
      // machine a kill switch and a Build button that need no setup. Both
      // back onto lib/adminProxy.ts. The exclusion is required rather than
      // cosmetic: Next applies afterFiles rewrites BEFORE dynamic routes, so
      // a catch-all route handler cannot win against `/api/:path*` — the
      // request would be rewritten to the backend and never reach it.
      { source: "/api/:path((?!admin/|trade-intents/).*)", destination: `${API_BASE}/api/:path` },
    ];
  },
};

export default nextConfig;
