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
- 当前最新提交：`9305382 feat: label basic and commercial insurance coverage`

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
