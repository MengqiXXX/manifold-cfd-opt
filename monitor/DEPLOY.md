# 监控系统公网部署指南（无 VPN 用户可访问）

这套监控是一个 FastAPI + 静态页面的 Web 服务，默认监听 `8090`，并通过 SSH 采集远端 OpenFOAM 状态。

公网部署要点：
- 监控服务所在机器必须能 SSH 到 OpenFOAM 机器（例如 `192.168.110.10`）。
- 建议只开放 443（HTTPS），并加认证；不要把未鉴权的管理接口暴露到公网。
- WebSocket 路由需要反向代理的 Upgrade 头（`/ws/*`）。

下面给出两种常用方案（推荐优先用 A）。

---

## 方案 A（推荐）：Cloudflare Tunnel（无需公网 IP / 无需开防火墙端口）

适用：OpenFOAM 服务器在内网（例如实验室网段），但你希望外网用户直接访问。

如果你的目标是“外部链接只读、无需登录”，建议：
- 开启 `MONITOR_READONLY=1`
- 不配置 Cloudflare Access（即不登录）
- 但至少用 Cloudflare WAF 做 IP 白名单（可选）

### 1) 在“监控服务所在机器”安装并登录 cloudflared

```bash
sudo apt-get update
sudo apt-get install -y cloudflared
cloudflared tunnel login
```

### 2) 创建 Tunnel 并绑定域名

```bash
cloudflared tunnel create openfoam-monitor
cloudflared tunnel route dns openfoam-monitor monitor.example.com
```

### 3) 配置转发到本地监控端口

创建 `/etc/cloudflared/config.yml`：

```yaml
tunnel: openfoam-monitor
credentials-file: /etc/cloudflared/<your-tunnel-id>.json

ingress:
  - hostname: monitor.example.com
    service: http://127.0.0.1:8090
  - service: http_status:404
```

仓库内也提供了模板文件：`monitor/deploy/cloudflared/config.yml`。

### 4) 以 systemd 启动 tunnel

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### 5)（强烈建议）在 Cloudflare Access 上加登录

Cloudflare Zero Trust → Access → Applications
- 给 `monitor.example.com` 配置邮箱/Google/GitHub 登录
- 或最少配置 One-time PIN

这样外网无需 VPN，但必须登录才可访问。

---

## 方案 B：传统公网部署（Nginx + HTTPS + Systemd）

适用：你有一台具备公网入口的机器（云服务器或带端口映射的网关）。

### 1) 启动监控服务（建议 systemd）

把监控服务跑在 `127.0.0.1:8090`（只给 Nginx 访问）。

示例 `systemd` unit：见 `monitor/deploy/systemd/monitor.service`。

```bash
sudo cp monitor/deploy/systemd/monitor.service /etc/systemd/system/openfoam-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now openfoam-monitor
```

### 2) Nginx 反向代理 + WebSocket

示例 Nginx 配置：见 `monitor/deploy/nginx/monitor.conf`。

```bash
sudo cp monitor/deploy/nginx/monitor.conf /etc/nginx/sites-available/openfoam-monitor
sudo ln -s /etc/nginx/sites-available/openfoam-monitor /etc/nginx/sites-enabled/openfoam-monitor
sudo nginx -t && sudo systemctl reload nginx
```

### 3) HTTPS 证书（Let’s Encrypt）

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d monitor.example.com
```

### 4) 认证（建议放在 Nginx 层）

最简单是 Basic Auth：

```bash
sudo apt-get install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-monitor admin
```

然后在 Nginx 配置里启用 `auth_basic`。

---

## SSH / 环境变量配置

监控服务通过环境变量读取 SSH/LLM/案例目录配置：
- `VORTEX_SSH_HOST` / `VORTEX_SSH_USER` / `VORTEX_SSH_PORT` / `VORTEX_SSH_KEY`
- `VORTEX_REMOTE_CASES_BASE`（例如 `~/manifold_cases`）
- `VORTEX_LLM_BASE_URL` / `VORTEX_LLM_MODEL` / `VORTEX_LLM_API_KEY`

如果你把监控服务部署在 OpenFOAM 同一台机器上，通常只需要：
- `VORTEX_SSH_HOST=127.0.0.1`
- `VORTEX_SSH_USER=<当前用户>`

---

## 安全注意事项（非常重要）

- 不要把 `/api/admin/ssh-exec` 暴露到公网。建议保持默认关闭，必要时加 Token。
- 如果必须公网开放，至少：HTTPS + 强认证（Access/SSO 或 Basic Auth）+ 防火墙只开 443。

---

## 只读公开访问（无需登录）

如果你希望“任何人可打开页面查看，但不能触发诊断/刷新/执行命令”，可以启用只读模式。

### 1) 启用只读模式

设置环境变量：
- `MONITOR_READONLY=1`

Linux + systemd 方式可以直接用仓库里的 unit：`monitor/deploy/systemd/monitor-readonly.service`。

效果：
- 禁用 `POST /api/qwen-diagnose`（不会再触发诊断/启动）
- 禁用 `POST /api/cfd-status/refresh`（只能等自动刷新）
- `/api/admin/ssh-exec` 默认隐藏（除非你显式配置 `MONITOR_ADMIN_TOKEN`）

前端会自动识别只读模式，并把按钮显示为“只读/已禁用”。

### 2) 反向代理层进一步限制（推荐）

即使应用层已只读，也建议在 Nginx/网关层禁止 POST：

```nginx
location /api/ {
  limit_except GET { deny all; }
  proxy_pass http://127.0.0.1:8090;
}
```

---

## 只读外部链接一键清单（Cloudflare Tunnel）

在部署机器（能访问 OpenFOAM 内网的那台）上：

1) 启动只读监控服务（systemd）

```bash
sudo cp monitor/deploy/systemd/monitor-readonly.service /etc/systemd/system/openfoam-monitor-readonly.service
sudo systemctl daemon-reload
sudo systemctl enable --now openfoam-monitor-readonly
```

2) 安装并登录 cloudflared

```bash
sudo apt-get update
sudo apt-get install -y cloudflared
cloudflared tunnel login
```

3) 创建 tunnel 并绑定域名

```bash
cloudflared tunnel create openfoam-monitor
cloudflared tunnel route dns openfoam-monitor monitor.example.com
```

4) 写入 `/etc/cloudflared/config.yml` 并启动

```bash
sudo mkdir -p /etc/cloudflared
sudo cp monitor/deploy/cloudflared/config.yml /etc/cloudflared/config.yml
sudo sed -i 's/monitor.example.com/你的域名/g' /etc/cloudflared/config.yml
sudo sed -i 's#credentials-file:.*#credentials-file: /etc/cloudflared/openfoam-monitor.json#g' /etc/cloudflared/config.yml

sudo cp ~/.cloudflared/*.json /etc/cloudflared/openfoam-monitor.json
sudo systemctl enable --now cloudflared
```

如果你希望用自定义 systemd unit（固定 config 路径），仓库也提供了：`monitor/deploy/systemd/cloudflared-tunnel.service`。
