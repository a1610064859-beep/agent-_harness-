# f773ae7 复评：问题与修复对照

- 评审原文：`E:\claude\inbox\fiscal-agent-harness-f773ae7-review\REVIEW.md`（2026-10-27），探针脚本 `review_ocr_edges.py`
- 修复所在分支：`feat/invoicegen-integration`
- 2 条复现路径转成回归测试：`tests/test_review_f773ae7.py`（8 项）。修复前在本分支运行评审探针，2 条都复现

| 探针 | 修复前 | 修复后 |
|---|---|---|
| same_id_changed_tax_breakdown | `imported, duplicate` | `imported, conflict`，提示“金额 100.00 → 106.60；税额 13.00 → 6.40” |
| legacy_qr_accepts_tax_inclusive_total | 无问题，确认后导入 | “二维码中的金额 113.00 与识别出的不含税金额 100.00 不一致”，拒收 |

## 逐条处理

| # | 问题 | 处理 | 回归测试 |
|---|---|---|---|
| 1 [P2] | 同号票价税合计相同、金额与税额不同时误判为重复 | `invoice_files.py` `_differences`：库内票来自导入文件时，用 `invoice_sources.parsed_json` 比较全部票面业务字段（开票日期、购销方名称与税号、项目、金额、税额、价税合计）；票面备注、版式、文件名不比（同一张票的电子件与扫描件本来就会不同）。来自示例数据的票没有来源文件，只能比较库表字段。冲突提示写明哪些字段不同 | `test_same_total_different_tax_breakdown_is_conflict`（同批、分两次）、`test_identical_reimport_is_still_duplicate`、`test_sample_data_invoice_without_source_compares_table_fields` |
| 2 [P2] | 旧版票二维码也接受价税合计 | `_check_ocr`：号码不是 20 位的旧版票，二维码金额必须等于不含税金额（真实样本实测口径）；只有数电票接受不含税或价税合计任一一致，并提示“金额口径未经真实样本验证，请人工核对金额” | `test_legacy_qr_with_tax_inclusive_amount_is_rejected`、`test_digital_qr_accepts_either_amount_with_unverified_note`（两种口径）、`test_digital_qr_with_unrelated_amount_is_rejected` |

## 评审中的其他发现与处理

- **自检目录全是测试样张**：`ocr_check.py` 现在单独统计测试样张，全部跳过时说明“只能用来验证测试票被拒收，不能衡量识别准确率，请改用真实电子发票 PDF”。正常票面的 Standard 准确率与无缓存速度仍待测，不能由测试样张的跳过结果代替。
- **C 盘空间不足、MINERU_HOME**：`setup-ocr.bat` 默认把 MinerU 的模型、配置与缓存放在项目内 `.ocr\home`，并写入 `.env`；`.env` 已有 `MINERU_HOME` 时沿用，不会重复下载。
- **Windows Standard 档**：评审已在 RTX 3070 Ti Laptop 上装好并核验（模型完整、本地服务健康、`mode=managed`、`tier=standard`），真实识别一张 TEST 票后识别出 TEST 号码并默认拒收，整次约 64 秒（含识别、二维码与清理，非正常票面吞吐）。

## 验证

- `python -m pytest -q`：**161 passed**
- 本轮只改导入路径与脚本，未涉及对话；真实模型评测沿用 87/87（`evals/results/20261027-082932/`），未重新运行
