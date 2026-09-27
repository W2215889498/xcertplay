# 硬件版远程 MFi 服务（CH341 + MFi 芯片）

把 CH341A + MFi 认证芯片插到服务器上（Linux/树莓派/任意有 USB 的常驻机器），
对外暴露 xcertplay 需要的三个接口，签名由芯片完成，**不需要 Apple 证书文件**（证书在芯片里）。

```
车机 App (MFI target=Remote) ──HTTP──> 本服务 ──USB(pyusb)──> CH341A ──I2C──> MFi 芯片
```

## 1. 文件

| 文件 | 说明 |
|---|---|
| `ch341.py` | CH341A USB→I2C 传输层，移植自 `shared/.../transport/Ch341I2cTransport.kt` + `Ch341I2cStreamEncoder.kt`（32 字节流包、`0xAA/0x74/0x75/0x80/0xC0` 编码、ACK/NACK 状态字节、STOP→START 5ms 间隔） |
| `mfi_chip.py` | MFi 寄存器时序，移植自 `shared/.../mfi/MfiAuthenticationClient.kt`（0x02/0x10/0x11/0x12/0x20/0x21/0x30/0x31…、128 字节证书窗口、签名轮询 0x10=成功） |
| `server.py` | FastAPI 三接口（`type=mfi`）+ `/mfi/health` |

## 2. 接线

- CH341A：SDA/SCL 接 MFi 芯片的对应脚，**VCC 用 3.3V**（CH341A 有的板子可跳 5V/3.3V，MFi 芯片不要接 5V），GND 共地。
- MFi 芯片 I2C 地址默认 **0x11**（本仓库实测 0x10 NACK、0x11 正常），可用 `MFI_I2C_ADDRESS` 覆盖。
- 若你的芯片需要硬件复位（RST），仓库里 `Ch341I2cTransport.pulseActiveLowReset()` 用 CH341 的 D0..D5 GPIO 脉冲实现；本服务默认不做复位，需要的话告诉我我加上。

## 3. 安装与运行

```bash
sudo apt install libusb-1.0-0
python3 -m venv venv && ./venv/bin/pip install pyusb fastapi uvicorn

# 让普通用户能访问 CH341（推荐，避免 root 运行服务）
sudo tee /etc/udev/rules.d/99-ch341-xcertplay.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="5512", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

# 自检（此时应能读到芯片证书）
./venv/bin/python - <<'PY'
from ch341 import Ch341I2c
from mfi_chip import MfiChip
chip = MfiChip(Ch341I2c())
print("protocolMajor:", chip.protocol_major())
print("certificate bytes:", len(chip.certificate()))
print("signature bytes:", len(chip.sign_challenge(bytes(range(32)))))
PY

# 起服务
export MFI_TOKEN=your-secret      # 可选
./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
curl -s localhost:8080/mfi/health
```

常驻建议用 systemd（把 `../mfi-server/deploy/xcertplay-mfi.service` 里的 ExecStart 换成这里的路径即可）。

## 4. App 配置

设置页 → **MFI target = Remote** → **Server address** 填 `http://服务器IP:8080`（车机同内网最稳；跨公网见 `../mfi-server/DEPLOY.md` 的 frp/Caddy 方案）→ Token 可选填 → 保存并重连。

成功日志：`mfi remote service ready server=http://... protocolMajor=2`。

## 5. 说明与注意事项

- 本目录是**照仓库逻辑移植的实现**，与 Android 端寄存器/流包编码逐条对应；我这边没有 CH341+芯片实物，未能实机验证——第一次跑请先用上面的自检脚本确认能读到证书。
- 签名返回的是芯片原始签名（MFi 2.0C 通常 128/256 字节）；`type=mfi` 下客户端会把证书原样用于 iAP2 0xAA01。
- 总线上同一时刻只允许一个事务：服务内部已加锁（FastAPI 可能并发请求）。
- 若 Linux 内核自带 `i2c-ch341` 驱动并把设备暴露成 `/dev/i2c-N`，可以把 `ch341.py` 换成 smbus2 的 `I2C_RDWR` 实现（更简单），协议层 `mfi_chip.py` 不用改。
