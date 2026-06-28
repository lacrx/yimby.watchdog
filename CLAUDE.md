# Agent Instructions

## Knowledge Base
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
