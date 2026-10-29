# Monet 3.0.0 (co.appnation.artgenerator) — 逆向 R1

> 样本：`monet-v3_0_0-mod-apkvision.apk`（108.4 MB，APKVision 分流版，已重签）
> 结论日期：2026-10-29 · 状态：**协议全还原 / 生成链路被 App Check 硬阻塞 / 本地网关+demo+压测已交付**

---

## 0. 一句话结论

Monet 3.0.0 的协议与 3.5.5 几乎同构（同 Firebase 项目、同 Cloud Function、同请求字段），
但 **3.0.0 这个版本根本没接 App Check**（Dart 侧无 `firebase_app_check` 依赖、无 `X-Firebase-AppCheck` 头），
而上游 `v2app` / `v2test` 两个函数**当前都以 App Check enforcement 在跑**。
所以：3.0.0 比 3.5.5 **更不可能打通**真实生成 —— 换老版本并不能绕过 App Check，反而连 token 都拿不到。

本地可交付的部分（协议客户端、OpenAI 兼容网关、demo 页、压测、浏览器 E2E）已全部落地并跑通。

---

## 1. 样本信息

| 项 | 值 |
|---|---|
| 文件 | `monet-v3_0_0-mod-apkvision.apk` (108.4 MB) |
| package | `co.appnation.artgenerator` |
| versionName / versionCode | `3.0.0` / `95` |
| compileSdk / minSdk / targetSdk | 34 / 26 / 34 |
| ABI | arm64-v8a only（`libapp.so` 12.69 MB、`libflutter.so` 10.11 MB） |
| 框架 | Flutter，Dart **3.4.3** (`libflutter.so` 版本串) |
| 主入口逻辑 | `libapp.so`（Dart AOT），反编译：blutter → `blutter_out/asm/` |
| 加固 | 无 DexProtector/pairip 类加固（对比：同目录 3.5.5 带 `libpairipcore.so`，本包**没有**） |
| 签名 | APKVision 重签（`META-INF` 证书非原厂；dex 内无 apkvision 注入串） |
| FFmpeg | `libffmpegkit.so` + `libavcodec` 等（视频生成/转码链路） |

产物目录：`D:\apk-analysis\monet300\`

---

## 2. 上游架构

```
                    ┌──────────────────────────────────────────────┐
   Firebase Auth ──►│ identitytoolkit.googleapis.com (apiKey)      │ 匿名 idToken
   (无 App Check)   └──────────────────────────────────────────────┘
                                   │ Authorization: Bearer <idToken>
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ Cloud Functions (v1 / Express)   us-central1-art-generator-e907c     │
   │   /v2app   ← 生产    /v2test ← 测试   ★ App Check enforcement 开启   │
   │   /generate_without_lora  /generate_with_lora  /generate_with_illusion│
   │   /generate_video  /nsfw_detection  /upscale  /status                │
   │   /firebase/save_user_art[_log]  /firebase/set_user_art_publish_request│
   │   /firebase/like_or_unlike_user_art  ← 3.0.0 独有                    │
   └──────────────────────────────────────────────────────────────────────┘
                                   │ 出图真身
                                   ▼
                          fal.ai（Remote Config 自述："sent to fal.ai"）
                 modelName = SG161222/RealVisXL_V3.0，23 种风格 / 12 条 LoRA

   ┌──────────────────────────────────────────────────────────────────────┐
   │ Cloud Run   management-api-production-vfywoevzkq-uc.a.run.app        │
   │   /v1/rate  /v1/feedback  ← devkit 通用服务，非生成链路               │
   │   无 App Check，改用 X-App-Id 白名单（见 §4.3）                        │
   └──────────────────────────────────────────────────────────────────────┘
```

Firebase 项目常量（APK 内明文）：

| 常量 | 值 |
|---|---|
| project_id | `art-generator-e907c` |
| google_api_key | `AIzaSyABB3fgxYajNGJv40sqkbR5ZXni72TlbOQ`（公开 key） |
| google_app_id | `1:238482661076:android:11aba563876e0276246541` |
| storage bucket | `art-generator-e907c.appspot.com` |
| default_web_client_id | `238482661076-qik1k3vjib6eok7fpht8ob5j8hecvhrk.apps.googleusercontent.com` |
| facebook_app_id | `716105643156585` |

---

## 3. 鉴权链（实测链路，全绿到 auth，卡在函数网关）

```http
POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<API_KEY>
{"returnSecureToken":true,"clientType":"CLIENT_TYPE_ANDROID"}     → 200 idToken ✅
```
随后每个业务请求带：

| Header | 值 | 来源 |
|---|---|---|
| `Authorization` | `Bearer <firebase idToken>` | `an_http_client.dart` |
| `X-App-Id` | `co.appnation.artgenerator` | 同上 |
| `X-App-Platform` | `android` | 同上 |
| `X-App-Version` | `3.0.0` | `package_info` |
| `X-Firebase-AppCheck` | **3.0.0 完全不发送** | 3.5.5 才有 |

匿名 token 拿到后所有业务路由一律在 Google 前端被拦，见 §4。

---

## 4. App Check 硬阻塞（证据链）

### 4.1 服务端行为（`evidence/appcheck_matrix.txt`）

| 探测 | 结果 |
|---|---|
| `POST /v2app/status` 无 AppCheck | `401 Unauthorized`（纯文本） |
| `POST /v2app/status` 伪 token | `503 Service Unavailable` |
| `POST /v2app/definitely_not_a_route` 无 AppCheck | `401`（**不存在的路由也 401** → 拦截发生在 Express 之前） |
| `POST /v2app/generate_without_lora` | `401 / 503` |
| `POST /v2test/*` | 完全相同的 401/503 |
| X-App-Version 改 3.0.0 / 3.5.5 | 无差别 |

### 4.2 函数名枚举（排除"换个函数名"的可能）

`/v1 /v2 /api /app /v2prod /v3app /v2api /generate /webhook /admin` → 全部 GFE HTML 404；
只有 `/v2app`、`/v2test` 存在，且都被 App Check 拦住。

### 4.3 3.0.0 为什么更糟（静态证据）

| 证据 | 3.0.0 | 3.5.5 |
|---|---|---|
| Dart `firebase_app_check` 包 | **不存在**（`blutter_out/asm/` 无该目录，`pp.txt` 仅 1 处 `usesAppCheck` 配置位） | 有 |
| `an_app_check_interceptor.dart` | **不存在** | 有（`FirebaseAppCheck.instance.getToken()` → `X-Firebase-AppCheck`） |
| 字面量 `X-Firebase-AppCheck` | **无** | 有 |
| 原生插件 `io/flutter/plugins/firebase/appcheck` | 有（传递依赖进来的 AAR，但 Dart 侧从不调用） | 有 |

结论：**即便用真机跑 3.0.0，也不会向 /v2app 附加 App Check token，一样 401。**
线上 Remote Config 的 `reviewerOptionsV2.version` 已是 `4.3.2`，说明 3.0.0 属于被强制升级掉的老版本。

### 4.4 旁证：只要是 Firebase 托管面，都收紧

| 面 | 状态 |
|---|---|
| Firebase Remote Config（`namespaces/firebase:fetch`） | **无 App Check，可匿名拉取**（拿到 54 个键，见 §5） |
| Cloud Functions `v2app` / `v2test` | App Check enforcement |
| Firestore REST | 403（3.5.5 时代同样） |
| Firebase Storage（带 download token 的公开对象） | 可直读（风格图 / LoRA / banner） |

---

## 5. Remote Config 公开面（本轮新增产出）

用 apiKey + 随机 `app_instance_id` 直接 `firebase:fetch` → **200，54 个键**，落盘 `rc_current.json`。
关键项：

| key | 当前值 | 说明 |
|---|---|---|
| `artStylesV2` | 23 条 JSON | 风格全表 → `styles.json`（12 条带 LoRA） |
| `freeArtLimit` | `5` | 免费额度 |
| `isLoginSystemEnabled` | `true` | 现强制登录（3.0.0 时代为 false） |
| `creditLimit` | `{"ios":0,"android":0}` | 客户端不做限制 |
| `generateCoolDownInSeconds` | `60` | 生成冷却 |
| `reviewerOptionsV2` | 含审核账号 email/密码，`version: 4.3.2` | **公开面泄露**，公开仓库中已 REDACTED |
| `masterLoginEmails` | 3 个 @appnation.co 地址 | 内部白名单 |
| `aiPermissionConfig` | 文案自述数据送往 **fal.ai** | 上游真身佐证 |
| `artApiList` | DALL-E / Stable Diffusion / Midjourney / Dreamshaper | 历史 provider 列表（旧版 UI） |

> `artStylesV2` 里 `foodDesign` **重复出现两次**（一条带 LoRA、一条 premium），网关按 key 去重 → 22 个唯一风格。

---

## 6. 端点与字段

完整逐字段规格见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。要点：

- 生成 3 条路由差别只在**是否带 `loras` / `imageUrl`**，字段全集：
  `prompt, numImages, width, height, modelName, stylePrompt, negativePrompt, promptStrength, imageUrl, loras[{path,scale}], seed, steps`
- 3.0.0 独有：`POST /firebase/like_or_unlike_user_art` `{userArtId, isLike}`（3.5.5 无）
- 3.5.5 的 `/v1/rate`、`/v1/feedback` 在 3.0.0 里搬到了 Cloud Run `management-api-*`（`/v1/rate` 服务端仍存在）

---

## 7. 交付物与复现

| 文件 | 作用 |
|---|---|
| `monet_client.py` | 原生协议客户端：匿名 auth + 全部路由 + `MockMonetClient` 离线替身 |
| `demo_server.py` | OpenAI 兼容网关：`/v1/images/generations`、`/v1/models`、`/health`、`/stats`、`/mock/*.png`，模式 `real/auto/mock` |
| `demo.html` | 可视化 demo 页（风格下拉、n/size、结果网格、实时 stats） |
| `stress_test.py` | 本地压测：并发阶梯 / 图片字节校验 / `/health` 吞吐 |
| `browser_test.py` | Playwright 真浏览器 E2E（点击→出图→断言 naturalWidth→截图） |
| `styles.json` | 当前 `artStylesV2` 全量（23 条） |
| `probe_appcheck.py` | 复现 §4 全部证据 |
| `evidence/` | 探针原始输出、压测原始输出、截图 |

```bash
# 1) 环境
pip install playwright pillow && python -m playwright install chromium

# 2) 网关（mock 模式，零上游流量）
python demo_server.py --mode mock --port 8951
#    浏览器打开 http://127.0.0.1:8951/

# 3) 压测
python stress_test.py -n 200 --ramp 1,4,8,16,32 --verify-image
python stress_test.py -n 2000 -c 32 --health-bench

# 4) 浏览器 E2E
python browser_test.py --url http://127.0.0.1:8951

# 5) 复现阻塞证据
python probe_appcheck.py

# 6) 真机 token 逃逸口（有 Play Integrity 真机时才可能）
set MONET_APPCHECK_TOKEN=<device token>
python demo_server.py --mode real --port 8971
```

---

## 8. 压测结果（本机，mock 模式，0 上游流量）

`evidence/bench_generation.txt` / `bench_health.txt`：

| 并发 | 请求 | RPS | p50 | p95 | p99 | 失败 |
|---|---|---|---|---|---|---|
| 1 | 200 | 57.3 | 0.8 ms | 1.0 ms | 1.1 ms | 0 |
| 4 | 200 | 126.0 | 1.1 ms | 2.4 ms | 9.7 ms | 0 |
| 8 | 200 | 124.6 | 7.3 ms | 26.5 ms | 33.0 ms | 0 |
| 16 | 200 | 133.8 | 26.3 ms | 82.0 ms | 614.6 ms | 0 |
| 32 | 200 | 140.1 | 19.8 ms | 592.0 ms | 1065.1 ms | 0 |
| 32 (`/health`) | 2000 | **2809.2** | 2.2 ms | 5.7 ms | 13.8 ms | 0 |

- 1000 次生成请求 **0 失败**，每张图都做了 PNG magic + 字节校验。
- c≥16 后 p99 抬到 0.6–1.1 s：瓶颈是本机 Python 网关（GIL + PIL 出图 + 每次响应的图片 GET 校验），
  不是协议层；真实上游延迟在秒级（fal.ai），本表仅代表**网关自身开销**。
- 健康探针 2809 rps @ c=32 说明 HTTP 层不是瓶颈。

三种模式的实测行为：

| 模式 | 结果 |
|---|---|
| `mock` | 200 + 真实 PNG 字节，`X-Monet-Mode: mock` |
| `auto` | 先打真实（≈6.9 s 完成匿名 auth + 401），自动降级 mock，`/stats.last_error` 记录 `real failed (401), served mock` |
| `real` | `503 upstream_app_check_required`（错误映射干净，不假装成功） |

浏览器 E2E（`browser_demo.png`）：9 项断言全 PASS —— 标题/模式徽标/风格列表 23 项/健康面板/两张结果/图片 naturalWidth=512/日志 200 OK/统计递增。
（注：headless Chromium 在 12–13px 中文上有一处字形描边伪影，已用"同字符 4 种 CSS 变体渲染一致 + computed `text-decoration: none`"排除是页面缺陷，真实浏览器不受影响。）

---

## 9. 结论与下一步选项

**已确认（有实测证据）**：3.0.0 协议 100% 还原；生成链路被 App Check enforcement 阻断；
3.0.0 甚至不具备产出 token 的能力；换函数名/换版本号/伪 token 全部无效。

**可选推进路线**（需你拍板）：

1. **停在存档**：保留本仓库作为"协议已还原、上游不可达"的定案记录（3.5.5 已同结论）。
2. **真机取 token**：arm64 真机 + Play Integrity 通过后，用 `MONET_APPCHECK_TOKEN` 走 `--mode real`；
   token 约 1 小时有效，需要保活/自动刷新（Frida hook `DefaultFirebaseAppCheck` 拿 token 是最省事的做法）。
3. **换更老的版本再试**：无意义 —— App Check 是服务端开关，对历史版本同样生效，只会更糟。
4. **另找不带 App Check 的同源白标**：appnation 是白标工厂（`appnation_devkit/ui/purchases` 公共包），
   若存在同一套 `v2app` 代码但未开 enforcement 的兄弟 App，那才是能直连的入口。

---

## 10. 脱敏声明

- 公开仓库/交付件中：Firebase apiKey 保留（APK 内公开常量），
  `reviewerOptionsV2` 的邮箱口令与 Storage download token 一律 REDACTED。
- `apktool_out/`、`blutter_out/`、`unzip/`、`*.apk` 不进公开仓库（体积 + 版权）。
- 未调用任何付费 API；真实上游只发生过匿名 sign-up 与若干空 body 探针（均在 App Check 前置被拦，未进入业务逻辑，无计费）。
