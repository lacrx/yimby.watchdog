# Agent Instructions

This repo is the ETL/pipeline layer for civic monitoring. It scrapes meeting agendas, transcribes video, extracts structured data (JSONL), and manages the transcription backlog. It does NOT generate prose analysis or advocacy intelligence — that lives in `yimby.analysis`.

## Architecture

**Split-mode pipeline:** AWS Lambda scrapes on schedule → writes S3 marker → local machine polls for marker via `extract-watch` → runs `claude -p` extraction via `extract-local`.

**Config:** Jurisdiction-specific settings (city name, council roster, feeds, advocacy lens) live in AWS SSM Parameter Store. Local dev uses `config.local.yaml` fallback. `agencies.yaml` is the structural scraper registry (platforms, URLs, bodies). See `config.py` for the loader.

**Key files:**
- `agencies.yaml` — agency registry (platform, base_url, enabled, bodies, lookback)
- `config.py` — SSM/YAML config loader (cached, ~50 lines)
- `config.local.yaml` — local dev config (gitignored)
- `lambda_handler.py` — Lambda entry point, dispatches by platform
- `extract-watch` — cron-driven S3 marker poller
- `extract-local` — runs `claude -p` extraction, syncs results to S3
- `civic-pipeline` — legacy local orchestrator (still works for manual runs)

## Related Projects
- `yimby.analysis`: downstream analysis — executive summaries, council member profiles, leadership grading. Reads from this repo's `data/` directory
- `stoside.data`: municipal fiscal intelligence, budget/CIP/vote history

## Knowledge Base Repos (upstream, read-only)

Two external repos own all reusable knowledge — articles for context and Claude Code skills for session-level tooling. Fetch articles from them as needed; skills are handled by the Claude Code system, not by pipeline code.

- **`lacrx/policy-knowledge-docs`** — policy articles and skills. Articles: CA housing law enforcement, PRA strategy, fiscal productivity analysis, crash data methodology. Skills: `draft-pra-request`, `fetch-policy-bundle`, `evaluate-crash-study`.
- **`lacrx/agent-knowledge-docs`** — engineering articles and skills. Articles: AWS deployment, Fargate, SDK patterns, testing. Skills: `scaffold-fastapi`, `provision-fargate-task`, `ecr-push-deploy`, etc.

### Fetching Articles

Both KBs use the same discovery flow. Fetch articles for context during planning and implementation — not skills, which are loaded by Claude Code automatically.

```
gh api repos/lacrx/{repo}/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"
```

1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.

### When to Fetch Which

- **Engineering KB** (`agent-knowledge-docs`): infrastructure, deployment, cloud services, testing patterns, frameworks. Fetch once during planning.
- **Policy KB** (`policy-knowledge-docs`): housing law, land use, transit, municipal governance, advocacy, PRA strategy. Fetch **whenever relevant** — during extractions, analysis, drafting, evaluation, or any policy-adjacent work.
- **Skip both**: GIS/spatial analysis, pure data pipeline code that doesn't touch infrastructure or policy.
