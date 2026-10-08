import asyncio

from scrapling.fetchers import AsyncStealthySession
from skip_vc import (bg_rect, dump_capture, listen_vc, reset_saved, slide,
                     wait_saved)
from docr import pd_move


def on_handler_error(e):
    """异步下监听器里抛的异常会走到这，不会污染主流程"""
    print("[监听器异常]", type(e).__name__, e)


async def main():
    async with AsyncStealthySession(
        headless=False,                             # 有头
        #user_data_dir=r".profile",    # 固定 profile：登录态持久化（相对当前目录）
        timeout=300000,
    ) as s:
        # 先挂错误兜底，再挂业务监听
        s.context.on("error", on_handler_error)
        # 只能挂 response：request 事件传进来的是 Request，没有 .body()
        s.context.on("response", listen_vc)

        page = await s.context.new_page()           # 自己开，不走 fetch
        await page.goto("https://tieba.baidu.com/", wait_until="domcontentloaded")

        # locator() 是同步返回，不能 await；wait_for 没有 timeout 会永久挂住
        handle = page.locator(".passMod_slide-btn")
        reset_saved()                               # 只认这一轮新落的图
        print("在浏览器里手动把验证码点出来（最长 5 分钟）...")
        try:
            await handle.wait_for(state="visible", timeout=300000)
        except Exception as e:
            print("没等到滑块:", type(e).__name__)
            await dump_capture(page)                # 顺手把候选组件打出来
            await asyncio.to_thread(input, "回车退出...")
            return
        await asyncio.sleep(0.5)                    # 等入场动画跑完，否则 bbox 是插值坐标

        # 等 listen_vc 把两张图写完再往下，否则 pd_move 会读到上一轮的旧图
        if not await wait_saved():
            await asyncio.to_thread(input, "回车退出...")
            return

        d = await asyncio.to_thread(pd_move)        # cv2/ddddocr 是同步的，别卡事件循环
        gap_x = d["x"]                              # 注意是真实图上的 x，不是屏幕距离

        bg = await bg_rect(page, d["bg_w"])         # 用 pd_move 解出的真实宽度去认图
        if bg is None:
            print("[换算] ✗ 没量到背景图，按 1:1 用（几乎肯定不对，先解决这条）")
            distance = float(gap_x)
        else:
            nw, nh, w, h = bg
            scale = nw / w
            print("[换算] 真实 %dx%d / 显示 %.1fx%.1f -> scale x=%.3f y=%.3f"
                  % (nw, nh, w, h, scale, nh / h))
            if abs(scale - nh / h) > 0.02:
                print("[换算] ✗ x/y scale 不一致，不是等比缩放")
            distance = gap_x / scale
            print("[换算] 缺口 x=%d -> 屏幕距离 %.1fpx（行程上限约 %.1f）"
                  % (gap_x, distance, w - 40))

        await slide(page, handle, distance)

        await asyncio.to_thread(input, "回车退出...")
        print(await page.title())


asyncio.run(main())
