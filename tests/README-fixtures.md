# 离线回放夹具（fixtures）

`src/scraper.py` 是一个 1300 行、把"驱动浏览器 / 操作筛选 UI / 解析 / 调度"揉在一起的
单文件，且没有任何自动化测试（上游 CI 也不跑 pytest）。改动它之前，先靠这一层夹具
把"数据形态"钉住：**不启浏览器、不联网、不碰闲鱼**，只把录制的响应喂给解析层，
再走判定与入库。

## 现有夹具

| 文件 | 用途 |
|---|---|
| `search_results.json` | 正常单条搜索结果 |
| `search_results_mixed.json` | 坏条目 + "万" 价格 + 缺发布时间 + 正常条目混合 |
| `search_results_empty.json` | 页面确实没有商品 |
| `search_results_schema_changed.json` | 字段结构变化（没有 `resultList`） |
| `user_head.json` / `user_items.json` / `ratings.json` | 卖家主页、商品列表、评价列表 |
| `state.sample.json` | 登录态结构样例（**不含真实 cookie**） |
| `config.sample.json` | 任务配置样例 |

对应的回放测试：`tests/integration/test_pipeline_parse.py`（解析层）、
`tests/integration/test_pipeline_replay.py`（解析 → 判定 → 入库 全链路）。

## 怎么录制真实响应

1. 本地以非无头模式跑一次抓取，或直接用浏览器开发者工具；
2. 打开 `https://www.goofish.com/search?q=<你的关键词>`，在 Network 面板过滤
   `mtop.taobao.idlemtopsearch`，把响应 JSON 复制出来；
3. **抹掉个人信息**（卖家昵称、地区、链接中的 `id` 可保留但要确认不是真实交易记录），
   再存成 `tests/fixtures/` 下的新文件；
4. 在 `test_pipeline_replay.py` 里加一条回放用例，断言你依赖的字段。

> 提交前请确认夹具里没有真实 cookie、手机号、订单号或卖家个人信息。
> `.gitignore` 已排除 `state/`，但夹具目录是通过 `git add` 显式加入的，需要人工过一眼。

## 为什么值得维护

- 站点改版时，先在这里看到"哪个字段变了"，而不是在任务日志里猜；
- P2 的抓取流程抽取（`SiteAdapter`）要求**先有覆盖再动刀**，这一层就是那个网；
- 回放是确定性的：同样的输入必须产出同样的记录，回归一眼可见。
