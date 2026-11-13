"""日粒度离散事件模拟：需求冲击、排队分配、取消重约与删失。

方法要点（假设清单见报告 assumptions）：
- 容量 = 单元数 × (开放分钟 // 单次服务分钟)，按工作日展开；情景的 extra_units
  可在池原本不开放的工作日增加号源（新增半天门诊）。
- 默认按历史归属池路由；情景开启 allow_transfer 时，兼容院区的其他池也可服务
  （历史归属池始终可用，即使该院区不在 compatible_sites 中——历史数据如此）。
- 同一池被多个服务共享时，按 (请求日期, chain_id) 的先进先出顺序竞争。
- 取消按单次占位概率发生，释放的号源当天可再分配；取消的链按经验重约间隔
  重新排队，链身份不变（取消后重约仍属于同一条链）。
- 三种情景共享同一组需求冲击（公共随机数）：同一复制内，新链到达、模板与
  每条链的取消抽签完全一致，保证成对差异可比较；相同随机种子可复算。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from dataclasses import dataclass, field

from .history import Calibration, ChainTemplate, Reconstruction
from .model import Pool, Scenario, WaitingFixture

# 同一号源当天因取消而释放后允许再分配的次数上限（防御性，正常远低于此）。
MAX_REALLOC_PASSES = 8


@dataclass(frozen=True)
class Behavior:
    """行为假设开关：取消与转院。用于区分容量作用与行为假设作用。"""

    cancellations: bool = True
    allow_transfer: bool = False


@dataclass(frozen=True)
class Variant:
    """一次模拟运行的配置：情景容量 × 行为假设。"""

    key: str
    scenario: str
    capacity: dict[str, dict[int, int]]  # pool_id -> weekday -> slots
    behavior: Behavior


@dataclass(frozen=True)
class Arrival:
    date: dt.date
    chain_id: str
    template: ChainTemplate


@dataclass
class SimChain:
    chain_id: str
    source: str  # history | arrival
    requested_at: dt.date
    steps: tuple[str, ...]
    compatible_sites: tuple[str, ...]
    deadline_days: int
    step_index: int
    status: str  # waiting | rebook | done | censored
    request_date: dt.date
    rejoin_date: dt.date | None = None
    completed_at: dt.date | None = None
    step_waits: dict[str, int] = field(default_factory=dict)
    cancellations: int = 0
    rng: random.Random | None = None


@dataclass(frozen=True)
class PoolDay:
    available: int = 0
    used: int = 0
    lost: int = 0
    cancellations: int = 0
    queue_depth: int = 0


@dataclass
class SimResult:
    variant_key: str
    scenario: str
    records: list[dict]
    pool_days: dict[str, list[PoolDay]]
    horizon_days: int


def _chain_rng(seed: int, replication: int, chain_id: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}|{replication}|{chain_id}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit = pow(2.718281828459045, -lam)
    count = 0
    product = 1.0
    while product > limit:
        product *= rng.random()
        count += 1
    return count - 1


def generate_arrivals(
    seed: int,
    replication: int,
    calibration: Calibration,
    origin: dt.date,
    horizon_days: int,
) -> list[Arrival]:
    """生成一次复制的需求冲击。只依赖 (seed, replication)，与情景无关。"""
    rng = random.Random(
        int.from_bytes(hashlib.sha256(f"{seed}|arrivals|{replication}".encode("utf-8")).digest()[:8], "big")
    )
    arrivals: list[Arrival] = []
    sequence = 0
    for offset in range(horizon_days):
        date = origin + dt.timedelta(days=offset)
        count = _poisson(rng, calibration.arrival_weekday_rates[date.weekday()])
        for _ in range(count):
            if not calibration.templates:
                break
            template = rng.choice(calibration.templates)
            arrivals.append(Arrival(date=date, chain_id=f"sim-{replication:03d}-{sequence:03d}", template=template))
            sequence += 1
    return arrivals


def build_variants(fixture: WaitingFixture) -> list[Variant]:
    """为每个情景构造基础变体与行为归因变体（去重）。

    - base：情景容量 + 情景自身行为（allow_transfer 取情景配置，取消开启）。
    - capacity-only：情景容量 + 基线行为（关闭转院），隔离纯容量作用。
    - no-cancel：情景容量与转院设置 + 关闭取消，衡量取消假设的影响。
    """
    variants: list[Variant] = []
    seen: set[tuple] = set()

    def capacity_map(scenario: Scenario) -> dict[str, dict[int, int]]:
        changes: dict[str, set[int]] = {}
        extra: dict[str, int] = {}
        for change in scenario.changes:
            changes.setdefault(change.pool_id, set()).update(change.weekdays)
            extra[change.pool_id] = extra.get(change.pool_id, 0) + change.extra_units
        capacity: dict[str, dict[int, int]] = {}
        for pool in fixture.pools:
            per_day: dict[int, int] = {}
            for weekday in range(7):
                slots = pool.base_capacity(weekday)
                if weekday in changes.get(pool.pool_id, set()):
                    slots += extra.get(pool.pool_id, 0) * pool.slots_per_unit_day
                per_day[weekday] = slots
            capacity[pool.pool_id] = per_day
        return capacity

    for scenario in fixture.scenarios:
        capacity = capacity_map(scenario)
        capacity_key = tuple(sorted((pid, tuple(sorted(days.items()))) for pid, days in capacity.items()))
        for suffix, behavior in (
            ("base", Behavior(cancellations=True, allow_transfer=scenario.allow_transfer)),
            ("capacity-only", Behavior(cancellations=True, allow_transfer=False)),
            ("no-cancel", Behavior(cancellations=False, allow_transfer=scenario.allow_transfer)),
        ):
            marker = (capacity_key, behavior)
            if marker in seen:
                continue
            seen.add(marker)
            variants.append(
                Variant(
                    key=f"{scenario.name}|{suffix}",
                    scenario=scenario.name,
                    capacity=capacity,
                    behavior=behavior,
                )
            )
    return variants


def _eligible(
    chain: SimChain,
    pool: Pool,
    date: dt.date,
    calibration: Calibration,
    allow_transfer: bool,
) -> bool:
    if chain.status != "waiting" or chain.request_date > date:
        return False
    if chain.step_index >= len(chain.steps):
        return False
    service = chain.steps[chain.step_index]
    if service not in pool.services:
        return False
    if calibration.home_pool.get(service) == pool.pool_id:
        return True
    return allow_transfer and pool.site in chain.compatible_sites


def simulate(
    fixture: WaitingFixture,
    calibration: Calibration,
    reconstruction: Reconstruction,
    variant: Variant,
    arrivals: list[Arrival],
    seed: int,
    replication: int,
) -> SimResult:
    """在指定变体与需求冲击下，从重建队列向前模拟 horizon_days 天。"""
    origin = fixture.origin
    horizon_end = fixture.horizon_end
    chain_map = fixture.chain_map()
    pool_by_id = fixture.pool_map()
    pools = list(fixture.pools)

    chains: dict[str, SimChain] = {}
    for state in reconstruction.active:
        chain = chain_map[state.chain_id]
        sim = SimChain(
            chain_id=chain.chain_id,
            source="history",
            requested_at=chain.requested_at,
            steps=chain.steps,
            compatible_sites=chain.compatible_sites,
            deadline_days=chain.deadline_days,
            step_index=state.step_index,
            status="waiting" if state.status in ("waiting", "held") else "rebook",
            request_date=state.request_date,
            rejoin_date=state.rejoin_date,
            rng=_chain_rng(seed, replication, chain.chain_id),
        )
        chains[sim.chain_id] = sim
    arrivals_by_date: dict[dt.date, list[Arrival]] = {}
    for arrival in arrivals:
        arrivals_by_date.setdefault(arrival.date, []).append(arrival)

    pool_days: dict[str, list[PoolDay]] = {pool.pool_id: [] for pool in pools}
    allow_transfer = variant.behavior.allow_transfer
    cancellations_on = variant.behavior.cancellations
    gap = calibration.min_step_gap_days

    for offset in range(fixture.horizon_days):
        date = origin + dt.timedelta(days=offset)
        weekday = date.weekday()

        # 1) 取消后重约的链重新排队（重约日期早于或等于当天即生效）。
        for sim in chains.values():
            if sim.status == "rebook" and sim.rejoin_date is not None and sim.rejoin_date <= date:
                sim.status = "waiting"
                sim.request_date = sim.rejoin_date
                sim.rejoin_date = None

        # 2) 新需求冲击到达。
        for arrival in arrivals_by_date.get(date, []):
            template = arrival.template
            chains[arrival.chain_id] = SimChain(
                chain_id=arrival.chain_id,
                source="arrival",
                requested_at=date,
                steps=template.steps,
                compatible_sites=template.compatible_sites,
                deadline_days=template.deadline_days,
                step_index=0,
                status="waiting",
                request_date=date,
                rng=_chain_rng(seed, replication, arrival.chain_id),
            )

        # 3) 各池按配置顺序分配当天号源；取消释放的号源当天可再分配。
        for pool in pools:
            available = variant.capacity[pool.pool_id][weekday]
            used = lost = cancelled = 0
            if available:
                slots = available
                passes = 0
                while slots > 0:
                    candidates = sorted(
                        (
                            sim
                            for sim in chains.values()
                            if _eligible(sim, pool, date, calibration, allow_transfer)
                        ),
                        key=lambda sim: (sim.request_date, sim.chain_id),
                    )
                    if not candidates:
                        break
                    sim = candidates[0]
                    service = sim.steps[sim.step_index]
                    assert sim.rng is not None
                    if cancellations_on and sim.rng.random() < calibration.cancel_probability.get(service, 0.0):
                        cancelled += 1
                        sim.cancellations += 1
                        delay = sim.rng.choice(calibration.rebook_delays)
                        sim.status = "rebook"
                        sim.rejoin_date = date + dt.timedelta(days=delay)
                        passes += 1
                        if passes > MAX_REALLOC_PASSES:
                            slots -= 1
                            lost += 1
                            passes = 0
                        continue
                    passes = 0
                    slots -= 1
                    used += 1
                    sim.step_waits[service] = (date - sim.request_date).days
                    sim.step_index += 1
                    if sim.step_index >= len(sim.steps):
                        sim.status = "done"
                        sim.completed_at = date
                    else:
                        sim.request_date = date + dt.timedelta(days=gap)
            depth = sum(1 for sim in chains.values() if _eligible(sim, pool, date, calibration, allow_transfer))
            pool_days[pool.pool_id].append(
                PoolDay(available=available, used=used, lost=lost, cancellations=cancelled, queue_depth=depth)
            )

    # 4) 观察截止：未完成的链保留在风险集中，按删失处理（截止日不等于完成日）。
    records: list[dict] = []
    for chain_id in sorted(chains):
        sim = chains[chain_id]
        done = sim.status == "done"
        end = sim.completed_at if done else horizon_end
        total_wait = (end - sim.requested_at).days
        overtime = total_wait > sim.deadline_days
        records.append(
            {
                "chain_id": chain_id,
                "source": sim.source,
                "requested_at": sim.requested_at.isoformat(),
                "steps": list(sim.steps),
                "compatible_sites": list(sim.compatible_sites),
                "deadline_days": sim.deadline_days,
                "completed": done,
                "completed_at": sim.completed_at.isoformat() if sim.completed_at else None,
                "censored": not done,
                "total_wait_days": total_wait,
                "overtime": overtime,
                "at_risk": (not done) and not overtime,
                "step_waits": dict(sim.step_waits),
                "cancellations": sim.cancellations,
            }
        )
    return SimResult(
        variant_key=variant.key,
        scenario=variant.scenario,
        records=records,
        pool_days=pool_days,
        horizon_days=fixture.horizon_days,
    )
