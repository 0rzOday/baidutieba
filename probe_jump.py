# -*- coding: utf-8 -*-
"""一次性探测：列表页能不能"直接跳到某个滚动位置"。

collect.py 里所有滚动都是 `scrollTop += 1000` 的**自增**，从没试过直接赋一个大值。
而"续爬锚点"要靠这个才划得来 —— 不跳的话，每重启一次都要从列表顶部滚到已采区的
尽头，已采条数越多越贵（734 条约 4.5 分钟，12 万条就是十几小时），而重扫本身
不产出任何数据。

第一次跑（2026-10-08）的结论：**跳不动**。见下面"实测"。

--------------------------------------------------------------------------
实测（2026-10-08，最新 tab，一屏 clientHeight=1020）：

  请求 scrollTop   实际落在   scrollHeight
       20000          1571      2591 -> 4031
       60000          3011      4031 -> 5384
      200000          4364      5384 -> 7233
      600000          6213      7233 -> 9192

两件事同时成立：

  1. **scrollTop 被 clamp 在 scrollHeight - clientHeight。** 设 600000 只落
     到 6213 —— 不是没生效，是上面根本没有内容可滚。列表是**边滚边往长里长**
     的，没滚到的地方压根不存在。
  2. **一次跳只换来约 1500px 的新内容**（≈ 8 个帖），和滚动一步 1000px 是
     同一个量级。所谓"跳"，实际效果就是"滚到当前已加载的底部，等 loader 再灌
     一小段"。

所以**没法传送到已采区的深处**：每跳一次前进约 1500px，还要等 loader；
而 `settle()` 之后逐步滚一步只要约 0.3~0.5s、前进 1000px。跳比一步步滚还慢，
"二分查找 seek" 的前提不成立。这条路到此为止，闸门关掉。

--------------------------------------------------------------------------
这个脚本仍然留着，改判据了，以后再怀疑这件事时直接重跑：

  1. 直接设 scrollTop = 大值之后，**落点那一屏渲染出来了吗**？
  2. 设的值**真的到达了吗**（还是被 clamp 了）？
  3. 读到的 tid 随实际 scrollTop 单调变化吗？

判据里最重要也最容易漏的是第 2 条。第一版就漏了：只看了"有没有往前走、
有没有渲染、tid 单调不单调"，三条全过，于是打印了个 ✓ "可以跳" —— 而真相是
每次都被 clamp 到已加载内容的底部，压根没跳到要去的地方。**"跳了"和"跳到了"
不是一回事。**

只读：不碰 out\、不碰 state.json、不改 collect.py。用真实 .profile（要它才不
弹验证码），所以跑之前先把别的采集进程关掉，不然 profile 被锁住起不来。
"""
import asyncio
import sys

from scrapling.fetchers import AsyncStealthySession

import collect
from collect import FEED_MIN, JS_TIDS, PROFILE, SCROLL_STEP, enter_list

# 直接赋值，不走 collect.JS_SCROLL 的 += 自增
JS_SET = """(v) => {
  const f = document.querySelector('.frs-page-wrap');
  if (!f) return -1;
  f.scrollTop = v;
  return Math.round(f.scrollTop);
}"""

JS_METRICS = """() => {
  const f = document.querySelector('.frs-page-wrap');
  if (!f) return null;
  return {top: Math.round(f.scrollTop),
          sh: Math.round(f.scrollHeight),
          ch: Math.round(f.clientHeight),
          links: document.querySelectorAll('a[href*="/p/"]').length};
}"""

# 每页 30 帖摊在几千 px 上，这几个目标大致是第 3 / 10 / 33 / 100 页。
# 故意放得很大（大到不可能到）—— 正好用来看 clamp 在哪。
TARGETS = (20000, 60000, 200000, 600000)


def dump(label, metrics, tids):
    if not metrics:
        print("  %-12s ✗ 页面上找不到 .frs-page-wrap" % label)
        return
    lo = min(tids, key=int) if tids else "-"
    hi = max(tids, key=int) if tids else "-"
    print("  %-12s scrollTop=%-7s scrollHeight=%-8s clientHeight=%-6s "
          "链接=%-4s tid %d 个  (%s .. %s)"
          % (label, metrics["top"], metrics["sh"], metrics["ch"],
             metrics["links"], len(tids), lo, hi))


async def main():
    try:
        async with AsyncStealthySession(headless=False, timeout=300000,
                                        user_data_dir=PROFILE) as s:
            page = await s.context.new_page()
            if not await enter_list(page):
                print("\n✗ 没切到「最新」—— 探的是全吧列表，切不过去就别测了")
                return 1

            print("\n===== 起手（顶部） =====")
            m0 = await page.evaluate(JS_METRICS)
            t0 = await page.evaluate(JS_TIDS)
            dump("顶部", m0, t0)
            if not m0:
                print("✗ 找不到滚动容器，后面没法测")
                return 1

            # (请求值, 实际落点, scrollHeight, tid 列表)；第一条是起手
            points = [(0, m0["top"], m0["sh"], t0)]

            for target in TARGETS:
                print("\n===== 直接 scrollTop = %d =====" % target)
                got = await page.evaluate(JS_SET, target)
                if got < target - 50:
                    print("  ⚠ 被 clamp：请求 %d，实际只到 %d（差 %d）—— "
                          "上面没有已加载的内容可滚。" % (target, got, target - got))
                tids = []
                for wait in (1.0, 2.0, 3.0):
                    await asyncio.sleep(wait)
                    m = await page.evaluate(JS_METRICS)
                    tids = await page.evaluate(JS_TIDS)
                    dump("等 %.0fs 后" % wait, m, tids)
                    if len(tids) >= FEED_MIN:
                        break
                points.append((target, m["top"] if m else -1,
                               m["sh"] if m else -1, tids))

            # ---------------- 判据 ----------------
            print("\n===== 结论 =====")
            jumped = points[1:]

            rendered = all(len(t) >= FEED_MIN for _, _, _, t in jumped)
            mins = [int(min(t, key=int)) for _, _, _, t in jumped if t]
            mono = all(mins[i] > mins[i + 1] for i in range(len(mins) - 1))
            advanced = [points[i + 1][1] - points[i][1]
                        for i in range(len(points) - 1)]
            clamped = [(t, a) for t, a, _, _ in jumped if a < t - 50]
            avg_adv = sum(advanced) / len(advanced) if advanced else 0

            print("  落点渲染出来了吗（tid >= %d）： %s" % (FEED_MIN, rendered))
            print("  设的值到达了吗：%s"
                  % ("没有，%d/%d 次被 clamp" % (len(clamped), len(jumped))
                     if clamped else "到了"))
            for t, a in clamped:
                print("      请求 %-7d 只到 %-7d" % (t, a))
            print("  每次跳实际前进： %s px（平均 %.0f，逐步滚一步是 %d）"
                  % (advanced, avg_adv, SCROLL_STEP))
            print("  实际位置越往下 tid 越小（单调）： %s  %s" % (mono, mins))

            print()
            if clamped and avg_adv <= SCROLL_STEP * 2.5:
                print("  ✗ 跳不动 —— 每次只推进约 %d px，和逐步滚一步（%d px）"
                      "同一个量级，\n    而跳一次还要等 loader。"
                      % (avg_adv, SCROLL_STEP))
                print("    scrollTop 被 clamp 在 scrollHeight - clientHeight，"
                      "\n    列表是边滚边长的，没滚到的地方根本不存在。"
                      "\n    => 锚点走不了\"传送\"这条路，别实现二分 seek。"
                      "\n    重扫只能逐步滚 —— 那就靠 settle() 把每步等短。")
            elif not rendered:
                print("  ✗ 落地是空的/没渲染，跳不动。")
            elif not mono:
                print("  ✗ tid 随位置不单调，就算能跳也不能二分。")
            elif not clamped and mono and rendered:
                print("  ✓ 真的跳到了，而且渲染正常、tid 单调 —— "
                      "\n    这种页面可以实现「seek 到已采区尽头再交回逐步滚动」。"
                      "\n    注意：跳过去的中间区段没进过 DOM，只能用来跳过已采区。")
            else:
                print("  ? 合成结论不明确，把上面几行贴出来再看。")
            return 0
    except Exception as e:
        print("\n✗ 起浏览器失败：%s: %s" % (type(e).__name__, e))
        print("  多半是 .profile 被占着 —— 先把别的 collect.py / Chrome 关掉再跑。")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
