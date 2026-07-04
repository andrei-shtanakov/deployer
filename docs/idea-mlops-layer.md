---
name: idea-mlops-layer
description: "Direction — AI apps need an MLOps layer above DevOps; design pluggable seams now, don't pre-build the platform"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5533d8be-b0fc-4868-b62b-f57b499389e9
---

Andrei's framing (2026-07-03): for AI apps, DevOps (CI/CD, IaC, containers) is the substrate; MLOps sits on top — experiment tracking (many models/hyperparams/datasets), model registry + staged promotion (staging→prod), scheduled/event-driven retraining on drift, offline eval (holdout/benchmark) + online experiments (A/B, shadow, canary) by quality KPIs. Key truth: models are non-deterministic — green pipelines ≠ good model; a model can degrade on new data.

My assessment:
- Framing is correct, but the **big fork**: "app that TRAINS/fine-tunes models" vs "app that CONSUMES foundation models via API". Most AI apps (incl. the voice-bot hiring example) are consumers — they need prompt/chain versioning, eval, online experiments, cost/quality routing — NOT MLflow/SageMaker/retraining. Full MLOps stack applies only if you own/fine-tune weights. Don't over-provision.
- **Ecosystem already has the offline half**: ATP = offline eval (benchmark_runs, LLM-judge); [[project-model-lifecycle-adr-003a]] = discovery→benchmark-gated promotion (no autobump); arbiter = routing from test results ([[ecosystem-arbiter-loop-status]]). Real GAP = online experiments (shadow/canary/A-B in prod), live drift monitoring, retrain-trigger loop.
- Promotion gates must be **statistical** (metric thresholds + confidence, not binary pass/fail); rollout **progressive** (canary + auto-rollback on KPI regression). Different mental model from DevOps deploy gates.
- "Изначально учитывать" = design **pluggable seams** (eval hooks, promotion criteria as declarative intent, deploy_target) so MLOps can attach later — NOT build the platform on day one (YAGNI). Same "intent not mechanism" principle as the deploy discussion. Relates to [[idea-deployer-subproject]].
