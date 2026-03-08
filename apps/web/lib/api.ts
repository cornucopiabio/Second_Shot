export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type Match = {
  label: string;
  mondo_id: string;
};

export type Candidate = {
  drug: string;
  target: string;
  action: string;
  repurposing_score: number;
  why: string;
  score_breakdown: {
    disease_target_relevance: number;
    pathway_intervention_fit: number;
    mechanism_directionality_fit: number;
    structural_plausibility: number | null;
    repurposability_score: number;
  };
};

export type Run = {
  run_id: string;
  status: string;
  stage: string;
  mondo_id: string;
  docking_enabled: boolean;
  candidates: Candidate[];
  limitations: string[];
};

export async function resolveIndication(query: string): Promise<Match[]> {
  const response = await fetch(`${API_BASE_URL}/resolve-indication`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!response.ok) throw new Error("Failed to resolve indication");
  const data = await response.json();
  return data.matches;
}

export async function createRun(mondoId: string, enableDocking: boolean): Promise<Run> {
  const response = await fetch(`${API_BASE_URL}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mondo_id: mondoId, top_k: 20, enable_docking: enableDocking }),
  });
  if (!response.ok) throw new Error("Failed to create run");
  return response.json();
}

export async function dockRun(runId: string, drug: string, target: string): Promise<Run> {
  const response = await fetch(`${API_BASE_URL}/runs/${runId}/dock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pairs: [{ drug, target }] }),
  });
  if (!response.ok) throw new Error("Failed to dock run");
  return response.json();
}
