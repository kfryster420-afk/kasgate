"use client";

import { Ban, ExternalLink, FileCode2, Pencil, Plus, RotateCcw, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { listedPrice } from "@/lib/format";
import type { EndpointRead, EndpointUpdate } from "@/lib/types";
import { CopyButton } from "./copy-button";
import { EndpointForm } from "./endpoint-form";
import { OpenApiImport } from "./openapi-import";
import { ErrorBanner, StatusBadge } from "./ui";

interface Props {
  endpoints: EndpointRead[];
  onChanged: () => void;
  onUnauthorized: () => void;
}

export function EndpointsPanel({ endpoints, onChanged, onUnauthorized }: Props) {
  const [editing, setEditing] = useState<EndpointRead | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);

  function handle(e: unknown, target: "list" | "form") {
    if (e instanceof ApiError && e.status === 401) {
      onUnauthorized();
      return;
    }
    if (target === "form") setFormError(errorMessage(e));
    else setError(errorMessage(e));
  }

  async function save(endpoint: EndpointRead, payload: EndpointUpdate) {
    setBusyId(endpoint.id);
    setFormError(null);
    try {
      await apiFetch<EndpointRead>(`/api/endpoints/${endpoint.id}`, { method: "PATCH", body: payload });
      setEditing(null);
      onChanged();
    } catch (e) {
      handle(e, "form");
    } finally {
      setBusyId(null);
    }
  }

  async function deactivate(endpoint: EndpointRead) {
    if (!window.confirm(`Deactivate "${endpoint.name}"? Buyers will receive 404 until you reactivate it.`)) return;
    setBusyId(endpoint.id);
    setError(null);
    try {
      await apiFetch<undefined>(`/api/endpoints/${endpoint.id}`, { method: "DELETE" });
      onChanged();
    } catch (e) {
      handle(e, "list");
    } finally {
      setBusyId(null);
    }
  }

  async function reactivate(endpoint: EndpointRead) {
    setBusyId(endpoint.id);
    setError(null);
    try {
      await apiFetch<EndpointRead>(`/api/endpoints/${endpoint.id}`, { method: "PATCH", body: { is_active: true } satisfies EndpointUpdate });
      onChanged();
    } catch (e) {
      handle(e, "list");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="space-y-3" aria-labelledby="endpoints-heading">
      <div className="flex items-center justify-between">
        <h2 id="endpoints-heading" className="text-lg font-medium text-zinc-100">
          Endpoints
        </h2>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-secondary" onClick={() => setImporting(true)}>
            <FileCode2 className="h-4 w-4" aria-hidden />
            Import from OpenAPI
          </button>
          <Link href="/onboarding" className="btn-secondary">
            <Plus className="h-4 w-4" aria-hidden />
            New endpoint
          </Link>
        </div>
      </div>
      <ErrorBanner message={error} />
      {endpoints.length === 0 ? (
        <div className="card p-8 text-center text-sm text-zinc-400">
          No endpoints yet.{" "}
          <Link href="/onboarding" className="text-kas-400 hover:underline">
            Register your first API
          </Link>
          .
        </div>
      ) : (
        <ul className="grid gap-3">
          {endpoints.map((endpoint) => (
            <li key={endpoint.id} className="card p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-medium text-zinc-100">{endpoint.name}</h3>
                    <StatusBadge status={endpoint.is_active ? "active" : "inactive"} />
                    {endpoint.is_public_listed ? <span className="badge border-zinc-700 bg-zinc-900 text-zinc-400">listed</span> : null}
                    {endpoint.prepaid_only ? <span className="badge border-kas-800 bg-kas-500/10 text-kas-300">prepaid only</span> : null}
                    {endpoint.free_trial_calls > 0 ? (
                      <span className="badge border-emerald-800 bg-emerald-500/10 text-emerald-300" title={`${endpoint.trial_daily_cap} trial calls per day across all buyers`}>
                        {endpoint.free_trial_calls} free {endpoint.free_trial_calls === 1 ? "call" : "calls"}
                      </span>
                    ) : null}
                    {endpoint.category ? <span className="badge border-zinc-700 bg-zinc-900 text-zinc-400">{endpoint.category}</span> : null}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <code className="max-w-full overflow-x-auto rounded border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-xs text-kas-300">{endpoint.proxy_url}</code>
                    <CopyButton value={endpoint.proxy_url} label="Copy" />
                  </div>
                  <p className="mt-2 flex items-center gap-1 truncate font-mono text-xs text-zinc-500">
                    <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
                    {endpoint.upstream_url}
                  </p>
                </div>
                <div className="text-right">
                  <div className="font-mono text-lg font-semibold text-zinc-100">{listedPrice(endpoint).primary}</div>
                  <div className="font-mono text-xs text-zinc-500">{listedPrice(endpoint).secondary} / call</div>
                  <div className="mt-1 font-mono text-xs text-zinc-500">{endpoint.allowed_methods.join(" · ")}</div>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2 border-t border-zinc-800 pt-3">
                <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => { setFormError(null); setEditing(endpoint); }} disabled={busyId === endpoint.id}>
                  <Pencil className="h-3.5 w-3.5" aria-hidden />
                  Edit
                </button>
                {endpoint.is_active ? (
                  <button type="button" className="btn-danger px-3 py-1.5 text-xs" onClick={() => void deactivate(endpoint)} disabled={busyId === endpoint.id}>
                    <Ban className="h-3.5 w-3.5" aria-hidden />
                    Deactivate
                  </button>
                ) : (
                  <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void reactivate(endpoint)} disabled={busyId === endpoint.id}>
                    <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                    Reactivate
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {editing ? (
        <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label={`Edit ${editing.name}`}>
          <div className="card mt-8 w-full max-w-2xl bg-zinc-950 p-6">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-lg font-medium text-zinc-100">Edit endpoint</h3>
              <button type="button" className="btn-ghost px-2 py-1" onClick={() => setEditing(null)} aria-label="Close">
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>
            <EndpointForm
              key={editing.id}
              mode="edit"
              initial={editing}
              busy={busyId === editing.id}
              error={formError}
              onCancel={() => setEditing(null)}
              onSubmit={(payload) => void save(editing, payload)}
            />
          </div>
        </div>
      ) : null}

      {importing ? <OpenApiImport onClose={() => setImporting(false)} onImported={onChanged} onUnauthorized={onUnauthorized} /> : null}
    </section>
  );
}
