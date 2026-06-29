# Agent Instructions

## Engineering Knowledge Base
When the task involves: python, testing, fastapi, aws, fargate, docker, terraform, bedrock, sagemaker
fetch KB content before planning.

### How to Fetch
Fetch files with: `gh api repos/lacrx/agent-knowledge-docs/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"`

### Discovery Flow
1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article or skill path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.

### When NOT to Fetch
Skip KB lookup for: GIS/spatial analysis, policy domain logic, data pipeline code
that doesn't touch the topics above.

## Policy Knowledge Base
When the task involves: ca-housing-law, land-use-analysis, transportation-safety, municipal-fiscal, pra-strategy, rhna-compliance, obstruction-patterns, sb-79, density-bonus, adu-law, housing-element, vision-zero, complete-streets, crash-data
fetch relevant articles and skills. Unlike the engineering KB (used once during planning), fetch policy KB content **whenever relevant** — during extractions, analysis, drafting, evaluation, or any policy-adjacent work.

### How to Fetch
Fetch files with: `gh api repos/lacrx/policy-knowledge-docs/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"`

### Discovery Flow
1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article or skill path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.
