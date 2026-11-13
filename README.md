# oncology_capacity_exchange

区域服务容量记录处理：容量单位归一、号源占位、回执对账和状态恢复。当前入口是 `oncology_capacity_exchange.bin.capacity_sync`，它使用本地 JSON 资料和 SQLite 状态；目录 `oncology_capacity_exchange` 存放业务模块，`tests/capacity_contracts` 验证既有业务行为。

## 环境

使用 Python 3.11，全部依赖来自标准库。`requirements.txt` 为空，不需要下载数据、模型或第三方包。数据库在运行时创建，可以删除临时数据库重新开始。

## 检查

```bash
python3 -m unittest discover -s tests/capacity_contracts -v
python3 -m unittest discover -s tests/analysis_context -v
python3 -m compileall -q oncology_capacity_exchange tests scripts
```

## 运行

```bash
python3 -m oncology_capacity_exchange.bin.capacity_sync  fixtures/demo.json --db /tmp/oncology_capacity_exchange-demo.sqlite3
python3 scripts/check_project.py
```

`check_project.py` 串行执行测试、编译和真实业务入口，将本次业务输出完整打印到终端；状态文件使用独立临时目录，重复运行不沿用上次状态。

批量分析资料位于 `fixtures/waiting.json`，字段含义见 `docs/data.md`。它与当前单笔流程的输入格式不同，不能直接交给原来的入口处理。

## 候诊压力分析（批量，可复用）

`oncology_capacity_exchange.waiting` 在单笔流程之外提供候诊压力分析：用历史预约、占位释放、取消和实际完成记录在分析起点重建等待队列，按工作日历与环节顺序向前模拟，并比较现状、单点扩容、共享号源三种配置。这是服务容量分析，不涉及临床建议，也不改动线上预约的原有结果。

```bash
python3 -m oncology_capacity_exchange.waiting fixtures/waiting.json --output /tmp/waiting-result
```

输出目录包含：

- `report.json`：按就诊链统计的等待分布、超时比例、各资源池占用、成对情景差异及不确定性、行为假设归因、外推边界、构成尾部的具体记录（含历史事件）。
- `summary.md`：上述结果的可读汇总与假设清单。
- `chains.csv`：各情景下每条链的等待与超时/删失统计，可追溯到具体记录。

相同随机种子可复算（`--seed`，默认取资料中的 `seed`），三种情景共享同一组需求冲击；`--replications` 控制复制次数。分析起点之后才获知的记录不作为起点时的分配依据；尚未完成的链保留在风险集中，观察截止日不等于完成日。

直接展示扩容前后差异的例子：

```bash
python3 -m oncology_capacity_exchange.waiting fixtures/waiting.json --output /tmp/waiting-result
python3 - <<'EOF'
import json
report = json.load(open("/tmp/waiting-result/report.json"))
for name, scenario in report["scenarios"].items():
    metrics = scenario["metrics"]
    print(name, "平均候诊天数", metrics["mean_total_wait"]["mean"],
          "超时比例", metrics["overtime_ratio"]["mean"])
for row in report["pairwise"]:
    if row["metric"] == "overtime_ratio":
        print(row["pair"], "超时比例差异", row["mean"], "90% 区间", [row["p05"], row["p95"]])
EOF
```

在当前资料上可以看到：单点扩容（single-site，只给本有富余的 A-shared 加半天）超时比例几乎不变，而共享号源（shared-half-day，给瓶颈池 B-consult 加半天并允许转院）显著降低超时比例；报告的行为假设归因段会把改善拆成纯容量作用与转院假设作用，外推边界段会标明哪些容量组合历史从未覆盖（改善仅为模型投影）。

回归测试：`tests/analysis_context/test_waiting.py` 覆盖小队列人工可核对结果、共享池竞争、跨环节中断、未结束观察、相同种子可复算与外推边界标注。
