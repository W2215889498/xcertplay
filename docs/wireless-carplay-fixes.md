# 无线 CarPlay 修复报告：热点自动加入、周期性重连、视频卡帧

> 测试环境：泰山派 3M（RK3576 / Android 14，无线 CarPlay 接收端）
> 手机：iPhone（iOS 14.7.1 → 升级 iOS 16）
> 项目版本：xcertplay v1.2.4（HEAD `8885cba`）

本次共定位并修复三个独立问题，设备实测全部通过：

| # | 现象 | 根因 | 状态 |
|---|------|------|------|
| 1 | iPhone 不自动加入车机热点，每次手动点 | 热点 SSID/密码每次启动随机生成 | 已修复 |
| 2 | 约 45 秒自动断连、反复重连 | Wi-Fi iAP 隧道永远不出现，45s 看门狗拆掉整个栈 | 已修复 |
| 3 | 视频只出 1~3 帧就卡死（音频正常），重连后尤甚；软解有马赛克 | App 在解码器输入缓冲超时时**静默丢帧**，打断参考链 | 已修复 |

---

## 问题 1：iPhone 不会自动加入热点

### 现象

无线 CarPlay 每次都需要在 iPhone 上手动点选热点才能入网，不会自动连接。

### 根因

`WifiP2pGroupManager.start()` 每次启动都调用 `randomCredentials()` 生成新的 SSID/密码：

```
启动 1: ssid=DIRECT-xcgJLe passphrase=...   (channel 149)
启动 2: ssid=DIRECT-xcDEv6 passphrase=...   (channel 161)
```

SSID 每次都变，iOS 永远没有“已保存的已知网络”可以自动加入，只能弹窗手动确认。

### 修复

将凭据改为首次生成后持久化（`SharedPreferences`），此后每次启动复用同一组 SSID/密码。

```kotlin
// shared/.../network/WifiP2pGroupManager.kt
private fun stableCredentials(): Credentials {
    val preferences = appContext.getSharedPreferences(CREDENTIALS_PREFS, Context.MODE_PRIVATE)
    val storedSsid = preferences.getString(KEY_SSID, null)
    val storedPassphrase = preferences.getString(KEY_PASSPHRASE, null)
    if (!storedSsid.isNullOrBlank() && !storedPassphrase.isNullOrBlank()) {
        return Credentials(storedSsid, storedPassphrase)
    }
    val generated = Credentials(
        ssid = "DIRECT-xc${randomToken(4)}",
        passphrase = randomToken(16),
    )
    preferences.edit()
        .putString(KEY_SSID, generated.ssid)
        .putString(KEY_PASSPHRASE, generated.passphrase)
        .apply()
    return generated
}
```

### 验证

设备日志：连续多次启动 SSID 均为 `DIRECT-xc2oyt`；持久化文件确认：

```xml
<string name="wifi_p2p_ssid">DIRECT-xc2oyt</string>
<string name="wifi_p2p_passphrase">Q5Ugb8AabJKcggj5</string>
```

iPhone 现在可自动入网（用户实测确认）。

---

## 问题 2：每约 45 秒自动断连重连

### 现象

连接后约 45 秒，AirPlay 会话被拆掉并重新建立，循环往复；日志里 `AirPlay session active` 与 `handoff timed out` 交替出现。

### 根因

无线 CarPlay 收到 `disableBluetooth` 后进入 handoff，等待 iPhone 建立 type-130 iAP 隧道：

```
wireless CarPlay Bluetooth handoff requested; waiting for tunnel iAP2 readiness
...
wireless RFCOMM EOF: iapState=POST_TRANSPORT_WIFI_CONFIG_SENT
    handoffRequested=true tunnelReady=false
ERROR Wireless CarPlay handoff timed out waiting for tunnel iAP2
```

实测 iOS 14 与 iOS 16 **都不会**发起 type-130 `SETUP`（`onDataStream` 从未被调用）；45 秒看门狗随后 `closeWirelessStack()` + `fail()`，把正在工作的会话拆掉重连。蓝牙 iAP2 实际上一直在正常工作（RFCOMM 持续收到 `PowerUpdate`），隧道只是可选路径。

### 修复

`armWirelessHandoffWatchdog` 超时后不再拆栈，只记录日志；隧道若出现仍走原有握手路径。

```kotlin
// shared/.../orchestration/CarPlayController.kt
// The Wi-Fi iAP tunnel is optional: older iPhones keep iAP2 on Bluetooth and
// never open the type-130 channel. Tearing the session down here killed a
// working CarPlay session, so only report the timeout and keep the current
// transport running.
debugLog(
    "wireless handoff tunnel did not appear within " +
        "${WIRELESS_HANDOFF_TIMEOUT_MILLIS}ms; keeping the Bluetooth iAP2 session",
)
```

另：`0x5703` 现在支持可选 BSSID（`Iap2WirelessCarPlayEndpoint.bssid`），并过滤 Android 的占位地址（`00:00:00:00:00:00` / `02:00:00:00:00:00`）。

### 验证

修复后会话不再被看门狗拆掉，AirPlay 控制连接持续保活（cseq 持续递增），直到对端主动结束。

---

## 问题 3：视频只解码 1~3 帧就卡住

### 现象

- 画面停在某一帧（音频正常）；触摸有转发（`airplay touch report sent`）但画面不更新；
- 硬解时 `C2RKMpiDec` 持续输出 `skip error frame`，`input frames` 持续增长而 `output frames` 停在第 3 帧；
- 软解能出画面但有大片马赛克、不流畅。

### 定位过程

1. **加每帧埋点**：记录 `input/output frames`、`input unavailable`（丢帧）、`oversized`、分帧转换失败。
2. **dump 喂给解码器的原始码流**（AnnexB，文件头写入 VPS/SPS/PPS），用 ffmpeg 离线分析。
3. 对比结论：

| 证据 | 结果 |
|------|------|
| `video decoder input unavailable queued=3 dropped=1` | 会话开头第 4 帧被丢 |
| `C2RKMpiDec: skip error frame`（同会话） | 1300+ 条，输出停在 3 帧 |
| `ffmpeg -v error -i failing.h265 -f null -` | **0 错误** |
| `ffprobe -count_frames` | **1313 帧全部解出** |
| `video frame is not annexB` / `oversized` | 0（分帧与解密正确） |

即：**码流本身完好**，但 App 在解码器尚未归还输入缓冲（`dequeueInputBuffer(10ms)` 超时）时把帧丢了。手机静止画面时首帧 IDR 之后只发 P 帧、不再发新 IDR，丢一帧即打断参考链，后续全部解不出来——硬解报 `skip error frame`，软解错误隐藏成马赛克。

### 原始代码（HEAD `8885cba`，`AndroidMediaSink.VideoDecoder.feed()`）

```kotlin
if (annexB.isEmpty()) return
val index = codec.dequeueInputBuffer(INPUT_TIMEOUT_US)   // 10ms
if (index < 0) return                                    // ← 静默丢帧
```

### 修复

等不到输入缓冲时不再丢帧：最多等待 1 秒，期间持续排空输出（输出堆积也会导致输入缓冲不归还），仅在真正超时才丢弃并告警。

```kotlin
if (annexB.isEmpty()) return
var index = codec.dequeueInputBuffer(INPUT_TIMEOUT_US)
val inputDeadline = System.nanoTime() + INPUT_WAIT_NANOS   // 1s
while (index < 0 && running && System.nanoTime() < inputDeadline) {
    drainOutput(codec)
    index = codec.dequeueInputBuffer(INPUT_TIMEOUT_US)
}
if (index < 0) {
    inputDropped++
    if (inputDropped == 1 || inputDropped % VIDEO_FRAME_LOG_INTERVAL == 0) {
        Log.w(TAG, "video decoder input unavailable queued=$inputFrames dropped=$inputDropped")
    }
    return
}
```

### 验证

| 会话 | 结果 |
|------|------|
| 首连 | `input == output`，0 丢帧，`skip error frame` 0 条 |
| 修复前重连 | `input=1740` / `output=3`，1300+ `skip error frame` |
| **修复后重连** | **`input == output == 7380`，0 丢帧，0 报错，连续运行 12 分钟以上（用户确认画面/交互正常）** |

---

## 诊断能力（随修复一并加入，便于复现同类问题）

- `video decoder input unavailable queued=.. dropped=..`：丢帧告警（本问题就是靠它定位）；
- `video decoder input/output frames=N`：每 60 帧计数，快速判断“有输入没输出”；
- **视频码流 dump**（需在 App 设置里开启 debug 日志）：每个解码配置生成一份
  `files/video-dump/video-<streamType>-<ts>.h26x`，文件头含 VPS/SPS/PPS，可直接用 ffmpeg/ffprobe 离线分析，单文件上限 8MB。

---

## 已知残留问题（非 App 可修，建议上游/厂商跟进）

1. **RK 硬解对重连码流的兼容性**：修复丢帧后，仍观察到个别重连会话 `c2.rk.hevc.decoder` 报 `skip error frame`（无 App 侧丢帧），而同一条 dump 用软解/ffmpeg 可完整解码 → 疑似 RK MPP 硬件解码器缺陷；当前可用 `HEVC software decoder` 规避（画面可能有短暂马赛克）。
2. **H.264 硬解**：`c2.rk.avc.decoder` 在同一设备上首连即报 421 条 `skip error frame`、输出停在第 2 帧；HEVC 路径正常。建议保持 HEVC。

---

## 变更文件

| 文件 | 内容 |
|------|------|
| `shared/.../network/WifiP2pGroupManager.kt` | SSID/密码持久化 |
| `shared/.../orchestration/CarPlayController.kt` | handoff 看门狗不再拆栈；0x5703 BSSID 支持与占位过滤 |
| `shared/.../transport/Iap2WirelessControlClient.kt` | endpoint 可选 `bssid` |
| `shared/.../media/AndroidMediaSink.kt` | 不丢帧修复、丢帧告警、帧计数、debug 视频 dump |
| `shared/.../airplay/ScreenStream.kt` | 非 AnnexB 帧告警（诊断） |
| `common/.../CarPlayHostActivity.kt` | debug 下启用视频 dump 目录 |

## 质量检查（本机已全部通过）

```bash
./gradlew :shared:testDebugUnitTest :common:lintDebug :mobile:lintDebug \
          :automotive:lintDebug :mobile:assembleDebug :automotive:assembleDebug
# BUILD SUCCESSFUL
```
