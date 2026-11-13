"""候诊分析输入资料的结构与校验。

配置需要表达：工作时段（pools 的 weekdays/open/close/minutes_per_service）、
环节先后（chains 的 steps 顺序）、兼容院区（chains 的 compatible_sites）、
服务时限（chains 的 deadline_days）。所有时刻按同一地方日期解释。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

EVENT_KINDS = ("requested", "held", "cancelled", "completed")


class WaitingDataError(ValueError):
    """候诊分析输入资料不合法。"""


def _parse_date(value: Any, field_name: str) -> dt.date:
    if not isinstance(value, str):
        raise WaitingDataError(f"{field_name} 必须是 ISO 日期字符串")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise WaitingDataError(f"{field_name} 不是合法日期: {value!r}") from None


def _parse_minutes(value: Any, field_name: str) -> int:
    if not isinstance(value, str) or ":" not in value:
        raise WaitingDataError(f"{field_name} 必须是 HH:MM 格式")
    head, _, tail = value.partition(":")
    try:
        hours, minutes = int(head), int(tail)
    except ValueError:
        raise WaitingDataError(f"{field_name} 必须是 HH:MM 格式") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise WaitingDataError(f"{field_name} 超出一天范围: {value!r}")
    return hours * 60 + minutes


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WaitingDataError(f"{field_name} 必须是非空字符串")
    return value.strip()


def _text_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise WaitingDataError(f"{field_name} 必须是非空列表")
    return tuple(_text(item, field_name) for item in value)


def _weekdays(value: Any, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise WaitingDataError(f"{field_name} 必须是非空工作日列表")
    days = tuple(sorted({int(day) for day in value}))
    if any(day < 0 or day > 6 for day in days):
        raise WaitingDataError(f"{field_name} 工作日必须在 0..6")
    return days


@dataclass(frozen=True)
class Pool:
    """号源池：同一池可被多个服务（环节）共享。"""

    pool_id: str
    site: str
    units: int
    services: tuple[str, ...]
    minutes_per_service: int
    weekdays: tuple[int, ...]
    open_minutes: int
    close_minutes: int

    @property
    def slots_per_unit_day(self) -> int:
        return (self.close_minutes - self.open_minutes) // self.minutes_per_service

    def base_capacity(self, weekday: int) -> int:
        """该工作日（按历史配置）可用的号源数。"""
        if weekday not in self.weekdays:
            return 0
        return self.units * self.slots_per_unit_day


@dataclass(frozen=True)
class Chain:
    """就诊链：同一主体按顺序完成若干环节，取消后重约仍属于同一条链。"""

    chain_id: str
    subject_id: str
    requested_at: dt.date
    steps: tuple[str, ...]
    compatible_sites: tuple[str, ...]
    deadline_days: int
    priority_group: str


@dataclass(frozen=True)
class Event:
    """历史记录。at 是业务发生日期，known_at 是系统获知日期。"""

    event_id: str
    chain_id: str
    step: str
    kind: str
    pool_id: str | None
    at: dt.date
    known_at: dt.date


@dataclass(frozen=True)
class ScenarioChange:
    pool_id: str
    extra_units: int
    weekdays: tuple[int, ...]


@dataclass(frozen=True)
class Scenario:
    """容量情景。allow_transfer 表示允许跨院区共享号源（转院行为假设）。"""

    name: str
    changes: tuple[ScenarioChange, ...]
    allow_transfer: bool = False


@dataclass(frozen=True)
class DemandOverride:
    """可选的需求/行为覆盖；缺省时全部从历史校准。"""

    arrival_weekday_rates: tuple[float, ...] | None = None
    cancel_probability: Mapping[str, float] | None = None
    rebook_delays: tuple[int, ...] | None = None
    min_step_gap_days: int | None = None


@dataclass(frozen=True)
class WaitingFixture:
    schema_version: int
    seed: int
    origin: dt.date
    observed_until: dt.date
    horizon_days: int
    chains: tuple[Chain, ...]
    events: tuple[Event, ...]
    pools: tuple[Pool, ...]
    scenarios: tuple[Scenario, ...]
    demand: DemandOverride = field(default_factory=DemandOverride)
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def horizon_end(self) -> dt.date:
        """观察截止（删失）日期：起点之后 horizon_days 天，不等于任何完成日。"""
        return self.origin + dt.timedelta(days=self.horizon_days)

    def pool_map(self) -> dict[str, Pool]:
        return {pool.pool_id: pool for pool in self.pools}

    def chain_map(self) -> dict[str, Chain]:
        return {chain.chain_id: chain for chain in self.chains}


def _parse_pool(row: Mapping[str, Any]) -> Pool:
    pool_id = _text(row.get("pool_id"), "pool.pool_id")
    open_minutes = _parse_minutes(row.get("open"), f"pool.{pool_id}.open")
    close_minutes = _parse_minutes(row.get("close"), f"pool.{pool_id}.close")
    if close_minutes <= open_minutes:
        raise WaitingDataError(f"pool.{pool_id} 的 close 必须晚于 open")
    minutes_per_service = int(row.get("minutes_per_service", 0))
    if minutes_per_service <= 0:
        raise WaitingDataError(f"pool.{pool_id}.minutes_per_service 必须为正")
    units = int(row.get("units", 0))
    if units <= 0:
        raise WaitingDataError(f"pool.{pool_id}.units 必须为正")
    pool = Pool(
        pool_id=pool_id,
        site=_text(row.get("site"), f"pool.{pool_id}.site"),
        units=units,
        services=_text_list(row.get("services"), f"pool.{pool_id}.services"),
        minutes_per_service=minutes_per_service,
        weekdays=_weekdays(row.get("weekdays"), f"pool.{pool_id}.weekdays"),
        open_minutes=open_minutes,
        close_minutes=close_minutes,
    )
    if pool.slots_per_unit_day < 1:
        raise WaitingDataError(f"pool.{pool_id} 开放时段不足一次服务")
    return pool


def _parse_chain(row: Mapping[str, Any]) -> Chain:
    chain_id = _text(row.get("chain_id"), "chain.chain_id")
    deadline = int(row.get("deadline_days", 0))
    if deadline <= 0:
        raise WaitingDataError(f"chain.{chain_id}.deadline_days 必须为正")
    return Chain(
        chain_id=chain_id,
        subject_id=_text(row.get("subject_id", chain_id), f"chain.{chain_id}.subject_id"),
        requested_at=_parse_date(row.get("requested_at"), f"chain.{chain_id}.requested_at"),
        steps=_text_list(row.get("steps"), f"chain.{chain_id}.steps"),
        compatible_sites=_text_list(row.get("compatible_sites"), f"chain.{chain_id}.compatible_sites"),
        deadline_days=deadline,
        priority_group=str(row.get("priority_group", "default")),
    )


def _parse_event(row: Mapping[str, Any], chains: Mapping[str, Chain], pools: Mapping[str, Pool]) -> Event:
    event_id = _text(row.get("event_id"), "event.event_id")
    kind = _text(row.get("kind"), f"event.{event_id}.kind")
    if kind not in EVENT_KINDS:
        raise WaitingDataError(f"event.{event_id}.kind 未知: {kind!r}")
    chain_id = _text(row.get("chain_id"), f"event.{event_id}.chain_id")
    if chain_id not in chains:
        raise WaitingDataError(f"event.{event_id} 引用了未知链 {chain_id!r}")
    step = _text(row.get("step"), f"event.{event_id}.step")
    if step not in chains[chain_id].steps:
        raise WaitingDataError(f"event.{event_id} 的环节 {step!r} 不在链 {chain_id} 的环节序列中")
    pool_id = row.get("pool_id")
    if kind in ("held", "completed"):
        if pool_id is None:
            raise WaitingDataError(f"event.{event_id}（{kind}）缺少 pool_id")
        pool_id = _text(pool_id, f"event.{event_id}.pool_id")
        if pool_id not in pools:
            raise WaitingDataError(f"event.{event_id} 引用了未知号源池 {pool_id!r}")
    elif pool_id is not None:
        pool_id = _text(pool_id, f"event.{event_id}.pool_id")
    return Event(
        event_id=event_id,
        chain_id=chain_id,
        step=step,
        kind=kind,
        pool_id=pool_id,
        at=_parse_date(row.get("at"), f"event.{event_id}.at"),
        known_at=_parse_date(row.get("known_at"), f"event.{event_id}.known_at"),
    )


def _parse_scenario(row: Mapping[str, Any], pools: Mapping[str, Pool]) -> Scenario:
    name = _text(row.get("name"), "scenario.name")
    changes = []
    for item in row.get("changes", []) or []:
        pool_id = _text(item.get("pool_id"), f"scenario.{name}.changes.pool_id")
        if pool_id not in pools:
            raise WaitingDataError(f"scenario.{name} 引用了未知号源池 {pool_id!r}")
        extra_units = int(item.get("extra_units", 0))
        if extra_units < 0:
            raise WaitingDataError(f"scenario.{name} 的 extra_units 不能为负")
        changes.append(
            ScenarioChange(
                pool_id=pool_id,
                extra_units=extra_units,
                weekdays=_weekdays(item.get("weekdays"), f"scenario.{name}.changes.weekdays"),
            )
        )
    return Scenario(name=name, changes=tuple(changes), allow_transfer=bool(row.get("allow_transfer", False)))


def _parse_demand(row: Mapping[str, Any] | None) -> DemandOverride:
    if not row:
        return DemandOverride()
    rates = row.get("arrival_weekday_rates")
    if rates is not None:
        if not isinstance(rates, (list, tuple)) or len(rates) != 7:
            raise WaitingDataError("demand.arrival_weekday_rates 必须是长度为 7 的列表")
        rates = tuple(float(rate) for rate in rates)
        if any(rate < 0 for rate in rates):
            raise WaitingDataError("demand.arrival_weekday_rates 不能为负")
    cancel = row.get("cancel_probability")
    if cancel is not None:
        cancel = {str(key): float(value) for key, value in cancel.items()}
        if any(not 0.0 <= value <= 1.0 for value in cancel.values()):
            raise WaitingDataError("demand.cancel_probability 必须在 [0, 1]")
    delays = row.get("rebook_delays")
    if delays is not None:
        delays = tuple(int(value) for value in delays)
        if not delays or any(value < 0 for value in delays):
            raise WaitingDataError("demand.rebook_delays 必须是非负整数列表")
    gap = row.get("min_step_gap_days")
    if gap is not None and int(gap) < 0:
        raise WaitingDataError("demand.min_step_gap_days 不能为负")
    return DemandOverride(
        arrival_weekday_rates=rates,
        cancel_probability=cancel,
        rebook_delays=delays,
        min_step_gap_days=None if gap is None else int(gap),
    )


def parse_fixture(data: Mapping[str, Any]) -> WaitingFixture:
    """校验并构造候诊分析输入。不合法时抛出 WaitingDataError。"""
    if not isinstance(data, Mapping):
        raise WaitingDataError("输入必须是 JSON 对象")
    chains = tuple(_parse_chain(row) for row in data.get("chains", []) or [])
    if not chains:
        raise WaitingDataError("chains 不能为空")
    chain_ids = [chain.chain_id for chain in chains]
    if len(set(chain_ids)) != len(chain_ids):
        raise WaitingDataError("chain_id 存在重复")
    chain_map = {chain.chain_id: chain for chain in chains}

    pools = tuple(_parse_pool(row) for row in data.get("pools", []) or [])
    if not pools:
        raise WaitingDataError("pools 不能为空")
    pool_ids = [pool.pool_id for pool in pools]
    if len(set(pool_ids)) != len(pool_ids):
        raise WaitingDataError("pool_id 存在重复")
    pool_map = {pool.pool_id: pool for pool in pools}

    served = {service for pool in pools for service in pool.services}
    for chain in chains:
        for step in chain.steps:
            if step not in served:
                raise WaitingDataError(f"chain.{chain.chain_id} 的环节 {step!r} 没有任何号源池提供")

    events = tuple(_parse_event(row, chain_map, pool_map) for row in data.get("events", []) or [])
    event_ids = [event.event_id for event in events]
    if len(set(event_ids)) != len(event_ids):
        raise WaitingDataError("event_id 存在重复")

    scenarios = tuple(_parse_scenario(row, pool_map) for row in data.get("scenarios", []) or [])
    if not scenarios:
        raise WaitingDataError("scenarios 不能为空")
    names = [scenario.name for scenario in scenarios]
    if len(set(names)) != len(names):
        raise WaitingDataError("scenario.name 存在重复")

    origin = _parse_date(data.get("origin"), "origin")
    observed_until = _parse_date(data.get("observed_until"), "observed_until")
    if observed_until < origin:
        raise WaitingDataError("observed_until 不能早于 origin")
    horizon_days = int(data.get("horizon_days", 0))
    if horizon_days <= 0:
        raise WaitingDataError("horizon_days 必须为正")

    return WaitingFixture(
        schema_version=int(data.get("schema_version", 1)),
        seed=int(data.get("seed", 0)),
        origin=origin,
        observed_until=observed_until,
        horizon_days=horizon_days,
        chains=chains,
        events=events,
        pools=pools,
        scenarios=scenarios,
        demand=_parse_demand(data.get("demand")),
        raw=dict(data),
    )


def load_fixture(path: str | Path) -> WaitingFixture:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_fixture(data)
