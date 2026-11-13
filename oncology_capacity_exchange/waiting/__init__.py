"""候诊压力分析（批量、可复用）。

在既有单条容量记录流程之外，提供基于历史预约、占位释放、取消和实际完成记录的
等待队列重建能力，并按现状、单点扩容、共享号源等配置做情景比较。

本模块只做服务容量分析，不涉及临床建议，也不改动线上预约流程的原有结果。
"""

from .model import (
    Chain,
    Event,
    Pool,
    Scenario,
    WaitingDataError,
    WaitingFixture,
    load_fixture,
    parse_fixture,
)
from .history import (
    Calibration,
    Reconstruction,
    calibrate_behavior,
    historical_summary,
    reconstruct_queues,
)
from .analysis import analyze
from .report import write_outputs

__all__ = [
    "Chain",
    "Event",
    "Pool",
    "Scenario",
    "WaitingDataError",
    "WaitingFixture",
    "load_fixture",
    "parse_fixture",
    "Calibration",
    "Reconstruction",
    "calibrate_behavior",
    "historical_summary",
    "reconstruct_queues",
    "analyze",
    "write_outputs",
]
