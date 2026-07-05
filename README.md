# ClickUp → Linear Migration

Migrates all ClickUp spaces, tasks, tags, statuses, dates, and time tracking into a fresh Linear workspace.

## Mapping

| ClickUp | Linear |
|---|---|
| Spaces (flattened) | One team + each space → a Project |
| Tasks | Issues |
| Tags | Labels |
| Status | Issue State |
| Priority | Priority |
| Time Tracking (seconds) | `estimateMinutes` |
| `startDate` / `dueDate` | `startAt` / `dueAt` |
| `date_created`          | `createdAt`       |
| `date_closed`           | `completedAt`     |

## Setup

1. Install dependencies:
   ```bash
   pip3 install -r requirements.txt
   ```

2. Create a `.env` file with your API tokens:
   ```bash
   cp .env.example .env
   # Edit .env and paste your tokens
   ```
   - **ClickUp token**: [https://app.clickup.com/settings/api](https://app.clickup.com/settings/api)
   - **Linear token**: [https://linear.app/settings/api](https://linear.app/settings/api)

## Usage

**Dry run (preview, no changes):**
```bash
python3 migrate.py --dry-run
```

**Actual migration:**
```bash
python3 migrate.py
```

## Notes

- A Linear team called "Migrated from ClickUp" is created to hold all tasks.
- Subtasks (tasks with parents) are skipped in v1 — migrate top-level tasks first.
- Errors are logged to `migration_log.json` in the script directory.
