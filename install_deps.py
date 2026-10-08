# -*- coding: utf-8 -*-
"""新机器上一键把依赖装齐：python install_deps.py

为什么要包一层，而不是直接 `pip install -r requirements.txt`：

  浏览器二进制（patchright 的 chromium）**不在 PyPI 上**，pip 装不到。少了它，
  pip 那步会一路绿灯，然后在 AsyncStealthySession(...) 那一行才抛
  Executable doesn't exist —— 看着像代码问题，其实是环境没装完。
  所以"装完"= pip 一步 + patchright install 一步，这个脚本把两步串起来。

只装依赖，不碰 .profile / out / state.json —— 那些是数据不是依赖。
"""
import os
import subprocess
import sys

# 先切到脚本所在目录：requirements.txt 和 docr.py 都在这儿，从别处跑找不到
_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 控制台默认 GBK，print 落 ✓/✗ 会抛 UnicodeEncodeError。
# 这个项目已经被这个坑咬过一次（抛在成功分支上，看着像失败），照 skip_vc.py 办。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REQ = "requirements.txt"


def run(args, what, env=None):
    print("\n=== %s ===" % what)
    print("$ %s" % " ".join(args))
    rc = subprocess.run(args, env=env).returncode
    if rc:
        print("✗ %s 失败（退出码 %d）" % (what, rc))
    return rc == 0


def main():
    print("Python %s  (%s)" % (sys.version.split()[0], sys.executable))

    # 清单是从 3.11.9 上导的。换个次版本，numpy / opencv 的 wheel 未必有，
    # pip 会退化成源码编译，然后在缺编译器的地方失败 —— 报错跟依赖本身无关，
    # 很难查。所以这里先拦一道。
    if sys.version_info[:2] != (3, 11):
        print("⚠ 清单是从 Python 3.11.9 上导出来的，这台是 %d.%d。"
              % sys.version_info[:2])
        print("  numpy 2.4.6 / opencv-python 4.13 未必有对应 wheel，pip 可能转去"
              "源码编译然后失败。建议先装个 3.11.x。")
        print("  接着跑也行，就是失败时记得往这儿想。")

    if not os.path.exists(REQ):
        print("✗ 当前目录没有 %s —— 是不是没把 collect.py 那些一起拷过来？" % REQ)
        return 1

    # ---- 第 1 步：Python 包 ----
    if not run([sys.executable, "-m", "pip", "install", "-r", REQ],
               "装 Python 包"):
        print("\npip 这步没过，先解决它，浏览器二进制装了也用不上")
        return 1

    # ---- 第 2 步：浏览器二进制 ----
    # 官方 cdn.playwright.dev 在国内实测只有 76 KB/s，192MB 要 42 分钟；
    # npmmirror 的镜像 530 KB/s，6 分钟就完。同一份文件（大小分毫不差）。
    # patchright 认 PLAYWRIGHT_DOWNLOAD_HOST，但只认当前进程的环境变量，
    # 开个新窗口就没了 —— 所以在这儿设，别指望用户记着 export。
    # 已经自己设过的就不覆盖，方便有人要走代理或别的镜像。
    env = None
    if not os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST"):
        MIRROR = "https://cdn.npmmirror.com/binaries/playwright"
        env = dict(os.environ, PLAYWRIGHT_DOWNLOAD_HOST=MIRROR)
        print("\n浏览器下载走国内镜像（官方 cdn 在国内容易 40 分钟起步）：")
        print("  PLAYWRIGHT_DOWNLOAD_HOST=%s" % MIRROR)
        print("  想走官方/代理就自己先 export 这个变量，这儿不会覆盖")

    browser_ok = run([sys.executable, "-m", "patchright", "install", "chromium"],
                     "装浏览器二进制（patchright 的 chromium）", env=env)

    # ---- 第 3 步：自检 ----
    # 按"缺了会在哪一步炸"排序，不是按字母。
    print("\n=== 自检 ===")
    bad = []
    for m in ("scrapling", "patchright", "playwright", "browserforge",
              "msgspec", "cv2", "numpy", "docr", "skip_vc", "collect"):
        try:
            __import__(m)
            print("  ✓ %s" % m)
        except Exception as e:
            print("  ✗ %s  %s: %s" % (m, type(e).__name__, e))
            bad.append(m)

    # ddddocr 是可选的：docr.py 里是惰性导入，没有就退到自研兜底
    try:
        import ddddocr
        print("  ✓ ddddocr %s（验证码缺口识别）"
              % getattr(ddddocr, "__version__", ""))
    except Exception:
        print("  · ddddocr 没装 —— 能跑，走 docr.py 自研的 NCC/chamfer 兜底，")
        print("    但实测 27 组里只中 16~17（ddddocr 全中），解不开的次数会偏多。")
        print("    想装：%s -m pip install ddddocr" % sys.executable)

    if bad or not browser_ok:
        print("\n还差点东西，看上面 ✗ 的行。")
        return 1

    # ---- 收尾 ----
    print("\n依赖齐了。还差一步（脚本不替你干，这是改源码）：")
    print("  改 collect.py 55~57 行的三个绝对路径 —— OUT / PROFILE / STATE_FILE")
    print("  说明见 迁移说明.md §3")
    print("\n然后 cd 到项目目录，跑这个验证：")
    print("  python -u collect.py 5 --rounds 1 --max-per-round 3")
    print("看到 [列表] 当前 tab=最新(全吧) 和 退出码 0（正常收工）就算通了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
