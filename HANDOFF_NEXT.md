# YPCX 交接文档（2026-11-26）

项目目录：`/Users/xiaoxu/Desktop/YPCX`
当前 HEAD：`4bc4135 docs: add free Docker alternatives for macOS`
上一轮交接：`HANDOFF_NEXT.md` 的 2026-11-23 版本（已在 git 历史里）
本文档面向**新对话**，重点交接“适应症获批时间”优化。

> 一句话状态：适应症简称/分型已经切到千问 LLM 并完成全库回填；
> 获批时间仍是旧规则（54/90 为空、12 条等于上市月、只有 22 条有来源），
> 下一步就是把它做成“证据 + 引用 + 可留空”的 LLM 裁决。

---

## 1. 环境与启动

```bash
cd /Users/xiaoxu/Desktop/YPCX

# 后端
./venv/bin/uvicorn main:app --reload --port 8000

# 前端：直接打开 index.html（API 指向 127.0.0.1:8000）
open index.html

# 测试 / 静态检查
./venv/bin/python -m pytest -q
./venv/bin/ruff check .
```

当前测试：**84 passed**；`ruff` 干净。

Docker（可选，未实际 build，机器没装 Docker）：

```bash
cp .env.example .env
mkdir -p docker-data snapshots reports
cp drug_agent.db docker-data/drug_agent.db
docker compose up -d --build
# 或 Colima：brew install colima docker docker-compose && colima start
```

详见 `docker/README.md`。

---

## 2. 当前数据状态

```text
总行数 90：APPROVED 85，DRAFT 5
DRAFT：id 97–101（泰它西普 5 条）
适应症获批时间为空：54 行
有获批来源 URL：22 行
获批时间 == 药品上市时间：12 行
最近日期：id 93 美泊利珠 鼻窦炎 2026-11-14
```

按药品看空值最多的：

| 药品 | 空值行数 |
|---|---|
| 替雷利珠单抗注射液 | 10 |
| 注射用卡瑞利珠单抗 | 8 |
| 信迪利单抗注射液 | 5 |
| 斯鲁利单抗注射液 | 5 |
| 舒格利单抗注射液 | 4 |
| 注射用泰它西普 | 4 |

备份（都在项目根目录，`*.db` 已被 gitignore）：

```text
drug_agent.backup_20261126_210621_before_llm_indications.db   # 全库简称/分型回填前
drug_agent.backup_20261126_211121_before_llm_indications.db   # 23/94 规则修正前
drug_agent.backup_20261126_201202_before_subtype_cleanup.db
drug_agent.backup_20261126_200235_before_telitacicept.db
drug_agent.backup_20261123_163820_before_drug_insurance.db    # 103 行旧库
```

> 注意：103 → 90 是启动时 `_cleanup_drafts()` 清掉了超过 24 小时的旧 DRAFT，
> 不是回填删除；APPROVED 一直是 85 行。

---

## 3. 这一轮改了什么（简称/分型）

### 3.1 大模型归一

- 新增 `indication_llm.py`：
  - 输入同一药品的全部 `indication_full`；
  - 输出 `short`（最上层大类）+ `subtype`（具体病名全称）；
  - 规则：`肺癌 / 非小细胞肺癌`、`膀胱癌 / 肌层浸润性膀胱癌`、
    `重症肌无力 / 全身型重症肌无力`、`IgA肾病 / 原发性免疫球蛋白A肾病`；
  - 没有更细病名时：`short = subtype = 疾病全称`；
  - 例外：哮喘保留“重度/嗜酸性”表型；实体瘤保留 MSI-H/dMMR 分子分型。
- `agent._fetch_new_drug_impl` 在构建完 `all_extracted_products` 后调用
  `indication_llm.normalize_products()`；`flatten_to_rows` 优先使用
  `_llm_normalized` 的 short/subtype。

### 3.2 开关（在 `.env`，未入库）

```env
INDICATION_MODE=llm
INDICATION_DICTS=off
INSURANCE_ALIASES=on
LLM_INDICATION_THINKING=0
LLM_OVERWRITE_APPROVED=1
```

- `INDICATION_DICTS=off`：隐藏 `_CONCISE_RULES`、`ALIASES_DICT`、
  `_GUIDE_DISEASE_HINTS`、`_OVERSEAS_DISEASE_HINTS`、`_GENERIC_DISEASE_NAMES`；
- `INSURANCE_ALIASES=on`：医保病名别名暂时保留，避免医保状态大面积变“未纳入”；
- `nhi_dictionary.json` 始终保留（医保目录权威来源）；
- `LLM_OVERWRITE_APPROVED=1`：允许 LLM 结果覆盖 APPROVED 的 short/subtype；
- 改 `.env` 后必须**重启服务**。

### 3.3 全库回填结果

- 90/90 匹配，74 行变更；
- 费用约 ¥0.039；
- 2 条规则修正（id 23 MSI-H、id 94 哮喘）费用 ¥0.0012；
- 写库前后行数 90 → 90，状态不变，只改 short/subtype；
- 脚本：
  - `scripts/shadow_indications.py`：20 条 shadow，只读；
  - `scripts/shadow_production.py`：副本库跑生产路径，验证不新增行；
  - `scripts/apply_llm_indications.py`：`--ids` / `--all`，默认 dry-run，`--apply` 写库。

### 3.4 其他修复

- `_short` 修了 SLE（系统性红斑狼疮）、IgAN、COPD 被“疾病”覆盖的问题；
- `_save_rows` 先按“通用名+厂家+剂型+规格+全文”匹配已有行，简称变了不会新建重复行；
- `llm_client` 关闭 SDK 自动重试（`max_retries=0`），避免看起来“卡死”；
- 测试新增 `tests/conftest.py`，固定测试用 regex/字典开关，避免 `.env` 影响离线用例。

### 3.5 Docker

`Dockerfile`、`docker-compose.yml`、`.dockerignore`、`docker/README.md`；
api 8000，可选 market 8001 / web 8080。未实际 build（本机无 Docker）。

---

## 4. 已知问题 / 不要踩

1. **不要手工改库**：用户明确要求“修规则，不手改数据”；先改 prompt/校验，重跑 LLM。
2. **DRAFT 24 小时会被清掉**：`main._cleanup_drafts()` 每次启动删除
   `updated_at` 超过 24h 的 DRAFT；重要待审核数据当天处理。
3. **`INDICATION_DICTS=off` 的副作用**：指南归属、海外去重、泛称排除会变弱；
   目前靠全文匹配兜底，后续要么接 LLM 去重，要么把该开关调回 on。
4. **`LLM_OVERWRITE_APPROVED=1` 仍在生产**：重新查询某个药时，LLM 结果可覆盖
   APPROVED 的 short/subtype；如果不希望，回填完成后改回 0。
5. **部分分型仍偏粗**：id 24 默克尔细胞癌、83 子宫内膜癌、92 COPD、96 高嗜酸性
   粒细胞综合征仍是“全称/全称”，没有更细病名；要细化就在 `indication_llm.py`
   里继续加规则，不要改库。
6. **Docker 未验证**：本机没有 Docker；compose YAML 已用 PyYAML 解析通过。
7. **不要在查询途中改 `.py`**：uvicorn reload 会断 SSE。

---

## 5. 下一步：优化适应症获批时间

### 5.1 现状

| 项 | 数值 |
|---|---|
| 空值 | 54/90 |
| 有来源 URL | 22/90 |
| 获批时间 == 上市时间 | 12 行 |
| 空值最多的药 | 替雷利珠 10、卡瑞利珠 8、信迪利 5、斯鲁利 5、舒格利 4、泰它西普 4 |

当前实现：

- `agent._fill_missing_approval_dates()`（agent.py:450）：
  - 每药最多补 2 条；
  - 只补“纯网搜产品”或全文含“新增/新适应症”的行；
  - 调用 `approval_dates.resolve_indication_approval_date()`。
- `approval_dates.py`：
  - 日期必须与病名同句；
  - 一线/二线/围术期/HER2/单药/鳞/非鳞等限定词必须一致；
  - 同一月份至少 **2 个独立域名**才采用；
  - “首次上市/首次获批”句里与上市同月的日期不算；
  - 单来源/打平 → 返回空。
- PPT：`indication_approval_times` 只按病名匹配，同名多行直接跳过。
- PDF：VLM prompt 明确“**不要输出获批时间**”，只解析首次上市时间；
  说明书正文通常没有分适应症日期。
- `main._clamp_indication_approval_date()`（main.py:572）：
  - 中国行早于上市日 → 留空；
  - 方案 A：没有可靠日期就留空，不用上市时间兜底。
- `indication_approval_source`：22 行有 URL，前端“源”链接。

### 5.2 为什么现有规则不够

- 单来源的 NMPA 公告/药企新闻被“双域名”规则全部丢掉；
- 搜索片段里的日期常与病名不在同一句，或句子被截断；
- PPT 的同名多行（不同一线/联合方案）无法区分，直接跳过；
- 12 条“等于上市月”可能是首个适应症的真实日期，也可能是旧兜底值，没人复核；
- 54 条空值里，有些确实“未公开”，有些是搜索没抓到。

### 5.3 建议方案：LLM 证据裁决（方案 A 不变）

新增 `approval_llm.py`，思路与 `indication_llm.py` 一致：

**输入（每药一批）**
- 药品通用名/商品名、中国上市时间；
- 每条适应症的 `id / full / short / 现有日期 / 现有来源`；
- PPT 的 `indication_approval_times`；
- NHSA 2026 申报 PDF 的相关段落（如有“批准时间/批准日期”）；
- 搜索命中的证据句（含 URL、发布日期、来源域名、authority）；
- 已保存的 `source_snapshot` 快照。

**输出**
```json
{"id": 3, "date": "2024-06-12", "precision": "day|month",
 "evidence_quote": "原文句子", "source_url": "https://...",
 "reason_code": "first_approval|new_indication|overseas|unknown",
 "confidence": 0.0}
```

**硬约束**
- 无证据 → `null`，不写库；
- `evidence_quote` 必须是输入原文子串（归一化空白后）；
- 中国行日期不得早于上市日、不得晚于今天；
- 单来源网搜 → 只标“待核实”，不自动写；
- 同名多行无法区分一线/联合方案 → `null`；
- 海外行单独口径，不要用中国上市日 clamp；
- 写入时同时写 `indication_approval_source`。

**落地顺序**
1. **只读盘点**：`scripts/approval_inventory.py`
   - 统计空值/等于上市月/有来源/疑似旧兜底；
   - 按药输出样例，人工确认口径。
2. **Shadow**：`scripts/shadow_approval_dates.py`
   - 选 20–30 条（替雷利珠、卡瑞利珠、信迪利、斯鲁利、舒格利、泰它西普优先）；
   - 只出 `old → new + evidence + source`，不写库；
   - 人工抽检 10 条。
3. **DRAFT 写库**：`scripts/apply_approval_dates.py --ids ...`
   - 备份 → 只改 `indication_approval_date/source`；
   - 不动 status、不动其他字段。
4. **APPROVED 写库**：加 `APPROVAL_OVERWRITE_APPROVED=1`，同样先出 before/after，再写。
5. **UI**：`indication_approval_date` 为空显示“未公开”，有值但单来源显示“待核实”；来源链接悬浮显示 URL。

**模型/成本**
- 批量：`qwen3.8-flash` 非思考，约 ¥0.005–0.01/药；
- 硬样本：`thinking=True`，约 ¥0.03/药；
- 全库 25 个通用名跑一遍 < ¥1。

**验收**
- `pytest` 84+ 通过，`ruff` 干净；
- shadow 20–30 条，人工确认证据句/来源；
- 写库前后行数不变，只改日期/来源两列；
- 空值仍允许存在，不能为了“填满”而编日期。

---

## 6. 文件索引

```text
main.py
  _split_short_subtype           简 称/分型（LLM 行会被跳过）
  _save_rows                     按全文匹配已有行；LLM 覆盖 APPROVED
  _clamp_indication_approval_date 日期早于上市 → 留空
  flatten_to_rows                行组装；优先 LLM short/subtype
  /stats/cost /stats/usage       成本/用量
agent.py
  _fetch_new_drug_impl           主流程；PPT 时间、PDF 抽取
  _fill_missing_approval_dates   当前补日期（每药 2 条）
  _normalize_disease_with_dict   INDICATION_DICTS=off 时直接返回原值
indication_llm.py                LLM 简称/分型（本轮新增）
approval_dates.py                网搜日期规则（下一步要改/替换）
skill_vision_parser.py           PDF/PPT 解析；PDF 禁止输出日期
llm_client.py                    统一 LLM 调用/thinking/usage
usage_store.py                   llm_usage/search_usage
source_snapshot.py               搜索/LLM/文档快照
scripts/
  shadow_indications.py          简称/分型 20 条 shadow
  shadow_production.py           副本库生产路径 shadow
  apply_llm_indications.py       简称/分型回填（--ids/--all/--apply）
  p1_preview.py                  旧简称回填预演
Dockerfile / docker-compose.yml / docker/README.md
```

---

## 7. 验证 / 回滚

```bash
cd /Users/xiaoxu/Desktop/YPCX
./venv/bin/python -m pytest -q
./venv/bin/ruff check .
```

回滚简称/分型（先停服务）：

```bash
./venv/bin/python -c "import sqlite3; s=sqlite3.connect('drug_agent.backup_20261126_210621_before_llm_indications.db'); d=sqlite3.connect('drug_agent.db'); s.backup(d)"
```

回滚 23/94：

```bash
./venv/bin/python -c "import sqlite3; s=sqlite3.connect('drug_agent.backup_20261126_211121_before_llm_indications.db'); d=sqlite3.connect('drug_agent.db'); s.backup(d)"
```

代码回滚：`git revert <commit>`；当前提交：

```text
4bc4135 docs: add free Docker alternatives for macOS
a4ddb75 feat: add asthma/MSI-H subtype rules and Docker deployment
0befa1e feat: normalize indications with LLM and add backfill tooling
0b5aba1 feat: add LLM usage logging, replay snapshots, and model routing
```

---

## 8. 不要做的事

- 不要手工改 `drugs.indication_short/subtype`；要改就改 `indication_llm.py` 的规则/示例，重跑 LLM。
- 不要用上市时间/医保年份兜底获批时间。
- 不要在没有原文证据时写日期；空是合法结果。
- 不要把 VPN/代理/密钥写进 `.env.example` 或提交 `.env`。
- 不要在查询途中改 `.py`（reload 断 SSE）。
- 不要直接 `git push` 到 main；先看 diff 和测试。
- 不要删除 `drug_agent.backup_*.db`，回滚要用。

---

## 9. 新对话开场建议

把下面这段直接发给新对话：

> 读 `HANDOFF_NEXT.md`。当前简称/分型已切千问 LLM 并全库回填；
> 下一步做“适应症获批时间”优化。先只读盘点 54 条空值和 12 条等于上市月的行，
> 设计 LLM 证据裁决（必须带原文引用、单来源只标待核实），先 shadow 不写库。
> 不要手工改数据库，改规则/脚本。
