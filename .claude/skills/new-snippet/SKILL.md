---
name: new-snippet
description: "Create or update a TerMinal quick snippet (repo .TerMinal/snippets.json or global). Use on /new-snippet or wanting a reusable quick prompt / one-click launcher item."
---

# /new-snippet — Create a TerMinal Quick Snippet

Create one reusable prompt snippet for TerMinal's terminal quick launcher.

## Target Files

Default target is the current repo:

- `.TerMinal/snippets.json`

If the user explicitly asks for a global snippet, write:

- `~/.config/TerMinal/snippets.json`

Treat a missing file as:

```json
{ "version": 2, "snippets": [] }
```

## Schema

```json
{
  "version": 2,
  "snippets": [
    {
      "id": "kebab-case",
      "title": "Short label",
      "group": "Common",
      "description": "Optional one-line description",
      "prompt": "Prompt text inserted into the active terminal"
    }
  ]
}
```

## Workflow

1. Read existing snippets from the target file.
2. Pick a short kebab-case `id`, a clear `title`, and a useful `group`.
3. Write one focused `prompt` that is safe to paste into Claude Code or Codex.
4. Append or replace the snippet with the same `id`.
5. Preserve existing snippets and write JSON with 2-space indentation.

## Rules

- These files are user/repo-owned. Do not copy TerMinal's built-in preset snippets into them.
- Built-in presets are app-owned and can be hidden/restored from TerMinal Settings.
- Do not create broad, multi-purpose snippets.
- Do not include secrets or machine-specific paths unless the user explicitly asks.
- Keep prompt text direct and action-oriented.
- Prefer groups like `Common`, `Checks`, `Review`, `Git`, `Tickets`, `Docs`, `Debug`, `Refactor`, `Tests`, or `Context`.

End with the path updated and the snippet title.
