"""Generate the rerun checklist from live leaderboard state.

Usage::

    PYTHONPATH=<repo-root> python rerun_checklist.py

Writes RERUN_CHECKLIST.md. Read-only w.r.t. the leaderboard.

Why each slot may need a rerun:
- MISSING: no slot recorded for that (model, condition, task).
- ABORTED / NO_RESULT / NO_SESSION_END: the run died (upstream outage or a
  killed harness); not a model score.
- NETWORK: telemetry.network_retry_count >= RETRY_WARN.
- HANG: short/long slot with almost no completion tokens.
- BRIEF_STALE: workspace/TASK.md differs from the current brief -> the task
  got a DIFFERENT (usually easier) spec, so its score is not comparable.
  This one CANNOT be fixed offline; the model must be re-run.
- PLAN_STALE: B-condition PLAN.md lacks the S1-S5 structure.
- SCORER_STALE: grader code changed after scoring -> fixable offline via
  rescore_fast.py / rescore_long.py (0 tokens).
"""
import json
import os
import pathlib
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "D:/vscode/kimisubagentexplore/subagentbenchmark")
from benchmark_v3.bench_harness.core.report import MasterLeaderboard as M

M._bind_catalog()
lb = json.loads(pathlib.Path("bench_runs/leaderboard.json").read_text(encoding="utf-8"))

A_TASKS = list(M.CANONICAL_TASKS)
B_TASKS = list(M.CANONICAL_B_TASKS)
SUITE_OF = dict(M.TASK_SUITES)
B_SUITE = {"short": "short_b", "long": "long_b"}
TINY_SUITES = {"short", "long"}

NO_CHANNEL = {"deepseek-flash@max", "muse-spark-1.3-contributor@xhigh"}
RETRY_WARN = 10
TINY_COMPLETION = 1000

SUITE_FILES = {
    "short": pathlib.Path("bench_harness/suites/short_task.py"),
    "reviewer": pathlib.Path("bench_harness/suites/reviewer.py"),
    "critic": pathlib.Path("bench_harness/suites/critic.py"),
    "long": pathlib.Path("bench_harness/suites/long_task.py"),
}


def slot_dir(run_dir, task, cond):
    fam = SUITE_OF.get(task, "")
    suite = B_SUITE.get(fam, fam) if cond == "b" else fam
    return pathlib.Path(str(run_dir)) / suite / task


def current_brief(task, fam):
    try:
        if fam == "short":
            from benchmark_v3.bench_harness.suites.short_task import TASK_BRIEFS
            return TASK_BRIEFS[task]["brief"]
        if fam == "reviewer":
            from benchmark_v3.bench_harness.suites.reviewer import REVIEWER_TASKS
            return REVIEWER_TASKS[task]["brief"]
        if fam == "long":
            from benchmark_v3.bench_harness.suites.long_task import LONG_BRIEFS
            return LONG_BRIEFS[task][1]
    except Exception:
        return None
    return None


def norm(s):
    return " ".join(s.split())


def brief_stale(run_dir, task, cond):
    fam = SUITE_OF.get(task, "")
    p = slot_dir(run_dir, task, cond) / "workspace" / "TASK.md"
    if not p.is_file():
        return False
    cur = current_brief(task, fam)
    if not cur:
        return False
    return norm(cur) not in norm(p.read_text(encoding="utf-8", errors="replace"))


def plan_stale(run_dir, task, cond):
    if cond != "b":
        return False
    try:
        from benchmark_v3.bench_harness.suites.b_plans import B_PLANS
        want = B_PLANS.get(task)
    except Exception:
        return False
    if not want:
        return False
    p = slot_dir(run_dir, task, cond) / "workspace" / "PLAN.md"
    if not p.is_file():
        return True
    return norm(want) not in norm(p.read_text(encoding="utf-8", errors="replace"))


def scorer_stale(run_dir, task, cond, slot):
    fam = SUITE_OF.get(task, "")
    f = SUITE_FILES.get(fam)
    if not f or not f.is_file():
        return False
    ev = slot_dir(run_dir, task, cond) / "evaluation.json"
    if not ev.is_file():
        return False
    newest = ev.stat().st_mtime
    up = str(slot.get("updated_at") or "")
    if up:
        try:
            newest = max(newest, datetime.fromisoformat(up.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return newest < f.stat().st_mtime


def has_session_end(d):
    w = d / "wire.jsonl"
    if not w.is_file():
        return False
    return any('"session_end"' in ln for ln in w.read_text(encoding="utf-8", errors="replace").splitlines())


def diagnose(run_dir, task, cond, slot):
    d = slot_dir(run_dir, task, cond)
    if (d / "ABORTED.json").is_file():
        return "ABORTED"
    ev = d / "evaluation.json"
    if not ev.is_file():
        return "NO_RESULT"
    try:
        e = json.loads(ev.read_text(encoding="utf-8"))
    except Exception:
        return "BAD_RESULT"
    tel = e.get("telemetry") or {}
    tm = e.get("token_metrics") or {}
    if not has_session_end(d):
        return "NO_SESSION_END"
    if (tel.get("network_retry_count") or 0) >= RETRY_WARN:
        return "NETWORK"
    fam = SUITE_OF.get(task, "")
    if fam in TINY_SUITES and (tm.get("completion_tokens") or 0) < TINY_COMPLETION:
        return "HANG"
    if brief_stale(run_dir, task, cond):
        return "BRIEF_STALE"
    if plan_stale(run_dir, task, cond):
        return "PLAN_STALE"
    if scorer_stale(run_dir, task, cond, slot):
        return "SCORER_STALE"
    return None


MUST_RERUN = {"MISSING", "ABORTED", "NO_RESULT", "BAD_RESULT", "NO_SESSION_END",
              "NETWORK", "HANG"}
# 题面/计划过期：当前 brief 比存档更详细（旧题面更模糊 -> 更难），所以这些
# 分数是保守可信的（不会虚高），只是横向可比性稍弱。列为「可选」而非必须，
# 避免为不影响结论的槽位烧 token。
OPTIONAL_RERUN = {"BRIEF_STALE", "PLAN_STALE"}
OFFLINE = {"SCORER_STALE"}

rows = []
for model, e in sorted(lb.items()):
    if model in NO_CHANNEL:
        continue
    for cond, tlist in (("a", A_TASKS), ("b", B_TASKS)):
        for t in tlist:
            key = t if cond == "a" else f"{t}@b"
            slot = (e.get("tasks") or {}).get(key)
            if slot is None:
                rows.append((model, cond.upper(), t, "-", "MISSING"))
                continue
            why = diagnose(slot.get("run_dir"), t, cond, slot)
            if why:
                rows.append((model, cond.upper(), t, str(slot.get("reward")), why))

n_must = sum(1 for r in rows if r[4] in MUST_RERUN)
n_opt = sum(1 for r in rows if r[4] in OPTIONAL_RERUN)
n_off = sum(1 for r in rows if r[4] in OFFLINE)
lines = ["# Benchmark v3 重跑清单", "",
         f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
         f"> 需处理槽位：**{len(rows)}** —— 必须重跑 **{n_must}**"
         f" / 可选重跑 **{n_opt}** / 可离线复算 **{n_off}**", "",
         "> **必须**：该槽位要么完全没测，要么那次运行死了（上游中断 / 进程被杀 / 挂起），"
         "记录里的分不是模型能力的证据。",
         "> **可选**：题面或 B 计划是旧版。当前 brief 比旧版更详细，"
         "所以旧题面更模糊、更难；模型在旧题面下拿的分是保守可信的（不会虚高），"
         "只是与其他槽位横向可比性稍弱。不影响结论时可以不跑。", ""]

lines += ["## 一、必须重跑（没测 / 跑死了）", "",
          "| 模型 | 条件 | 任务 | 当前分 | 原因 |", "|------|------|------|--------|------|"]
for m, c, t, r, why in rows:
    if why in MUST_RERUN:
        lines.append(f"| `{m}` | {c} | `{t}` | {r} | {why} |")
lines.append("")

lines += ["## 二、可选重跑（题面/计划过期，分数保守可信）", "",
          "| 模型 | 条件 | 任务 | 当前分 | 原因 |", "|------|------|------|--------|------|"]
for m, c, t, r, why in rows:
    if why in OPTIONAL_RERUN:
        lines.append(f"| `{m}` | {c} | `{t}` | {r} | {why} |")
lines.append("")

lines += ["## 三、可离线复算（0 Token）", "",
          "| 模型 | 条件 | 任务 | 当前分 | 原因 |", "|------|------|------|--------|------|"]
for m, c, t, r, why in rows:
    if why in OFFLINE:
        lines.append(f"| `{m}` | {c} | `{t}` | {r} | {why} |")
lines.append("")

lines += ["## 按原因统计", ""]
cnt = defaultdict(int)
for m, c, t, r, why in rows:
    cnt[why] += 1
lines += ["| 原因 | 数量 | 处置 |", "|------|------|------|"]
for why, n in sorted(cnt.items(), key=lambda x: -x[1]):
    act = ("必须重跑" if why in MUST_RERUN
           else "可选重跑" if why in OPTIONAL_RERUN else "rescore_*.py")
    lines.append(f"| {why} | {n} | {act} |")
lines.append("")

pathlib.Path("RERUN_CHECKLIST.md").write_text("\n".join(lines), encoding="utf-8")
print(f"written RERUN_CHECKLIST.md  必须={n_must} 可选={n_opt} 离线={n_off}")
for m, c, t, r, why in rows:
    tag = ("MUST " if why in MUST_RERUN
           else "OPT  " if why in OPTIONAL_RERUN else "OFFL ")
    print(f"  {tag} {m:32s} [{c}] {t:22s} r={r:7s} {why}")
