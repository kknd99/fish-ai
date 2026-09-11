#!/usr/bin/env bash
#
# 局域网部署一键准备 + 启动（在 Linux 目标机的仓库根目录执行）
#
#   bash deploy-lan.sh
#
# 做五件事：检查 docker、生成并校验 .env、补齐单文件挂载点、建数据目录、构建并启动。
# 幂等，可以反复执行；任何一步不满足条件就停下并说明原因，绝不带着半成品启动。
#
# 只依赖 docker 与 bash，不需要目标机装 Node / Python / 浏览器——镜像内部自带
# 前端构建产物与 Playwright Chromium。

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.lan.yaml"

step() { printf '\n[%s] %s\n' "$1" "$2"; }
fail() { printf '\n错误：%s\n' "$1" >&2; exit 1; }

# 读 .env 里的键值：取最后一个匹配、去掉首尾空白与成对引号。
get_env() {
  sed -n "s/^$1=//p" .env 2>/dev/null \
    | tail -1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

# ---------- 1. 依赖 ----------
step 1/5 "检查 docker 与 compose v2"
command -v docker >/dev/null 2>&1 \
  || fail "未找到 docker 命令。请先安装 Docker（含 Compose v2 插件）。"
docker compose version >/dev/null 2>&1 \
  || fail "docker compose（v2）不可用。注意需要的是 'docker compose' 而不是老版 'docker-compose'。"

# 只查版本不够：`docker compose version` 不访问守护进程，权限不足时它照样成功，
# 直到第 5 步构建才报 "permission denied while trying to connect to the docker API"。
# 这里提前真正连一次守护进程，把权限问题在第 1 步就说清楚。
if ! docker_info_output="$(docker info 2>&1)"; then
  if printf '%s' "$docker_info_output" | grep -qi "permission denied"; then
    cat >&2 <<'TEXT'

错误：当前用户没有访问 Docker 守护进程的权限（/var/run/docker.sock 属于 docker 组）。

两种解决办法：

  1) 把当前用户加入 docker 组（推荐，一次搞定）：
       sudo usermod -aG docker $USER
       newgrp docker            # 让当前终端立即生效；或者直接注销重新登录
     验证：docker info 或 docker ps 能正常输出即可

  2) 本次直接用 sudo 跑本脚本：
       sudo bash deploy-lan.sh
     注意容器内以 root 运行，bind mount 出来的 data/ logs/ 等会属 root，
     之后想在宿主机直接读写要先 sudo chown -R $USER:$USER data logs jsonl state

TEXT
    exit 1
  fi
  fail "无法连接 Docker 守护进程：$(printf '%s' "$docker_info_output" | head -2 | tr '\n' ' ')"
fi

[ -f "$COMPOSE_FILE" ] || fail "缺少 $COMPOSE_FILE，请在仓库根目录执行本脚本。"

# ---------- 2. .env ----------
step 2/5 "准备并校验 .env"
if [ ! -f .env ]; then
  [ -f .env.example ] || fail "缺少 .env.example，无法生成 .env。"
  cp .env.example .env
  chmod 600 .env
  cat <<'TEXT'

已从 .env.example 生成 .env，请填好以下内容后重新执行本脚本：

  OPENAI_API_KEY      必填
  OPENAI_BASE_URL     必填，例如 https://api.openai.com/v1
  OPENAI_MODEL_NAME   必填，必须是支持图片上传的多模态模型
  WEB_PASSWORD        必填，且不能是默认的 admin123
  FEISHU_BOT_URL      可选，飞书群机器人 Webhook 地址
  FEISHU_BOT_SECRET   可选，仅机器人开启「签名校验」时需要

TEXT
  exit 1
fi

chmod 600 .env

missing=""
for key in OPENAI_API_KEY OPENAI_BASE_URL OPENAI_MODEL_NAME WEB_PASSWORD; do
  value="$(get_env "$key")"
  [ -n "$value" ] || missing="$missing $key"
done
[ -z "$missing" ] || fail ".env 里这些必填项还是空的：$missing"

# 重复项检测：dotenv 与本脚本都取**最后一处**，所以"在上面粘了一行新值"不会生效，
# 用编辑器打开时光标默认在第 1 行，直接粘贴就会插到开头，非常容易踩。
dups=""
for key in OPENAI_API_KEY OPENAI_BASE_URL OPENAI_MODEL_NAME WEB_PASSWORD WEB_USERNAME; do
  count="$(grep -c "^$key=" .env || true)"
  if [ "$count" -gt 1 ]; then
    line_numbers="$(grep -n "^$key=" .env | cut -d: -f1 | tr '\n' ' ')"
    dups="$dups  $key 出现 ${count} 次（行号：$line_numbers）\n"
  fi
done
if [ -n "$dups" ]; then
  {
    printf '\n错误：.env 里有重复的配置项，每一项只能保留一行。\n\n'
    printf '%b' "$dups"
    cat <<'TEXT'

为什么会这样：用 nano/vim 打开文件时光标停在**第 1 行**，直接粘贴会把新内容
插到文件开头，原来那行并没有被替换；而读取配置时取的是**最后一处**，所以
"我明明改了却没生效"。

最稳的清理办法（先删掉所有同名行，再追加唯一的一行）：

  # Linux
  sed -i '/^WEB_PASSWORD=/d' .env
  printf 'WEB_PASSWORD=%s\n' '你的新密码' >> .env
  grep -n WEB_PASSWORD .env      # 确认只剩一行

  # macOS 的 sed 需要带一个空备份后缀
  sed -i '' '/^WEB_PASSWORD=/d' .env

如果还想从干净状态重来（会清掉已填的其它项）：

  cp .env.example .env && chmod 600 .env && nano .env

密码里不要用单引号，避免上面的 shell 引号冲突。
TEXT
  } >&2
  exit 1
fi

web_password="$(get_env WEB_PASSWORD)"
if [ "$web_password" = "admin123" ]; then
  fail "WEB_PASSWORD 还是公开文档里的默认值 admin123。端口一旦绑到 0.0.0.0，管理接口就等于对整个局域网敞开，必须改成强口令。"
fi
if [ "${#web_password}" -lt 8 ]; then
  fail "WEB_PASSWORD 太短（当前 ${#web_password} 位）。建议至少 12 位，混合大小写、数字与符号。"
fi

host_port="${APP_PORT:-}"
[ -n "$host_port" ] || host_port="$(get_env APP_PORT)"
host_port="${host_port:-8000}"

# 宿主端口预检。NAS 上 8000 经常被系统服务或别的容器占用，而 compose 报错是在
# **构建完成之后**创建容器时——不预检就要白等一次完整构建（含下载 Chromium）。
if command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1; then
  if command -v ss >/dev/null 2>&1; then
    listen_dump="$(ss -ltn 2>/dev/null | awk '{print $4}')"
  else
    # 用 -an 再筛 LISTEN，而不是 -ltn：macOS 的 `netstat -l` 行为不一致，实测会把
    # 监听中的端口漏掉（只列出同一端口的 TIME_WAIT 记录）。也不用按行号跳表头：
    # 表头行的第 4 个字段是 "Local"，不会匹配端口模式。
    listen_dump="$(netstat -an 2>/dev/null | grep -i 'LISTEN' | awk '{print $4}')"
  fi
  if printf '%s\n' "$listen_dump" | grep -qE "[:.]${host_port}([^0-9]|$)"; then
    {
      printf '\n错误：宿主机端口 %s 已被占用，容器无法绑定这个端口。\n\n' "$host_port"
      printf '当前占用情况：\n'
      # 注意：这段是在 set -e 下执行的，任何一条管道失败都会让脚本**当场退出**、
      # 把后面的提示全部吞掉。macOS 的 netstat 里 -p 是"指定协议"且需要参数，
      # 所以这里统一不用 -p，并给管道兜底。
      occupy_detail=""
      if command -v ss >/dev/null 2>&1; then
        occupy_detail="$(ss -ltnp 2>/dev/null | grep -E "[:.]${host_port}([^0-9]|$)" | head -3 || true)"
      fi
      if [ -z "$occupy_detail" ]; then
        occupy_detail="$(netstat -an 2>/dev/null | grep -i 'LISTEN' | grep -E "[:.]${host_port}([^0-9]|$)" | head -3 || true)"
      fi
      if [ -n "$occupy_detail" ]; then
        printf '%s\n' "$occupy_detail" | sed 's/^/  /'
      else
        printf '  （未能列出占用进程，可自行执行 ss -ltnp 或 lsof -i :%s 查看）\n' "$host_port"
      fi
      cat <<'TEXT'

换一个宿主端口即可。注意：改的是 APP_PORT（宿主机端口），**不要**改 .env 里的
SERVER_PORT（那是容器内监听端口，compose 已把它钉死为 8000）：

  echo 'APP_PORT=8800' >> .env
  bash deploy-lan.sh

或者临时指定：APP_PORT=8800 bash deploy-lan.sh

确认新端口空闲：ss -ltn | grep :8800
TEXT
    } >&2
    exit 1
  fi
fi

# ---------- 3. 单文件挂载点 ----------
step 3/5 "准备单文件挂载点"
# 这两个是单文件挂载：宿主机上不存在的话，Docker 会创建同名**目录**顶上去，
# 应用随后读配置就会出错，所以必须在 up 之前保证它们是文件。
if [ ! -e config.json ]; then
  fail "缺少 config.json。它必须是一个文件（内容可以是 [])。"
fi
[ -d config.json ] && fail "config.json 是个目录而不是文件——多半是上一次挂载点不存在被 Docker 建出来的。请删掉该目录并放入真实的 config.json 文件。"

if [ -d xianyu_state.json ]; then
  fail "xianyu_state.json 是个目录而不是文件，请删掉它，本脚本会重新建一个空文件。"
fi
if [ ! -f xianyu_state.json ]; then
  printf '{}\n' > xianyu_state.json
  printf '已创建空的 xianyu_state.json（登录态导入后会写在这里）。\n'
fi
chmod 600 xianyu_state.json

# ---------- 4. 数据目录 ----------
step 4/5 "创建数据目录"
mkdir -p data state prompts jsonl logs images price_history
printf '已确保目录存在：data state prompts jsonl logs images price_history\n'

# ---------- 5. 构建并启动 ----------
step 5/5 "构建并启动（首次构建要下载依赖与 Chromium，视网络可能需要几分钟）"
docker compose -f "$COMPOSE_FILE" up -d --build

printf '\n容器状态：\n'
docker compose -f "$COMPOSE_FILE" ps

printf '\n健康检查 http://127.0.0.1:%s/health ...\n' "$host_port"
health_ok=0
i=1
while [ "$i" -le 30 ]; do
  if curl -fsS "http://127.0.0.1:$host_port/health" >/dev/null 2>&1; then
    health_ok=1
    break
  fi
  sleep 2
  i=$((i + 1))
done

if [ "$health_ok" -eq 1 ]; then
  printf '健康检查通过。\n'
else
  printf '健康检查尚未通过，可能仍在启动。请查看日志：\n  docker compose -f %s logs -f app\n' "$COMPOSE_FILE"
fi

printf '\n完成。访问地址：\n'
printf '  本机     http://127.0.0.1:%s\n' "$host_port"
ip_list="$(hostname -I 2>/dev/null || true)"
for ip in $ip_list; do
  printf '  局域网   http://%s:%s\n' "$ip" "$host_port"
done
cat <<'TEXT'

后续步骤：
  1. 浏览器登录（用 .env 里的 WEB_USERNAME / WEB_PASSWORD）
  2. 用 chrome-extension/ 里的扩展在正常浏览器导出闲鱼登录态，在 Web UI 里导入
  3. 在设置页点通知渠道的「测试」，确认能收到推送
  4. 建一个任务小范围试跑，确认能抓到商品后再开定时

常用命令：
  日志   docker compose -f docker-compose.lan.yaml logs -f app
  重启   docker compose -f docker-compose.lan.yaml restart app
  停止   docker compose -f docker-compose.lan.yaml down
  换端口 APP_PORT=9000 docker compose -f docker-compose.lan.yaml up -d
TEXT
