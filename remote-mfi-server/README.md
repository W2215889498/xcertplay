# 远程 MFi 鉴权服务（xcertplay Remote 模式配套）

xcertplay 车机端的 `MFI target = Remote` 只需要一个实现三个接口的服务；本目录提供两条可选实现路线。

| 目录 | 路线 | 需要 | 适用 |
|---|---|---|---|
| `soft/` | 软件签名（BAA / Apple 证书） | Apple 签发的叶子证书 + 私钥 + 中间证书 | 有证书、无芯片；可跑在任意常驻机器/云服务器 |
| `hardware/` | 硬件签名（CH341 + MFi 芯片） | CH341A + MFi 认证芯片插在服务端机器上 | 有芯片（证书在芯片内，无需额外证书文件） |

## 接口（两条路线完全一致）

| 方法 | 路径 | 请求 | 成功响应 |
|---|---|---|---|
| GET | `/mfi/certificate` | 无 | `{"protocolMajor":2,"type":"baa"\|"mfi","certificate":"<base64>","certificateSha256":"<64位hex>"}` |
| POST | `/mfi/sign` | `{"challenge":"<base64>","requestId":"<uuid>"}` | `{"signature":"<base64>"}` |
| POST | `/mfi/reset` | `{}` | `{"detail":""}` |

可选 `Authorization: Bearer <token>`；失败返回非 2xx + `{"detail":"..."}`。

## App 端配置

设置 → **MFI target = Remote** → **Server address**（必须 `http://` 或 `https://` 开头）→ **Token (optional)** 与服务端一致 → 保存并重连。

成功后日志：

```
mfi discovery backend=Remote server=http://...
mfi remote service ready server=http://... protocolMajor=2
```

## 选择建议

- 手上有 MFi 芯片（你现在就是 CH341 + 芯片）：直接用 `hardware/`，把芯片插到服务器上即可，不需要任何 Apple 证书文件；
- 只有 Apple 签发的 BAA/DeviceIdentity 证书与私钥：用 `soft/`，部署与 frp/HTTPS 方案见 `soft/DEPLOY.md`；
- 车机没有外网时（无线 CarPlay 热点无上行），服务必须部署在车机可达的内网，或车机走以太网/4G。

## 安全提醒

- 对外暴露务必 HTTPS + Bearer Token + 来源限制；
- 私钥/芯片是认证凭据，服务端机器要控制访问权限。
