"use client";

import { Plus, RefreshCw, RotateCcw, Send, Trash2 } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { formatDateTime, shortHash } from "@/lib/format";
import { WEBHOOK_EVENT_TYPES, type WebhookCreated, type WebhookDeliveryRead, type WebhookDeliveryStatus, type WebhookEventType, type WebhookRead } from "@/lib/types";
import { checkWebhookEventTypes, checkWebhookUrl } from "@/lib/validation";
import { WEBHOOK_VERIFY_NODE, WEBHOOK_VERIFY_PYTHON } from "@/lib/webhook-snippets";
import { ApiKeyReveal } from "./api-key-reveal";
import { CodeBlock } from "./code-block";
import { ErrorBanner, Field, Spinner, Toggle } from "./ui";

const EVENT_LABELS: Record<WebhookEventType, string> = {
  "payment.settled": "Payment settled (all payment formats)",
  "prepaid.deposit": "Prepaid deposit",
  "prepaid.debit": "Prepaid debit",
  "trial.used": "Free trial call used",
  "endpoint.created": "Endpoint created",
  "endpoint.updated": "Endpoint updated",
};

const STATUS_TONE: Record<WebhookDeliveryStatus, string> = {
  succeeded: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
  pending: "border-amber-800 bg-amber-950/50 text-amber-300",
  delivering: "border-sky-800 bg-sky-950/50 text-sky-300",
  dead: "border-red-900 bg-red-950/50 text-red-300",
};

function DeliveryBadge({ status }: { status: WebhookDeliveryStatus }) {
  return <span className={`badge ${STATUS_TONE[status]}`}>{status}</span>;
}

function CreateWebhookForm({ onCreated }: { onCreated: (created: WebhookCreated) => void }) {
  const uid = useId();
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [types, setTypes] = useState<WebhookEventType[]>(["payment.settled"]);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const urlCheck = checkWebhookUrl(url);
  const typesCheck = checkWebhookEventTypes(types);

  function toggle(type: WebhookEventType) {
    setTypes((current) => (current.includes(type) ? current.filter((t) => t !== type) : [...current, type]));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!urlCheck.ok || !typesCheck.ok) return;
    setBusy(true);
    setError(null);
    try {
      const created = await apiFetch<WebhookCreated>("/api/webhooks", {
        method: "POST",
        body: { url: urlCheck.value, event_types: typesCheck.value, description: description.trim() || null },
      });
      setUrl("");
      setDescription("");
      setTouched(false);
      onCreated(created);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4 p-5" noValidate aria-label="Add webhook">
      <div className="grid gap-4 sm:grid-cols-[2fr_1fr]">
        <Field
          label="HTTPS endpoint URL"
          htmlFor={`${uid}-url`}
          error={touched && !urlCheck.ok ? urlCheck.error : null}
          hint="Public HTTPS only. Private, loopback and link-local addresses are refused, also after DNS resolution."
        >
          <input id={`${uid}-url`} className="input font-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hooks.example.com/kasgate" inputMode="url" autoComplete="off" spellCheck={false} />
        </Field>
        <Field label="Description (optional)" htmlFor={`${uid}-desc`}>
          <input id={`${uid}-desc`} className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Order fulfilment" maxLength={200} autoComplete="off" />
        </Field>
      </div>
      <fieldset>
        <legend className="label">Events</legend>
        <div className="flex flex-wrap gap-2">
          {WEBHOOK_EVENT_TYPES.map((type) => {
            const on = types.includes(type);
            return (
              <label
                key={type}
                title={EVENT_LABELS[type]}
                className={`cursor-pointer rounded-md border px-3 py-1.5 font-mono text-xs transition ${on ? "border-kas-500 bg-kas-500/15 text-kas-300" : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700"}`}
              >
                <input type="checkbox" className="sr-only" checked={on} onChange={() => toggle(type)} />
                {type}
              </label>
            );
          })}
        </div>
        {touched && !typesCheck.ok ? <p className="mt-1.5 text-xs text-red-400">{typesCheck.error}</p> : null}
      </fieldset>
      <ErrorBanner message={error} />
      <button type="submit" className="btn-primary" disabled={busy}>
        <Plus className="h-4 w-4" aria-hidden />
        {busy ? "Adding…" : "Add webhook"}
      </button>
    </form>
  );
}

function DeliveryLog({ webhook, reloadKey, onError }: { webhook: WebhookRead; reloadKey: number; onError: (message: string) => void }) {
  const [status, setStatus] = useState<WebhookDeliveryStatus | "">("");
  const [rows, setRows] = useState<WebhookDeliveryRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const query = status ? `&status=${status}` : "";
    apiFetch<WebhookDeliveryRead[]>(`/api/webhooks/${webhook.id}/deliveries?limit=25${query}`)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) onError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [webhook.id, status, reloadKey, tick, onError]);

  async function redeliver(delivery: WebhookDeliveryRead) {
    setBusyId(delivery.id);
    try {
      await apiFetch<WebhookDeliveryRead>(`/api/webhooks/${webhook.id}/deliveries/${delivery.id}/redeliver`, { method: "POST" });
      setTick((t) => t + 1);
    } catch (e) {
      onError(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-zinc-300">Delivery log</h4>
        <div className="flex items-center gap-2">
          <select className="input w-auto py-1 text-xs" value={status} onChange={(e) => setStatus(e.target.value as WebhookDeliveryStatus | "")} aria-label="Filter deliveries by status">
            <option value="">All statuses</option>
            <option value="succeeded">Succeeded</option>
            <option value="pending">Pending (retrying)</option>
            <option value="delivering">Delivering</option>
            <option value="dead">Dead (gave up)</option>
          </select>
          <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => setTick((t) => t + 1)} aria-label="Reload delivery log">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </div>
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full min-w-[760px]">
          <thead>
            <tr className="border-b border-zinc-800">
              <th className="th">Event</th>
              <th className="th">Status</th>
              <th className="th text-right">Attempts</th>
              <th className="th">Last result</th>
              <th className="th">Next / delivered (UTC)</th>
              <th className="th" />
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-zinc-900/60">
                <td className="td">
                  <div className="font-mono text-xs text-zinc-200">{row.event_type}</div>
                  <div className="font-mono text-[11px] text-zinc-600" title={row.event_id}>
                    {shortHash(row.event_id, 12, 6)}
                  </div>
                </td>
                <td className="td">
                  <DeliveryBadge status={row.status} />
                </td>
                <td className="td text-right font-mono text-xs text-zinc-300">{row.attempts}</td>
                <td className="td max-w-[280px] text-xs text-zinc-400">
                  {row.last_status_code !== null ? <span className="font-mono text-zinc-300">HTTP {row.last_status_code} </span> : null}
                  {row.last_error ? <span className="break-words">{row.last_error}</span> : row.status === "succeeded" ? "OK" : null}
                </td>
                <td className="td font-mono text-xs text-zinc-400">{row.status === "succeeded" ? formatDateTime(row.delivered_at) : formatDateTime(row.next_attempt_at)}</td>
                <td className="td text-right">
                  {row.status !== "delivering" ? (
                    <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void redeliver(row)} disabled={busyId === row.id}>
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                      Redeliver
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading ? <Spinner label="Loading deliveries…" /> : null}
        {!loading && rows.length === 0 ? <p className="px-4 py-6 text-center text-sm text-zinc-500">No deliveries yet. Send a test event to try your endpoint.</p> : null}
      </div>
    </div>
  );
}

/** Merchant webhooks (CONTRACT v2.12): signed, retried event deliveries to the merchant's HTTPS endpoints. */
export function WebhooksPanel({ reloadKey }: { reloadKey: number }) {
  const uid = useId();
  const [hooks, setHooks] = useState<WebhookRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ value: string; title: string } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [logKey, setLogKey] = useState(0);

  const load = useCallback(async () => {
    try {
      const list = await apiFetch<WebhookRead[]>("/api/webhooks");
      setHooks(list);
      setSelected((current) => (current && list.some((h) => h.id === current) ? current : (list[0]?.id ?? null)));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const selectedHook = useMemo(() => hooks.find((h) => h.id === selected) ?? null, [hooks, selected]);
  const reportError = useCallback((message: string) => setError(message), []);

  async function run(id: string, action: () => Promise<void>) {
    setBusy(id);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const testFire = (hook: WebhookRead) =>
    run(hook.id, async () => {
      const delivery = await apiFetch<WebhookDeliveryRead>(`/api/webhooks/${hook.id}/test`, { method: "POST" });
      setSelected(hook.id);
      setLogKey((k) => k + 1);
      setNotice(
        delivery.status === "succeeded"
          ? `Test event delivered (HTTP ${delivery.last_status_code ?? "2xx"}).`
          : `Test event not accepted: ${delivery.last_error ?? "no response"}. It will be retried with backoff.`,
      );
    });

  const toggleActive = (hook: WebhookRead, active: boolean) =>
    run(hook.id, async () => {
      const updated = await apiFetch<WebhookRead>(`/api/webhooks/${hook.id}`, { method: "PATCH", body: { is_active: active } });
      setHooks((list) => list.map((h) => (h.id === updated.id ? updated : h)));
    });

  const rotate = (hook: WebhookRead) =>
    run(hook.id, async () => {
      if (!window.confirm("Rotate the signing secret?\n\nThe current secret stops signing immediately; update your receiver right away.")) return;
      const result = await apiFetch<WebhookCreated>(`/api/webhooks/${hook.id}/rotate-secret`, { method: "POST" });
      setHooks((list) => list.map((h) => (h.id === result.webhook.id ? result.webhook : h)));
      setSecret({ value: result.secret, title: "New webhook signing secret (shown once)" });
    });

  const remove = (hook: WebhookRead) =>
    run(hook.id, async () => {
      if (!window.confirm(`Delete the webhook for ${hook.url}?\n\nIts delivery log is deleted too.`)) return;
      await apiFetch<undefined>(`/api/webhooks/${hook.id}`, { method: "DELETE" });
      await load();
    });

  return (
    <section className="space-y-3" aria-labelledby={`${uid}-heading`}>
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id={`${uid}-heading`} className="text-lg font-medium text-zinc-100">
            Webhooks
          </h2>
          <p className="text-xs text-zinc-500">
            KasGate POSTs signed JSON events to your HTTPS endpoint. Failed deliveries are retried with exponential backoff (30 s up to 1 h, 8 attempts),
            then marked dead; you can redeliver any event. Deliveries never slow down or block payments.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
          <Plus className="h-4 w-4" aria-hidden />
          {showForm ? "Close" : "Add webhook"}
        </button>
      </div>

      <ErrorBanner message={error} />
      {notice ? (
        <p role="status" className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-sm text-zinc-300">
          {notice}
        </p>
      ) : null}
      {secret ? (
        <ApiKeyReveal
          apiKey={secret.value}
          title={secret.title}
          doneLabel="Done"
          copyLabel="Copy secret"
          note="This signing secret is shown only now. Store it in your receiver's secret store; KasGate keeps it encrypted and never shows it again (rotate to get a new one)."
          onDone={() => setSecret(null)}
        />
      ) : null}
      {showForm ? (
        <CreateWebhookForm
          onCreated={(created) => {
            setShowForm(false);
            setHooks((list) => [...list, created.webhook]);
            setSelected(created.webhook.id);
            setSecret({ value: created.secret, title: "Webhook signing secret (shown once)" });
          }}
        />
      ) : null}

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[760px]">
          <thead>
            <tr className="border-b border-zinc-800">
              <th className="th">Endpoint</th>
              <th className="th">Events</th>
              <th className="th">Secret</th>
              <th className="th">Active</th>
              <th className="th text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {hooks.map((hook) => (
              <tr key={hook.id} className={`cursor-pointer hover:bg-zinc-900/60 ${hook.id === selected ? "bg-zinc-900/40" : ""}`} onClick={() => setSelected(hook.id)}>
                <td className="td">
                  <div className="break-all font-mono text-xs text-zinc-200">{hook.url}</div>
                  {hook.description ? <div className="text-xs text-zinc-500">{hook.description}</div> : null}
                </td>
                <td className="td">
                  <div className="flex flex-wrap gap-1">
                    {hook.event_types.map((type) => (
                      <span key={type} className="badge border-zinc-700 bg-zinc-900 font-mono text-zinc-400">
                        {type}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="td font-mono text-xs text-zinc-500">{hook.secret_prefix}…</td>
                <td className="td" onClick={(e) => e.stopPropagation()}>
                  <Toggle id={`${uid}-active-${hook.id}`} checked={hook.is_active} onChange={(next) => void toggleActive(hook, next)} label={hook.is_active ? "On" : "Off"} />
                </td>
                <td className="td text-right" onClick={(e) => e.stopPropagation()}>
                  <div className="flex justify-end gap-1">
                    <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void testFire(hook)} disabled={busy === hook.id}>
                      <Send className="h-3.5 w-3.5" aria-hidden />
                      Send test
                    </button>
                    <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void rotate(hook)} disabled={busy === hook.id}>
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                      Rotate secret
                    </button>
                    <button type="button" className="btn-ghost px-2 py-1 text-xs text-red-300" onClick={() => void remove(hook)} disabled={busy === hook.id} aria-label={`Delete webhook ${hook.url}`}>
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading ? <Spinner label="Loading webhooks…" /> : null}
        {!loading && hooks.length === 0 ? <p className="px-4 py-6 text-center text-sm text-zinc-500">No webhooks yet. Add one to get notified of payments, deposits and trial calls.</p> : null}
      </div>

      {selectedHook ? <DeliveryLog webhook={selectedHook} reloadKey={reloadKey + logKey} onError={reportError} /> : null}

      <details className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-4 py-3 text-sm">
        <summary className="cursor-pointer text-zinc-400">Verify signatures on your server</summary>
        <div className="mt-3 space-y-3 text-xs text-zinc-400">
          <p>
            Every delivery carries <code className="font-mono">X-KasGate-Signature: t=&lt;unix&gt;,v1=&lt;hex&gt;</code>, an HMAC-SHA256 of{" "}
            <code className="font-mono">&quot;&lt;t&gt;.&lt;raw body&gt;&quot;</code> with your secret, plus <code className="font-mono">X-KasGate-Event-Id</code> (dedupe on it) and{" "}
            <code className="font-mono">X-KasGate-Timestamp</code>. Reject signatures older than 5 minutes.
          </p>
          <CodeBlock title="Node.js" code={WEBHOOK_VERIFY_NODE} />
          <CodeBlock title="Python (kasgate-client)" code={WEBHOOK_VERIFY_PYTHON} />
        </div>
      </details>
    </section>
  );
}
