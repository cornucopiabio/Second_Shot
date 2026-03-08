# Systems Design Diagram

## Component Architecture

```mermaid
flowchart LR
    U[User Browser] --> WEB[Next.js Web App]
    WEB --> API[FastAPI Orchestrator]

    API --> MONDO[MONDO Resolver Service]
    API --> OT[Open Targets Service]
    API --> REACTOME[Reactome Service]
    API --> DRUGS[Drug Retrieval Service]
    API --> AGENTS[Agent Consortium]
    API --> DOCK[Docking Service (Optional)]

    API --> PG[(Postgres)]
    API --> REDIS[(Redis Cache/Jobs)]
    API --> OBJ[(Artifact Storage)]

    AGENTS --> LLM[LLM Provider]
    DOCK --> OBJ
```

## Runtime Sequence

```mermaid
sequenceDiagram
    participant U as User
    participant W as Web UI
    participant A as API
    participant M as MONDO
    participant O as Open Targets
    participant R as Reactome
    participant D as Drug Service
    participant G as Agent Consortium
    participant K as Docking (Optional)
    participant S as Storage

    U->>W: Enter indication text
    W->>A: POST /resolve-indication
    A->>M: Resolve to MONDO terms
    M-->>A: Candidate ontology matches
    A-->>W: Ranked matches

    U->>W: Select MONDO and start run
    W->>A: POST /runs
    A->>O: Fetch target-disease evidence
    O-->>A: Targets + scores
    A->>R: Pathway expansion
    R-->>A: Pathways + nodes
    A->>D: Candidate drug retrieval
    D-->>A: Drugs + MOA + repurposability signals
    A->>G: Agent A/B/C reasoning and ranking
    G-->>A: Ranked candidates + explanations
    A->>S: Persist run artifacts
    A-->>W: Run status and top candidates

    opt Docking requested
        W->>A: POST /runs/{id}/dock
        A->>K: Dock top pair(s)
        K-->>A: Structural plausibility signal
        A->>G: Agent D interpretation
        G-->>A: Confidence adjustment
        A->>S: Persist docking artifacts
        A-->>W: Updated ranking
    end
```
