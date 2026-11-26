# YPCX Docker 运行说明

## 1. 准备

```bash
cd /Users/xiaoxu/Desktop/YPCX

# 1) 环境变量（至少填 MAIN_LLM_API_KEY、VISION_API_KEY）
cp .env.example .env
# 编辑 .env

# 2) 数据目录
mkdir -p docker-data snapshots reports

# 3) 复用现有数据库（推荐；没有就跳过，容器启动时会建空表）
cp drug_agent.db docker-data/drug_agent.db
```

`docker-data/` 存 SQLite，`snapshots/` 存搜索/LLM 快照，`reports/` 存回填报告；
这三个目录都在宿主上，删容器不会丢。

## 2. 启动

```bash
docker compose up -d --build
```

- API：<http://localhost:8000>
- 健康检查：`curl http://localhost:8000/drugs`
- 前端：直接用浏览器打开 `index.html`，或启用 web 服务：

```bash
docker compose --profile web up -d
# http://localhost:8080
```

`index.html` 里 API 地址默认是 `http://127.0.0.1:8000`，所以在同一台机器上打开即可。

## 3. 可选：市场服务

```bash
touch docker-data/market.db
docker compose --profile market up -d
# http://localhost:8001
```

## 4. 常用命令

```bash
docker compose logs -f api          # 日志
docker compose restart api          # 改 .env 后重启
docker compose down                 # 停止
docker compose down -v              # 连匿名卷一起删（不会删 bind mount 的 docker-data）
```

## 5. 说明

- 容器内路径：代码 `/app`，数据库 `/app/data/drug_agent.db`，快照 `/app/snapshots`。
- `.env` 不打进镜像，由 compose 的 `env_file` 注入；不要提交 `.env`。
- 首次启动如果 `docker-data/drug_agent.db` 不存在，`init_db()` 会建空表；
  要恢复数据，把备份文件复制成 `docker-data/drug_agent.db` 再重启。
- 镜像默认以 root 运行，方便写 bind mount 的 SQLite；生产环境可再改成非 root。
- 改代码后重新构建：`docker compose up -d --build`。
- 不包含 Redis：没有 Redis 时应用自动用内存缓存。

## macOS 免费替代（不装 Docker Desktop）

Docker Engine / CLI 本身是免费的；收费的是 **Docker Desktop 在大型商业场景**的授权。
个人使用、小团队（<250 人且年收入 <1000 万美元）、教育/非商业开源通常免费。
如果不想用 Docker Desktop，可以用：

### Colima（免费、MIT）

```bash
brew install colima docker docker-compose
colima start --cpu 2 --memory 4
docker context use colima
docker compose up -d --build
```

### Podman（免费）

```bash
brew install podman
podman machine init
podman machine start
podman compose up -d --build
```

本项目用的是标准 Dockerfile / compose，不依赖 Docker Desktop 专有功能。
