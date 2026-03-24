# DeepSeek Unofficial API Proxy

[中文](#中文) | [English](#english)

## 中文

一个基于浏览器自动化的本地 DeepSeek 非官方 API 代理。

当前项目核心只有一个入口：

- [API-proxy.py](./API-proxy.py)

它会自动打开 `chat.deepseek.com`，注入本地 `userToken` 和站点偏好设置，然后发送请求并返回结果。

## 特性

- 支持 `POST /chat`
- 支持单次请求
- 支持多轮对话模式
- 支持并发请求
- 浏览器池会在忙时自动新开实例
- 支持 token 池轮换
- 支持 `deepthink` 深度思考开关
- 支持 `search` 联网搜索开关
- 通过 `localStorage` 注入：
  - `userToken`
  - `thinkingEnabled`
  - `searchEnabled`
- 能区分 `thinking` 和最终 `text`
- 默认 `headless=true`
- 默认 `verbose=true`

## 接口设计

项目没有兼容 OpenAI 风格请求体，也不打算做 OpenAI 风格响应包装。

只保留一个简单协议：

```json
{
  "request": "你好",
  "deepthink": true,
  "search": true,
  "multi_turn": false,
  "session_id": null,
  "timeout": 60
}
```

### 字段说明

- `request`: 必填，用户请求文本
- `deepthink`: 可选，是否开启深度思考
- `search`: 可选，是否开启联网搜索
- `multi_turn`: 可选，是否启用多轮对话模式
- `session_id`: 可选，多轮模式下用于继续某个会话
- `timeout`: 可选，等待回答的超时时间，单位秒

## 单次请求模式

当：

```json
"multi_turn": false
```

或者不传 `multi_turn` 时：

- 代理会直接访问 `https://chat.deepseek.com/`
- 发送请求
- 获取回答
- 保留常驻浏览器进程，但重新进入新对话页

这意味着：

- 每次都是新对话
- 不保留上下文
- 返回包中的 `session_id` 为 `null`

## 多轮对话模式

当：

```json
"multi_turn": true
```

时，逻辑如下：

### 1. 不带 `session_id`

如果请求包没有 `session_id`：

- 依旧从新对话页开始
- 发送消息
- 等回答完成
- 从当前 URL 中提取 `session_id`

例如页面 URL：

```text
https://chat.deepseek.com/a/chat/s/1932c424-9746-4a5e-b1f7-2a22ca2832f6
```

会提取出：

```text
1932c424-9746-4a5e-b1f7-2a22ca2832f6
```

然后在返回包中带回这个 `session_id`。

### 2. 带 `session_id`

如果请求包已经带了 `session_id`：

- 代理会直接访问该会话页面
- URL 格式为：

```text
https://chat.deepseek.com/a/chat/s/{session_id}
```

- 然后继续发送消息并获取新回答
- 返回包里也会继续携带这个 `session_id`

这样做的好处是：

- 调用方逻辑简单
- 前后端都可以把 `session_id` 当成唯一会话标识

## 配置方式

请在根目录创建 `.env` 文件：

```env
# If you only have one token, fill the first line and leave the second line empty.
DEEPSEEK_USER_TOKEN=your_user_token_here
# If you have multiple tokens, leave the first line empty and join all tokens with commas here.
DEEPSEEK_USER_TOKENS=
PORT=8000
```

说明：

- 只有一个 token：填写 `DEEPSEEK_USER_TOKEN`，把 `DEEPSEEK_USER_TOKENS` 留空
- 有多个 token：把 `DEEPSEEK_USER_TOKEN` 留空，在 `DEEPSEEK_USER_TOKENS` 里用英文逗号连接，例如 `token_a,token_b,token_c`
- 配置了多个 token 后，代理会在并发时轮换使用，降低单 token 压力



## 如何获取 `userToken`

1. 用浏览器登录 [https://chat.deepseek.com/](https://chat.deepseek.com/)
2. 按 `F12` 打开开发者工具
3. 打开 `Console`
4. 粘贴并执行下面这段脚本

```js
(() => {
  const raw = localStorage.getItem('userToken');
  if (!raw) {
    console.error('未找到 localStorage.userToken，请先确认当前页面已经登录。');
    return;
  }

  try {
    const parsed = JSON.parse(raw);
    console.log('raw:', raw);
    console.log('token:', parsed.value);
    copy(parsed.value);
    console.log('userToken 已复制到剪贴板。');
  } catch (err) {
    console.error('解析 userToken 失败：', err);
    console.log('raw:', raw);
  }
})();
```

如果浏览器不支持 `copy(...)`，也可以直接执行：

```js
JSON.parse(localStorage.getItem('userToken')).value
```

## 安装依赖

```bash
pip install -r requirements.txt
```

## 启动

```bash
python API-proxy.py
```

默认监听：

```text
http://127.0.0.1:8000
```

端口可通过 `.env` 里的 `PORT` 修改。

## 并发说明

当前代理使用常驻浏览器池：

- 如果有空闲浏览器实例，直接复用
- 如果现有实例都在执行任务，会自动新开浏览器实例
- 新实例会从 token 池中轮换选择 token

这意味着：

- 单请求不会每次都重新启动 Chrome
- 并发请求可以并行执行
- 多 token 配合浏览器池，可以更好分摊风控压力

## 路由

- `POST /chat`
- `GET /`
- `GET /test`
- `GET /health`
- `POST /health`

## 连通性测试

项目内置了一个连通性测试接口：

- `GET /health`
- `POST /health`

它会默认发起一个全新会话，并发送固定提示词：

```text
你是连通性测试器，请只回复：测试通畅✅
```

如果最终返回正好是：

```text
测试通畅✅
```

则会返回：

```json
{
  "ok": true,
  "expected": "测试通畅✅",
  "mode": "single_turn",
  "session_id": null,
  "text": "测试通畅✅",
  "thinking": null
}
```

## 本地调试页面

项目自带一个黑色风格的本地调试页：

- `GET /`
- `GET /test`

打开后可以直接：

- 发送单次请求
- 测试多轮对话
- 自动接续 `session_id`
- 执行连通性测试
- 查看 `thinking` 与 `text`

## 请求示例

### 单次请求

```json
{
  "request": "解释一下快速排序",
  "deepthink": true,
  "search": false,
  "multi_turn": false,
  "timeout": 60
}
```

### 多轮对话，首次请求

```json
{
  "request": "我们来聊聊快速排序",
  "deepthink": true,
  "search": false,
  "multi_turn": true,
  "timeout": 60
}
```

### 多轮对话，继续请求

```json
{
  "request": "那它的时间复杂度呢？",
  "deepthink": true,
  "search": false,
  "multi_turn": true,
  "session_id": "1932c424-9746-4a5e-b1f7-2a22ca2832f6",
  "timeout": 60
}
```

## 返回示例

### 单次请求返回

```json
{
  "mode": "single_turn",
  "session_id": null,
  "text": "这是回答内容",
  "thinking": "这是思考内容"
}
```

### 多轮对话返回

```json
{
  "mode": "multi_turn",
  "session_id": "1932c424-9746-4a5e-b1f7-2a22ca2832f6",
  "text": "这是回答内容",
  "thinking": "这是思考内容"
}
```

## curl 示例

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"request\":\"解释一下快速排序\",\"deepthink\":true,\"search\":false,\"multi_turn\":false}"
```

## 实现说明

当前主要依赖这些前端特征：

- `textarea`
- `div.ec4f5d61`
- `.ds-markdown`
- `div.ds-flex._hash`
- `localStorage.userToken`
- `localStorage.thinkingEnabled`
- `localStorage.searchEnabled`

## 免责声明

- 本项目与 DeepSeek 官方无关
- 这是网页自动化方案，不是官方 API
- 页面结构变化后可能需要更新逻辑
- 请确保你的使用符合目标网站条款和当地法律法规

---

## English

A local unofficial DeepSeek API proxy based on browser automation.

The project currently has a single main entry point:

- [API-proxy.py](./API-proxy.py)

It opens `chat.deepseek.com`, injects the local `userToken` and site preferences, sends a request, and returns the response.

## Features

- Supports `POST /chat`
- Supports single-turn requests
- Supports multi-turn conversation mode
- Supports concurrent requests
- Opens new browser workers when all existing ones are busy
- Supports token pool rotation
- Supports `deepthink`
- Supports `search`
- Injects the following values through `localStorage`:
  - `userToken`
  - `thinkingEnabled`
  - `searchEnabled`
- Separates `thinking` from final `text`
- Default `headless=true`
- Default `verbose=true`

## API design

The project no longer supports OpenAI-style request or response compatibility.

Only one simple request format is kept:

```json
{
  "request": "hello",
  "deepthink": true,
  "search": true,
  "multi_turn": false,
  "session_id": null,
  "timeout": 60
}
```

### Field meanings

- `request`: required, the user input text
- `deepthink`: optional, enable thinking mode
- `search`: optional, enable web search mode
- `multi_turn`: optional, enable multi-turn conversation mode
- `session_id`: optional, continue a previous conversation in multi-turn mode
- `timeout`: optional, response timeout in seconds

## Single-turn mode

When:

```json
"multi_turn": false
```

or when `multi_turn` is omitted:

- the proxy opens `https://chat.deepseek.com/`
- sends the request
- waits for the response
- keeps the browser process alive, but re-enters a fresh chat page

This means:

- every request starts a fresh conversation
- no context is preserved
- `session_id` in the response will be `null`

## Multi-turn mode

When:

```json
"multi_turn": true
```

the behavior is:

### 1. Without `session_id`

If the request does not include `session_id`:

- the proxy still starts from a fresh chat page
- sends the request
- waits for the response
- extracts `session_id` from the page URL

For example, if the page URL becomes:

```text
https://chat.deepseek.com/a/chat/s/1932c424-9746-4a5e-b1f7-2a22ca2832f6
```

then the extracted `session_id` is:

```text
1932c424-9746-4a5e-b1f7-2a22ca2832f6
```

and it will be returned in the response payload.

### 2. With `session_id`

If the request already includes `session_id`:

- the proxy opens that specific conversation page
- URL format:

```text
https://chat.deepseek.com/a/chat/s/{session_id}
```

- sends the next message in that session
- returns the same `session_id` in the response

This makes client-side session handling straightforward.

## Configuration

Create a `.env` file in the project root:

```env
# If you only have one token, fill the first line and leave the second line empty.
DEEPSEEK_USER_TOKEN=your_user_token_here
# If you have multiple tokens, leave the first line empty and join all tokens with commas here.
DEEPSEEK_USER_TOKENS=
PORT=8000
```

Notes:

- If you only have one token, fill `DEEPSEEK_USER_TOKEN` and leave `DEEPSEEK_USER_TOKENS` empty.
- If you have multiple tokens, leave `DEEPSEEK_USER_TOKEN` empty and put all tokens into `DEEPSEEK_USER_TOKENS` separated by commas, for example `token_a,token_b,token_c`.
- When multiple tokens are configured, the proxy rotates across them during concurrent work.

Or copy the example file:

```bash
copy .env.example .env
```

## How to get `userToken`

1. Log in at [https://chat.deepseek.com/](https://chat.deepseek.com/)
2. Press `F12` to open Developer Tools
3. Open the `Console` tab
4. Paste and run the script below

```js
(() => {
  const raw = localStorage.getItem('userToken');
  if (!raw) {
    console.error('localStorage.userToken was not found. Make sure you are already logged in.');
    return;
  }

  try {
    const parsed = JSON.parse(raw);
    console.log('raw:', raw);
    console.log('token:', parsed.value);
    copy(parsed.value);
    console.log('userToken has been copied to the clipboard.');
  } catch (err) {
    console.error('Failed to parse userToken:', err);
    console.log('raw:', raw);
  }
})();
```

If your browser does not support `copy(...)`, run this:

```js
JSON.parse(localStorage.getItem('userToken')).value
```

## Install dependencies

```bash
pip install -r requirements.txt
```

## Start

```bash
python API-proxy.py
```

Default bind address:

```text
http://127.0.0.1:8000
```

The port can be changed with `PORT` in `.env`.

## Concurrency

The proxy now uses a persistent browser pool:

- if an idle browser worker exists, it is reused
- if all workers are busy, a new browser instance is created
- new workers rotate across the configured token pool

This means:

- Chrome does not need to restart for every request
- concurrent requests can run in parallel
- multiple tokens help spread load and reduce pressure on any single account

## Route

- `POST /chat`
- `GET /`
- `GET /test`
- `GET /health`
- `POST /health`

## Connectivity test

The project includes a built-in connectivity test endpoint:

- `GET /health`
- `POST /health`

It always starts a fresh conversation and sends the fixed probe prompt:

```text
你是连通性测试器，请只回复：测试通畅✅
```

If the final response is exactly:

```text
测试通畅✅
```

the API returns:

```json
{
  "ok": true,
  "expected": "测试通畅✅",
  "mode": "single_turn",
  "session_id": null,
  "text": "测试通畅✅",
  "thinking": null
}
```

## Local web UI

The project also includes a built-in dark web UI:

- `GET /`
- `GET /test`

It can be used to:

- send single-turn requests
- test multi-turn conversations
- automatically reuse `session_id`
- run connectivity tests
- inspect both `thinking` and final `text`

## Request examples

### Single-turn request

```json
{
  "request": "Explain quicksort",
  "deepthink": true,
  "search": false,
  "multi_turn": false,
  "timeout": 60
}
```

### Multi-turn, first request

```json
{
  "request": "Let's talk about quicksort",
  "deepthink": true,
  "search": false,
  "multi_turn": true,
  "timeout": 60
}
```

### Multi-turn, follow-up request

```json
{
  "request": "What is its time complexity?",
  "deepthink": true,
  "search": false,
  "multi_turn": true,
  "session_id": "1932c424-9746-4a5e-b1f7-2a22ca2832f6",
  "timeout": 60
}
```

## Response examples

### Single-turn response

```json
{
  "mode": "single_turn",
  "session_id": null,
  "text": "This is the answer",
  "thinking": "This is the thinking trace"
}
```

### Multi-turn response

```json
{
  "mode": "multi_turn",
  "session_id": "1932c424-9746-4a5e-b1f7-2a22ca2832f6",
  "text": "This is the answer",
  "thinking": "This is the thinking trace"
}
```

## curl example

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"request\":\"Explain quicksort\",\"deepthink\":true,\"search\":false,\"multi_turn\":false}"
```

## Implementation details

The current logic depends on these frontend signals:

## setup.py configuration

This project now supports a dedicated `setup.py` config file so these runtime options do not stay hardcoded inside `API-proxy.py`.

Edit `setup.py` and update `SETTINGS = ProxySettings(...)` if you want to change:

- `fixed_timeout_enabled`: force every request to use the same timeout.
- `fixed_timeout_seconds`: the timeout value used when fixed timeout is enabled.
- `cloudflare_wait_enabled`: whether to wait for Cloudflare verification.
- `cloudflare_wait_seconds`: how long Cloudflare verification can wait.
- `char_count_enabled`: whether to count request / response characters and export a `log/char-count-*.txt` file when the proxy exits.
- `debug_mode_enabled`: whether to enable broader debug logging and automatically disable headless mode.

- `textarea`
- `div.ec4f5d61`
- `.ds-markdown`
- `div.ds-flex._hash`
- `localStorage.userToken`
- `localStorage.thinkingEnabled`
- `localStorage.searchEnabled`

## Disclaimer

- This project is not affiliated with DeepSeek
- This is browser automation, not an official API
- Frontend changes may require updates
- Make sure your usage complies with the website terms and local laws
