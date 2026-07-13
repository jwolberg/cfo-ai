---
name: knowledge-rag
description: Add or update TerMinal Knowledge Base entries backed by the lyonzin/knowledge-rag local RAG MCP server. Use when the user wants category-specific RAG knowledge, semantic/vector search over docs, URL/document ingestion into the Knowledge Base, or help configuring the Knowledge Base tab's RAG item type.
---

# Knowledge RAG for TerMinal

TerMinal supports two Knowledge Base modes:

- Traditional items: `markdown`, `link`, `image`, `video`, `file`.
- RAG items: `rag`, backed by the upstream `lyonzin/knowledge-rag` MCP server and a local documents/vector workspace.

Use this skill when adding searchable, category-specific knowledge that should be indexed instead of stored only as a note/link.

## Storage

Knowledge Base JSON lives at:

- Repo scope: `<repo>/.TerMinal/knowledge.json`
- Global scope: `~/.config/TerMinal/knowledge.json`

RAG workspaces default to:

- Repo scope: `<repo>/.TerMinal/knowledge-rag/<category-or-title>/`
- Global scope: `~/.config/TerMinal/knowledge-rag/<category-or-title>/`

Each workspace contains `config.yaml`, `documents/`, `data/`, and `models_cache/`. TerMinal creates these on first status/search/reindex/add action.

## RAG item shape

Add a normal category first, then add an item like:

```json
{
  "id": "research-rag",
  "categoryId": "research",
  "kind": "rag",
  "title": "Research RAG",
  "description": "Semantic search over research notes and imported docs.",
  "rag": {
    "command": "uvx",
    "args": ["--python", "3.11", "knowledge-rag==3.9.0"],
    "category": "research",
    "hybridAlpha": 0.3,
    "maxResults": 5
  },
  "tags": [],
  "createdAt": 0,
  "updatedAt": 0
}
```

Only set `rag.rootDir` when the user wants a custom workspace. Otherwise let TerMinal choose the default path.

## Workflow

1. Read the current KB JSON through the app if available, or edit the JSON path directly.
2. Preserve existing categories and items. Do not rewrite unrelated entries.
3. Add or update one `rag` item per category that should have vector-backed search.
4. In TerMinal, open Knowledge Base, select the RAG item, then use:
   - `Status` to create/check the workspace.
   - `Add text` for pasted markdown/text.
   - `Add URL` for web pages.
   - `Reindex` after manually adding files under `documents/`.
   - `Search` to query the local index.
5. For first-time setup, expect the configured `uvx --python 3.11 knowledge-rag==3.9.0` command to install the upstream package and managed Python environment.

## Notes

- `knowledge-rag` is an MCP/package repo, not a Codex `SKILL.md` skill. This skill is the TerMinal wrapper for using it.
- Keep `command` and `args` explicit and pinned unless the user asks to change versions.
- If `uvx` is unavailable, an item can use the upstream NPM wrapper instead: `command: "bunx"`, `args: ["knowledge-rag@3.9.0"]`; that path requires Python 3.11+ already on PATH.
- RAG data is local. Do not commit generated `data/` or `models_cache/` directories unless the repo explicitly wants that.
