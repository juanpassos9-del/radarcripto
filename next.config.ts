import type { NextConfig } from "next";
const apiOrigin = process.env.NEXUS_API_ORIGIN || "http://127.0.0.1:8001";
const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/health", destination: `${apiOrigin}/health` },
    ];
  },
};
export default nextConfig;
