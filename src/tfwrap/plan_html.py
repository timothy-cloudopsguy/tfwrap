"""Render Terraform plan JSON as a self-contained HTML report."""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

ACTION_ORDER = ('create', 'update', 'delete', 'replace', 'no-op')
ACTION_LABELS = {
  'create': 'Create',
  'update': 'Update',
  'delete': 'Delete',
  'replace': 'Replace',
  'no-op': 'No change',
}


def classify_actions(actions: Sequence[str]) -> str:
  ordered = list(actions)
  if ordered == ['no-op']:
    return 'no-op'
  if set(ordered) == {'delete', 'create'} or set(ordered) == {'create', 'delete'}:
    return 'replace'
  if ordered == ['create']:
    return 'create'
  if ordered == ['update']:
    return 'update'
  if ordered == ['delete']:
    return 'delete'
  return '+'.join(ordered)


def _is_sensitive(sensitive: Any, path: Tuple[str, ...]) -> bool:
  if sensitive is True:
    return True
  if not isinstance(sensitive, dict) or not path:
    return False
  head, *tail = path
  if head not in sensitive:
    return False
  if not tail:
    return sensitive[head] is True
  return _is_sensitive(sensitive[head], tuple(tail))


def _format_value(value: Any, *, sensitive: bool, unknown: bool) -> str:
  if unknown:
    return '<span class="unknown">(known after apply)</span>'
  if sensitive:
    return '<span class="sensitive">(sensitive value)</span>'
  if value is None:
    return '<span class="null">null</span>'
  if isinstance(value, (dict, list)):
    text = json.dumps(value, indent=2, sort_keys=True)
    if len(text) > 4000:
      text = text[:4000] + '\n… (truncated)'
    return f'<pre class="value-json">{html.escape(text)}</pre>'
  text = str(value)
  if len(text) > 2000:
    text = text[:2000] + '… (truncated)'
  return f'<span class="value-scalar">{html.escape(text)}</span>'


def _collect_diffs(
  before: Any,
  after: Any,
  before_sensitive: Any,
  after_sensitive: Any,
  after_unknown: Any,
  path: Tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
  """Return attribute paths that differ between before and after."""
  diffs: List[Dict[str, Any]] = []

  if isinstance(before, dict) and isinstance(after, dict):
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
      diffs.extend(
        _collect_diffs(
          before.get(key),
          after.get(key),
          before_sensitive,
          after_sensitive,
          after_unknown,
          path + (key,),
        )
      )
    return diffs

  if before == after:
    return diffs

  attr = '.'.join(path) if path else '(root)'
  unknown = _is_sensitive(after_unknown, path) if isinstance(after_unknown, dict) else False
  sens = _is_sensitive(before_sensitive, path) or _is_sensitive(after_sensitive, path)

  diffs.append({
    'attribute': attr,
    'before': before,
    'after': after,
    'sensitive': sens,
    'unknown': unknown,
  })
  return diffs


def _summarize_plan(plan: Dict[str, Any]) -> Dict[str, int]:
  counts = {k: 0 for k in ACTION_ORDER}
  for resource in plan.get('resource_changes', []):
    action = classify_actions(resource.get('change', {}).get('actions', []))
    if action in counts:
      counts[action] += 1
    else:
      counts[action] = counts.get(action, 0) + 1
  return counts


def _resource_rows(plan: Dict[str, Any], *, show_noop: bool) -> List[Dict[str, Any]]:
  rows: List[Dict[str, Any]] = []
  for resource in plan.get('resource_changes', []):
    change = resource.get('change', {})
    action = classify_actions(change.get('actions', []))
    if action == 'no-op' and not show_noop:
      continue

    before = change.get('before')
    after = change.get('after')
    diffs = _collect_diffs(
      before if before is not None else {},
      after if after is not None else {},
      change.get('before_sensitive') or {},
      change.get('after_sensitive') or {},
      change.get('after_unknown') or {},
    )

    rows.append({
      'address': resource.get('address', ''),
      'type': resource.get('type', ''),
      'mode': resource.get('mode', ''),
      'provider': resource.get('provider_name', ''),
      'module': resource.get('module_address', ''),
      'action': action,
      'actions': change.get('actions', []),
      'diffs': diffs,
    })

  rows.sort(key=lambda r: (ACTION_ORDER.index(r['action']) if r['action'] in ACTION_ORDER else 99, r['address']))
  return rows


def render_plan_html(plan: Dict[str, Any], *, title: str = 'Terraform Plan', show_noop: bool = False) -> str:
  """Return a complete HTML document for a Terraform plan JSON object."""
  counts = _summarize_plan(plan)
  rows = _resource_rows(plan, show_noop=show_noop)
  variables = plan.get('variables') or {}
  timestamp = plan.get('timestamp', '')
  tf_version = plan.get('terraform_version', '')
  generated = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

  summary_cards = []
  for action in ACTION_ORDER:
    count = counts.get(action, 0)
    if action == 'no-op' and not show_noop and count:
      summary_cards.append(
        f'<div class="card card-noop muted"><span class="count">{count}</span>'
        f'<span class="label">{ACTION_LABELS[action]} (hidden)</span></div>'
      )
      continue
    if count == 0:
      continue
    summary_cards.append(
      f'<div class="card card-{action}"><span class="count">{count}</span>'
      f'<span class="label">{ACTION_LABELS[action]}</span></div>'
    )

  var_rows = []
  for name, meta in sorted(variables.items()):
    value = meta.get('value', '') if isinstance(meta, dict) else meta
    var_rows.append(
      '<tr>'
      f'<td><code>{html.escape(name)}</code></td>'
      f'<td><code>{html.escape(json.dumps(value))}</code></td>'
      '</tr>'
    )

  resource_blocks = []
  for row in rows:
    diff_rows = []
    for diff in row['diffs']:
      diff_rows.append(
        '<tr>'
        f'<td class="attr"><code>{html.escape(diff["attribute"])}</code></td>'
        f'<td class="before">{_format_value(diff["before"], sensitive=diff["sensitive"], unknown=False)}</td>'
        f'<td class="after">{_format_value(diff["after"], sensitive=diff["sensitive"], unknown=diff["unknown"])}</td>'
        '</tr>'
      )

    diff_table = ''
    if diff_rows:
      diff_table = (
        '<table class="diff-table"><thead><tr>'
        '<th>Attribute</th><th>Before</th><th>After</th>'
        '</tr></thead><tbody>'
        + ''.join(diff_rows)
        + '</tbody></table>'
      )
    elif row['action'] == 'create':
      diff_table = '<p class="hint">New resource — values will be created at apply time.</p>'
    elif row['action'] == 'delete':
      diff_table = '<p class="hint">Resource will be destroyed.</p>'
    else:
      diff_table = '<p class="hint">No attribute differences detected.</p>'

    module_line = ''
    if row['module']:
      module_line = f'<div class="module">Module: <code>{html.escape(row["module"])}</code></div>'

    resource_blocks.append(
      f'<article class="resource action-{row["action"]}" data-action="{html.escape(row["action"])}">'
      f'<header class="resource-header">'
      f'<span class="badge badge-{row["action"]}">{html.escape(ACTION_LABELS.get(row["action"], row["action"]))}</span>'
      f'<h2><code>{html.escape(row["address"])}</code></h2>'
      f'<div class="meta">'
      f'<span>{html.escape(row["type"])}</span>'
      f'<span>{html.escape(row["provider"].split("/")[-1])}</span>'
      f'</div>'
      f'{module_line}'
      f'</header>'
      f'<div class="resource-body">{diff_table}</div>'
      f'</article>'
    )

  applyable = plan.get('applyable')
  applyable_text = ''
  if applyable is not None:
    applyable_text = 'Yes' if applyable else 'No'

  variables_section = ''
  if variables:
    var_body = ''.join(var_rows) if var_rows else '<tr><td colspan="2">No variables</td></tr>'
    variables_section = (
      '<section class="block">'
      '<h2>Input variables</h2>'
      f'<table class="vars"><tbody>{var_body}</tbody></table>'
      '</section>'
    )

  noop_filter_btn = (
    '<button type="button" data-filter="no-op">No change</button>' if show_noop else ''
  )
  plan_time_line = f'<span>Plan time: {html.escape(timestamp)}</span>' if timestamp else ''
  applyable_line = f'<span>Applyable: {html.escape(applyable_text)}</span>' if applyable_text else ''

  return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #0f1419;
      --surface: #1a2332;
      --surface2: #243044;
      --text: #e6edf3;
      --muted: #8b949e;
      --border: #30363d;
      --create: #3fb950;
      --update: #d29922;
      --delete: #f85149;
      --replace: #a371f7;
      --noop: #6e7681;
      --accent: #58a6ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem; }}
    header.page-header {{
      margin-bottom: 1.5rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border);
    }}
    header.page-header h1 {{ margin: 0 0 .5rem; font-size: 1.75rem; }}
    .meta-line {{ color: var(--muted); font-size: .9rem; }}
    .meta-line span + span::before {{ content: " · "; }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: .75rem;
      margin-bottom: 1.5rem;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: .75rem 1rem;
      min-width: 110px;
      border-left: 4px solid var(--muted);
    }}
    .card .count {{ display: block; font-size: 1.5rem; font-weight: 700; }}
    .card .label {{ color: var(--muted); font-size: .85rem; }}
    .card-create {{ border-left-color: var(--create); }}
    .card-update {{ border-left-color: var(--update); }}
    .card-delete {{ border-left-color: var(--delete); }}
    .card-replace {{ border-left-color: var(--replace); }}
    .card-noop {{ border-left-color: var(--noop); }}
    .card.muted {{ opacity: .75; }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: .5rem;
      margin-bottom: 1rem;
      align-items: center;
    }}
    .toolbar button {{
      background: var(--surface2);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: .35rem .75rem;
      cursor: pointer;
      font-size: .85rem;
    }}
    .toolbar button:hover, .toolbar button.active {{
      border-color: var(--accent);
      color: var(--accent);
    }}
    section.block {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 1.5rem;
      overflow: hidden;
    }}
    section.block h2 {{
      margin: 0;
      padding: .75rem 1rem;
      font-size: 1rem;
      background: var(--surface2);
      border-bottom: 1px solid var(--border);
    }}
    table.vars {{ width: 100%; border-collapse: collapse; }}
    table.vars td {{ padding: .5rem 1rem; border-top: 1px solid var(--border); vertical-align: top; }}
    .resources {{ display: flex; flex-direction: column; gap: 1rem; }}
    .resource {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .resource-header {{ padding: 1rem; border-bottom: 1px solid var(--border); }}
    .resource-header h2 {{
      margin: .5rem 0 .25rem;
      font-size: 1rem;
      font-weight: 600;
      word-break: break-all;
    }}
    .resource-header .meta {{
      color: var(--muted);
      font-size: .85rem;
      display: flex;
      gap: .75rem;
      flex-wrap: wrap;
    }}
    .module {{ margin-top: .35rem; font-size: .85rem; color: var(--muted); }}
    .badge {{
      display: inline-block;
      font-size: .75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .03em;
      padding: .15rem .5rem;
      border-radius: 999px;
    }}
    .badge-create {{ background: rgba(63,185,80,.2); color: var(--create); }}
    .badge-update {{ background: rgba(210,153,34,.2); color: var(--update); }}
    .badge-delete {{ background: rgba(248,81,73,.2); color: var(--delete); }}
    .badge-replace {{ background: rgba(163,113,247,.2); color: var(--replace); }}
    .badge-no-op {{ background: rgba(110,118,129,.2); color: var(--noop); }}
    .resource-body {{ padding: 0 1rem 1rem; }}
    .diff-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: .85rem;
      margin-top: .75rem;
    }}
    .diff-table th, .diff-table td {{
      border: 1px solid var(--border);
      padding: .5rem .65rem;
      vertical-align: top;
      text-align: left;
    }}
    .diff-table th {{ background: var(--surface2); color: var(--muted); font-weight: 600; }}
    .diff-table .attr {{ width: 22%; }}
    .diff-table .before {{ width: 39%; background: rgba(248,81,73,.06); }}
    .diff-table .after {{ width: 39%; background: rgba(63,185,80,.06); }}
    pre.value-json {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: .8rem;
      max-height: 280px;
      overflow: auto;
    }}
    .value-scalar {{ word-break: break-all; font-family: ui-monospace, monospace; font-size: .8rem; }}
    .sensitive {{ color: var(--update); font-style: italic; }}
    .unknown {{ color: var(--accent); font-style: italic; }}
    .null {{ color: var(--muted); font-style: italic; }}
    .hint {{ color: var(--muted); font-size: .9rem; margin: .75rem 0 0; }}
    footer {{ color: var(--muted); font-size: .8rem; margin-top: 2rem; text-align: center; }}
    .hidden-resource {{ display: none !important; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: .9em; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="page-header">
      <h1>{html.escape(title)}</h1>
      <div class="meta-line">
        <span>Terraform {html.escape(tf_version)}</span>
        {plan_time_line}
        {applyable_line}
        <span>Report generated: {html.escape(generated)}</span>
      </div>
    </header>

    <div class="summary">{''.join(summary_cards)}</div>

    <div class="toolbar" id="filters">
      <span style="color:var(--muted);font-size:.85rem;margin-right:.25rem">Show:</span>
      <button type="button" data-filter="all" class="active">All</button>
      <button type="button" data-filter="create">Create</button>
      <button type="button" data-filter="update">Update</button>
      <button type="button" data-filter="delete">Delete</button>
      <button type="button" data-filter="replace">Replace</button>
      {noop_filter_btn}
    </div>

    {variables_section}

    <div class="resources" id="resources">
      {''.join(resource_blocks) if resource_blocks else '<p class="hint">No resource changes to display.</p>'}
    </div>

    <footer>Generated by tfwrap</footer>
  </div>
  <script>
    (function() {{
      var buttons = document.querySelectorAll('#filters button');
      var resources = document.querySelectorAll('.resource[data-action]');
      buttons.forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          buttons.forEach(function(b) {{ b.classList.remove('active'); }});
          btn.classList.add('active');
          var filter = btn.getAttribute('data-filter');
          resources.forEach(function(el) {{
            if (filter === 'all' || el.getAttribute('data-action') === filter) {{
              el.classList.remove('hidden-resource');
            }} else {{
              el.classList.add('hidden-resource');
            }}
          }});
        }});
      }});
    }})();
  </script>
</body>
</html>'''


def write_plan_html_file(plan: Dict[str, Any], output_path: str, *, show_noop: bool = False, title: str = 'Terraform Plan') -> None:
  content = render_plan_html(plan, title=title, show_noop=show_noop)
  with open(output_path, 'w', encoding='utf-8') as f:
    f.write(content)


def load_plan_json(path: str) -> Dict[str, Any]:
  with open(path, 'r', encoding='utf-8') as f:
    return json.load(f)
