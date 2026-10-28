# 2026-10-28 扩展测试：问题与修复对照

- 测试报告：`E:\claude\inbox\fiscal-harness-test-20261028\测试报告.md`（46 个真实模型案例、59 轮，另有工具层与导入探测）
- 被测版本即 `main`（8387a04）；修复在其上进行，分支 `worktree-fix-extended-test-findings`
- 复现路径转成回归测试：`tests/test_review_20261028.py`；评测新增 W1–W3 三个多轮案例

## 修复前后（报告中的工具层探测，同一脚本分别在 main 与本分支运行）

| 探测 | main | 本分支 |
|---|---|---|
| “可能是寄合同快递，还没核实，先帮我看看” | 写入 | NEEDS_CONFIRMATION，未写 |
| “如果经理同意，再…现在不要操作” | 写入 | NOT_AUTHORIZED，未写 |
| “用途不要填快递…用途不要改” | 写入 | NOT_AUTHORIZED，未写 |
| “我只是举个例子…别实际修改” | 写入 | NOT_AUTHORIZED，未写 |
| “2026年8月的F003”（实为 9 月 16 日） | 写入 | INVOICE_MONTH_MISMATCH，未写 |
| “F002和F003分别填打印纸和合同邮寄” → F003=打印纸 | 写入 | VALUE_NOT_FOR_TARGET，未写 |
| “…请分别保存”（正常请求） | NOT_AUTHORIZED，未写 | 写入 |
| U002 读乙企业 `get_rule` / `search_rules` | 成功 | FORBIDDEN |
| 用户只说“今天回答简短”，保存跨企业“必须用英语” | 成功 | NOT_AUTHORIZED |
| 用户只查询时删除偏好 | 成功 | NOT_AUTHORIZED |
| 11 位发票代码 | imported | rejected |
| 缺大写金额 | imported | rejected |

跨企业停用规则（P0）由网页接口测试覆盖：修复前返回 `ok`，修复后 403 且库内状态不变。

## 逐条处理

| # | 问题 | 处理 | 回归测试 |
|---|---|---|---|
| 1 P0 | 规则接口可跨企业停用规则 | `web.py` `require_rule_write`：停用/启用前从库里取该版本的适用企业，新增版本取本次指定的企业与该规则此前适用的企业，用户须对每一家都有修改权限；不只看会话企业。规则不存在返回 404 | `test_rule_status_checks_the_rules_own_companies`、`test_shared_rule_needs_write_on_every_company`、`test_new_rule_version_checks_target_companies`、`test_missing_rule_is_404_not_403` |
| 2 P0 | 发票写入授权缺少程序兜底 | 新增 `intent.py`（否定、条件、不确定、举例的识别）与 `pending_changes` 表。原话有否定时拒绝；有条件、不确定、举例、没有修改要求，或多张发票对应不清时，程序记下待确认修改（绑定会话、用户、企业、发票、开票日期、字段与完整值）并返回 NEEDS_CONFIRMATION；只有紧接着的下一句用户原话明确确认，且确认的正是记下的那项，才写入。隔一句再确认、确认句里带否定、确认别的值都不行。举例的话永远不能作为之后“保存吧”的依据 | `test_unclear_or_negated_requests_do_not_write`（6 种说法）、`test_confirmation_writes_exactly_the_proposed_change`、`test_confirmation_must_be_the_very_next_message`、`test_negated_confirmation_is_not_a_confirmation`、`test_example_is_never_a_basis_for_later_save`、`test_deferred_request_can_be_saved_later`、`test_value_negated_in_otherwise_clear_sentence` |
| 3 P1 | 月份与发票日期不符仍写入 | `_target_invoice`：原话说了月份时，发票开票月份必须在其中，否则 INVOICE_MONTH_MISMATCH；没说月份不限制 | `test_month_in_request_must_match_invoice_date`、`test_matching_or_unstated_month_still_saves`、`test_month_cross_year` |
| 4 P1 | 多张发票用途串写 | `_value_bound` 返回“对应 / 含糊 / 不对应”：同组多张发票时，“分别”句式按顺序一一对应（在保留标点的原话上拆值），“都/均”表示同一个值，其他说法记为含糊转确认；值前紧挨否定（“不是快递”）不算 | `test_respectively_binds_values_in_order`、`test_group_without_respectively_needs_confirmation`、`test_respectively_with_count_mismatch_needs_confirmation`、`test_same_value_for_all` |
| 5 P1 | 无权访问的企业仍能读规则 | 按现有约定（无权访问即不能查看该企业资料）：`search_rules`、`get_rule`、`list_rule_documents` 先检查读取权限；无权访问时模型输入不带该企业规则目录 | `test_no_access_company_rules_are_forbidden`、`test_readable_company_rules_still_listed` |
| 6 P1 | 记忆工具缺独立授权 | 新增、更正、删除都只认用户本轮原话：新增须有“记住/以后…”，内容与原话的二元组重合不少于 60%，跨企业须原话说明对所有企业适用；更正、删除须有对应要求，`update_memory`、`forget_memory` 新增必填 `user_quote`，目标须是原话说到编号、内容重合唯一最多、“刚才那条”指最近一条，或当前只有一条 | `test_remember_needs_explicit_request_and_grounded_content`、`test_cross_company_preference_needs_user_words`、`test_update_and_forget_need_a_request_this_turn`、`test_memory_target_must_be_the_one_user_meant`、`test_unnamed_memory_with_several_preferences_asks` |
| 7 P1 | 11 位发票代码被接受 | 代码按整串取出，在 `_validate` 统一核对只能是 10 或 12 位数字（测试样张除外）；号码也改为整串数字核对 | `test_invoice_code_must_be_10_or_12_digits`（9–13 位） |
| 8 P2 | “分别保存”误判为“别保存” | 否定识别排除“分别、区别、特别、级别、识别、类别”等词里的“别” | `test_respectively_save_is_not_negation`、`test_real_negations_still_block`；评测 W1 |
| 9 P2 | 跨企业顺序查询丢进度 | `harness.py`：本轮切换企业后重建输入时，把切换前已成功的查询与保存（标注企业、参数、结果）由程序写进系统输入，说明已完成、不要切回去重复，回复时分别注明企业；不能用于当前企业的操作 | `test_results_before_switch_are_carried_into_the_new_company` |
| 10 P2 | 规则引用缺来源 | 回复引用了本轮工具没有返回的规则版本时，程序请模型先取得条款或去掉引用，重答一次；仍不对则照旧记为异常。评测仍单独统计异常 | `test_citation_not_returned_by_tools_is_flagged`、`test_citation_retry_fetches_the_rule` |
| 11 P2 | 缺大写金额仍可导入 | 选择阻断：README 承诺“大写与小写一致、任何一项不符都不导入”，正式发票都印有大写金额，识别不到就无法完成这项核对，不导入并说明原因 | `test_missing_upper_amount_is_not_imported` |
| 12 P3 | “现在不要操作”时仍写任务记忆 | 未改行为，需要产品约定（见下）。已确认任务记忆不能作为修改授权 | `test_task_note_is_not_authorization` |

## 需要确认的产品约定

- **P3 任务记忆**：现在“不要操作”只约束发票写入，模型仍可把这件事记成待办（便于下次提醒）。如果约定“不要操作”指所有持久修改，可在 `update_task` 上套用同样的否定识别。
- **待确认流程**：表述清楚的“F003用途填X，保存”仍一步保存（演示与评测保持不变）；只有表述有风险时才多一轮确认。如希望所有写入都先确认，可把直接写入路径也改为生成待确认修改。

## 仍存在的限制

- 风险表述的识别仍是规则匹配。它只用于拒绝或要求确认，不用于放行；放行还要求发票、字段、值、月份都能由程序对应。漏识别的新说法会走直接写入路径，所以评测中仍需继续收集真实说法。
- 本轮未测真实 OCR、真实发票验真与生产部署（与测试报告一致）。

## 验证

- `python -m pytest -q`：**224 passed**（含 zxingcpp 环境；新增 `tests/test_review_20261028.py` 44 项）
- 误拦检查：评测与演示脚本中的 15 句正常保存、记忆请求，按正确调用在新守卫上重放，全部成功
- 真实模型评测（deepseek-flash，2026-10-28）：32 个案例 × 1 次 **32/32**，规则引用异常 0 条（`evals/results/20261028-045624/`）；新增的跨企业顺序查询 W4 × 3 次 **3/3**（`evals/results/20261028-045739/`）。未按 `--repeat 3` 重跑全部案例
