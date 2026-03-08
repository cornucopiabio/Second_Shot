"use client";

import { FormEvent, useMemo, useState } from "react";

import {
  Candidate,
  Match,
  Run,
  createRun,
  dockRun,
  resolveIndication,
} from "@/lib/api";

export default function Home() {
  const [query, setQuery] = useState("lung fibrosis");
  const [matches, setMatches] = useState<Match[]>([]);
  const [selectedMondoId, setSelectedMondoId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [enableDockingAtStart, setEnableDockingAtStart] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const topCandidate: Candidate | null = useMemo(() => {
    return run?.candidates?.[0] ?? null;
  }, [run]);

  async function onResolve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError("");

    try {
      const resolved = await resolveIndication(query);
      setMatches(resolved);
      setSelectedMondoId(resolved[0]?.mondo_id ?? "");
      setRun(null);
    } catch {
      setError("Could not resolve the indication.");
    } finally {
      setLoading(false);
    }
  }

  async function onRun() {
    if (!selectedMondoId) {
      setError("Select a MONDO term first.");
      return;
    }

    setLoading(true);
    setError("");

    try {
      const created = await createRun(selectedMondoId, enableDockingAtStart);
      setRun(created);
    } catch {
      setError("Could not start a run.");
    } finally {
      setLoading(false);
    }
  }

  async function onDockTop() {
    if (!run || !topCandidate) return;

    setLoading(true);
    setError("");

    try {
      const updated = await dockRun(run.run_id, topCandidate.drug, topCandidate.target);
      setRun(updated);
    } catch {
      setError("Docking request failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-2xl font-semibold">Second Shot</h1>
        <p className="mt-2 text-sm text-slate-600">
          Indication-first drug repurposing MVP with deterministic retrieval and
          optional docking.
        </p>

        <form onSubmit={onResolve} className="mt-6 flex gap-3">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Enter indication"
            className="w-full rounded-lg border border-slate-300 px-3 py-2"
          />
          <button
            type="submit"
            disabled={loading}
            className="rounded-lg bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
          >
            Resolve
          </button>
        </form>

        {matches.length > 0 && (
          <div className="mt-6">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
              MONDO Matches
            </h2>
            <div className="mt-2 space-y-2">
              {matches.map((match) => (
                <label
                  key={match.mondo_id}
                  className="flex cursor-pointer items-center gap-3 rounded-lg border border-slate-200 p-3"
                >
                  <input
                    type="radio"
                    name="mondo"
                    value={match.mondo_id}
                    checked={selectedMondoId === match.mondo_id}
                    onChange={() => setSelectedMondoId(match.mondo_id)}
                  />
                  <span className="text-sm">
                    {match.label} <span className="text-slate-500">({match.mondo_id})</span>
                  </span>
                </label>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3">
              <label className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={enableDockingAtStart}
                  onChange={(e) => setEnableDockingAtStart(e.target.checked)}
                />
                Enable docking in base run
              </label>
              <button
                type="button"
                onClick={onRun}
                disabled={loading}
                className="rounded-lg bg-emerald-600 px-4 py-2 text-white disabled:opacity-50"
              >
                Start Run
              </button>
            </div>
          </div>
        )}

        {error && (
          <p className="mt-4 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        {run && (
          <section className="mt-8 rounded-xl border border-slate-200 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Run {run.run_id}</h2>
                <p className="text-sm text-slate-600">
                  Status: {run.status} | Stage: {run.stage}
                </p>
              </div>
              {topCandidate && (
                <button
                  type="button"
                  onClick={onDockTop}
                  disabled={loading}
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:opacity-50"
                >
                  Dock Top Candidate
                </button>
              )}
            </div>

            <div className="mt-4 grid gap-3">
              {run.candidates.map((candidate) => (
                <article
                  key={`${candidate.drug}-${candidate.target}`}
                  className="rounded-lg border border-slate-200 p-3"
                >
                  <p className="font-medium">
                    {candidate.drug} -> {candidate.target}
                  </p>
                  <p className="text-sm text-slate-600">Action: {candidate.action}</p>
                  <p className="text-sm text-slate-600">
                    Score: {candidate.repurposing_score.toFixed(3)}
                  </p>
                  <p className="mt-1 text-sm">{candidate.why}</p>
                </article>
              ))}
            </div>

            <div className="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-800">
              <p className="font-medium">Limitations</p>
              <ul className="mt-1 list-disc pl-5">
                {run.limitations.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
