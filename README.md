# OpenAI 批量注册工具 V0.6

一键部署的 OpenAI 账号批量注册 + API 代理工具。通过 Web 控制台操作，支持 Pre-flight 预检和 Clash 自动 IP 轮换。

## 功能特性

- **Web 控制台**：浏览器操作，实时日志，进度追踪
- **Pre-flight Check**：启动前自动检查环境变量、目录权限、IMAP、代理、Clash 连通性
- **Clash 自动 IP 轮换**：每注册一个账号自动切换代理节点，无需手动操作
- **节点过滤**：可按关键词筛选节点（如只用"美国"节点）
- **自动收码**：通过 IMAP 自动获取 OpenAI 验证码
- **Token 同步**：注册完成后自动同步到 CLI Proxy，Codex/Cursor 即开即用

## 前置准备

### 1. 安装 Docker Desktop

- **macOS**: https://docs.docker.com/desktop/install/mac-install/
- **Windows**: https://docs.docker.com/desktop/install/windows-install/

安装后确保 Docker Desktop 处于运行状态。

### 2. 安装 Clash Verge

安装 [Clash Verge](https://github.com/clash-verge-rev/clash-verge-rev/releases)，导入你的代理订阅，确保能正常访问外网。

**注意事项：**
- 默认代理端口为 `7897`（Clash Verge 默认），如果不同请在 `.env` 中修改
- 建议使用 **Global 模式**，确保所有流量走代理
- **需要启用 External Controller**（用于自动切换节点，见下方说明）

**启用 API Bridge（必须）：**

由于 macOS Docker 容器无法直接访问 Clash 的 Unix Socket，项目自带一个轻量桥接脚本。在启动 Docker 服务之前，先运行：

```bash
python3 clash-bridge.py
```

它会将 Clash 的 Unix Socket API 暴露到 TCP 端口 9090，Docker 容器通过 `host.docker.internal:9090` 即可访问。

终端会显示：
```
[Clash Bridge] /tmp/verge/verge-mihomo.sock → 0.0.0.0:9090
[Clash Bridge] Docker 容器可通过 http://host.docker.internal:9090 访问
```

保持此终端运行即可。如需自定义端口：`python3 clash-bridge.py 8090`，同时修改 `.env` 中的 `CLASH_API_PORT=8090`。

### 3. 准备 Gmail 邮箱

1. 登录 Google 账号 → [安全设置](https://myaccount.google.com/security)
2. 开启**两步验证**（如果还没开）
3. 在顶部搜索栏搜索「专用」，找到并进入 **应用专用密码**
4. 创建一个新的应用专用密码，记下 16 位密码（格式如 `xxxx xxxx xxxx xxxx`）

### 4. 准备域名 + Cloudflare 邮件转发

1. 在 [Namecheap](https://www.namecheap.com) 或 [Cloudflare](https://www.cloudflare.com) 购买一个 `.com` 域名
2. 将域名的 DNS 托管到 Cloudflare
3. 在 Cloudflare 控制台：
   - 进入域名 → **电子邮件** → **电子邮件路由**
   - 启用 Email Routing（Cloudflare 会自动添加 MX 记录）
   - 添加**目标地址**：你的 Gmail 邮箱（需要去 Gmail 点确认链接）
   - 设置 **Catch-All 地址** → 操作选「发送到电子邮件」→ 选你的 Gmail
   - 确保 Catch-All 状态显示「活动」

## 快速部署

### 步骤 1：配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填写 **4 项必填**配置：

```env
MAIL_DOMAIN=your-domain.com          # 你的域名
IMAP_USER=your-email@gmail.com       # Gmail 邮箱
IMAP_PASSWORD=xxxx xxxx xxxx xxxx    # 16位应用专用密码
PROXY_PORT=7897                      # Clash Verge 代理端口
```

可选配置（建议修改）：

```env
CLI_PROXY_API_KEY=sk-your-custom-key   # Codex/Cursor 连接用的 Key
CLI_PROXY_MANAGEMENT_KEY=your-password  # CLI Proxy 管理面板密码
CLASH_API_PORT=9090                     # Clash API 端口
CLASH_API_SECRET=                       # Clash API 密钥（如有）
CLASH_NODE_FILTER=                      # 节点过滤，如 "美国"
```

### 步骤 2：启动 Clash Bridge

确保 Clash Verge 正在运行，然后在项目目录下运行：

```bash
python3 clash-bridge.py
```

保持此终端窗口运行。

### 步骤 3：启动服务

```bash
docker compose up -d
```

首次启动会构建镜像，需要几分钟。启动后会运行两个服务：
- **注册控制台**：http://localhost:8080
- **CLI Proxy**：http://localhost:8317

### 步骤 4：验证部署

浏览器打开 http://localhost:8080 ，页面顶部的**环境状态**面板会显示各项连接状态：

| 指示灯 | 含义 |
|--------|------|
| 域名 | `.env` 中的 MAIL_DOMAIN 是否配置 |
| IMAP | Gmail IMAP 是否连接成功 |
| 代理 IP | 通过代理访问外网获取的出口 IP 和地区 |
| CLI Proxy | CLI Proxy 服务是否正常 |
| Clash | Clash API 是否可用，可用节点数 |
| 当前节点 | Clash 当前选中的代理节点名称 |

所有指示灯为绿色即表示部署成功。

## 使用方法

### 注册账号

1. 打开 http://localhost:8080
2. 在**注册控制**区域：
   - **注册数量**：输入需要注册的账号数（建议每天不超过 5 个）
   - **节点过滤**（可选）：输入关键词筛选 Clash 节点，如填 `美国` 则只用名称含"美国"的节点。留空则使用所有可用节点（自动排除 CN/HK）
3. （可选）点击 **Pre-flight** 按钮，查看逐项检查结果
4. 点击**开始注册**

注册流程会自动执行：
1. **Pre-flight Check** — 检查环境变量、目录权限、IMAP、代理、Clash
2. **切换节点** — 从可用节点列表中选择一个，切换并验证 IP
3. **注册账号** — 提交注册 → 等待验证码 → 验证 → 创建账户 → 获取 Token
4. **保存结果** — Token 保存到本地并同步到 CLI Proxy
5. **休息** — 等待 30-60 秒
6. **切换到下一个节点** — 重复步骤 2-5

实时日志区域会显示每一步的详细进度，包括节点名称和 IP 信息。

### 使用注册的账号

注册完成后，Token 会自动同步到 CLI Proxy，可以直接使用。

#### Codex / Cursor 配置

| 配置项 | 值 |
|--------|------|
| Base URL | `http://localhost:8317/v1` |
| API Key | `.env` 中的 `CLI_PROXY_API_KEY` |
| 模型 | `gpt-5.3-codex` 或其他可用模型 |

#### CLI Proxy 管理面板

访问 http://localhost:8317/management.html ，密码为 `.env` 中的 `CLI_PROXY_MANAGEMENT_KEY`。

## 自动 IP 轮换原理

工具通过 Clash Verge 内置的 mihomo RESTful API 实现自动节点切换：

1. `clash-bridge.py` 将 Clash 的 Unix Socket API 桥接为 TCP 端口（默认 9090）
2. Docker 容器通过 `host.docker.internal:9090` 调用 Clash API
3. 每注册一个账号前，脚本通过 `PUT /proxies/GLOBAL` 切换代理节点
4. 切换后通过 Cloudflare trace 验证出口 IP 已变化且不在 CN/HK 地区
5. 效果等同于你在 Clash Verge GUI 上手动点击切换节点

节点自动过滤规则：排除 DIRECT/REJECT、包含"禁"字、香港/HK 节点。

## 安全建议

- 每天注册不超过 5 个账号
- 每次注册自动使用不同 IP（已内置）
- 不要使用中国大陆或香港的代理节点（已自动过滤）
- 注册超过 20 个账号时，建议使用多个域名分散

## 常用命令

```bash
# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 重启服务
docker compose restart

# 重新构建（修改代码后）
docker compose up -d --build
```

## 故障排查

### 状态面板红灯

| 红灯项 | 可能原因 | 解决方法 |
|--------|---------|---------|
| 域名 | `.env` 未配置 MAIL_DOMAIN | 检查 `.env` 文件 |
| IMAP | Gmail 密码错误或两步验证未开启 | 重新生成应用专用密码 |
| 代理 IP | Clash Verge 未运行或端口不对 | 启动 Clash Verge，检查 PROXY_PORT |
| Clash | API 无法连接 | 确保 `python3 clash-bridge.py` 正在运行 |
| CLI Proxy | 容器未启动 | `docker compose restart cli-proxy` |

### Pre-flight 检查失败

- **目录写权限**：重启容器 `docker restart openai-registrar`
- **代理连通 - 地区不支持**：切换 Clash 节点到非 CN/HK 地区
- **Clash API - 无法连接**：确认 `python3 clash-bridge.py` 正在运行且 Clash Verge 已启动

### IMAP 收码超时

- 检查 Cloudflare 邮件路由的 Catch-All 是否为「活动」状态
- 检查 Gmail 中是否能收到转发的邮件（可能在垃圾箱）
- 验证应用专用密码是否正确（注意去掉空格）

## 目录结构

```
registrar/
├── docker-compose.yml       # Docker 编排文件
├── .env.example             # 配置模板
├── .env                     # 你的配置（不要提交到 Git）
├── clash-bridge.py          # Clash API 桥接脚本（宿主机运行）
├── registrar/               # 注册服务
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py            # Web 后端
│   ├── openai_reg.py        # 注册引擎（含 Clash 控制器）
│   └── static/index.html    # Web 前端
├── cli-proxy/               # CLI Proxy 配置
│   ├── config.yaml
│   └── entrypoint.sh
└── data/                    # 运行数据（自动生成）
    ├── tokens/              # Token 文件
    ├── keys/                # 账号密码
    └── logs/                # 错误日志
```
