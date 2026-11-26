# YPCX 交接文档（2026-11-26）

项目目录：`/Users/xiaoxu/Desktop/YPCX`
远端：`git@github.com:heidi-lu/YPCX.git`，`main` 已同步（本轮 8 个 commit 已 push）
当前提交：以 `git log -1` 为准

> 当前状态：简称/分型已切千问 LLM 并完成全库回填；
> 获批时间仍是旧规则（54/90 为空、12 条等于上市月、22 条有来源）；
> 下一步做“适应症获批时间”的 LLM 证据裁决。

---

## 1. 启动 / 测试

```bash
cd /Users/xiaoxu/Desktop/YPCX

# 后端
./venv/bin/uvicorn main:app --reload --port 8000
# 前端
open index.html

# 测试 / 静态检查
./venv/bin/python -m pytest -q     # 84 passed
./venv/bin/ruff check .
```

Docker（可选，本机未装 Docker，未实际 build）：

```bash
cp .env.example .env
mkdir -p docker-data snapshots reports
cp drug_agent.db docker-data/drug_agent.db
docker compose up -d --build
# Colima 免费替代见 docker/README.md
```

---

## 2. 当前数据

```text
总行数 90：APPROVED 85，DRAFT 5
DRAFT：id 97–101（泰它西普）
适应症获批时间为空：54 行
有获批来源 URL：22 行
获批时间 == 药品上市时间：12 行
```

空值最多的药：

| 药品 | 空值 |
|---|---|
| 替雷利珠单抗注射液 | 10 |
| 注射用卡瑞利珠单抗 | 8 |
| 信迪利单抗注射液 | 5 |
| 斯鲁利单抗注射液 | 5 |
| 舒格利单抗注射液 | 4 |
| 注射用泰它西普 | 4 |

唯一回滚备份：

```text
drug_agent.backup_before_llm_indications.db   # 2026-11-26 21:06，全库简称/分型回填前
```

> 103 → 90 是启动时 `_cleanup_drafts()` 清掉超过 24h 的旧 DRAFT，不是回填删除；
> APPROVED 一直是 85 行。

---

## 3. 代码流程（当前真实顺序）

### 3.1 官方建档（PDF / PPT）——优先级最高

1. `find_drug_files(drug_name)` 找 NHSA 材料，按通用名分组；
   每组取**年份最新的 PDF / PPT**。
2. PDF 本地解析 `_extract_pdf_key_info()`：
   - 封面药品名 `cover_drug_name`
   - 生产企业/上市许可持有人 `manufacturer`
   - 首次上市时间 `china_approval_date`（支持到日）
   - 注册分类、批准文号、全球首个上市国家/地区
   - 封面/正文商品名
3. PDF 正文 VLM `_extract_body_info()`：
   - `dosage_form`、`specification`
   - `indications`：`indication_short` / `indication_full` / `dosage_full` / `guidelines`
4. PPT 解析 `_extract_ppt_info()`：
   - 商品名（`_extract_brand_from_text`，跳过参照药）
   - `indication_approval_times`（病名 → 获批时间）
   - `guidelines`（指南名/推荐/级别）
5. **适应症以 PDF 正则为第一优先**：
   `extract_indications_from_pdf()` 从“说明书全部适应症”块按编号切分；
   只要切出结果，就用它覆盖 VLM 的 `indications`；
   VLM/PPT 的指南、用法用量、规格通过 `_inherit_pdf_indication_fields()` 继承过来。
   → 所以不是“VLM 优先”，而是“PDF 正则切分优先，VLM 补全文/用法/指南”。
6. PPT 获批时间只按病名匹配；同一病名对应多条适应症（不同一线/联合方案）时
   **直接跳过**，避免串日期（方案 A）。
7. **LLM 简称/分型归一**（`.env` 的 `INDICATION_MODE=llm`）：
   整药批量调用 `indication_llm.normalize_products()`，输出
   `short`（最上层大类）+ `subtype`（具体病名全称），打 `_llm_normalized` 标记。
   - 例：`肺癌 / 非小细胞肺癌`、`膀胱癌 / 肌层浸润性膀胱癌`、
     `重症肌无力 / 全身型重症肌无力`、`IgA肾病 / 原发性免疫球蛋白A肾病`
   - 无更细分型时：`short = subtype = 疾病全称`
   - 哮喘保留“重度/嗜酸性”表型；实体瘤保留 MSI-H/dMMR 分子分型

### 3.2 官方字段优先级

- PDF/PPT 是权威来源；`_save_rows` 对已有 APPROVED 行默认**只补空、不覆盖**；
- `LLM_OVERWRITE_APPROVED=1` 时，LLM 的 short/subtype 可以覆盖 APPROVED；
- 网搜只补官方没有的字段/行，不反向覆盖官方全文。

### 3.3 网搜（不是只有两块）

| 阶段 | 做什么 |
|---|---|
| `_fill_brand_from_metadata` | 通用名 + 厂家 + 上市时间 → 补本品商品名 |
| 品牌发现 | 搜索全部中文商品名，逐个校验是否属于该通用名 |
| 新适应症 | 只补最近 12 个月“新获批/新增”；官方已覆盖的丢掉 |
| 网搜上市时间 | 补 `china_approval_date` 为空的产品 |
| 缺失获批时间 | 对新适应症/网搜产品再搜一次；每药最多 2 条 |
| `_add_latest_generics` | 最近一年新获批仿制药/生物类似药，复制原研适应症 |
| FDA 海外 | 仅原研且非国产，补海外适应症/日期 |
| `_enrich_missing_brands` | 按适应症补商品名，厂家不同时新增产品 |
| `_enrich_missing_dosage` | 补缺失用法用量 |
| `_enrich_market_price` | 兔灵网最低市场价 |
| `_fill_missing_approval_dates` | 最后兜底补日期（每药最多 2 条） |

> 你原来的总结“网搜两部分”方向对，但实际还包含：
> 用法用量、市场价格、FDA 海外、仿制药；
> 缺失字段也不只商品名/获批时间，还有上市时间、用法用量、价格。

### 3.4 医保匹配不在解析阶段

- PDF/PPT 解析**不产生医保字段**；
- 查询结果写入 / 展示时，`flatten_to_rows()` 用
  `_match_nhi(通用名, 剂型)` 去 `nhi_dictionary.json` 匹配；
- 由字典计算：
  - `drug_insurance_date`（药品首次进医保）
  - `indication_insurance_date`
  - `insurance_status_text`（如“2022年进入，2026年退出”）
  - `insurance_category` / `insurance_restriction`
- 医保目录按“通用名 + 剂型”收录；同一成分可能有两条（如泰它西普两个通用名），
  这是官方目录本身两条，不是字典重复。

---

## 4. 本轮改动（2026-11-26）

### 4.1 简称/分型 LLM 化

- 新增 `indication_llm.py`：大类/分型规则，不依赖 `medical_aliases.json`、
  `_CONCISE_RULES`；
- `.env`：

```env
INDICATION_MODE=llm
INDICATION_DICTS=off
INSURANCE_ALIASES=on
LLM_INDICATION_THINKING=0
LLM_OVERWRITE_APPROVED=1
```

- 全库回填：90/90 匹配，74 行变更，费用约 ¥0.039；
- 规则修正：id 23 MSI-H、id 94 哮喘，费用 ¥0.0012；
- 写库前后 90 → 90，状态不变，只改 short/subtype。

### 4.2 代码修复

- `_short`：SLE / IgAN / COPD 不再被“疾病”覆盖；
- `_save_rows`：按“通用名+厂家+剂型+规格+全文”匹配已有行，简称变化不新建重复行；
- `llm_client`：关闭 SDK 自动重试，避免批量 shadow 看起来卡死；
- `tests/conftest.py`：固定测试开关，不依赖本地 `.env`。

### 4.3 其他

- `Dockerfile` / `docker-compose.yml` / `docker/README.md`；
- `reports/llm_indications_apply_final.json` 保留最后一次回填报告；
- 依赖收敛为 `requirements.txt`（精确版本）+ `requirements-dev.txt`。

---

## 5. 已知问题 / 不要踩

1. **LLM 归一发生在网搜之前**：网搜后来新增的适应症仍走
   `_normalize_disease_with_dict` + `_split_short_subtype` 兜底，
   可能没有“大类/具体分型”；下一步可在 `flatten_to_rows` 前再归一一次。
2. `INDICATION_DICTS=off` 会隐藏指南 hints、海外 hints、泛称排除；
   指南归属/海外去重会变弱。
3. `LLM_OVERWRITE_APPROVED=1` 仍在生产：重新查询可能覆盖 APPROVED 的
   short/subtype；如果不希望，回填完成后改回 0。
4. DRAFT 超过 24 小时会被启动清理；重要待审核数据当天处理。
5. Docker 未实际 build（本机无 Docker）；compose YAML 已校验。
6. 不要手工改 `indication_short/subtype`；改 `indication_llm.py` 规则后重跑。

---

## 6. 下一步：适应症获批时间

### 6.1 现状

- 54/90 为空；22 条有来源；12 条等于上市时间；
- 当前规则在 `approval_dates.py` + `agent._fill_missing_approval_dates()`：
  - 日期必须与病名同句；
  - 限定词（一线/二线/围术期/HER2/单药/鳞/非鳞）一致；
  - 至少 2 个独立域名；
  - “首次上市/首次获批”同月不算；
  - 每药最多 2 条，只补网搜产品/新增适应症。
- PPT 的 `indication_approval_times` 只按病名匹配；同名多行跳过；
- PDF VLM prompt 明确“不要输出获批时间”；
- `main._clamp_indication_approval_date()` 早于上市日 → 留空；
- `indication_approval_source` 有 22 条 URL。

### 6.2 为什么现有规则不够

- 单来源的 NMPA 公告/药企新闻被“双域名”全部丢掉；
- 日期常与病名不在同一句，搜索片段被截断；
- PPT 同名多行无法区分，直接跳过；
- 12 条“等于上市月”可能是真首个适应症，也可能是旧兜底值；
- 有些空值确实“未公开”，不能为了填满而编。

### 6.3 建议：LLM 证据裁决（方案 A 不变）

新增 `approval_llm.py`：

**输入（每药一批）**
- 通用名/商品名、中国上市时间；
- 每条适应症 id / full / short / 现有日期 / 现有来源；
- PPT `indication_approval_times`；
- NHSA 申报 PDF 的“批准时间/批准日期”段落（如有）；
- 搜索命中（URL、发布日期、域名、authority）；
- `source_snapshot` 已保存的快照。

**输出**
```json
{"id": 3, "date": "2024-06-12", "precision": "day|month",
 "evidence_quote": "原文句子", "source_url": "https://...",
 "reason_code": "first_approval|new_indication|overseas|unknown",
 "confidence": 0.0}
```

**硬约束**
- 无证据 → `null`，不写库；
- `evidence_quote` 必须是输入原文子串；
- 中国行不得早于上市日、不得晚于今天；
- 单来源网搜 → 只标“待核实”，不自动写；
- 同名多行无法区分一线/联合 → `null`；
- 海外行单独口径，不用中国上市日 clamp；
- 写入时同时写 `indication_approval_source`。

**落地顺序**
1. `scripts/approval_inventory.py`：只读盘点空值/等于上市月/有来源/疑似旧兜底；
2. `scripts/shadow_approval_dates.py`：20–30 条，只出 old→new + evidence + source；
3. `scripts/apply_approval_dates.py --ids`：备份后写 DRAFT；
4. 加 `APPROVAL_OVERWRITE_APPROVED=1`，先出 before/after，再写 APPROVED；
5. UI：空值显示“未公开”，单来源显示“待核实”，来源可点。

**成本**：千问非思考约 ¥0.005–0.01/药；thinking 硬样本约 ¥0.03/药；
全库 25 个通用名 < ¥1。

**验收**：`pytest` 84+、`ruff` 干净；shadow 人工确认证据；
写库前后行数不变，只改日期/来源两列；空值允许存在。

---

## 7. 文件索引

```text
main.py
  _split_short_subtype           简称/分型（LLM 行跳过）
  _save_rows                     全文匹配；LLM 覆盖 APPROVED
  _clamp_indication_approval_date 早于上市 → 留空
  flatten_to_rows                行组装；医保匹配；日期 clamp
agent.py
  _fetch_new_drug_impl           主流程
  _fill_missing_approval_dates   当前补日期（每药 2 条）
  _fill_brand_from_metadata      商品名
  _enrich_missing_brands/dosage/market_price
  _add_latest_generics           仿制药
indication_llm.py                LLM 简称/分型（本轮新增）
approval_dates.py                网搜日期规则（下一步改/替换）
skill_vision_parser.py           PDF/PPT 解析
llm_client.py / usage_store.py   统一 LLM / 成本日志
source_snapshot.py               搜索/LLM/文档快照
scripts/apply_llm_indications.py 简称/分型回填
Dockerfile / docker-compose.yml / docker/README.md
```

---

## 8. 验证 / 回滚

```bash
cd /Users/xiaoxu/Desktop/YPCX
./venv/bin/python -m pytest -q
./venv/bin/ruff check .
```

回滚简称/分型（先停服务）：

```bash
./venv/bin/python -c "import sqlite3; s=sqlite3.connect('drug_agent.backup_before_llm_indications.db'); d=sqlite3.connect('drug_agent.db'); s.backup(d)"
```

代码回滚：`git revert <commit>`。

---

## 9. 新对话开场

> 读 `HANDOFF.md`。当前简称/分型已切千问 LLM 并全库回填；
> 下一步做“适应症获批时间”优化。先只读盘点 54 条空值和 12 条等于上市月的行，
> 设计 LLM 证据裁决（必须带原文引用、单来源只标待核实），先 shadow 不写库。
> 不要手工改数据库，改规则/脚本。
