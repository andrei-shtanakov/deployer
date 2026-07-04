---
name: idea-deployer-subproject
description: "Future-direction idea — a deploy AI-agent subproject; author-not-execute, arbiter-gated, MCP for plan/read only"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5533d8be-b0fc-4868-b62b-f57b499389e9
---

Idea (Andrei, 2026-07-03): ecosystem has no deploy layer (only ATP self-deploys). Add a deploy-oriented AI-agent capability that drives various environments (docker/podman, cloud, cluster, GPU) incl. via MCP.

Agreed design constraint: **authoring ≠ execution.**
- Agent does authoring: generate/fix Dockerfile, CI workflows, Helm, Terraform from project + `deploy_target` intent; diagnose failed CI. High value, low risk (just files in a PR).
- Execution stays deterministic: real CI/IaC applies artifacts. MCP used for read/plan/dry-run only; mutating actions gated by **arbiter policy** + human approval on prod. Never "agent runs `terraform apply` autonomously."

Ecosystem overlap to reuse rather than reinvent:
- **proctor-a already planned this**: `infra/` (Phase 3 — Docker SDK, asyncssh, Vagrant, Ansible, tmux) + `mcp/` (Phase 3) in `proctor-a/CLAUDE.md`. Deploy agent ≈ proctor worker with infra-MCP tools.
- **arbiter** = policy/guardrail engine → gates which deploy actions are allowed.
- **ATP** = validation/smoke-test of built artifacts.
- **Maestro** = deploy-agent can be a workstream/spawner type, not a whole new universe.

Recommendation: don't spawn yet-another orphan repo (cf. [[project-new-unregistered-repos-2026-05]], [[project-repo-topology-decision]]). Either revive proctor-a `infra/`+`mcp/`, or a narrow new `deployer` repo (deploy-author + deploy-exec) registered in COWORK_CONTEXT.md + CI from day one. Relates to [[idea-mlops-layer]].
