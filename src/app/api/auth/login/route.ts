import { NextResponse } from "next/server";
import { createSession, COOKIE_NAME, getConfiguredUsers, SESSION_SECONDS } from "@/lib/auth";

export async function POST(request: Request) {
  try {
    const { email, password } = await request.json();
    if (typeof email !== "string" || typeof password !== "string") {
      return NextResponse.json({ error: "Informe e-mail e senha." }, { status: 400 });
    }
    const normalizedEmail = email.trim().toLowerCase();
    const match = getConfiguredUsers().find((user) => user.email === normalizedEmail && user.password === password);
    if (!match) return NextResponse.json({ error: "E-mail ou senha inválidos." }, { status: 401 });

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
