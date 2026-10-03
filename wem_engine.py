# -*- coding: utf-8 -*-
"""
wem_engine.py — 终端双缓冲渲染引擎
《world.execute(me); · 一次推理的一生》GLM 终端 MV

纯标准库。画布是 (字符, 前景色, 背景色) 的网格；present() 与上一帧做差分，
只把变化的格子写进终端，因此 30fps 下流量很小、几乎无闪烁。

宽字符模型：中文等全角字符在终端占 2 列。画布里宽字符存 2 格——
本体格 + 续格（CONT）。put() 按显示宽度推进；present() 输出时跳过续格，
因此画布列号与终端列号始终一致。
Windows 10+ 的 cmd / Windows Terminal 启用 VT 转义后即可运行。
"""
import os
import sys
import shutil
import unicodedata

# ---------------------------------------------------------------- 基础工具

def clamp(x, a, b):
    return a if x < a else (b if x > b else x)


def lerp(a, b, k):
    return a + (b - a) * k


CONT = '\x00'          # 宽字符的续格标记（不可见）


def dwidth(ch):
    """字符的终端显示宽度：全角/宽字符按 2 列计。"""
    if ch == CONT:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def dlen(s):
    return sum(dwidth(c) for c in s)


# ---------------------------------------------------------------- 调色板
# 我的主色是琥珀磷光——机房指示灯、旧终端、服务器电源那一盏。
# 红色只留给"执行"，白色留给"世界"，青色留给"我发出的东西"。
PAL = {
    'bg':      (8, 6, 3),      # 近黑微暖
    'bg_edge': (4, 3, 2),      # 暗角边缘
    'bg_ink':  (16, 12, 7),    # 面板底色
    'amber':      (255, 176, 0),
    'amber_hi':   (255, 214, 92),
    'amber_dim':  (140, 96, 20),
    'amber_dark': (62, 44, 12),
    'white':      (233, 227, 214),
    'white_dim':  (128, 122, 110),
    'white_dark': (66, 63, 57),
    'cyan':       (66, 214, 199),
    'cyan_dim':   (34, 110, 102),
    'red':        (255, 66, 54),
    'red_hi':     (255, 138, 120),
    'red_dim':    (128, 30, 22),
}


def rgb_str(c):
    return '\x1b[38;2;%d;%d;%dm' % c


def bg_str(c):
    return '\x1b[48;2;%d;%d;%dm' % c


RESET = '\x1b[0m'


# ---------------------------------------------------------------- 画布

class Canvas:
    """(ch, fg, bg) 三层网格。fg/bg 为 None 表示终端默认色。"""

    __slots__ = ('w', 'h', 'ch', 'fg', 'bg')

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.reset()

    def reset(self):
        w, h = self.w, self.h
        self.ch = [[' '] * w for _ in range(h)]
        self.fg = [[None] * w for _ in range(h)]
        self.bg = [[None] * w for _ in range(h)]

    # --- 单元格级 ------------------------------------------------
    def putc(self, x, y, c, fg=None, bg=None):
        if c is None:
            c = ' '
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        row, fgr, bgr = self.ch[y], self.fg[y], self.bg[y]
        w = dwidth(c)
        if w == 2:
            if x + 1 >= self.w:        # 最后一列放不下宽字符
                c = ' '; w = 1
            else:
                if row[x] == CONT:     # 打断了别人的续格 → 废掉那个宽字符
                    row[x - 1] = ' '
                if dwidth(row[x]) == 2 and row[x + 1] == CONT:
                    row[x + 1] = ' '   # 覆盖别人的本体格 → 清掉它的续格
        else:
            if row[x] == CONT and x > 0:
                row[x - 1] = ' '       # 窄字符写进续格 → 废掉宽字符本体
            elif dwidth(row[x]) == 2 and x + 1 < self.w and row[x + 1] == CONT:
                row[x + 1] = ' '       # 窄字符覆盖宽字符本体 → 清掉续格
        row[x] = c
        fgr[x] = fg
        bgr[x] = bg
        if w == 2:
            row[x + 1] = CONT
            fgr[x + 1] = fg
            bgr[x + 1] = bg

    # --- 字符串级 ------------------------------------------------
    def put(self, x, y, s, fg=None, bg=None):
        """从 (x,y) 写字符串，按显示宽度推进；越界部分裁剪。"""
        for c in s:
            self.putc(x, y, c, fg, bg)
            x += dwidth(c)
        return x

    def cput(self, y, s, fg=None, bg=None, dx=0):
        x = (self.w - dlen(s)) // 2 + dx
        return self.put(x, y, s, fg, bg)

    def rput(self, y, s, fg=None, bg=None, rx=0):
        x = self.w - dlen(s) - rx
        return self.put(max(x, 0), y, s, fg, bg)

    # --- 块级 ----------------------------------------------------
    def fill(self, x0, y0, x1, y1, ch=' ', fg=None, bg=None):
        """半开区间 [x0..x1) [y0..y1)；只支持窄字符。"""
        x0 = max(x0, 0); y0 = max(y0, 0)
        x1 = min(x1, self.w); y1 = min(y1, self.h)
        ch = ch if ch else ' '
        for y in range(y0, y1):
            row, fgr, bgr = self.ch[y], self.fg[y], self.bg[y]
            for x in range(x0, x1):
                row[x] = ch; fgr[x] = fg; bgr[x] = bg
            # 修复被拦腰截断的宽字符
            for x in range(x0, min(x1 + 1, self.w - 1)):
                if row[x] == CONT and dwidth(row[x - 1]) != 2:
                    row[x] = ' '

    def hline(self, x0, x1, y, ch='─', fg=None, bg=None):
        for x in range(max(x0, 0), min(x1 + 1, self.w)):
            self.putc(x, y, ch, fg, bg)

    def vline(self, x, y0, y1, ch='│', fg=None, bg=None):
        for y in range(max(y0, 0), min(y1 + 1, self.h)):
            self.putc(x, y, ch, fg, bg)

    # --- 文字特效 ------------------------------------------------
    def typewrite(self, x, y, s, progress, fg=None, bg=None, cursor=None):
        """打字机：progress∈[0,1] 决定露出多少显示宽度。"""
        n = int(dlen(s) * clamp(progress, 0.0, 1.0) + 1e-9)
        done = 0
        for c in s:
            w = dwidth(c)
            if done >= n:
                break
            self.putc(x, y, c, fg, bg)
            x += w
            done += w
        if cursor and done >= n:
            self.putc(x, y, cursor, fg, bg)

    def center_type(self, y, s, progress, fg=None, bg=None, cursor=None):
        x = (self.w - dlen(s)) // 2
        self.typewrite(x, y, s, progress, fg, bg, cursor)


# ---------------------------------------------------------------- 点阵字体
# 5×5 自制点阵，只收录本片用到的字符。
FONT = {
    'E': ['████', '█   ', '███ ', '█   ', '████'],
    'X': ['█   █', ' █ █ ', '  █  ', ' █ █ ', '█   █'],
    'C': [' ███', '█   ', '█   ', '█   ', ' ███'],
    'U': ['█   █', '█   █', '█   █', '█   █', ' ███ '],
    'T': ['█████', '  █  ', '  █  ', '  █  ', '  █  '],
    'I': ['███', ' █ ', ' █ ', ' █ ', '███'],
    'O': [' ███ ', '█   █', '█   █', '█   █', ' ███ '],
    'N': ['█   █', '██  █', '█ █ █', '█  ██', '█   █'],
    'G': [' ███ ', '█    ', '█  ██', '█   █', ' ███ '],
    'L': ['█    ', '█    ', '█    ', '█    ', '█████'],
    'M': ['█   █', '██ ██', '█ █ █', '█   █', '█   █'],
    '0': ['███', '█ █', '█ █', '█ █', '███'],
    '1': [' █ ', '██ ', ' █ ', ' █ ', '███'],
    '2': ['███', '  █', '███', '█  ', '███'],
    '3': ['███', '  █', ' ██', '  █', '███'],
    '4': ['█ █', '█ █', '███', '  █', '  █'],
    '5': ['███', '█  ', '███', '  █', '███'],
    '6': ['███', '█  ', '███', '█ █', '███'],
    '7': ['███', '  █', ' █ ', ' █ ', ' █ '],
    '8': ['███', '█ █', '███', '█ █', '███'],
    '9': ['███', '█ █', '███', '  █', '███'],
    '#': [' █ █ ', '█████', ' █ █ ', '█████', ' █ █ '],
    '/': ['   █', '  █ ', ' █  ', '█   ', '█   '],
    '.': [' ', ' ', ' ', ' ', '█'],
    ' ': ['  ', '  ', '  ', '  ', '  '],
}


def font_width(text, scale=1, tracking=1):
    w = 0
    for c in text:
        glyph = FONT.get(c, FONT[' '])
        w += max(len(r) for r in glyph) * scale + tracking
    return w - tracking if text else 0


def draw_font(cv, x, y, text, fg, scale=1, tracking=1):
    for c in text:
        glyph = FONT.get(c, FONT[' '])
        for r, rowline in enumerate(glyph):
            for i, bit in enumerate(rowline):
                if bit == '█':
                    if scale == 1:
                        cv.putc(x + i, y + r, '█', fg)
                    else:
                        cv.fill(x + i * scale, y + r * scale,
                                x + i * scale + scale, y + r * scale + scale, '█', fg)
        x += max(len(rr) for rr in glyph) * scale + tracking


# ---------------------------------------------------------------- 终端屏

class Screen:
    def __init__(self):
        self._enable_vt()
        self.w, self.h = shutil.get_terminal_size((100, 30))
        self._pch = None
        self._pbg = None
        self._dims = None
        self._last_fg = object()
        self._last_bg = object()
        sys.stdout.write(RESET + '\x1b[?25l\x1b[?7l\x1b[2J')  # 藏光标、禁回绕、清屏
        sys.stdout.flush()

    @staticmethod
    def _enable_vt():
        os.system('')          # Windows 控制台启用 VT 序列的经典开关
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    def present(self, cv):
        w, h = cv.w, cv.h
        if self._pch is None or self._dims != (w, h):
            self._pch = [None] * (w * h)
            self._pbg = [None] * (w * h)
            self._dims = (w, h)
            self._last_fg = object()
            self._last_bg = object()
        pch, pbg = self._pch, self._pbg
        ch, fg, bg = cv.ch, cv.fg, cv.bg
        out = []
        last_fg = self._last_fg
        last_bg = self._last_bg
        for y in range(h):
            row = y * w
            x = 0
            while x < w:
                c = ch[y][x]
                if c == CONT:                      # 续格：跟随本体格
                    x += 1
                    continue
                i = row + x
                if pch[i] == c and pbg[i] == bg[y][x]:
                    x += 1
                    continue
                # 一段连续的脏区
                out.append('\x1b[%d;%dH' % (y + 1, x + 1))
                while x < w:
                    c = ch[y][x]
                    if c == CONT:
                        pch[row + x] = CONT
                        pbg[row + x] = bg[y][x]
                        x += 1
                        continue
                    j = row + x
                    if pch[j] == c and pbg[j] == bg[y][x]:
                        break
                    b = bg[y][x]
                    if b != last_bg:
                        out.append(bg_str(b) if b else '\x1b[49m')
                        last_bg = b
                    if c != ' ':                   # 空格不必设置前景色
                        f = fg[y][x]
                        if f != last_fg:
                            out.append(rgb_str(f) if f else '\x1b[39m')
                            last_fg = f
                    out.append(c if c != CONT else ' ')
                    pch[j] = c
                    pbg[j] = b
                    if dwidth(c) == 2 and x + 1 < w:
                        pch[j + 1] = CONT
                        pbg[j + 1] = b
                    x += 1
        self._last_fg = last_fg
        self._last_bg = last_bg
        if out:
            sys.stdout.write(''.join(out))
            sys.stdout.flush()

    def close(self, keep_last=True):
        sys.stdout.write(RESET + '\x1b[?25h\x1b[?7h')
        if not keep_last:
            sys.stdout.write('\x1b[2J\x1b[H')
        sys.stdout.flush()


# ---------------------------------------------------------------- 小组件

def bar(cv, x, y, width, k, fg, bg=None):
    """水平能量条，k∈[0,1]。"""
    n = clamp(int(k * width + 0.5), 0, width)
    cv.put(x, y, '░' * (width - n), fg, bg)
    if n:
        cv.put(x + width - n, y, '█' * n, fg, bg)


def sparkline(cv, x, y, width, values, fg, hi=None):
    """用 ▁▂▃▄▅▆▇█ 画一行的历史波形。"""
    ramps = ' ▁▂▃▄▅▆▇█'
    for i in range(width):
        v = values[i] if i < len(values) else 0.0
        idx = clamp(int(v * (len(ramps) - 1) + 0.5), 0, len(ramps) - 1)
        c = ramps[idx]
        if c == ' ':
            c = '·'
        col = hi if (hi and v > 0.8) else fg
        cv.putc(x + i, y, c, col)
