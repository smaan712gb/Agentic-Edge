/** @type {import('next').NextConfig} */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

const nextConfig = {
  async rewrites() {
    return [
      // Everything EXCEPT /api/admin/* proxies straight to FastAPI.
      //
      // /api/admin/* is deliberately left for the Next server to handle, so
      // app/api/admin/[...path]/route.ts can attach the operator's admin
      // token on a loopback-bound dashboard and give the machine a kill
      // switch that needs no setup. The exclusion is required rather than
      // cosmetic: Next applies afterFiles rewrites BEFORE dynamic routes, so
      // a catch-all route handler cannot win against `/api/:path*` — the
      // request would be rewritten to the backend and never reach it.
      { source: "/api/:path((?!admin/).*)", destination: `${API_BASE}/api/:path` },
    ];
  },
};

export default nextConfig;
