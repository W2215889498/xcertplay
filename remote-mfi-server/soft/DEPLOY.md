# 部署远程 MFi/BAA 服务

## 拓扑选择

```
方案 A：车机 →(公网)→ 云服务器上的签名服务（需要域名 + TLS）
   车机(以太网/4G 上行) ─── https://mfi.example.com ───> Caddy ──> 127.0.0.1:8080 (uvicorn)

方案 B：签名服务在内网（放证书的机器），云服务器只做 frp 中转
   车机 ─── http(s)://云IP:18080 ───> frps(云服务器) ══frp隧道══> frpc(内网主机) ──> 127.0.0.1:8080

方案 C：车机与服务器同内网（最简单、最稳）
   车机 ─── http://192.168.x.x:8080 ───> 签名服务
```

无线 CarPlay 时车机的 Wi-Fi 在做热点、没有外网，所以：

- 车机有以太网口/4G 上行 → 方案 A/B 可用；
- 车机完全离线 → 只能方案 C，或把签名服务跑在同一板子上（那就失去“远程”的意义，不如直接插 MFi 芯片）。

## 云服务器部署（方案 A）

```bash
sudo useradd -r -s /usr/sbin/nologin xcertplay
sudo mkdir -p /opt/xcertplay-mfi/secrets
sudo chown -R xcertplay:xcertplay /opt/xcertplay-mfi
# 上传 server.py 与证书到 /opt/xcertplay-mfi/，证书放 secrets/（chmod 600）
cd /opt/xcertplay-mfi
python3 -m venv venv && ./venv/bin/pip install fastapi uvicorn cryptography
sudo cp deploy/xcertplay-mfi.service /etc/systemd/system/
sudo systemctl enable --now xcertplay-mfi
systemctl status xcertplay-mfi
curl -s localhost:8080/mfi/certificate | head -c 200
```

TLS 用 Caddy（自动 Let's Encrypt）：把 `deploy/Caddyfile` 放到 `/etc/caddy/Caddyfile`，`systemctl reload caddy`。

## frp 中转（方案 B）

1. 云服务器：装 `frps`，配置 `bindPort=7000` + `auth.token`，开放 7000/tcp 与 18080/tcp；可选再放 Caddy 把域名 TLS 反代到 `127.0.0.1:18080`，这样 App 里能用 `https://`。
2. 内网主机（放证书的机器）：装 `frpc`，用 `deploy/frpc.toml`，把本机 8080 暴露为云服务器的 18080。
3. 自测：`curl -s http://云IP:18080/mfi/certificate | head -c 200`。

## App 配置

设置页 → **MFI target = Remote** → **Server address** 填：

- 方案 A：`https://mfi.example.com`
- 方案 B/直连：`http://云IP:18080` 或 `http://192.168.x.x:8080`

→ **Token (optional)** 填与服务端 `MFI_TOKEN` 一致的值 → 保存并重连。

成功日志：

```
mfi discovery backend=Remote server=https://mfi.example.com
mfi remote service ready server=https://mfi.example.com protocolMajor=2
```

失败时会看到 `Remote MFI request failed ...` 或 `HTTP 401/...`，同时 CarPlay 不会启动（认证阶段就失败了）。

## 排查清单

| 现象 | 可能原因 |
|---|---|
| `HTTP 401` | Token 不一致（服务端设了、App 没填或填错） |
| 连接超时 / 不可达 | 车机没有上行网络；安全组/防火墙没放行；frp 未连通 |
| `certificateSha256 does not match` | 服务端返回的哈希算的不是解码后的 `certificate` 字节 |
| iPhone 侧认证失败（本地日志无 HTTP 错误） | 证书不是 Apple 签发的有效 BAA/MFi 证书，或证书链不完整 |
| 每次启动都失败 | 服务没设为开机自启/挂了（客户端在 MFI 阶段强依赖它） |
