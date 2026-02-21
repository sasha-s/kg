---
name: note
description: Add a quick timestamped note to today's daily notes node
---

# Note: Quick Note Capture

Extract the note text from the user's message (everything after `/note`).

## Workflow

### Phase 1: Add note immediately (sync)

1. Get today's date/time using `date` command or Python
2. Daily node slug: `notes-YYYY-MM-DD`, title: `Notes YYYY-MM-DD`
3. Create node if missing: `kg create notes-YYYY-MM-DD "Notes YYYY-MM-DD"`
   (idempotent — safe to run even if it exists)
4. Format: `[HH:MM] note text`
5. Add: `kg add notes-YYYY-MM-DD "[HH:MM] note text" --type note`
6. Report the bullet ID and formatted note to user

### Phase 2: Cross-ref enrichment (async background)

After confirming, spawn a background Task to enrich with cross-references:

```
Task(
    prompt="""You are a cross-referencing agent for kg.
BULLET_ID: {bullet_id}
NOTE_TEXT: {formatted_note}
NODE_SLUG: notes-YYYY-MM-DD

1. Run: kg search "{note_text}"
2. Run: kg context "{note_text}"
3. If relevant nodes found (clearly related, not tangential), update:
   kg update {bullet_id} "[HH:MM] {note_text} [slug1] [slug2]"
4. Report cross-refs added, or "no relevant cross-refs found"

Be conservative — only add cross-refs that are clearly relevant.""",
    subagent_type="general-purpose",
    run_in_background=True,
)
```

## Implementation

```python
from datetime import datetime
now = datetime.now()
date_str = now.strftime("%Y-%m-%d")
time_str = now.strftime("[%H:%M]")
daily_slug = f"notes-{date_str}"
formatted = f"{time_str} {note_text}"
```

```bash
kg create notes-{date_str} "Notes {date_str}"   # idempotent
kg add {daily_slug} "{formatted}" --type note    # returns bullet id
```

## Example

**Input:** `/note meta.json uses flock + atomic rename for safe RMW`

**Output:**
```
Added to [notes-2026-02-20]:
  [14:32] meta.json uses flock + atomic rename for safe RMW
  b-a1b2c3d4

Cross-referencing in background...
```

## Edge Cases

- **Empty note text**: Ask user for note content
- **First note of day**: `kg create` auto-handles it (idempotent)
- **No cross-refs found**: Note stays as-is, report "no relevant cross-refs"
