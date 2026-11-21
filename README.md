# YPCX — 药品情报 Agent

输入药品名，抓取国家医保局（NHSA）申报材料、CDE 说明书和网页资料，
调用文本/视觉模型抽取，写入 SQLite，并通过 Web 页面展示与导出。

技术栈：FastAPI、SQLAlchemy、SQLite、SSE、Vue 3 / Element Plus、PyMuPDF。

## 快速启动

```bash
git clone git@github.com:heidi-lu/YPCX.git
cd YPCX

python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# 编辑 .env：至少填 MAIN_LLM_API_KEY；读 PDF 再填 VISION_API_KEY

uvicorn main:app --reload --port 8000
```

另开终端启动前端：

```bash
cd YPCX
source venv/bin/activate
python -m http.server 5173
```

浏览器打开 `http://127.0.0.1:5173/index.html`；也可以直接打开 `index.html`（前端会回退到 `127.0.0.1:8000`）。

## 环境变量

见 [`.env.example`](.env.example)。主要配置：

| 变量 | 用途 |
|---|---|
| `MAIN_LLM_API_KEY` / `MAIN_LLM_BASE_URL` / `MAIN_LLM_MODEL` | 主文本模型 |
| `MARKET_LLM_*` | 市场信息抽取模型（可复用主模型） |
| `SEARCH_API_KEY` / `SEARCH_BASE_URL` | 搜索服务 |
| `VISION_API_KEY` / `VISION_BASE_URL` / `VISION_MODEL` | 视觉模型（DashScope / Qwen-VL） |
| `DATABASE_URL` | 可选；默认项目目录下 `drug_agent.db` |
| `REDIS_*` | 可选；无 Redis 时自动走内存缓存 |

`.env` 已被 `.gitignore` 忽略，不要提交。

## 数据

- `nhi_dictionary.json`：医保字典，由 `build_dict.py` 从原始 CSV 生成；
  当前已提交，保证 clone 后可直接运行。时间轴和 `first_execution_date` 使用
  CSV 的 **执行时间**，不是文件名/目录年份；名称归一会合并 `aliases`。
- `medical_aliases.json` / `origin_manufacturers.json`：同义词与原研厂家字典，提交。
- `nhi_excels/`、`nhsa_split/`、PDF/PPT、`*.db*`：本地数据，不提交。
- 原始数据更新后：

```bash
python build_dict.py
```

## 测试与检查

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

CI 配置在 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)，push 和 PR 都会运行
ruff + pytest。

## 项目结构

```text
main.py                 FastAPI 路由、SSE、数据库读写
agent.py                药品抽取主流程
nhsa_crawler.py         医保局页面抓取
deepseek_search.py      搜索封装
skill_market.py         市场数据
skill_vision_parser.py  PDF/图片视觉解析
models.py / db.py       SQLAlchemy 模型与 Session
build_dict.py           由医保 CSV 生成 nhi_dictionary.json
market_module/          市场数据模块
index.html              主页面
market.html             市场页面
tests/                  离线冒烟测试
```

## API

- `GET /query/stream?drug_names=药A,药B`：SSE 查询流
- `GET /drugs`：药品行列表
- `GET /docs`：FastAPI 自动文档

## 开发约定

- 不提交 `.env`、`*.db*`、`venv/`、原始 PDF/PPT、`nhi_excels/`、`nhsa_split/`。
- 外部数据保留来源 URL；没有来源的字段留空，不编造。
- 修改抽取字段时同步检查 `agent.py`、`models.py` 和前端列定义。
- 改完代码至少跑 `ruff check .` 和 `pytest -q`，再提交。
- 提交前用 `git diff` 检查，不要把密钥或本地数据带进去。

## 已知限制

- **NHSA**：同一药品通常有 PDF 和 PPT 两份材料；PPT 第一页常含首次上市时间、
  注册规格、拟新增适应症和用法用量，不能只读 PDF。PDF/PPT 优先读文本层，
  扫描件才回退视觉模型。
- **CDE**：存在反爬，说明书链接可能为空，不应阻塞主流程。
- **SSE**：查询流会保持长连接；反向代理需要关闭缓冲。
- **SQLite**：单机使用；多进程写入需要额外并发控制。

## 相关文档

- [`AGENTS.md`](AGENTS.md)：给 DSH/Codex 等编码 agent 的项目约定。
- 建议后续补一个 `CHANGES.md` 记录版本变更。
