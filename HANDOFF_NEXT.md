# YPCX 交接文档（2026-11-23）

项目目录：`/Users/xiaoxu/Desktop/YPCX`
上一轮历史：`HANDOFF.md`（2026-11-22，已过时，仅作历史参考）

> 当前状态：已完成“简称/分型、医保时间、获批时间、商品名、PDF 分页、去重、前端交互”这一轮修复。
> 测试 **66 passed**，`ruff` 干净；数据库 **103 行（APPROVED 85 / DRAFT 18）**。

---

## 1. 环境与启动

```bash
cd /Users/xiaoxu/Desktop/YPCX
Cmd + Shift + B            # VS Code：dev: start（8000 主服务 + 8001 市场服务）
```

单独启动：

```bash
./venv/bin/python dev_server.py main:app 8000
./venv/bin/python dev_server.py market_module.app:app 8001   # 可选
```

- 前端：直接打开 `file:///Users/xiaoxu/Desktop/YPCX/index.html`
- 测试：`./venv/bin/python -m pytest -q` → **66 passed**
- 静态检查：`./venv/bin/ruff check .`
- 数据库：`drug_agent.db`（SQLite WAL）

外部依赖：

| 用途 | 服务 |
|---|---|
| PDF/PPT 视觉解析 | 阿里云百炼 `qwen3-vl-plus` |
| 文本抽取 | DeepSeek `deepseek-chat` |
| 搜索 | DeepSeek `api.deepseek.com/v1/tools/search`（5 元/千次成功请求） |
| 兔灵价 | 搜索 + 文本模型抽取 |

---

## 2. 当前数据状态

```text
总行 103：APPROVED 85，DRAFT 18
获批日期为空 53 行
带来源 URL 的获批日期 24 行
适应症分型非空 2 行（存量还没回填）
药品进医保时间已回填 103 行
```

备份（保留最新一份，旧的已清理）：

```text
drug_agent.backup_20261123_163820_before_drug_insurance.db
```

---

## 3. 这一轮改了什么

### 3.1 字典收敛

- 新增 `disease_terms.py`：**同一份 `medical_aliases.json` 只解析一次**
  - `ALIASES_DICT`：别名 → 规范名（671 映射）
  - `DISEASE_ALIASES`：规范名 → 别名
  - `DISEASE_VARIANTS`：10 条结构性变体（截断/缩写）
  - `ABBREVIATIONS`：从 JSON 的 `abbreviations` 字段构建
- `_DISEASE_ABBR` 代码硬编码字典已删除；缩写写进 `medical_aliases.json`：
  - `非小细胞肺癌: NSCLC`、`小细胞肺癌: SCLC`、`慢性阻塞性肺疾病: 慢阻肺/COPD`、`重症肌无力: gMG`、`视神经脊髓炎: NMOSD`
- 删除了 `非小细胞肺癌→肺癌`、`小细胞肺癌→肺癌` 两条有害映射；
- 修了 `_disease_covered` 的子串判断：SCLC 不会再被 NSCLC 覆盖。

当前字典共 **13 处**（运行期 10 / 构建期 3），完整清单见第 6 节。

### 3.2 适应症简称 + 适应症分型

- 前端列：`适应症简称`（原“适应症简”）+ 新列 `适应症分型`（`indication_subtype`）；
- 后端 `_split_short_subtype(raw, full)`：
  - 简称取最简大类：`非小细胞肺癌→肺癌`、`肝细胞癌→肝癌`、`食管鳞状细胞癌→食管癌`、`三阴性乳腺癌→乳腺癌`、`胃或胃食管结合部腺癌→胃癌`、`过敏性/重度…哮喘→哮喘`；
  - 分型取：全文 `【】/[]` 内病名 → 原文更细写法 → `术前/术后/新辅助/围术期/辅助`；
  - 清洗 `期或转移性…`、`或IIIA期…` 等排版残留。
- 注意：简称列现在会出现多个“肺癌”，靠分型区分。

### 3.3 医保时间

- 新增列 **药品进医保时间**（`drug_insurance_date`，字段先于“适应症进医保时间”）：取 `nhi_dictionary.json` 的 `first_execution_date`，没有则 `first_year`，不在目录为“未纳入医保”；
- 适应症医保匹配仍以**全称**为主：
  - `_clause_conflicts`：鳞状/非鳞状互斥；联合维迪西妥或 HER2 的适应症必须条款含 HER2；
  - 进入年取修复后的首个匹配年；“退出”仍按最新目录缺失判断（替雷利珠/赛帕利霍奇金已确认是真实退出）。
- `indication_insurance_date` 兼容字段仍用简称，前端主显示用 `insurance_status_text`（全称口径）。**按用户要求不动。**

### 3.4 获批时间（方案 A：没有证据就留空）

- **PDF 正文的 VLM 不再输出 `approval_date`**（说明书通常没有分适应症日期，以前模型拿上市时间凑，是“贝莫苏拜 2024-04、卡瑞利珠 2019-05”这类错误的根因）；
- 首次上市时间支持到“日”（`2024-05-09`），使“早于上市”能被 clamp 掉；
- 拆分合并适应症时不再复制同一日期；PPT 同名多行不再乱赋值；
- 网搜补日期：`approval_dates.py`
  - 日期必须与病名同句；
  - 限定词（一线/二线/围术期/HER2/单药/鳞/非鳞）必须一致；
  - 同月至少 2 个独立域名来源；
  - 句子必须含“批准/获批/上市/NMPA/FDA”；
  - 保留“日”精度；
  - 只对“新增/新适应症”或纯网搜产品补，每药最多 2 条（不再每个适应症都搜）。
- 新增 `indication_approval_source` 字段，前端日期后“源”链接，悬浮显示 URL。

### 3.5 商品名

- PPT 商品名提取重写 `_extract_brand_from_text`：
  - 优先 `通用名/商品名 X / Y`；
  - 其次 `商品名：X`；
  - `（X®）` 兜底时跳过“参照药品/参比/对照药”上下文。
- 修复案例：菲诺利单抗曾把参照药“爱必妥”当商品名，现为“安佑平”。

### 3.6 PDF 分页问题

- 申报条件删除从 `(?=【|$)` 改为**逐句删除** `_DECLARATION_COND_RE`；
- 修复替雷利珠说明书跨页只剩 11 条的问题，现在能解析 14 条，第 11 条完整。

### 3.7 去重

- 国家去重 key：`通用名 + 厂家 + 简称 + 分型`；
- 网搜行：**先比分型**（同分型已被官方覆盖就丢），再按简称+变体；
- 海外行：**先比分型**，再按 `_OVERSEAS_DISEASE_HINTS`；
- 仍有全文级去重 `_drop_web_full_text_duplicates`。

### 3.8 前端

- 全局搜索修好：Tabulator 6 只支持**单个函数**过滤，原来传“函数数组”被静默忽略；
- 「一键入库」：工具栏 + 进度条每条药品完成后可单独入库；
- 状态文案：`DRAFT` → **待审核**，不再出现“草稿”；
- **已收录数据不再铺在表格**：`libraryRows` 单独保存，只用于“药品已在库”提示；每次查询只显示本次结果；
- 在库提示：`该药品最近一次入库时间为 xxx，是否需要更新最新获批适应症/仿制药？`，确认后才开始查询；
- 补全限制：`LOCKED_EDIT_FIELDS` 前端置灰 + 后端过滤：
  - 药品上市时间、适应症全称、药品进医保时间、适应症进医保时间、指南名称、指南提及、医保局PDF、PPT摘要、医保类别、医保限制、剂型、规格
- `通用名` 保持禁用（去重身份）。

---

## 4. 关键约定（不要踩）

1. **适应症简称不写病名词典**：用后缀/边界/括号同位语规则；字典只做“命名/事实”（展示归一、医保病名、厂家、缩写）。
2. **获批时间没有证据就留空**，不要用上市时间或医保年份兜底。
3. **PDF 只出病名/全文/用法用量**，不要再让 VLM 猜获批日期。
4. **医保匹配要区分限定词**：鳞/非鳞、HER2、小细胞/非小细胞、一线/既往。
5. **已收录数据不进表格**；`/drugs` 只用于在库提示和统计。
6. **补全不能改权威字段**（见 3.8 的锁定列表）。
7. 改 `.py` 会触发 uvicorn reload，查询途中不要改，否则 SSE 会断。
8. 不要手动删 `drug_agent.db-wal/-shm`；备份用 SQLite `backup()` API。

---

## 5. 已知问题 / 下一步（按优先级）

1. **存量回填**：103 行里简称/分型还是旧的（分型只有 2 行非空）。按新规则约 83 行会变；先出 diff 再写库。
2. **在库秒出 + 增量**：命中 APPROVED 时只查“最近一年新适应症 + 最新仿制药”，不整药重跑（用户之前提到，尚未做）。
3. **成本/token 日志**：目前没有 usage 记录，单药约 0.3~0.5 元只能估算。
4. **feedback 闭环**：`feedback` 表存在但 0 行、无接口；人工修正应写入并做回归。
5. **海外行日期**：帕博利珠子宫内膜癌（美国）因 FDA/中国口径对不上，已留空；如需补需要确认口径。
6. **商保**：`catalog_type=商业健康保险创新药` 会输出“（商保创新药）”；恩沃利/阿得贝利经确认不在目录，仍“未纳入”。
7. **2027 执行年**：显示“2027年进入”指 2026 版目录、2027-01-01 执行，前端文案可再优化。
8. **52 条“获批日=上市月”**：可能是真首个适应症，也可能是旧兜底值，尚未批量清理。

---

## 6. 字典清单（13 处）

运行期：

```text
 1 disease_terms.ALIASES_DICT         671   病名归一
 2 disease_terms.DISEASE_ALIASES      154   医保条款病名
 3 disease_terms.DISEASE_VARIANTS      10   去重/变体
 4 disease_terms.ABBREVIATIONS         11   缩写展开（含大小写）
 5 agent._GUIDE_DISEASE_HINTS          38   指南继承
 6 main._OVERSEAS_DISEASE_HINTS        38   海外行匹配
 7 main._GENERIC_DISEASE_NAMES          3   排除泛称
 8 agent.PLACEHOLDER_MANUFACTURERS      9   厂家占位词
 9 origin_manufacturers.json → 映射    54   国产/进口/原研
10 nhi_dictionary.json → NHI_DICT    7130   医保目录（数据字典）
```

构建期：

```text
11 build_dict.COL_ALIASES               9   CSV 列名
12 data/nhi_normalization/nhi_name_map.json   495
13 data/nhi_normalization/nhi_name_splits.csv 637
```

下一步可继续去字典的候选：2 个 38 条的 hints（改用适应症自身病名）、`DISEASE_VARIANTS`、`_GENERIC_DISEASE_NAMES`。

---

## 7. 文件索引

```text
main.py
  LOCKED_EDIT_FIELDS        补全锁定字段
  _split_short_subtype      简称/分型
  _clause_conflicts         医保限定词
  _insurance_status_text    适应症医保状态
  flatten_to_rows           行组装 / 药品进医保时间 / 来源字段
  _country_dedupe_rows      分型优先去重
  /drugs/approve-all        一键入库

agent.py
  _validate_date            日期归一
  _fill_missing_approval_dates  只补新增/网搜
  _regex_short_cleanup      简称碎片修复
  _disease_covered          覆盖判断（已修 SCLC 子串）
  _extract_brand_from_text  （在 skill_vision_parser.py）

indication_regex.py
  disease_short_from_full   后缀+边界取病名
  looks_like_short_fragment 碎片判定
  _strip_declaration_conditions  申报条件逐句删除

approval_dates.py           网搜日期：证据句 + 双域名
disease_terms.py            病名/缩写/变体统一入口
skill_vision_parser.py      PDF/PPT 抽取（含商品名修复）
index.html                  前端表格/补全/搜索/一键入库
tests/test_smoke.py         66 个离线用例
```

---

## 8. 验证 / 回滚

```bash
cd /Users/xiaoxu/Desktop/YPCX
./venv/bin/python -m pytest -q
./venv/bin/ruff check .
```

回滚数据库（停服务后）：

```bash
./venv/bin/python -c "import sqlite3; s=sqlite3.connect('drug_agent.backup_20261123_163820_before_drug_insurance.db'); d=sqlite3.connect('drug_agent.db'); s.backup(d)"
```

代码回滚：当前工作区仍未提交（`git status` 有多个修改），建议先 commit 一个快照再继续改动。

---

## 9. 变更流程（AGENTS.md）

- 改代码前先说明：问题、根因、拟改文件、影响范围、验证方案；
- 字典/数据库重建先给统计和样例；
- 改完跑 `pytest` + `ruff`，报告 diff、测试结果、回滚方式；
- 不在查询途中改 `.py`（会 reload 断 SSE）。
