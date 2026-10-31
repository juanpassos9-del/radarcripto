import { SignJWT, jwtVerify } from "jose";

const COOKIE_NAME = "nexus_session";
const SESSION_SECONDS = 60 * 60 * 24 * 7;
const MARKET_TOKEN_ISSUER = "nexus-terminal-web";
const MARKET_TOKEN_AUDIENCE = "nexus-market-api";
const DEFAULT_BETA_MAX_USERS = 10;

export function getBetaMaxUsers() {
  const configured = Number.parseInt(process.env.BETA_MAX_USERS ?? String(DEFAULT_BETA_MAX_USERS), 10);
  return Number.isFinite(configured) ? Math.max(1, Math.min(100, configured)) : DEFAULT_BETA_MAX_USERS;
}

function secretKey() {
  const secret = process.env.AUTH_SECRET;
  if (!secret || secret.length < 32) throw new Error("AUTH_SECRET precisa ter pelo menos 32 caracteres.");
  return new TextEncoder().encode(secret);
}

function marketSecretKey() {
  const secret = process.env.NEXUS_MARKET_TOKEN_SECRET;
  if (!secret || secret.length < 32) throw new Error("NEXUS_MARKET_TOKEN_SECRET precisa ter pelo menos 32 caracteres.");
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

export function getBetaUsers() {
  return getConfiguredUsers().slice(0, getBetaMaxUsers());
}

export async function createSession(email: string) {
  return new SignJWT({ email }).setProtectedHeader({ alg: "HS256" }).setIssuedAt()
    .setExpirationTime(`${SESSION_SECONDS}s`).sign(secretKey());
}

export async function verifySession(token: string) {
  try {
    const { payload } = await jwtVerify(token, secretKey());
    if (typeof payload.email !== "string") return null;
    return getBetaUsers().some((user) => user.email === payload.email) ? payload.email : null;
  } catch {
    return null;
  }
}

export async function createMarketToken(email: string, use: "api" | "ws", lifetimeSeconds: number) {
  return new SignJWT({ email, token_use: use })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(MARKET_TOKEN_ISSUER)
    .setAudience(MARKET_TOKEN_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(`${lifetimeSeconds}s`)
    .sign(marketSecretKey());
}

export { COOKIE_NAME, SESSION_SECONDS, MARKET_TOKEN_ISSUER, MARKET_TOKEN_AUDIENCE };
