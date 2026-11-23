# YPCX 药品信息查询项目交接

更新时间：2026-11-22  
项目目录：`/Users/xiaoxu/Desktop/YPCX`

---

## 1. 项目结构

- 后端：FastAPI + SQLAlchemy + SQLite
- 前端：Vue 3 + Element Plus + Tabulator
- 主服务：`main.py`，端口 `8000`
- 市场信息模块：`market_module`，端口 `8001`
- 数据库：`drug_agent.db`，市场库 `market.db`
- 测试：`tests/test_smoke.py`
- 当前测试：`21 passed`
- 当前最新提交：`1b19b38 docs: add project handoff`（代码功能提交为 `9305382`）

启动方式：

```bash
cd /Users/xiaoxu/Desktop/YPCX
Cmd + Shift + B
```

对应 VS Code 任务：

- `dev: backend` → `dev_server.py main:app 8000`
- `dev: market` → `dev_server.py market_module.app:app 8001`
- `dev: start` → 并行启动

`dev_server.py` 是本地启动包装器，作用是让 uvicorn reload 忽略 `venv` 文件变化。

---

## 2. 当前数据状态

当前数据库不是空库：

```text
drugs：22 条，全部 DRAFT
```

当前内容：

```text
依沃西单抗注射液        2
卡度尼利单抗注射液      3
度伐利尤单抗            7
纳武利尤单抗注射液      10
```

没有 `APPROVED`。

如需重新开始查询，可先清空草稿：

```bash
cd /Users/xiaoxu/Desktop/YPCX
sqlite3 drug_agent.db "delete from drugs where status='DRAFT';"
```

不要直接删除整个 `drug_agent.db`，除非确认已备份。SQLite 的 `-wal` / `-shm` 是正常附属文件，服务运行时不要手动删。

---

## 3. PDF / PPT 解析当前方案

### 3.1 解析器主体

`skill_vision_parser.py` 已回退到今早版本：

```text
commit e8474f6
```

不要再随意改这个文件。用户非常在意 PDF/PPT 解析结果。

### 3.2 适应症

新增 `indication_regex.py`，在 agent 中调用，用正则覆盖 PDF 适应症，不调用大模型。

只从以下段落取：

```text
说明书全部适应症/功能主治
```

到：

```text
现行医保目录
说明书用法用量
所治疗疾病
```

结束。

支持：

1. `【1】【2】...`
2. `1. 2. 3. ...`
3. 没有编号时按 `。` / `；` 拆
4. 忽略申报表里插入的“医保支付范围调整 1. 2. 3.”子列表
5. 全文保留在 `indication_full`
6. 简称用正则清洗

已测试：

- 替雷利珠单抗 PDF → 14 条
- 伊奈利珠单抗（伟立瑞）PDF → 3 条
- 度普利尤单抗 PDF → 5 条

### 3.3 指南

指南仍依赖早上 parser 的 VLM prompt：

```text
"guidelines":[{"name":"", "recommendation":"", "level":""}]
```

然后 `agent._inherit_pdf_indication_fields` 按病名/指南名把 VLM 指南继承到正则拆出的适应症上。

如果指南为空，优先检查 VLM，而不是先改解析器。

### 3.4 简称

`agent._regex_short_cleanup` 只做正则清洗：

- 非小细胞肺癌各亚型 → 非小细胞肺癌
- 食管鳞癌 → 食管癌
- 胃/胃食管交界处腺癌 → 胃癌
- 实体瘤等

不再调用大模型生成简称。

---

## 4. 当前最重要的阻塞：阿里云百炼欠费

VLM 配置在 `config.py`：

```text
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-plus
```

直接请求返回：

```json
{
  "code": "Arrearage",
  "type": "Arrearage",
  "message": "Access denied ... overdue-payment"
}
```

即阿里云百炼账户欠费/未结清。

当前代码已处理：

- `skill_vision_parser.VisionAccountError`
- `main.process_drug` 捕获后返回 `biz_error`
- 前端提示：
  ```text
  视觉模型账户欠费，已停止查询；请充值后再试，未使用网搜兜底
  ```
- 不会继续网搜补美国英文适应症

**新对话第一件事：确认阿里云百炼账户已充值、模型已开通。否则 PDF 正文/指南一定为空。**

验证命令：

```bash
cd /Users/xiaoxu/Desktop/YPCX
./venv/bin/python - <<'EOF'
import requests
from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
r = requests.post(
    VISION_BASE_URL.rstrip('/') + '/chat/completions',
    headers={'Authorization': f'Bearer {VISION_API_KEY}', 'Content-Type': 'application/json'},
    json={'model': VISION_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10},
    timeout=20,
)
print(r.status_code, r.text[:500])
EOF
```

返回 200 才说明恢复。

---

## 5. 医保匹配与显示

`main._insurance_status_text` 现在按年逐条匹配医保限制，输出：

```text
2021年进入
2021年进入，2026年退出
```

外层再根据 `catalog_type` 加类型：

```text
2021年进入（基本医保）
2026年进入（商保创新药）
```

显示字段：`insurance_status_text`。  
前端列名仍是“适应症进医保时间”。

重要规则：

- 医保目录是权威，不因为获批时间晚于医保时间就改医保时间
- 经典型霍奇金淋巴瘤：2021 年进入，2026 年退出
- 围术期非小细胞肺癌：2026 年进入，不能套 2022
- 医保匹配按完整适应症，不只用简称

当前限制：

- 字典里同一通用名通常只有一种 `catalog_type`
- 一个药同时有基本医保和商保两条记录时，显示“双行”尚未实现
- 商保数据在 `nhi_dictionary.json` 里已有 29 条，`catalog_type = 商业健康保险创新药`

---

## 6. 去重规则

`main._country_dedupe_rows`：

- 同一通用名 + 厂家 + 归一简称
- 中国有 → 保留中国，删除美国/全球重复
- 中国没有 → 海外保留最具体的一条
- 额外别名：
  - `鼻窦炎` → `慢性鼻窦炎伴鼻息肉`
  - `结节性瘙痒症` → `结节性痒疹`

品牌校验在 `agent.py`：

- `_is_plausible_brand`：拒绝超过 6 字、以“单抗”结尾、含“注射液/片”的假品牌
- `_brand_matches_generic`：二次确认品牌属于通用名

中国适应症如果 `indication_full` 没有中文，`main.flatten_to_rows` 直接跳过，避免 FDA 英文行。

---

## 7. 入库 / 草稿

- 查询结果写入 `DRAFT`
- 不点入库的草稿 24 小时后清理，启动/查询时都会清理
- 点“入库”才变 `APPROVED`
- `APPROVED` 永久保留
- 入库时间在 `updated_at`
- 前端“保存草稿”和“保存并入库”两个按钮
- 查询结果只保留当前查询，不会自动叠加历史

相关接口：

- `POST /drugs` 入库
- `POST /drugs/draft` 保存草稿
- `PATCH /drugs/{id}/draft` 更新草稿
- `PATCH /drugs/{id}` 人工补全
- `GET /drugs` 只返回 APPROVED

数据库曾移除唯一约束，改为程序按完整 `indication_full` 去重。原因是 4 条非小细胞肺癌简称相同但临床场景不同，不能靠简称唯一键。

---

## 8. 前端当前状态

`index.html`：

- 已删除第一列“序号”
- “适应症全称”保留 4 行截断 + “查看全文”弹窗
- 增加“适应症排序：按编号 / 按获批时间”
- “只看待反馈”包含 `DRAFT / MACHINE / FEEDBACK`
- 长字段：适应症、用法用量、医保限制、指南都支持查看全文
- 用法用量四列：单次用量、用药频次、给药途径、疗程
- 用法用量先用正则解析，失败再问 LLM
- 表格只显示当前查询结果

已知待验证问题：

- 多个药品同时查询时的实际排序是否完全符合预期
- 同一药品多个 NSCLC 适应症简称都为“非小细胞肺癌”，靠完整原文区分
- 指南是否能在 VLM 恢复后正确继承到对应适应症

---

## 9. 用户明确偏好

- 不要药品白名单
- 不要英文 → 中文商品名字典映射
- 不要 `source` 字段
- 不要随意改 PDF/PPT 解析
- 中国药品不要出现 FDA 英文适应症
- 适应症全称要尽量完整
- 适应症简称要简洁，但不要为了匹配把简称越写越长
- 医保目录权威，不要用获批时间否定医保时间
- 原研排在仿制前
- 厂家优先取 PDF 封面“企业名称”，不要“上市许可持有人（授权企业）”
- 不要再把第一列序号加回来

---

## 10. 新对话建议排查顺序

1. 先验证 VLM 是否已充值恢复（第 4 节命令）
2. 清空 DRAFT 后查一个药：
   - 替雷利珠单抗：预期 14 条中国适应症
   - 伊奈利珠单抗 / 伟立瑞：预期 3 条，不能出现“8.2020年申报条件”
   - 度普利尤单抗：预期中国 5 条 + 海外独有适应症
3. 检查“适应症全称”是否中文、是否从 `说明书全部适应症` 开始
4. 检查指南是否有值；没有先看 VLM 是否 400/欠费
5. 检查医保显示是否是“年份进入（基本医保/商保创新药）”
6. 检查美国/中国是否只剩中国行
7. 最后再看排序

---

## 11. 关键文件

```text
main.py                  FastAPI、flatten、医保匹配、草稿/入库
agent.py                 NHSA 文件抓取、品牌校验、PDF 正则接入、指南继承
indication_regex.py      PDF 适应症正则拆分
skill_vision_parser.py   已回退到今早 e8474f6，不要随意改
models.py                Drug 表
db.py                    建表/字段迁移
index.html               前端
dev_server.py            reload 包装器
nhi_dictionary.json      医保 + 商保字典
medical_aliases.json     疾病别名（未删，但简称已不走 LLM）
tests/test_smoke.py      21 个测试
```

---

## 12. 备份

桌面保留的最新完整备份：

```text
/Users/xiaoxu/Desktop/YPCX-db-backup-20261121-172232-full-reset.db
```

项目里还有重置后生成的数据库和 WAL/SHM 附属文件：

```text
drug_agent.db
drug_agent.db-wal
drug_agent.db-shm
```

服务运行时不要手动删除 `-wal` / `-shm`。

---

## 13. 2026-11-22 本轮修复记录（新对话复跑后）

### 13.1 环境
- VLM 已验证恢复：`qwen3-vl-plus` 返回 200，欠费问题解除。
- `pytest`：**30 passed**（新增 9 个针对本轮 bug 的用例）。
- 测试方式：直接请求 `http://127.0.0.1:8000/query/stream`，不要边跑查询边改 `.py`（uvicorn reload 会中断流式请求）。

### 13.2 已修复
1. **医保标签大面积错成“未纳入”**
   `main._status_terms` 原来只认 12 个写死癌种。现在合并：硬编码关键词 + `_disease_terms(简称)` + 医学字典命中 + “…癌/…瘤”兜底，并补了 SCLC/NSCLC 双向防串。
   验证：度普利尤 特应性皮炎 2021、结节性痒疹/哮喘/COPD/大疱性类天疱疮 2027；伊奈利珠 视神经脊髓炎 2023；卡度尼利 宫颈癌 2025/2026。
2. **中国/海外去重没接线**
   `_country_dedupe_rows` 之前从未被调用。现在 `flatten_to_rows` 末尾调用，并新增：
   - 同一适应症全文、官方档案行优先，去掉网搜同文的“幻影产品”；
   - 海外行如果只是中国已覆盖的同类病或“恶性肿瘤”这种泛指，直接丢掉。
   验证：度普利尤 10→8、伊奈利珠 4→3、替雷 16→14。
3. **简称折叠/残留冒号**
   `_normalize_disease_with_dict` 不再把非小细胞肺癌/小细胞肺癌归一成“肺癌”；
   `_regex_short_cleanup` 与 `indication_regex._short` 去掉尾部 `：:`。
   验证：替雷 14 行简称正确区分 NSCLC/SCLC。
4. **指南继承错配**
   `_inherit_pdf_indication_fields` 重写为逐条过滤：指南名病名对不上原文就丢；推荐病名/治疗线对不上就只留指南名；同名指南去重；肺癌缩写（NSCLC/SCLC/慢阻肺/gMG/NMOSD）统一展开。
   验证：替雷只有围术期那条带推荐，3/4/5 行只留指南名；斯鲁利 SCLC 指南回到 SCLC 行；度普利尤 特应性皮炎 不再挂 GOLD。
5. **厂家被 PDF 折行截断**
   `skill_vision_parser._extract_pdf_key_info` 会继续拼接下一行。
   验证：`上海复宏瑞霖生物技术有`+`限公司` → `上海复宏瑞霖生物技术有限公司`（纳武、替雷、斯鲁利三个 PDF 都验证过）。
6. **`_save_rows` 返回旧草稿**
   现在只返回本次查询写入/命中的行；旧草稿留在库里但不混进当前结果。

### 13.3 仍未处理（需要你拍板）
- **纳武利尤**：PDF 正则只有 2 条完整适应症，PPT 时间表又补了 8 行 `indication_full` 只有“肝癌”“结直肠癌”这种两字病名，简称还有“与伊匹木单抗”“单药或与化疗”。是否保留这些补行？
- 斯鲁利【3】里 ①非鳞状 / ②鳞状 还没拆行（医保字典是两条限制）。
- 海外行 `insurance_status_text` 仍显示“未纳入”，而 `indication_insurance_date` 是“不适用”，文案不一致。
- VLM 自身有错标（斯鲁利 PDF：第 2 条 short 非小细胞肺癌但 full 是小细胞肺癌；胃癌条目下挂食管癌推荐），继承逻辑只能过滤，不能纠正病名。

### 13.4 当前数据
- 本轮复跑后 `drugs` 共 **51 条 DRAFT**（含用户 08:15 的卡度尼利/依沃西/斯鲁利、00:29 的度伐利尤、00:30 的纳武利尤，以及本轮复跑的替雷 14、度普利尤 8、伊奈利珠 3）。
- 仍未提交 git；改动文件：`main.py`、`agent.py`、`indication_regex.py`、`skill_vision_parser.py`、`tests/test_smoke.py`。

### 13.5 第二批（用户确认后）

- 离线回填了现有 51 条草稿的医保标签（不调模型）：卡度尼利 宫颈癌 二线→2025、一线→2026；同时清掉 `宫颈癌:`、`食管鳞状细胞癌：` 的尾部冒号。
- PPT 指南解析：`_extract_ppt_info` 现在会递归读 **表格单元格** 和 **组合形状** 里的文本，并在同一次文本模型调用里同时抽 `indication_approval_times` 和 `guidelines`；`agent._attach_ppt_guidelines` 按指南自带的病名挂到对应适应症。
  - 卡度尼利实测：胃癌行拿到 CSCO胃癌指南（2025）/CACA 2025/PD-L1 共识；宫颈癌行拿到 9 部指南（妇科肿瘤免疫检查点 2025、复发转移性宫颈癌 2025、CSCO 免疫检查点 2025 等）。
- 指南推荐按治疗线拆段：`_filter_recommendation` 只保留与当前适应症原文匹配的那一段（一线行只留一线，既往治疗失败的行可留二线）。
- 仍未处理：卡度尼利「适应症获批时间 2021-11」实际是 PDF 里**贝伐珠单抗**的获批时间（卡度尼利首次上市 2022-06），需要加“中国行批准时间不得早于首次上市时间”的校验。

### 13.6 第三批（国产/进口 + 价格 + 入库保留）

- 新增字段：`origin_region`（国产/进口）、`price_insurance`（医保支付标准），`price_market` 复用为兔灵市场价；`db.init_db` 会自动 ALTER 补列。
- 国产判定：优先 PDF「该通用名全球首个上市国家/地区」，其次批准文号「国药准字」，最后按 `origin_manufacturers.json` 进口厂家字典；三者都没有时默认国产。
- 国产药不再跑 FDA 海外步骤（`agent` 的第三步条件改为 `原研 且 非国产`），阿得贝利那两条“美国/周期性発熱症候群”幻觉行已从库里删除。
- 价格：`_enrich_market_price` 用搜索 + 文本模型从兔灵网（315jiage.cn）抽最低销售价；医保支付价从 `nhi_dictionary.json` 的 `price_medical_insurance` 取，`*` 视为未公开。
- 前端：新增「国产/进口」「价格」两列；页面加载时用 `GET /drugs` 把 APPROVED 行放回表格，新查询只清草稿、保留已入库行（一张表）。
- 现有 59 条草稿已离线回填 `origin_region`（34 国产 / 25 进口）和 `price_insurance`；兔灵价需要重新查询才会写入。
- 验证：`pytest` 38 passed（含入库持久化、国产判定、价格抽取、flatten 字段），`ruff check .` 通过。

### 13.7 适应症获批时间校验

- `main._clamp_indication_approval_date`：中国行的适应症获批时间早于药品首次上市时间时，按首次上市时间兜底并记日志（海外行不校验）。
- 已离线回填现有草稿：卡度尼利 宫颈癌 2021-11 → 2022-06（2021-11 实际是 PDF 里贝伐珠单抗的获批时间）。
- `pytest` 39 passed，`ruff check .` 通过。

### 13.8 收尾（剩余问题）

- 纳武利尤的 PPT 碎片行：`_disease_covered` 判断病名是否已被完整全文覆盖（含肝癌/肝细胞癌、头颈鳞癌/头颈等同义写法），已覆盖就不再补“全称=简称”的碎片行。纳武重新查询会从 10 行收敛到 2 行完整行。
- 斯鲁利【3】的 ①非鳞状 / ②鳞状：`indication_regex._split_circled` 按 ①② 拆行并保留共同前缀，医保字典第 3、4 条可分别匹配。重新查询会从 4 行变 5 行。
- 海外行医保文案：`insurance_status_text` 统一显示“不适用”（原来显示“未纳入”，和 `indication_insurance_date` 矛盾）。
- `pytest` 42 passed，`ruff check .` 通过。
- 本轮起后台服务已停掉，端口 8000/8001 空闲；用 VS Code `Cmd+Shift+B` 重新启动。

### 13.9 恩沃利单抗暴露的问题（老 PDF）

- 现象：恩沃利的适应症全称变成“3.纳入《新型冠状病毒肺炎诊疗方案…》”等申报条件，简称也成了“纳入《…”，原研/仿制为空，商品名成了“恩维达通用名”。
- 根因：
  1. 2022 年老 PDF 把“用/入/门”写成康熙部首兼容字（`⽤` `⼊`），正则里的“说明书用法用量”匹配不到结束边界，把后面的“药品申报条件”整段吞进了适应症块。
  2. PPT 文本是“商品名：恩维达®\n通用名：…”，去空白后品牌被取成“恩维达®通用名”。
  3. 老 PDF 没有“注册分类”字段，`_judge_origin_by_reg` 返回空，原研/仿制就空了。
- 修复：
  - `indication_regex._normalize_compat` / `skill_vision_parser._normalize_compat`：只归一化康熙部首与兼容汉字，不动 ①②（否则会被 NFKC 拆成数字）。
  - 适应症结束边界增加“药品申报条件/申报条件”。
  - 无编号时不再按句号/分号硬拆，只在“本品适用于/用于/与常规…联合用于”开头处拆；①② 拆分统一在所有分支生效。
  - 品牌清洗：商品名后面若接“通用名/商品名/规格/包装”就截断。
  - 原研兜底：`全球首个上市国家/地区 = 中国` 且没有注册分类时按原研。
- 已删除恩沃利 78–83 六条申报条件脏行；重新查询应只剩 1 条「实体瘤」完整行（恩沃利本身就是 MSI-H/dMMR 实体瘤一个适应症）。
- `pytest` 45 passed，`ruff check .` 通过。

### 13.10 编号优先级 + 原研/仿制缺失

- 编号拆分按优先级依次尝试，命中一种就不再试后面的：
  `【1】` → `[1]` → `1. / 1、` → `1) / 1）` → `（1）/(1)` → `①②` → `病名：` → `本品适用于/用于`。
  - 每风格都要求编号从 1 连续，避免把“0.12g”之类误当编号。
  - `病名：`风格用于“头颈部鳞状细胞癌：…。肝细胞癌：…”。
- `_short` 与 `_regex_short_cleanup` 同步支持 `1)`、`（1）`、`1：` 前缀，并补了“复发或转移性/不可切除或转移性”前缀清洗。
- 原研/仿制缺失的根因：同一通用名下网搜又建了一个“第二产品”，它的 `origin_type` 为空。
  - 新建网搜产品时继承已有产品的 `origin_type`；同一通用名下已回填 3 行（赛帕利/菲诺利）。
  - 仍为空的 8 行是网搜造出的重复通用名产品（如“恩沃利单抗”少了“注射液”），重新查询后会走官方产品。
- 实测：菲诺利 2 条（头颈部鳞癌/肝细胞癌）、塔戈利 2 条（鼻咽癌二线/一线）、赛帕利 1 条（霍奇金）。
- `pytest` 46 passed，`ruff check .` 通过。

### 13.11 网搜重复 + 无序号/小标题解析

- 网搜重复：同一通用名下网搜常补出“第二产品”，其适应症与官方 PDF 行重复。
  `main._drop_web_covered_rows`：没有医保局 PDF/PPT 的行，若其病名已被同通用名（去掉剂型后缀）的官方行覆盖，就不再显示；官方没有的新病名仍保留。
- 编号/小标题优先级完整顺序：
  `【1】` → `[1]` → `1.`/`1、` → `1)`/`1）` → `（1）/(1)` → `【病名】`/`[病名]` → `①②` → `病名：` → `病名本品` → `本品适用于/用于`。
- 无冒号格式“宫颈癌本品适用于…”也会拆（赛帕利官方 PDF 就是这种）。
- “其中包括：1）…2）…”这类子条件不拆（度普利尤的哮喘）。
- 适应症块里被 PDF 排版插进来的“1.20xx年…申报条件”从该处截断（贝莫苏拜）。
- 实测：赛帕利 2 条（霍奇金/宫颈癌）、菲诺利 2 条、塔戈利 2 条、贝莫苏拜 3 条、度普利尤哮喘 1 条。
- `pytest` 49 passed，`ruff check .` 通过。

### 13.12 网搜只补“最近一年新获批”

- 原关键词：`{通用名} {商品名} 说明书 全部适应症 获批时间` + 逐适应症 `{商品名} {适应症} 获批时间 NMPA`。
- 现关键词：`{通用名} {商品名} 最新获批适应症 新增适应症 NMPA {cutoff}`（cutoff = 今天往前 365 天的 YYYY-MM）。
- Prompt 只要求输出 cutoff 之后新获批/新增的适应症，并把官方 PDF 已有的病名列表传进去让它不要重复。
- 后置过滤：`_drop_covered_web_inds` 去掉官方全文已覆盖的病名；`_keep_recent_web_inds` 去掉日期早于 cutoff 或没有日期的。
- 只有存在官方 PDF/PPT 时才启用这个“只补新”逻辑；没有官方材料的药仍保留原逻辑，避免网搜数据被清空。
- 效果：赛帕利/菲诺利的网搜重复行不会再录入；真正近一年新增的适应症仍会补。
- `pytest` 50 passed，`ruff check .` 通过。

### 13.13 网搜关键词调整 + 在库判断 + 最新仿制药

- 去掉“说明书”关键词；保留 `{通用名} {商品名} 全部适应症 获批时间`（8 条）用于获批时间。
- 新增 `{通用名} {商品名} 最新获批适应症 新增适应症 NMPA {cutoff}`（6 条），只抽 cutoff 之后的新适应症；cutoff = 今天往前 365 天。
- `_known_indications`：查询前从 SQLite 取该药已入库（APPROVED）的适应症，传给 `fetch_new_drug`，并入“已有适应症”列表与 prompt，网搜结果先跟库内比对。
- 新增最新仿制药：`_add_latest_generics` 搜 `{通用名} 最新获批 仿制药 生物类似药 NMPA {cutoff}`，过滤日期/品牌/厂家并做品牌校验，最多补 2 个；以 `origin_type="仿制"` 建产品，适应症复制原研的中国适应症。
- 去重函数对 `origin_type="仿制"` 的行豁免，不会被“官方已覆盖”或“全文相同”误删。
- 未做：在库药品“秒出”（命中 APPROVED 直接返回、只跑网搜增量）还没接；目前仍每次重抓官方文件。
- `pytest` 52 passed，`ruff check .` 通过。

### 13.14 入库时间语义（不加新字段）

- `updated_at` 就是“入库时间”，一行一个：点入库时写一次，之后不再变。
- 去掉 `models.updated_at` 的 `onupdate`；补全已入库行（PATCH /drugs/{id}）不再刷新；查询命中已入库行补空字段也不再刷新。
- 草稿仍会在保存/查询命中时刷新 `updated_at`，24 小时清理逻辑不变。
- `POST /drugs`、`POST /drugs/{id}/approve` 仍是写入口。
- 以后若需要“最后修改时间”，再加独立字段，不复用这个。
- `pytest` 53 passed，`ruff check .` 通过；新增测试覆盖“入库后编辑不改入库时间”。

### 13.15 批量药名输入（单个查询）

- 前端输入框：空格/逗号/顿号/分号/换行分隔的一批药名会一次性加入标签，并提示“已添加 N 个药品”。
- 后端 `split_drug_names`：同样的分隔符去重保序；`QUERY_MAX_BATCH` 默认 50，`QUERY_CONCURRENCY` 默认 3。
- 每个药仍是独立的 `fetch_new_drug` 任务（单个查询、单独进度、单独结果），只是并发受限，避免 22 个药同时打爆模型 API。
- 22 个药大约 4 批处理，单批 1~2 分钟；想更快可改 `.env` 的 `QUERY_CONCURRENCY`（注意限流）。
- `pytest` 54 passed，`ruff check .` 通过；用用户给的 22 个药名做了拆分测试。

### 13.16 批量查询暴露的问题（导出/品牌/病名/标题）

- 导出 Excel：页面没加载 SheetJS，Tabulator 的 xlsx 下载不可用。已加 `xlsx@0.18.5`，并在组件缺失时降级导出 CSV。
- 阿替利珠重复：网搜新增的品牌产品继承了官方 PDF URL，看起来像官方产品逃过去重；现在网搜新产品不再继承 `nhsa_pdf_url/ppt_url`。
- 商品名来源：`_extract_pdf_key_info` 增加读 PDF 的 `商品名`（如特瑞普利 → 拓益），PDF 优先于 PPT。
- 特瑞普利少第 1 条：`_short` 的候选正则漏了“瘤”，黑色素瘤被丢弃；补上 `瘤`，现在 13 条齐全。
- 纳武：`1.与伊匹木单抗…` 现在简称取正文首个病名（肝细胞癌→显示肝癌）；`2.单药或与化疗…相关适应症（详见说明书）` 归为“其他癌症”。
- 贝莫苏拜：申报条件不再是“从 1.20xx 年整段截断”，而是只删申报条件段落，保留后面的【肾细胞癌】【腺泡状软组织肉瘤】；现在 5 条都有全称。
- 舒格利：`【联合治疗】/【单药治疗】` 作为总标题各成一行，不再把 ①② 拆开；方形括号只认含中文的病名，避免把 `[CPS]` 当标题。
- `pytest` 55 passed，`ruff check .` 通过。
- 注意：这些是代码修复，批量查询里已有的旧行要重新查询才会更新。

### 13.17 特瑞普利第一条 / 舒格利疾病为主

- 特瑞普利第 1 条（既往全身治疗失败的黑色素瘤）之前被丢，是简称候选漏了“瘤”；补上后 PDF 解析 13 条齐全。旧库行需重查。
- 舒格利改为“疾病为主”：
  - `非小细胞肺癌` + 【联合治疗】一行（①② 合在一起）
  - `非小细胞肺癌` + 【单药治疗】一行
  - 结外NK/T细胞淋巴瘤、食管鳞癌、胃及胃食管结合部腺癌各一行
  - 标题前的“非小细胞肺癌：”前缀会补回正文，简称取这个疾病名；【联合治疗】/【单药治疗】不再当简称。
- 标题切分后残留的“2.”序号会清掉。
- `pytest` 55 passed，`ruff check .` 通过。
