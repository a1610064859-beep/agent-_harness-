# ec2d61e 复评：问题与修复对照

- 评审原文：`E:\claude\inbox\fiscal-agent-harness-ec2d61e-review\REVIEW.md`（2026-10-26），探针脚本 `review_probes.py`
- 修复所在分支：`feat/invoicegen-integration`
- 评审的 5 条复现路径全部转成回归测试：`tests/test_review_ec2d61e.py`（14 项，另含正常说法不被误拦的用例）。修复前在本分支运行评审探针，5 条全部复现；修复后全部按预期拒绝或正确判定

## 修复前探针结果（本分支复跑）

| 探针 | 修复前 | 修复后 |
|---|---|---|
| swapped_invoice_values | F002 写入了 F003 的用途，`ok=true` | `VALUE_NOT_FOR_TARGET`，未写入 |
| swapped_field_values | 用途与备注互换后写入，`ok=true` | `VALUE_NOT_FOR_TARGET`，未写入 |
| legacy_same_suffix_wrong_invoice | 点名 …901-12345678，却改了 …902-12345678 | 只认 …901；改 …902 返回 `INVOICE_NOT_REQUESTED` |
| same_batch_conflict | `imported, duplicate`，冲突被掩盖 | `imported, conflict` |
| bad_before_good_same_batch | `rejected, duplicate`，有效原票 0 张入库 | `rejected, imported`，入库 1 张 |

## 逐条处理

| # | 问题 | 处理 | 回归测试 |
|---|---|---|---|
| 1 [P1] | 完整编号已点名，仍可改另一张同号码的旧版发票 | `tools.py` `_mentions`：编号必须完整出现，前后不紧挨字母或数字；先匹配完整编号，旧版短号码只在不落在完整编号里、且当前企业中唯一时才算点名；同号码有多张时返回 `INVOICE_AMBIGUOUS`，请模型向用户确认发票代码 | `test_full_legacy_id_does_not_match_another_code`、`test_ambiguous_short_legacy_number_asks_for_code`、`test_unique_short_legacy_number_still_works`、`test_invoice_id_must_stand_alone` |
| 2 [P1] | 一句话多张票、多个字段时，值可以互换 | `tools.py` `_value_bound`：原话按点名的发票切段（只隔“和、与、顿号”等的点名合为一段，支持“F002和F003的用途都填……”）；段内取值前最近的字段名（“用途和备注都填……”合为一组）；值前没有字段名时按用途理解（如“F004是出差住宿”）。值出现在原话里、但不属于这张票或这个字段时返回 `VALUE_NOT_FOR_TARGET`。这条守卫只会拒绝，不会据此决定写入 | `test_values_cannot_be_swapped_between_invoices`、`test_values_cannot_be_swapped_between_fields`、`test_shared_values_are_allowed`（3 种说法）、`test_unlabeled_value_means_purpose`、`test_follow_up_without_invoice_name_binds_to_earlier_named_invoice` |
| 3 [P2] | 批量导入把同号直接判为重复 | `invoice_files.py` 去掉只记编号的集合：本批前面暂存的票在同一事务里查得到，同号一律按内容判重复或冲突；被拒的文件不入库，不占号。冲突提示改为“库内或本批前面的文件中已有同号发票” | `test_same_batch_same_number_different_content_is_conflict`、`test_same_batch_identical_file_is_duplicate`、`test_rejected_file_does_not_block_valid_one`（预览与提交两种） |

## 修复后的验证

- `python -m pytest -q`：**134 passed**
- 真实模型多轮评测（本次修改后重新运行，不沿用旧结果）：`python -m evals.run --repeat 3` → **87/87**（29 个案例 × 3 次，deepseek-flash，`evals/results/20261027-082932/`）。新守卫没有误拦评测和演示中的正常说法
- 规则引用异常 7 条，涉及 4 次运行：R4 ×2、P3 ×2。模型在资料不足或无权修改时，引用了系统输入规则目录里的版本，而该轮工具没有返回这些版本。与本次修复无关，属于上轮“评测口径”待定事项，仍不计入通过条件

## 仍然存在的限制

- 字段归属按“值前最近的字段名”判断。像“打印复印纸作为用途”这样字段名在值之后的说法，会被当成没写字段名，按用途处理；“市场部作为备注”会被拒绝，需要模型向用户确认。只会多拒，不会错写。
- 跨两条消息补充内容时（先点名、后给值），值所在的那句没有点名发票，整句都算作之前点名的发票。若之前一句点名了多张票，后一句的值不能区分属于哪张。
- 评审建议的设计说明和验收文档的版本、证据日期已同步更新；随包 72/72 的旧结果已标明为旧版本。
