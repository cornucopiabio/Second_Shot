from __future__ import annotations

import json
import os
from typing import Any

import httpx


class AnthropicRanker:
    def __init__(self) -> None:
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.timeout = float(os.getenv("API_TIMEOUT_SECONDS", "8")) + 4

    def rerank(
        self,
        mondo_id: str,
        targets: list[dict[str, Any]],
        pathways: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not candidates:
            return candidates, None

        if not self.api_key:
            return candidates, "Anthropic unavailable: ANTHROPIC_API_KEY not configured; using deterministic ranking only."

        payload = {
            "disease": {"mondo_id": mondo_id},
            "targets": targets[:15],
            "pathways": pathways[:10],
            "candidates": [
                {
                    "drug": c["drug"],
                    "target": c["target"],
                    "action": c.get("action"),
                    "repurposing_score": c["repurposing_score"],
                    "score_breakdown": c.get("score_breakdown", {}),
                }
                for c in candidates
            ],
        }

        prompt = (
            "You are the Drug Repurposing Agent. Re-rank the candidates and return strict JSON only. "
            "Keep all candidates. Provide concise mechanism-alignment rationale and an optional score_delta in range [-0.05, 0.05]. "
            "Output schema: {\"ranked\":[{\"drug\":str,\"target\":str,\"why\":str,\"score_delta\":number}]}.\n"
            f"Input:\n{json.dumps(payload)}"
        )

        body = {
            "model": self.model,
            "max_tokens": 1400,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
        }

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        try:
            response = httpx.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=body,
                timeout=self.timeout,
            )
            response.raise_for_status()
            response_payload = response.json()

            content = response_payload.get("content", [])
            if not isinstance(content, list):
                return candidates, "Anthropic response parse warning: invalid content shape; using deterministic ranking."

            text_chunks = [item.get("text", "") for item in content if isinstance(item, dict)]
            text = "\n".join(chunk for chunk in text_chunks if chunk).strip()
            if not text:
                return candidates, "Anthropic response parse warning: empty message content; using deterministic ranking."

            start_idx = text.find("{")
            end_idx = text.rfind("}")
            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                return candidates, "Anthropic response parse warning: no JSON object detected; using deterministic ranking."

            parsed = json.loads(text[start_idx : end_idx + 1])
            ranked = parsed.get("ranked")
            if not isinstance(ranked, list):
                return candidates, "Anthropic response parse warning: ranked list missing; using deterministic ranking."

            candidate_map = {(c["drug"], c["target"]): dict(c) for c in candidates}
            merged: list[dict[str, Any]] = []

            for item in ranked:
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("drug", "")), str(item.get("target", "")))
                if key not in candidate_map:
                    continue

                candidate = candidate_map.pop(key)
                rationale = str(item.get("why", "")).strip()
                if rationale:
                    candidate["why"] = rationale

                delta = item.get("score_delta", 0)
                try:
                    delta_value = max(-0.05, min(0.05, float(delta)))
                except (TypeError, ValueError):
                    delta_value = 0.0

                candidate["repurposing_score"] = round(
                    max(0.0, min(1.0, candidate["repurposing_score"] + delta_value)), 3
                )
                merged.append(candidate)

            if candidate_map:
                merged.extend(candidate_map.values())

            merged.sort(key=lambda c: c["repurposing_score"], reverse=True)
            return merged, None
        except Exception as error:  # noqa: BLE001 - resilience over strict failure
            return candidates, f"Anthropic unavailable ({error.__class__.__name__}); using deterministic ranking."


class TamarindDockingClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("TAMARIND_API_KEY", "").strip()
        self.base_url = os.getenv("TAMARIND_BASE_URL", "").rstrip("/")
        self.timeout = float(os.getenv("API_TIMEOUT_SECONDS", "8")) + 15

    def dock_pairs(
        self, pairs: list[dict[str, str]]
    ) -> tuple[dict[tuple[str, str], float], str | None]:
        if not pairs:
            return {}, None

        if not self.api_key or not self.base_url:
            return {}, "Tamarind unavailable: TAMARIND_API_KEY or TAMARIND_BASE_URL not configured; using simulated docking score."

        endpoint = f"{self.base_url}/dock"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "content-type": "application/json",
        }

        body = {"pairs": pairs}

        try:
            response = httpx.post(endpoint, headers=headers, json=body, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()

            raw_results: list[Any]
            if isinstance(payload, dict):
                raw_results = payload.get("results") or payload.get("data") or []
            elif isinstance(payload, list):
                raw_results = payload
            else:
                raw_results = []

            parsed_scores: dict[tuple[str, str], float] = {}
            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                drug = str(item.get("drug", "")).strip()
                target = str(item.get("target", "")).strip()
                if not drug or not target:
                    continue

                raw_score = item.get("confidence", item.get("score", item.get("plausibility", 0.0)))
                try:
                    score = max(0.0, min(1.0, float(raw_score)))
                except (TypeError, ValueError):
                    continue

                parsed_scores[(drug, target)] = round(score, 3)

            if not parsed_scores:
                return {}, "Tamarind response parse warning: no usable docking scores; using simulated docking score."

            return parsed_scores, None
        except Exception as error:  # noqa: BLE001 - resilience over strict failure
            return {}, f"Tamarind unavailable ({error.__class__.__name__}); using simulated docking score."
