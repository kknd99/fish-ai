# 局域网部署指南

在局域网另一台电脑上部署本仓库（含二次开发改动）。

## 0. 为什么不能直接用 `docker-compose.yaml`

`docker-compose.yaml` 是给"只想跑官方镜像的使用者"准备的：

```yaml
image: ${APP_IMAGE:-ghcr.io/usagi-org/ai-goofish:latest}
pull_policy: always      # 每次 up 都去拉上游镜像
# 没有 build: 段
```

也就是 `docker compose up -d` 会把**官方上游镜像**拉下来运行，本仓库的认证、路径约束、
飞书推送、交易层等改动一行都不会生效，而且不报错 —— 容易误判成部署成功。
它的端口也只绑 `127.0.0.1`，局域网访问不到。

局域网部署请用 `docker-compose.lan.yaml`：`build: .` 构建本仓库代码、端口绑所有网卡、
并把 `.env` 与 `xianyu_state.json` 挂出来持久化。

## 1. 目标机准备

- 装 Docker（含 Compose v2）。镜像里已经包含前端构建产物与 Playwright Chromium，
  **目标机不需要装 Node.js / Python / 浏览器**。
- 磁盘预留 ~5 GB（镜像约 2 GB + Chromium + 数据）。
- 目标机能出网：构建阶段要访问 pip 源（`Dockerfile` 用的是清华源），运行阶段要访问
  闲鱼、你的 AI 接口，以及飞书 `open.feishu.cn`。

## 2. 把代码送过去

`origin` 指向上游 `Usagi-org/ai-goofish-monitor`，你没有推送权限，所以要么走局域网直拷，
要么先推到你自己的私有仓库。当前分支是 `phase1/foundation`。

**路线 A：局域网直拷（最快，推荐一次性部署）**

```bash
# 在源机器上执行，target 换成目标机的用户名@IP
rsync -av --progress \
  --exclude '__pycache__' --exclude 'node_modules' --exclude 'dist' \
  --exclude '.pytest_cache' --exclude '.venv' \
  --exclude '.env' --exclude 'logs/*' --exclude 'images/*' --exclude 'jsonl/*' \
  /Users/mc/Documents/ai-goofish-monitor/ target:/opt/ai-goofish-monitor/
```

- 保留 `.git`（只有 4.9 MB），目标机上仍能看历史、切分支。
- 排除 `.env`：密钥应该每台机器各自生成，不要跟着代码走。
- 想连任务与历史结果一起搬，就去掉 `logs/*`、`jsonl/*`、`images/*` 的排除项，
  并额外带上 `data/`（SQLite 库在里面）。
- **不要**搬 `state/`：里面的 cookie 已经失效，搬过去只会误导。

**路线 B：推到你自己的私有仓库（便于以后 `git pull` 更新）**

```bash
# 在源机器上执行；先在 GitHub/Gitee 建一个空私有仓库
git fetch --unshallow                       # 本地是浅克隆，先补全历史再推更稳妥
git remote add mine <你的私有仓库地址>
git push -u mine phase1/foundation
```

目标机上 `git clone <你的私有仓库> && git checkout phase1/foundation` 即可。
国内访问 GitHub 不稳的话用 Gitee，或干脆走路线 A。

## 3. 准备宿主机文件

在目标机的仓库根目录执行。**两个单文件挂载点必须先存在**，否则 Docker 会创建同名
**目录**顶上去，应用读配置就会出错：

```bash
cp .env.example .env
chmod 600 .env
echo '{}' > xianyu_state.json && chmod 600 xianyu_state.json
ls -l config.json                # 必须是文件，且已随仓库跟过来
```

编辑 `.env`，最少填这几项：

```ini
# AI（多模态模型，必须支持图片上传）
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL_NAME=gpt-4o

# Web 登录：绑了 0.0.0.0 就必须改掉默认值
WEB_USERNAME=admin
WEB_PASSWORD=<换成一个强口令>
# 可选：固定会话签名密钥。不填则由 WEB_PASSWORD 派生，改密码会踢掉所有已登录会话。
WEB_SESSION_SECRET=<随便一串长随机字符>

# 可选：飞书推送（飞书群 → 添加自定义机器人 → 复制 Webhook 地址）
FEISHU_BOT_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
# 仅当机器人开启了「签名校验」时必填
FEISHU_BOT_SECRET=

# 交易相关保持默认关闭：TRADE_ENABLED=false、TRADE_DRY_RUN=true
```

> Windows 上编辑 `.env` 请用 VS Code / Notepad++ 并存成 **UTF-8 无 BOM**。
> 记事本可能写入 BOM，会让第一个键名变成 `﻿OPENAI_API_KEY`，表现为"配置填了但不生效"。

## 4. 启动

```bash
docker compose -f docker-compose.lan.yaml up -d --build
docker compose -f docker-compose.lan.yaml logs -f app
```

本机自检（在目标机上执行）：

```bash
curl http://127.0.0.1:8000/health
# {"status":"healthy","message":"服务正常运行"}
```

启动日志里如果出现
`警告：未在 .env 文件中完整设置 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME`，
说明 `.env` 没填全，AI 分析不可用。

## 5. 放行防火墙并从其它电脑访问

容器内部 uvicorn 监听 `0.0.0.0:8000`，能不能从别的机器访问取决于两件事：
compose 的端口映射（`docker-compose.lan.yaml` 已经是 `8000:8000`）和**目标机防火墙**。

- **Linux**：`sudo ufw allow from 192.168.0.0/16 to any port 8000 proto tcp`
  （firewalld：`sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload`）
- **Windows**：管理员 PowerShell 执行
  `New-NetFirewallRule -DisplayName "ai-goofish 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow`
- **macOS**：系统设置 → 网络 → 防火墙，允许 Docker 接受传入连接（Docker Desktop 首次
  发布端口时会弹窗询问）。

在**另一台**电脑上验证：

```bash
curl http://<目标机IP>:8000/health
```

能返回 JSON 就说明通了，浏览器打开 `http://<目标机IP>:8000` 用 `WEB_USERNAME`/`WEB_PASSWORD`
登录。查目标机 IP：Linux `ip a`、macOS `ipconfig getifaddr en0`、Windows `ipconfig`。

> 管理接口走的是明文 HTTP，会话 cookie 在局域网里是明文传输的。自用内网一般可以接受；
> 要更稳妥就在前面套一层带 HTTPS 的反向代理。

## 6. 首次使用

1. 浏览器登录 Web UI。
2. **导入登录态**：用 `chrome-extension/` 里的扩展在**正常浏览器**上导出闲鱼登录态
   （先手动登录闲鱼），再在 Web UI 的登录态页面粘贴导入。导入的 content 会写到
   `xianyu_state.json`，因为挂了卷，容器重建也不会丢。
3. 在设置页确认通知渠道（如飞书）能收到测试推送。
4. 建任务、跑一次 `--debug-limit` 级别的试跑，确认能抓到商品。
5. 确认无误后再开定时任务。

> 登录态是**短时效**的：`_m_h5_tk` 这类 token 大约几十分钟就失效。导出后尽快导入并试跑，
> 长期跑需要定期重新导出。抓不到商品时优先怀疑登录态，而不是代码。

## 7. 日常更新

```bash
git pull                       # 路线 B；路线 A 则重新 rsync 一次
docker compose -f docker-compose.lan.yaml up -d --build
```

改了后端 Python 想快速生效（`docker-compose.lan.yaml` 没挂 `src/`，所以仍要重建）；
用 `docker-compose.dev.yaml` 的话它挂了 `./src`，后端改动 `restart` 即可，但前端产物在
镜像里，改 Vue 仍需 `--build`。

## 8. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 部署完发现改动没生效 | 用了 `docker-compose.yaml`（拉上游镜像，无 `build`） | 改用 `docker-compose.lan.yaml` |
| `config.json` 变成目录 / 应用读配置报错 | 单文件挂载点在宿主机不存在，Docker 建了目录 | 删掉那个目录，先 `touch`/写入真实文件再 `up` |
| 容器重建后登录态、通知设置全没了 | `.env` / `xianyu_state.json` 没挂卷 | 用 `docker-compose.lan.yaml`，它两个都挂了 |
| 别的电脑打不开页面 | 防火墙没放行，或 compose 端口还绑在 127.0.0.1 | 见第 5 节 |
| `.env` 填了但读不到（尤其 Windows） | 文件带 BOM | 存成 UTF-8 无 BOM |
| 提示"登录态失效"、抓到反爬页 | 登录态过期，或目标机网络 IP 被风控 | 重新导出登录态；必要时换网络 |
| 日志/数据文件在 Linux 上属 root，读不了 | 镜像内以 root 运行，bind mount 落盘即 root 所有 | `sudo chown -R $USER:$USER data logs jsonl state` |

## 9. 安全检查清单

- [ ] `WEB_PASSWORD` 已改成非默认值（默认 `admin/admin123` 是公开文档里的值）
- [ ] 只需要本机访问就别开 `8000:8000`，保持绑 `127.0.0.1`
- [ ] `.env` 与 `xianyu_state.json` 权限 0600，且都**没有**进版本库
- [ ] `.dockerignore` 已排除 `.env`、`state/`、`xianyu_state.json`（仓库自带）
- [ ] 交易相关保持 `TRADE_ENABLED=false`，先跑 `TRADE_DRY_RUN=true`
- [ ] 不要在两台机器上同时跑同一批任务，否则会重复推送、重复触发交易
