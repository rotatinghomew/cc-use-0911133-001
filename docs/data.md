# 业务样例数据

`fixtures/waiting.json` 使用本地合成数据，不含真实个人资料。

就诊链和事件均为虚构，at 是业务发生时间，known_at 是系统获知时间；pools 记录周内开放时段，所有时刻按同一地方时间解释。services 是可共享同池的环节。

chains 的 steps 表示环节先后，compatible_sites 表示兼容院区，deadline_days 是服务时限；scenarios 的 changes 用 extra_units 在指定工作日增加号源单元，allow_transfer 表示允许跨院区共享号源。可选的 demand 段（arrival_weekday_rates、cancel_probability、rebook_delays、min_step_gap_days）用于覆盖需求与行为参数，缺省时由 `oncology_capacity_exchange.waiting` 从起点前已获知的记录校准。

`fixtures/demo.json` 是当前单笔业务流程的样例，和批量分析资料分开保存。
