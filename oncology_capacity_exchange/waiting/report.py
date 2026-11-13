"""分析结果落盘：report.json、summary.md、chains.csv。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _interval(summary: dict) -> str:
    if summary.get("mean") is None:
        return "—"
    return f"{_fmt(summary['mean'])} [{_fmt(summary.get('p05'))}, {_fmt(summary.get('p95'))}]"


def render_summary_md(report: dict) -> str:
    lines: list[str] = []
    lines.append("# 候诊压力分析报告")
    lines.append("")
    lines.append("## 运行与复算")
    lines.append("")
    lines.append(f"- 分析起点 origin: {report['origin']}；观察截止 observed_until: {report['observed_until']}")
    lines.append(f"- 模拟窗口: {report['horizon_days']} 天（删失日 {report['horizon_end']}，截止日不等于完成日）")
    lines.append(f"- 随机种子: {report['seed']}；复制次数: {report['replications']}（相同种子可复算，三情景共享需求冲击）")
    lines.append(f"- 配置指纹: `{report['config_fingerprint'][:16]}…`")
    lines.append("")
    lines.append("## 队列重建（起点时刻，仅用起点前已获知的记录）")
    lines.append("")
    recon = report["reconstruction"]
    lines.append(f"- 仍在系统内的链: {recon['active_chains']}（按状态: {recon['by_status']}）")
    lines.append(f"- 起点前已完成: {len(recon['done_before_origin'])}；起点前未知: {len(recon['unknown_before_origin'])}")
    lines.append("")
    lines.append("## 历史镜像（observed_until 之前的全部已知记录）")
    lines.append("")
    hist = report["historical"]
    dist = hist["total_wait_days"]
    lines.append(f"- 链总数 {hist['chains_total']}：完成 {hist['completed']}，删失（保留在风险集）{hist['censored']}")
    lines.append(
        f"- 完成链总候诊天数: mean {_fmt(dist['mean'])}，p50 {_fmt(dist['p50'])}，"
        f"p90 {_fmt(dist['p90'])}，max {_fmt(dist['max'])}"
    )
    lines.append(f"- 超时（含已超时的删失链）: {hist['overtime']}（比例 {_fmt(hist['overtime_ratio'])}）")
    for step, step_dist in hist["step_wait_days"].items():
        lines.append(f"- 环节 {step}: 平均等待 {_fmt(step_dist['mean'])} 天（n={step_dist['n']}）")
    lines.append("")
    lines.append("## 情景结果（各次复制的均值 [p05, p95]）")
    lines.append("")
    lines.append("| 情景 | 平均候诊天数 | P90 候诊天数 | 超时比例 | 风险集（删失） |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, scenario in report["scenarios"].items():
        metrics = scenario["metrics"]
        lines.append(
            f"| {name} | {_interval(metrics['mean_total_wait'])} | {_interval(metrics['p90_total_wait'])}"
            f" | {_interval(metrics['overtime_ratio'])} | {_interval(metrics['censored'])} |"
        )
    lines.append("")
    lines.append("### 号源池占用")
    lines.append("")
    lines.append("| 情景 | 池 | 占用率 | 平均排队深度 | 可用号源 | 已用号源 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name, scenario in report["scenarios"].items():
        for pool_id, pool in scenario["pools"].items():
            lines.append(
                f"| {name} | {pool_id} | {_interval(pool['occupancy'])} | {_interval(pool['mean_queue_depth'])}"
                f" | {_fmt(pool['available_slots']['mean'], 1)} | {_fmt(pool['used_slots']['mean'], 1)} |"
            )
    lines.append("")
    lines.append("## 成对情景差异（同一复制配对，负值表示后者更优）")
    lines.append("")
    lines.append("| 对比 | 指标 | 差异均值 | 90% 区间 | 改善概率 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in report["pairwise"]:
        left, right = row["pair"]
        lines.append(
            f"| {left} → {right} | {row['metric']} | {_fmt(row['mean'])}"
            f" | [{_fmt(row['p05'])}, {_fmt(row['p95'])}] | {_fmt(row['prob_improvement'])} |"
        )
    lines.append("")
    lines.append("## 行为假设归因（容量作用 vs 取消/转院假设）")
    lines.append("")
    for entry in report["behavior_attribution"]:
        lines.append(f"### {entry['scenario']}")
        lines.append("")
        lines.append("| 指标 | 纯容量作用 | 转院假设作用 | 取消假设敏感度（本情景） | 取消假设敏感度（基线） |")
        lines.append("| --- | --- | --- | --- | --- |")
        for metric, parts in entry["metrics"].items():
            capacity = parts["capacity_effect"] or {}
            transfer = parts["transfer_effect"] or {}
            cancel_s = parts["cancel_sensitivity_scenario"] or {}
            cancel_b = parts["cancel_sensitivity_baseline"] or {}
            lines.append(
                f"| {metric} | {_fmt(capacity.get('mean'))} | {_fmt(transfer.get('mean'))}"
                f" | {_fmt(cancel_s.get('mean'))} | {_fmt(cancel_b.get('mean'))} |"
            )
        lines.append("")
        lines.append(f"> {entry['note']}")
        lines.append("")
    lines.append("## 外推边界（历史未覆盖的容量组合）")
    lines.append("")
    any_note = False
    for name, scenario in report["scenarios"].items():
        for note in scenario["extrapolation"]:
            any_note = True
            lines.append(f"- [{name}] {note['detail']}。{note['boundary']}")
    if not any_note:
        lines.append("- 无")
    lines.append("")
    lines.append("## 构成尾部的具体记录")
    lines.append("")
    lines.append("按各次复制中稳定超时（超时次数）排序；历史来源链的逐条事件记录见 report.json 的 tail[].historical_events。")
    lines.append("")
    lines.append("| 情景 | 链 | 来源 | 出现次数 | 超时次数 | 平均候诊天数 | 最大候诊天数 | 删失概率 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in report["tail"]:
        lines.append(
            f"| {row['scenario']} | {row['chain_id']} | {row['source']} | {row['appearances']} | {row['overtime_count']}"
            f" | {_fmt(row['mean_total_wait'])} | {_fmt(row['max_total_wait'])} | {_fmt(row['censored_probability'])} |"
        )
    lines.append("")
    lines.append("## 假设清单")
    lines.append("")
    for item in report["assumptions"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_outputs(report: dict, output_dir: str | Path) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    summary_path = out / "summary.md"
    summary_path.write_text(render_summary_md(report), encoding="utf-8")

    chains_path = out / "chains.csv"
    with chains_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "scenario",
                "chain_id",
                "source",
                "requested_at",
                "appearances",
                "mean_total_wait",
                "max_total_wait",
                "overtime_count",
                "censored_count",
                "overtime_probability",
                "censored_probability",
            ]
        )
        for row in report["chain_stats"]:
            writer.writerow(
                [
                    row["scenario"],
                    row["chain_id"],
                    row["source"],
                    row["requested_at"],
                    row["appearances"],
                    row["mean_total_wait"],
                    row["max_total_wait"],
                    row["overtime_count"],
                    row["censored_count"],
                    row["overtime_probability"],
                    row["censored_probability"],
                ]
            )
    return {
        "report": str(report_path),
        "summary": str(summary_path),
        "chains": str(chains_path),
    }
