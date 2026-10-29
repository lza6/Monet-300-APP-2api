# Monet 3.0.0 协议规格（逐字段）

来源全部为静态 + 实测：

| 来源 | 取出内容 |
|---|---|
| `blutter_out/asm/art_generator/constant/network_constants.dart` | base URL 选择逻辑 |
| `blutter_out/asm/art_generator/data/service/art_service.dart` | 全部路由 + 请求体字段拼装 |
| `blutter_out/asm/appnation_devkit/src/data/models/http/an_http_client.dart` | 请求头 |
| `res/values/strings.xml` | firebase apiKey / appId / bucket |
| `firebase:fetch`（Remote Config REST） | 风格表、限额、审核配置 |
| 线上探针 | 状态码、App Check 行为 |

---

## 1. 基础

```text
base_prod = https://us-central1-art-generator-e907c.cloudfunctions.net/v2app
base_test = https://us-central1-art-generator-e907c.cloudfunctions.net/v2test
mgmt_prod = https://management-api-production-vfywoevzkq-uc.a.run.app
mgmt_test = https://management-api-test-vfywoevzkq-uc.a.run.app
method    = POST
encoding  = application/json
```

`NetworkConstants.url()` 把 `v2app` / `v2test` 两个常量交给
`ANApiManager.getFunctionsApiUrl()` 二选一（环境开关），业务路由挂在函数根下，**没有 `/v1` 前缀**。

---

## 2. 鉴权

### 2.1 Firebase ID token

```http
POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<API_KEY>
Content-Type: application/json

{"returnSecureToken": true, "clientType": "CLIENT_TYPE_ANDROID"}
```

→ `idToken / refreshToken / expiresIn / localId`。匿名即可，无需邮箱。

刷新：

```http
POST https://securetoken.googleapis.com/v1/token?key=<API_KEY>
{"grantType":"refresh_token","refresh_token":"<refreshToken>"}
```

### 2.2 请求头（3.0.0 全集）

| Header | 值 | 备注 |
|---|---|---|
| `Content-Type` | `application/json` | |
| `Authorization` | `Bearer <idToken>` | |
| `X-App-Id` | `co.appnation.artgenerator` | 硬编码 |
| `X-App-Platform` | `android` | 硬编码 |
| `X-App-Version` | `3.0.0` | `package_info.plus` |

3.0.0 **没有** `X-Firebase-AppCheck`；`Config.usesAppCheck` 字段存在但 Dart 侧没有任何取 token 的调用。

### 2.3 与 3.5.5 的差别

| 项 | 3.0.0 | 3.5.5 |
|---|---|---|
| App Check 注入 | 无 | `an_app_check_interceptor.dart` → `X-Firebase-AppCheck` |
| `/v1/rate` `/v1/feedback` | 走 Cloud Run `management-api-*` | 走 `.env` 的 devkit base |
| `/firebase/like_or_unlike_user_art` | **有** | 无 |
| `/advanced_options` `/custom_onboarding` `/auth/authorize` | 无 | 有 |
| `.env`（flutter_assets 明文配置） | 无（改用 Remote Config） | 有 |
| 加固 | 无 pairip | `libpairipcore.so` |

### 2.4 App Check 阻塞（实测矩阵）

```
v2app /status                无 AppCheck  -> 401 Unauthorized
v2app /status                伪 token     -> 503 Service Unavailable
v2app /generate_without_lora 无 AppCheck  -> 503 / 401（交替观察）
v2app /definitely_not_a_route 无 AppCheck -> 401          ← 不存在的路由也拦
v2test /status               无 AppCheck  -> 401
```

401/503 都在 Express 之前（Google 前端），路由存在与否不影响结果 → 是**函数级 enforcement**，
不是应用层中间件。原始日志：`evidence/appcheck_matrix.txt`。

### 2.5 management-api（Cloud Run）鉴权

```http
POST https://management-api-production-vfywoevzkq-uc.a.run.app/v1/feedback
X-App-Id: ???          ← 白名单校验
```

| X-App-Id | 响应 |
|---|---|
| 缺失 / 空 | `401 {"message":"X-App-Id header is missing!"}` |
| `co.appnation.artgenerator` | `401 {"message":"X-App-Id header is invalid!"}` |
| `artgenerator` / `monet` / `Monet` / Firebase appId | 同上 `invalid!` |
| 未知路由 | `404 {"message":"Cannot POST /"}`（**路由在鉴权之前**） |

即：该服务**没有 App Check**，但用一套我们还没对上的 appId 白名单。
价值有限（`/v1/rate` `/v1/feedback` 不是生成链路），记录为后续可能入口。

---

## 3. 图像生成

三条路由的差别只在**要不要 `loras` / `imageUrl`**，字段全集一致：

### 3.1 `POST /generate_without_lora`

```jsonc
{
  "prompt": "a lighthouse on a cliff at dusk",
  "numImages": 1,                              // int
  "width": 1024,                               // int
  "height": 1024,                              // int
  "modelName": "SG161222/RealVisXL_V3.0",      // 可选，随风格
  "stylePrompt": "high quality, realistic, 8k",// 可选
  "negativePrompt": "cgi, cartoon, ...",       // 可选
  "imageUrl": "https://firebasestorage...",     // 可选（图生图）
  "promptStrength": 0.7,                       // 可选（重绘保留度）
  "seed": 123456,                              // 可选
  "steps": 30                                  // 可选
}
```

字段名来源：`ArtService` 里 Map 组装的字面量（blutter 3.0.0 与 3.5.5 完全一致）：
`prompt / numImages / modelName / stylePrompt / negativePrompt / imageUrl / promptStrength / width / height / loras`。

### 3.2 `POST /generate_with_lora`

在 3.1 基础上必带：

```jsonc
"loras": [
  {
    "path": "https://firebasestorage.googleapis.com/v0/b/art-generator-e907c.appspot.com/o/Lora%2FAnalog_Style.safetensors?alt=media&token=<REDACTED>",
    "scale": 1
  }
]
```

`styles.json` 里 12 个风格自带 `loras`（analog / pixar / tattooStyle / Wallpaper / pixelArt / isometric /
tShirtDesign / pastLife / foodDesign / creepy / logo / funkoPop），App 对这类风格自动切到本路由。

### 3.3 `POST /generate_with_illusion`

参考图重绘：`imageUrl` 必填，`promptStrength` 控制保留度，其余同 3.1。

### 3.4 `POST /generate_video`

```jsonc
{
  "prompt": "...", "numImages": 1, "width": 1024, "height": 1024,
  "modelName": "...", "stylePrompt": "...", "negativePrompt": "...",
  "imageUrl": "...", "promptStrength": 0.5, "seed": 1, "steps": 30
}
```

---

## 4. 其它端点

| 路由 | 请求体 | 说明 |
|---|---|---|
| `/nsfw_detection` | `{"base64Image":"<data>"}` | 上传前置审核 |
| `/upscale` | `{"imageUrl":"<url>"}` | 放大 |
| `/status` | `{}` | devkit 状态/版本 |
| `/firebase/save_user_art` | `{"userId":"...", ...art 字段}` | 作品落 Firestore |
| `/firebase/save_user_art_log` | `{...}` | 埋点 |
| `/firebase/set_user_art_publish_request` | `{"userArtId":"...","userId":"..."}` | 发布到社区 |
| `/firebase/like_or_unlike_user_art` | `{"userArtId":"...","isLike":true}` | **3.0.0 独有** |
| `mgmt /v1/rate` | 评分负载 | Cloud Run |
| `mgmt /v1/feedback` | 文本反馈 | Cloud Run |

---

## 5. 响应模型（`UserArt`）

```
id, imageUrls[], coverImageUrls[], imageUrl, prompt, promptStrength, seed, steps,
artRatio, imageQuantity, styleKey, inputImageUrl, regeneratedFrom, userId,
createdAt, hide, hideV2, canBePublish, favoriteCount, likeCount, isLiked,
mediaType, creationWeek, apiKey
```

图片为 Firebase Storage URL（`art-generator-e907c.appspot.com`）。
> 真实响应体形状**未能端到端确认**（生成请求进不到业务层），
> 故 `monet_client.extract_image_urls()` 做柔性解析，`demo_server` 同时兼容 `imageUrls` / `imageUrl`。标 `待验证`。

---

## 6. 错误码（实测）

| 场景 | HTTP | Body |
|---|---|---|
| 缺 App Check | 401 | `Unauthorized`（纯文本） |
| App Check 非法 | 401 / 503 | `Unauthorized` / `Service Unavailable` |
| 不存在路由（函数内） | 401 / 503 | 同上（前置拦截） |
| 不存在的函数名 | 404 | GFE HTML |
| mgmt 缺 `X-App-Id` | 401 | JSON `X-App-Id header is missing!` |
| mgmt `X-App-Id` 不匹配 | 401 | JSON `X-App-Id header is invalid!` |
| mgmt 未知路由 | 404 | JSON `Cannot POST /...` |
| Firestore REST 直读 | 403 | `{"error":{"code":403,"message":"Missing or insufficient permissions."}}` |

---

## 7. 风格目录（`artStylesV2`，23 条 → 22 个唯一 key）

`styles.json` 为当次 `firebase:fetch` 原文。字段：
`name / key / type(=fal.ai) / isPremium / stylePrompt / negativePrompt / modelName? / loras[] / mediaUrl / samplePrompts[]`

```text
非 premium: noStyle, realistic, cinematic, analog, illustration, neon, isometric,
            signature, lowPoly, creepy, Wallpaper
premium   : anime, pixar, tattooStyle, pixelArt, gta, tShirtDesign, pastLife,
            foodDesign(×2), logo, funkoPop
```

- `modelName` 只有 `realistic` 显式给了 `SG161222/RealVisXL_V3.0`，其余留空 → 上游按风格 prompt 走默认模型。
- `foodDesign` 在上游配置里**重复两条**（一条带 LoRA、一条 premium），网关按 key 去重。

---

## 8. Remote Config 取值（公开可读）

```http
POST https://firebaseremoteconfig.googleapis.com/v1/projects/art-generator-e907c/namespaces/firebase:fetch?key=<API_KEY>
{"app_instance_id":"<uuid4>","app_id":"1:238482661076:android:11aba563876e0276246541",
 "country_code":"US","language_code":"en","platform_version":"34",
 "app_version":"3.0.0","package_name":"co.appnation.artgenerator",
 "sdk_version":"21.0.0","first_open_time":"2024-06-01T00:00:00Z","time_zone":"UTC"}
```

→ 200，54 键。本轮落盘 `rc_current.json`。与生成链路直接相关的：

| key | 值 | 影响 |
|---|---|---|
| `artStylesV2` | 23 条 | 风格表（客户端不做二次校验） |
| `freeArtLimit` | `5` | 免费次数 |
| `isLoginSystemEnabled` | `true` | 现强制登录（3.0.0 时期 false） |
| `creditLimit` | `{"ios":0,"android":0}` | 客户端不限 |
| `generateCoolDownInSeconds` | `60` | 冷却 |
| `reviewerOptionsV2` | 审核账号 + `version:4.3.2` | 公开面泄露（交付件已 REDACTED） |
| `aiPermissionConfig` | 文案自述图片送 **fal.ai** | 上游佐证 |
