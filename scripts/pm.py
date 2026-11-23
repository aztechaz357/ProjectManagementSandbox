#!/usr/bin/env python3
"""GitHub Projects の Insights でバーンダウン / バーンアップ / フィーバーチャートを試すためのモック管理スクリプト。

標準ライブラリ + gh CLI だけで動きます。

  python scripts/pm.py preview          # GitHub に触れず、想定されるチャートを docs/preview-*.svg に出力
  python scripts/pm.py setup            # Project・フィールド・Issue を作成し Day 0 の状態にする
  python scripts/pm.py tick [--day N]   # 今日 (または Day N) の状態まで Issue / Status を進める
  python scripts/pm.py fever [--day N]  # 実データから進捗率・バッファ消費率を計算し、トラッカーと SVG を更新
  python scripts/pm.py status           # 現在の Day と集計を表示
  python scripts/pm.py token [--push]   # .env の PROJECT_TOKEN を検証 (--push で Actions Secret に登録)

.env に PROJECT_TOKEN=ghp_xxx を書くと、gh の呼び出しにそのトークンを使います (GH_TOKEN が既にあればそちら優先)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOTENV = ROOT / ".env"
TOKEN_KEY = "PROJECT_TOKEN"
REQUIRED_SCOPES = {"repo", "project"}
CONFIG = ROOT / "mock" / "config.json"
BACKLOG = ROOT / "mock" / "backlog.json"
STATE = ROOT / "mock" / "state.json"
DOCS = ROOT / "docs"
HISTORY = DOCS / "fever-history.json"
FEVER_SVG = DOCS / "fever-chart.svg"

JST = dt.timezone(dt.timedelta(hours=9))
KEY_RE = re.compile(r"<!-- mock-key: (\S+) -->")

F_POINTS, F_SPRINT, F_STATUS = "Story Points", "Sprint", "Status"
F_FEVER_VALUE, F_FEVER_METRIC, F_FEVER_ZONE = "Fever Value", "Fever Metric", "Fever Zone"

TRACKERS = [
    {"key": "FEVER-PROGRESS", "metric": "Progress %", "title": "📈 Fever: 進捗率 (Progress %)"},
    {"key": "FEVER-BUFFER", "metric": "Buffer %", "title": "📈 Fever: バッファ消費率 (Buffer %)"},
]
ZONE_EMOJI = {"Green": "🟢", "Yellow": "🟡", "Red": "🔴"}


# --------------------------------------------------------------------------- utils
def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dotenv(path: Path = DOTENV) -> None:
    """KEY=VALUE 形式の .env を読み、未設定の環境変数だけ埋める。PROJECT_TOKEN は GH_TOKEN として gh に渡す。"""
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value and key not in os.environ:
                os.environ[key] = value
    if os.environ.get(TOKEN_KEY) and not os.environ.get("GH_TOKEN"):
        os.environ["GH_TOKEN"] = os.environ[TOKEN_KEY]


def gh(*args: str, json_out: bool = False, check: bool = True, stdin: str | None = None):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", input=stdin)
    if r.returncode != 0:
        if check:
            sys.exit(f"[error] gh {' '.join(args[:3])} failed:\n{r.stderr.strip()}")
        return None
    out = r.stdout.strip()
    return json.loads(out) if json_out else out


def gql(query: str, check: bool = True, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        args += ["-F", f"{k}={v}"] if isinstance(v, (int, bool)) else ["-f", f"{k}={v}"]
    res = gh(*args, json_out=True, check=check)
    return res["data"] if res else None


def q(s: str) -> str:
    """GraphQL 文字列リテラル."""
    return json.dumps(s, ensure_ascii=False)


# ------------------------------------------------------------------ business days
def is_bday(d: dt.date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def next_bday(d: dt.date, holidays: set[str]) -> dt.date:
    while not is_bday(d, holidays):
        d += dt.timedelta(days=1)
    return d


def day_index(start: dt.date, today: dt.date, holidays: set[str]) -> int:
    """開始日 = Day 0。土日祝は直前の営業日と同じ Day。開始前は -1。"""
    if today < start:
        return -1
    n, d = -1, start
    while d <= today:
        if is_bday(d, holidays):
            n += 1
        d += dt.timedelta(days=1)
    return max(n, 0)


def date_of_day(start: dt.date, day: int, holidays: set[str]) -> dt.date:
    d, n = next_bday(start, holidays), 0
    while n < day:
        d += dt.timedelta(days=1)
        if is_bday(d, holidays):
            n += 1
    return d


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


# ------------------------------------------------------------------ simulation
def sim_status(item: dict, day: int) -> str:
    if day >= item["done_day"]:
        return "Done"
    if day >= item["start_day"]:
        return "In Progress"
    return "Todo"


def fever_point(done: float, total: float, elapsed: int, plan: dict) -> tuple[float, float]:
    """CCPM の簡易式: 進捗率 p、バッファ消費率 = max(0, 経過日数 - p×クリティカルチェーン日数) / バッファ日数。"""
    p = done / total if total else 0.0
    used = max(0.0, elapsed - p * plan["critical_chain_days"]) / plan["project_buffer_days"]
    return p, used


def fever_zone(p: float, b: float, zones: dict) -> str:
    def line(z):
        return z[0] + (z[1] - z[0]) * p

    if b >= line(zones["red"]):
        return "Red"
    if b >= line(zones["yellow"]):
        return "Yellow"
    return "Green"


def simulate(cfg: dict, backlog: list[dict], last_day: int) -> list[dict]:
    rows = []
    for d in range(last_day + 1):
        live = [i for i in backlog if i.get("added_day", 0) <= d]
        total = sum(i["points"] for i in live)
        done = sum(i["points"] for i in live if sim_status(i, d) == "Done")
        p, b = fever_point(done, total, d, cfg["plan"])
        per_sprint = {
            s["title"]: sum(i["points"] for i in live if i["sprint"] == s["title"] and sim_status(i, d) != "Done")
            for s in cfg["sprints"]
        }
        rows.append({"day": d, "done_points": done, "total_points": total, "remaining": total - done,
                     "progress": round(p * 100, 1), "buffer": round(b * 100, 1),
                     "zone": fever_zone(p, b, cfg["fever_zones"]), "sprint_remaining": per_sprint})
    return rows


# ------------------------------------------------------------------ SVG
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_line_chart(path: Path, title: str, xlabels: list[str], series: list[dict], ylabel: str) -> None:
    W, H, L, R, T, B = 720, 400, 60, 170, 50, 50
    pw, ph = W - L - R, H - T - B
    ymax = max(max(v for v in s["values"] if v is not None) for s in series) or 1
    ymax = int(ymax * 1.1) + 1
    n = len(xlabels)

    def x(i):
        return L + pw * i / max(n - 1, 1)

    def y(v):
        return T + ph * (1 - v / ymax)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="28" font-size="16" font-weight="bold" fill="#24292f">{_esc(title)}</text>']
    for k in range(6):
        v = ymax * k / 5
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#eaeef2"/>')
        out.append(f'<text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#57606a">{v:.0f}</text>')
    for i, lab in enumerate(xlabels):
        out.append(f'<text x="{x(i):.1f}" y="{T + ph + 18}" text-anchor="middle" fill="#57606a">{_esc(lab)}</text>')
    out.append(f'<text x="16" y="{T + ph / 2}" transform="rotate(-90 16 {T + ph / 2})" text-anchor="middle" fill="#57606a">{_esc(ylabel)}</text>')
    out.append(f'<line x1="{L}" x2="{L}" y1="{T}" y2="{T + ph}" stroke="#8c959f"/>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}" stroke="#8c959f"/>')
    for si, s in enumerate(series):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s["values"]) if v is not None)
        dash = ' stroke-dasharray="6 4"' if s.get("dashed") else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" stroke-width="2.5"{dash}/>')
        if not s.get("dashed"):
            for i, v in enumerate(s["values"]):
                if v is not None:
                    out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" fill="{s["color"]}"/>')
        ly = T + 10 + si * 22
        out.append(f'<line x1="{L + pw + 16}" x2="{L + pw + 40}" y1="{ly}" y2="{ly}" stroke="{s["color"]}" stroke-width="2.5"{dash}/>')
        out.append(f'<text x="{L + pw + 46}" y="{ly + 4}" fill="#24292f">{_esc(s["name"])}</text>')
    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def svg_fever_chart(path: Path, history: list[dict], zones: dict, title: str) -> None:
    W, H, L, R, T, B = 640, 480, 70, 30, 50, 60
    pw, ph = W - L - R, H - T - B
    ymax = max([120] + [h["buffer"] + 10 for h in history])

    def x(p):
        return L + pw * p / 100

    def y(b):
        return T + ph * (1 - b / ymax)

    def zline(z, p):
        return (z[0] + (z[1] - z[0]) * p / 100) * 100

    yl, rl = zones["yellow"], zones["red"]
    green = f"{x(0)},{y(0)} {x(100)},{y(0)} {x(100)},{y(zline(yl, 100))} {x(0)},{y(zline(yl, 0))}"
    yellow = f"{x(0)},{y(zline(yl, 0))} {x(100)},{y(zline(yl, 100))} {x(100)},{y(zline(rl, 100))} {x(0)},{y(zline(rl, 0))}"
    red = f"{x(0)},{y(zline(rl, 0))} {x(100)},{y(zline(rl, 100))} {x(100)},{y(ymax)} {x(0)},{y(ymax)}"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="30" font-size="16" font-weight="bold" fill="#24292f">{_esc(title)}</text>',
           f'<polygon points="{green}" fill="#dafbe1"/>',
           f'<polygon points="{yellow}" fill="#fff8c5"/>',
           f'<polygon points="{red}" fill="#ffebe9"/>']
    for k in range(0, 101, 20):
        out.append(f'<text x="{x(k):.1f}" y="{T + ph + 18}" text-anchor="middle" fill="#57606a">{k}%</text>')
    for k in range(0, int(ymax) + 1, 20):
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(k):.1f}" y2="{y(k):.1f}" stroke="#00000014"/>')
        out.append(f'<text x="{L - 8}" y="{y(k) + 4:.1f}" text-anchor="end" fill="#57606a">{k}%</text>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(100):.1f}" y2="{y(100):.1f}" stroke="#cf222e" stroke-dasharray="4 4"/>')
    out.append(f'<text x="{L + pw - 4}" y="{y(100) - 6:.1f}" text-anchor="end" fill="#cf222e">バッファ枯渇 (納期遅延)</text>')
    out.append(f'<text x="{L + pw / 2}" y="{H - 16}" text-anchor="middle" fill="#24292f">進捗率 (完了ポイント / 総ポイント)</text>')
    out.append(f'<text x="18" y="{T + ph / 2}" transform="rotate(-90 18 {T + ph / 2})" text-anchor="middle" fill="#24292f">バッファ消費率</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#8c959f"/>')
    if history:
        pts = " ".join(f"{x(h['progress']):.1f},{y(h['buffer']):.1f}" for h in history)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#24292f" stroke-width="2"/>')
        color = {"Green": "#1a7f37", "Yellow": "#9a6700", "Red": "#cf222e"}
        for i, h in enumerate(history):
            last = i == len(history) - 1
            out.append(f'<circle cx="{x(h["progress"]):.1f}" cy="{y(h["buffer"]):.1f}" r="{6 if last else 4}" '
                       f'fill="{color[h["zone"]]}" stroke="#fff" stroke-width="1.5"/>')
            out.append(f'<text x="{x(h["progress"]) + 7:.1f}" y="{y(h["buffer"]) - 7:.1f}" fill="#24292f" font-size="11">D{h["day"]}</text>')
    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ GitHub access
FIELDS_QUERY = """
query($id: ID!) { node(id: $id) { ... on ProjectV2 {
  fields(first: 50) { nodes {
    __typename
    ... on ProjectV2FieldCommon { id name }
    ... on ProjectV2SingleSelectField { options { id name } }
    ... on ProjectV2IterationField { configuration {
      iterations { id title startDate } completedIterations { id title startDate } } }
  } } } } }"""

ITEMS_QUERY = """
query($id: ID!, $after: String) { node(id: $id) { ... on ProjectV2 {
  items(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id
      content { __typename ... on Issue { id number title state body url } }
      fieldValues(first: 20) { nodes {
        __typename
        ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
        ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
        ... on ProjectV2ItemFieldIterationValue { title field { ... on ProjectV2FieldCommon { name } } }
      } } } } } } }"""


def get_fields(pid: str) -> dict:
    nodes = gql(FIELDS_QUERY, id=pid)["node"]["fields"]["nodes"]
    out = {}
    for f in nodes:
        if "name" not in f:
            continue
        entry = {"id": f["id"], "type": f["__typename"]}
        if "options" in f:
            entry["options"] = {o["name"]: o["id"] for o in f["options"]}
        if "configuration" in f:
            c = f["configuration"]
            entry["iterations"] = {i["title"]: i["id"] for i in c["iterations"] + c["completedIterations"]}
        out[f["name"]] = entry
    return out


def get_items(pid: str) -> dict:
    """mock-key -> {item_id, number, state, url, values{field: value}}"""
    out, after = {}, None
    while True:
        data = gql(ITEMS_QUERY, id=pid, after=after)["node"]["items"]
        for n in data["nodes"]:
            c = n.get("content") or {}
            m = KEY_RE.search(c.get("body") or "")
            if c.get("__typename") != "Issue" or not m:
                continue
            values = {}
            for v in n["fieldValues"]["nodes"]:
                if "field" in v and v["field"]:
                    values[v["field"]["name"]] = v.get("number", v.get("name", v.get("title")))
            out[m.group(1)] = {"item_id": n["id"], "number": c["number"], "state": c["state"], "url": c["url"], "values": values}
        if not data["pageInfo"]["hasNextPage"]:
            return out
        after = data["pageInfo"]["endCursor"]


def get_issues(repo: str) -> dict:
    issues = []
    for label in ("mock", "fever"):
        issues += gh("issue", "list", "--repo", repo, "--state", "all", "--limit", "500",
                     "--label", label, "--json", "number,body,url,state", json_out=True)
    out = {}
    for i in issues:
        m = KEY_RE.search(i["body"] or "")
        if m:
            out[m.group(1)] = i
    return out


def set_field(st: dict, item_id: str, field: dict, *, number=None, option=None, iteration=None) -> None:
    args = ["project", "item-edit", "--id", item_id, "--project-id", st["project_id"], "--field-id", field["id"]]
    if number is not None:
        args += ["--number", str(number)]
    elif option is not None:
        args += ["--single-select-option-id", field["options"][option]]
    elif iteration is not None:
        args += ["--iteration-id", field["iterations"][iteration]]
    gh(*args)


def ensure_issue(st: dict, key: str, title: str, body: str, labels: list[str], issues: dict, items: dict) -> dict:
    """Issue を作成 (なければ) し、Project に追加 (なければ) して item 情報を返す。"""
    if key in items:
        return items[key]
    if key in issues:
        url = issues[key]["url"]
    else:
        full_body = f"{body}\n\n{'<!-- mock-key: ' + key + ' -->'}".strip()
        args = ["issue", "create", "--repo", st["repo"], "--title", title, "--body", full_body]
        for lab in labels:
            args += ["--label", lab]
        url = gh(*args).splitlines()[-1]
        print(f"  + issue {url}  {title}")
    added = gh("project", "item-add", str(st["project_number"]), "--owner", st["owner"], "--url", url,
               "--format", "json", json_out=True)
    number = int(url.rstrip("/").split("/")[-1])
    items[key] = {"item_id": added["id"], "number": number, "state": "OPEN", "url": url, "values": {}}
    return items[key]


def tracker_body(st: dict, cfg: dict, history: list[dict]) -> str:
    plan = cfg["plan"]
    lines = [
        "フィーバーチャート (CCPM のバッファ管理) 用のトラッカー Issue です。",
        "`scripts/pm.py fever` が毎営業日、`Fever Value` フィールドとこの本文を自動更新します。",
        "Insights では `label:fever` で絞り込み、`Fever Value` を `Fever Metric` ごとに表示します。",
        "",
        f"- クリティカルチェーン: **{plan['critical_chain_days']} 営業日** / プロジェクトバッファ: **{plan['project_buffer_days']} 営業日**",
        f"- 開始日: {st['start_date']}",
        f"- 本来のフィーバーチャート (散布図): https://github.com/{st['repo']}/blob/main/docs/fever-chart.svg",
        "",
    ]
    if history:
        h = history[-1]
        lines += [f"### 現在 (Day {h['day']}): {ZONE_EMOJI[h['zone']]} {h['zone']} — 進捗 {h['progress']}% / バッファ消費 {h['buffer']}%", "",
                  "| Day | 日付 | 完了 / 総 pt | 進捗率 | バッファ消費率 | ゾーン |", "|---:|---|---:|---:|---:|:---:|"]
        for h in history:
            lines.append(f"| {h['day']} | {h['date']} | {h['done_points']} / {h['total_points']} | {h['progress']}% | {h['buffer']}% | {ZONE_EMOJI[h['zone']]} |")
    return "\n".join(lines)


# ------------------------------------------------------------------ commands
def load_all():
    cfg, backlog = load(CONFIG), load(BACKLOG)["items"]
    st = load(STATE) if STATE.exists() else None
    return cfg, backlog, st


def resolve_day(cfg: dict, st: dict, override) -> int:
    if override is not None:
        return override
    hol = set(cfg["plan"].get("holidays", []))
    return day_index(dt.date.fromisoformat(st["start_date"]), today_jst(), hol)


def require_state(st):
    if not st:
        sys.exit("[error] mock/state.json がありません。先に `python scripts/pm.py setup` を実行してください。")
    return st


def create_single_select(pid: str, name: str, options: list[tuple[str, str, str]]) -> None:
    opts = ", ".join(f"{{name: {q(n)}, color: {c}, description: {q(d)}}}" for n, c, d in options)
    gql(f"mutation {{ createProjectV2Field(input: {{projectId: {q(pid)}, dataType: SINGLE_SELECT, name: {q(name)}, "
        f"singleSelectOptions: [{opts}]}}) {{ projectV2Field {{ ... on ProjectV2SingleSelectField {{ id }} }} }} }}")


def create_iteration(pid: str, cfg: dict, start: dt.date) -> bool:
    its = []
    for i, s in enumerate(cfg["sprints"]):
        d = start + dt.timedelta(days=7 * i)
        its.append(f"{{title: {q(s['title'])}, startDate: {q(d.isoformat())}, duration: 7}}")
    res = gql(f"mutation {{ createProjectV2Field(input: {{projectId: {q(pid)}, dataType: ITERATION, name: {q(F_SPRINT)}, "
              f"iterationConfiguration: {{startDate: {q(start.isoformat())}, duration: 7, iterations: [{', '.join(its)}]}}}}) "
              f"{{ projectV2Field {{ ... on ProjectV2IterationField {{ id }} }} }} }}", check=False)
    return res is not None


def cmd_setup(a) -> None:
    cfg, backlog, st = load_all()
    if st and not a.force:
        sys.exit(f"[error] 既にセットアップ済みです ({st['project_url']})。作り直す場合は --force")
    repo = a.repo or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    owner = a.owner or repo.split("/")[0]
    hol = set(cfg["plan"].get("holidays", []))
    start = next_bday(dt.date.fromisoformat(a.start_date) if a.start_date else today_jst(), hol)

    print(f"repo={repo} owner={owner} start={start}")
    for name, lab in cfg["labels"].items():
        gh("label", "create", name, "--repo", repo, "--color", lab["color"], "--description", lab["description"], "--force")

    proj = gh("project", "create", "--owner", owner, "--title", cfg["project_title"], "--format", "json", json_out=True)
    print(f"project: {proj['url']}")
    if gh("project", "link", str(proj["number"]), "--owner", owner, "--repo", repo, check=False) is None:
        print("  [warn] リポジトリへのリンクに失敗しました (Project の設定から手動でリンクできます)")
    gh("project", "edit", str(proj["number"]), "--owner", owner, "--description", cfg["project_description"], check=False)

    pid = proj["id"]
    for name in (F_POINTS, F_FEVER_VALUE):
        gh("project", "field-create", str(proj["number"]), "--owner", owner, "--name", name, "--data-type", "NUMBER")
    create_single_select(pid, F_FEVER_METRIC, [("Progress %", "BLUE", "進捗率"), ("Buffer %", "ORANGE", "バッファ消費率")])
    create_single_select(pid, F_FEVER_ZONE, [("Green", "GREEN", "順調"), ("Yellow", "YELLOW", "注意・対策準備"),
                                             ("Red", "RED", "要対策")])
    if not create_iteration(pid, cfg, start):
        print("  [warn] Iteration フィールド 'Sprint' を API で作れませんでした。Project の Settings から手動で"
              " 'Sprint' (1週間, 開始日 {}) を作成し、`python scripts/pm.py tick` を再実行してください。".format(start))

    st = {"owner": owner, "repo": repo, "project_number": proj["number"], "project_id": pid,
          "project_url": proj["url"], "start_date": start.isoformat()}
    save(STATE, st)
    print(f"saved {STATE.relative_to(ROOT)}")
    day = max(resolve_day(cfg, st, None), 0)
    do_tick(cfg, backlog, st, day)
    do_fever(cfg, backlog, st, day)
    print(f"\n完了: {proj['url']}\nmock/state.json と docs/ をコミットし、README の手順で Actions を有効化してください。")


def do_tick(cfg, backlog, st, day: int) -> None:
    print(f"== tick: Day {day}")
    fields = get_fields(st["project_id"])
    items, issues = get_items(st["project_id"]), get_issues(st["repo"])
    for it in backlog:
        if it.get("added_day", 0) > day:
            continue
        cur = ensure_issue(st, it["key"], it["title"], it.get("body", ""), ["mock"] + it.get("labels", []), issues, items)
        vals = cur["values"]
        if vals.get(F_POINTS) != it["points"]:
            set_field(st, cur["item_id"], fields[F_POINTS], number=it["points"])
        if F_SPRINT in fields and vals.get(F_SPRINT) != it["sprint"] and it["sprint"] in fields[F_SPRINT].get("iterations", {}):
            set_field(st, cur["item_id"], fields[F_SPRINT], iteration=it["sprint"])
        want = sim_status(it, day)
        if vals.get(F_STATUS) != want:
            set_field(st, cur["item_id"], fields[F_STATUS], option=want)
            print(f"  #{cur['number']} {it['title']}: {vals.get(F_STATUS)} -> {want}")
        if want == "Done" and cur["state"] == "OPEN":
            gh("issue", "close", str(cur["number"]), "--repo", st["repo"], "--reason", "completed")
        elif want != "Done" and cur["state"] == "CLOSED":
            gh("issue", "reopen", str(cur["number"]), "--repo", st["repo"])
    for tr in TRACKERS:
        cur = ensure_issue(st, tr["key"], tr["title"], "", ["fever"], issues, items)
        if cur["values"].get(F_FEVER_METRIC) != tr["metric"]:
            set_field(st, cur["item_id"], fields[F_FEVER_METRIC], option=tr["metric"])


def do_fever(cfg, backlog, st, day: int) -> None:
    print(f"== fever: Day {day}")
    fields = get_fields(st["project_id"])
    items = get_items(st["project_id"])
    work = [v for k, v in items.items() if not k.startswith("FEVER-")]
    total = sum(v["values"].get(F_POINTS) or 0 for v in work)
    done = sum(v["values"].get(F_POINTS) or 0 for v in work if v["values"].get(F_STATUS) == "Done")
    p, b = fever_point(done, total, day, cfg["plan"])
    zone = fever_zone(p, b, cfg["fever_zones"])
    hol = set(cfg["plan"].get("holidays", []))
    row = {"day": day, "date": date_of_day(dt.date.fromisoformat(st["start_date"]), day, hol).isoformat(),
           "done_points": done, "total_points": total, "progress": round(p * 100, 1),
           "buffer": round(b * 100, 1), "zone": zone}
    history = [h for h in (load(HISTORY) if HISTORY.exists() else []) if h["day"] != day] + [row]
    history.sort(key=lambda h: h["day"])
    save(HISTORY, history)
    svg_fever_chart(FEVER_SVG, history, cfg["fever_zones"], f"フィーバーチャート — {cfg['project_title']}")
    print(f"  progress={row['progress']}% buffer={row['buffer']}% zone={zone}  ({done}/{total} pt)")

    body = tracker_body(st, cfg, history)
    for tr in TRACKERS:
        cur = items.get(tr["key"])
        if not cur:
            continue
        value = row["progress"] if tr["metric"] == "Progress %" else row["buffer"]
        if cur["values"].get(F_FEVER_VALUE) != value:
            set_field(st, cur["item_id"], fields[F_FEVER_VALUE], number=value)
        if cur["values"].get(F_FEVER_ZONE) != zone:
            set_field(st, cur["item_id"], fields[F_FEVER_ZONE], option=zone)
        gh("issue", "edit", str(cur["number"]), "--repo", st["repo"],
           "--body", f"{body}\n\n<!-- mock-key: {tr['key']} -->")


def cmd_tick(a) -> None:
    cfg, backlog, st = load_all()
    st = require_state(st)
    day = resolve_day(cfg, st, a.day)
    if day < 0:
        print(f"開始日 {st['start_date']} 前なので何もしません")
        return
    do_tick(cfg, backlog, st, day)


def cmd_fever(a) -> None:
    cfg, backlog, st = load_all()
    st = require_state(st)
    day = resolve_day(cfg, st, a.day)
    if day >= 0:
        do_fever(cfg, backlog, st, day)


def cmd_status(a) -> None:
    cfg, backlog, st = load_all()
    st = require_state(st)
    day = resolve_day(cfg, st, a.day)
    print(f"project: {st['project_url']}\nstart: {st['start_date']}  today: Day {day}")
    for k, v in sorted(get_items(st["project_id"]).items()):
        vals = v["values"]
        print(f"  {k:15} #{v['number']:<4} {str(vals.get(F_STATUS)):12} {vals.get(F_POINTS)!s:>4}pt  {vals.get(F_SPRINT, '')}")


def cmd_preview(a) -> None:
    cfg, backlog, _ = load_all()
    last = max(i["done_day"] for i in backlog)
    rows = simulate(cfg, backlog, last)
    labels = [f"D{r['day']}" for r in rows]
    total0 = rows[0]["total_points"]
    svg_line_chart(DOCS / "preview-burnup.svg", "バーンアップ (想定)", labels, [
        {"name": "総スコープ", "color": "#8250df", "values": [r["total_points"] for r in rows]},
        {"name": "完了", "color": "#1a7f37", "values": [r["done_points"] for r in rows]},
    ], "Story Points")
    series = [{"name": "残り (全体)", "color": "#0969da", "values": [r["remaining"] for r in rows]},
              {"name": "理想線", "color": "#8c959f", "dashed": True,
               "values": [round(total0 * (1 - d / last), 1) for d in range(last + 1)]}]
    colors = ["#bf3989", "#bc4c00"]
    for i, s in enumerate(cfg["sprints"]):
        series.append({"name": f"残り ({s['title']})", "color": colors[i % 2],
                       "values": [r["sprint_remaining"][s["title"]] if r["day"] >= s["start_day"] else None for r in rows]})
    svg_line_chart(DOCS / "preview-burndown.svg", "バーンダウン (想定)", labels, series, "残り Story Points")
    svg_fever_chart(DOCS / "preview-fever.svg", rows, cfg["fever_zones"], "フィーバーチャート (想定)")
    if not FEVER_SVG.exists():  # 実データの図は setup / fever で上書きされる。それまではゾーンだけの空チャート
        svg_fever_chart(FEVER_SVG, [], cfg["fever_zones"], "フィーバーチャート — まだデータがありません (setup 後に更新)")
    print("Day | done/total | remaining | progress | buffer | zone")
    for r in rows:
        print(f"{r['day']:>3} | {r['done_points']:>2}/{r['total_points']:<2}     | {r['remaining']:>9} | "
              f"{r['progress']:>7}% | {r['buffer']:>5}% | {r['zone']}")
    print("wrote docs/preview-burnup.svg, docs/preview-burndown.svg, docs/preview-fever.svg")


def cmd_token(a) -> None:
    token = os.environ.get(TOKEN_KEY)
    if not token:
        sys.exit(f"[error] {TOKEN_KEY} がありません。.env.example を .env にコピーしてトークンを書いてください。")
    # トークンの権限 (X-OAuth-Scopes ヘッダ) を確認。fine-grained トークンはこのヘッダを返さない
    env = {**os.environ, "GH_TOKEN": token}
    r = subprocess.run(["gh", "api", "-i", "user"], capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        sys.exit("[error] トークンで GitHub に認証できませんでした (期限切れ・削除済み・貼り間違いの可能性)")
    headers = r.stdout.split("\r\n\r\n" if "\r\n\r\n" in r.stdout else "\n\n", 1)[0]
    scopes_line = next((h for h in headers.splitlines() if h.lower().startswith("x-oauth-scopes:")), None)
    login = json.loads(r.stdout[len(headers):].strip() or "{}").get("login", "?")
    if scopes_line is None:
        print(f"user={login}: fine-grained トークンのため権限は自動確認できません")
    else:
        scopes = {s.strip() for s in scopes_line.split(":", 1)[1].split(",") if s.strip()}
        missing = REQUIRED_SCOPES - scopes
        print(f"user={login} scopes={', '.join(sorted(scopes)) or '(なし)'}")
        if missing:
            sys.exit(f"[error] 権限が足りません: {', '.join(sorted(missing))}")
        print("OK: repo / project の権限があります")
    if a.push:
        st = load(STATE) if STATE.exists() else {}
        repo = st.get("repo") or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        gh("secret", "set", TOKEN_KEY, "--repo", repo, stdin=token)  # 引数でなく標準入力で渡す
        print(f"Actions Secret {TOKEN_KEY} を {repo} に登録しました")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="Project / フィールド / Issue を作成")
    s.add_argument("--owner", help="Project のオーナー (ユーザー or Organization)。既定はリポジトリのオーナー")
    s.add_argument("--repo", help="owner/name。既定はカレントリポジトリ")
    s.add_argument("--start-date", help="Day 0 の日付 (YYYY-MM-DD)。既定は今日 (JST)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_setup)
    for name, fn, h in [("tick", cmd_tick, "シミュレーションを進める"), ("fever", cmd_fever, "フィーバー指標を更新"),
                        ("status", cmd_status, "状態を表示")]:
        p = sub.add_parser(name, help=h)
        p.add_argument("--day", type=int, help="Day を明示 (既定: 開始日からの営業日数)")
        p.set_defaults(fn=fn)
    sub.add_parser("preview", help="GitHub を使わず想定チャートを出力").set_defaults(fn=cmd_preview)
    t = sub.add_parser("token", help=f".env の {TOKEN_KEY} を検証")
    t.add_argument("--push", action="store_true", help=f"検証後、リポジトリの Actions Secret {TOKEN_KEY} に登録")
    t.set_defaults(fn=cmd_token)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    main()
