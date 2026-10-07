"use client";

import { useState } from "react";
import { fmtDate, send, type Group } from "@/lib/api";
import { Badge, ErrorBox, useAction, useApi } from "@/components/ui";
import { copy } from "@/components/config/common";
import { ROLE_LABEL, type Invitation } from "@/lib/onboarding-types";
import { StepFooter, useWizard } from "./common";

type Row = { email: string; name: string; role: Invitation["role"]; group_ids: number[] };
const empty = (): Row => ({ email: "", name: "", role: "agent", group_ids: [] });

export default function TeamStep() {
  const { next, goTo, reload } = useWizard();
  const groups = useApi<Group[]>("/api/groups").data ?? [];
  const pending = useApi<Invitation[]>("/api/invitations");
  const [rows, setRows] = useState<Row[]>([empty(), empty()]);
  const [created, setCreated] = useState<Invitation[]>([]);
  const [copied, setCopied] = useState<number | null>(null);
  const [run, busy, error] = useAction();

  const setRow = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const valid = rows.filter((r) => /\S+@\S+\.\S+/.test(r.email.trim()));

  async function invite() {
    if (!valid.length) return;
    const r = await run(() =>
      send<Invitation[]>("/api/invitations", "POST", {
        invites: valid.map((v) => ({ email: v.email.trim(), name: v.name.trim() || null, role: v.role, group_ids: v.group_ids })),
      }),
    );
    if (r) {
      setCreated(r);
      setRows([empty()]);
      pending.reload();
      await reload();
    }
  }

  async function revoke(id: number) {
    await run(() => send(`/api/invitations/${id}`, "DELETE"));
    pending.reload();
  }

  function copyLink(inv: Invitation) {
    if (!inv.link) return;
    copy(inv.link);
    setCopied(inv.id);
    setTimeout(() => setCopied(null), 1500);
  }

  const open = (pending.data ?? []).filter((i) => !i.accepted_at);

  return (
    <div className="ob-step">
      <section className="card">
        <h3>Invita a tu equipo</h3>
        <p className="small muted">Cada persona recibe un enlace para crear su contraseña. Los asesores atienden conversaciones; los supervisores además ven reportes y calidad.</p>
        <div className="ob-invite-table" role="table" aria-label="Invitaciones">
          <div className="ob-invite-row head" role="row">
            <span role="columnheader">Correo</span>
            <span role="columnheader">Nombre</span>
            <span role="columnheader">Rol</span>
            <span role="columnheader">Grupos</span>
            <span />
          </div>
          {rows.map((r, i) => (
            <div key={i} className="ob-invite-row" role="row">
              <input type="email" aria-label="Correo" placeholder="asesor@empresa.com" value={r.email} onChange={(e) => setRow(i, { email: e.target.value })} />
              <input aria-label="Nombre" placeholder="Nombre" value={r.name} onChange={(e) => setRow(i, { name: e.target.value })} />
              <select aria-label="Rol" value={r.role} onChange={(e) => setRow(i, { role: e.target.value as Row["role"] })}>
                {(Object.keys(ROLE_LABEL) as Row["role"][]).map((k) => (
                  <option key={k} value={k}>
                    {ROLE_LABEL[k]}
                  </option>
                ))}
              </select>
              <select
                aria-label="Grupos"
                multiple
                value={r.group_ids.map(String)}
                onChange={(e) => setRow(i, { group_ids: Array.from(e.target.selectedOptions).map((o) => Number(o.value)) })}
                size={Math.min(3, Math.max(1, groups.length))}
              >
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
              <button className="icon" aria-label="Quitar fila" onClick={() => setRows(rows.length > 1 ? rows.filter((_, j) => j !== i) : [empty()])}>
                ✕
              </button>
            </div>
          ))}
        </div>
        <div className="inline" style={{ justifyContent: "space-between", marginTop: 8 }}>
          <button className="link" onClick={() => setRows([...rows, empty()])}>
            + Agregar otra persona
          </button>
          <button className="primary" onClick={invite} disabled={busy || !valid.length}>
            {busy ? "Invitando…" : `Invitar ${valid.length || ""}`.trim()}
          </button>
        </div>
        <ErrorBox error={error} />
      </section>

      {created.length > 0 && (
        <section className="card" aria-live="polite">
          <h3>Invitaciones creadas</h3>
          <ul className="ob-invite-results">
            {created.map((inv) => (
              <li key={inv.id}>
                <span>
                  <span className="strong">{inv.email}</span> · {ROLE_LABEL[inv.role]}
                </span>
                {inv.email_sent ? (
                  <Badge tone="ok">Correo enviado</Badge>
                ) : (
                  <span className="inline">
                    <Badge tone="warn">Comparte el enlace</Badge>
                    {inv.link && (
                      <button className="small" onClick={() => copyLink(inv)}>
                        {copied === inv.id ? "¡Copiado!" : "Copiar enlace"}
                      </button>
                    )}
                  </span>
                )}
              </li>
            ))}
          </ul>
          {created.some((c) => !c.email_sent) && <p className="small muted">El envío de correos no está configurado: comparte el enlace por WhatsApp o correo. Vence en 7 días.</p>}
        </section>
      )}

      {open.length > 0 && (
        <section className="card">
          <h3>Pendientes de aceptar ({open.length})</h3>
          <ul className="ob-invite-results">
            {open.map((inv) => (
              <li key={inv.id}>
                <span>
                  {inv.email} · {ROLE_LABEL[inv.role]} <span className="small muted">· vence {fmtDate(inv.expires_at)}</span>
                </span>
                <button className="link small danger" onClick={() => revoke(inv.id)}>
                  Revocar
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <StepFooter
        onBack={() => goTo("ai")}
        onNext={async () => {
          await send("/api/onboarding/steps/team/run", "POST").catch(() => undefined);
          await reload();
          next();
        }}
        nextLabel={open.length || created.length ? "Continuar" : "Lo hago después"}
      />
    </div>
  );
}
