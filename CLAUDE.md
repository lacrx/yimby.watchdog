# Agent Instructions

## Engineering Knowledge Base
For engineering tasks (infrastructure, deployment, testing patterns, cloud services, frameworks), fetch KB content before planning.

### How to Fetch
Fetch files with: `gh api repos/lacrx/agent-knowledge-docs/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"`

### Discovery Flow
1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article or skill path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.

### When NOT to Fetch
Skip KB lookup for: GIS/spatial analysis, policy domain logic, data pipeline code that doesn't touch engineering infrastructure.

## Policy Knowledge Base
For policy-adjacent work (housing law, land use, transit, municipal governance, advocacy, PRA strategy), fetch relevant articles and skills. Unlike the engineering KB (used once during planning), fetch policy KB content **whenever relevant** — during extractions, analysis, drafting, evaluation, or any policy-adjacent work.

### How to Fetch
Fetch files with: `gh api repos/lacrx/policy-knowledge-docs/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"`

### Discovery Flow
1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article or skill path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.
