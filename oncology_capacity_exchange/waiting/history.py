"""历史回放：等待队列重建、行为校准与历史镜像。

信息纪律：分析起点（origin）之后才被系统获知的记录，不能作为起点当时的
分配依据。因此重建与校准只使用 known_at <= origin 的事件；observed_until
之前的全部记录仅用于"历史镜像"统计。尚未完成的链保留在风险集中，
观察截止日不等于完成日（按删失处理）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import median
from typing import Iterable

from .model import Chain, Event, WaitingFixture

# 环节间交接的最小间隔（天）。历史最小交接间隔为 6 天，但其中主要是排队等待，
# 不作为硬约束；仅假设交接至少 1 天（历史从未出现当天交接）。
DEFAULT_MIN_STEP_GAP_DAYS = 1
# 历史上没有可估计的重约间隔时的缺省值。
DEFAULT_REBOOK_DELAYS = (1,)


@dataclass(frozen=True)
class ChainState:
    """分析起点时一条链的重建状态。"""

    chain_id: str
    step_index: int
    status: str  # waiting | held | rebook
    request_date: dt.date  # 当前未完成请求的日期（取消后重约会更新）
    held_pool: str | None = None
    rejoin_date: dt.date | None = None  # rebook 状态下重新排队的日期


@dataclass(frozen=True)
class Reconstruction:
    origin: dt.date
    active: tuple[ChainState, ...]  # 起点时仍在系统内的链（等待/占位/待重约）
    done: tuple[str, ...]  # 起点前已完成全部环节的链
    unknown: tuple[str, ...]  # 起点时尚无任何已知记录的链

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state in self.active:
            counts[state.status] = counts.get(state.status, 0) + 1
        return counts


@dataclass(frozen=True)
class ChainTemplate:
    steps: tuple[str, ...]
    compatible_sites: tuple[str, ...]
    deadline_days: int
    priority_group: str


@dataclass(frozen=True)
class Calibration:
    """从起点前已知记录估计的需求与行为参数。"""

    arrival_weekday_rates: tuple[float, ...]  # 每个工作日的新链到达率（Poisson）
    cancel_probability: dict[str, float]  # 每个服务的单次占位取消概率
    rebook_delays: tuple[int, ...]  # 取消后重约间隔的经验分布（天）
    templates: tuple[ChainTemplate, ...]  # 新链抽样模板（来自起点前已知的链）
    home_pool: dict[str, str]  # 每个服务的历史归属池（完成记录最多的池）
    min_step_gap_days: int
    notes: tuple[str, ...] = ()


def _events_known_at(events: Iterable[Event], cutoff: dt.date) -> list[Event]:
    known = [event for event in events if event.known_at <= cutoff]
    known.sort(key=lambda event: (event.at, event.event_id))
    return known


def _group_by_chain(events: Iterable[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.chain_id, []).append(event)
    return grouped


def _replay(chain: Chain, events: list[Event]) -> tuple[int, str, dt.date | None, str | None, dt.date | None]:
    """按 (at, event_id) 顺序回放一条链，返回 (环节序号, 状态, 请求日, 占位池, 取消日)。

    只接受与当前环节匹配的事件；与当前环节不符的记录被忽略（防御性处理）。
    """
    steps = chain.steps
    step_index = 0
    status = "waiting"
    request_date: dt.date | None = None
    held_pool: str | None = None
    cancel_date: dt.date | None = None
    for event in events:
        if step_index >= len(steps) or event.step != steps[step_index]:
            continue
        if event.kind == "requested":
            status, request_date, held_pool = "waiting", event.at, None
        elif event.kind == "held":
            status, held_pool = "held", event.pool_id
        elif event.kind == "cancelled":
            status, held_pool, cancel_date = "rebook", None, event.at
        elif event.kind == "completed":
            step_index += 1
            held_pool = None
            if step_index >= len(steps):
                status = "done"
            else:
                status, request_date = "waiting", event.at
    return step_index, status, request_date, held_pool, cancel_date


def calibrate_behavior(fixture: WaitingFixture) -> Calibration:
    """仅用 known_at <= origin 的记录估计需求与行为参数。"""
    origin = fixture.origin
    demand = fixture.demand
    notes: list[str] = []
    known = _events_known_at(fixture.events, origin)
    by_chain = _group_by_chain(known)

    # 取消后重约间隔：同一链同一环节先取消、之后又请求的间隔。
    rebook_delays: list[int] = []
    for chain in fixture.chains:
        events = by_chain.get(chain.chain_id, [])
        cancels = [event.at for event in events if event.kind == "cancelled"]
        requests = [event.at for event in events if event.kind == "requested"]
        for cancel_at in cancels:
            later = [req for req in requests if req > cancel_at]
            if later:
                rebook_delays.append((min(later) - cancel_at).days)
    if not rebook_delays:
        rebook_delays = list(DEFAULT_REBOOK_DELAYS)
        notes.append("历史上没有可观察的取消后重约，重约间隔取缺省 1 天")

    # 单次占位取消概率：按服务统计 取消数 / 占位数。
    cancel_probability: dict[str, float] = {}
    held_counts: dict[str, int] = {}
    cancel_counts: dict[str, int] = {}
    for event in known:
        if event.kind == "held":
            held_counts[event.step] = held_counts.get(event.step, 0) + 1
        elif event.kind == "cancelled":
            cancel_counts[event.step] = cancel_counts.get(event.step, 0) + 1
    for service in sorted(set(held_counts) | set(cancel_counts)):
        held = held_counts.get(service, 0)
        cancel_probability[service] = (cancel_counts.get(service, 0) / held) if held else 0.0

    # 新链到达率：首条 requested 记录按工作日统计（重约不计入新需求）。
    first_requests: list[Event] = []
    for chain in fixture.chains:
        events = [event for event in by_chain.get(chain.chain_id, []) if event.kind == "requested"]
        if events:
            first_requests.append(min(events, key=lambda event: (event.at, event.event_id)))
    rates = [0.0] * 7
    if first_requests:
        start = min(event.at for event in first_requests)
        days = (origin - start).days + 1
        counts = [0] * 7
        for event in first_requests:
            counts[event.at.weekday()] += 1
        for weekday in range(7):
            occurrences = sum(
                1 for offset in range(days) if (start + dt.timedelta(days=offset)).weekday() == weekday
            )
            rates[weekday] = counts[weekday] / occurrences if occurrences else 0.0

    # 历史归属池：完成记录最多的池（并列时取配置中靠前的池）。
    completion_counts: dict[str, dict[str, int]] = {}
    for event in known:
        if event.kind == "completed" and event.pool_id:
            completion_counts.setdefault(event.step, {})
            completion_counts[event.step][event.pool_id] = completion_counts[event.step].get(event.pool_id, 0) + 1
    home_pool: dict[str, str] = {}
    pool_order = [pool.pool_id for pool in fixture.pools]
    for chain in fixture.chains:
        for service in chain.steps:
            if service in home_pool:
                continue
            counts = completion_counts.get(service, {})
            if counts:
                home_pool[service] = max(pool_order, key=lambda pid: (counts.get(pid, 0), -pool_order.index(pid)))
            else:
                candidates = [pool.pool_id for pool in fixture.pools if service in pool.services]
                home_pool[service] = candidates[0]
                notes.append(f"服务 {service} 没有历史完成记录，归属池取配置中第一个提供该服务的池")

    # 新链模板：起点前已有任何已知记录的链。
    templates = tuple(
        ChainTemplate(chain.steps, chain.compatible_sites, chain.deadline_days, chain.priority_group)
        for chain in fixture.chains
        if by_chain.get(chain.chain_id)
    )

    # 覆盖项（demand 段）优先于历史校准。
    if demand.arrival_weekday_rates is not None:
        rates = list(demand.arrival_weekday_rates)
        notes.append("需求到达率使用 demand 覆盖值，未从历史校准")
    if demand.cancel_probability is not None:
        cancel_probability = dict(demand.cancel_probability)
        notes.append("取消概率使用 demand 覆盖值，未从历史校准")
    if demand.rebook_delays is not None:
        rebook_delays = list(demand.rebook_delays)
        notes.append("重约间隔使用 demand 覆盖值，未从历史校准")
    gap = DEFAULT_MIN_STEP_GAP_DAYS if demand.min_step_gap_days is None else demand.min_step_gap_days

    return Calibration(
        arrival_weekday_rates=tuple(rates),
        cancel_probability=cancel_probability,
        rebook_delays=tuple(sorted(set(rebook_delays))),
        templates=templates,
        home_pool=home_pool,
        min_step_gap_days=gap,
        notes=tuple(notes),
    )


def reconstruct_queues(fixture: WaitingFixture, calibration: Calibration | None = None) -> Reconstruction:
    """在分析起点重建等待队列：只用起点前已获知的记录。"""
    if calibration is None:
        calibration = calibrate_behavior(fixture)
    origin = fixture.origin
    known = _events_known_at(fixture.events, origin)
    by_chain = _group_by_chain(known)
    median_delay = int(median(calibration.rebook_delays))

    active: list[ChainState] = []
    done: list[str] = []
    unknown: list[str] = []
    for chain in fixture.chains:
        events = by_chain.get(chain.chain_id)
        if not events:
            unknown.append(chain.chain_id)
            continue
        step_index, status, request_date, held_pool, cancel_date = _replay(chain, events)
        if status == "done":
            done.append(chain.chain_id)
            continue
        rejoin_date = None
        if status == "rebook" and cancel_date is not None:
            rejoin_date = cancel_date + dt.timedelta(days=median_delay)
        active.append(
            ChainState(
                chain_id=chain.chain_id,
                step_index=step_index,
                status=status,
                request_date=request_date or chain.requested_at,
                held_pool=held_pool,
                rejoin_date=rejoin_date,
            )
        )
    return Reconstruction(origin=origin, active=tuple(active), done=tuple(done), unknown=tuple(unknown))


def historical_summary(fixture: WaitingFixture) -> dict:
    """历史镜像：observed_until 之前的全部已知记录。

    未完成的链保留在风险集中，按 observed_until 删失；观察截止日不等于完成日。
    """
    until = fixture.observed_until
    known = _events_known_at(fixture.events, until)
    by_chain = _group_by_chain(known)

    waits: list[int] = []
    step_waits: dict[str, list[int]] = {}
    completed = 0
    censored: list[dict] = []
    overtime = 0
    for chain in fixture.chains:
        events = by_chain.get(chain.chain_id, [])
        step_index, status, request_date, _, _ = _replay(chain, events)
        done = status == "done"
        # 每个环节的等待：从该环节最近一次未完成请求（或上一环节完成）到完成。
        per_step: dict[str, int] = {}
        open_request: dict[str, dt.date] = {}
        for event in events:
            if event.kind == "requested":
                open_request.setdefault(event.step, event.at)
            elif event.kind == "completed":
                base = open_request.get(event.step)
                if base is None:
                    # 后续环节的请求是隐式的：以上一环节完成时间为请求时间。
                    earlier = [e.at for e in events if e.kind == "completed" and e.at <= event.at and e.step != event.step]
                    base = max(earlier) if earlier else chain.requested_at
                per_step[event.step] = (event.at - base).days
        if done:
            completed += 1
            last_completion = max(event.at for event in events if event.kind == "completed")
            wait = (last_completion - chain.requested_at).days
            waits.append(wait)
            if wait > chain.deadline_days:
                overtime += 1
            for step, value in per_step.items():
                step_waits.setdefault(step, []).append(value)
        else:
            censored_wait = (until - chain.requested_at).days
            if censored_wait > chain.deadline_days:
                overtime += 1
            censored.append(
                {
                    "chain_id": chain.chain_id,
                    "requested_at": chain.requested_at.isoformat(),
                    "censored_wait_days": censored_wait,
                    "already_overtime": censored_wait > chain.deadline_days,
                }
            )

    total = len(fixture.chains)
    return {
        "observed_until": until.isoformat(),
        "chains_total": total,
        "completed": completed,
        "censored": len(censored),
        "risk_set": censored,
        "overtime": overtime,
        "overtime_ratio": (overtime / total) if total else None,
        "total_wait_days": _distribution(waits),
        "step_wait_days": {step: _distribution(values) for step, values in sorted(step_waits.items())},
        "note": "未完成的链保留在风险集中，按观察截止日删失；观察截止日不等于完成日。",
    }


def _distribution(values: list[int | float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "max": ordered[-1],
    }


def _percentile(sorted_values: list[int | float], q: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * q
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return float(sorted_values[low] * (1 - frac) + sorted_values[high] * frac)
