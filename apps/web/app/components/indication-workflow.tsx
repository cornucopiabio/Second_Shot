"use client";

import { FormEvent, useMemo, useState } from "react";

import {
  Candidate,
  Match,
  Run,
  TermNode,
  createRun,
  dockRun,
  resolveIndication,
} from "@/lib/api";

type RunSelection = {
  label: string;
  mondo_id: string;
  open_targets_id: string;
};

function downloadRunCsv(run: Run) {
  const headers = [
    "drug",
    "target",
    "action",
    "repurposing_score",
    "mondo_id",
    "disease_id",
    "why",
  ];
  const rows = run.candidates.map((candidate) =>
    [
      candidate.drug,
      candidate.target,
      candidate.action,
      candidate.repurposing_score.toFixed(3),
      run.mondo_id,
      run.disease_id ?? "",
      candidate.why,
    ]
      .map((value) => `"${String(value).replace(/"/g, '""')}"`)
      .join(","),
  );
  const csv = [headers.join(","), ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${run.run_id}-candidates.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function SelectedTermMeta({
  openTargetsId,
  targetCount,
}: {
  openTargetsId: string | null;
  targetCount: number;
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-600">
      {openTargetsId && (
        <span className="rounded-full bg-slate-100 px-2 py-1">
          Open Targets: {openTargetsId}
        </span>
      )}
      <span className="rounded-full bg-slate-100 px-2 py-1">
        Target count: {targetCount}
      </span>
    </div>
  );
}

export default function IndicationWorkflow() {
  const [query, setQuery] = useState("cardiomyopathy");
  const [matches, setMatches] = useState<Match[]>([]);
  const [selectedMondoId, setSelectedMondoId] = useState("");
  const [selectedRefinementId, setSelectedRefinementId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [enableDockingAtStart, setEnableDockingAtStart] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState("");
  const [error, setError] = useState("");

  const selectedMatch = useMemo(() => {
    return matches.find((match) => match.mondo_id === selectedMondoId) ?? null;
  }, [matches, selectedMondoId]);

  const selectedRefinement = useMemo(() => {
    if (!selectedMatch) {
      return null;
    }
    return (
      selectedMatch.refinements.find(
        (refinement) => refinement.mondo_id === selectedRefinementId,
      ) ?? null
    );
  }, [selectedMatch, selectedRefinementId]);

  const selectedTerm = useMemo<RunSelection | null>(() => {
    if (!selectedMatch) {
      return null;
    }

    if (selectedMatch.requires_refinement) {
      if (!selectedRefinement?.open_targets_id) {
        return null;
      }
      return {
        label: selectedRefinement.label,
        mondo_id: selectedRefinement.mondo_id,
        open_targets_id: selectedRefinement.open_targets_id,
      };
    }

    if (!selectedMatch.open_targets_id) {
      return null;
    }

    return {
      label: selectedMatch.label,
      mondo_id: selectedMatch.mondo_id,
      open_targets_id: selectedMatch.open_targets_id,
    };
  }, [selectedMatch, selectedRefinement]);

  const topCandidate: Candidate | null = useMemo(() => {
    return run?.candidates?.[0] ?? null;
  }, [run]);

  async function onResolve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setLoadingMessage(
      "Querying MONDO, checking Open Targets mappings, and assembling subtype suggestions...",
    );
    setError("");

    try {
      const resolved = await resolveIndication(query);
      setMatches(resolved);
      setSelectedMondoId(resolved[0]?.mondo_id ?? "");
      setSelectedRefinementId("");
      setRun(null);
    } catch {
      setError("Could not resolve the indication.");
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }

  async function onRun() {
    if (!selectedMatch) {
      setError("Select a MONDO term first.");
      return;
    }

    if (selectedMatch.requires_refinement && !selectedRefinement) {
      setError("Choose a specific MONDO subtype before starting a run.");
      return;
    }

    if (!selectedTerm) {
      setError("The selected indication could not be normalized to a runnable disease id.");
      return;
    }

    setLoading(true);
    setLoadingMessage(
      "Querying Open Targets, expanding Reactome pathways, and ranking candidate drugs...",
    );
    setError("");

    try {
      const created = await createRun(
        selectedTerm.mondo_id,
        selectedTerm.open_targets_id,
        selectedTerm.label,
        enableDockingAtStart,
      );
      setRun(created);
    } catch {
      setError("Could not start a run.");
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }

  async function onDockTop() {
    if (!run || !topCandidate) {
      return;
    }

    setLoading(true);
    setLoadingMessage("Querying docking scores and updating the final ranking...");
    setError("");

    try {
      const updated = await dockRun(run.run_id, topCandidate.drug, topCandidate.target);
      setRun(updated);
    } catch {
      setError("Docking request failed.");
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }

  return (
    <main className="min-h-screen bg-slate-950 px-6 py-10 text-slate-100">
      <div className="mx-auto max-w-6xl">
        <div className="rounded-3xl border border-slate-800 bg-gradient-to-br from-slate-900 via-slate-900 to-slate-800 p-8 shadow-2xl">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-300">
                Second Shot
              </p>
              <h1 className="mt-2 text-3xl font-semibold text-white">
                Disease-first repurposing workflow
              </h1>
              <p className="mt-3 max-w-3xl text-sm text-slate-300">
                Resolve an indication into a precise MONDO term, normalize it to
                the downstream disease id used by Open Targets, and then launch a
                ranking run with transparent retrieval steps.
              </p>
            </div>
            <div className="rounded-2xl border border-slate-700 bg-slate-900/70 px-4 py-3 text-sm text-slate-300">
              <p className="font-medium text-white">Current flow</p>
              <p className="mt-1">MONDO -&gt; Open Targets -&gt; Reactome -&gt; Ranking</p>
            </div>
          </div>

          <div className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/70 p-6">
            <p className="text-sm text-slate-300">
          Indication-first drug repurposing MVP with deterministic retrieval and
          exact downstream disease normalization.
            </p>

        <form onSubmit={onResolve} className="mt-6 flex flex-col gap-3 sm:flex-row">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Enter indication"
            className="w-full rounded-xl border border-slate-700 bg-slate-950 px-4 py-3 text-slate-100 placeholder:text-slate-500"
          />
          <button
            type="submit"
            disabled={loading}
            className="rounded-xl bg-emerald-500 px-5 py-3 font-medium text-slate-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            Resolve
          </button>
        </form>

        {loadingMessage && (
          <div className="mt-4 rounded-xl border border-blue-900/60 bg-blue-950/40 px-4 py-3 text-sm text-blue-100">
            <p className="font-medium">Working...</p>
            <p className="mt-1 text-blue-200">{loadingMessage}</p>
          </div>
        )}

        {matches.length > 0 && (
          <section className="mt-6">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
              MONDO Matches
            </h2>
            <div className="mt-2 space-y-3">
              {matches.map((match) => (
                <label
                  key={match.mondo_id}
                  className="block cursor-pointer rounded-2xl border border-slate-800 bg-slate-900/80 p-4 transition hover:border-slate-700"
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="radio"
                      name="mondo"
                      value={match.mondo_id}
                      checked={selectedMondoId === match.mondo_id}
                      onChange={() => {
                        setSelectedMondoId(match.mondo_id);
                        setSelectedRefinementId("");
                      }}
                      className="mt-1"
                    />
                    <div className="text-sm">
                      <p className="font-medium text-white">
                        {match.label}{" "}
                        <span className="text-slate-400">({match.mondo_id})</span>
                      </p>
                      {match.synonyms.length > 0 && (
                        <p className="mt-1 text-slate-300">
                          Synonyms: {match.synonyms.slice(0, 3).join(", ")}
                        </p>
                      )}
                      <SelectedTermMeta
                        openTargetsId={match.open_targets_id}
                        targetCount={match.target_count}
                      />
                      <p className="mt-2 text-xs text-slate-400">
                        {match.requires_refinement
                          ? "Refinement required before running."
                          : match.runnable
                            ? "Runnable as selected."
                            : "No supported downstream disease mapping yet."}
                      </p>
                    </div>
                  </div>
                </label>
              ))}
            </div>

            {selectedMatch?.requires_refinement && (
              <div className="mt-6 rounded-2xl border border-emerald-900/60 bg-emerald-950/30 p-4">
                <h3 className="text-sm font-semibold text-emerald-200">
                  Choose a specific subtype
                </h3>
                <p className="mt-1 text-sm text-emerald-100">
                  {selectedMatch.label} is too broad to run directly. Pick a
                  child term with direct Open Targets support.
                </p>
                <div className="mt-3 space-y-2">
                  {selectedMatch.refinements.map((refinement: TermNode) => (
                    <label
                      key={refinement.mondo_id}
                      className="block cursor-pointer rounded-xl border border-emerald-900/60 bg-slate-950/70 p-3"
                    >
                      <div className="flex items-start gap-3">
                        <input
                          type="radio"
                          name="refinement"
                          value={refinement.mondo_id}
                          checked={selectedRefinementId === refinement.mondo_id}
                          onChange={() => setSelectedRefinementId(refinement.mondo_id)}
                          className="mt-1"
                        />
                        <div className="text-sm">
                          <p className="font-medium text-white">
                            {refinement.label}{" "}
                            <span className="text-slate-400">
                              ({refinement.mondo_id})
                            </span>
                          </p>
                          <SelectedTermMeta
                            openTargetsId={refinement.open_targets_id}
                            targetCount={refinement.target_count}
                          />
                        </div>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={enableDockingAtStart}
                  onChange={(event) => setEnableDockingAtStart(event.target.checked)}
                />
                Enable docking in base run
              </label>
              <button
                type="button"
                onClick={onRun}
                disabled={loading || !selectedTerm}
                className="rounded-xl bg-emerald-500 px-4 py-2 font-medium text-slate-950 transition hover:bg-emerald-400 disabled:opacity-50"
              >
                Start Run
              </button>
            </div>
          </section>
        )}

        {error && (
          <p className="mt-4 rounded-xl border border-rose-900/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-200">
            {error}
          </p>
        )}

        {run && (
          <section className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-white">Run {run.run_id}</h2>
                <p className="text-sm text-slate-300">
                  Status: {run.status} | Stage: {run.stage}
                </p>
                {run.disease_id && (
                  <p className="text-sm text-slate-300">
                    Downstream disease id: {run.disease_id}
                  </p>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {run.disease_id && (
                  <a
                    href={`https://platform.opentargets.org/search?q=${encodeURIComponent(run.disease_id)}`}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-xl border border-slate-700 px-3 py-2 text-sm text-slate-200 transition hover:bg-slate-800"
                  >
                    Open Targets Search
                  </a>
                )}
                <button
                  type="button"
                  onClick={() => downloadRunCsv(run)}
                  className="rounded-xl border border-slate-700 px-3 py-2 text-sm text-slate-200 transition hover:bg-slate-800"
                >
                  Export CSV
                </button>
                {topCandidate && (
                  <button
                    type="button"
                    onClick={onDockTop}
                    disabled={loading}
                    className="rounded-xl border border-slate-700 px-3 py-2 text-sm text-slate-200 transition hover:bg-slate-800 disabled:opacity-50"
                  >
                    Dock Top Candidate
                  </button>
                )}
              </div>
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-2">
              {run.candidates.map((candidate) => (
                <article
                  key={`${candidate.drug}-${candidate.target}`}
                  className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"
                >
                  <p className="font-medium text-white">
                    {candidate.drug} -&gt; {candidate.target}
                  </p>
                  <p className="text-sm text-slate-300">Action: {candidate.action}</p>
                  <p className="text-sm text-slate-300">
                    Score: {candidate.repurposing_score.toFixed(3)}
                  </p>
                  <p className="mt-1 text-sm text-slate-200">{candidate.why}</p>
                </article>
              ))}
            </div>

            <div className="mt-4 rounded-xl border border-amber-900/60 bg-amber-950/30 p-4 text-sm text-amber-100">
              <p className="font-medium">Limitations</p>
              <ul className="mt-1 list-disc pl-5">
                {run.limitations.map((item, index) => (
                  <li key={`${index}-${item}`}>{item}</li>
                ))}
              </ul>
            </div>
          </section>
        )}
          </div>
      </div>
      </div>
    </main>
  );
}
