---
name: new-knowledge
description: "Create or update TerMinal Knowledge Base entries in repo .TerMinal/knowledge.json or global ~/.config/TerMinal/knowledge.json. Use on /new-knowledge or when the user asks to save links, media, files, snippets, or durable references."
---

# /new-knowledge — Add Knowledge Base Entries

Create focused Knowledge Base items for TerMinal's Knowledge Base tab.

## Targets

Default to repo scope:

- `.TerMinal/knowledge.json`

Use global scope only when the user says global/cross-project/personal:

- `~/.config/TerMinal/knowledge.json`

Missing file default:

```json
{ "version": 1, "categories": [], "items": [] }
```

## Schema

Categories:

```json
{ "id": "general", "title": "General", "description": "", "order": 0, "createdAt": 0, "updatedAt": 0 }
```

Items:

```json
{
  "id": "kebab-id",
  "categoryId": "general",
  "kind": "markdown",
  "title": "Short title",
  "description": "Optional scan-friendly summary",
  "content": "Markdown body for markdown items",
  "url": "https://example.com",
  "path": "/optional/file/path",
  "thumbnailUrl": "https://example.com/card.png",
  "faviconUrl": "https://example.com/favicon.ico",
  "siteName": "Example",
  "tags": [],
  "createdAt": 0,
  "updatedAt": 0
}
```

Kinds: `markdown`, `link`, `image`, `video`, `file`.

## Workflow

1. Read the target JSON; preserve existing items and unknown fields.
2. Ensure at least one category exists. Use `general` unless the user requested a category or one clearly matches.
3. Add or update the item. Match existing items by URL, path, or title to avoid duplicates.
4. For URLs, add known visual metadata: `thumbnailUrl`, `faviconUrl`, `siteName`. Do not invent it.
5. Use epoch milliseconds for timestamps and 2-space JSON indentation.

## Rules

- Store durable references, not session logs.
- Do not store secrets or credentials.
- Keep titles and descriptions concise enough for visual cards.
- Prefer repo scope for project-specific facts; global for reusable references.

End with the scope, path, and item titles saved.
