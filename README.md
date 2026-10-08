# 弱智吧采集器

百度贴吧「弱智吧」全吧帖子采集器。驱动一个真实浏览器翻列表页，把**帖子 + 评论 +
楼中楼 + 图片**落成「一帖一目录」的结构化数据。

- 全吧约 **123,840 帖**（列表接口 `page.total_count`）
- 输出是纯文件，没有数据库：`out/<tid>/post.json` + 该帖的所有图片
- 断点续采靠 `post.json` 在不在，重启/换 session 都不会重采已采过的帖
- 长跑按退出码交接，可无人值守

---

## 为什么必须驱动浏览器

贴吧 PC 端是 Vue SPA，数据来自 `POST /c/f/pb/page_pc`。**`sign` 参数绑定请求体**：

| 试过的事 | 结果 |
|---|---|
| 同一 body 原样重放 | 通过（`err:0`，拿到 15 条） |
| 只改 `pn` 一个参数 | `110001` 报错 |
| 自己按 page 内容算 sign | 算不出来 |

所以自己造包这条路是死的，**只能驱动浏览器、读它自己发的响应**。本项目用
[scrapling](https://github.com/D4Vinci/Scrapling) 的 `AsyncStealthySession`
（底层是 patchright 的 chromium，带 stealth 补丁），监听 `response` 事件把
`page_pc` 的 JSON 取下来。

顺带一个坑：列表页默认落在**「热门」** tab（`total_count` 只有 12000），必须切到
**「最新」**（`total_count=123840`）。两者在 DOM 上长得一样，只有接口返回的
`total_count` 能区分。

## 数据长什么样

```
out/
└── 10000680006/
    ├── post.json
    └── *.jpg / *.png / *.webp      # 帖子正文和评论里的图，按魔数定扩展名
```

`post.json`：

```jsonc
{
  "tid": "10000680006",           // 帖子 ID，也是目录名
  "title": "恭喜大老李荣获本院赠送的秋天第一口棺材",
  "author": "朕才是对的",
  "time": "2025-09-01 12:05:07",
  "reply_num": 5,
  "agree": 4,
  "content": "请大家恭喜他",       // 正文；表情转 [表情名]，图片占位 [图]
  "img_list": [],                 // 本地图片文件名
  "comments": [
    {
      "id": "...",
      "author": "...",
      "ip": "广东",               // 贴吧显示的属地
      "time": "...",
      "agree": 0,
      "content": "...",
      "img_list": [],
      "sub_num": 3,
      "sub": [ /* 楼中楼，字段同上层 */ ]
    }
  ]
}
```

几点约定：

- **所有出库文本都过一遍控制字符清理**（`clean_text`）。源数据里真的混进过
  `U+0018` 这类字符，直接喂给 json/parquet 解析器会噎住。`\t` 和 `\n` 是人写的，保留。
- **图片必须下载，不能只存 URL**：URL 上带着 `?tbpicau=...` 的时效签名，过期就 403。
- **正文里的图片按 `type==3` 的片段数核对**，而不是数 URL —— 一个图片片段三个 URL
  字段全空的话，两边都是 0，比对会"顺利通过"却漏图。
- 楼中楼第一页白送（约 10%），点「展开更多」会弹登录墙，所以只取白送的那部分。

## 安装

要求 **Python 3.11**（`install_deps.py` 会硬拦其他版本：numpy/opencv 的 wheel
在别的 minor 上不一定有，pip 会退化成源码编译然后因为别的原因失败）。

```bash
python install_deps.py
```

它把两步串起来，跑完还逐个 import 自检：

1. `pip install -r requirements.txt`
2. `python -m patchright install chromium` —— **浏览器二进制不在 pip 包里**，最容易漏

> 国内一定要走镜像。官方 `cdn.playwright.dev` 实测 76 KB/s（192 MB 要 42 分钟），
> npmmirror 530 KB/s（6 分钟）。
> `install_deps.py` 里已经设好了 `PLAYWRIGHT_DOWNLOAD_HOST`；
> 手动跑的话自己 `export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`。

> `requirements.txt` **必须保持纯 ASCII**。pip 读 requirements 文件用的是系统 locale
> 编码（中文 Windows = GBK），文件是 UTF-8 会直接 `UnicodeDecodeError`。中文说明写在这里。

可选但强烈建议装 `ddddocr`（验证码缺口识别，见下）：

```bash
python -m pip install ddddocr
```

## 配置

**默认什么都不用配** —— 三个落点都跟着脚本自己走，clone 到哪台机器都能直接跑：

```
<项目目录>\out\          数据（一帖一目录）
<项目目录>\.profile\     浏览器 profile（持久化，别每次新建）
<项目目录>\state.json    跨进程状态
```

想让它们落在别处（比如数据放另一个盘、profile 放 SSD）就设环境变量，代码不用动：

| 环境变量 | 含义 | 不设时的默认 |
|---|---|---|
| `RZB_OUT` | 数据目录 | `<脚本目录>\out` |
| `RZB_PROFILE` | 浏览器 profile | `<脚本目录>\.profile` |
| `RZB_STATE` | 状态文件 | `<脚本目录>\state.json` |

```bat
:: cmd —— 永久生效，但要重开窗口
setx RZB_OUT D:\some\where\out
:: 或只在这个窗口里生效
set RZB_OUT=D:\some\where\out
```

```bash
# Git Bash
export RZB_OUT=/d/some/where/out
```

> profile 一定要**固定**、别每次新建 —— 每次都是新设备的话，一发就弹验证码。

另外 `docr.py` 里的 `bg.jpeg` / `font.png` 是**相对当前工作目录**读的，所以要在项目
目录下跑，别从别处 `python <路径>\collect.py`。

## 用法

命令行直接调：

```bash
python -u collect.py                     # 从列表顶部一路翻到底
python -u collect.py 5                   # 每帖最多带 5 条评论
python -u collect.py --rounds 3          # 只翻 3 批就停（试跑）
python -u collect.py --minutes 60        # 跑够 60 分钟收工（长跑定时）
python -u collect.py --tid 123,456       # 指定补采这几个帖，不走列表页
python -u collect.py --force --tid 123   # 重采并覆盖已有的 post.json
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `<数字>`（位置参数） | 10 | 每帖最多采几条评论 |
| `--rounds N` | 0（到底） | 翻多少批就停 |
| `--minutes N` | 0（不到点） | 跑多少分钟收工 |
| `--max-per-round N` | 20 | 一批最多攒多少新帖就先采掉 |
| `--tid a,b,c` | — | 只采指定帖，不翻列表 |
| `--force` | 关 | 重采已采过的帖（默认只跑一轮，加 `--rounds` 可多跑） |

Windows 上包了一层，更省事（不用记参数、不用管工作目录、日志自动落文件）：

```bat
run.bat 5 --rounds 1 --max-per-round 3    :: 跑一趟，日志落 logs\collect_<时间戳>.log
loop.bat                                  :: 无人值守，按退出码自动拉下一趟
```

> `.bat` 必须**纯 ASCII**：cmd.exe 在批处理文件里按**字节偏移**找下一行，UTF-8 中文
> 会让它算错位置然后执行半行（实测报 `'o' 不是内部或外部命令`）。改脚本前先看这个。
>
> 另外 `run.bat` 里那句 `set PYTHONIOENCODING=utf-8` 不是可有可无：stdout 重定向到
> 文件时 Python 用 locale 编码（cp936），日志变 GBK，`print` 的 `✓` 会
> `UnicodeEncodeError` —— 而且抛在**成功分支**上，看起来像失败。

### 退出码

外面接定时任务照着判，`loop.bat` 就是照这个表调的：

| 码 | 含义 | 该怎么办 |
|---|---|---|
| 0 | 真到底，正常收工 | 吧里的帖采完了，停机 |
| 2 | **刹车停机** | 要人来看：换 profile 到上限，或连着 3 轮颗粒无收 |
| 3 | `--minutes` 到点 / `--rounds` 跑满 | 正常，拉下一趟 |
| 4 | 浏览器整个没了 | 重跑 |

**只有 0 是「全吧采完」。** 3 是「这趟正常收工但没到底」。

> 别用 `timeout` 或杀进程的方式收工 —— 会把 Chrome 晾成孤儿，`.profile` 上的
> `SingletonLock` 不释放，下次启动直接起不来。要限时就交给 `--minutes`，它走的是
> 正常退出，浏览器关得干净。

## 它怎么工作

两个常驻 page：

- **列表页**只往下滚，不导航。滚动容器是 `DIV.frs-page-wrap`（`overflow:scroll`），
  `window.scrollBy` 是空操作。一屏 5~8 帖，一页 30 帖摊在约 6000px 上。
- **帖子页**逐帖导航采集。不能共用列表页 —— 进过详情页会把滚动位置弹回顶部。

几个关键设计：

- **到底判据是接口的 `page.has_more`**，不是 DOM。`has_more==0` 才是真到底；
  `has_more==1` 明确判 `stuck`（页面还在动但没新帖），**不当到底处理**。
- **虚拟列表**：只有可见窗口渲染。滚动步长故意压到 **1000px** —— 步子大了卡片不会
  出现在任何一帧，会**静默丢帖**。
- **不能"传送"**：试过直接设 `scrollTop = 大值`，实测被 clamp 在
  `scrollHeight - clientHeight`，列表是边滚边往长里长的，没滚到的地方根本不存在。
  一次跳只换来约 1553px，还不如逐步滚。所以**没有 seek**，重扫只能硬滚。
- **续爬**：`post.json` 在不在 = 采没采过。重启后从列表顶部重滚，已采的跳过。
  重扫本身不产出数据，所以做了三处缓解（`settle()` 自适应等待、磁盘判据提到探活
  之前、整批已采的 tid 不交出去）。

### 验证码

触发验证码时 `skip_vc.py` 会接住：监听响应里的两张图（背景 + 滑块），
`docr.py` 定位缺口，算屏幕距离，滑过去。

- 定位主路是 **ddddocr**（实测 27/27）；没装就走自研的 NCC/chamfer 兜底
  （27 组里中 17 / 16），能用但明显差一截。
- 缺口坐标是**真实图**上的，要按浏览器里的显示宽度换算，且要确认是等比缩放。

### 三档刹车

都记在 `state.json` 里，跨进程累计：

- **冷却**：换过 profile 后静默 30 分钟，指数退避 30/60/120，封顶 120
- **重置上限**：一个进程里最多换 5 次 profile
- **连续 3 轮颗粒无收**（有失败、没产出）→ 停机交人工，不自己转圈

换了 profile 会切到慢节奏（5~10s/帖 + 每 30 帖歇 60s），因为新 profile 更容易被拦。

## 文件说明

| 文件 | 作用 |
|---|---|
| `collect.py` | 主程序，列表页 + 帖子页 + 落盘 + 刹车 |
| `skip_vc.py` | 验证码：监听滑块组件、截图、拖动 |
| `docr.py` | 验证码缺口定位（ddddocr 主路 + NCC/chamfer 兜底），本地模块不是 pip 包 |
| `main.py` | 手动解验证码的独立小工具（调试用，不是启动器） |
| `run.bat` | 跑一趟的启动器，日志自动落 `logs\` |
| `loop.bat` | 无人值守：按退出码自动拉下一趟 |
| `probe_jump.py` | 一次性探测：列表页能不能直接跳（结论：不能） |
| `requirements.txt` | 依赖清单，必须纯 ASCII |
| `install_deps.py` | 装依赖 + 浏览器二进制 + 自检 |
| `bg.jpeg` `font.png` | 验证码样本图。`docr.py` 的 `pd_move()` 按固定文件名读它们，缺了会 `FileNotFoundError`；每次解验证码时会用新下的图覆盖 |

## 已知问题

- **吞吐会衰减，原因未定位。** 一趟 39 小时的长跑里，吞吐从 669 帖/小时单调掉到
  21 帖/小时，没有拐点也没恢复（中位间隔 4.0s → 130s），而 `state=0` 全程走快节奏，
  所以**不是自己的节流**。按 21 帖/小时算，12.4 万帖要 245 天。
  `loop.bat` 每 6 小时重启一次进程，本身就是这个对照实验；日志里的
  `[耗时] 最近 N 帖平均采集 X.Xs` 是读这条曲线的入口。
- **`out/` 的历史数据可能混着两种 schema。** 早期版本采的评论没有 `sub`（楼中楼）
  字段。跳过判据只看文件在不在，所以新旧会共存，下游要自己容错。
- **Windows 专用。** `.bat`、profile 文件锁、`os.replace` 都按 Windows 写的。
- **单进程。** 没有并发，也没做分布式。

## 免责声明

采集的是公开可访问的帖子内容，仅供个人学习与研究使用。请自行控制请求频率、遵守
目标站点的服务条款；由此产生的任何后果由使用者自负。
