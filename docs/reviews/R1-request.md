# R1 审查请求（交 GPT‑6.1 Sol）

> 你只负责独立审查，不编写功能、测试或修改源码。请只报告有证据、可操作的问题，每条写明：影响、文件与行号、触发条件或复现步骤、修复方向，并标注“已验证”或“待核实”。没有发现可核实的问题时如实说明，不需要为凑数量提问题。审查结论请写入 `docs/reviews/R1.md`，由 Claude 修复后在同一文件登记关闭情况。

## 1. 版本与范围

- 仓库：`fiscal-agent-harness`，分支 `main`。审查的版本以本文件所在的提交为准（`git log -1`）。
- 依据：官方材料 v1.1.0（`data/source/`），以及开发规划 `docs/development-review-plan-v2.md`。
- 本轮范围对应规划中的 R1 与 R2（进度提前，合并审查）：数据契约、权限、规则选择、金额口径；三场景全链路、隔离、授权、保存核验、运行记录完整性。

## 2. 复现命令

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt     # Windows：scripts\setup.bat
cp .env.example .env    # 填 LLM_API_KEY（OpenAI 兼容接口，默认 DeepSeek）
python -m fiscal_harness init
python -m pytest -q                       # 确定性测试，不调用模型
python -m evals.run --repeat 3            # 真实模型多轮评测（约 ¥0.03）
python -m evals.experiment --repeat 5     # 规则解析对比实验
python -m fiscal_harness serve            # 演示网页 http://127.0.0.1:8765
```

实际结果见 `docs/acceptance/report.md`。

## 3. 请重点核查

**赛题硬约束**

1. **不得用关键词或固定分支代替模型理解。** `tools.mentioned_months` 和 `_norm` 只用于核对出处，不用来判断意图。请判断这个边界是否站得住，以及 `harness.run_turn` 中有没有隐藏的场景分支。
2. **已停用的规则不能作为依据。** 规则选择逻辑在 `knowledge.resolve`。请检查生效日期、停用状态、多版本、不同企业等边界情况。
3. **无权限或查询失败不能回答 0。** 见 `tools._require`、`switch_company`，以及 `summarize_month` 返回中的 `note`。
4. **只能修改用途和备注；保存后必须回读；不能重复修改。** 见 `tools.update_invoice`、`verify_operation`，以及幂等键的构成。
5. **新开对话不代表获得修改授权。** 见 `update_invoice` 中 `user_quote` 与修改内容的出处校验。
6. **运行记录要能看到本轮模型输入，且不能记录密钥。** 见 `runlog._redact`；另外，删除记忆后会对运行记录做 `_scrub_runs` 清理。

**隔离**

7. 用户、企业、会话三个维度的隔离：`context.history` 按会话和企业过滤；任务表对未完成任务有唯一索引；偏好的范围通过 `memory._owned` 控制。
8. 同一个会话里切换企业后，旧企业的对话、待办和月份会不会混进来？

**一致性与恢复**

9. `update_invoice` 在事务里先写入、再登记 pending 操作并提交，随后回读。请检查中途失败时各种状态的组合，以及 `recover_pending` 的处理是否正确。
10. 规则变化提醒（`context.rule_drift`）和长对话摘要（`context._digest`）：摘要中的事实是否都有出处，会不会把过时信息当成当前数据。

**评测与证据**

11. `evals/cases.py` 的断言能否真正区分对错，有没有过宽或误判的可能。
12. `evals/experiment.py` 的评分方式，特别是 X7 题“指出冲突也算可接受”这一判定是否合理，结论有没有超出样本能支持的范围。

## 4. 已知限制（不必重复报告）

- 网页没有登录，用户由页面直接选择。这和材料中“由演示入口选定用户”的设定一致。
- 程序中断后只做回读核对，不会自动重新提交。
- SQLite 是单机方案，只考虑单人演示，没有处理并发写入。
- 长对话摘要由程序按规则生成，没有调用模型做语义总结。
