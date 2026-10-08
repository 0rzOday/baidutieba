# -*- coding: utf-8 -*-
"""
易盾滑块缺口定位

背景
----
易盾的 front 图不是一张"滑块小图"，而是一张约 60x158 的竖条，
真正的拼图块只占顶部一小块，下面全是透明/白色填充。
把它整张丢给 ddddocr.slide_match 会连着踩两个坑：

  1. 模板高度按 158 算，竖向被拉伸，匹配自然歪；
  2. slide_match 返回的是**中心点**，不是左上角
     （源码 ddddocr/core/slide_engine.py：
      center_x = max_loc[0] + target_w // 2）。

两条叠一起就是"总是偏右下一点"：x 大约 +27，y 恒在 80 上下 ——
因为 center_y = match_y + 158//2，而 match_y 只在 0~2 之间动，
竖直方向其实根本没在搜。正确做法是先按 alpha 裁出拼图块再喂进去，
最后把中心点减掉半宽半高换回左上角。

关键观察
--------
拼图块在竖条里的纵向位置是固定的，做题时它只做水平平移，
所以缺口在背景图里的 y 就等于这个偏移量 —— y 是已知的，只需要解 x。

做法
----
`locate_gap` 默认走 `ddddocr_locate`：裁出拼图块喂给 ddddocr，再把返回的
中心点换算成左上角。27 组实测全中，是这里最靠得住的一条路。

同文件另写了两条不依赖 ddddocr 的线索，只在没装 ddddocr 时兜底
（27 组里 NCC 中 17、chamfer 中 16，明显不如 ddddocr，别当主力）：

1. NCC（ncc_map）。背景的缺口里常常保留着原图内容，易盾只是把它整体
   压暗了一层。零均值归一化的相关性对亮度/对比度的整体缩放免疫，所以
   缺口处会出很尖的峰（实测 0.45~0.92，别处多在 0.2 以下）。只在拼图块
   真正的形状内统计，透明部分（内凹的两个角）在两边都掩掉。
   缺点：缺口被画成别的样子（不保留原图内容）时就失效。

2. chamfer（chamfer_map）。拼图块轮廓对背景边缘距离场：
   背景 -> Canny -> 距离变换 dt，模板轮廓归一化后与 dt 做 TM_CCORR，
   匹配值就是"轮廓点到最近边缘的平均距离"。再按"块内部平均距离"归一化
   一次，消掉纹理密集处的假阳性。缺点：纹理密的地方仍然容易误判。
"""
import cv2
import numpy as np


def _to_bgr(img):
    if img.ndim == 3 and img.shape[2] == 4:
        return img[:, :, :3]
    return img


def _decode(data):
    """bytes -> ndarray；已经是 ndarray 就原样返回"""
    if isinstance(data, (bytes, bytearray)):
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError('图片解码失败')
        return img
    return data


def piece_box(front, alpha_threshold=128):
    """只算拼图块的外接矩形，返回 (y0, y1, x0, x1)（都是闭区间端点）。

    给"只要位置、不要形状"的调用方用 —— ddddocr 主路就是这种：它要的是
    把矩形抠出来当图片喂进去，不需要知道框内哪个像素属于块。

    切片时记得左闭右开：front[y0:y1 + 1, x0:x1 + 1]。

    两级阈值分工：alpha>32 定**框**（把抗锯齿软边一起框进来，框才不会
    随图片抖动），框内再用 alpha_threshold 定**形状**。

    实测 y0 随样本在 10~93 之间变（竖条 158 高、块 55 高，纵向余量 103），
    而 x0 恒为 3（竖条 60 宽、块 55 宽，横向余量只有 5）。y0 这个变化量
    就是缺口在背景图里的 y，必须逐样本取。
    """
    if front.ndim != 3 or front.shape[2] != 4:
        raise ValueError('front 必须是带 alpha 通道的 PNG')
    alpha = front[:, :, 3]
    ys, xs = np.where(alpha > 32)
    if len(ys) == 0:
        raise ValueError('front 的 alpha 通道全透明')
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def cut_piece(front, alpha_threshold=128):
    """从竖条里裁出真正的拼图块（形状）。

    掩码不是"矩形"——矩形里约 37% 的像素是透明的（凸起旁的凹角、中间的
    圆洞），掩码把那些剔掉，只留实心部分。它带走的是"哪个像素属于块"这个
    决策，下游拿到就能直接用，不必知道 alpha 和阈值的存在。

    行数/列数从掩码的 .shape 读得出来，因为形状同时承载了尺寸。

    :return: (piece_mask, piece_y, piece_x)
             piece_mask 是二值掩码，piece_y / piece_x 是它在竖条里的偏移，
             piece_y 同时就是缺口在背景图里的 y。
    """
    y0, y1, x0, x1 = piece_box(front, alpha_threshold)
    alpha = front[:, :, 3]
    piece_mask = (alpha[y0:y1 + 1, x0:x1 + 1] > alpha_threshold).astype(np.uint8)
    return piece_mask, y0, x0


def cut_piece_rgb(front, alpha_threshold=128):
    """裁出拼图块的 (掩码, 彩色块, y, x)，给像素匹配用。"""
    mask, y0, x0 = cut_piece(front, alpha_threshold)
    ph, pw = mask.shape
    rgb = _to_bgr(front)[y0:y0 + ph, x0:x0 + pw]
    return mask, rgb, y0, x0


def chamfer_map(piece_mask, background, norm=False):
    """轮廓对背景边缘距离场的 chamfer 匹配打分图。

    score[y, x] = 把轮廓左上角放在 (x, y) 时，轮廓点到最近边缘的平均像素
    距离。越小越可信（实测正确位置 ~0.5，错误位置普遍 >1.9）。

    norm=True 时除以"拼图块内部点到最近边缘的平均距离"，即
        ratio = 轮廓均值 / 内部均值
    纯粹的轮廓 chamfer 有个硬伤：在纹理密集的地方（远山、树丛），随便摆
    一个轮廓都能贴着某条边缘，分数一样很低 —— 实测这就是 x 落到左半边
    群山上的原因。真缺口的特点是"轮廓严丝合缝、内部却是一片平坦"，
    所以两者的比值能把纹理区压下去：真位置 ~0.2，纹理区接近 1。
    """
    k = np.ones((3, 3), np.uint8)
    outline = cv2.morphologyEx(piece_mask, cv2.MORPH_GRADIENT, k).astype(np.float32)
    n = outline.sum()
    if n == 0:
        raise ValueError('拼图块轮廓为空，检查 alpha_threshold')
    outline /= n                                   # 归一化后 TM_CCORR 即平均值

    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    dist = cv2.distanceTransform(255 - cv2.Canny(gray, 50, 150),
                                 cv2.DIST_L2, 3).astype(np.float32)
    score = cv2.matchTemplate(dist, outline, cv2.TM_CCORR)
    if not norm:
        return score

    inner = cv2.erode(piece_mask, np.ones((11, 11), np.uint8)).astype(np.float32)
    if inner.sum() == 0:                           # 块太小，退化回原始打分
        return score
    inner /= inner.sum()
    base = cv2.matchTemplate(dist, inner, cv2.TM_CCORR)
    # 分母兜个底：内部本来就极干净（大海/天空）时比值会爆掉，没意义
    return score / np.maximum(base, 0.5)


def ncc_map(piece_mask, piece_bgr, background, erode_px=1):
    """拼图块像素对背景的掩码归一化互相关（NCC）。

    背景的缺口里保留着原图内容 —— 易盾只是把它整体压暗了一层。零均值
    归一化的相关性对亮度和对比度的整体缩放是免疫的，所以缺口处会出很尖
    的峰（实测 0.45~0.83），而别处大多在 0.2 以下。

    只在拼图块真正的形状内统计：透明部分（内凹的两个角）在两边都掩掉，
    否则那里的底色会污染统计。掩码先腐蚀一圈，避开抗锯齿边。

    :return: NCC 图，形状同 matchTemplate 的 valid 区域，越大越可信
    """
    m = piece_mask.astype(np.uint8)
    if erode_px:
        m = cv2.erode(m, np.ones((2 * erode_px + 1,) * 2, np.uint8))
    m = m.astype(np.float32)
    n = float(m.sum())
    if n == 0:
        raise ValueError('掩码腐蚀后为空，拼图块太小')

    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY).astype(np.float32)
    t = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    tp = t * m

    s_t = float(tp.sum())                       # 模板侧全是常数
    s_tt = float((tp * t).sum())
    s_i = cv2.matchTemplate(gray, m, cv2.TM_CCORR)
    s_ii = cv2.matchTemplate(gray * gray, m, cv2.TM_CCORR)
    s_ti = cv2.matchTemplate(gray, tp, cv2.TM_CCORR)

    num = s_ti - s_t * s_i / n
    var_t = s_tt - s_t * s_t / n
    var_i = s_ii - s_i * s_i / n
    return num / np.sqrt(np.maximum(var_t * var_i, 1e-6))


NCC_MIN_PEAK = 0.75
"""NCC 峰值到这个数才单独采信。17 组实测：峰值 ≥0.72 的那些 NCC 全对，
峰值 ≤0.64 的三组全错。0.75 落在这个空档里，再高就会把峰值 0.77 的正确
结果一起否掉。"""


def _to_bytes(img):
    """ndarray -> 编码后的 bytes（ddddocr 只吃 bytes）。"""
    if isinstance(img, (bytes, bytearray)):
        return bytes(img)
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise ValueError('图片编码失败')
    return buf.tobytes()


def ddddocr_locate(front, background, alpha_threshold=128, ocr=None):
    """用 ddddocr 定位 —— 这里最靠得住的一条路（27 组实测全中）。

    两个坑，少踩一个都偏：
      * 必须喂按 alpha 裁出来的拼图块（55x55），不能喂整条（60x158）。
        喂整条的话模板高度按 158 算，竖向被拉伸，而且中心点也按竖条算。
      * slide_match 返回的是**中心点**（源码 slide_engine.py：
        center_x = max_loc[0] + target_w // 2），要减掉半宽半高换回左上角。
    simple_target 必须 False，为 True 时整条和裁块两种情况都是垃圾。

    喂进去的 crop 是**矩形**（含块 bbox 内凹的透明区），不是抠出来的块 ——
    ddddocr 的模板需要那些透明区一起构成边缘。而 alpha 本身它并不用：
    源码走 image_to_numpy(pil, 'RGB')，第一步就把 alpha 丢了，透明区的
    RGB 成为填充。实测带 alpha 与只给 RGB 结果逐行相同，所以这里传的是
    原始 RGBA 只是"忠实地带上"，换成 [..., :3] 也一样。
    （透明区 RGB 实测是 (0,0,0)，等于块贴在黑底上。填别的均匀色也行，
    但填白会吃掉块边缘的亮色对比度、让 Canny 漏掉一段轮廓 —— 见
    分析/compare_slide.py 的实测记录。）

    :return: dict(x, y, w, h, score, method)
             score 是 ddddocr 的 confidence，不是同一量纲，别跟 chamfer 比
    """
    import ddddocr

    front = _decode(front)
    # 主路只要矩形，不要形状 —— 走 piece_box，不必先算一个掩码再读它的 .shape
    y0, y1, x0, x1 = piece_box(front, alpha_threshold)
    crop = front[y0:y1 + 1, x0:x1 + 1]
    ph, pw = y1 - y0 + 1, x1 - x0 + 1

    if ocr is None:
        ocr = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
    r = ocr.slide_match(_to_bytes(crop), _to_bytes(background),
                        simple_target=False)
    cx, cy = int(r['target'][0]), int(r['target'][1])
    return {
        'x': cx - pw // 2, 'y': cy - ph // 2, 'w': int(pw), 'h': int(ph),
        'score': float(r.get('confidence', float('nan'))), 'method': 'ddddocr',
    }


def _band_range(piece_y, y_search, height):
    lo = max(0, piece_y - y_search)
    return lo, min(height, piece_y + y_search + 1)


def _pick(score, lo, hi, bigger_is_better):
    """在窄带 [lo, hi) 里找最优点，返回 (x, y, 该点的值)。

    先对整条带**按列聚合再找 x**，不能直接对二维图取全局极值 —— 真位置在
    带里每一行都优，噪声往往只在一行上偶然冒个头，直接找会被那一行拐跑
    （参考样本就被拽到 x=159 y=18，正确答案 x=201 y=23，两者只差 0.002）。
    """
    band = score[lo:hi]
    prof = band.mean(axis=0)
    x = int(np.argmax(prof) if bigger_is_better else np.argmin(prof))
    col = band[:, x]
    y = int((np.argmax(col) if bigger_is_better else np.argmin(col)) + lo)
    return x, y, float(score[y, x])


def locate_gap(front, background, y_search=6, alpha_threshold=128,
               method='auto', ocr=None):
    """
    :param front:      易盾 front 图（带 alpha 的 PNG），bytes 或 ndarray
    :param background: 易盾 bg 图，bytes 或 ndarray
    :param y_search:   自研兜底时在竖条偏移上下各搜多少像素（ddddocr 不用）
    :param alpha_threshold: 提取拼图块用的 alpha 阈值
    :param method:     'auto'    = 能用 ddddocr 就用（默认），否则自研
                       'ddddocr' = 只用 ddddocr，没有就抛
                       'ncc'     = 只用拼图块像素的掩码互相关（自研）
                       'chamfer' = 只用轮廓对背景边缘距离场（自研）
    :param ocr:        复用外部建好的 DdddOcr 实例，省去重复初始化
    :return: dict(x, y, w, h, score, method)
             (x, y) 是缺口左上角在背景图中的坐标，w/h 是拼图块尺寸。
             score 的含义跟着 method 走，别跨方法比。

    自研那两条线索（ncc / chamfer）是给没有 ddddocr 的环境兜底的，实测
    27 组里 NCC 中 17、chamfer 中 16，明显不如 ddddocr 裁块，别当主力。

    它们各自的道理：
      NCC 靠"缺口里还留着原图内容（易盾只是整体压暗了一层）"，峰值高的
      时候非常准；缺口要是被画成别的样子（不保留原图内容）它就失效，但
      这时峰值通常也上不去，正好用 NCC_MIN_PEAK 卡掉。
      chamfer 靠"轮廓贴着背景的边"，不依赖缺口内容，但纹理密集处容易误判。

    两条线索都只在 y0 上下一条窄带里搜 x（拼图块只做水平平移），而且要先
    对整条带**按列聚合再找 x**，不能直接对二维图取全局极值 —— 真位置在带
    里每一行都优，噪声往往只在一行上偶然冒个头，直接找会被那一行拐跑。
    """
    if method in ('auto', 'ddddocr'):
        try:
            return ddddocr_locate(front, background, alpha_threshold, ocr)
        except ImportError:
            if method == 'ddddocr':
                raise

    front = _decode(front)
    background = _to_bgr(_decode(background))
    mask, piece_bgr, piece_y, _ = cut_piece_rgb(front, alpha_threshold)
    ph, pw = mask.shape
    lo, hi = _band_range(piece_y, y_search, background.shape[0])

    ncc_pick = None
    if method in ('auto', 'ncc'):
        ncc_pick = _pick(ncc_map(mask, piece_bgr, background), lo, hi, True)
    if method == 'ncc':
        x, y, score = ncc_pick
    elif method == 'chamfer':
        x, y, score = _pick(chamfer_map(mask, background, norm=True),
                            lo, hi, False)
    elif method == 'auto':
        if ncc_pick[2] >= NCC_MIN_PEAK:
            x, y, score = ncc_pick
            method = 'auto/ncc'
        else:
            x, y, score = _pick(chamfer_map(mask, background, norm=True),
                                lo, hi, False)
            method = 'auto/chamfer'
    else:
        raise ValueError('未知 method: %r' % (method,))

    return {
        'x': x, 'y': y, 'w': int(pw), 'h': int(ph),
        'score': score, 'method': method,
    }

def pd_move():
    with open('bg.jpeg', 'rb') as f:
            bg_bytes = f.read()
    with open('font.png', 'rb') as f:
            front_bytes = f.read()
    
    r = locate_gap(front_bytes, bg_bytes)

    im = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    r['bg_w'], r['bg_h'] = im.shape[1], im.shape[0]   # 真实尺寸，给浏览器里的显示宽度做换算
    print('背景图 %dx%d（真实）' % (r['bg_w'], r['bg_h']))
    print('缺口: x=%d y=%d w=%d h=%d  method=%s  score=%.3f'
            % (r['x'], r['y'], r['w'], r['h'], r['method'], r['score']))

    cv2.rectangle(im, (r['x'], r['y']),
                      (r['x'] + r['w'], r['y'] + r['h']), (0, 0, 255), 2)
    # cv2.imwrite 在中文路径上会静默失败，走 imencode + tofile
    cv2.imencode('.jpg', im)[1].tofile('result.jpg')
    return r
    
