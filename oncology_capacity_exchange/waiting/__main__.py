"""命令行入口：python3 -m oncology_capacity_exchange.waiting <fixture> --output <dir>"""

from __future__ import annotations

import argparse
import json

from .analysis import analyze
from .model import load_fixture
from .report import write_outputs


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="oncology_capacity_exchange.waiting",
        description="候诊压力分析：重建等待队列并比较现状、单点扩容与共享号源三种配置（服务容量分析，不涉及临床建议）",
    )
    parser.add_argument("fixture", help="批量分析资料（如 fixtures/waiting.json）")
    parser.add_argument("--output", default="waiting-result", help="结果输出目录（默认 waiting-result）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（默认取资料中的 seed）")
    parser.add_argument("--replications", type=int, default=40, help="每个情景的复制次数（默认 40）")
    parser.add_argument("--top", type=int, default=10, help="每个情景保留的尾部链数量（默认 10）")
    args = parser.parse_args()

    fixture = load_fixture(args.fixture)
    report = analyze(fixture, seed=args.seed, replications=args.replications, top=args.top)
    paths = write_outputs(report, args.output)

    headline = {
        "output": paths,
        "seed": report["seed"],
        "replications": report["replications"],
        "scenarios": {
            name: {
                "mean_total_wait": scenario["metrics"]["mean_total_wait"]["mean"],
                "overtime_ratio": scenario["metrics"]["overtime_ratio"]["mean"],
            }
            for name, scenario in report["scenarios"].items()
        },
        "pairwise": [
            {
                "pair": row["pair"],
                "metric": row["metric"],
                "mean": row["mean"],
                "p05": row["p05"],
                "p95": row["p95"],
            }
            for row in report["pairwise"]
            if row["metric"] == "overtime_ratio"
        ],
    }
    print(json.dumps(headline, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
