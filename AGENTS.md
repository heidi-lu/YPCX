# YPCX — 药品情报 Agent

## 项目概览

YPCX 是一个药品情报抽取与展示系统：输入药品名，抓取 NHSA 医保局材料、
CDE 说明书和网页资料，调用文本/视觉模型抽取，写入 SQLite，并通过 Web
页面展示和导出。

技术栈：FastAPI、SQLAlchemy、SQLite、SSE、Vue 3 / Element Plus、PyMuPDF。

## 运行

```bash
cd /Users/xiaoxu/Desktop/YPCX
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填 MAIN_LLM_API_KEY；读 PDF 再填 VISION_API_KEY
uvicorn main:app --reload --port 8000
```

前端可直接打开 `index.html`，或在项目目录起静态服务：

```bash
python -m http.server 5173
```

## 在 VS Code 启动

- 不需要额外插件：`Cmd+Shift+B` 运行 `dev: start`，并行启动 8000 主后端和 8001 市场服务。
- `F5` 只启动主后端；日志直接显示在集成终端；`Ctrl+C` 停止。
- 前端直接打开 `index.html`，不需要单独起静态服务。
- 真正登录自启见 `docs/launchd.md`，不要默认给任务加 `runOn: folderOpen`。

## 目录

- `main.py`：FastAPI 路由、SSE、数据库读写
- `agent.py`：药品抽取主流程
- `nhsa_crawler.py`：医保局页面抓取
- `models.py` / `db.py`：SQLAlchemy 模型与 Session
- `build_dict.py`：由医保 CSV 生成 `nhi_dictionary.json`
- `market_module/`：市场数据相关模块
- `index.html` / `market.html`：前端页面

## 数据约定

- `.env`、`*.db`、`venv/`、原始 PDF/PPT、`nhi_excels/`、`nhsa_split/` 不入库。
- `nhi_dictionary.json` 是生成物，由 `build_dict.py` 从原始数据重建；
  不要手工修改。时间轴以 CSV 的 `执行时间` 为准，不是文件名/`年份`；历史药名放在 `aliases`。
- 外部来源的数据必须保留来源 URL；没有来源的字段留空，不要编造。
- 修改抽取字段时，同时检查 `agent.py`、`models.py` 和前端列定义。

## 适应症 LLM 归一（2026-11-26 起）

- 简称/分型走 `indication_llm.py`；`.env` 使用
  `INDICATION_MODE=llm`、`INDICATION_DICTS=off`、`INSURANCE_ALIASES=on`、
  `LLM_OVERWRITE_APPROVED=1`。
- **禁止手工改** `drugs.indication_short/subtype`；改 prompt/规则后重跑
  `scripts/apply_llm_indications.py`（先 dry-run，再备份 + `--apply`）。
- 覆盖 APPROVED 必须显式 `LLM_OVERWRITE_APPROVED=1`，并先出 before/after。
- 写库前用 SQLite `backup()` 备份；回滚命令见 `HANDOFF_NEXT.md`。
- 测试由 `tests/conftest.py` 固定开关，不依赖本地 `.env`。

## 交接文档

- 当前状态/下一步：`HANDOFF_NEXT.md`（2026-11-26，获批时间优化）。
- `HANDOFF.md` 是 2026-11-22 的历史交接，已归档。

## 变更流程（必须遵守）

- 修改任何代码前，先说明：问题、根因、拟修改文件、影响范围和验证方案。
- 等用户明确同意后再改代码；不要先改后说。
- 字典/数据库重建同样先给统计和样例，不直接覆盖。
- 用户同意后，改动尽量小，并在完成后报告 diff、测试结果和回滚方式。

## 开发约定

- 不提交任何密钥、数据库、缓存或虚拟环境。
- 改代码后至少跑 `ruff check .` 和 `pytest -q`，并做一次接口冒烟。
- 保持现有 API 行为；新增字段时考虑旧数据兼容。
- 外部网络失败要记录日志并降级，不要伪造成功。
- Git 工作流：DSH 改代码 → VS Code 看 diff → 测试 → commit → push。
- 不直接 push 到 `main` 之前先确认 diff 和测试结果。

## 已知坑

- NHSA 同一药品通常有 PDF 和 PPT 两份材料；PPT 第一页常含首次上市
  时间、注册规格、拟新增适应症和用法用量，不能只读 PDF。
- PDF/PPT 优先读文本层，扫描件才走视觉模型。
- CDE 有反爬，说明书链接可能为空，这不应阻塞主流程。
