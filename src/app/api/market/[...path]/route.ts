import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const origin = process.env.NEXUS_API_ORIGIN?.replace(/\/+$/, "");
  if (!origin) {
    return NextResponse.json({ detail: "A origem da API de mercado não está configurada no serviço web." }, { status: 503 });
  }

  const { path } = await context.params;
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, origin);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: { Authorization: request.headers.get("authorization") ?? "" },
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    console.error("Market API proxy request failed", error);
    return NextResponse.json({ detail: "Não foi possível conectar ao serviço da API de mercado." }, { status: 502 });
  }
}

export const GET = proxy;
