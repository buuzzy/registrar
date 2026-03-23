# OpenAI 批量注册工具 V0.5

一键部署的 OpenAI 账号批量注册 + API 代理工具，通过 Web 控制台操作。

## 前置准备

在开始之前，你需要准备以下内容：

### 1. 安装 Docker Desktop

- **macOS**: https://docs.docker.com/desktop/install/mac-install/
- **Windows**: https://docs.docker.com/desktop/install/windows-install/

安装后确保 Docker Desktop 处于运行状态。

### 2. 安装代理工具

安装 [Clash Verge](https://github.com/clash-verge-rev/clash-verge-rev/releases) 或其他代理工具，确保能正常访问外网。

默认代理端口为 `7897`（Clash Verge 默认），如果不同请在 `.env` 中修改。

### 3. 准备 Gmail 邮箱

1. 登录 Google 账号 → [安全设置](https://myaccount.google.com/security)
2. 开启**两步验证**（如果还没开）
3. 进入 [应用专用密码](https://myaccount.google.com/apppasswords)
4. 创建一个新的应用专用密码，记下 16 位密码（格式如 `xxxx xxxx xxxx xxxx`）

### 4. 准备域名 + Cloudflare 邮件转发

1. 在 [Namecheap](https://www.namecheap.com) 或 [Cloudflare](https://www.cloudflare.com) 购买一个 `.com` 域名
2. 将域名的 DNS 托管到 Cloudflare
3. 在 Cloudflare 控制台：
   - 进入域名 → **电子邮件** → **电子邮件路由**
   - 启用 Email Routing（CloudFlare 会自动添加 MX 记录）
   - 添加**目标地址**：你的 Gmail 邮箱（需要去 Gmail 点确认链接）
   - 设置 **Catch-All 地址** → 操作选「发送到电子邮件」→ 选你的 Gmail
   - 确保 Catch-All 状态显示「活动」

## 快速部署

### 步骤 1：下载项目

将本目录复制到你的电脑上。

### 步骤 2：配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填写 4 项必填配置：

```env
MAIL_DOMAIN=your-domain.com          # 你的域名
IMAP_USER=your-email@gmail.com       # Gmail 邮箱
IMAP_PASSWORD=xxxx xxxx xxxx xxxx    # 16位应用专用密码
PROXY_PORT=7897                      # 代理端口
```

可选配置（建议修改）：

```env
CLI_PROXY_API_KEY=sk-your-custom-key   # Codex/Cursor 连接用的 Key
CLI_PROXY_MANAGEMENT_KEY=your-password  # CLI Proxy 管理面板密码
```

### 步骤 3：启动服务

```bash
docker compose up -d
```

首次启动会构建镜像，需要几分钟。

### 步骤 4：打开控制台

浏览器访问：**http://localhost:8080**

1. 点击「刷新检查」确认所有状态为绿色
2. 输入注册数量，点击「开始注册」
3. 观察实时日志，等待注册完成

## 使用注册的账号

注册完成后，Token 会自动同步到 CLI Proxy。

### Codex / Cursor 配置

| 配置项 | 值 |
|--------|------|
| Base URL | `http://localhost:8317/v1` |
| API Key | `.env` 中的 `CLI_PROXY_API_KEY` |
| 模型 | `gpt-5.3-codex` 或其他可用模型 |

### CLI Proxy 管理面板

访问 http://localhost:8317/management.html ，密码为 `.env` 中的 `CLI_PROXY_MANAGEMENT_KEY`。

## 安全建议

- 每天注册不超过 5 个账号
- 每次注册前切换代理节点（不同国家）
- 不要使用中国大陆或香港的代理节点
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

## 目录结构

```
V0.5/
├── docker-compose.yml       # Docker 编排文件
├── .env.example             # 配置模板
├── .env                     # 你的配置（不要提交到 Git）
├── registrar/               # 注册服务
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py            # Web 后端
│   ├── openai_reg.py        # 注册引擎
│   └── static/index.html    # Web 前端
├── cli-proxy/               # CLI Proxy 配置
│   ├── config.yaml
│   └── entrypoint.sh
└── data/                    # 运行数据（自动生成）
    ├── tokens/              # Token 文件
    ├── keys/                # 账号密码
    └── logs/                # 错误日志
```
