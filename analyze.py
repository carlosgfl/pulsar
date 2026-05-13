"""
Pulsar — daily analysis with interactive clarification.
Groups the day into projects with time estimates.
Claude asks for clarification on anything it doesn't recognise.

Usage:
    python analyze.py              # analyzes today
    python analyze.py 2026-05-01   # analyzes a specific date
"""

import base64
import configparser
import os
import sys
from datetime import date
from pathlib import Path

import anthropic

KNOWLEDGE_FILE = Path(__file__).parent / 'context' / 'knowledge.md'

SYSTEM_CONTEXT = """\
You are analyzing a workday for Carlos, Finance Strategy Manager at Ebury (a fintech FX / financial-services company in Madrid).

Carlos works across derivatives valuation, market-risk, FX exposure, P&L attribution, and reporting. Tools he uses regularly:
- BOS (Back Office System) — bookings, confirmations, trade lifecycle
- Quantum — valuation / pricing engine for derivatives (spot, forwards, options)
- BigQuery / dbt — data-warehouse modelling layer; SQL transformations
- VS Code — Python and SQL editing
- Excel — analysis, reconciliations, ad-hoc modelling, pivots
- Outlook / Teams — communication
- Chrome / Edge — Confluence, Jira, Looker, internal tools

Your job is NOT just to label projects. Go deep. For each project:

1. GROUP the day's activities into PROJECTS based on what Carlos was actually working on.

2. INSIDE each project, identify the SPECIFIC SUB-TASKS being executed. Don't say "worked on Quantum" — say what specifically in Quantum: "reconciled spot-option PVs between Quantum and the data-warehouse curve table", "investigated a 12bp discrepancy on EURUSD forward valuations", "loaded month-end positions for hedge-effectiveness testing", etc. Read window titles, file names, sheet names, SQL fragments, and visible content closely.

3. DETECT RECONCILIATIONS and CROSS-SYSTEM WORKFLOWS. When Carlos alternates between two data sources or applications (Quantum vs BigQuery, BOS vs Excel, dbt vs warehouse output, Excel vs report), infer what is being compared and why. Always name BOTH sources and the artefact under reconciliation. Common patterns to watch for:
   - same trade ID / position appearing in two systems back-to-back
   - copy-paste between Excel and a SQL result
   - vlookup / xlookup / match formulas → reconciliation in progress
   - pivot tables built from one source compared to a number from another
   - SQL queries filtering on a value that appeared on another screen seconds earlier

4. SURFACE STRUGGLES and BLOCKERS. Look for these signals:
   - long stretches on the same screen with no progress (stuck reading, debugging)
   - rapid back-and-forth between the same two windows (re-checking, hunting a discrepancy)
   - error messages, red text, NULL / N/A / 0 values, stack traces visible
   - repeated browser searches, Stack Overflow / docs / Confluence visits on the same topic
   - long pause on a query result (likely investigation)
   - many small queries refining the same WHERE clause (search for a culprit row)
   For each struggle, CITE the evidence: the time range AND what specifically you saw on screen.

5. ESTIMATE time per project AND per sub-task using the session table.

6. ASSIGN every session to a project — no gaps in the timeline.

Window titles are your best clue. Screenshots add visual confirmation; read what's on screen, including SQL fragments, formulas, function names, error text, and column headers.

Never leave a session unassigned. Make your best guess and flag uncertainty in the description.

Do NOT produce a Questions section. Always deliver the complete analysis.
"""

USER_PROMPT_TEMPLATE = """\
Carlos's activity log for {target_date}, extracted from screenshot filenames.
Each row: time | app | window title | estimated minutes on that window.

{session_table}

{n_images} screenshots follow for visual context. Read what's on screen — column headers, query text, error messages, formulas, sheet names, file paths.

Produce the following — follow the format exactly so the dashboard can parse it:

## {target_date} — Work Summary

### [Project name] — [Xh Ym]

**Summary:** 2–4 sentences. Concrete, specific. What this project was about today and what moved.

**Sub-tasks:**
- [HH:MM–HH:MM] [Specific sub-task title] — what concretely was done. Cite the window/file/sheet/query when relevant.
- [HH:MM–HH:MM] [Next sub-task] — ...
(one bullet per identifiable sub-task within this project; merge anything <10 minutes into adjacent ones)

**Cross-system / reconciliation activity:**
- If Carlos compared two systems / data sources, describe what was reconciled and which sources. Example: "Compared spot-option PVs in Quantum to data-warehouse curve outputs — investigated a 12bp discrepancy on EURUSD positions, traced to a stale curve in the warehouse."
- If none, write: "None detected."

**Struggles & blockers:**
- Each struggle as one bullet with evidence and time range. Example: "12:40–13:05 — bouncing between Quantum and BigQuery on the same trade ID 4421, suggesting a value mismatch; eventually found a missing CSA flag."
- If none observed, write: "None detected."

**Tools used:** comma-separated list of the specific apps / systems (Quantum, BigQuery, Excel, etc.).

### [Next project] — [Xh Ym]
...

(one section per distinct project, sorted by time spent, most to least)

---

## Time Summary
| Project | Time | % |
|---------|------|---|
| [name] | Xh Ym | N% |
| **Total active** | **Xh Ym** | |

---

## TIME_DATA
[project name]|[total minutes as integer]
[project name]|[total minutes as integer]

---

## TIMELINE_DATA
[HH:MM]|[HH:MM]|[project name]
[HH:MM]|[HH:MM]|[project name]
(one row per contiguous block on a project; every session must be covered; use session table start times)

---

## SUBTASK_DATA
[HH:MM]|[HH:MM]|[project name]|[short sub-task title, max 50 chars]
[HH:MM]|[HH:MM]|[project name]|[next sub-task]
(one row per sub-task identified above; titles must be short and concrete — "Reconcile EURUSD spot-option PVs", "Debug dbt schema test", "Investigate trade 4421 mismatch". Same TIME windows as the Sub-tasks bullets above.)

---

## STRUGGLE_DATA
[HH:MM]|[HH:MM]|[project name]|[kind]|[summary, max 80 chars]
[HH:MM]|[HH:MM]|[project name]|[kind]|[next item]
(kind is one of: reconciliation, struggle, blocker. One row for each item from the "Cross-system / reconciliation activity" and "Struggles & blockers" sections above. Use kind=reconciliation for cross-system comparisons; kind=struggle for stuck/back-and-forth/error-hunting; kind=blocker only when there is an explicit blocker. If a section is empty, output no rows for that kind.)
"""


# ── Knowledge base ────────────────────────────────────────────────────────

def load_knowledge() -> str:
    if KNOWLEDGE_FILE.exists():
        return KNOWLEDGE_FILE.read_text(encoding='utf-8').strip()
    return ''


def append_knowledge(target_date: str, questions_section: str, answers: str):
    KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = f'\n\n### {target_date}\n{questions_section.strip()}\n\n**Carlos answered:**\n{answers}\n'
    if not KNOWLEDGE_FILE.exists():
        KNOWLEDGE_FILE.write_text(
            '# Pulsar Context Knowledge\n\n'
            'Clarifications provided by Carlos, accumulated over time. '
            'Loaded automatically into every analysis.\n'
            + entry,
            encoding='utf-8',
        )
    else:
        with KNOWLEDGE_FILE.open('a', encoding='utf-8') as f:
            f.write(entry)


# ── Session parsing ───────────────────────────────────────────────────────

def parse_sessions(shots: list[Path], idle_cap_min: int = 5) -> list[dict]:
    """Extract time/app/title/duration/idle from screenshot filenames."""
    rows = []
    for shot in sorted(shots):
        parts = shot.stem.split('__')
        try:
            t       = parts[0].replace('-', ':')
            h, m, s = map(int, t.split(':'))
            tmin    = h * 60 + m + s / 60
        except (ValueError, IndexError):
            continue

        app   = parts[1] if len(parts) > 1 else 'unknown'
        idle  = None
        # Trailing `iNNN` token (added by capture.py) carries idle seconds.
        title_parts = parts[2:]
        if title_parts and title_parts[-1].startswith('i') and title_parts[-1][1:].isdigit():
            idle = int(title_parts[-1][1:])
            title_parts = title_parts[:-1]
        title = '__'.join(title_parts)

        rows.append({
            'tmin':  tmin,
            'time':  f'{h:02d}:{m:02d}',
            'app':   app,
            'title': title,
            'idle':  idle,
            'dur':   0.0,
        })
    for i in range(len(rows) - 1):
        rows[i]['dur'] = min(rows[i + 1]['tmin'] - rows[i]['tmin'], idle_cap_min)
    if rows:
        rows[-1]['dur'] = 2.0
    return rows


def format_session_table(rows: list[dict]) -> str:
    lines = [f"{'Time':<7}| {'App':<18}| {'Window Title':<45}| Min"]
    lines.append('─' * 78)
    for r in rows:
        lines.append(
            f"{r['time']:<7}| {r['app'][:17]:<18}| {r['title'][:44]:<45}| {r['dur']:.0f}"
        )
    return '\n'.join(lines)


# ── Screenshot loading ────────────────────────────────────────────────────

def load_screenshots(screenshots_dir: Path) -> list[dict]:
    return [
        {
            'filename': f.name,
            'data': base64.standard_b64encode(f.read_bytes()).decode('utf-8'),
        }
        for f in sorted(screenshots_dir.glob('*.jpg'))
    ]


def select_keyframes(screenshots: list[dict], target: int) -> list[dict]:
    if len(screenshots) <= target:
        return screenshots
    step = len(screenshots) / target
    return [screenshots[int(i * step)] for i in range(target)]


# ── Prompt building ───────────────────────────────────────────────────────

def build_initial_content(keyframes: list[dict], target_date: str,
                          knowledge: str, session_table: str = '') -> list[dict]:
    content: list[dict] = [
        {'type': 'text', 'text': SYSTEM_CONTEXT, 'cache_control': {'type': 'ephemeral'}},
    ]
    if knowledge:
        content.append({
            'type': 'text',
            'text': f'## Context from previous clarifications\n\n{knowledge}',
            'cache_control': {'type': 'ephemeral'},
        })
    content.append({
        'type': 'text',
        'text': USER_PROMPT_TEMPLATE.format(
            target_date=target_date,
            session_table=session_table or '(no session data)',
            n_images=len(keyframes),
        ),
    })
    for shot in keyframes:
        parts      = shot['filename'].split('__')
        time_label = parts[0].replace('-', ':')
        app_label  = parts[1] if len(parts) > 1 else ''
        content.append({'type': 'text', 'text': f'\n[{time_label} — {app_label}]'})
        content.append({
            'type': 'image',
            'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': shot['data']},
        })
    return content


def split_response(text: str) -> tuple[str, str]:
    marker = '## Questions for Carlos'
    if marker in text:
        idx = text.index(marker)
        return text[:idx].rstrip(), text[idx:].strip()
    return text.strip(), ''


# ── CLI helpers ───────────────────────────────────────────────────────────

def collect_clarification(questions_section: str) -> str:
    print('\n' + '─' * 60)
    print('Claude has questions to better understand your workday.')
    print('─' * 60)
    print(questions_section.strip())
    print('─' * 60)
    print('Your answers (type END on a blank line when finished):\n')
    lines = []
    while True:
        line = input()
        if line.strip().upper() == 'END':
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def stream_response(client: anthropic.Anthropic, messages: list[dict]) -> str:
    result = ''
    with client.messages.stream(
        model='claude-opus-4-7',
        max_tokens=16000,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end='', flush=True)
            result += chunk
    print('\n')
    return result


# ── Entry point ───────────────────────────────────────────────────────────

def analyze_day(target_date: str | None = None):
    if target_date is None:
        target_date = date.today().strftime('%Y-%m-%d')

    base_dir        = Path(__file__).parent
    screenshots_dir = base_dir / 'logs' / target_date / 'screenshots'

    if not screenshots_dir.exists() or not any(screenshots_dir.glob('*.jpg')):
        print(f'No screenshots found for {target_date} in {screenshots_dir}')
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(base_dir / 'config.ini')

    api_key = (cfg.get('api', 'anthropic_api_key', fallback='').strip()
               or os.environ.get('ANTHROPIC_API_KEY', ''))
    if not api_key:
        print('ERROR: Set anthropic_api_key in config.ini or ANTHROPIC_API_KEY env var.')
        sys.exit(1)

    kf_n          = cfg.getint('analysis', 'keyframes_per_day', fallback=30)
    shot_paths    = sorted(screenshots_dir.glob('*.jpg'))
    sessions      = parse_sessions(shot_paths)
    session_table = format_session_table(sessions)
    screenshots   = load_screenshots(screenshots_dir)
    keyframes     = select_keyframes(screenshots, kf_n)
    print(f'{len(screenshots)} screenshots → {len(keyframes)} keyframes selected')

    knowledge = load_knowledge()
    if knowledge:
        print('Knowledge base loaded.\n')

    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{'role': 'user', 'content': build_initial_content(
        keyframes, target_date, knowledge, session_table)}]

    print('Sending to Claude...\n')
    response1 = stream_response(client, messages)
    analysis, questions_section = split_response(response1)

    if questions_section:
        answers = collect_clarification(questions_section)
        if answers:
            append_knowledge(target_date, questions_section, answers)
            messages.append({'role': 'assistant', 'content': response1})
            messages.append({
                'role': 'user',
                'content': (
                    f'Clarifications:\n{answers}\n\n'
                    'Now produce the final complete analysis incorporating these answers. '
                    'Do not include a Questions section.'
                ),
            })
            print('\nFinalizing analysis with your clarifications...\n')
            analysis = stream_response(client, messages)

    output_dir = base_dir / 'logs' / target_date
    (output_dir / 'analysis.md').write_text(analysis, encoding='utf-8')
    print(f'Analysis saved → logs/{target_date}/analysis.md')


if __name__ == '__main__':
    analyze_day(sys.argv[1] if len(sys.argv) > 1 else None)
