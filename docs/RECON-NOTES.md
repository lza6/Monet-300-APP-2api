# 侦察笔记（Monet 3.0.0，2026-10-29）

给下一个会话/下一个同类 App 的可复用套路与坑。结论本身在 `README.md`，这里是"怎么做的 + 别踩什么"。

---

## 1. 判型（5 分钟定路线）

```bash
# 1) 是不是 Flutter？
unzip -l app.apk | grep -E "libapp.so|libflutter.so|flutter_assets"
# → lib/arm64-v8a/libapp.so 存在 = Dart AOT，jadx 读不到业务逻辑，直接上 blutter

# 2) Dart 版本（决定 blutter 要不要现场编 Dart SDK）
python -c "import re;d=open('libflutter.so','rb').read();print(set(re.findall(rb'\d\.\d{1,2}\.\d{1,3} \(stable\)[ -~]{0,20}',d)))"
# → 3.0.0 = Dart 3.4.3（blutter/dartsdk 里已有 v3.3.4 / v3.6.2 / v3.7.2，3.4.3 需现编）

# 3) 有无加固
unzip -l app.apk | grep -E "pairip|dexprotector|libjiagu|libDexHelper"
# → 本包无；同目录 3.5.5 有 libpairipcore.so

# 4) 明文配置金矿（先看再动手）
unzip -p app.apk assets/flutter_assets/.env         # 3.5.5 有；3.0.0 没有
unzip -p app.apk res/values/strings.xml | grep -E "google_api_key|google_app_id|google_storage_bucket"
```

## 2. blutter on Windows（关键坑）

**必须**在带 MSVC 环境的 cmd 里跑，且**不要**从 Git Bash 直接 `cmd //c 'call "C:\...\vcvars64.bat" && ...'`
——Git Bash 会把引号转义成 `\"`，cmd 报"不是内部或外部命令"。

正确做法：写一个 `.bat`，用**绝对路径**调它：

```bat
@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d D:\apk-analysis\blutter
python D:\apk-analysis\blutter\blutter.py <lib/arm64-v8a 目录> <输出目录>
```

```bash
cmd //c "D:\apk-analysis\monet300\run_blutter.bat"     # 注意：不要先 cd 再 cmd //c run_blutter.bat
```

本机环境：`/c/Program Files/Microsoft Visual Studio/18/Community/...`（VS18 = 2026 版），
`vcvars64.bat` 只有 39 字节（`vcvarsall.bat` 的 shim），正常。
Dart 3.4.3 首次编 SDK 约 4 分钟（有缓存后几秒），全量 asm + pp.txt 产出 ~2.4 MB。

## 3. 从 asm 里取协议（比读一大坨 asm 高效的做法）

```bash
cd blutter_out/asm
# a) 端点
grep -rn '"/[a-z_]*"' art_generator/ | grep -oE '"/[a-zA-Z0-9_/]+"' | sort -u
# b) 请求头 —— 直接搜字面量，Dart 字符串在 rodata 里是独立对象
grep -rn '"X-[A-Za-z-]*"\|"Authorization"' --include=*.dart .
# c) 请求体字段
grep -n '"numImages"\|"stylePrompt"\|"loras"\|"promptStrength"' <service>.dart
# d) base url
grep -n 'cloudfunctions.net' -r .
```

`pp.txt` 里 `grep -c AppCheck` 就能判定"这个版本有没有 App Check"（本例只有 1 处配置位）。

## 4. Firebase Remote Config 是最大的免费情报源

只要 apiKey + 任意 `app_instance_id` 就能拉，**不需要 App Check、不需要登录**：

```bash
POST https://firebaseremoteconfig.googleapis.com/v1/projects/<proj>/namespaces/firebase:fetch?key=<API_KEY>
{"app_instance_id":"<uuid4>","app_id":"<google_app_id>","country_code":"US",
 "language_code":"en","platform_version":"34","app_version":"<ver>",
 "package_name":"<pkg>","sdk_version":"21.0.0","time_zone":"UTC"}
```

一次拿到：风格表、限额、冷却、**审核账号口令**、功能开关、上游 provider 文案（"sent to fal.ai"）。
`rc_current.json` 是本轮原文。

## 5. App Check 判定五连（10 分钟出结论）

| # | 探针 | 判定 |
|---|---|---|
| 1 | 正常头 + 无 AppCheck 打 `/status` | 401 纯文本 → 大概率 enforcement |
| 2 | 伪 token（`bogus`） | 401/503 交替 → 确认在验证 token 有效性 |
| 3 | 打**不存在**的路由 | 也 401 → 拦截在应用之前（函数级，不是中间件） |
| 4 | 换函数名枚举 | 只有已知函数存在，且同样 401 → 没有"漏开的后门函数" |
| 5 | 静态：Dart 侧有没有取 token 的调用 | 没有 → **这个版本自己都进不去**，老版本路线直接判死 |

第 5 条最容易被忽略，却直接决定"换老版本"是否值得做 —— Monet 3.0.0 就是反例。

## 6. 端口与后台进程

- 89xx 段被其它会话占满（8931/8941 都是别人的服务，curl 会返回别家 JSON，**骗人**）。
  先探活再绑：

```bash
python -c "import socket;[print(p,'BUSY' if (lambda s:s.connect_ex(('127.0.0.1',p)))(socket.socket())==0 else 'free') for p in (8931,8941,8951,8961)]"
```

- 本会话用 **8951**（mock）、**8961**（auto）、**8971**（real 一次性验证）。
- 要跨会话常驻，按历史惯例用 `Start-Process -WindowStyle Hidden`（bash 后台会随会话被杀）。

## 7. 时间分配（本轮实际）

| 阶段 | 占比 | 产出 |
|---|---|---|
| 判型 + 静态 strings + manifest | 10% | 上游、端点、头、版本 |
| Remote Config 拉取 | 5% | 54 键 + 风格表 + 审核口令 |
| 线上探针（App Check 矩阵 + 函数枚举 + mgmt 白名单） | 15% | 阻塞证据链 |
| blutter 反编译 + asm 提取 | 15% | 逐字段协议、3.0.0 vs 3.5.5 差异 |
| 客户端 + 网关 + demo 页 | 25% | `monet_client.py` `demo_server.py` `demo.html` |
| 压测 + 浏览器 E2E | 20% | 基准数据 + `browser_demo.png` |
| 文档 | 10% | README / PROTOCOL / 本文件 |

## 8. 坑清单

1. **Git Bash 里 `cmd //c` 带引号路径会失败** → 用 `.bat` + 绝对路径。
2. **`/d/...` 路径不能传给 Windows python**（`C:\Python314\python.exe`）→ 用 `D:/...`。
3. **`strings` 命令在本机 bash 里不存在** → 用 python 正则提取。
4. **端口被占不报错**，只会在 curl 时返回别家服务的数据 → 先 socket 探活。
5. **Dart 字符串上下文不连续**，`d=b[start-250:start+500]` 打出来的是隔壁字符串 → 别试图靠上下文猜代码，直接看 blutter 的 asm 文件。
6. **`artStylesV2` 有重复 key**（`foodDesign` ×2）→ 用 dict 去重并断言 22+1=23 个下拉项，否则 E2E 计数对不上。
7. **headless Chromium 12–13px 中文有字形描边伪影**：用"同字符 4 种 CSS 变体 + computed `text-decoration:none`"验证是渲染环境问题，别去改页面 CSS。
