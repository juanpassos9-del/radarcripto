import { NextResponse } from "next/server";
import { createSession, COOKIE_NAME, getBetaUsers, SESSION_SECONDS } from "@/lib/auth";

const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 10;
const attempts = new Map<string, { count: number; expiresAt: number }>();

export async function POST(request: Request) {
  try {
    const { email, password } = await request.json();
    if (typeof email !== "string" || typeof password !== "string") {
      return NextResponse.json({ error: "Informe e-mail e senha." }, { status: 400 });
    }
    const normalizedEmail = email.trim().toLowerCase();
    const ip = request.headers.get("x-real-ip") ?? request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
    const key = `${ip}:${normalizedEmail}`;
    const now = Date.now();
    for (const [attemptKey, value] of attempts) if (value.expiresAt <= now) attempts.delete(attemptKey);
    const state = attempts.get(key);
    if (state && state.expiresAt > now && state.count >= MAX_ATTEMPTS) {
      return NextResponse.json({ error: "Muitas tentativas. Aguarde 15 minutos e tente novamente." }, { status: 429 });
    }
    const match = getBetaUsers().find((user) => user.email === normalizedEmail && user.password === password);
    if (!match) {
      attempts.set(key, { count: state && state.expiresAt > now ? state.count + 1 : 1, expiresAt: state && state.expiresAt > now ? state.expiresAt : now + WINDOW_MS });
      return NextResponse.json({ error: "E-mail ou senha inválidos." }, { status: 401 });
    }
    attempts.delete(key);

    const response = NextResponse.json({ ok: true });
    response.cookies.set(COOKIE_NAME, await createSession(normalizedEmail), {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: SESSION_SECONDS,
    });
    return response;
  } catch {
    return NextResponse.json({ error: "Não foi possível entrar. Tente novamente." }, { status: 400 });
  }
}
