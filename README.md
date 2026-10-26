# 财税 Agent Harness（赛题二 · 初赛作品）

一个能查询财税资料、补充发票用途，并在多轮对话中继续处理任务的 AI 助手。
Harness 把模型、记忆、规则知识库和业务工具连在一起：**模型负责理解与决策，程序负责计算、校验、保存与记录**。

所有企业、用户、数据和规则均来自主办方材料包 v1.1.0 的比赛模拟内容。

## 快速开始

需要 Python 3.10 及以上版本。

**Windows**

```bat
scripts\setup.bat        :: 建虚拟环境、装依赖、生成 .env、导入示例数据
:: 在 .env 里填 LLM_API_KEY（DeepSeek 或其他 OpenAI 兼容平台；主办方统一 Key 同样填这里）
scripts\start-web.bat    :: 打开演示网页 http://127.0.0.1:8765
scripts\chat.bat --user U001 --company C001   :: 或在命令行对话
```

**Linux / macOS**

```bash
scripts/setup.sh
.venv/bin/python -m fiscal_harness serve          # 网页
.venv/bin/python -m fiscal_harness chat --user U001 --company C001
```

如需恢复为初始资料（会清空保存的修改、记忆、任务与运行记录），运行 `scripts\reset.bat`，或执行 `python -m fiscal_harness init --reset`。

## 模型配置（`.env`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_API_KEY` | 模型密钥，只从环境变量读取，不会写进运行记录 | — |
| `LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `LLM_MODEL` | 模型名 | `deepseek-flash` |
| `LLM_EFFORT` | 可选：推理强度（low/high/max） | 不传 |
| `FH_TODAY` | 业务日期，固定后可复现演示 | 当天 |

换用主办方统一提供的 Key 时，只需修改上面三项，代码不用动。

## 命令行一览（`python -m fiscal_harness …`）

| 命令 | 作用 |
|---|---|
| `init [--reset]` | 导入《示例数据》与《业务规则》 |
| `chat` / `ask "…"` | 交互对话，或单轮提问。对话中可用 `/new`、`/company C002`、`/run`、`/memory` |
| `rules list / show R002 / resolve --company C001 --month 2026-09` | 查看规则版本，以及某企业某月适用的版本 |
| `rules search --company C001 -q "用途"` | 在适用规则中检索条款 |
| `rules import --file X.docx/.json` | 导入规则 |
| `rules update R003 2.0 --body-file new.txt --effective-from 2026-10-01` | 更新规则（新增版本） |
| `rules disable R003 2.0` / `rules enable …` | 停用或启用某个版本 |
| `memory list / edit M-xx --content … / delete M-xx --user U001 --company C001` | 查看、更正、删除记忆 |
| `runs list` / `runs show R-xxxx [--full] [--json]` | 运行记录：完整模型输入、选用的记忆与规则、工具调用及结果 |
| `data invoices --company C001 --month 2026-09` / `data ops` | 核对库内当前数据，以及每次写入的前后对比 |

## 验证

```bash
python -m pytest -q          # 36 项确定性测试：标准答案、权限、隔离、幂等、回读、记忆、规则版本，不调用模型
python -m evals.run --repeat 3   # 24 个多轮案例用真实模型运行，结果写入 evals/results/<时间>/
```

最近一次真实评测（2026-10-26，deepseek-flash）：**72/72 通过**（24 个案例 × 3 次），明细见 `evals/results/20261026-082634/`。评测结果只记录实际运行得到的数据。

## 目录

```text
fiscal_harness/
  harness.py    统一运行循环（三个场景共用）
  context.py    每轮模型输入：会话绑定、任务记忆、长期偏好、规则目录、最近对话
  tools.py      工具与守卫：范围注入、权限、月份出处、修改授权、幂等写入、回读核验
  knowledge.py  规则导入、检索、版本解析、更新与停用
  memory.py     任务记忆与待办账本、长期偏好
  runlog.py     运行记录（过滤密钥）
  llm.py        OpenAI 兼容适配器；测试用脚本化模型
  web.py        演示网页 API
web/index.html  演示网页（单文件，不需要构建）
data/source/    官方材料原件（只在导入时读取）
tests/          确定性测试
evals/          多轮评测案例与运行器
docs/           规划、设计说明、审查记录
```

设计说明见 [docs/design.md](docs/design.md)。
