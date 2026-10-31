import { SignJWT, jwtVerify } from "jose";

const COOKIE_NAME = "nexus_session";
const SESSION_SECONDS = 60 * 60 * 24 * 7;

function secretKey() {
  const secret = process.env.AUTH_SECRET;
  if (!secret || secret.length < 32) throw new Error("AUTH_SECRET precisa ter pelo menos 32 caracteres.");
  return new TextEncoder().encode(secret);
}

export function getConfiguredUsers() {
  const source = process.env.AUTH_USERS;
  if (!source) return [] as Array<{ email: string; password: string }>;
  try {
    const parsed: unknown = JSON.parse(source);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((user): user is { email: string; password: string } =>
      typeof user?.email === "string" && typeof user?.password === "string" && user.password.length >= 12,
    ).map((user) => ({ email: user.email.trim().toLowerCase(), password: user.password }));
  } catch {
    return [];
  }
}

export async function createSession(email: string) {
  return new SignJWT({ email }).setProtectedHeader({ alg: "HS256" }).setIssuedAt()
    .setExpirationTime(`${SESSION_SECONDS}s`).sign(secretKey());
}

export async function verifySession(token: string) {
  try {
    const { payload } = await jwtVerify(token, secretKey());
    return typeof payload.email === "string" ? payload.email : null;
  } catch {
    return null;
  }
}

export { COOKIE_NAME, SESSION_SECONDS };
