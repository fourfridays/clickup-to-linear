#!/usr/bin/env python3
"""
ClickUp -> Linear Migration Script
==================================
Migrates all spaces, tasks, tags, statuses, dates, and time tracking
from ClickUp into a fresh Linear workspace.

Mapping:
  ClickUp Spaces  ->  One Linear Team + each space becomes a Project
  ClickUp Tasks   ->  Linear Issues
  ClickUp Tags    ->  Linear Labels (created on-demand)
  ClickUp Status  ->  Linear Issue State
  ClickUp Priority -> Linear Priority
  Time Tracking   ->  estimate
  dueDate         ->  dueDate

Usage:
  export CLICKUP_API_TOKEN=...
  export LINEAR_API_TOKEN=...
  python migrate.py [--dry-run] [--env .env]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
LINEAR_API_BASE = "https://api.linear.app/graphql"
DEFAULT_TEAM_NAME = "Migrated from ClickUp"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env(path: str) -> None:
    """Load key=value pairs from a .env file into os.environ."""
    env_path = Path(path)
    if not env_path.exists():
        print(f"  (env file {path} not found, skipping)")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ and value:
                os.environ[key] = value


def clickup_get(url: str, token: str, params=None) -> list:
    """Paginate through a ClickUp list endpoint and return all results."""
    all_items = []
    params = params or {}
    headers = {
        "accept": "application/json",
        "Authorization": token
    }
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("lists") or data.get("tasks") or
                 data.get("spaces") or data.get("teams") or [])
        if isinstance(data.get("task"), dict):
            return data
        all_items.extend(items)
        cursor = data.get("cursor")
        params = None
        if cursor:
            url = f"{CLICKUP_API_BASE}/{cursor}"
        else:
            break
    return all_items


def linear_query(token: str, query: str, variables=None) -> dict:
    """Execute a Linear GraphQL query/mutation."""
    headers = {"Authorization": token}
    resp = requests.post(
        LINEAR_API_BASE,
        headers=headers,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Linear API error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear API error: {data['errors']}")
    return data["data"]


# ---------------------------------------------------------------------------
# ClickUp data fetching
# ---------------------------------------------------------------------------


def fetch_clickup_data(token: str, dry_run: bool) -> dict:
    """Fetch all teams, spaces, lists, and tasks from ClickUp.

    ClickUp hierarchy: Teams -> Spaces -> Lists -> Tasks
    """
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}=== Fetching ClickUp data ===\n")

    # 1. Teams
    print("  Fetching teams ...")
    teams = clickup_get(f"{CLICKUP_API_BASE}/team", token)
    print(f"    Found {len(teams)} team(s).")

    # 2. Spaces per team
    all_spaces = []
    for team in teams:
        tid = str(team["id"])
        tname = team.get("name", f"Team {tid}")
        print(f"    Fetching spaces for team '{tname}' ...")
        spaces = clickup_get(f"{CLICKUP_API_BASE}/team/{tid}/space", token)
        for s in spaces:
            s["_team_id"] = tid
            s["_team_name"] = tname
        all_spaces.extend(spaces)
        print(f"      {len(spaces)} space(s).")

    print(f"    Total: {len(all_spaces)} space(s) across {len(teams)} team(s).")

    # 3. Lists per space
    space_lists = {}
    for space in all_spaces:
        sid = str(space["id"])
        name = space.get("name", f"Space {sid}")
        print(f"    Fetching lists for space '{name}' ...")
        lists = clickup_get(f"{CLICKUP_API_BASE}/space/{sid}/list", token)
        space_lists[sid] = lists
        print(f"      {len(lists)} list(s).")

    # 4. Tasks per list
    all_tasks = []
    task_count = 0
    for sid, lists in space_lists.items():
        for lst in lists:
            lid = str(lst["id"])
            lst_name = lst.get("name", f"List {lid}")
            print(f"    Fetching tasks for list '{lst_name}' ...")
            tasks = clickup_get(
                f"{CLICKUP_API_BASE}/list/{lid}/task",
                token,
            )
            for t in tasks:
                t["_space_id"] = sid
                t["_list_id"] = lid
                t["_list_name"] = lst_name
            all_tasks.extend(tasks)
            task_count += len(tasks)
            print(f"      {len(tasks)} task(s).")

    print(f"\n{prefix}Total: {len(teams)} team(s), "
          f"{len(all_spaces)} space(s), "
          f"{sum(len(v) for v in space_lists.values())} list(s), "
          f"{task_count} task(s).")

    # Debug: print first task status/priority format
    if all_tasks:
        t = all_tasks[0]
        print(f"\n  [DEBUG] Sample task '{t.get('name')}':")
        print(f"    status type={type(t.get('status')).__name__} value={t.get('status')}")
        print(f"    priority type={type(t.get('priority')).__name__} value={t.get('priority')}")
        print(f"    time_tracking={t.get('time_tracking')}")
        print(f"    custom_fields={t.get('custom_fields')}")

    return {"spaces": all_spaces, "space_lists": space_lists, "tasks": all_tasks}


# ---------------------------------------------------------------------------
# Linear GraphQL operations (all use variables, String IDs)
# ---------------------------------------------------------------------------

GET_TEAM_QUERY = """
query {
  teams {
    nodes { id name }
  }
}
"""

CREATE_TEAM_MUTATION = """
mutation CreateTeam($name: String!) {
  teamCreate(input: {name: $name}) {
    success
    team { id name }
  }
}
"""

GET_STATES_QUERY = """
query GetStates($teamId: String!) {
  team(id: $teamId) {
    states {
      nodes { id name }
    }
  }
}
"""

CREATE_PROJECT_MUTATION = """
mutation CreateProject($teamIds: [String!]!, $name: String!) {
  projectCreate(input: {teamIds: $teamIds, name: $name}) {
    success
    project { id name }
  }
}
"""

GET_LABELS_QUERY = """
query GetLabels($teamId: String!) {
  team(id: $teamId) {
    labels(first: 100) {
      nodes { id name }
    }
  }
}
"""

CREATE_LABEL_MUTATION = """
mutation CreateLabel($teamId: String!, $name: String!, $color: String!) {
  issueLabelCreate(input: {teamId: $teamId, name: $name, color: $color}) {
    success
    issueLabel { id name }
  }
}
"""

CREATE_ISSUE_MUTATION = """
mutation CreateIssue(
  $teamId: String!
  $projectId: String
  $title: String!
  $description: String
  $priority: Int
  $stateId: String
  $labelIds: [String!]
  $estimate: Int
  $dueDate: TimelessDate
) {
  issueCreate(input: {
    teamId: $teamId
    projectId: $projectId
    title: $title
    description: $description
    priority: $priority
    stateId: $stateId
    labelIds: $labelIds
    estimate: $estimate
    dueDate: $dueDate
  }) {
    success
    issue { id identifier url }
  }
}
"""

LABEL_COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4",
    "#FFEAA7", "#DDA0DD", "#98D8C8", "#F7DC6F",
    "#BB8FCE", "#85C1E9", "#F0B27A", "#82E0AA",
]


# ---------------------------------------------------------------------------
# Linear setup
# ---------------------------------------------------------------------------


def sanitize_name(name: str, max_len: int = 80) -> str:
    if not name:
        return "Untitled"
    return name.replace('"', '').replace('\\', '').replace('\n', ' ').strip()[:max_len]


def setup_linear(token: str, dry_run: bool) -> dict:
    """Create/find team, fetch states in Linear."""
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}=== Setting up Linear ===\n")

    # 1. Find or create the default team
    print(f"{prefix}Finding or creating team '{DEFAULT_TEAM_NAME}' ...")
    if not dry_run:
        data = linear_query(token, GET_TEAM_QUERY)
        teams = data.get("teams", {}).get("nodes", [])
    else:
        teams = []

    team = None
    for t in teams:
        if t["name"] == DEFAULT_TEAM_NAME:
            team = t
            break

    if team is None:
        print(f"  Creating team '{DEFAULT_TEAM_NAME}' ...")
        if not dry_run:
            data = linear_query(token, CREATE_TEAM_MUTATION, {"name": DEFAULT_TEAM_NAME})
            team = data["teamCreate"]["team"]
        else:
            team = {"id": "dry-run-team-id", "name": DEFAULT_TEAM_NAME}
        print(f"    Created (id: {team['id']})")
    else:
        print(f"    Found existing team (id: {team['id']})")

    team_id = team["id"]

    # 2. Fetch team states
    print(f"{prefix}Fetching team states ...")
    if not dry_run:
        states = linear_query(token, GET_STATES_QUERY, {"teamId": team_id})["team"]["states"]["nodes"]
    else:
        states = [
            {"id": "s-backlog", "name": "Backlog"},
            {"id": "s-in-progress", "name": "In Progress"},
            {"id": "s-done", "name": "Done"},
        ]

    # Build state lookup by name (case-insensitive)
    state_map = {}
    for s in states:
        name_lower = s.get("name", "").lower()
        if "done" in name_lower:
            state_map["done"] = s["id"]
        elif "in progress" in name_lower:
            state_map["in_progress"] = s["id"]
        elif "backlog" in name_lower:
            state_map["backlog"] = s["id"]
        elif "unstarted" in name_lower:
            state_map["unstarted"] = s["id"]

    # Ensure we have all keys
    if "in_progress" not in state_map:
        state_map["in_progress"] = state_map.get("unstarted", states[0]["id"])
    if "unstarted" not in state_map:
        state_map["unstarted"] = state_map.get("backlog", states[0]["id"])
    if "done" not in state_map:
        state_map["done"] = states[-1]["id"]

    default_state = state_map["backlog"]
    print(f"    States: {', '.join(f'{k}={v}' for k, v in state_map.items())}")
    print(f"    Default: {default_state}")

    return {"team_id": team_id, "state_map": state_map, "default_state": default_state}


def create_projects(token: str, team_id: str, spaces: list, dry_run: bool) -> dict:
    """Create a Linear project per ClickUp space."""
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}=== Creating projects (one per space) ===\n")

    space_to_project = {}
    for space in spaces:
        sid = str(space["id"])
        name = sanitize_name(space.get("name", f"Space {sid}"))
        print(f"  Project: '{name}' ...")

        if dry_run:
            space_to_project[sid] = f"dry-run-project-{sid}"
            print(f"    [DRY-RUN] Would create project")
            continue

        data = linear_query(token, CREATE_PROJECT_MUTATION,
                            {"teamIds": [team_id], "name": name})
        project = data["projectCreate"]["project"]
        space_to_project[sid] = project["id"]
        print(f"    Created (id: {project['id']})")

    return space_to_project


def sync_labels(token: str, team_id: str, all_tasks: list, dry_run: bool) -> dict:
    """Collect all unique tags across tasks, create labels in Linear."""
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}=== Syncing labels (from ClickUp tags) ===\n")

    tag_set = set()
    for task in all_tasks:
        for tag in task.get("tags", []):
            tag_name = tag.get("name", "").strip()
            if tag_name:
                tag_set.add(tag_name.lower())

    print(f"  Found {len(tag_set)} unique tag(s).")

    if not dry_run:
        labels_data = linear_query(token, GET_LABELS_QUERY, {"teamId": team_id})
        existing_labels = {
            l["name"].lower(): l["id"]
            for l in labels_data.get("team", {}).get("labels", {}).get("nodes", [])
        }
    else:
        existing_labels = {}

    label_map = dict(existing_labels)
    color_idx = 0

    for tag_name in sorted(tag_set):
        if tag_name in label_map:
            continue
        color = LABEL_COLORS[color_idx % len(LABEL_COLORS)]
        color_idx += 1
        print(f"  Label: '{tag_name}' ...")

        if dry_run:
            label_map[tag_name] = f"dry-run-label-{tag_name}"
            print(f"    [DRY-RUN] Would create label")
            continue

        try:
            data = linear_query(token, CREATE_LABEL_MUTATION,
                                {"teamId": team_id, "name": tag_name, "color": color})
            label = data["issueLabelCreate"]["issueLabel"]
            label_map[tag_name] = label["id"]
            print(f"    Created (id: {label['id']})")
        except Exception as e:
            print(f"    Skipped: {e}")

    return label_map


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def extract_clickup_priority(task) -> Optional[int]:
    """Extract priority int from ClickUp task.

    ClickUp returns priority as either an int or a dict like {"priority": 1}.
    """
    p = task.get("priority")
    if p is None:
        return None
    if isinstance(p, dict):
        return p.get("priority")
    return p


def extract_clickup_status(task) -> str:
    """Extract status name string from ClickUp task.

    ClickUp returns status as either a string or a dict.
    The dict can have keys like "status", "name", or others.
    """
    s = task.get("status")
    if s is None:
        return ""
    if isinstance(s, str):
        return s
    if isinstance(s, dict):
        # Try common keys ClickUp uses
        for key in ("status", "name", "label"):
            if key in s and isinstance(s[key], str):
                return s[key]
        # Last resort: return the stringified dict
        return str(s)
    return str(s)


def map_priority(clickup_priority) -> Optional[int]:
    """ClickUp priority (0=Urgent, 1=High, 2=Medium, 3=Low) -> Linear priority int.

    Linear uses: 1=Low, 2=Medium, 3=High, 4=Urgent
    """
    if clickup_priority is None:
        return None
    mapping = {0: 4, 1: 3, 2: 2, 3: 1}
    return mapping.get(clickup_priority)


def map_status(status_name: str, state_map: dict, default_state: str) -> str:
    """ClickUp status name -> Linear state id."""
    if not status_name:
        return default_state
    s = status_name.lower()
    if s in ("closed", "complete", "done", "completed", "win"):
        return state_map.get("done", default_state)
    elif s in ("in progress", "fixed"):
        return state_map.get("in_progress", default_state)
    elif s in ("open", "todo"):
        return state_map.get("backlog", default_state)
    else:
        return default_state


def format_description(task: dict) -> str:
    """Build a Markdown description from ClickUp task fields."""
    parts = []

    content = task.get("content", "") or task.get("description", "")
    if content:
        parts.append(content)

    custom_fields = task.get("custom_fields", [])
    if custom_fields:
        parts.append("## Custom Fields")
        for cf in custom_fields:
            name = cf.get("name", "Unnamed")
            value = cf.get("value")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            parts.append(f"- **{name}**: {value}")

    checklists = task.get("checklists", [])
    for cl in checklists:
        cl_name = cl.get("name", "Checklist")
        items = cl.get("items", [])
        if items:
            parts.append(f"## Checklist: {cl_name}")
            for item in items:
                checked = "x" if item.get("checked", False) else " "
                item_name = item.get("name", "")
                parts.append(f"- [{checked}] {item_name}")

    time_tracking = task.get("time_tracking", {})
    if time_tracking and isinstance(time_tracking, dict):
        total = time_tracking.get("total", 0)
        if total and total > 0:
            hours = total / 3600
            parts.append(f"\n> Time tracked: {hours:.1f}h")

    return "\n\n".join(parts)


def parse_clickup_date(ts) -> Optional[str]:
    """Convert ClickUp epoch-ms to ISO 8601 date string (YYYY-MM-DD)."""
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        return None


def extract_time_tracking_seconds(task) -> Optional[int]:
    """Extract total tracked time in seconds from ClickUp task."""
    tt = task.get("time_tracking")
    if not tt or not isinstance(tt, dict):
        return None
    total = tt.get("total")
    if total is None:
        return None
    try:
        return int(total)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------


def migrate_tasks(
    token: str,
    team_id: str,
    space_to_project: dict,
    label_map: dict,
    state_map: dict,
    default_state: str,
    tasks: list,
    dry_run: bool,
) -> list:
    """Create Linear issues for each ClickUp task."""
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}=== Migrating tasks ===\n")

    results = []
    skipped = 0

    for idx, task in enumerate(tasks, 1):
        title = sanitize_name(task.get("name", "Untitled"))
        sid = str(task.get("_space_id", ""))
        project_id = space_to_project.get(sid, "")

        if not project_id:
            print(f"  [{idx}/{len(tasks)}] SKIP '{title}' - no project mapping")
            results.append({"title": title, "status": "skipped", "reason": "no project"})
            skipped += 1
            continue

        # Parent task (subtask) - skip for now
        if task.get("parent"):
            results.append({"title": title, "status": "skipped", "reason": "subtask"})
            skipped += 1
            continue

        # Priority
        priority = map_priority(extract_clickup_priority(task))

        # Status
        status_name = extract_clickup_status(task)
        state_id = map_status(status_name, state_map, default_state)

        # Labels
        tag_ids = []
        for tag in task.get("tags", []):
            tag_name = tag.get("name", "").strip().lower()
            if tag_name in label_map:
                tag_ids.append(label_map[tag_name])

        # Estimate (minutes from tracked seconds)
        total_seconds = extract_time_tracking_seconds(task)
        estimate = round(total_seconds / 60) if total_seconds else None

        # Dates
        due_date = parse_clickup_date(task.get("dueDate"))

        # Description
        description = format_description(task)

        if dry_run:
            print(f"  [{idx}/{len(tasks)}] [DRY-RUN] '{title}'")
            print(f"         project={project_id} priority={priority} state={state_id}")
            if estimate:
                print(f"         estimate={estimate}min")
            if due_date:
                print(f"         due={due_date}")
            results.append({"title": title, "status": "would_create"})
            continue

        # Build GraphQL variables
        issue_vars = {
            "teamId": team_id,
            "title": title,
            "stateId": state_id,
        }
        if tag_ids:
            issue_vars["labelIds"] = tag_ids
        if project_id:
            issue_vars["projectId"] = project_id
        if description:
            issue_vars["description"] = description
        if priority is not None:
            issue_vars["priority"] = priority
        if estimate is not None:
            issue_vars["estimate"] = estimate
        if due_date:
            issue_vars["dueDate"] = due_date

        try:
            data = linear_query(token, CREATE_ISSUE_MUTATION, issue_vars)
            issue = data["issueCreate"]["issue"]
            print(f"  [{idx}/{len(tasks)}] OK '{title}' -> {issue['identifier']}")
            results.append({
                "title": title,
                "status": "created",
                "identifier": issue.get("identifier"),
                "url": issue.get("url"),
            })
        except Exception as e:
            print(f"  [{idx}/{len(tasks)}] FAIL '{title}' - {e}")
            results.append({"title": title, "status": "error", "reason": str(e)})
            skipped += 1

        # Rate limiting
        if idx % 10 == 0:
            time.sleep(0.5)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Migrate ClickUp -> Linear")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making API calls")
    parser.add_argument("--env", default=".env",
                        help="Path to .env file (default: .env)")
    args = parser.parse_args()

    load_env(args.env)

    clickup_token = os.environ.get("CLICKUP_API_TOKEN", "")
    linear_token = os.environ.get("LINEAR_API_TOKEN", "")

    if not clickup_token:
        print("ERROR: CLICKUP_API_TOKEN not set.")
        sys.exit(1)
    if not linear_token:
        print("ERROR: LINEAR_API_TOKEN not set.")
        sys.exit(1)

    dry_run = args.dry_run
    mode = "DRY RUN" if dry_run else "LIVE"
    print("=" * 60)
    print(f"  CLICKUP -> LINEAR MIGRATION ({mode})")
    print("=" * 60)

    # Phase 1: Fetch ClickUp data
    clickup_data = fetch_clickup_data(clickup_token, dry_run)

    if not clickup_data["tasks"]:
        print("\nNo tasks found in ClickUp. Nothing to migrate.")
        sys.exit(0)

    # Phase 2: Setup Linear
    linear_setup = setup_linear(linear_token, dry_run)

    # Phase 3: Create projects
    space_to_project = create_projects(
        linear_token, linear_setup["team_id"],
        clickup_data["spaces"], dry_run,
    )

    # Phase 4: Sync labels
    label_map = sync_labels(
        linear_token, linear_setup["team_id"],
        clickup_data["tasks"], dry_run,
    )

    # Phase 5: Migrate tasks
    results = migrate_tasks(
        linear_token,
        linear_setup["team_id"],
        space_to_project,
        label_map,
        linear_setup["state_map"],
        linear_setup["default_state"],
        clickup_data["tasks"],
        dry_run,
    )

    # Summary
    created = sum(1 for r in results if r["status"] == "created")
    would_create = sum(1 for r in results if r["status"] == "would_create")
    skipped = sum(1 for r in results if r["status"] in ("skipped", "error"))

    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total tasks processed: {len(results)}")
    if dry_run:
        print(f"  Would create:        {would_create}")
    else:
        print(f"  Created:             {created}")
    print(f"  Skipped/Errors:      {skipped}")

    errors = [r for r in results if r["status"] == "error"]
    if errors and not dry_run:
        log_path = Path("migration_log.json")
        with open(log_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n  Migration log written to {log_path}")

    print()


if __name__ == "__main__":
    main()
