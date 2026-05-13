"""
Pulsar — weekly summary.
Reads all daily analyses for the current week and produces a higher-level pattern report + diagram.

Usage:
    python weekly.py               # current week
    python weekly.py 2026-05-01    # week containing this date
"""

import configparser
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic


def week_dates(reference: datetime) -> list[str]:
    monday = reference - timedelta(days=reference.weekday())
    return [(monday + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(5)]


def load_analyses(base_dir: Path, dates: list[str]) -> dict[str, str]:
    result = {}
    for d in dates:
        f = base_dir / 'logs' / d / 'analysis.md'
        if f.exists():
            result[d] = f.read_text(encoding='utf-8')
    return result


def generate_weekly(target_date: str | None = None):
    base_dir = Path(__file__).parent

    cfg = configparser.ConfigParser()
    cfg.read(base_dir / 'config.ini')

    api_key = cfg.get('api', 'anthropic_api_key', fallback='').strip() or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        print('ERROR: Set anthropic_api_key in config.ini or ANTHROPIC_API_KEY environment variable.')
        sys.exit(1)

    ref      = datetime.strptime(target_date, '%Y-%m-%d') if target_date else datetime.now()
    dates    = week_dates(ref)
    analyses = load_analyses(base_dir, dates)

    if not analyses:
        print(f'No daily analyses found for week of {dates[0]}')
        sys.exit(1)

    print(f'Building weekly summary from {len(analyses)} day(s): {", ".join(sorted(analyses))}')

    daily_content = '\n\n---\n\n'.join(
        f'## {day}\n\n{text}' for day, text in sorted(analyses.items())
    )

    prompt = f"""\
You are reviewing a week of work logs for Carlos, Finance Strategy Manager at Ebury (fintech FX company, Madrid).

Carlos's role: owns all company revenue models — FX deltas, valuations, P&L attribution.
He works alone, coordinates many teams, and is building agentic AI workflows.

Daily analyses:

{daily_content}

Produce a weekly review with this exact format:

## Weekly Review — Week of {dates[0]}

### What was accomplished
Key outputs, decisions, and progress made this week.

### Recurring workflows
Processes that appeared more than once. These are the best candidates for automation.

### Time distribution
Rough breakdown of where time went (e.g. data reconciliation 40%, analysis 30%, meetings 20%, dev 10%).

### Friction & blockers
What repeatedly slowed things down or required manual intervention.

### Patterns worth acting on
2–3 concrete observations about how work could be improved, automated, or delegated.

### Weekly Mermaid Diagram
```mermaid
flowchart TD
    [master diagram of all major workflows from the week — show data sources, tools, outputs, and decision points]
```
"""

    client = anthropic.Anthropic(api_key=api_key)
    result = ''

    with client.messages.stream(
        model='claude-opus-4-7',
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end='', flush=True)
            result += chunk

    print('\n')

    out_md  = base_dir / 'logs' / f'weekly_{dates[0]}.md'
    out_mmd = base_dir / 'logs' / f'weekly_{dates[0]}.mmd'

    out_md.write_text(result, encoding='utf-8')

    if '```mermaid' in result:
        start   = result.index('```mermaid') + 10
        end     = result.index('```', start)
        mermaid = result[start:end].strip()
        out_mmd.write_text(mermaid, encoding='utf-8')
        print(f'Mermaid diagram → logs/weekly_{dates[0]}.mmd')

    print(f'Weekly summary → logs/weekly_{dates[0]}.md')


if __name__ == '__main__':
    generate_weekly(sys.argv[1] if len(sys.argv) > 1 else None)
