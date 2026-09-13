#!/usr/bin/env bash
#
# fish-ai 一键健康检查（在项目根目录执行，需要 docker 权限）
#
#   bash health-check.sh
#
# 把这轮排查反复要问的东西一次查完：
#   1. 容器与端口是否正常
#   2. 镜像里的补丁是否都在（此前多次出现"以为打了补丁，其实 Docker 用缓存跳过了"）
#   3. 关键配置是否已填写（只显示键名与长度，不打印密钥内容）
#   4. 登录态文件与过期 cookie（过期的 cookie 会被浏览器丢弃，等于未登录）
#   5. 失败保护状态（是否被暂停、暂停到什么时候）
#   6. 任务与抓取结果统计、最近一次运行时间
#   7. HTTP 健康检查
#
# 退出码：0 = 全部通过；1 = 有需要处理的问题（详情见输出里的 ✗ / ⚠️ ）
#
# 可用环境变量覆盖：
#   CONTAINER=fish-ai-app     容器名
#   COMPOSE_FILE=docker-compose.lan.yaml
#   APP_PORT=8000             宿主端口（用于健康检查）

set -uo pipefail

cd "$(dirname "$0")"

CONTAINER="${CONTAINER:-fish-ai-app}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.lan.yaml}"
APP_PORT="${APP_PORT:-}"

PROBLEMS=0
WARNINGS=0

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; PROBLEMS=$((PROBLEMS + 1)); }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
info() { printf '    %s\n' "$1"; }

# 统一用 sudo docker（群晖/绿联上 docker 组不保证生效）
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  fi
fi

printf '\033[1mfish-ai 健康检查\033[0m  （%s）\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# ---------------------------------------------------------------- 1. 容器
section "1. 容器与端口"

# 先给状态兜底：set -u 下若 Docker 不可用，后面各节引用 $state 会直接报错退出
state="unknown"

if ! $DOCKER info >/dev/null 2>&1; then
  bad "无法连接 Docker 守护进程（检查 docker 是否运行、当前用户是否有权限）"
else
  ok "Docker 守护进程正常"

  # 容器名自动识别：改名前后分别是 fish-ai-app / ai-goofish-monitor-app，
  # 用户可能只更新了代码没重建容器，写死一个名字会误报"容器不存在"。
  if ! $DOCKER inspect "$CONTAINER" >/dev/null 2>&1; then
    detected="$($DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -iE 'fish-ai|goofish' | head -1)"
    if [ -n "$detected" ]; then
      info "指定的容器名 $CONTAINER 不存在，实际找到: $detected"
      CONTAINER="$detected"
    fi
  fi

  state="$($DOCKER inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo 'missing')"
  case "$state" in
    running)
      started="$($DOCKER inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null | cut -c1-19)"
      ok "容器 $CONTAINER 正在运行（启动于 $started）"
      ;;
    missing)
      bad "找不到容器 $CONTAINER（用 --build 重建过吗？容器名在改名后是 fish-ai-app）"
      ;;
    *)
      bad "容器 $CONTAINER 状态异常: $state"
      ;;
  esac

  if [ -z "$APP_PORT" ] && [ -f .env ]; then
    APP_PORT="$(sed -n 's/^APP_PORT=//p' .env | tail -1 | tr -d '"'"'"' ')"
  fi
  APP_PORT="${APP_PORT:-8000}"

  published="$($DOCKER port "$CONTAINER" 8000/tcp 2>/dev/null | head -1)"
  if [ -n "$published" ]; then
    ok "端口映射: $published（宿主端口 $APP_PORT）"
  else
    warn "读不到端口映射（容器可能没在跑）"
  fi
fi

# ---------------------------------------------------------------- 2. 补丁
section "2. 镜像内的补丁版本"

if [ "$state" != "running" ]; then
  warn "容器未运行，跳过补丁检查"
else
  markers="$($DOCKER exec "$CONTAINER" python -c '
import sys
sys.path.insert(0, "/app")
checks = []
try:
    from src.scraper import _is_forbidden_header, _build_snapshot_init_script
    checks.append(("请求头过滤(_is_forbidden_header)", True))
    checks.append(("快照恢复(_build_snapshot_init_script)", True))
except Exception:
    checks.append(("请求头过滤/快照恢复", False))
try:
    from src.prompt_utils import strip_reasoning
    checks.append(("思维链剥离(strip_reasoning)", True))
except Exception:
    checks.append(("思维链剥离(strip_reasoning)", False))
try:
    from src.infrastructure.external.notification_clients.feishu_bot_client import FeishuBotClient
    checks.append(("飞书图文卡片(build_card_payload)", hasattr(FeishuBotClient, "build_card_payload")))
except Exception:
    checks.append(("飞书图文卡片", False))
for name, present in checks:
    print("%s|%s" % ("OK" if present else "MISSING", name))
' 2>/dev/null)"

  if [ -z "$markers" ]; then
    warn "无法读取镜像内代码（容器可能刚启动，稍后重试）"
  else
    printf '%s\n' "$markers" | while IFS='|' read -r status name; do
      if [ "$status" = "OK" ]; then ok "$name"; else bad "$name —— 镜像里没有这段代码，需要重建镜像"; fi
    done
    missing_count="$(printf '%s\n' "$markers" | grep -c '^MISSING|' || true)"
    PROBLEMS=$((PROBLEMS + missing_count))
  fi
fi

# ---------------------------------------------------------------- 3. 配置
section "3. 关键配置（只显示键名与长度，不打印内容）"

if [ ! -f .env ]; then
  bad "缺少 .env（复制 .env.example 后填写）"
else
  ok ".env 存在（权限 $(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null)）"

  env_report="$(python3 - <<'PY' 2>/dev/null || true
import pathlib

# 这些键的值一律不打印：它们本身就是凭据，或（如机器人 webhook 地址）内含 token。
# 之前这里直接把值打了出来，把 App Secret 明文印到了终端上。
SENSITIVE_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "BOT_URL")


def mask(key, value):
    if not value:
        return "(未设置)"
    if any(marker in key.upper() for marker in SENSITIVE_MARKERS):
        return "已设置（%d 字符，已隐藏）" % len(value)
    return value


required = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "WEB_PASSWORD"]
optional = ["FEISHU_BOT_URL", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
            "ACCOUNT_ROTATION_ENABLED", "ACCOUNT_ROTATION_MODE",
            "PROXY_ROTATION_ENABLED", "PROXY_POOL",
            "TRADE_ENABLED", "TRADE_DRY_RUN", "SPIDER_DEBUG_LIMIT"]
values = {}
for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    values[key.strip()] = value.strip().strip('"').strip("'")
for key in required:
    print("REQ|%s|%d" % (key, len(values.get(key, ""))))
for key in optional:
    print("OPT|%s|%s" % (key, mask(key, values.get(key, ""))))
PY
)"

  if [ -z "$env_report" ]; then
    warn "无法解析 .env（检查是否装了 python3）"
  else
    printf '%s\n' "$env_report" | while IFS='|' read -r kind key value; do
      if [ "$kind" = "REQ" ]; then
        if [ "$value" -gt 0 ] 2>/dev/null; then ok "$key 已填写（$value 字符）"; else bad "$key 为空（必填）"; fi
      else
        info "$key = $value"
      fi
    done
    empty_count="$(printf '%s\n' "$env_report" | grep -cE '^REQ\|[^|]+\|0$' || true)"
    PROBLEMS=$((PROBLEMS + empty_count))
  fi

  if grep -qE '^WEB_PASSWORD=(admin123)?$' .env 2>/dev/null; then
    warn "WEB_PASSWORD 仍是默认值或为空 —— 局域网访问必须改"
  fi
fi

# ---------------------------------------------------------------- 4. 登录态
section "4. 登录态"

state_files=""
for f in xianyu_state.json state/*.json; do
  [ -f "$f" ] && state_files="$state_files $f"
done

if [ -z "$state_files" ]; then
  bad "没有找到任何登录态文件（xianyu_state.json 或 state/*.json）"
else
  for f in $state_files; do
    output="$(python3 - "$f" <<'PY' 2>/dev/null
import json, sys, time
path = sys.argv[1]
try:
    raw = open(path, encoding="utf-8").read()
except FileNotFoundError:
    print("  读取失败：文件不存在")
    sys.exit(1)
except PermissionError:
    print("  读取失败：权限不足（文件可能属 root，请用 sudo 运行本脚本）")
    sys.exit(1)
except OSError as exc:
    print("  读取失败：%s" % exc)
    sys.exit(1)

try:
    data = json.loads(raw)
except ValueError as exc:
    print("  读取失败：内容不是合法 JSON（%s）" % exc)
    sys.exit(1)
cookies = data.get("cookies") or []
now = time.time()
expired = [c.get("name") for c in cookies
           if isinstance(c.get("expires"), (int, float)) and 0 < c.get("expires", 0) < now]
has_unb = any(c.get("name") == "unb" for c in cookies)
has_think = "<think" in json.dumps(data, ensure_ascii=False)
mark = "\033[32m✓\033[0m" if cookies and not expired and has_unb else "\033[33m⚠\033[0m"
print("  %s %s：%d 个 cookie%s%s" % (
    mark, path, len(cookies),
    "，unb(用户ID) 存在" if has_unb else "，\033[31m缺少 unb(等于未登录)\033[0m",
    ("，\033[33m有 %d 个已过期会被浏览器丢弃\033[0m" % len(expired)) if expired else "",
))
if has_think:
    print("      \033[33m文件里含 <think> 标签，可能混入了模型思维链\033[0m")

sys.exit(2 if (expired or not has_unb or has_think) else 0)
PY
)"
    rc=$?
    printf '%s\n' "$output"
    if [ "$rc" -eq 2 ]; then
      WARNINGS=$((WARNINGS + 1))
    elif [ "$rc" -ne 0 ]; then
      warn "解析 $f 时出错（见上一行的原因）"
    fi
  done
fi

# ---------------------------------------------------------------- 5. 失败保护
section "5. 失败保护（FailureGuard）"

guard="logs/task-failure-guard.json"
if [ ! -f "$guard" ]; then
  ok "还没有失败记录（$guard 不存在）"
else
  output="$(python3 - "$guard" <<'PY' 2>/dev/null
import json, sys
from datetime import datetime
data = json.load(open(sys.argv[1], encoding="utf-8"))
tasks = data.get("tasks") or {}
if not tasks:
    print("  \033[32m✓\033[0m 没有任务失败记录")
now = datetime.now().astimezone()
for name, entry in tasks.items():
    failures = entry.get("consecutive_failures", 0)
    reason = entry.get("last_failure_reason") or "未知"
    paused_raw = entry.get("paused_until")
    paused = None
    if paused_raw:
        try:
            parsed = datetime.fromisoformat(paused_raw)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            paused = parsed
        except ValueError:
            paused = None
    if paused and paused > now:
        left = int((paused - now).total_seconds())
        if left >= 86400:
            left_text = "%d 天 %d 小时" % (left // 86400, (left % 86400) // 3600)
        elif left >= 3600:
            left_text = "%d 小时" % (left // 3600)
        else:
            left_text = "%d 分钟" % max(1, left // 60)
        print("  \033[33m⚠\033[0m %s：已暂停重试，剩余约 %s（暂停至 %s，原因 %s）" % (
            name, left_text, paused.strftime("%Y-%m-%d %H:%M"), reason))
        print("      更新登录态即可自动解除：重新导入登录态，或 touch 一下登录态文件")
    elif failures:
        print("  \033[33m⚠\033[0m %s：连续失败 %d 次（最近原因 %s），尚未暂停" % (name, failures, reason))
    else:
        print("  \033[32m✓\033[0m %s：无连续失败" % name)

sys.exit(2 if any(
    (entry.get("consecutive_failures") or 0) > 0 for entry in tasks.values()
) else 0)
PY
)"
  rc=$?
  printf '%s\n' "$output"
  if [ "$rc" -eq 2 ]; then
    WARNINGS=$((WARNINGS + 1))
  elif [ "$rc" -ne 0 ]; then
    warn "无法解析 $guard（宿主机缺 python3？）"
  fi
fi

# ---------------------------------------------------------------- 6. 任务与结果
section "6. 任务与抓取结果"

if [ "$state" != "running" ]; then
  warn "容器未运行，跳过数据库检查"
else
  db_report="$($DOCKER exec "$CONTAINER" python -c '
import sqlite3
conn = sqlite3.connect("/app/data/app.sqlite3")
tasks = list(conn.execute("select task_name, enabled, coalesce(cron, \"\") from tasks order by task_name"))
print("TASKS|%d" % len(tasks))
for name, enabled, cron in tasks:
    print("TASK|%s|%s|%s" % (name, "启用" if enabled else "停用", cron or "无定时"))
rows = list(conn.execute("select task_name, count(*) from result_items group by task_name"))
print("RESULTS|%d" % sum(n for _, n in rows))
for name, count in rows:
    print("RESULT|%s|%d" % (name, count))
last = conn.execute("select max(crawl_time) from result_items").fetchone()[0]
print("LAST|%s" % (last or "无"))
' 2>/dev/null)"

  if [ -z "$db_report" ]; then
    warn "无法读取数据库（容器可能刚启动）"
  else
    printf '%s\n' "$db_report" | while IFS='|' read -r kind a b c; do
      case "$kind" in
        TASKS) info "任务数: $a" ;;
        TASK)  info "  - $a（$b，定时: $c）" ;;
        RESULTS) info "结果总数: $a 条" ;;
        RESULT) info "  - $a: $b 条" ;;
        LAST)  info "最近抓取时间: $a" ;;
      esac
    done
  fi
fi

# ---------------------------------------------------------------- 7. 健康检查
section "7. HTTP 健康检查"

if [ "$state" != "running" ]; then
  warn "容器未运行，跳过"
elif command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:${APP_PORT}/health" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    ok "http://127.0.0.1:${APP_PORT}/health 返回 200"
  else
    bad "http://127.0.0.1:${APP_PORT}/health 返回 $code（端口对不对？看 APP_PORT）"
  fi
else
  warn "宿主机没有 curl，跳过"
fi

# ---------------------------------------------------------------- 汇总
printf '\n\033[1m汇总\033[0m：%d 个问题，%d 个提醒\n' "$PROBLEMS" "$WARNINGS"
if [ "$PROBLEMS" -eq 0 ] && [ "$WARNINGS" -eq 0 ]; then
  printf '\033[32m一切正常 ✓\033[0m\n'
elif [ "$PROBLEMS" -eq 0 ]; then
  printf '\033[33m没有阻塞性问题，但上面的提醒值得看一眼。\033[0m\n'
else
  printf '\033[31m有 %d 个问题需要处理（见上面 ✗ 的行）。\033[0m\n' "$PROBLEMS"
fi

exit $(( PROBLEMS > 0 ? 1 : 0 ))
