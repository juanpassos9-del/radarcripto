import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const origins = [...new Set([
    process.env.NEXUS_API_ORIGIN,
    process.env.NEXT_PUBLIC_MARKET_API_URL,
  ].map(value => value?.trim().replace(/\/+$/, "")).filter((value): value is string => Boolean(value)))];
  if (!origins.length) {
    return NextResponse.json({ detail: "A origem da API de mercado não está configurada no serviço web." }, { status: 503 });
  }

  const { path } = await context.params;
  for (const [index, origin] of origins.entries()) {
    const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, origin);
    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers: { Authorization: request.headers.get("authorization") ?? "" },
        cache: "no-store",
        signal: AbortSignal.timeout(20_000),
      });
      // A stale Render service URL returns a platform-level 404 with no server.
      // Try the public API URL baked into the web service before failing the request.
      if (upstream.status === 404 && upstream.headers.get("x-render-routing") === "no-server" && index < origins.length - 1) continue;
      return new NextResponse(upstream.body, {
        status: upstream.status,
        headers: {
          "Content-Type": upstream.headers.get("content-type") ?? "application/json",
          "Cache-Control": "no-store",
          ...(upstream.headers.has("retry-after") ? { "Retry-After": upstream.headers.get("retry-after")! } : {}),
        },
      });
    } catch (error) {
      if (index === origins.length - 1) {
        console.error("Market API proxy request failed", error);
        return NextResponse.json({ detail: "Não foi possível conectar ao serviço da API de mercado." }, { status: 502 });
      }
    }
  }
  return NextResponse.json({ detail: "Não foi possível conectar ao serviço da API de mercado." }, { status: 502 });
}

export const GET = proxy;
