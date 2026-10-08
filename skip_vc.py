import asyncio
import random
import sys

from docr import pd_move

# Windows 控制台默认 GBK，print 落到 ✓/✗/→ 这类字符会抛 UnicodeEncodeError。
# 要命的是它抛在"滑完之后的成功分支"上，看着像验证码没过，其实是打印炸了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BG_FILE, FRONT_FILE = "bg.jpeg", "font.png"

# 按 page 分桶。None 键 = 认不出页时的兜底（main.py 单 page 手动那套走这条）。
# 双 page 下必须分桶：A 页的 bg 配上 B 页的 front，滑的是 B 页却用 A 的底图，
# 缺口位置整个是错的 —— 而且看起来就像"轨迹不过"，不像 bug。
_round = {}                    # page -> {kind: 文件名}
_saved = {}                    # page -> Event


def _page_of(p):
    """这个响应是哪个 page 发出来的。

    Response.frame -> Frame，Frame.page -> Page（playwright API 已核实，
    不做协议往返，所以不会返回 None）。取不到就返回 None，调用方按
    "认不出页"走 None 桶那套。
    """
    try:
        return p.frame.page
    except Exception:
        return None


def _ev(page):
    e = _saved.get(page)
    if e is None:
        e = _saved[page] = asyncio.Event()
    return e


def reset_saved(page=None):
    """刷新页面前调用：只认下一页新落的那对图，别读上一轮的旧图"""
    _round.pop(page, None)
    _ev(page).clear()


async def wait_saved(page=None, timeout=30):
    """等这一轮的 bg + front 都落地"""
    try:
        await asyncio.wait_for(_ev(page).wait(), timeout)
        return True
    except asyncio.TimeoutError:
        print("[验证码] 等图超时")
        return False


# 判断数据类型
def img_kind(b: bytes) -> str:
    if b.startswith(b'\x89PNG\r\n\x1a\n'): return 'png'
    if b.startswith(b'\xff\xd8\xff'):      return 'jpeg'
    if b.startswith(b'GIF8'):              return 'gif'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP': return 'webp'
    if b.startswith(b'BM'):                return 'bmp'
    return 'unknown'


async def _grab_img(p):
    """把 /cap/img?ak= 的图落盘。返回 True = 这一轮的 bg + front 都齐了

    bg 和 front 分两个请求下来（jpeg / png），所以按 kind 配对，
    凑齐一对才算一轮 —— 只收到一张就去识图，读到的会是上一轮的旧图。
    """
    try:
        body = await p.body()                  # 只取一次；302/流式响应会抛异常
    except Exception as e:
        print("[验证码] 取 body 失败:", type(e).__name__, e, p.url)
        return False
    kind = img_kind(body)
    name = {"jpeg": BG_FILE, "png": FRONT_FILE}.get(kind)
    if not name:
        print("[验证码] 未知图片类型:", kind, len(body), "字节", p.url)
        return False
    with open(name, "wb") as f:
        f.write(body)
    page = _page_of(p)
    print("[验证码] 收到", name, len(body), "字节")
    d = _round.setdefault(page, {})
    d[kind] = name
    if len(d) < 2:                         # 只按同一个 page 配对
        return False
    _round.pop(page, None)
    _ev(page).set()
    return True


# 只落图的那种用法（main.py 手动触发验证码走这条）
async def listen_vc(p):
    if "/cap/img?ak=" not in p.url:
        return
    await _grab_img(p)


class Captcha:
    """验证码钩子：一对图到手就自己去解，主流程完全不用管验证码。

        cap = Captcha()
        s.context.on("response", cap.hook)     # 监测写成钩子
        cap.pages = [list_page, post_page]     # 想交叉核对就填（可省）

    解题过程在 solve_captcha 里（没过就刷新页面重新判断）。

    双 page 的坑：验证码可能弹在列表页，也可能弹在帖子页，钩子必须知道去哪个
    page 上滑。原来靠 bind() 记一个 page —— 单 page 够用，双 page 就抓瞎了。
    现在一律问响应本身（_page_of）。

    **故意不做"认不出就退回某个 page"的兜底**：solve_captcha 把"15 秒内没看到
    滑块"当成**已经解开**（见那边的注释），所以在错的 page 上滑会静默报成功、
    busy 归位，而真验证码还挂在另一个 page 上没人管 —— 之后每轮都读不到东西，
    一句报错都没有。宁可当场认输、把话打出来。
    """

    def __init__(self, retries=3):
        self.retries = retries
        self.busy = False
        self.task = None
        self.pages = []                 # 交叉核对用，见 _cross_check
        # retries 次全用完还是没过 = "解不掉"。主流程拿这个数当换 profile 的判据
        # （见 collect.py 里 cap.fails）。光看 busy 不行：busy 只说明"在解"。
        self.fails = 0

    async def hook(self, p):
        if "/cap/img?ak=" not in p.url:
            return
        if not await _grab_img(p):      # 还没凑齐一对，等下一张
            return
        # busy 必须**在这里**就置上，不能等 _run 里再置：pyee 每个监听器是各自
        # 一个 task，两个钩子可以都停在上面那个 await 里，然后同一轮双双看到
        # busy=False、双双起一个解题任务 —— 两个任务同时拽同一个 page.mouse，
        # 滑出来必然乱，而且看着像"轨迹不过"不像 bug。中间没 await 才安全。
        if self.busy:                   # 已经在解了，别起第二个
            print("[验证码] 已经在解了，跳过这次触发")
            return
        page = _page_of(p)
        if page is None:
            print("[验证码] 认不出弹在哪个 page 上，跳过这次")
            return
        self.busy = True
        self.task = asyncio.create_task(self._run(page))

    async def _run(self, page):
        try:
            print("[验证码] 弹在", page.url[:70])
            ok = await solve_captcha(page, self.retries, images_ready=True)
            print("[验证码] ——%s——" % ("过了" if ok else "✗ 重试用完还是没过"))
            if ok:
                await self._cross_check(page)
            else:
                self.fails += 1         # 主流程读这个数决定要不要换 profile
        except Exception as e:
            print("[验证码] 解的时候炸了:", type(e).__name__, e)
            self.fails += 1             # 炸了和解不掉一个待遇：这次信任是坏的
        finally:
            self.busy = False

    async def _cross_check(self, solved_on):
        """报成功之后，看一眼别的 page 上还剩没剩滑块。

        solve_captcha 是"没滑块就算过"，所以在错的 page 上滑它也会报成功。
        这里补一刀：别的 page 还挂着滑块 = 刚才那个"过"是假的，得当场说清楚。
        """
        for p in self.pages:
            if p is solved_on:
                continue
            try:
                if await p.locator(".passMod_slide-btn").count():
                    print("[验证码] ⚠ 这边报过了，但另一个 page 上还有滑块 —— "
                          "多半滑错页了，下一轮可能还是采不到")
            except Exception:
                continue


# ==================== 定位辅助 ====================

# 找背景图：取 naturalWidth*naturalHeight 最大的可见 img
# 注意 x/y 是 frame 内坐标，跨 frame 直接喂 mouse 会偏；
# 位置一律用 locator.bounding_box() 取（它会换算到主 frame 视口）
BG_JS = """() => {
  let best = null;
  for (const img of document.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width < 50 || r.height < 20 || !img.naturalWidth) continue;
    if (!best || img.naturalWidth * img.naturalHeight > best.nw * best.nh) {
      best = {cls: (img.className || '') + '', src: (img.src || '').slice(0, 80),
              x: r.x, y: r.y, w: r.width, h: r.height,
              nw: img.naturalWidth, nh: img.naturalHeight};
    }
  }
  return best;
}"""

# 选择器没命中时，把所有长得像验证码的组件 dump 出来照着改
DUMP_JS = """() => {
  const out = [];
  document.querySelectorAll('div,img,button,span').forEach(el => {
    const c = (el.className || '') + '';
    if (!/yidun|slide|jigsaw|bg|control|track|captcha|verify|passMod/i.test(c)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) return;
    out.push([el.tagName, c.slice(0, 55), Math.round(r.x), Math.round(r.y),
              Math.round(r.width) + 'x' + Math.round(r.height),
              el.tagName === 'IMG' ? el.naturalWidth + 'x' + el.naturalHeight : '']);
  });
  return out;
}"""


async def dump_capture(page):
    """把所有 frame 里长得像验证码的组件打出来（定位不到时用）"""
    hit = False
    for f in page.frames:
        try:
            rows = await f.evaluate(DUMP_JS)
        except Exception:
            continue
        if rows:
            hit = True
            print(f"--- frame: {f.url[:80]}  ({len(rows)} 个候选)")
            for tag, cls, x, y, wh, nat in rows:
                print(f"    {tag:<5} {cls:<57} pos=({x},{y}) {wh:<10} nat={nat}")
    if not hit:
        print("[dump] 一个候选都没有 —— 验证码是不是还没弹出来？")


# 列出所有够大的 img，用来核对"选中的背景图对不对"
IMGS_JS = """() => [...document.querySelectorAll('img')].map(i => {
  const r = i.getBoundingClientRect();
  return [(i.className || '') + '', Math.round(r.x), Math.round(r.y),
          Math.round(r.width) + 'x' + Math.round(r.height),
          i.naturalWidth + 'x' + i.naturalHeight];
}).filter(a => parseInt(a[3]) >= 50)"""


# 把手自检：尺寸、中心点被谁盖住、是不是它自己
PROBE_JS = """el => {
  const r = el.getBoundingClientRect();
  const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  return {w: Math.round(r.width), h: Math.round(r.height),
          top: t ? (t.tagName + '.' + (t.className || '')).slice(0, 60) : null,
          hitSelf: !!(t && (t === el || el.contains(t) || t.contains(el)))};
}"""


async def probe(handle):
    """把手自检。这段跑在把手自己所在的 frame 里，坐标是 frame 内的，
    和 page.mouse 用的视口坐标不是一个系 —— 只用来判断"盖没盖住/命中没命中"，
    不要拿它算鼠标位置。"""
    return await handle.evaluate(PROBE_JS)


async def bg_rect(page, real_w=None):
    """量背景图。返回 (真实宽, 真实高, 显示宽, 显示高)，找不到返回 None

    真实宽/显示宽 就是 真实图 x -> 屏幕 px 的换算比例，别写死 /3。

    real_w 是 pd_move 解出来的真实宽度，传了就按它去认图 ——
    不然"全页最大的 img"很容易挑到 banner 之类，scale 直接garbage。
    """
    for f in page.frames:
        try:
            imgs = await f.evaluate(IMGS_JS)
            r = await f.evaluate(BG_JS)
        except Exception:
            continue
        if imgs:
            print(f"[图] frame={f.url[:60]} 共 {len(imgs)} 张")
            for cls, x, y, wh, nat in imgs:
                print(f"     {cls:<45} pos=({x},{y}) 显示={wh:<10} 真实={nat}")
        if not (r and r["w"]):
            continue
        if real_w and r["nw"] != real_w:
            print(f"[背景图] 最大的那张是 {r['nw']}x{r['nh']}，"
                  f"不等于识别用的 {real_w} —— 换按真实宽度找")
            hit = [a for a in imgs if a[4].split("x")[0] == str(real_w)]
            if hit:
                cls, x, y, wh, nat = hit[0]
                w, h = wh.split("x")
                print(f"[背景图] 按真实宽度命中 {cls or '(无名)'} 显示={wh}")
                return int(nat.split("x")[0]), int(nat.split("x")[1]), float(w), float(h)
            print("[背景图] 按真实宽度也没找到，检查是不是在别的 frame / 是 CSS background")
            return None
        print(f"[背景图] 选中 {r['cls']} {r['src']}")
        return r["nw"], r["nh"], r["w"], r["h"]
    return None


# ==================== 滑动 ====================

async def slide(page, handle, distance: float):
    """滑到 distance 处。

    速度走梯形：起步加速 -> **匀速主段** -> 收尾减速，全程约 1.2s。
    原来那条 1-(1-t)**3 是纯 ease-out，先快后慢、没有匀速段，起手那一下太
    "机器"；匀速段才是人手拖拽的主要特征，末端那两下超调+校正只是收尾。
    """
    # ---- 滑动前先自检：这一步是为了分清"坐标没落上"和"拖动没被接住" ----
    p = await probe(handle)
    print("[探针] 把手 %dx%d  命中自身=%s  中心处顶层元素=%s"
          % (p["w"], p["h"], p["hitSelf"], p["top"]))
    if p["w"] > 120 or p["h"] > 120:
        print("[探针] ✗ 尺寸不像把手（应该 40 上下）—— 多半选中了容器而不是把手")
    if not p["hitSelf"]:
        print("[探针] ✗ 把手中心被上层元素盖住了，mousedown 到不了它")

    b = await handle.bounding_box()
    x0, y0 = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    print("[探针] bbox=%s -> 起点 (%.1f, %.1f)" % (b, x0, y0))

    await page.mouse.move(x0, y0)
    print("[探针] 鼠标移上去后 hover=%s"
          % (await handle.evaluate("el => el.matches(':hover')")))

    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.down()

    # 梯形速度曲线：每步一个速度权重，累计位移再归一化到 1。
    # t 取 i/(n+1) 而不是 i/n —— 否则最后一步 t=1，收尾那条式子正好归零，
    # 白走一步（位移 0 的重复 move）。
    n = 60
    v = []
    for i in range(1, n + 1):
        t = i / (n + 1)
        if t < 0.15:
            v.append(t / 0.15)                     # 起步：加速
        elif t < 0.80:
            v.append(1.0)                          # 主段：匀速
        else:
            v.append((1 - t) / 0.20)               # 收尾：减速
    tot = sum(v)

    k = 0.0
    for x in v:
        k += x / tot
        await page.mouse.move(
            x0 + distance * k + random.uniform(-0.4, 0.4),
            y0 + random.uniform(-1.2, 1.2),        # y 要有抖动
        )
        # 步长均匀 ≈ 匀速；不 sleep 就是超人手速
        await asyncio.sleep(random.uniform(0.013, 0.025))

    await asyncio.sleep(random.uniform(0.10, 0.22))                   # 到点了停一下再松手
    await page.mouse.move(x0 + distance + random.uniform(2, 5), y0)   # 冲过头
    await asyncio.sleep(random.uniform(0.04, 0.08))
    await page.mouse.move(x0 + distance, y0)                          # 修回精确位置
    await asyncio.sleep(random.uniform(0.05, 0.1))
    await page.mouse.up()

    # ---- 滑完回读位置，把"没滑动"从肉眼感觉变成打印出来的事实 ----
    after = await handle.bounding_box()
    moved = after["x"] - b["x"]
    print("[结果] 把手 x %.1f -> %.1f（动了 %.1fpx，期望 %.1fpx）"
          % (b["x"], after["x"], moved, distance))
    if abs(moved - distance) > 5:
        print("[结果] ✗ 没滑动 / 滑完弹回 —— 看上面哪条探针打了 ✗")
    else:
        print("[结果] ✓ 位置对了，剩下就看轨迹过不过检测")


async def solve_captcha(page, retries=3, images_ready=False):
    """解验证码的一整件事：等滑块 -> 识图 -> 滑 -> 查过没过；没过就刷新页面重新判断。

    images_ready=True 表示钩子已经把这一轮的图落盘了（钩子进来时就是这种情况），
    第一轮直接用，不用再等。

    返回 True = 过了。
    """
    handle = page.locator(".passMod_slide-btn")
    for i in range(1, retries + 1):
        try:
            await handle.wait_for(state="visible", timeout=15000)
        except Exception:
            print("[验证码] 第%d次：没滑块，当它过了" % i)
            return True
        await asyncio.sleep(0.5)                # 等入场动画跑完，否则 bbox 是插值坐标

        if not images_ready:
            if not await wait_saved(page):
                print("[验证码] 第%d次：没等到图，刷新重来" % i)
                await _reload(page)
                continue
        images_ready = False

        d = await asyncio.to_thread(pd_move)     # cv2/ddddocr 是同步的，别卡事件循环
        bg = await bg_rect(page, d["bg_w"])      # 用 pd_move 解出的真实宽度去认图
        if bg is None:
            # 量不到背景图就没法换算，1:1 硬滑是瞎滑 —— 换张新题比重滑一次划算
            print("[验证码] ✗ 没量到背景图，这一轮不滑，刷新换题")
            await _reload(page)
            continue

        nw, nh, w, h = bg
        scale = nw / w
        distance = d["x"] / scale
        limit_px = w - 46                        # 把手宽约 46，滑过头没意义
        if not 0 < distance <= limit_px:
            print("[验证码] ✗ 距离 %.1f 不合理（要 0 < x <= %.1f），"
                  "这一轮不滑，刷新换题" % (distance, limit_px))
            await _reload(page)
            continue
        print("[验证码] 真实 %dx%d / 显示 %.1fx%.1f -> scale x=%.3f y=%.3f"
              % (nw, nh, w, h, scale, nh / h))
        print("[验证码] 缺口 x=%d -> 屏幕距离 %.1fpx（上限 %.1f）"
              % (d["x"], distance, limit_px))

        await slide(page, handle, distance)

        # 滑完看它还在不在 —— 这是"过没过"的唯一判据
        await asyncio.sleep(1.5)
        if await handle.count() == 0:
            print("[验证码] ✓ 第%d次过了" % i)
            return True
        print("[验证码] ✗ 第%d次没过，刷新页面重来" % i)
        await _reload(page)

    return False


async def _reload(page):
    """刷新页面重新判断 —— 先清掉这一页这一轮的图，免得下一轮读到旧的"""
    reset_saved(page)
    await page.reload(wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.5, 2.5))
