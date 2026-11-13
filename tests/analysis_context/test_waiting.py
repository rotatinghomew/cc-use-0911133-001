"""候诊压力分析回归测试。

覆盖：小队列人工可核对结果、共享池竞争、跨环节中断、未结束观察，
以及相同随机种子可复算、三情景共享需求冲击、外推边界标注。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from oncology_capacity_exchange.waiting import (
    WaitingDataError,
    analyze,
    calibrate_behavior,
    historical_summary,
    load_fixture,
    parse_fixture,
    reconstruct_queues,
)


ROOT = Path(__file__).resolve().parents[2]

# 2026-08-10 是周一（weekday 0）。
ORIGIN = "2026-08-10"


def _pool(pool_id, services, weekdays, units=1, open_="09:00", close="10:00", minutes=60, site="A"):
    return {
        "pool_id": pool_id,
        "site": site,
        "units": units,
        "services": services,
        "minutes_per_service": minutes,
        "weekdays": weekdays,
        "open": open_,
        "close": close,
    }


def _chain(chain_id, requested_at, steps, deadline=30, sites=("A",)):
    return {
        "chain_id": chain_id,
        "subject_id": f"subj-{chain_id}",
        "requested_at": requested_at,
        "steps": list(steps),
        "compatible_sites": list(sites),
        "deadline_days": deadline,
        "priority_group": "test",
    }


def _event(event_id, chain_id, step, kind, at, known_at=None, pool_id=None):
    row = {
        "event_id": event_id,
        "chain_id": chain_id,
        "step": step,
        "kind": kind,
        "at": at,
        "known_at": known_at if known_at is not None else at,
    }
    if pool_id is not None:
        row["pool_id"] = pool_id
    return row


def _fixture(chains, events, pools, scenarios=None, horizon_days=21, demand=None, observed_until="2026-09-11"):
    data = {
        "schema_version": 1,
        "seed": 5,
        "origin": ORIGIN,
        "observed_until": observed_until,
        "horizon_days": horizon_days,
        "chains": chains,
        "events": events,
        "pools": pools,
        "scenarios": scenarios if scenarios is not None else [{"name": "current", "changes": []}],
        "demand": demand
        if demand is not None
        else {"arrival_weekday_rates": [0, 0, 0, 0, 0, 0, 0], "cancel_probability": {}, "rebook_delays": [1]},
    }
    return parse_fixture(data)


def _metric(report, scenario, metric):
    return report["scenarios"][scenario]["metrics"][metric]


def _chain_row(report, scenario, chain_id):
    for row in report["chain_stats"]:
        if row["scenario"] == scenario and row["chain_id"] == chain_id:
            return row
    raise AssertionError(f"未找到链 {chain_id}")


class SmallQueueTests(unittest.TestCase):
    """小队列人工可核对：两条链竞争每周一仅有的 1 个评估号源。"""

    def setUp(self):
        # P1 每周一 1 个评估号源；P2 每天 3 个咨询号源（不构成瓶颈）。
        self.fixture = _fixture(
            chains=[
                _chain("c1", "2026-08-09", ["assessment", "consultation"]),
                _chain("c2", "2026-08-09", ["assessment", "consultation"]),
            ],
            events=[
                _event("e1", "c1", "assessment", "requested", "2026-08-09"),
                _event("e2", "c2", "assessment", "requested", "2026-08-09"),
            ],
            pools=[
                _pool("P1", ["assessment"], [0], close="10:00"),
                _pool("P2", ["consultation"], [0, 1, 2, 3, 4, 5, 6], open_="09:00", close="12:00"),
            ],
            horizon_days=21,
            demand={
                "arrival_weekday_rates": [0, 0, 0, 0, 0, 0, 0],
                "cancel_probability": {},
                "rebook_delays": [1],
                "min_step_gap_days": 1,
            },
        )

    def test_hand_checkable_waits(self):
        report = analyze(self.fixture, seed=5, replications=3, top=5)
        # c1（chain_id 字典序优先）08-10 周一完成评估，08-11 完成咨询：总等待 2 天。
        # c2 下一周一 08-17 完成评估，08-18 完成咨询：总等待 9 天。
        self.assertEqual(_chain_row(report, "current", "c1")["mean_total_wait"], 2)
        self.assertEqual(_chain_row(report, "current", "c2")["mean_total_wait"], 9)
        metrics = _metric(report, "current", "mean_total_wait")
        self.assertEqual(metrics["mean"], 5.5)
        self.assertEqual(metrics["sd"], 0)  # 无随机因素，各次复制完全一致
        self.assertEqual(_metric(report, "current", "completed")["mean"], 2)
        self.assertEqual(_metric(report, "current", "censored")["mean"], 0)
        self.assertEqual(_metric(report, "current", "overtime")["mean"], 0)

    def test_hand_checkable_occupancy(self):
        report = analyze(self.fixture, seed=5, replications=3, top=5)
        pools = report["scenarios"]["current"]["pools"]
        # 21 天窗口内 3 个周一 → P1 可用 3，已用 2。
        self.assertEqual(pools["P1"]["available_slots"]["mean"], 3)
        self.assertEqual(pools["P1"]["used_slots"]["mean"], 2)
        self.assertAlmostEqual(pools["P1"]["occupancy"]["mean"], 2 / 3, places=3)
        # P2 每天 3 个号源 × 21 天 = 63，已用 2。
        self.assertEqual(pools["P2"]["available_slots"]["mean"], 63)
        self.assertEqual(pools["P2"]["used_slots"]["mean"], 2)


class SharedPoolCompetitionTests(unittest.TestCase):
    """共享池竞争：同一号源池被两个服务共享，按请求先后竞争。"""

    def test_fifo_across_services(self):
        fixture = _fixture(
            chains=[
                _chain("cA", "2026-08-09", ["assessment"]),
                _chain("cB", "2026-08-08", ["consultation"]),
            ],
            events=[
                _event("eA", "cA", "assessment", "requested", "2026-08-09"),
                _event("eB", "cB", "consultation", "requested", "2026-08-08"),
            ],
            pools=[_pool("P", ["assessment", "consultation"], [0, 1, 2, 3, 4, 5, 6])],
            horizon_days=7,
        )
        report = analyze(fixture, seed=5, replications=3, top=5)
        # 每天 1 个号源：cB 请求更早（08-08），08-10 先完成；cA 08-11 完成。
        self.assertEqual(_chain_row(report, "current", "cB")["mean_total_wait"], 2)
        self.assertEqual(_chain_row(report, "current", "cA")["mean_total_wait"], 2)
        pools = report["scenarios"]["current"]["pools"]
        self.assertEqual(pools["P"]["available_slots"]["mean"], 7)
        self.assertEqual(pools["P"]["used_slots"]["mean"], 2)


class CrossStepInterruptionTests(unittest.TestCase):
    """跨环节中断：占位被取消后重约仍属于同一条链，链级等待从最初请求累计。"""

    def setUp(self):
        self.fixture = _fixture(
            chains=[_chain("c1", "2026-08-01", ["assessment", "consultation"])],
            events=[
                _event("e1", "c1", "assessment", "requested", "2026-08-01"),
                _event("e2", "c1", "assessment", "held", "2026-08-03", pool_id="P1"),
                _event("e3", "c1", "assessment", "cancelled", "2026-08-05"),
            ],
            pools=[
                _pool("P1", ["assessment"], [0, 1, 2, 3, 4, 5, 6]),
                _pool("P2", ["consultation"], [0, 1, 2, 3, 4, 5, 6], open_="09:00", close="12:00"),
            ],
            horizon_days=20,
            demand={
                "arrival_weekday_rates": [0, 0, 0, 0, 0, 0, 0],
                "cancel_probability": {},
                "rebook_delays": [3],
                "min_step_gap_days": 1,
            },
        )

    def test_reconstruction_marks_rebook(self):
        reconstruction = reconstruct_queues(self.fixture, calibrate_behavior(self.fixture))
        self.assertEqual(len(reconstruction.active), 1)
        state = reconstruction.active[0]
        self.assertEqual(state.status, "rebook")
        self.assertEqual(state.rejoin_date.isoformat(), "2026-08-08")  # 取消 08-05 + 3 天

    def test_chain_identity_and_accumulated_wait(self):
        report = analyze(self.fixture, seed=5, replications=3, top=5)
        row = _chain_row(report, "current", "c1")
        # 重约 08-08 生效 → 08-10 完成评估，08-11 完成咨询；链仍是同一条，
        # 链级等待从最初请求 08-01 累计：10 天（中断不重置时钟）。
        self.assertEqual(row["mean_total_wait"], 10)
        self.assertEqual(row["appearances"], 3)
        self.assertEqual(_metric(report, "current", "chains_total")["mean"], 1)
        self.assertEqual(_metric(report, "current", "completed")["mean"], 1)


class UnfinishedObservationTests(unittest.TestCase):
    """未结束观察：窗口内未完成的链保留在风险集中，截止日不等于完成日。"""

    def setUp(self):
        # P1 仅周日开放；5 天窗口（周一至周五）内没有任何号源。
        self.fixture = _fixture(
            chains=[
                _chain("c1", "2026-08-09", ["assessment"], deadline=14),
                _chain("c2", "2026-08-01", ["assessment"], deadline=10),
            ],
            events=[
                _event("e1", "c1", "assessment", "requested", "2026-08-09"),
                _event("e2", "c2", "assessment", "requested", "2026-08-01"),
            ],
            pools=[_pool("P1", ["assessment"], [6])],
            horizon_days=5,
            observed_until="2026-08-20",
        )

    def test_censored_chains_stay_in_risk_set(self):
        report = analyze(self.fixture, seed=5, replications=3, top=5)
        metrics = report["scenarios"]["current"]["metrics"]
        self.assertEqual(metrics["completed"]["mean"], 0)
        self.assertEqual(metrics["censored"]["mean"], 2)
        # c1 删失等待 6 天 ≤ 14 → 仍在风险集；c2 删失等待 14 天 > 10 → 已超时。
        self.assertEqual(metrics["at_risk"]["mean"], 1)
        self.assertEqual(metrics["overtime"]["mean"], 1)
        self.assertEqual(metrics["overtime_ratio"]["mean"], 0.5)

    def test_historical_summary_censors_at_observed_until(self):
        summary = historical_summary(self.fixture)
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(summary["censored"], 2)
        self.assertEqual(summary["overtime"], 1)  # 仅 c2（19 天 > 10）
        self.assertEqual(len(summary["risk_set"]), 2)
        self.assertIn("观察截止日不等于完成日", summary["note"])


class ReproducibilityTests(unittest.TestCase):
    """相同随机种子可复算；三种情景共享可比较的需求冲击。"""

    def setUp(self):
        self.fixture = load_fixture(ROOT / "fixtures/waiting.json")

    def test_same_seed_reproduces_report(self):
        first = analyze(self.fixture, seed=71, replications=5, top=5)
        second = analyze(self.fixture, seed=71, replications=5, top=5)
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_different_seed_changes_demand_shocks(self):
        first = analyze(self.fixture, seed=71, replications=5, top=5)
        second = analyze(self.fixture, seed=99, replications=5, top=5)
        self.assertNotEqual(
            json.dumps(first["scenarios"], sort_keys=True),
            json.dumps(second["scenarios"], sort_keys=True),
        )

    def test_scenarios_share_demand_shocks(self):
        # single-site 只给本就有富余的 A-shared 加容量，对瓶颈无影响；
        # 若三情景不共享需求冲击，该差异不可能精确为 0。
        report = analyze(self.fixture, seed=71, replications=10, top=5)
        for row in report["pairwise"]:
            if row["pair"] == ["current", "single-site"]:
                self.assertEqual(row["mean"], 0.0)
                self.assertEqual(row["sd"], 0.0)


class ExtrapolationTests(unittest.TestCase):
    """历史从未覆盖的容量组合必须明确标注外推边界。"""

    def setUp(self):
        self.fixture = load_fixture(ROOT / "fixtures/waiting.json")
        self.report = analyze(self.fixture, seed=71, replications=5, top=5)

    def test_new_weekday_and_units_flagged(self):
        notes = self.report["scenarios"]["shared-half-day"]["extrapolation"]
        dimensions = {note["dimension"] for note in notes}
        self.assertIn("weekday", dimensions)  # B-consult 历史上工作日 2 不开放
        self.assertIn("service-pool", dimensions)  # consultation@A-shared 历史未出现
        for note in notes:
            self.assertIn("未被历史证实", note["boundary"])
        single = self.report["scenarios"]["single-site"]["extrapolation"]
        self.assertEqual({note["dimension"] for note in single}, {"units"})
        self.assertEqual(self.report["scenarios"]["current"]["extrapolation"], [])

    def test_behavior_attribution_separates_capacity_from_transfer(self):
        attribution = {entry["scenario"]: entry for entry in self.report["behavior_attribution"]}
        shared = attribution["shared-half-day"]["metrics"]["mean_total_wait"]
        # 纯容量作用与转院假设作用都被单独给出，且方向一致（均为负=改善）。
        self.assertLess(shared["capacity_effect"]["mean"], 0)
        self.assertLess(shared["transfer_effect"]["mean"], 0)
        single = attribution["single-site"]["metrics"]["mean_total_wait"]
        self.assertIsNone(single["transfer_effect"])  # 未开启转院，无转院作用
        self.assertEqual(single["capacity_effect"]["mean"], 0.0)


class ValidationTests(unittest.TestCase):
    def test_invalid_config_rejected(self):
        with self.assertRaises(WaitingDataError):
            _fixture(
                chains=[_chain("c1", "2026-08-09", ["assessment"])],
                events=[],
                pools=[_pool("P1", ["assessment"], [0])],
                scenarios=[{"name": "bad", "changes": [{"pool_id": "ghost", "extra_units": 1, "weekdays": [1]}]}],
            )

    def test_unserved_step_rejected(self):
        with self.assertRaises(WaitingDataError):
            _fixture(
                chains=[_chain("c1", "2026-08-09", ["assessment", "surgery"])],
                events=[],
                pools=[_pool("P1", ["assessment"], [0])],
            )


if __name__ == "__main__":
    unittest.main()
