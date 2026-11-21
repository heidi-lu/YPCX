#!/bin/zsh
# 双击启动：后端 uvicorn + 打开 index.html
# 关闭终端或 Ctrl+C 会停止由本脚本启动的后端。
set -u
cd "$(dirname "$0")" || exit 1

if [ -x "venv/bin/uvicorn" ]; then
  UVICORN="venv/bin/uvicorn"
  PYTHON="venv/bin/python"
elif [ -x ".venv/bin/uvicorn" ]; then
  UVICORN=".venv/bin/uvicorn"
  PYTHON=".venv/bin/python"
else
  echo "未找到 venv/bin/uvicorn。"
  echo "请先执行："
  echo "  python3 -m venv venv"
  echo "  source venv/bin/activate"
  echo "  pip install -r requirements-dev.txt"
  printf "按回车关闭..."
  read -r _
  exit 1
fi

if ! "$PYTHON" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "venv 存在，但依赖导入失败（可能是架构不匹配或依赖损坏）。"
  echo "建议重建："
  echo "  mv venv venv.broken-$(date +%Y%m%d)"
  echo "  python3 -m venv venv"
  echo "  source venv/bin/activate"
  echo "  pip install -r requirements-dev.txt"
  printf "按回车关闭..."
  read -r _
  exit 1
fi

mkdir -p .run
BACK_PID=""

if curl -fsS "http://127.0.0.1:8000/docs" >/dev/null 2>&1; then
  echo "后端已在 8000 运行，直接打开前端。"
else
  echo "启动后端..."
  "$UVICORN" main:app --reload --port 8000 >.run/backend.log 2>&1 &
  BACK_PID=$!
fi

cleanup() {
  if [ -n "${BACK_PID:-}" ]; then
    echo "停止后端..."
    kill "$BACK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [ -n "$BACK_PID" ]; then
  for _ in {1..30}; do
    if curl -fsS "http://127.0.0.1:8000/docs" >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$BACK_PID" 2>/dev/null; then
      echo "后端启动失败，日志："
      tail -n 50 .run/backend.log
      printf "按回车关闭..."
      read -r _
      exit 1
    fi
    sleep 0.5
  done
fi

open "index.html" 2>/dev/null || true
echo "已打开 index.html"
echo "后端日志：.run/backend.log"
echo "关闭此终端窗口或按 Ctrl+C 停止后端。"

if [ -n "$BACK_PID" ]; then
  wait "$BACK_PID"
else
  printf "按回车关闭..."
  read -r _
fi
