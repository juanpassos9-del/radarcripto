import { NextRequest, NextResponse } from "next/server";
import { COOKIE_NAME, createMarketToken, verifySession } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const session = request.cookies.get(COOKIE_NAME)?.value;
  const email = session ? await verifySession(session) : null;
  if (!email) return NextResponse.json({ error: "Sessão inválida." }, { status: 401, headers: { "Cache-Control": "no-store" } });

  const use = request.nextUrl.searchParams.get("use");
  if (use !== "api" && use !== "ws") return NextResponse.json({ error: "Tipo de token inválido." }, { status: 400, headers: { "Cache-Control": "no-store" } });

  try {
    const expiresIn = use === "api" ? 60 : 45;
    const token = await createMarketToken(email, use, expiresIn);
    return NextResponse.json({ token, expiresIn }, { headers: { "Cache-Control": "no-store, private" } });
  } catch {
    return NextResponse.json({ error: "Autenticação da API não configurada." }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
