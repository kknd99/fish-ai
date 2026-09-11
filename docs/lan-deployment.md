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
  --exclude '__pycache__' --exclude 'node_modules' --exclude '.pytest_cache' --exclude '.venv' \
  --exclude '/dist' --exclude '/web-ui/dist' \
  --exclude '.env' --exclude '/logs/*' --exclude '/images/*' --exclude '/jsonl/*' \
  /Users/mc/Documents/ai-goofish-monitor/ target:/volume1/docker/xyfish/
```

- 保留 `.git`（只有 4.9 MB），目标机上仍能看历史、切分支。
- 排除 `.env`：密钥应该每台机器各自生成，不要跟着代码走。
- 排除 `node_modules` 很关键：它有 144 MB，而排除运行数据后整份代码只有约 **8 MB**
  （约 1057 个文件）。镜像在 Docker 内部自己 `npm ci`，不需要本地依赖。
- 想连任务与历史结果一起搬，就去掉 `logs/*`、`jsonl/*`、`images/*` 的排除项，
  并额外带上 `data/`（SQLite 库在里面）。
- **不要**搬 `state/`：里面的 cookie 已经失效，搬过去只会误导。

**路线 A2：单文件部署包（最省事，推荐）**

把要带的东西打成一个约 6.7 MB 的包，目标机只需解包再跑脚本：

```bash
# 在源机器上打包
# 排除模式必须锚定到包内路径：写成 --exclude='data' 会连带删掉
# web-ui/src/data/（前端构建会报 Cannot find module '@/data/goofishRegions.json'）
tar czf ai-goofish-lan-deploy.tar.gz \
  --exclude='__pycache__' --exclude='node_modules' \
  --exclude='.pytest_cache' --exclude='.venv' \
  --exclude='ai-goofish-monitor/.env' \
  --exclude='ai-goofish-monitor/xianyu_state.json' \
  --exclude='ai-goofish-monitor/dist' \
  --exclude='ai-goofish-monitor/web-ui/dist' \
  --exclude='ai-goofish-monitor/data' \
  --exclude='ai-goofish-monitor/logs' \
  --exclude='ai-goofish-monitor/images' \
  --exclude='ai-goofish-monitor/jsonl' \
  --exclude='ai-goofish-monitor/state' \
  --exclude='ai-goofish-monitor/price_history' \
  -C /Users/mc/Documents ai-goofish-monitor

scp ai-goofish-lan-deploy.tar.gz target:/volume1/docker/

# 在目标机上解包并一键部署
# 把包内容直接铺进项目目录：--strip-components=1 去掉包内的顶层目录名，
# 已有的 .env、登录态等不会被覆盖（包里本来就不含它们）
tar xzf ai-goofish-lan-deploy.tar.gz -C /volume1/docker/xyfish --strip-components=1
cd /volume1/docker/xyfish && bash deploy-lan.sh
```

包里只含代码、编排文件（yaml + json）与部署脚本，**不含** `.env`、登录态与运行数据。

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

### 推荐：一键脚本

仓库根目录的 `deploy-lan.sh` 把第 3 节的准备工作全包了，并且任何一步不满足就停下
说明原因，绝不带着半成品启动（幂等，可反复执行）：

```bash
bash deploy-lan.sh
```

它会依次：检查 docker 与 compose v2 → 生成并校验 `.env`（缺 `.env` 时会从
`.env.example` 生成并提示你填哪几项，`WEB_PASSWORD` 仍是 `admin123` 会被直接拒绝）→
补齐 `config.json` / `xianyu_state.json` 两个单文件挂载点（必要时建空文件并设 0600）→
建七个数据目录 → `up -d --build` → 等健康检查通过 → 打印本机与局域网访问地址。

换宿主端口用 `APP_PORT`：`APP_PORT=9000 bash deploy-lan.sh`。

### 手动：等价的命令

```bash
docker compose -f docker-compose.lan.yaml up -d --build
docker compose -f docker-compose.lan.yaml logs -f app
```

### JSON 版编排文件

`docker-compose.lan.json` 与 `.yaml` 内容严格等价（脚本转出并核对过）。JSON 是
YAML 1.2 的子集，所以 Compose 和 NAS / Portainer 面板都能直接吃，需要往面板里粘贴时
用哪份都行。注意两个文件都**不在 Compose 的默认发现列表**里，必须显式 `-f` 指定，
否则裸跑 `docker compose up -d` 会命中仓库里那份"拉上游镜像"的 `docker-compose.yaml`。

```bash
docker compose -f docker-compose.lan.json up -d --build
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
compose 的端口映射（`docker-compose.lan.yaml` 已经是 `${APP_PORT:-8000}:8000`，且
容器内的 `SERVER_PORT` 被钉死为 8000，避免 `.env` 里改端口导致映射对不上）
和**目标机防火墙**。

- **Linux**：`sudo ufw allow from 192.168.0.0/16 to any port 8000 proto tcp`
  （firewalld：`sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload`）
- **Windows**：管理员 PowerShell 执行
  `New-NetFirewallRule -DisplayName "ai-goofish 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow`
- **macOS**：系统设置 → 网络 → 防火墙，允许 Docker 接受传入连接（Docker Desktop 首次
  发布端口时会弹窗询问）。

> **Linux 上注意：Docker 发布端口是直接写 iptables 的，会绕过 ufw。**
> 所以 `ufw deny 8000` **拦不住**已经映射出去的容器端口，别把它当安全边界。
> 想限制来源，正确做法是改 compose 的端口映射绑定到具体网卡或地址，例如
> `"192.168.1.10:8000:8000"`（只允许该网段访问），需要精细规则则写
> `iptables -I DOCKER-USER ...`。反过来，`ufw allow` 之类的放行规则是有效的。

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

## 8. Linux 目标机专属注意事项

- **SELinux**：Fedora / RHEL / Rocky / Alma 默认 enforcing。`docker-compose.lan.yaml`
  里的挂载都带了 `:z` 共享标签，正常可用；若你另外加挂载点却没带 `:z`，容器会读不到
  文件，表现为「`.env` 明明填了却读不到」「登录态文件不存在」。排查：
  `getenforce`、`sudo ausearch -m avc -ts recent`。
- **时钟必须先同步**：容器用的是宿主机内核时钟。NAS / 树莓派这类设备如果时间漂移，
  会直接影响两个功能：cron 定时任务的触发时刻，以及**飞书签名校验**——飞书要求请求
  时间戳与服务器时间相差不超过 1 小时，偏差过大推送会被拒（报 `sign match fail`）。
  先确认 `timedatectl status` 里 NTP 已同步。
- **浏览器必须无头**：容器里没有 X display，`RUN_HEADLESS` 保持默认 `true`。
  别在容器里设 `RUN_HEADLESS=false`，Playwright 会因无法启动有头浏览器而失败；
  需要看浏览器窗口时请在本机（非容器）跑。
- **端口占用**：`ss -ltnp | grep :8000`，被占就改 compose 的映射与 `.env` 里的
  `SERVER_PORT`。
- **docker 权限**：把用户加进 docker 组（`sudo usermod -aG docker $USER`，重新登录生效），
  否则每条命令都要 sudo。
- **架构差异不用管**：源机器是 arm64（Apple Silicon），目标机通常是 x86_64 —— 因为镜像是
  **在目标机上构建**的，天然匹配本机架构，不需要 buildx/QEMU 交叉构建。
- **挂载文件属 root**：镜像内以 root 运行，bind mount 落盘的 `data/`、`logs/` 等属 root。
  想在宿主机直接改，`sudo chown -R $USER:$USER data logs jsonl state price_history`。

### 群晖 / 绿联等 NAS 上的几点差异

- **先确认架构**：`uname -m`。必须是 `x86_64` 或 `aarch64`；如果是 `armv7l`（32 位
  ARM 的老型号），Playwright 的 Chromium 没有对应构建，这个项目在 Docker 里跑不起来。
- **内存**：Chromium 加 Python 进程，建议 2 GB 以上可用内存；低配型号容易在抓取时被 OOM 杀掉。
- **权限**：群晖上 `docker` 组的配置不保证生效，最省事是直接 `sudo bash deploy-lan.sh`
  （脚本会提示你这一点）。用 sudo 后 bind mount 出来的 `data/`、`logs/` 属 root，
  想在文件管理器里直接改就 `sudo chown -R "$(id -u):$(id -g)" data logs jsonl state`。
- **没有 SELinux**：挂载上的 `:z` 是空操作，不用管。
- **图形界面替代方案**：群晖 Container Manager 的「项目」功能也能跑，把
  `docker-compose.lan.yaml` 的内容粘进去即可；但用命令行跑脚本更直接，日志也更好看。

## 9. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 构建报 `Cannot find module '@/data/goofishRegions.json'` | 打包时排除模式没锚定，把 `web-ui/src/data/` 一起排除了 | 包内含 `.git`，在项目目录执行 `git checkout -- web-ui/src/data/goofishRegions.json` 即可本地还原，无需重新传输 |
| `.env` 里改了密码，脚本仍提示要强口令 | 用 nano/vim 粘贴时光标在第 1 行，新值插到了**文件开头**，旧的 `admin123` 还在下面；读取取**最后一处** | `sed -i '/^WEB_PASSWORD=/d' .env` 后 `printf 'WEB_PASSWORD=%s\n' '新密码' >> .env`，再 `grep -n WEB_PASSWORD .env` 确认只剩一行 |
| 部署完发现改动没生效 | 用了 `docker-compose.yaml`（拉上游镜像，无 `build`） | 改用 `docker-compose.lan.yaml` |
| `config.json` 变成目录 / 应用读配置报错 | 单文件挂载点在宿主机不存在，Docker 建了目录 | 删掉那个目录，先 `touch`/写入真实文件再 `up` |
| 容器重建后登录态、通知设置全没了 | `.env` / `xianyu_state.json` 没挂卷 | 用 `docker-compose.lan.yaml`，它两个都挂了 |
| 别的电脑打不开页面 | 防火墙没放行，或 compose 端口还绑在 127.0.0.1 | 见第 5 节 |
| `.env` 填了但读不到（尤其 Windows） | 文件带 BOM | 存成 UTF-8 无 BOM |
| SELinux 机器上容器读不到挂载文件 | 挂载点缺 `:z` 标签 | 挂载加 `:z`（本仓库 compose 已带）；`getenforce` 查看 |
| 飞书推送报 `sign match fail` | 宿主机时钟漂移超过 1 小时 | `timedatectl status` 确认 NTP 已同步 |
| `ufw deny 8000` 之后外部仍能访问 | Docker 直接写 iptables，绕过 ufw | 改绑具体 IP，或用 `DOCKER-USER` 链 |
| 提示"登录态失效"、抓到反爬页 | 登录态过期，或目标机网络 IP 被风控 | 重新导出登录态；必要时换网络 |
| 日志/数据文件在 Linux 上属 root，读不了 | 镜像内以 root 运行，bind mount 落盘即 root 所有 | `sudo chown -R $USER:$USER data logs jsonl state` |

## 10. 安全检查清单

- [ ] `WEB_PASSWORD` 已改成非默认值（默认 `admin/admin123` 是公开文档里的值）
- [ ] 只需要本机访问就别开 `8000:8000`，保持绑 `127.0.0.1`
- [ ] `.env` 与 `xianyu_state.json` 权限 0600，且都**没有**进版本库
- [ ] `.dockerignore` 已排除 `.env`、`state/`、`xianyu_state.json`（仓库自带）
- [ ] 交易相关保持 `TRADE_ENABLED=false`，先跑 `TRADE_DRY_RUN=true`
- [ ] 不要在两台机器上同时跑同一批任务，否则会重复推送、重复触发交易
