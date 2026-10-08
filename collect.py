# -*- coding: utf-8 -*-
"""
弱智吧采集：每个帖子一个文件夹 = post.json + 该帖所有图片

数据全部来自浏览器自己发的 POST /c/f/pb/page_pc。sign 绑请求体 ——
实测同一个 body 原样重放能过（err:0, 15 条），只改 pn 一个参数就 110001，
所以自己造包这条路走不通，只能驱动浏览器、读它自己的响应。

一页 15 条，MAX_COMMENTS=10 只要 pn=1，不需要登录。

两个 page：列表页常驻、只往下滚（进帖子详情页会把滚动位置弹回顶部，所以帖子
一律交给另一个 page 去开），帖子页负责进帖和采集。列表页一路翻到底就收工，
中途冒出的新帖不追 —— 重启接着跑时，已经有 post.json 的会跳过。

但"跳过"不等于"免费"：列表页每次新开都停在顶部（换 session、验证码 reload、
重启进程都会），所以进程一重启就得从列表最上面重新滚到上次停下的地方。
已采条数越多这段越长（734 条约 4.5 分钟，12 万条就是十几小时），而且一条数据
都不产出。缓解分三处：settle() 把每步固定 1.5s 的等待改成按需缩短；fetch_one
把磁盘判据提到探活之前；next_batch 不把整批已采的 tid 交出去。至于能不能**直接
跳**过已采区（那样才是根治），先跑 probe_jump.py 看结论再定。

路径是相对当前目录的（bg.jpeg 之类），所以就在 main 目录下跑。

用法：
  python -u collect.py                    从顶部一路往下翻，翻到底收工
  python -u collect.py 5                  同上，每帖最多带 5 条评论
  python -u collect.py --rounds 3         只翻 3 批就停（试跑用）
  python -u collect.py --minutes 60       跑够 60 分钟就收工（长跑定时用）
  python -u collect.py --tid 123,456      指定补采这几个帖，单趟跑完就退
  python -u collect.py --force --tid 123  重采，覆盖已有的 post.json

包一层更好用（不用记参数、不用管工作目录、日志自动落文件，细节看脚本注释）：
  run.bat                                 跑一趟，日志落 logs\collect_<时间戳>.log
  loop.bat                                无人值守，按退出码自动拉下一趟

--minutes 是在主循环里判到点，不是外面 timeout 砍进程 —— 砍进程会把 Chrome
晾成孤儿，.profile 上的 SingletonLock 不释放，下一次启动直接起不来。
从这里收工走的是正常退出，浏览器关得干净。

退出码（外面接定时任务时照着判；loop.bat 就是照这个表调的）：
  0 = 真到底，正常收工        3 = --minutes 到点 / --rounds 跑满
  2 = 刹车停机，要人来看      4 = 浏览器整个没了
只有 0 是"全吧采完、可以停机"；3 是"这趟正常收工，接着再来一趟"。

三个刹车（都写在 state.json 里，跨进程累计）：
  · 冷却：换 profile 后静默 30 分钟，指数退避 30/60/120，封顶 120
  · 重置上限：一个进程里最多换 5 次 profile，超了就停机
  · 连续 3 轮颗粒无收（有失败、没产出）就停机，不自己转圈
"""
import asyncio
import datetime
import glob
import json
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import quote

from scrapling.fetchers import AsyncStealthySession

from skip_vc import Captcha

# ---- 三个落点 ----
# 一律走环境变量，没设就落在**脚本自己所在的目录**旁边。这样仓库里不用出现任何
# 真实路径，clone 到哪台机器都能直接跑。名字取项目首字母，只有本机认得：
#   RZB_OUT      数据目录       默认 <脚本目录>\out
#   RZB_PROFILE  浏览器 profile  默认 <脚本目录>\.profile
#   RZB_STATE    状态文件       默认 <脚本目录>\state.json
# 想换地方（比如数据放别的盘）就在外面设环境变量，代码不用动。
_HERE = Path(__file__).resolve().parent


def _place(env, default):
    v = os.environ.get(env)
    return Path(v).expanduser() if v else _HERE / default


OUT = _place("RZB_OUT", "out")
# 固定 profile，不然每次都是新设备，一发就弹验证码
PROFILE = str(_place("RZB_PROFILE", ".profile"))
STATE_FILE = _place("RZB_STATE", "state.json")
KW = "弱智吧"
MAX_COMMENTS = 10
MAX_PER_ROUND = 20              # 一轮最多攒这么多新 tid 就先采掉，别一直翻不采

# ---- 三个刹车的参数 ----
COOL_BASE = 30 * 60             # 第一次换 profile 后静默 30 分钟
COOL_MAX = 120 * 60             # 指数退避封顶 120 分钟
RESET_MAX = 5                   # 一个进程里最多换几次 profile
ROUND_FAIL_MAX = 3              # 连着几轮颗粒无收就停机交人工
REST_EVERY = 30                 # state>=1 时，采满这么多帖歇一次
REST_SECS = 60
ROLL_WIN = 10                   # 每帖耗时打日志时，平均最近这么多帖
SLOW_LO, SLOW_HI = 5.0, 10.0    # state>=1：每帖间隔 5~10s（约 10.1s/帖含开销）
FAST_LO, FAST_HI = 1.5, 3.5     # state==0：还没换过 profile，按原来的慢节奏快跑

# ---- 退出码 ----
EXIT_OK = 0                     # 真到底
EXIT_BRAKE = 2                  # 刹车停机，要人来看
# --minutes 到点，或者 --rounds 跑满。两个都是"正常收工但没到底"，外面接着来一趟
# 就对了。**不能跟 0 混用**：loop.bat 拿 0 当"全吧采完了，停机"，而 --rounds 跑满
# 时给 0 会让调度器在第一趟之后就报"到底了"然后退出。
EXIT_TIME = 3
EXIT_DEAD = 4                   # 浏览器整个没了


# ---------------------------------------------------------------- 解析

def _frags(content):
    """content 是一个片段列表，也可能直接给一个 dict"""
    if isinstance(content, dict):
        return [content]
    return content or []


# 控制字符出口统一清掉。原帖正文里真的混进过 U+0018（不是编码错误，源数据自带的），
# 下游拿去喂模型或者进 json/parquet 解析器容易被噎住。\t 和 \n 是人写的，留着。
_CTRL = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CTRL[0x7F] = None            # DEL 也是 Cc，一起清


def clean_text(s):
    """清掉控制字符。所有出库的文本（标题/作者/正文）都得过这一道，别在调用点各写各的。"""
    return (s or "").translate(_CTRL)


def frag_text(content):
    """片段列表 -> 可读文本。表情转 [中文名]，图片占位 [图]

    type: 0=文本 2=表情 3=图片。链接/视频之类还没见过，
    走最后的兜底把 text 拿出来，至少不丢内容。
    """
    out = []
    for it in _frags(content):
        t = it.get("type")
        if t == 0:
            out.append(it.get("text") or "")
        elif t == 2:
            out.append("[%s]" % (it.get("c") or it.get("text") or ""))
        elif t == 3:
            out.append("[图]")
        else:
            out.append(it.get("text") or "")
    return clean_text("".join(out))


def frag_imgs(content):
    """片段列表 -> 图片 URL。取压缩版（big_cdn_src 是 960 宽的那张）。

    图片项的 src 是 None，地址散在 big_cdn_src / cdn_src / origin_src 里。
    原图 origin_src 只有点"查看原图"才发出去，所以监听拿不到；压缩版
    是页面真渲染的那张，主动请求也拿得到，这里就用它。

    注意带上 ?tbpicau=... 的时效签名，过期 403 —— 必须下载，不能只存 URL。
    """
    urls = []
    for it in _frags(content):
        if it.get("type") != 3:
            continue
        u = it.get("big_cdn_src") or it.get("cdn_src") or it.get("origin_src")
        if u:
            urls.append("https:" + u if u.startswith("//") else u)
    return urls


def n_imgs(content):
    """片段里声明了 type==3（图片）的条数 —— 这是"该有几张"的唯一准数。

    必须数它，不能拿 len(frag_imgs(...)) 当基准：frag_imgs 是从
    big_cdn_src / cdn_src / origin_src 里挑 URL，一个 type-3 片段三个字段全空的
    话它一条 URL 都产不出来 —— 于是"该有几张"和"下到几张"双双为 0，比对顺利
    通过，post.json 落盘、正文里留着一个 [图]、img_list 是空的。洞 5 原样复现。
    """
    return sum(1 for it in _frags(content) if it.get("type") == 3)


def ts(t):
    if not t:
        return ""
    return datetime.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d %H:%M:%S")


def img_kind(b):
    """按魔数定扩展名，别信 URL 的后缀（.jpg 里装 webp 很常见）"""
    if b.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if b.startswith(b"\xff\xd8\xff"):      return "jpeg"
    if b.startswith(b"GIF8"):              return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    if b.startswith(b"BM"):                return "bmp"
    return None


# ---------------------------------------------------------------- 图片

async def save_imgs(page, urls, folder, owner):
    """把 urls 存成 folder/<owner>_<序号>.<ext>，返回文件名列表

    文件名以所属 id 打头（帖子用 tid，评论用评论 id），
    所以从文件名就能反查这张图属于谁。
    """
    names = []
    for n, url in enumerate(urls, 1):
        try:
            r = await page.request.get(
                url, headers={"referer": "https://tieba.baidu.com/"})
            if not r.ok:
                print("    图 %s 失败: HTTP %s" % (url[:60], r.status))
                continue
            body = await r.body()
        except Exception as e:
            print("    图 %s 异常: %s %s" % (url[:60], type(e).__name__, e))
            continue
        kind = img_kind(body)
        if not kind:
            # 魔数不认 = 下回来的根本不是图片（反盗链吐的 HTML，也是 HTTP 200）。
            # 原来 fallback 成 ".jpg" 照样算成功 —— 计数对得上，盘上却是个 HTML，
            # 外面看和"采到了"一模一样。
            print("    图 %s 不是图片(%d 字节，开头 %r)，当失败"
                  % (url[:60], len(body), body[:8]))
            continue
        name = "%s_%d.%s" % (owner, n, kind)
        (folder / name).write_bytes(body)
        names.append(name)
        await asyncio.sleep(random.uniform(0.15, 0.45))     # 别把图床打急了
    return names


# ---------------------------------------------------------------- 列表页

# 一张卡片上有好几个 a[href*="/p/"]（标题、缩略图…）指向同一个 tid，
# 所以必须去重：不去重的话一屏 90~105 个链接其实只有 5~8 个帖，
# "这一批攒够 N 个"会被重复计数骗到，实际根本没攒够。
JS_TIDS = r"""() => {
  const root = document.querySelector('.feed-list-container') || document;
  const ids = [...root.querySelectorAll('a[href*="/p/"]')]
    .map(a => (a.getAttribute('href') || '').match(/\/p\/(\d+)/))
    .filter(Boolean).map(m => m[1]);
  return [...new Set(ids)];
}"""

# 一步 1000px，不是 2500。虚拟列表只有可视窗口那几张卡片在 DOM 里，一步跨得
# 超过一个窗口的跨度，中间的卡片**从来不会出现在任何一帧里** —— 不报错、不漏痕迹，
# 只是帖子静悄悄少采了。宁可多滚几次。
#
# 真正在滚的是 DIV.frs-page-wrap（overflow:scroll）。window.scrollBy 在这个页面上
# 是**空操作**（document 不滚动，scrollY 恒为 0），留着它只是因为无害。
# 返回值 = 滚完之后列表的 scrollTop，调用方靠它判断"还能不能往下滚"。
SCROLL_STEP = 1000

JS_SCROLL = """() => {
  for (const el of document.querySelectorAll('div')) {
    if (el.scrollHeight > el.clientHeight + 300 && el.clientHeight > 200) {
      el.scrollTop += %d;
    }
  }
  window.scrollBy(0, %d);
  const f = document.querySelector('.frs-page-wrap');
  return f ? Math.round(f.scrollTop) : -1;
}""" % (SCROLL_STEP, SCROLL_STEP)


JS_POS = """() => {
  const f = document.querySelector('.frs-page-wrap');
  return f ? Math.round(f.scrollTop) : -1;
}"""


async def scroll_pos(page):
    """列表当前滚到哪了。拿不到返回 None。"""
    try:
        return await page.evaluate(JS_POS)
    except Exception:
        return None


async def settle(list_page, tids_before, timeout=1.5, poll=0.15):
    """滚完之后等列表跟上：DOM 里的 tid 集合一变就立刻返回，没变就等满 timeout。

    原来这儿是个写死的 sleep(1.5)。那 1.5s 是留给**新一页的接口响应**的
    （一页 30 帖、约 6000px 才有一页），可大多数滚动只是在虚拟列表里挪窗口 ——
    同步重渲染，几十毫秒就完了，剩下 1.4s 纯白等。续爬重扫那段要连着走几百上千步，
    白等的就是几十分钟。

    注意这**不是**靠"缩短等待"提速的：tid 集合没变时照样等满 timeout。所以
    "滚不动了 = 到底"的判据、以及内容落 DOM 的时序都没动过，只是不再为
    "已经渲染好了"的情况付全款。

    tids_before 传本轮的 tid 列表；读 DOM 抛异常（_reload 打断）时返回 False，
    当"没等到"，和原来固定 sleep 的行为一致。
    """
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(poll)
        try:
            if await list_page.evaluate(JS_TIDS) != tids_before:
                return True
        except Exception:
            return False
    return False
# 页面"第几代"。reload 会重开一个文档，performance.timeOrigin 跟着变 ——
# 用它判断列表页是不是被验证码的 _reload 刷过（刷过 = 滚动位置归零）。
NAV_JS = "() => performance.timeOrigin"


FEED_MIN = 15        # 列表页正常 99~211 个链接，低于这个数就是还没渲染好


async def wait_feed(page, timeout=8000):
    """等列表真的渲染出来。

    goto(wait_until="domcontentloaded") 一返回就读 DOM 必然是 0 个链接 ——
    那只等到了 HTML 骨架，Vue 还没去拉 /c/f/frs/page_pc、还没渲染帖子卡片。
    实测点击轮 96% 撞上这个空窗期，外面看着就是"弹回一个空白列表页"。
    固定 sleep 治不了根（快慢不定），等元素出现才是准的。
    """
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('a[href*=\"/p/\"]').length >= %d"
            % FEED_MIN, timeout=timeout)
        return True
    except Exception:
        return False


# 列表页顶部真实 tab：精华 / 热门 / 最新。
# 服务端 frs_tab_default=1 = 默认落在"热门"，而热门 total_count=12000（400 页），
# "最新"才是全吧 123840（4128 页）。不切的话我们一路扫的是热门 —— 而且日志上
# 完全看不出来，是"翻到 60 帖就说到头了"那类静默少采的翻版。
TAB_JS = """() => {
  for (const a of document.querySelectorAll('a.tab-item')) {
    if ((a.textContent || '').trim() === '最新') {
      const r = a.getBoundingClientRect();
      if (r.width < 5 || r.height < 5) continue;
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }
  }
  return null;
}"""

ACTIVE_TAB_JS = """() => {
  const a = document.querySelector('a.tab-item.active');
  return a ? (a.textContent || '').trim() : null;
}"""


async def pick_tab(page, tries=3):
    """把列表切到"最新"（全吧）。返回 True = 现在确实在最新上。

    实测点一次之后，后续分页都保持 tab_id=503，不用每页点；但**页面 reload
    （验证码的 _reload）会打回热门**，所以每次进列表、以及每次发现页面换代，
    都要重切。
    """
    for _ in range(tries):
        try:
            if await page.evaluate(ACTIVE_TAB_JS) == "最新":
                return True
        except Exception:
            pass
        try:
            box = await page.evaluate(TAB_JS)
        except Exception:
            box = None
        if box:
            try:
                await page.mouse.click(box["x"], box["y"])
                await asyncio.sleep(2.5)
                continue
            except Exception:
                pass
        await asyncio.sleep(1.5)
    try:
        cur = await page.evaluate(ACTIVE_TAB_JS)
    except Exception:
        cur = None
    if cur != "最新":
        # 不抛异常 —— 切不过去也得跑，但要说清楚在扫什么
        print("[列表] ⚠ 没切到'最新'（当前 tab=%r）—— 这一轮扫的是热门，"
              "只有 12000 帖，不是全吧" % cur)
    return cur == "最新"


async def enter_list(list_page):
    """把列表页领到吧首页、切到"最新"、等它渲染完。返回 tab 钉住没有。

    返回值是**能不能开始扫**的判据，不是参考信息：钉不住"最新"就等于在扫热门，
    而热门跟全吧在 DOM 上长得一模一样 —— 扫完热门会以为"翻到底了"。所以这里
    宁可重试到失败，让调用方把这一轮记成颗粒无收，交给刹车去收。

    重试分两轮：第一次切不过去就 reload 一次再切 —— 页面刚起来时 tab 那一排
    可能还没挂上事件，reload 后就好了。
    """
    for attempt in (1, 2):
        await list_page.goto("https://tieba.baidu.com/f?kw=%s" % quote(KW),
                             wait_until="domcontentloaded")
        await wait_feed(list_page)
        ok = await pick_tab(list_page)
        # 把"扫的是哪个 tab"打在日志里：默认落在热门（12000 帖）跟全吧（123840）
        # 在 DOM 上没有任何区别，只有这行能看出来。
        print("[列表] 当前 tab=%s（第 %d 次）"
              % ("最新(全吧)" if ok else "⚠ 没切过去", attempt))
        if ok:
            return True
        if attempt == 1:
            await asyncio.sleep(3)
    return False


async def nav_gen(page):
    """页面当前是第几代（拿不到就 None）"""
    try:
        return await page.evaluate(NAV_JS)
    except Exception:
        return None


def judge_bottom(feed):
    """滚不动了之后，用接口响应给个说法。返回 (判据码, 文案)。

    **DOM 上"没有新帖"这件事，"真到底"和"被拦"长得一模一样**，但处理方式相反。
    接口分得开：
      has_more == 0   → 真到底（权威）
      error_code 非 0 → 被拦了
      has_more == 1   → 卡住了，也不是到底
    这正是当初"翻到 60 帖就报到底"能骗过所有人的地方。

    判据码决定接下来干嘛（见 main 里那句分派）：
      bottom  真到底   → 换 profile、收工，退出码 0
      blocked 被拦     → 换 profile、冷却，重开一个 session
      stuck   卡住     → 只重开 session，**不换 profile**（令牌好好的，别浪费）
      unknown 看不出来 → 当到底处理，但日志里说清楚是"猜"的
    """
    if not feed:
        return "unknown", "（没抓到 feed 响应，只能按滚动判据当到底）"
    if feed.get("err"):
        return "blocked", ("（⚠ 接口 error_code=%s %s —— 这是被拦了，不是真到底）"
                           % (feed.get("err"), (feed.get("msg") or "")[:40]))
    if feed.get("has_more") == 0:
        return "bottom", "（接口确认 has_more=0，真到底）"
    if feed.get("has_more") == 1:
        return "stuck", ("（⚠ 接口 has_more=1 且停在第 %s/%s 页 —— "
                         "这是卡住了，不是真到底）"
                         % (feed.get("cur"), feed.get("total")))
    return "unknown", "（接口没给 has_more，按滚动判据当到底）"


async def next_batch(list_page, cap, seen, feed=None, want=15, idle_max=15,
                     steps_max=300, force=False):
    """从列表页**当前滚动位置**继续往下翻，返回新出现的 tid。

    返回空列表 = 到底了。

    **到底不能只看"没新增"。** 实测这个列表是**一页 30 个帖**往里灌的：灌完一页
    之后，接着滚 ~6 次都是一条新帖都没有（虚拟列表把那 30 个帖摊在 ~6000px 上，
    而一屏只显示 5~8 个，中间几屏全是已经见过的），等滚到接近底部才灌下一页。
    所以"连着 6 次没新帖"根本不是到底，是**页与页之间的空档** —— 拿它当判据会在
    第 60 个帖就收工，而且日志上看着跟真的翻完了一模一样。

    真正靠得住的到底是**滚不动了**：scrollTop 不再前进（前进了不到 50px，
    就是已经顶到已加载内容的底部、loader 又不肯再灌了）。所以这里两个条件一起用：
    这一滚没吐新帖 **且** scrollTop 没动，才算到底。

    为什么不用链接数变化：虚拟列表滚动会回收 DOM 节点，链接数在 43~309 之间来回
    跳、根本不单调，拿它当信号必然误判。

    seen 由调用方持有、跨批次累积，所以这里是**追加**语义。滚动位置就是游标，
    不回顶部 —— 回顶部的话扫的永远是同一个窗口，frontier 不前进，后面的老帖
    永远够不着。

    续爬（`seen` 是空的、但磁盘上已经有一大堆 post.json）：攒够一批后先看是不是
    **全都采过了**，是就不交出去、接着往下滚（`force=True` 时不做这一步 ——
    那时候本来就是要把旧的全都重采一遍）。这只是省掉"交出去再逐条 skip"那一遍，
    **前面那一长段还是得一步一步滚过去**：没采过的区段不能跳（大跳会让中间卡片
    根本不进 DOM，见 SCROLL_STEP 上面那段注释），而滚过去恰恰是重扫真正的开销，
    所以这里配合 `settle()` 把每步的等待从固定 1.5s 压到"变了就走"。
    能不能连已采区也跳过去，要先跑 probe_jump.py 看结论。
    """
    await wait_feed(list_page)
    gen0 = await nav_gen(list_page)
    batch = []
    idle = 0
    pos = await scroll_pos(list_page)
    t_nb = time.time()
    steps = 0
    caught_up = False

    def _done(b):
        # 交出去时的滚动开销。和每帖那行 [耗时] 并排看，就能分清衰减是列表页
        # 这边变慢还是帖子页那边变慢 —— 一条动一条不动，很好认。
        print("[列表] 本批 %d 条，滚了 %d 步，用时 %.1fs"
              % (len(b), steps, time.time() - t_nb))
        return b

    for _ in range(steps_max):
        steps += 1
        while cap.busy:                     # 弹验证码了，等钩子解完再读 DOM
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.3)            # 解题里的 reload 收尾要一点时间

        # 验证码的 _reload 会把列表页刷掉，滚动位置归零。这时必须清 seen：
        # 不清的话，新文档顶部吐出来的 tid 全在 seen 里 → batch 空 → 被误判成
        # "到底"直接收工。已采过的帖靠 post.json 跳过，重走一遍不花什么代价。
        gen = await nav_gen(list_page)
        if gen != gen0:
            print("[列表] 列表页被刷过（验证码 reload），滚动位置归零，从头再走")
            seen.clear()
            gen0 = gen
            pos = None
            await pick_tab(list_page)      # reload 会打回"热门"，得重切回"最新"

        try:
            tids = await list_page.evaluate(JS_TIDS)
        except Exception as e:
            # _reload 打断 evaluate 是正常现象，不是错误：这一步当"这轮没新帖"
            print("[列表] 读 DOM 被打断(%s)，这轮跳过" % type(e).__name__)
            tids = []
        got = 0
        for t in tids:
            if t in seen:
                continue
            seen.add(t)
            batch.append(t)
            got += 1
        if len(batch) >= want:
            # 攒够了。但若这一批**全是磁盘上已经采过的**，交出去毫无意义 ——
            # 每条都只是走一遍 skip。续爬时前面一长段都是这种，直接扔掉接着往下
            # 滚，滚出真正没采过的帖再交。
            #
            # 用 os.path.exists 逐条 stat，不预装一份"已采 tid 集合"：一次 stat
            # 就够，不用把 out\ 枚举一遍（那是 12 万个目录）。
            # 顺带说清楚**为什么不把已采 tid 预装进 seen**：那样 next_batch 会
            # 一条都吐不出来 → idle 攒到 idle_max → 被当成"到底"收工，跟注释里
            # 那个"翻到 60 帖就说到头"是同一类静默少采。
            if force or any(not (OUT / t / "post.json").exists() for t in batch):
                return _done(batch)         # 攒够一批就交出去采，别等翻到底
            if not caught_up:
                caught_up = True
                print("[续爬] 已采过的这 %d 条不交出去，接着往下滚" % len(batch))
            batch = []

        prev = pos
        tids_now = tids
        try:
            pos = await list_page.evaluate(JS_SCROLL)
        except Exception:
            pos = None
        await settle(list_page, tids_now)

        if not got and prev is not None and pos is not None:
            if prev > 0 and pos - prev < 50:        # 滚不动了
                print("[列表] 滚不动了（位置卡在 %d）%s"
                      % (pos, judge_bottom(feed)[1]))   # [1] = 文案，判据码在返回时用
                return _done(batch)

        idle = 0 if got else idle + 1
        if idle >= idle_max:                # 兜底：万一 loader 卡死，别无限空转
            print("[列表] 连着 %d 次滚动既没新帖也还在动 —— 当到底处理" % idle)
            return _done(batch)
    return _done(batch)


async def wait_pc(page, tid, pc, limit=160):
    """等这个帖子的 page_pc。

    验证码由钩子自己解，这里只管等 —— 解题要滑 + 可能要刷新重试，
    所以 limit 给得宽（160 * 0.25s = 40s）。
    """
    for _ in range(limit):
        if tid in pc:
            return True
        await asyncio.sleep(0.25)
    return False


async def goto_post(page, tid, tries=2):
    """进帖子页。

    验证码重试里的 reload 可能打断正在进行的导航（抛 net::ERR_ABORTED 之类），
    那不是真错误，重来一次就好 —— 不包起来的话整个采集循环会被这一下打死。
    """
    url = "https://tieba.baidu.com/p/%s" % tid
    for k in range(1, tries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as e:
            print("  导航被打断(%s)，第%d次重来" % (type(e).__name__, k))
            await asyncio.sleep(random.uniform(1.5, 2.5))
    return False


async def ensure_page(s, page, cap, label="page"):
    """page 死了（或压根还没开）就重开一个。返回能用的那个。

    返回值 is 传进来那个 -> 没动过
    返回另一个 Page     -> 重开过。**调用方必须重置跟滚动位置有关的状态**，
                           新 page 停在 about:blank，什么也没有
    返回 None          -> 整个 context 都没了（浏览器窗口被整个关掉），
                           重开不了，调用方该收尾了

    不在这儿抛：一个关窗口动作不该把整个循环打崩。
    """
    if page is not None:
        try:
            await page.evaluate("1")             # 死 page 上这一步会抛
            return page
        except Exception:
            print("[%s] 已不可用，重开一个" % label)
            if page in cap.pages:
                cap.pages.remove(page)           # 别留着尸体给交叉核对用
            try:
                await page.close()               # 关掉，别烂在 context.pages 里
            except Exception:
                pass
    try:
        page = await s.context.new_page()
    except Exception as e:
        print("[%s] ✗ 重开失败(%s) —— context 已经没了，只能收尾"
              % (label, type(e).__name__))
        return None
    cap.pages.append(page)
    return page


# ---------------------------------------------------------------- 帖子页

IMG_TRIES = 3        # 同一个帖连着这么多轮还没把图下全，就带着 img_err 落盘


async def collect_post(page, tid, data, n_cmts, try_n=1):
    users = {u.get("id"): u for u in (data.get("user_list") or [])}
    th = data.get("thread") or {}
    ff = data.get("first_floor") or {}

    # page_pc 偶尔返回不带 first_floor 的响应（服务端抖动）。不拦的话会落一个
    # author / content / 帖子 img_list 全空的 post.json —— 而跳过判据是"文件在不在"，
    # 落了盘这条就永远不再补，从外面看和正常采到的长得一模一样。跟洞 5 同一个性质。
    # 实测 6597 条里中过 1 条（tid=9878907467：thread 齐全，first_floor 那边全空）。
    if not ff and try_n < IMG_TRIES:
        print("    first_floor 是空的（第 %d/%d 次）—— 这条不算采到，下轮重试"
              % (try_n, IMG_TRIES))
        return None

    def author(uid):
        u = users.get(uid) or {}
        return clean_text(u.get("name") or u.get("name_show") or "")

    folder = OUT / str(tid)
    folder.mkdir(parents=True, exist_ok=True)
    missing = []                    # 没下全的：(归属 id, 该几张, 下到几张)

    async def grab(owner, content):
        """下这一份内容的图，short 的话记进 missing"""
        want = n_imgs(content)
        got = await save_imgs(page, frag_imgs(content), folder, owner)
        if len(got) < want:
            missing.append((owner, want, len(got)))
        return got

    rec = {
        "tid": str(tid),
        "title": clean_text(th.get("title") or ff.get("title") or ""),
        "author": author(ff.get("author_id")),
        "time": ts(th.get("create_time") or ff.get("time")),
        "reply_num": th.get("reply_num", 0),
        "agree": (th.get("agree") or {}).get("agree_num", 0),
        "content": frag_text(ff.get("content")),
    }
    rec["img_list"] = await grab(str(tid), ff.get("content"))

    async def mk_comment(p, owner):
        """一条评论、或者一条楼中楼 —— 两者字段结构完全一样，共用这一个。"""
        c = {
            "id": str(p.get("id") or ""),
            "author": author(p.get("author_id")),
            "ip": (users.get(p.get("author_id")) or {}).get("ip_address", ""),
            "time": ts(p.get("time")),
            "agree": (p.get("agree") or {}).get("agree_num", 0),
            "content": frag_text(p.get("content")),
        }
        c["img_list"] = await grab(owner, p.get("content"))
        return c

    comments = []
    for idx, p in enumerate((data.get("post_list") or [])[:n_cmts]):
        cid = str(p.get("id") or "")
        # 没 id 的评论得自己造一个归属名，不能是空串：owner 为空时文件名是
        # "_1.jpeg"，两条无 id 的评论会互相覆盖，而 len(got)==len(want) 照样成立
        # —— 计数检查看不见，post.json 落盘声称有图，盘上却是别人的那张。
        owner = cid or "%s_c%d" % (tid, idx)
        c = await mk_comment(p, owner)
        c["sub_num"] = p.get("sub_post_number", 0)     # 楼中楼总数（只记数）
        # 楼中楼第一页是 page_pc 白送的（实测每条评论 3~4 条，覆盖 23.4% 的评论），
        # 零额外请求。更深的要登录才给（点"展开 N 条回复"弹的是扫码登录），
        # 不取 —— 所以 sub 里的条数远小于 sub_num，别拿它当全部。
        subs = (p.get("sub_post_list") or {}).get("sub_post_list") or []
        # sub 恒定存在：没楼中楼时是 []。以前是按需创建（实测 76.6% 的评论没这个键），
        # 下游写 c["sub"] 直接 KeyError —— 统一成空列表，读的地方不用再兜。
        c["sub"] = []
        for j, s in enumerate(subs):
            sid = str(s.get("id") or "")
            c["sub"].append(await mk_comment(s, sid or "%s_s%d" % (owner, j)))
        comments.append(c)
    rec["comments"] = comments

    if not ff:
        # 试满 IMG_TRIES 次 first_floor 还是空的。还是得落盘（不然这条永远采不到），
        # 但打上标记 —— 别让它看起来像一条正常帖。
        rec["ff_err"] = "重试 %d 次 first_floor 仍为空" % IMG_TRIES
        print("    ⚠ 试了 %d 次 first_floor 还是空的，带 ff_err 落盘" % IMG_TRIES)

    if missing:
        if try_n < IMG_TRIES:
            # 图没下全就别落 post.json：跳过判据是"json 在不在"，落了就等于把这条
            # 永久标记成"采过了"，而那几张图再也没人补。已下到的图留在盘上，
            # 下轮同名覆盖，不堆垃圾。
            print("    图没下全（第 %d/%d 次）：%s —— 这条不算采到，下轮重试"
                  % (try_n, IMG_TRIES,
                     "，".join("%s 缺 %d/%d" % (w, a - b, a)
                              for w, a, b in missing)))
            return None
        # 连着几轮都下不全，多半是这张图在 CDN 那边真没了，再试一万次也是 403。
        # 落盘，但把缺的写进 img_err —— 别假装完整。
        rec["img_err"] = ["%s 缺 %d/%d" % (w, a - b, a) for w, a, b in missing]
        print("    ⚠ 试了 %d 次仍缺图，带着 img_err 落盘（再试也不会好）" % IMG_TRIES)
    return rec


# ---------------------------------------------------------------- 跨进程状态

def load_state():
    """读 state.json。读不出来就当场开一个新的 —— 状态文件坏了不该拦住采集。"""
    d = {}
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[状态] 读不了 %s（%s），当成全新的重来" % (STATE_FILE.name, e))
    if not isinstance(d, dict):
        d = {}
    d.setdefault("state", 0)            # 0=还没换过 profile；>=1 起延迟策略
    d.setdefault("resets", 0)           # 累计换过几次 profile
    d.setdefault("consec_fail", 0)      # 连着几轮颗粒无收
    d.setdefault("profile_rounds", 0)   # 当前这个 profile 撑过了几轮（换的时候归档）
    d.setdefault("history", [])         # 每次换 profile 的来龙去脉
    return d


def save_state(d):
    """原子写。写到一半被杀留个截断的 json，下一轮 load_state 会当成全新的 ——
    那等于把重置上限和连续失败计数一起清零，刹车就废了。"""
    try:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("[状态] ✗ 写不进 %s：%s" % (STATE_FILE.name, e))


def cooldown_for(resets):
    """第 resets 次换 profile 之后的静默时长：30 -> 60 -> 120 分钟，封顶 120。"""
    return min(COOL_BASE * (2 ** max(0, resets - 1)), COOL_MAX)


def stash_profile():
    """把 .profile 改名备份（不是直接删干净），再删掉更老的那份。

    必须在浏览器关掉之后动手：Windows 上还开着的文件是锁着的，改名和删除都会
    失败 —— 而且失败得很难看（删一半，留下个半残的 profile，下次启动就是个
    认不出来的设备）。所以这里带重试，并且**宁可留着不删也不删一半**。

    改名（os.replace）是原子的，比 rmtree 稳得多。

    返回 (成功?, 说明)
    """
    if not os.path.isdir(PROFILE):
        return True, "（本来就没有 .profile）"

    # 时间戳到秒，两次重置落在同一秒就会撞名 —— 而 os.replace 改名到一个
    # **已存在且非空**的目录上，Windows 直接 WinError 5（拒绝访问），不是覆盖。
    # 撞上了就加个序号，别让一个纯命名问题把整趟跑停掉。
    base = "%s.bak-%s" % (PROFILE, time.strftime("%Y%m%d-%H%M%S"))
    dst, n = base, 1
    while os.path.exists(dst):
        n += 1
        dst = "%s-%d" % (base, n)

    for i in range(6):
        try:
            os.replace(PROFILE, dst)
            break
        except OSError as e:
            if i == 5:
                return False, "改名失败 %s：%s" % (dst, e)
            time.sleep(2)
    else:                                # 理论到不了，防 for 掉空
        return False, "改名失败 %s" % dst

    # 只留最近一份备份。再多也没用，一个 Chrome profile 几百 MB。
    baks = sorted(glob.glob(PROFILE + ".bak-*"))
    for old in baks[:-1]:
        shutil.rmtree(old, ignore_errors=True)
    return True, dst


# ---------------------------------------------------------------- 主流程

async def fetch_one(s, post_page, cap, tid, n_cmts, pc, wanted, force, try_n):
    """采一个帖。返回 (状态, post_page, rec)，状态是 ok / skip / bad / dead。

    dead 表示 context 整个没了，调用方该收尾。
    """
    # 磁盘判据放在探活**前面**：跳过的帖根本用不着浏览器。ensure_page 每次都要
    # 一次 page.evaluate("1") 的往返，重扫已采区时这笔开销乘以已采条数 —— 而
    # skip 分支不使用 post_page、不导航、不落盘，提前返回没有副作用。
    # 代价只有一个：本批全是 skip 时，死 page / 死 context 要等第一个真去采的
    # tid 才发现（"dead" 判定推迟，不影响正确性）。
    pj = OUT / str(tid) / "post.json"
    if pj.exists() and not force:        # 采过了 —— 重启后接着跑，不从头重来
        return "skip", post_page, None

    post_page = await ensure_page(s, post_page, cap, "帖子页")
    if post_page is None:
        return "dead", None, None

    wanted.add(tid)
    try:
        if not await goto_post(post_page, tid):
            return "bad", post_page, None
        if not await wait_pc(post_page, tid, pc):
            # 验证码的 _reload 会把正在发的 page_pc 打断，所以"没等到"多半是
            # 验证码在解，不是真失败。等它解完再进一次页面重来。
            if cap.busy:
                while cap.busy:
                    await asyncio.sleep(0.3)
                if await goto_post(post_page, tid):
                    await wait_pc(post_page, tid, pc)
        data = pc.get(tid)
        if not data:
            print("    %s ✗ 没等到 page_pc" % tid)
            return "bad", post_page, None
        if data.get("error_code"):
            print("    %s ✗ %s %s" % (tid, data.get("error_code"),
                                      data.get("error_msg")))
            return "bad", post_page, None
        rec = await collect_post(post_page, tid, data, n_cmts, try_n)
    except Exception as e:
        # page 在采集中途死了 —— 这条不落盘、下轮重试，但不能让它把整个 run
        # 带崩。图片走的是 context.request，所以图其实没丢，重试很便宜。
        print("    %s ✗ 采集途中炸了(%s)，下轮重试" % (tid, type(e).__name__))
        return "bad", post_page, None
    finally:
        wanted.discard(tid)
        # 用完就扔。攒着的话长跑是几百 MB；更要命的是洞 5 的重试路径 ——
        # 重试时 tid 还在 pc 里会直接"命中"，拿到上一轮那份 JSON，里面的
        # ?tbpicau= 签名早过期了，图必然 403，于是永远重试、永远失败。
        pc.pop(tid, None)

    if rec is None:
        return "bad", post_page, None

    # 原子写：先落 .tmp 再 replace。直接 write_text 的话，写到一半进程被杀会留个
    # 截断的 post.json —— 而下一轮靠 pj.exists() 判断"已采过"，那条会被永久跳过，
    # 也不会有任何地方报出来：静默丢数据。
    pj_tmp = OUT / str(tid) / "post.json.tmp"
    pj_tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    os.replace(pj_tmp, pj)
    return "ok", post_page, rec


def _pace(st):
    """两次采集之间的等待秒数。state>=1 之后换成慢节奏 + 每 REST_EVERY 帖长歇。

    为什么按 state 分两档：state==0 是"还没换过 profile"的第一趟，对面还没
    盯上，快跑把存量扫下来，中间弹一次验证码的成本无所谓；一旦换过 profile
    （state>=1）就说明开始被盯了，从此按慢的来。
    """
    st["n_since_rest"] += 1
    if st["state"] >= 1:
        if st["n_since_rest"] >= REST_EVERY:
            st["n_since_rest"] = 0
            print("[节奏] 采满 %d 帖，歇 %d 秒" % (REST_EVERY, REST_SECS))
            return REST_SECS
        return random.uniform(SLOW_LO, SLOW_HI)
    return random.uniform(FAST_LO, FAST_HI)


async def run_session(s, cfg, st):
    """跑一个 session（= 一个 browser context）。返回下一次该干嘛：

      "done"    真到底 / --tid 跑完了，收工
      "restart" 换一个 session 再来（profile 换不换看 st["reset"]）
      "stop"    刹车停机，要人来看
      "timeup"  --minutes 到点
      "dead"    浏览器整个没了

    跨 session 的东西全在 st 里（stats / attempts / t0 / 计数器 / state.json
    那份字典），session 内的东西（pc / wanted / feed / page）就在这儿现开 ——
    换了 context 它们本来就全作废了。
    """
    pc = {}                          # tid -> page_pc 的 JSON
    wanted = set()                   # 正在等哪些 tid —— 见 grab
    feed = {}                        # 列表接口最近一次的 page 信息（到底判据用）

    async def grab(resp):
        """按响应自己带的 kz 归档，不靠"当前在采谁"去猜。

        验证码重试会 reload 页面，如果它撞上主流程换帖子的那一刻，回来的
        可能是上一个帖子的数据。按 kz 归档，串号从构造上就不可能出现。

        只收 wanted 里的：见 fetch_one 里 pc.pop 那段注释。
        """
        if "/c/f/pb/page_pc" not in resp.url:
            return
        m = re.search(r"(?:^|&)kz=(\d+)", resp.request.post_data or "")
        if not m:
            return
        tid = m.group(1)
        if tid not in wanted:
            return
        try:
            pc[tid] = await resp.json()
        except Exception as e:
            print("  page_pc 解析失败:", type(e).__name__, e)

    async def grab_feed(resp):
        """记下列表接口最近一次的 page 信息 —— 到底判据要读它。

        has_more / error_code 是权威的，比 DOM 上"没有新帖"可靠得多：
        DOM 上"真到底"和"被拦"长得一模一样，接口分得开。
        """
        if "/c/f/frs/page_pc" not in resp.url:
            return
        try:
            b = await resp.json()
        except Exception:
            return
        pg = b.get("page") or {}
        feed.update({"err": b.get("error_code"),
                     "msg": b.get("error_msg"),
                     "has_more": pg.get("has_more"),
                     "cur": pg.get("current_page"),
                     "total": pg.get("total_page"),
                     "count": pg.get("total_count"),
                     "at": time.time()})

    cap = Captcha()                          # 验证码钩子：弹了自己解，主流程不用管
    cap_fails0 = cap.fails                   # 这一场里验证码失败了几次（换 profile 的判据）

    s.context.on("error", lambda e: print("[监听器异常]", type(e).__name__, e))
    s.context.on("response", cap.hook)
    s.context.on("response", grab)
    s.context.on("response", grab_feed)

    async def run_batch(seed_tids, post_page_state):
        """采一批 tid。返回 (post_page, 是否还活着)"""
        for tid in seed_tids:
            # 到点了就在批中间收手，别等这一批 20 个采完再退 —— 一批能拖十几分钟，
            # 说好 60 分钟就跑到 75 分钟了。没采完的下一轮重扫会补上。
            if cfg["minutes"] and time.time() - st["t0"] >= cfg["minutes"] * 60:
                print("[定时] 到 %d 分钟了，这批剩下的下次再采" % cfg["minutes"])
                st["deadline_hit"] = True
                return post_page_state, True
            t_fetch = time.time()
            r, post_page_state, rec = await fetch_one(
                s, post_page_state, cap, tid, cfg["n_cmts"], pc, wanted,
                cfg["force"], st["attempts"].get(tid, 0) + 1)
            dt = time.time() - t_fetch
            if r == "dead":
                return None, False
            if r == "skip":
                st["skip"] += 1
                continue
            if r == "bad":
                st["bad"] += 1
                st["attempts"][tid] = st["attempts"].get(tid, 0) + 1
                print("  · %s  ✗ 没采到  %.1fs" % (tid, dt))
                continue
            st["attempts"].pop(tid, None)
            st["ok"] += 1
            n_img = len(rec["img_list"]) + sum(len(c["img_list"])
                                               for c in rec["comments"])
            # 这一帖实际花了多久（含探活/导航/评论/图片下载），和下面的 _pace
            # sleep **分开算**。混在一起就分不出"对面变慢了"和"我们自己节流"——
            # 那趟 39 小时掉 30 倍到底是哪种，之前就卡在这个分不开上。
            st.setdefault("t_win", []).append(dt)
            del st["t_win"][:-ROLL_WIN]
            print("  ✓ %s  %d 评论  %d 图  %.1fs  %s"
                  % (tid, len(rec["comments"]), n_img, dt, rec["title"][:30]))
            if st["ok"] % 10 == 0:
                w = st["t_win"]
                print("[耗时] 最近 %d 帖平均采集 %.1fs（不含节流 sleep）"
                      " | 本趟累计 采到 %d 跳过 %d 没采到 %d"
                      % (len(w), sum(w) / len(w), st["ok"], st["skip"], st["bad"]))
            await asyncio.sleep(_pace(st))
        return post_page_state, True

    # ---- --tid：指定补采，单趟跑完就退，不碰列表页 ----
    if cfg["tids"]:
        print("补采 %d 个：%s" % (len(cfg["tids"]), " ".join(cfg["tids"])))
        _, alive = await run_batch(cfg["tids"], None)
        return "done" if alive else "dead"

    # ---- 持续采集：列表页只往下翻，不回头 ----
    list_page = None        # 常驻列表页：只滚动，**永不被导航走**
    post_page = None        # 帖子页：goto_post / collect_post 都在这上面
    seen = set()            # 本轮扫下来见过的 tid（跨批次累积）
    last_beat = time.time()
    blip = 0                # 翻页连续炸了几次

    if cfg["minutes"]:
        print("[定时] 跑 %d 分钟就收工（%s 到点）"
              % (cfg["minutes"], time.strftime(
                  "%H:%M", time.localtime(st["t0"] + cfg["minutes"] * 60))))

    while ((cfg["rounds_max"] == 0 or st["round_no"] < cfg["rounds_max"])
           and not (cfg["minutes"]
                    and time.time() - st["t0"] >= cfg["minutes"] * 60)):
        st["round_no"] += 1
        st["state_dirty"] = True     # 这一轮撑下来了，profile 的功劳要记账

        fresh = await ensure_page(s, list_page, cap, "列表页")
        if fresh is None:
            return "dead"
        if fresh is not list_page:
            # 列表页是新开的：停在 about:blank，滚动位置也归零。
            # seen 必须一起清 —— 不清的话新文档顶部吐出来的 tid 全在 seen 里，
            # batch 会是空的，被误判成"到底"直接收工。已采过的靠 post.json
            # 跳过，重走一遍不花什么代价。
            seen.clear()
            try:
                tab_ok = await enter_list(fresh)
            except Exception as e:
                print("[列表] 进列表页失败(%s)，等会儿再来" % type(e).__name__)
                list_page = fresh
                await asyncio.sleep(5)
                continue
            if not tab_ok:
                # 钉不住"最新"就等于在扫热门（12000 帖而不是 123840）。
                # **这一轮宁可不采**：采了会把热门那批混进来，而且扫完热门
                # 会以为到底了。记作一次颗粒无收，让 3 次刹车去收它。
                st["bad"] += 1
                if _strike(st):
                    return "stop"
                st["reset"] = ("tab", False)
                return "restart"
        list_page = fresh

        try:
            batch = await next_batch(list_page, cap, seen, feed,
                                     want=cfg["per_round"],
                                     force=cfg["force"])
        except Exception as e:
            print("[列表] 翻页炸了(%s)" % type(e).__name__)
            blip += 1
            if blip > 5:
                print("[列表] 连着炸 %d 次" % blip)
                _strike(st)
                return "stop"
            await asyncio.sleep(random.uniform(2, 4))
            continue
        blip = 0

        if not batch:
            code, why = judge_bottom(feed)
            print("[列表] 翻不动了 %s" % why)
            if code == "bottom":
                print("[列表] 真到底了，吧里的帖都在 %s 里" % OUT)
                st["reset"] = ("bottom", True)     # 到底也换 profile —— 令牌是消耗品
                return "done"
            if code == "blocked":
                print("[列表] 这是被拦了，不是到底 —— 换 profile 冷一下再来")
                st["reset"] = ("blocked", True)
                return "restart"
            if code == "stuck":
                print("[列表] 页面卡住了（令牌好好的，不换 profile）—— 重开一个 session")
                st["reset"] = ("stuck", False)
                return "restart"
            # unknown：只能当到底，但日志里已经说清楚了
            print("[列表] 判不出到底还是被拦，按到底处理（上面那行说明了依据）")
            st["reset"] = ("unknown", True)
            return "done"

        ok0, bad0 = st["ok"], st["bad"]
        post_page, alive = await run_batch(batch, post_page)
        if not alive:
            return "dead"
        if st["deadline_hit"]:
            return "timeup"

        # 验证码这轮没能解开 = 信任坏了。这是最硬的换 profile 判据。
        if cap.fails > cap_fails0:
            print("[验证码] 这一场有 %d 次没解开 —— 换 profile 重来"
                  % (cap.fails - cap_fails0))
            st["reset"] = ("captcha", True)
            return "restart"

        # 颗粒无收 = 有失败、没产出。全是"跳过"（早就采过）的不算失败。
        if st["ok"] == ok0 and st["bad"] > bad0:
            if _strike(st):
                return "stop"
            print("[刹车] 这轮没采到东西（没采到 %d 条），连续 %d/%d 轮"
                  % (st["bad"] - bad0, st["consec_fail"], ROUND_FAIL_MAX))
            if st["consec_fail"] == 1:
                # 先只换 session 试试 —— 换 session 几秒钟，换 profile 要冷 30 分钟
                st["reset"] = ("rounds_fail", False)
                return "restart"
        else:
            st["consec_fail"] = 0

        # 心跳：平时闭嘴（长跑正常推进就别刷屏），超过 10 分钟才报一次
        now = time.time()
        if now - last_beat > 600:
            last_beat = now
            print("[心跳] 第 %d 轮  采到 %d / 跳过 %d / 没采到 %d  累计 %.0f 分钟  "
                  "见过 %d 个 tid  第 %s/%s 页"
                  % (st["round_no"], st["ok"], st["skip"], st["bad"],
                     (now - st["t0"]) / 60, len(seen),
                     feed.get("cur", "?"), feed.get("total", "?")))

    # 循环能走到这儿，只可能是因为"--rounds 用完了"或"--minutes 到点" ——
    # 真到底走的是上面 judge_bottom 的 "bottom" 分支，在那儿就 return "done" 了。
    # 所以这两个都是"正常收工但没到底"，都得给 "timeup"。
    # 曾经 --rounds 这里是 return "done"（注释还写着"两者退出码不一样"，其实一样）：
    # 于是退出码 0，而 0 的意思是"真到底" —— loop.bat 会在第一趟之后就停机，
    # 日志上还明明白白写着"真到底"，看不出是被轮数上限截断的。
    if cfg["minutes"] and time.time() - st["t0"] >= cfg["minutes"] * 60:
        return "timeup"
    if cfg["rounds_max"] and st["round_no"] >= cfg["rounds_max"]:
        return "timeup"
    return "done"                       # 兜底：理论上到不了


def _strike(st):
    """记一次"颗粒无收"，返回 True = 该停机交人工了。

    只有**连着**才累加：中间有任何一轮采到东西，计数器就归零（调用方负责）。
    """
    st["consec_fail"] += 1
    if st["consec_fail"] >= ROUND_FAIL_MAX:
        print("[刹车] 连着 %d 轮颗粒无收 —— 停机交人工，不自己转圈"
              % ROUND_FAIL_MAX)
        return True
    return False


async def main():
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]      # 重采已经采过的帖子

    def take(flag, default):
        if flag not in args:
            return default
        i = args.index(flag)
        v = int(args[i + 1])
        del args[i:i + 2]
        return v

    tids_arg = None
    if "--tid" in args:                             # 指定帖子采，不走列表页
        i = args.index("--tid")
        tids_arg = [t for t in args[i + 1].split(",") if t]
        del args[i:i + 2]
    per_round = take("--max-per-round", MAX_PER_ROUND)
    rounds_max = take("--rounds", 0)                # 0 = 一直翻到到底
    minutes = take("--minutes", 0)                  # >0 = 到点收工
    n_cmts = int(args[0]) if args else MAX_COMMENTS
    OUT.mkdir(parents=True, exist_ok=True)

    if force and rounds_max == 0:
        # 不带 --rounds 又开了 --force，每轮都会把见过的帖全部重采一遍 ——
        # 那是无限循环干活。默认只让你跑一轮。
        rounds_max = 1
        print("--force 会重采已采过的帖，默认只跑一轮（要更多加 --rounds N）")

    cfg = {"force": force, "per_round": per_round, "rounds_max": rounds_max,
           "minutes": minutes, "n_cmts": n_cmts, "tids": tids_arg}

    disk = load_state()                     # state.json 那份（跨进程累计）
    st = {                                  # 这一趟 session 之间共用的东西
        "ok": 0, "skip": 0, "bad": 0,
        "attempts": {},
        "round_no": 0,
        "consec_fail": disk["consec_fail"],
        "n_since_rest": 0,
        "state": disk["state"],
        "t0": time.time(),
        "deadline_hit": False,
        "state_dirty": False,
        "reset": None,                      # (为什么, 换不换 profile)
    }
    if disk["consec_fail"]:
        print("[刹车] 上次留下的连续失败计数 = %d/%d（成功一轮就清零）"
              % (disk["consec_fail"], ROUND_FAIL_MAX))
    if st["state"] >= 1:
        print("[节奏] state=%d（换过 profile），按 %g~%gs/帖 + 每 %d 帖歇 %ds 跑"
              % (st["state"], SLOW_LO, SLOW_HI, REST_EVERY, REST_SECS))

    code = EXIT_OK
    while True:
        # 上一轮判了要换 profile —— 这会儿浏览器已经关了，才能动手改名。
        # 顺序不能反：Windows 上浏览器还开着时 .profile 里的文件是锁着的。
        if st["reset"] is not None:
            why, swap = st["reset"]
            st["reset"] = None
            if swap:
                code = _do_reset(disk, st, why)
                if code is not None:        # 到上限了，停机
                    break
            else:
                print("[重置] （%s）只重开 session，profile 留着" % why)

        if minutes and time.time() - st["t0"] >= minutes * 60:
            code = EXIT_TIME
            break
        if rounds_max and st["round_no"] >= rounds_max:
            code = EXIT_TIME            # 跑满指定轮数 = 正常收工，但**没到底**
            break

        cd = disk.get("cool_until", 0) - time.time()
        if cd > 0:
            # 冷却跨过收工时间的，不在这儿干睡 —— 那等于白占着进程。
            # cool_until 已经落在 state.json 里，下次启动会先等完再开工。
            if minutes and time.time() + cd > st["t0"] + minutes * 60:
                print("[冷却] 这次冷却要 %.0f 分钟，跨过了 %d 分钟的收工点 —— "
                      "先收工，冷却留给下次启动（已记在 %s）"
                      % (cd / 60, minutes, STATE_FILE.name))
                code = EXIT_TIME
                break
            print("[冷却] 静默 %.0f 分钟（到 %s），让网络和对面的风控都冷下来"
                  % (cd / 60, time.strftime(
                      "%H:%M", time.localtime(disk["cool_until"]))))
            await asyncio.sleep(cd)

        async with AsyncStealthySession(headless=False, timeout=300000,
                                        user_data_dir=PROFILE) as s:
            verdict = await run_session(s, cfg, st)

        # 一轮撑下来了：给 profile 记一笔寿命
        if st["state_dirty"]:
            disk["profile_rounds"] += 1
            st["state_dirty"] = False
        disk["consec_fail"] = st["consec_fail"]
        save_state(disk)

        if verdict == "done":
            code = EXIT_OK
            break
        if verdict == "timeup":
            code = EXIT_TIME
            break
        if verdict == "dead":
            print("[会话] 浏览器整个没了")
            code = EXIT_DEAD
            break
        if verdict == "stop":
            code = EXIT_BRAKE
            break
        # verdict == "restart" -> 转回去处理 st["reset"]，重开一个 session

    print("完事：采到 %d，跳过 %d，没采到 %d，用时 %.0f 分钟，看 %s"
          % (st["ok"], st["skip"], st["bad"],
             (time.time() - st["t0"]) / 60, OUT))
    if st["bad"]:
        print("  没采到的下一轮会自动重试（已有 post.json 的跳过）")
    print("  退出码 %d（%s）  state=%d 累计换过 %d 次 profile"
          % (code, {EXIT_OK: "真到底", EXIT_BRAKE: "刹车停机，要人来看",
                    EXIT_TIME: "到点/跑满轮数收工，没到底",
                    EXIT_DEAD: "浏览器没了"}.get(code, "?"),
             disk["state"], disk["resets"]))
    return code


def _do_reset(disk, st, why):
    """换 profile：改名备份 + 记账 + 定冷却。返回 None = 继续跑，返回退出码 = 停机。

    **冷却和换 profile 是同一件事的两半**，不能只做一半：令牌已经作废了，
    不换就是拿死令牌接着撞；换了不冷却是立刻用新设备再撞一次，白换。
    """
    if disk["resets"] >= RESET_MAX:
        print("[重置] ✗ 已经换过 %d 次 profile（上限 %d）—— 停机交人工，"
              "别自己转圈烧下去" % (disk["resets"], RESET_MAX))
        return EXIT_BRAKE

    if disk["profile_rounds"]:
        print("[重置] 上一个 profile 撑了 %d 轮才被换掉" % disk["profile_rounds"])

    ok, info = stash_profile()
    if not ok:
        # 改名失败多半是浏览器没关干净（文件还锁着）。宁可不换也不能换一半 ——
        # 半残的 profile 下次启动会被当成一个认不出来的新设备，比不换还糟。
        print("[重置] ✗ %s —— 不敢删一半，停机交人工" % info)
        return EXIT_BRAKE

    disk["resets"] += 1
    disk["state"] = 1                    # 从这一次起，延迟策略生效
    st["state"] = 1
    cool = cooldown_for(disk["resets"])
    disk["cool_until"] = time.time() + cool
    disk["history"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "why": why, "rounds": disk["profile_rounds"],
                            "cooldown_min": cool // 60, "backup": info})
    disk["profile_rounds"] = 0
    st["n_since_rest"] = 0               # 换了新档，节奏重新数
    save_state(disk)
    print("[重置] 第 %d 次换 profile（原因 %s）：旧档改名到 %s，静默 %.0f 分钟"
          % (disk["resets"], why, info, cool / 60))
    return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
