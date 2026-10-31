import { NextRequest, NextResponse } from "next/server";
import { COOKIE_NAME, verifySession } from "@/lib/auth";

export async function middleware(request: NextRequest) {
  const token = request.cookies.get(COOKIE_NAME)?.value;
  const email = token ? await verifySession(token) : null;
  const isLogin = request.nextUrl.pathname === "/login";

  if (email && isLogin) return NextResponse.redirect(new URL("/", request.url));
  if (!email && !isLogin) return NextResponse.redirect(new URL("/login", request.url));
  return NextResponse.next();
}

export const config = { matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico).*)"] };
