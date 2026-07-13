---
name: knowledge
description: "Capture durable project or global knowledge into TerMinal's Knowledge Base. Use on /knowledge, when saving links/media/snippets/references, or when the user asks to remember useful context visually."
---

# /knowledge — Capture Knowledge Base Items

Save durable references into TerMinal's Knowledge Base so they show up in the app's Knowledge Base tab.

## Target Files

Default to the current repo:

- `.TerMinal/knowledge.json`

If the user explicitly asks for global knowledge, write:

- `~/.config/TerMinal/knowledge.json`

Treat a missing file as:

```json
{
  "version": 1,
  "categories": [
    {
      "id": "general",
      "title": "General",
      "description": "Links, notes, and references that do not need a dedicated category yet.",
      "order": 0,
      "createdAt": 0,
      "updatedAt": 0
    }
  ],
  "items": []
}
```

## Item Schema

```json
{
  "id": "kebab-case-id",
  "categoryId": "general",
  "kind": "markdown",
  "title": "Short title",
  "description": "Optional summary",
  "content": "Markdown body for markdown items",
  "url": "https://example.com",
  "path": "/optional/local/path",
  "thumbnailUrl": "https://example.com/card.png",
  "faviconUrl": "https://example.com/favicon.ico",
  "siteName": "Example",
  "tags": ["tag"],
  "createdAt": 0,
  "updatedAt": 0
}
```

Allowed `kind` values:

- `markdown` for snippets, playbooks, findings, and compact notes.
- `link` for URLs, dashboards, issues, docs, repos, posts, or product pages.
- `image` for screenshots, diagrams, design refs, or remote/local images.
- `video` for YouTube, Vimeo, or direct videos.
- `file` for local files or remote documents.

## Workflow

1. Decide repo vs global scope. Prefer repo scope for project-specific context and global scope only for reusable cross-project knowledge.
2. Read the target JSON and preserve unknown fields.
3. Pick or create a category. Use `general` unless a clearer category already exists or the user requested one.
4. Upsert one or more focused items. Prefer updating an existing matching URL/path/title instead of duplicating it.
5. For links, include visual metadata when known: `thumbnailUrl`, `faviconUrl`, and `siteName`.
6. Use epoch milliseconds for `createdAt` and `updatedAt`.
7. Write pretty JSON with 2-space indentation.

## Rules

- Do not store secrets, private tokens, or pasted credentials.
- Keep descriptions short enough to scan in a visual card.
- For markdown items, keep `content` useful without needing surrounding chat history.
- For links, preserve the original URL and add thumbnails only when you can determine them confidently.
- Do not fabricate thumbnails or metadata. Leave fields blank if unknown.
- Do not replace the user's existing categories or items.

End with the path updated and the item titles captured.
