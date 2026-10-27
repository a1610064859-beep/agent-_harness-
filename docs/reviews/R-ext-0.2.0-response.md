# v0.2.0 外部评审：问题与修复对照

- 评审原文：`E:\claude\inbox\fiscal-agent-harness-0.2.0-review\REVIEW.md`（2026-10-26，对象为 0.2.0 提交包），探针脚本 `review_probes.py`
- 修复所在分支：`feat/invoice-file-import`
- 每条复现路径都转成了回归测试：`tests/test_review_regressions.py`。在修复前的提交 `de91956` 上运行，18 项中 16 项失败；修复后全部通过。另外 2 项是正常路径，用来确认修复没有误伤

## 修复前探针结果（评审方提供）

| 探针 | 修复前 |
|---|---|
| wrong_invoice | 要求改 F003，实际写入 F002，`verified=true` |
| explicit_no_save | “只是讨论，先别保存”仍然写入 |
| cross_company_replay | 甲企业的原话写入了乙企业 F002；切换后仍沿用甲企业的月份 |
| revert_A_B_A | 第三次被当作重复，库内停在 B，操作数 2 |
| same_turn_after_switch | 切换后的模型输入仍是“当前企业：C001”，并带有甲企业历史 |
| redaction | `runs.final_reply`、`messages`、导出记录中仍有密钥原文 |
| malformed_tool_argument | `AttributeError`，运行记录停在 `running` |
| web_permission | U002 无权访问乙企业，`/api/invoices` 仍返回 10 张发票 |

## 逐条处理

| # | 问题 | 处理 | 回归测试 |
|---|---|---|---|
| 1 [P1] | 授权出处没有绑定企业、发票和本次操作 | `tools.py` `_authorize_write`：出处只认本会话中**当前企业**最近连续的一段用户消息（本轮消息总算在内）；引文必须出自**一条**来源消息；只能改该消息点名的发票，没点名时取之前最近一条点名的消息；字段值必须出现在来源中；来源原话明确说“先别保存”时拒绝。月份出处也改按当前企业的这一段取 | `test_cannot_write_an_invoice_the_user_did_not_name`、`test_explicit_no_save_is_not_authorization`、`test_quote_from_before_company_switch_is_void`、`test_follow_up_save_in_same_session_uses_the_named_invoice`、`test_switch_message_itself_counts_for_new_company` |
| 2 [P1] | 网页数据接口绕过权限 | `web.py` 的所有数据接口改为只接收 `session_id`，用户与企业取自会话，不信任请求自报；查看要求非“无权访问”，导入发票和改规则要求“可查看并修改”；运行记录只能看本会话的。页面在无权访问时不请求、不渲染数据表 | `test_web_data_endpoints_enforce_session_permission`、`test_web_runs_are_limited_to_own_session` |
| 3 [P2] | 内容哈希做会话级幂等键，阻止 A→B→A | 幂等键加入来源消息 id：同一句原话的重试才算重复，新的一句话就是新的操作。重复时返回 `current_matches_request` | `test_revert_a_b_a_is_a_new_operation`；评测 S5（同句重发）仍为一次写入 |
| 4 [P2] | 同一轮切换企业后仍用旧上下文 | `harness.py`：切换成功后按新企业重建系统输入与历史；同一批次中排在切换之后的调用不执行，返回 `SKIPPED_AFTER_SWITCH`，由模型按新企业重新决定 | `test_same_turn_switch_rebuilds_context`（同时检查 tool_call 与 tool 结果一一对应） |
| 5 [P2] | 脱敏只覆盖步骤表 | `runlog.redact` 统一用于 `runs.user_message`、`final_reply`、`selected`、`anomalies`、`messages.content`；`runlog.load` 导出时再过滤一次，旧库里已有的记录也能覆盖 | `test_secret_redacted_everywhere`（检查导出、runs、messages、run_steps 四处） |
| 6 [P2] | 工具参数未做类型校验，异常中断整轮 | `dispatch` 按工具 schema 校验多余字段、必填项、类型、数组元素和枚举，返回可纠正的 `BAD_ARG`；可选参数传 null 视同未传。运行循环对非预期异常记 `error` 步骤，并以 `status=error` 收尾；`SimulatedCrash` 仍照常抛出，保留故障演示 | `test_malformed_tool_argument_returns_bad_arg`、`test_schema_validation`、`test_null_optional_argument_is_ignored`、`test_unexpected_exception_closes_the_run` |
| 交付 | 提交包缺 `scripts/package.py` | 从 `EXCLUDE` 中移除，提交包会带上该脚本 | — |
| 评测口径 | R4 的规则引用异常计为通过 | 暂不改通过条件；`evals.run` 在每行和汇总中单独列出异常条数（`summary.json` 的 `runs_with_anomalies`、`anomaly_total`）。是否把异常判为失败，需要定口径 | — |

## 仍然存在的限制

- 一句话里同时点名多张发票、多种用途时（如“F002 填 A，F003 填 B”），程序只核对发票和内容都出现在这句话里，不核对两者如何对应。
- “先别保存”的守卫只认几种常见说法，其他否定说法仍由模型理解。这条守卫只会拒绝，不会放行。
- 网页没有登录：会话号相当于访问凭证，适合本机演示，不适合公网部署。
