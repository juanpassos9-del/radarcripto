"use client";

import { FormEvent, useState } from "react";
import { Activity, ArrowRight, LockKeyhole, Mail } from "lucide-react";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const result = await response.json();
      if (!response.ok) {
        setError(result.error || "Não foi possível entrar.");
        return;
      }
      window.location.replace("/");
    } catch {
      setError("Falha de conexão. Tente novamente.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-screen">
      <section className="auth-card">
        <div className="auth-brand"><span className="auth-brand-icon"><Activity size={18} /></span><span>NEXUS <b>TERMINAL</b></span></div>
        <div className="auth-eyebrow">ACESSO PRIVADO</div>
        <h1>Entrar na plataforma</h1>
        <p className="auth-description">Use as credenciais fornecidas pelo administrador.</p>
        <form onSubmit={submit} className="auth-form">
          <label htmlFor="email">E-mail</label>
          <div className="auth-input-wrap"><Mail size={16} /><input id="email" type="email" autoComplete="username" placeholder="voce@exemplo.com" value={email} onChange={(event) => setEmail(event.target.value)} required /></div>
          <label htmlFor="password">Senha</label>
          <div className="auth-input-wrap"><LockKeyhole size={16} /><input id="password" type="password" autoComplete="current-password" placeholder="Sua senha" value={password} onChange={(event) => setPassword(event.target.value)} required /></div>
          {error && <div className="auth-error" role="alert">{error}</div>}
          <button className="auth-submit" type="submit" disabled={loading}>{loading ? "Verificando..." : "Entrar"}<ArrowRight size={16} /></button>
        </form>
        <div className="auth-footnote"><span className="auth-status-dot" /> Acesso protegido · Contas fornecidas pelo administrador</div>
      </section>
      <div className="auth-footer">NEXUS CRYPTO TERMINAL <span>·</span> MARKET INTELLIGENCE</div>
    </main>
  );
}
