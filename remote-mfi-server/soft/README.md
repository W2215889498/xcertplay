# 远程 MFi/BAA 鉴权服务器（示例）

对应客户端实现：`shared/.../mfi/RemoteMfiAuthenticationClient.kt`

## 1. 协议（服务端要实现的三条接口）

| 方法 | 路径 | 请求 | 成功响应 |
|---|---|---|---|
| GET | `/mfi/certificate` | 无 | `{"protocolMajor":2,"type":"baa","certificate":"<base64>","certificateSha256":"<64位hex>"}` |
| POST | `/mfi/sign` | `{"challenge":"<base64>","requestId":"<uuid>"}` | `{"signature":"<base64>"}` |
| POST | `/mfi/reset` | `{}` | 任意 2xx（`{"detail":""}` 即可） |

约束（客户端会校验）：

- `certificate` 解码后 1..65525 字节；`certificateSha256` 必须是解码后字节的 SHA-256（hex，64 字符）。
- `type` 取 `"mfi"`（缺省）或 `"baa"`：
  - `baa`：`certificate` 是 BAA 包 —— `[u32 BE 叶子证书长度][u32 BE 中间证书长度][叶子 DER][中间 DER]`；
  - `mfi`：`certificate` 就是芯片证书原文。
- `challenge` 解码后 1..128 字节；`signature` 非空且 ≤65525 字节。
- 可选鉴权：`Authorization: Bearer <token>`（App 设置里 "Token (optional)"）。
- 失败时返回非 2xx + `{"detail":"原因"}`；客户端对 `/mfi/sign`、`/mfi/certificate` 会自动重试（429/5xx/超时）。
- `/mfi/reset` 在每次开始认证前调用（客户端缓存失效）。

## 2. 签名怎么做

- **BAA（README 说唯一验证过的路径）**：Apple 签发的 ECDSA P-256 叶子证书 + 私钥；对 challenge 做 **ECDSA-SHA256**，输出 **ASN.1 DER（X9.62）** 再 base64。
  证书链要能过 iPhone 的校验，所以必须是 Apple 正式签发的 BAA/DeviceIdentity 证书（参考项目 `amineross/showcase` 的做法：用一个带 entitlement 的进程拿 DeviceIdentity 证书，私钥不出进程）。
- **MFi 芯片路径**：芯片证书（通常 RSA-2048）+ 私钥；签名用 **SHA256withRSA / PKCS#1 v1.5**。多数 MFi 芯片私钥不可导出，这条路径 README 未验证。

## 3. 启动

```bash
pip install fastapi uvicorn cryptography

export MFI_TYPE=baa
export MFI_LEAF_DER=leaf.der
export MFI_INTERMEDIATE_DER=intermediate.der
export MFI_KEY_PEM=leaf-key.pem
export MFI_TOKEN=your-secret        # 可选

# 开发：HTTP
uvicorn server:app --host 0.0.0.0 --port 8080

# 生产：HTTPS（自签证书 Android 默认不信任；要么用受信任证书/域名，要么在设备上安装 CA）
uvicorn server:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile server.key --ssl-certfile server.crt
```

## 4. App 端配置

设置 → MFI target 选 **Remote** → 填服务器地址（必须以 `http://` 或 `https://` 开头）→ 可选填 Token → 保存并重连。

启动日志应出现：

```
mfi discovery backend=Remote server=https://...
mfi remote service ready server=https://... protocolMajor=2
```

## 5. 注意

- 证书必须能被 iPhone 接受（Apple 签发的 BAA/MFi 证书），自签证书不会通过 iPhone 的认证；证书与私钥的申请/使用需遵守 Apple 的 MFi/BAA 条款。
- 服务器持有私钥，务必：HTTPS + 强 Token + 网络访问控制；`/mfi/sign` 只做签名，不要暴露私钥。
- 多台车机可共用同一台鉴权服务器（这正是 Remote 模式的意义）。
