#!/usr/bin/env python3
"""
ClickUp -> Linear Migration Script
==================================
Migrates all spaces, tasks, tags, statuses, dates, and time tracking
from ClickUp into a fresh Linear workspace.

Mapping:
  ClickUp Spaces  ->  One Linear Team each
  ClickUp Lists   ->  Labels within each team (development, documentation, etc.)
  ClickUp Tasks   ->  Linear Issues
  ClickUp Tags    ->  Linear Labels (created on-demand)
  ClickUp Status  ->  Linear Issue State
  ClickUp Priority -> Linear Priority
  Time Tracking   ->  estimate
  dueDate         ->  dueDate
  date_created    ->  createdAt
  date_closed     ->  completedAt

Usage:
  export CLICKUP_API_TOKEN=...
  export LINEAR_API_TOKEN=...
  python migrate.py [--dry-run] [--env .env]
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional, Dict, List, Any
import requests
import logging

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
LINEAR_API_BASE = "https://api.linear.app/graphql"
DEFAULT_TEAM_NAME = "Migrated from ClickUp"
API_TIMEOUT = 30
RATE_LIMIT_DELAY = 0.1  # seconds between API calls
BATCH_RATE_LIMIT_INTERVAL = 10  # number of operations before rate limit delay

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger(__name__)
API_TIMEOUT = 30
RATE_LIMIT_DELAY = 0.1  # seconds between API calls
BATCH_RATE_LIMIT_INTERVAL = 10  # number of operations before rate limit delay

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger(__name__)

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


def clickup_get(url: str, token: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """Paginate through a ClickUp list endpoint and return all results.
    
    Args:
        url: ClickUp API endpoint URL
        token: ClickUp API token
        params: Optional query parameters
        
    Returns:
        List of items from the paginated endpoint
    """
    all_items: List[Dict[str, Any]] = []
    params = params or {}
    headers = {
        "accept": "application/json",
        "Authorization": token
    }
    
    # List of possible item keys ClickUp returns
    item_keys = ["lists", "tasks", "spaces", "teams"]
    
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        
        # Handle single task response
        if isinstance(data.get("task"), dict):
            return data
        
        # Extract items from response (try multiple possible keys)
        items = next((data.get(key, []) for key in item_keys if key in data), [])
        all_items.extend(items)
        
        # Check for pagination
        cursor = data.get("cursor")
        params = None
        if cursor:
            url = f"{CLICKUP_API_BASE}/{cursor}"
            time.sleep(RATE_LIMIT_DELAY)
        else:
            break
    
    return all_items


def linear_query(token: str, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
    """Execute a Linear GraphQL query/mutation.
    
    Args:
        token: Linear API token
        query: GraphQL query or mutation string
        variables: Optional GraphQL variables
        
    Returns:
        Response data from Linear API
        
    Raises:
        RuntimeError: If API request fails
    """
    headers = {"Authorization": token}
    time.sleep(RATE_LIMIT_DELAY)
    
    resp = requests.post(
        LINEAR_API_BASE,
        headers=headers,
        json={"query": query, "variables": variables or {}},
        timeout=API_TIMEOUT,
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


def _log_section(title: str, dry_run: bool = False) -> None:
    """Print a section header for logging."""
    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info(f"\n{prefix}=== {title} ===")


def fetch_clickup_data(token: str, dry_run: bool) -> Dict[str, Any]:
    """Fetch all teams, spaces, lists, and tasks from ClickUp.

    ClickUp hierarchy: Teams -> Spaces -> Lists -> Tasks
    
    Args:
        token: ClickUp API token
        dry_run: If True, don't make actual API calls
        
    Returns:
        Dictionary containing spaces, space_lists, and tasks
    """
    _log_section("Fetching ClickUp data", dry_run)
    logger.info("")

    # 1. Teams
    logger.info("  Fetching teams ...")
    teams = clickup_get(f"{CLICKUP_API_BASE}/team", token)
    logger.info(f"    Found {len(teams)} team(s).")

    # 2. Spaces per team
    all_spaces = []
    for team in teams:
        tid = str(team["id"])
        tname = team.get("name", f"Team {tid}")
        logger.info(f"    Fetching spaces for team '{tname}' ...")
        spaces = clickup_get(f"{CLICKUP_API_BASE}/team/{tid}/space", token)
        for s in spaces:
            s["_team_id"] = tid
            s["_team_name"] = tname
        all_spaces.extend(spaces)
        logger.info(f"      {len(spaces)} space(s).")

    logger.info(f"    Total: {len(all_spaces)} space(s) across {len(teams)} team(s).")

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
                params={"include_closed": "true"},
            )
            # Attach metadata to each task
            for t in tasks:
                t["_space_id"] = sid
                t["_list_id"] = lid
                t["_list_name"] = lst_name
            all_tasks.extend(tasks)
            task_count += len(tasks)
            logger.info(f"      {len(tasks)} task(s).")

    prefix = "[DRY-RUN] " if dry_run else ""
    total_lists = sum(len(v) for v in space_lists.values())
    logger.info(f"\n{prefix}Total: {len(teams)} team(s), "
                f"{len(all_spaces)} space(s), "
                f"{total_lists} list(s), "
                f"{task_count} task(s).")

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
  $title: String!
  $description: String
  $priority: Int
  $stateId: String
  $labelIds: [String!]
  $estimate: Int
  $dueDate: TimelessDate
  $createdAt: DateTime
  $completedAt: DateTime
) {
  issueCreate(input: {
    teamId: $teamId
    title: $title
    description: $description
    priority: $priority
    stateId: $stateId
    labelIds: $labelIds
    estimate: $estimate
    dueDate: $dueDate
    createdAt: $createdAt
    completedAt: $completedAt
  }) {
    success
    issue { id identifier url completedAt }
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
    """Sanitize a name for Linear, removing URLs and trailing domain patterns.
    
    Linear rejects names containing URLs, so we strip any http/https/ftp URLs
    and standalone domain-like patterns (e.g. example.com).
    If the entire name is a domain, replace dots with dashes instead.
    """
    if not name:
        return "Untitled"
    # Strip full URLs
    name = re.sub(r'https?://[^\s]+', '', name)
    name = re.sub(r'ftp://[^\s]+', '', name)
    name = re.sub(r'www\.\S+', '', name)
    # If the entire name is a domain, replace dots with dashes
    if re.match(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.(?:com|net|org|io|co|dev|app|info|biz|me|us|uk)\b$', name, flags=re.IGNORECASE):
        name = name.replace('.', '-')
        return name.strip()[:max_len]
    # Strip standalone domain patterns from mixed names (e.g. "project.com extra")
    name = re.sub(r'\b[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.(?:com|net|org|io|co|dev|app|info|biz|me|us|uk)\b', '', name, flags=re.IGNORECASE)
    # Strip other problematic characters Linear doesn't like
    name = name.replace('"', '').replace('\\', '').replace('\n', ' ')
    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name)
    return name.strip()[:max_len]


def create_teams_and_states(token: str, spaces: list, dry_run: bool) -> Dict[str, Any]:
    """Create a Linear team per ClickUp space, fetch states for each.
    
    Args:
        token: Linear API token
        spaces: List of ClickUp spaces
        dry_run: If True, use mock data instead of API calls
        
    Returns:
        Dictionary with space_to_team mapping and state maps per team
    """
    _log_section("Creating Linear teams (one per space)", dry_run)
    prefix = "[DRY-RUN] " if dry_run else ""

    space_to_team: Dict[str, str] = {}
    team_states: Dict[str, dict] = {}
    team_default_state: Dict[str, str] = {}
    seen_team_names: Dict[str, int] = {}

    for space in spaces:
        sid = str(space["id"])
        space_name = space.get("name", f"Space {sid}")
        team_name = sanitize_name(space_name)
        # Deduplicate: if stripping domains produced a collision, append a suffix
        if team_name in seen_team_names:
            counter = seen_team_names[team_name]
            seen_team_names[team_name] += 1
            team_name = f"{team_name} ({counter})"
        else:
            seen_team_names[team_name] = 1
        logger.info(f"  Team: '{team_name}' ...")

        if dry_run:
            space_to_team[sid] = f"dry-run-team-{sid}"
            state_map = _build_state_map([
                {"id": "s-backlog", "name": "Backlog"},
                {"id": "s-in-progress", "name": "In Progress"},
                {"id": "s-done", "name": "Done"},
            ])
            team_states[sid] = state_map
            team_default_state[sid] = state_map["backlog"]
            logger.info(f"    [DRY-RUN] Would create team")
            continue

        # Create team
        data = linear_query(token, CREATE_TEAM_MUTATION, {"name": team_name})
        team = data["teamCreate"]["team"]
        team_id = team["id"]
        space_to_team[sid] = team_id
        logger.info(f"    Created (id: {team_id})")

        # Fetch states for this team
        states = linear_query(token, GET_STATES_QUERY, {"teamId": team_id})["team"]["states"]["nodes"]
        state_map = _build_state_map(states)
        team_states[sid] = state_map
        default_state = state_map.get("backlog", states[0]["id"] if states else "")
        team_default_state[sid] = default_state
        logger.info(f"    States: {', '.join(f'{k}={v}' for k, v in state_map.items())}")

    return {"space_to_team": space_to_team, "team_states": team_states, "team_default_state": team_default_state}


def sync_labels(token: str, space_to_team: dict, all_tasks: list, dry_run: bool) -> dict:
    """Collect all unique tags across tasks, create labels in each team.
    
    Args:
        token: Linear API token
        space_to_team: Mapping of space_id -> Linear team_id
        all_tasks: List of all tasks
        dry_run: If True, use mock data instead of API calls
        
    Returns:
        Mapping of tag_name -> {team_id: label_id}
    """
    _log_section("Syncing tag labels (across all teams)", dry_run)
    logger.info("")

    # Collect all unique tags from tasks
    tag_set = set()
    for task in all_tasks:
        for tag in task.get("tags", []):
            tag_name = tag.get("name", "").strip()
            if tag_name:
                tag_set.add(tag_name.lower())

    logger.info(f"  Found {len(tag_set)} unique tag(s).")

    # Create tag labels in each team
    # Returns: {tag_name: {team_id: label_id}}
    tag_label_map: Dict[str, Dict[str, str]] = {}

    for team_id in space_to_team.values():
        logger.info(f"  Team '{team_id}' ...")
        
        if not dry_run:
            labels_data = linear_query(token, GET_LABELS_QUERY, {"teamId": team_id})
            existing_labels = {
                l["name"].lower(): l["id"]
                for l in labels_data.get("team", {}).get("labels", {}).get("nodes", [])
            }
        else:
            existing_labels = {}

        color_idx = 0
        for tag_name in sorted(tag_set):
            if tag_name in existing_labels:
                continue
            color = LABEL_COLORS[color_idx % len(LABEL_COLORS)]
            color_idx += 1
            logger.info(f"    Label: '{tag_name}' ...")

            if dry_run:
                if tag_name not in tag_label_map:
                    tag_label_map[tag_name] = {}
                tag_label_map[tag_name][team_id] = f"dry-run-label-{tag_name}"
                logger.info(f"    [DRY-RUN] Would create label")
                continue

            try:
                data = linear_query(token, CREATE_LABEL_MUTATION,
                                    {"teamId": team_id, "name": tag_name, "color": color})
                label = data["issueLabelCreate"]["issueLabel"]
                if tag_name not in tag_label_map:
                    tag_label_map[tag_name] = {}
                tag_label_map[tag_name][team_id] = label["id"]
                logger.info(f"    Created (id: {label['id']})")
            except Exception as e:
                logger.warning(f"    Skipped: {e}")

    return tag_label_map


def create_list_labels(token: str, space_lists: dict, space_to_team: dict, dry_run: bool) -> Dict[str, Dict[str, str]]:
    """Create labels from ClickUp list names within each Linear team.
    
    Args:
        token: Linear API token
        space_lists: Mapping of space_id -> list dicts
        space_to_team: Mapping of space_id -> Linear team_id
        dry_run: If True, use mock data instead of API calls
        
    Returns:
        Mapping of space_id -> {list_name: label_id}
    """
    _log_section("Creating list labels (one per list per space)", dry_run)
    logger.info("")

    space_to_list_labels: Dict[str, Dict[str, str]] = {}

    for sid, lists in space_lists.items():
        team_id = space_to_team.get(sid)
        if not team_id:
            continue
        
        logger.info(f"  Space '{sid}' -> team '{team_id}' ...")
        list_label_map: Dict[str, str] = {}
        color_idx = 0

        for lst in lists:
            list_name = lst.get("name", f"List {lst['id']}").strip().lower()
            if not list_name:
                continue
            
            logger.info(f"    Label: '{list_name}' ...")

            if dry_run:
                list_label_map[list_name] = f"dry-run-label-{list_name}"
                logger.info(f"    [DRY-RUN] Would create label")
                continue

            color = LABEL_COLORS[color_idx % len(LABEL_COLORS)]
            color_idx += 1

            try:
                data = linear_query(token, CREATE_LABEL_MUTATION,
                                    {"teamId": team_id, "name": list_name, "color": color})
                label = data["issueLabelCreate"]["issueLabel"]
                list_label_map[list_name] = label["id"]
                logger.info(f"    Created (id: {label['id']})")
            except Exception as e:
                logger.warning(f"    Skipped: {e}")

        space_to_list_labels[sid] = list_label_map

    return space_to_list_labels


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _build_state_map(states: list) -> dict:
    """Build a category-based state mapping from Linear states.
    
    Creates a robust mapping that handles variations in state names.
    """
    state_map = {}
    for s in states:
        name_lower = s.get("name", "").lower()
        state_id = s["id"]
        
        # Map by category (first match wins)
        if "done" in name_lower or "complete" in name_lower or "closed" in name_lower:
            if "done" not in state_map:
                state_map["done"] = state_id
        elif "in progress" in name_lower or "active" in name_lower:
            if "in_progress" not in state_map:
                state_map["in_progress"] = state_id
        elif "backlog" in name_lower or "todo" in name_lower:
            if "backlog" not in state_map:
                state_map["backlog"] = state_id
        elif "unstarted" in name_lower:
            if "unstarted" not in state_map:
                state_map["unstarted"] = state_id
    
    # Ensure required keys with fallbacks
    if "in_progress" not in state_map and states:
        state_map["in_progress"] = state_map.get("backlog", states[0]["id"])
    if "backlog" not in state_map and states:
        state_map["backlog"] = states[0]["id"]
    if "done" not in state_map and states:
        state_map["done"] = states[-1]["id"]
    
    return state_map


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def extract_clickup_priority(task) -> Optional[int]:
    """Extract priority int from ClickUp task.

    ClickUp returns priority as either an int or a dict like {"priority": "high"}.
    """
    p = task.get("priority")
    if p is None:
        return None
    if isinstance(p, dict):
        p = p.get("priority")
    if p is None:
        return None
    # ClickUp can return string names or integers
    if isinstance(p, str):
        mapping = {"urgent": 0, "high": 1, "medium": 2, "low": 3,
                   "red": 0, "yellow": 1, "blue": 2, "green": 3}
        return mapping.get(p.lower())
    return int(p) if p else None


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


def format_description(task: dict, completed_at: Optional[str] = None) -> str:
    """Build a Markdown description from ClickUp task fields.
    
    Args:
        task: ClickUp task dict
        completed_at: Original completion date from ClickUp (for reference when Linear sets completion to "now")
    """
    parts = []

    content = task.get("content", "") or task.get("description", "")
    if content:
        parts.append(content)

    custom_fields = task.get("custom_fields", [])
    # Filter custom fields to only include those with non-None values
    fields_with_values = []
    for cf in custom_fields:
        value = cf.get("value")
        # Skip None values and empty lists
        if value is None or (isinstance(value, list) and not value):
            continue
        fields_with_values.append(cf)
    
    if fields_with_values:
        parts.append("## Custom Fields")
        for cf in fields_with_values:
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

    # Add original completion date as metadata for reference
    # (Linear sets completedAt to current time, this preserves the original ClickUp date)
    if completed_at:
        parts.append(f"\n> Completed in ClickUp: {completed_at}")

    return "\n\n".join(parts)


def parse_clickup_date(task: dict, field: str, as_datetime: bool = False) -> Optional[str]:
    """Extract and convert a date from ClickUp task.
    
    ClickUp uses snake_case field names: due_date, start_date, date_closed, etc.
    Values are string epoch-ms like "1779004800000".
    
    Args:
        task: ClickUp task dict
        field: Field name to extract (e.g. "dueDate", "date_created")
        as_datetime: If True, returns ISO 8601 datetime (for Linear DateTime type).
                     If False, returns date-only (for Linear TimelessDate type).
    """
    # Build comprehensive list of candidate field names
    candidates = [field, f"{field}_utc"]
    
    # Add snake_case versions
    snake_field = field.replace("dueDate", "due_date") \
                       .replace("startDate", "start_date") \
                       .replace("dateCreated", "date_created") \
                       .replace("dateClosed", "date_closed") \
                       .replace("dateDone", "date_done")
    if snake_field != field:
        candidates.append(snake_field)
        candidates.append(f"{snake_field}_utc")
    
    # Add specific ClickUp field names for different date types
    if "due" in field.lower():
        candidates.extend(["due_date", "due_date_utc"])
    elif "closed" in field.lower() or "done" in field.lower() or "complete" in field.lower():
        candidates.extend(["date_closed", "date_closed_utc", "date_done", "date_done_utc"])
    elif "created" in field.lower():
        candidates.extend(["date_created", "date_created_utc"])
    elif "start" in field.lower():
        candidates.extend(["start_date", "start_date_utc"])
    
    ts = None
    for c in candidates:
        val = task.get(c)
        if val is not None:
            ts = val
            break
    
    if ts is None:
        return None
    if isinstance(ts, dict):
        ts = ts.get("date") or ts.get("timestamp")
    if not ts:
        return None
    try:
        # ClickUp returns dates as string epoch-ms
        dt = datetime.fromtimestamp(float(str(ts)) / 1000, tz=timezone.utc)
        if as_datetime:
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError, TypeError):
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


def _build_issue_variables(
    team_id: str,
    title: str,
    description: Optional[str],
    priority: Optional[int],
    state_id: str,
    label_ids: list,
    estimate: Optional[int],
    due_date: Optional[str],
    created_at: Optional[str],
    closed_at: Optional[str],
) -> dict:
    """Build GraphQL variables for issue creation mutation.
    
    Only includes fields that have values to keep mutations lean.
    Linear accepts completedAt on create.
    """
    variables = {
        "teamId": team_id,
        "title": title,
        "stateId": state_id,
    }
    
    if description:
        variables["description"] = description
    if priority is not None:
        variables["priority"] = priority
    if label_ids:
        variables["labelIds"] = label_ids
    if estimate is not None:
        variables["estimate"] = estimate
    if due_date:
        variables["dueDate"] = due_date
    if created_at:
        variables["createdAt"] = created_at
    if closed_at and created_at:
        variables["completedAt"] = closed_at
    
    return variables


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------


def migrate_tasks(
    token: str,
    space_to_team: dict,
    space_to_list_labels: dict,
    tag_label_map: dict,
    team_states: dict,
    team_default_state: dict,
    tasks: list,
    dry_run: bool,
) -> list:
    """Create Linear issues for each ClickUp task.
    
    Args:
        token: Linear API token
        space_to_team: Mapping of ClickUp space IDs to Linear team IDs
        space_to_list_labels: Mapping of space_id -> {list_name: label_id}
        tag_label_map: Mapping of tag names to Linear label IDs
        team_states: Mapping of team_id -> state_map
        team_default_state: Mapping of team_id -> default state ID
        tasks: List of ClickUp tasks to migrate
        dry_run: If True, preview without making API calls
        
    Returns:
        List of migration results with status and details
    """
    _log_section("Migrating tasks", dry_run)
    logger.info("")

    results = []
    skipped = 0
    debug_limit = 3  # Show debug info for first N tasks

    for idx, task in enumerate(tasks, 1):
        title = sanitize_name(task.get("name", "Untitled"))
        sid = str(task.get("_space_id", ""))
        lid = str(task.get("_list_id", ""))

        # Skip tasks without space mapping
        if sid not in space_to_team:
            logger.debug(f"  [{idx}/{len(tasks)}] SKIP '{title}' - no space mapping")
            results.append({"title": title, "status": "skipped", "reason": "no space"})
            skipped += 1
            continue

        # Skip subtasks (parent task relationships) for now
        if task.get("parent"):
            logger.debug(f"  [{idx}/{len(tasks)}] SKIP '{title}' - subtask")
            results.append({"title": title, "status": "skipped", "reason": "subtask"})
            skipped += 1
            continue

        team_id = space_to_team[sid]
        team_state_map = team_states.get(sid, {})
        team_default = team_default_state.get(sid, "")

        # Extract task fields
        priority = map_priority(extract_clickup_priority(task))
        status_name = extract_clickup_status(task)
        state_id = map_status(status_name, team_state_map, team_default)
        
        # Collect labels: list label + tag labels
        label_ids = []
        
        # List label (from the ClickUp list this task belongs to)
        list_name = task.get("_list_name", "").strip().lower()
        space_labels = space_to_list_labels.get(sid, {})
        if list_name and list_name in space_labels:
            label_ids.append(space_labels[list_name])
        
        # Tag labels (per team)
        for tag in task.get("tags", []):
            tag_name = tag.get("name", "").strip().lower()
            if tag_name in tag_label_map and team_id in tag_label_map[tag_name]:
                label_ids.append(tag_label_map[tag_name][team_id])

        # Extract estimate (convert seconds to minutes)
        total_seconds = extract_time_tracking_seconds(task)
        estimate = round(total_seconds / 60) if total_seconds else None

        # Extract dates
        due_date = parse_clickup_date(task, "dueDate")
        created_at = parse_clickup_date(task, "date_created", as_datetime=True)
        closed_at = parse_clickup_date(task, "date_closed", as_datetime=True)
        if closed_at and not created_at:
            logger.warning(
                f"  [{idx}/{len(tasks)}] '{title}' has completion date but no created date; "
                "skipping completedAt for API compatibility"
            )

        # Log debug info for first few tasks
        if idx <= debug_limit and logger.isEnabledFor(logging.DEBUG):
            date_keys = [k for k in task.keys() if 'date' in k.lower() or 'due' in k.lower()]
            logger.debug(f"  [DEBUG] Task {idx}: {date_keys}")
            logger.debug(f"    Parsed: due={due_date}, created={created_at}, closed={closed_at}")

        # Format description
        description = format_description(task, completed_at=closed_at if state_id == team_state_map.get("done") else None)

        # Dry-run mode: preview only
        if dry_run:
            summary = f"  [{idx}/{len(tasks)}] [DRY-RUN] '{title}' -> team={team_id} state={state_id}"
            if estimate:
                summary += f" ({estimate}min)"
            if closed_at:
                summary += f" [completed: {closed_at}]"
            elif due_date:
                summary += f" [due: {due_date}]"
            logger.info(summary)
            results.append({"title": title, "status": "would_create"})
            continue

        # Build GraphQL variables for creation
        issue_vars = _build_issue_variables(
            team_id, title, description,
            priority, state_id, label_ids, estimate,
            due_date, created_at, closed_at
        )

        # Create issue in Linear
        try:
            # Log the variables being sent (for debugging)
            logger.debug(f"    GraphQL variables: {issue_vars}")
            
            data = linear_query(token, CREATE_ISSUE_MUTATION, issue_vars)
            issue = data["issueCreate"]["issue"]
            
            # Check if issue was created successfully
            if issue and issue.get("id"):
                completed_info = ""
                if issue.get("completedAt"):
                    completed_info = f" [completed: {issue['completedAt']}]"
                logger.info(f"  [{idx}/{len(tasks)}] OK '{title}' -> {issue['identifier']}{completed_info}")
                
                results.append({
                    "title": title,
                    "status": "created",
                    "identifier": issue.get("identifier"),
                    "url": issue.get("url"),
                })
            else:
                logger.error(f"  [{idx}/{len(tasks)}] FAIL '{title}' - issue not created (no ID returned)")
                results.append({"title": title, "status": "error", "reason": "no issue ID returned"})

        except Exception as e:
            logger.error(f"  [{idx}/{len(tasks)}] FAIL '{title}' - {e}")
            results.append({"title": title, "status": "error", "reason": str(e)})
            skipped += 1

        # Batch rate limiting
        if idx % BATCH_RATE_LIMIT_INTERVAL == 0:
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

    # Phase 2: Create teams (one per space) + fetch states
    teams_setup = create_teams_and_states(
        linear_token, clickup_data["spaces"], dry_run,
    )

    # Phase 3: Create list labels within each team
    space_to_list_labels = create_list_labels(
        linear_token, clickup_data["space_lists"],
        teams_setup["space_to_team"], dry_run,
    )

    # Phase 4: Sync tag labels (global, shared across all teams)
    tag_label_map = sync_labels(
        linear_token, teams_setup["space_to_team"],
        clickup_data["tasks"], dry_run,
    )

    # Phase 5: Migrate tasks
    results = migrate_tasks(
        linear_token,
        teams_setup["space_to_team"],
        space_to_list_labels,
        tag_label_map,
        teams_setup["team_states"],
        teams_setup["team_default_state"],
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
