# 2026-11-01 评审：问题与修复对照

- 修复所在分支：`fix/review-20261101`（基于 `fix/review-20261030`）
- 第一批修复：`e6cbc03`（作用域记忆删除、备选规则引用写法）
- 第二批修复：复查 `e6cbc03` 时发现的 4 项遗留问题，见下文“复查补充”
- 回归测试：`tests/test_review_20261101.py`（23 项）

## 第一批（e6cbc03）

| # | 问题 | 处理 | 回归测试 |
|---|---|---|---|
| 1 | 删除偏好时对全库运行记录做文本替换：会改到其他用户、其他企业的记录；数字偏好会把 `1000.00`、金额字段一起截断 | `_scrub_runs` 只清理偏好所有者的运行（本企业偏好只清本企业，以及曾选用这条记忆的跨企业运行）；`scrub_memory_text` 只改自然语言字段，数字偏好按边界匹配 | `test_numeric_preference_keeps_json_and_business_values`、`test_forgetting_cannot_change_another_user_or_company`、`test_global_preference_cleans_only_its_owner_across_companies` |
| 2 | 同一轮删除后，下一步模型输入（system、历史、工具结果）仍带有旧偏好 | 删除后清理本轮 `messages` 与本批工具结果，并重建 system；切换企业时不误删另一企业的同名有效偏好 | `test_deleted_preference_not_in_next_model_input_or_new_log`、`test_switch_after_forget_preserves_other_company_active_preference` |
| 3 | 引用核对只认“《规则名》x.x 版”，`R002 v1.0`、`（版本1.0）`、省略“版”等写法会绕过 | 规则名与编号两种引用都核对，兼容 v 前缀、括号、“版本”与省略“版” | `test_alternate_bad_citation_is_held`（7 种写法）、`test_alternate_good_citation_uses_tool_evidence` |

## 复查补充

| # | 问题 | 复现 | 处理 | 回归测试 |
|---|---|---|---|---|
| 4 | 删除那一轮模型若在回复里复述原文（“已删除偏好‘称呼用户为王会计’”），原文仍留在 `runs.final_reply`、最后一步 `llm_output` 和 `messages` 表，下一轮会作为历史再次进入模型输入 | 修复前探针：运行记录与对话消息中均可找到原文 | `run_turn` 累计本轮删除的偏好，结束时清理回复，并用 `tools.scrub_run` 清理本轮全部步骤、`runs` 首尾文本和对话消息 | `test_reply_echoing_deleted_preference_is_scrubbed` |
| 5 | `_scrub_runs` 只改 `run_steps`，较早运行的 `runs.user_message / final_reply` 与对话消息仍有原文 | 早一轮回复写了“以后回答只用中文”，删除后下一轮历史里仍有 | 对同一批运行一并清理 `runs` 首尾文本与 `messages`；本轮消息在结束时才改，避免同轮后续工具按原话核对授权时失败 | `test_forgetting_scrubs_earlier_history_and_run_text` |
| 6 | 新的编号、书名号引用规则把金额和日期当成版本号：`R002 1000.00元`、`R002 2026.09 起`、`《规则》 1000.00 元` 都记为错误引用，正确回答会被扣下（needs_review） | 修复前 3 种写法均被判为异常 | 带“版 / 版本 / v”标记的照常核对；没有标记的数字只有主版本不超过 2 位、后面不跟“元、%、万、年、月、日”等单位时才算版本号 | `test_amounts_and_dates_are_not_versions`（4 种写法）、`test_adjacent_citations_are_both_checked` |
| 7 | 同一轮 `update_memory` 成功后，后续步骤的 system 仍列出旧内容 | 更正后下一次模型输入同时出现新旧两条 | 更正偏好后与删除一样重建 system | `test_update_memory_refreshes_system_within_turn` |

另外修正：删除后在同一轮里切换企业时，重建的历史按本轮累计删除的偏好清理（原先只用当前批次的删除列表）。

## 验证

- `python -m pytest -q`：**322 passed**（修复前 314；新增测试在修复前代码上有 6 项失败）
- 本轮改动涉及对话主循环（引用核对、记忆清理），真实模型评测沿用 114/114（`evals/results/20261028-183314/`），**未重新运行**，需要有 API Key 的环境补跑 `python -m evals.run --repeat 3`
