"""情景比较：指标聚合、成对差异、行为归因与外推边界。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .history import Calibration, Reconstruction, calibrate_behavior, historical_summary, reconstruct_queues
from .model import Scenario, WaitingFixture
from .simulation import SimResult, build_variants, generate_arrivals, simulate

METRIC_KEYS = (
    "chains_total",
    "completed",
    "censored",
    "at_risk",
    "overtime",
    "overtime_ratio",
    "mean_total_wait",
    "p50_total_wait",
    "p90_total_wait",
    "max_total_wait",
    "cancellations",
)

PAIRWISE_METRICS = ("mean_total_wait", "p90_total_wait", "overtime_ratio")


@dataclass(frozen=True)
class ReplicationMetrics:
    scalars: dict[str, float | None]
    pool_scalars: dict[str, dict[str, float | None]]


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * q
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return float(sorted_values[low] * (1 - frac) + sorted_values[high] * frac)


def _summarize(values: list[float | None]) -> dict[str, float | None]:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return {"n": 0, "mean": None, "sd": None, "p05": None, "p50": None, "p95": None}
    mean = sum(clean) / len(clean)
    if len(clean) > 1:
        variance = sum((value - mean) ** 2 for value in clean) / (len(clean) - 1)
    else:
        variance = 0.0
    return {
        "n": len(clean),
        "mean": mean,
        "sd": variance**0.5,
        "p05": _percentile(clean, 0.05),
        "p50": _percentile(clean, 0.5),
        "p95": _percentile(clean, 0.95),
    }


def result_metrics(result: SimResult) -> ReplicationMetrics:
    """单次复制的标量指标。未完成的链计入风险集与删失，不计入完成。"""
    records = result.records
    total = len(records)
    completed = [record for record in records if record["completed"]]
    censored = [record for record in records if record["censored"]]
    overtime = [record for record in records if record["overtime"]]
    waits = [record["total_wait_days"] for record in completed]
    waits_sorted = sorted(float(value) for value in waits)
    step_waits: dict[str, list[float]] = {}
    for record in completed:
        for step, value in record["step_waits"].items():
            step_waits.setdefault(step, []).append(float(value))

    scalars: dict[str, float | None] = {
        "chains_total": float(total),
        "completed": float(len(completed)),
        "censored": float(len(censored)),
        "at_risk": float(sum(1 for record in censored if record["at_risk"])),
        "overtime": float(len(overtime)),
        "overtime_ratio": (len(overtime) / total) if total else None,
        "mean_total_wait": (sum(waits) / len(waits)) if waits else None,
        "p50_total_wait": _percentile(waits_sorted, 0.5),
        "p90_total_wait": _percentile(waits_sorted, 0.9),
        "max_total_wait": float(waits_sorted[-1]) if waits_sorted else None,
        "cancellations": float(sum(record["cancellations"] for record in records)),
    }
    for step, values in sorted(step_waits.items()):
        scalars[f"mean_step_wait.{step}"] = sum(values) / len(values)

    pool_scalars: dict[str, dict[str, float | None]] = {}
    for pool_id, days in result.pool_days.items():
        available = sum(day.available for day in days)
        used = sum(day.used for day in days)
        pool_scalars[pool_id] = {
            "available_slots": float(available),
            "used_slots": float(used),
            "lost_slots": float(sum(day.lost for day in days)),
            "cancellations": float(sum(day.cancellations for day in days)),
            "occupancy": (used / available) if available else None,
            "mean_queue_depth": (sum(day.queue_depth for day in days) / len(days)) if days else None,
        }
    return ReplicationMetrics(scalars=scalars, pool_scalars=pool_scalars)


def _paired_diff(base: list[float | None], other: list[float | None]) -> dict[str, Any]:
    diffs = [
        other_value - base_value
        for base_value, other_value in zip(base, other)
        if base_value is not None and other_value is not None
    ]
    if not diffs:
        return {"n": 0, "mean": None, "sd": None, "p05": None, "p95": None, "prob_improvement": None}
    mean = sum(diffs) / len(diffs)
    variance = sum((value - mean) ** 2 for value in diffs) / (len(diffs) - 1) if len(diffs) > 1 else 0.0
    ordered = sorted(diffs)
    return {
        "n": len(diffs),
        "mean": mean,
        "sd": variance**0.5,
        "p05": _percentile(ordered, 0.05),
        "p95": _percentile(ordered, 0.95),
        "prob_improvement": sum(1 for value in diffs if value < 0) / len(diffs),
    }


def _extrapolation_notes(fixture: WaitingFixture, scenario: Scenario, calibration: Calibration) -> list[dict]:
    """历史从未覆盖的容量组合：明确外推边界，不声称已证实改善。"""
    notes: list[dict] = []
    pool_map = fixture.pool_map()
    boundary = "历史数据未覆盖该容量组合；对应改善只是模型在既定假设下的投影，未被历史证实。"
    for change in scenario.changes:
        pool = pool_map[change.pool_id]
        for weekday in sorted(change.weekdays):
            base_units = pool.units if weekday in pool.weekdays else 0
            if weekday not in pool.weekdays:
                notes.append(
                    {
                        "scenario": scenario.name,
                        "pool_id": pool.pool_id,
                        "dimension": "weekday",
                        "detail": f"{pool.pool_id} 历史上工作日 {weekday} 不开放，情景在该日新增 {change.extra_units} 个单元",
                        "boundary": boundary,
                    }
                )
            if base_units + change.extra_units > pool.units:
                notes.append(
                    {
                        "scenario": scenario.name,
                        "pool_id": pool.pool_id,
                        "dimension": "units",
                        "detail": (
                            f"{pool.pool_id} 工作日 {weekday} 单元数 {base_units} → "
                            f"{base_units + change.extra_units}，历史峰值为 {pool.units}"
                        ),
                        "boundary": boundary,
                    }
                )
    if scenario.allow_transfer:
        observed_pairs = {
            (event.step, event.pool_id)
            for event in fixture.events
            if event.kind == "completed" and event.known_at <= fixture.origin and event.pool_id
        }
        for service, home in sorted(calibration.home_pool.items()):
            for pool in fixture.pools:
                if service in pool.services and pool.pool_id != home and (service, pool.pool_id) not in observed_pairs:
                    notes.append(
                        {
                            "scenario": scenario.name,
                            "pool_id": pool.pool_id,
                            "dimension": "service-pool",
                            "detail": f"转院假设下 {service} 可由 {pool.pool_id} 服务，但历史完成记录中从未出现该组合",
                            "boundary": boundary,
                        }
                    )
    return notes


def _chain_stats(scenario_name: str, results: list[SimResult]) -> list[dict]:
    """按链聚合各次复制（全部链），供 chains.csv 与尾部挑选使用。"""
    per_chain: dict[str, list[dict]] = {}
    for result in results:
        for record in result.records:
            per_chain.setdefault(record["chain_id"], []).append(record)
    rows = []
    for chain_id, records in per_chain.items():
        appearances = len(records)
        waits = [record["total_wait_days"] for record in records]
        overtime_count = sum(1 for record in records if record["overtime"])
        censored_count = sum(1 for record in records if record["censored"])
        rows.append(
            {
                "scenario": scenario_name,
                "chain_id": chain_id,
                "source": records[0]["source"],
                "requested_at": records[0]["requested_at"],
                "deadline_days": records[0]["deadline_days"],
                "appearances": appearances,
                "mean_total_wait": sum(waits) / appearances,
                "max_total_wait": max(waits),
                "overtime_count": overtime_count,
                "censored_count": censored_count,
                "overtime_probability": overtime_count / appearances,
                "censored_probability": censored_count / appearances,
            }
        )
    # 排序：优先展示在多次复制中稳定构成尾部的链（超时次数），其次平均等待。
    rows.sort(key=lambda row: (-row["overtime_count"], -row["mean_total_wait"], row["chain_id"]))
    return rows


def _tail_records(fixture: WaitingFixture, stats_rows: list[dict], top: int) -> list[dict]:
    """构成尾部的具体记录：超时概率最高、等待最长的链，可回溯到历史事件。"""
    events_by_chain: dict[str, list[dict]] = {}
    for event in fixture.events:
        events_by_chain.setdefault(event.chain_id, []).append(
            {
                "event_id": event.event_id,
                "kind": event.kind,
                "step": event.step,
                "pool_id": event.pool_id,
                "at": event.at.isoformat(),
                "known_at": event.known_at.isoformat(),
            }
        )
    tail = []
    for row in stats_rows[:top]:
        entry = dict(row)
        if row["source"] == "history":
            entry["historical_events"] = sorted(
                events_by_chain.get(row["chain_id"], []), key=lambda item: (item["at"], item["event_id"])
            )
        tail.append(entry)
    return tail


def analyze(
    fixture: WaitingFixture,
    seed: int | None = None,
    replications: int = 40,
    top: int = 10,
) -> dict:
    """完整分析：重建 → 校准 → 共享需求冲击的多情景模拟 → 比较与归因。"""
    if replications <= 0:
        raise ValueError("replications 必须为正")
    seed = fixture.seed if seed is None else seed
    calibration = calibrate_behavior(fixture)
    reconstruction = reconstruct_queues(fixture, calibration)
    variants = build_variants(fixture)
    variant_keys = [variant.key for variant in variants]

    metrics_by_variant: dict[str, list[ReplicationMetrics]] = {key: [] for key in variant_keys}
    results_by_variant: dict[str, list[SimResult]] = {key: [] for key in variant_keys}
    for replication in range(replications):
        arrivals = generate_arrivals(seed, replication, calibration, fixture.origin, fixture.horizon_days)
        for variant in variants:
            result = simulate(fixture, calibration, reconstruction, variant, arrivals, seed, replication)
            metrics_by_variant[variant.key].append(result_metrics(result))
            results_by_variant[variant.key].append(result)

    def series(variant_key: str, metric: str) -> list[float | None]:
        return [metrics.scalars.get(metric) for metrics in metrics_by_variant[variant_key]]

    scenarios_out: dict[str, Any] = {}
    base_key = {scenario.name: f"{scenario.name}|base" for scenario in fixture.scenarios}
    for scenario in fixture.scenarios:
        key = base_key[scenario.name]
        metrics = {metric: _summarize(series(key, metric)) for metric in METRIC_KEYS}
        step_metrics = sorted(
            {name for metrics_rep in metrics_by_variant[key] for name in metrics_rep.scalars if name.startswith("mean_step_wait.")}
        )
        for metric in step_metrics:
            metrics[metric] = _summarize(series(key, metric))
        pools_out = {}
        for pool in fixture.pools:
            pool_metrics = metrics_by_variant[key][0].pool_scalars.get(pool.pool_id, {})
            pools_out[pool.pool_id] = {
                name: _summarize([rep.pool_scalars[pool.pool_id][name] for rep in metrics_by_variant[key]])
                for name in pool_metrics
            }
        scenarios_out[scenario.name] = {
            "behavior": {
                "cancellations": True,
                "allow_transfer": scenario.allow_transfer,
            },
            "metrics": metrics,
            "pools": pools_out,
            "extrapolation": _extrapolation_notes(fixture, scenario, calibration),
        }

    # 成对差异：同一复制索引配对（公共随机数），负值表示改善。
    pairwise = []
    names = [scenario.name for scenario in fixture.scenarios]
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            for metric in PAIRWISE_METRICS:
                pairwise.append(
                    {
                        "pair": [left, right],
                        "metric": metric,
                        "direction": f"{right} - {left}（负值表示 {right} 更优）",
                        **_paired_diff(series(base_key[left], metric), series(base_key[right], metric)),
                    }
                )

    # 行为归因：容量作用 vs 取消/转院行为假设。
    baseline = fixture.scenarios[0].name
    attribution = []
    for scenario in fixture.scenarios[1:]:
        entry: dict[str, Any] = {"scenario": scenario.name, "metrics": {}}
        for metric in PAIRWISE_METRICS:
            # capacity-only 变体与 base 相同（情景未开启转院）时会被去重，回退到 base。
            capacity_only_key = f"{scenario.name}|capacity-only"
            if capacity_only_key not in metrics_by_variant:
                capacity_only_key = base_key[scenario.name]
            no_cancel_key = f"{scenario.name}|no-cancel"
            baseline_no_cancel = f"{baseline}|no-cancel"
            capacity_effect = _paired_diff(series(base_key[baseline], metric), series(capacity_only_key, metric))
            if scenario.allow_transfer:
                transfer_effect = _paired_diff(series(capacity_only_key, metric), series(base_key[scenario.name], metric))
            else:
                transfer_effect = None
            entry["metrics"][metric] = {
                "capacity_effect": capacity_effect,
                "transfer_effect": transfer_effect if scenario.allow_transfer else None,
                "cancel_sensitivity_scenario": _paired_diff(
                    series(base_key[scenario.name], metric), series(no_cancel_key, metric)
                ),
                "cancel_sensitivity_baseline": _paired_diff(
                    series(base_key[baseline], metric), series(baseline_no_cancel, metric)
                ),
            }
        entry["note"] = (
            "capacity_effect 为纯容量作用（基线行为假设下）；transfer_effect 为转院假设的额外作用；"
            "cancel_sensitivity 为关闭取消假设后指标的变化，用于衡量结论对取消行为假设的依赖。"
        )
        attribution.append(entry)

    tail = []
    chain_stats = []
    for scenario in fixture.scenarios:
        stats_rows = _chain_stats(scenario.name, results_by_variant[base_key[scenario.name]])
        chain_stats.extend(stats_rows)
        tail.extend(_tail_records(fixture, stats_rows, top))

    assumptions = [
        "日粒度离散事件模拟；容量 = 单元数 × (开放分钟 // 单次服务分钟)，按工作日展开。",
        "默认按历史归属池路由；allow_transfer 情景下，兼容院区的其他池也可服务（历史归属池始终可用）。",
        "同一池被多个服务共享时按 (请求日期, chain_id) 先进先出竞争。",
        "取消按单次占位概率发生（起点前已知记录估计），释放的号源当天可再分配；取消的链按经验重约间隔重新排队，链身份不变。",
        "新需求为按工作日的 Poisson 到达（起点前首约速率），模板从起点前已知的链抽样；三种情景共享同一组需求冲击（公共随机数）。",
        f"环节间交接至少 {calibration.min_step_gap_days} 天；历史交接间隔主要反映排队，不作为硬约束。",
        "分析起点之后才获知的记录不作为起点时的分配依据；未完成的链保留在风险集中，按观察截止删失，观察截止日不等于完成日。",
        "历史完成记录包含配置日历之外的日期，容量以配置日历为准，历史仅用于需求与行为校准。",
        "超出历史覆盖的容量组合（新工作日、超出历史单元数、未见过的服务-池组合）属于外推，改善幅度为模型投影而非已证实结论。",
    ]

    report = {
        "schema_version": 1,
        "seed": seed,
        "replications": replications,
        "origin": fixture.origin.isoformat(),
        "observed_until": fixture.observed_until.isoformat(),
        "horizon_days": fixture.horizon_days,
        "horizon_end": fixture.horizon_end.isoformat(),
        "config_fingerprint": hashlib.sha256(
            json.dumps(fixture.raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "baseline": baseline,
        "calibration": {
            "arrival_weekday_rates": list(calibration.arrival_weekday_rates),
            "cancel_probability": dict(calibration.cancel_probability),
            "rebook_delays": list(calibration.rebook_delays),
            "min_step_gap_days": calibration.min_step_gap_days,
            "home_pool": dict(calibration.home_pool),
            "templates_from_chains": len(calibration.templates),
            "notes": list(calibration.notes),
        },
        "reconstruction": {
            "at": reconstruction.origin.isoformat(),
            "active_chains": len(reconstruction.active),
            "by_status": reconstruction.by_status(),
            "done_before_origin": list(reconstruction.done),
            "unknown_before_origin": list(reconstruction.unknown),
            "chains": [
                {
                    "chain_id": state.chain_id,
                    "status": state.status,
                    "step_index": state.step_index,
                    "request_date": state.request_date.isoformat(),
                    "held_pool": state.held_pool,
                    "rejoin_date": state.rejoin_date.isoformat() if state.rejoin_date else None,
                }
                for state in reconstruction.active
            ],
        },
        "historical": historical_summary(fixture),
        "scenarios": scenarios_out,
        "pairwise": pairwise,
        "behavior_attribution": attribution,
        "tail": tail,
        "chain_stats": chain_stats,
        "assumptions": assumptions,
    }
    return _round(report)


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value
