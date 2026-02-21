---
name: notes
description: List recent notes from daily nodes
---

# Notes: List Recent Notes

Parse the user's message (after `/notes`) to determine what to show.

## Filters

| Input | Behavior |
|-------|----------|
| (empty) | Today's notes |
| `YYYY-MM-DD` | Notes for that date |
| `yesterday` | Yesterday's notes |
| `week` or `7d` | Last 7 days |
| `all` | All daily nodes |
| anything else | Search across all daily nodes |

## Implementation

### Today (default)
```bash
kg show notes-$(date +%Y-%m-%d)
```

### Specific date / yesterday
```bash
kg show notes-YYYY-MM-DD
```

### Week / last 7 days
```bash
kg nodes 'notes-*' --recent -l 7
# then kg show each
```

### All
```bash
kg nodes 'notes-*' --recent
```

### Search
```bash
kg search "{query}"
# filter results to notes-* nodes
```

## Output Format

```
## Notes 2026-02-20

- [14:32] meta.json uses flock + atomic rename [kg-architecture]
- [15:10] vote_score: harmful_weight=2 penalises bad bullets more

## Notes 2026-02-19

- [09:15] stop hook cwd bug fixed with glob scan
```

## Edge Cases

- **No notes today**: "No notes for today. Use /note to add one."
- **No notes at all**: "No daily notes found."
- **Invalid date**: Try to parse flexibly, fall back to search
