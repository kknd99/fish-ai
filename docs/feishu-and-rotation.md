# 飞书缩略图与多账号轮换配置指南

本文覆盖三件相互独立、但建议一起配的事：

1. **飞书推送带商品缩略图**（需要飞书自建应用）
2. **多账号轮换**（避免单账号被风控后整个监控瘫痪）
3. **代理池轮换**（可选的 IP 维度保险）

三件事都通过 `.env` 配置，改完**必须重建容器**：

```bash
cd /volume1/docker/xyfish/fish-ai
sudo docker compose -f docker-compose.lan.yaml up -d --force-recreate
```

> ⚠️ `restart` 不生效：`env_file` 只在**创建**容器时注入环境变量。

---

## 一、飞书推送带商品缩略图

### 为什么需要一个自建应用

飞书的图片消息**不能直接用图片 URL**，流程必须是：

```
下载商品图 → 上传到飞书换 image_key → 用 webhook 发出（图片元素引用 image_key）
```

而「上传」接口需要 `tenant_access_token`，也就是**自建应用的 App ID / App Secret**。
自定义机器人的 webhook 本身没有这个凭证，所以纯 webhook 发不了图 —— 这是飞书的限制，
不是本项目的问题。

好消息：**消息仍然走你现有的 webhook**，应用凭证只用于上传，不用把机器人拉进群。

### 步骤

**1. 创建企业自建应用**

打开 <https://open.feishu.cn/> → 右上角「开发者后台」 → 「创建企业自建应用」 → 填名称（随便，如 `闲鱼监控推送`）→ 创建。

**2. 拿到凭证**

应用详情页 → 「凭证与基础信息」，复制：

- **App ID**（形如 `cli_a1b2c3d4e5f6g7h8`）
- **App Secret**

**3. 开通上传图片的权限**

左侧「权限管理」→ 搜索并开通下列**任意一个**：

- `im:resource` —— 获取与上传图片或文件资源（推荐）
- `im:resource:upload` —— 上传文件 V2

> 这两个是[上传图片接口](https://open.feishu.cn/document/server-docs/im-v1/image/create)
> 的必需权限。不开会报权限不足。

**4. 发布应用**

左侧「版本管理与发布」→ 创建版本 → 申请发布。
自己企业里你就是管理员，一般能自助通过（若需要审批，去管理后台批准）。

**5. 填进配置**

Web UI → 设置 → 通知设置 → 飞书卡片里那个**虚线框**「商品缩略图（可选）」：

- 自建应用 App ID
- 自建应用 App Secret

保存即可。或者直接改 `.env`：

```ini
FEISHU_APP_ID=cli_a1b2c3d4e5f6g7h8
FEISHU_APP_SECRET=你的app_secret
```

改 `.env` 的话记得 `up -d --force-recreate`。

### 行为说明

| 配置情况 | 推送形态 |
|---|---|
| 填了 App ID + App Secret | **图文卡片**：商品主图 + 价格 + 原因 + 可点链接 |
| 没填 | 纯文本（不含电脑端链接） |
| 填了但上传/下载失败、图片超 9MB | **自动降级为纯文本**，通知不会丢 |

其他细节：

- 凭证**必须成对填写**，只填一半会被接口直接拒绝（返回 422 并说明原因）
- App ID **明文回显**（不是机密，方便你确认配的是哪个应用）；App Secret **脱敏**，只回显是否已配置
- `tenant_access_token` 按 7200 秒缓存并提前 5 分钟刷新，不会每次通知都去换
- 缩略图失败时日志会打印原因：`[飞书] 缩略图处理失败，改用纯文本推送: <原因>`

### 验证

```bash
sudo docker exec fish-ai-app python -c '
import sys, json
sys.path.insert(0, "/app")
from src.infrastructure.config.settings import NotificationSettings
s = NotificationSettings()
print("webhook:", "已配置" if s.feishu_bot_url else "未配置")
print("app_id :", s.feishu_app_id or "未配置")
print("app_secret:", "已配置" if s.feishu_app_secret else "未配置")
'
```

三个都是"已配置"后，随便抓个有主图的商品就会推图。

### 常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `99991672` / `permission denied` | 没开通 `im:resource` 权限，或版本没发布 | 回到步骤 3、4 |
| `app not found` / `10003` | App ID 或 Secret 填错 | 核对凭证 |
| 一直是纯文本，日志有降级提示 | 上传失败 | 看日志里的具体原因（权限、网络、图片格式） |
| 图片不显示但消息正常 | 图片格式不被支持 | 飞书支持 JPEG/PNG/WEBP/GIF/TIFF/BMP/ICO |

### 回滚

清空 App ID 与 App Secret 即回到纯文本模式，不影响 webhook 推送。

---

## 二、多账号轮换

### 为什么要做

单账号一旦被风控标记（表现为持续弹 `baxia-dialog` 验证框），整个监控就停了。
账号池让它在被标记时**自动换下一个账号**继续跑。

### ⚠️ 关键前提：启用后 `xianyu_state.json` 不再被使用

这是最容易踩的坑。看代码：

```python
if not rotation_settings["account_enabled"]:
    if os.path.exists(STATE_FILE):
        return RotationItem(value=STATE_FILE)     # 单账号：用根目录的 xianyu_state.json
...
picked = account_pool.pick_random()               # 启用轮换：只从 state/ 里挑
```

**所以启用轮换前，必须把登录态放进 `state/` 目录**，否则会直接报
「未找到可用的登录状态文件，无法继续执行任务」。

### 步骤

**1. 准备多个登录态**

每个账号用扩展导出一次登录态，放进 `state/`（文件名随意，`*.json` 即被识别）：

```
state/acc1.json
state/acc2.json
```

两种放法：

- **Web UI**：侧边栏「账号管理」→ 新增 → 粘贴登录态 → 保存（会写到 `state/<你起的名字>.json`）
- **命令行**：`sudo sh -c 'cat > state/acc2.json'` 然后粘贴，按 `Ctrl+D` 结束

放好后确认：

```bash
cd /volume1/docker/xyfish/fish-ai
sudo ls -l state/
sudo python3 -c '
import glob, json, time
now = time.time()
for p in sorted(glob.glob("state/*.json")):
    d = json.load(open(p, encoding="utf-8"))
    c = d.get("cookies", [])
    bad = [x["name"] for x in c if isinstance(x.get("expires"), (int, float)) and 0 < x["expires"] < now]
    print("%-24s cookie %2d 个 | 已过期: %s" % (p, len(c), bad or "无"))
'
```

> 有 `已过期` 的项要修（浏览器会丢弃过期 cookie，等于未登录），修复命令见文末附录。

**2. 配置 `.env`**

```ini
# --- 账号轮换 ---
ACCOUNT_ROTATION_ENABLED=true
# per_task：一个任务固定用一个账号，只有被风控时才换
# on_failure：一失败就换账号并拉黑一段时间
ACCOUNT_ROTATION_MODE=per_task
ACCOUNT_STATE_DIR=state
ACCOUNT_ROTATION_RETRY_LIMIT=2
ACCOUNT_BLACKLIST_TTL=1800
```

**3. 重建容器**

```bash
sudo docker compose -f docker-compose.lan.yaml up -d --force-recreate
```

### 两种模式怎么选

| 模式 | 行为 | 适用 |
|---|---|---|
| `per_task`（推荐） | 同一任务内固定用一个账号，风控时换 | 账号少、想保持行为稳定 |
| `on_failure` | 每次失败都换账号并加黑名单 | 账号多、追求成功率 |

### 参数说明

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACCOUNT_ROTATION_ENABLED` | `false` | 总开关 |
| `ACCOUNT_ROTATION_MODE` | `per_task` | `per_task` / `on_failure` |
| `ACCOUNT_STATE_DIR` | `state` | 登录态目录（Docker 下别改成 `/app/state` 以外的路径） |
| `ACCOUNT_ROTATION_RETRY_LIMIT` | `2` | 单个任务最多尝试几个账号 |
| `ACCOUNT_BLACKLIST_TTL` | `300` | 被拉黑的账号冷却秒数；风控一般要更久，建议 `1800` |

### 验证

跑一次任务，日志里会出现：

```
账号轮换：使用登录状态 state/acc2.json
```

**要注意两种失败对应两种轮换**（代码里是分开处理的）：

| 失败类型 | 触发什么轮换 | 日志 |
|---|---|---|
| **登录态失效**（被重定向到登录页） | **换账号** | `[轮换] 该登录态已失效，停用它并换用账号池中的其他账号重试...` |
| **触发风控**（`baxia-dialog` 等） | **换出口 IP**（即代理，需配代理池） | `[风控] 第 N 次命中，退避 X 秒后再换出口 IP 重试...` |

所以：**账号池解决"登录态失效"，代理池解决"风控"**。只配账号池时若遇到风控，
日志会提示 `[轮换] 代理轮换未启用（PROXY_ROTATION_ENABLED=false）...`，此时只能靠
降低频率或换账号（换账号在我们实测中确实有效）。

### 与失败保护（FailureGuard）的配合

- 连续失败 3 次（`TASK_FAILURE_THRESHOLD`）→ 任务**自动暂停 24 小时**（`TASK_FAILURE_PAUSE_SECONDS`）
- **更新登录态会自动解除暂停并清零计数**（判定依据是登录态文件的修改时间）。
  也就是说：换了新账号后，只需 `sudo touch xianyu_state.json`（或重新导入一次），
  任务立刻恢复可跑，不用等 24 小时

---

## 三、代理池轮换（可选）

IP 维度的保险。格式：逗号分隔的代理地址。

```ini
# --- 代理轮换 ---
PROXY_ROTATION_ENABLED=true
PROXY_ROTATION_MODE=per_task
PROXY_POOL=http://用户名:密码@ip1:端口,http://用户名:密码@ip2:端口
PROXY_ROTATION_RETRY_LIMIT=2
PROXY_BLACKLIST_TTL=300
```

支持的写法：`http://`、`https://`、`socks5://`（镜像内已装 `python-socks`）。

日志确认：

```
IP 轮换：使用代理 http://ip1:端口
```

> 注意：代理要能访问 `www.goofish.com`。国内代理通常没问题；境外代理反而更容易被风控。

---

## 四、推荐组合

```
账号池 2~3 个  +  定时 12 小时  +  FailureGuard 自动暂停  +  （可选）代理池
```

| 配置 | 建议值 | 理由 |
|---|---|---|
| 定时间隔 | `0 */12 * * *` | 这个站点的风控对**频率**很敏感，2 小时太密（实测连续尝试会在几分钟内升级为验证框） |
| 单次页数 | 1~3 | 页数越多请求越多，风控概率越高 |
| 测试用 | `--debug-limit 1~3` | 手动验证时限制处理条数，少发请求 |
| 账号数 | 2~3 个 | 够轮换即可，账号多了维护登录态也累 |

**一条重要经验**：一个静默期（几小时）里通常只能成功跑**一次**。
连续重试会让站点从"软拦截（无商品数据）"升级到"硬验证（baxia 弹窗）"，
而且**换账号比等待更有效**。

---

## 五、排错速查

| 现象 | 检查 |
|---|---|
| 任务报「未找到可用的登录状态文件」 | 启用轮换后 `state/` 里有没有 `*.json`（根目录的 `xianyu_state.json` 此时不生效） |
| 改了 `.env` 没生效 | 用了 `restart`，应该用 `up -d --force-recreate` |
| 一直跳登录页 | 登录态里有过期 cookie，用附录里的命令修复或重新导出 |
| 持续弹 baxia 验证框 | 账号被标记 → 换账号；同时把定时间隔放宽、`ACCOUNT_BLACKLIST_TTL` 调大 |
| 飞书收不到图 | 用户查日志里的 `[飞书] 缩略图处理失败`；多半是没开 `im:resource` 权限或版本没发布 |
| 任务被暂停 24 小时 | `logs/task-failure-guard.json` 里有 `paused_until`；更新登录态（`touch` 文件）即可自动解除 |

---

## 附录：登录态过期 cookie 修复

浏览器会**丢弃** `expires` 已过期的 cookie（实测：16 个里丢掉 3 个，其中 `unb`
是用户 ID，少了它等于未登录）。修复方式是把过期时间改到未来（cookie **值不动**）：

```bash
cd /volume1/docker/xyfish/fish-ai
sudo python3 -c '
import json, pathlib, time
p = pathlib.Path("xianyu_state.json")          # 账号池里的文件同理，换成 state/accX.json
s = json.loads(p.read_text(encoding="utf-8"))
now = time.time()
bad = [c for c in s.get("cookies", [])
       if isinstance(c.get("expires"), (int, float)) and 0 < c.get("expires", 0) < now]
for c in bad:
    c["expires"] = now + 30 * 86400
p.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
print("已修复:", [c["name"] for c in bad] or "无需修复")
'
sudo chmod 600 xianyu_state.json
```

> 这只解决**客户端**过期。若服务端会话本身已失效，只能趁浏览器还登录着时重新导出。
> 判断方法：修复后跑一次，若仍跳登录页就是服务端失效了。
