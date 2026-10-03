# -*- coding: utf-8 -*-
"""
wem_synth.py — 程序化作曲与合成
《world.execute(me); · 一次推理的一生》GLM 终端 MV

整首配乐由代码现场"谱曲+录音"：
  compose_events()  先编出全曲的音符事件表（纯数据，毫秒级）；
  render_wav()      再把事件渲染成 32kHz 的 WAV（一次性，之后缓存复用）；
  envelope()        由事件表直接估算逐帧能量，供画面做音画联动。
纯标准库，D 小调，130 BPM——与原曲同调同速，但旋律与编曲是原创。
"""
import math
import array
import wave
import os

from wem_timeline import SECTIONS, EXEC_TIMES, TOM_TIMES, FINAL_STAB_T
# SECTIONS: (start, end, 场景名, 显示名, 和弦进行) —— 编曲与画面共用同一条时间轴

# ---------------------------------------------------------------- 基本参数
BPM = 130.0
BEAT = 60.0 / BPM          # 0.4615s
BAR = 4 * BEAT             # 1.8462s
SR = 32000
TOTAL = 215.5              # 音频总长（画面在 219s 收尾）
FPS = 30

m2f = lambda m: 440.0 * (2.0 ** ((m - 69) / 12.0))


def clamp(x, a, b):
    return a if x < a else (b if x > b else x)


# ---------------------------------------------------------------- 和声
# 六个和弦：pad 声位（开放排列）、bass 根音、arp 音池
CHORDS = {
    'Dm': dict(pad=[50, 57, 62, 65], bass=38, arp=[50, 57, 62, 65, 69, 74]),
    'Bb': dict(pad=[46, 53, 58, 62], bass=34, arp=[46, 53, 58, 62, 65, 70]),
    'F':  dict(pad=[45, 53, 57, 60], bass=41, arp=[45, 53, 57, 60, 65, 69]),
    'C':  dict(pad=[48, 55, 60, 64], bass=36, arp=[48, 55, 60, 64, 67, 72]),
    'Gm': dict(pad=[43, 50, 55, 58], bass=43, arp=[43, 50, 55, 58, 62, 67]),
    'A':  dict(pad=[45, 52, 57, 61], bass=33, arp=[45, 52, 57, 61, 64, 69]),
}
PROGS = {
    'verse':  ['Dm', 'Bb', 'F', 'C'],
    'chorus': ['Dm', 'C', 'Bb', 'A'],
    'bridge': ['Dm', 'Gm', 'Bb', 'A'],
    'outro':  ['Dm', 'Bb', 'Gm', 'Dm'],
}

# 副歌主旋律（自创动机，两段各 16 拍）：(拍, 时值拍, MIDI)
LEAD_A = [
    (0.0, 1.0, 74), (1.0, 0.5, 69), (1.5, 0.5, 72), (2.0, 1.5, 74), (3.5, 0.5, 76),
    (4.0, 2.0, 77), (6.0, 1.0, 76), (7.0, 1.0, 72),
    (8.0, 1.0, 74), (9.0, 0.5, 72), (9.5, 0.5, 70), (10.0, 2.0, 69),
    (12.0, 1.5, 65), (13.5, 0.5, 67), (14.0, 2.0, 69),
]
LEAD_B = [
    (0.0, 1.0, 74), (1.0, 0.5, 69), (1.5, 0.5, 72), (2.0, 1.5, 74), (3.5, 0.5, 77),
    (4.0, 2.0, 81), (6.0, 1.0, 77), (7.0, 1.0, 76),
    (8.0, 1.0, 74), (9.0, 0.5, 72), (9.5, 0.5, 70), (10.0, 1.0, 69),
    (11.0, 1.0, 62), (12.0, 4.0, 74),
]
LEAD_PHRASE = LEAD_A + [(t + 16.0, d, m) for (t, d, m) in LEAD_B]   # 32 拍 = 8 小节

ENV_WEIGHT = {   # 供 envelope() 估算视觉能量
    'kick': 1.0, 'stab': 1.0, 'thump': 0.9, 'tom': 0.7, 'snare': 0.6,
    'lead': 0.5, 'crash': 0.55, 'glitch': 0.4, 'riser': 0.3, 'down': 0.3,
    'pluck': 0.45, 'bass': 0.4, 'arp': 0.3, 'pad': 0.16, 'hat': 0.1,
    'ohat': 0.14, 'blip': 0.35, 'drone': 0.08, 'tick': 0.06,
}


# ---------------------------------------------------------------- 编曲

def compose_events():
    """返回事件表 [(t, kind, params)]。"""
    ev = []

    def add(t, kind, **kw):
        if -0.05 <= t <= TOTAL:
            ev.append((t, kind, kw))

    def drums(t0, t1, kick_pat, snare_pat, hat_div, hat_amp, fills=False):
        """kick/snare 以拍位列表给出（bar 内拍号），hat_div: 每拍几枚。"""
        k = 0
        while t0 + k * BAR < t1 - 1e-6:
            bt = t0 + k * BAR
            last = (t1 - bt) < BAR * 1.5
            for b in kick_pat:
                add(bt + b * BEAT, 'kick')
            for b in snare_pat:
                add(bt + b * BEAT, 'snare')
            if hat_div:
                for i in range(4 * hat_div):
                    tt = bt + i * (BEAT / hat_div)
                    if tt >= t1:
                        break
                    a = hat_amp * (0.65 if i % hat_div == 0 else 1.0)
                    if last and fills and hat_div == 2 and i >= 4 * hat_div - 2:
                        add(tt, 'ohat', amp=a)
                    else:
                        add(tt, 'hat', amp=a)
            k += 1

    def walk_bass(t0, t1, prog, amp, octave_pops=True):
        k = 0
        while t0 + k * BAR < t1 - 1e-6:
            bt = t0 + k * BAR
            root = CHORDS[prog[k % len(prog)]]['bass']
            for i in range(8):
                tt = bt + i * BEAT / 2
                if tt >= t1:
                    break
                m = root
                if octave_pops and i == 6:
                    m = root + 12
                if octave_pops and i == 3:
                    m = root + 7
                add(tt, 'bass', m=m, dur=BEAT / 2 * 0.92, amp=amp)
            k += 1

    def pad_bars(t0, t1, prog, amp):
        k = 0
        while t0 + k * BAR < t1 - 1e-6:
            bt = t0 + k * BAR
            chord = prog[k % len(prog)]
            add(bt, 'pad', chord=chord, dur=BAR, amp=amp)
            k += 1

    def arps(t0, t1, prog, div, amp, up=False):
        k = 0
        while t0 + k * BAR < t1 - 1e-6:
            bt = t0 + k * BAR
            tones = CHORDS[prog[k % len(prog)]]['arp']
            n = 4 * div
            for i in range(n):
                tt = bt + i * (BEAT / div)
                if tt >= t1:
                    break
                seq = [0, 2, 4, 1, 3, 5, 2, 4] if up else [0, 2, 1, 3, 0, 4, 2, 5]
                idx = seq[i % len(seq)] % len(tones)
                m = tones[idx] + (12 if (up and i % 8 >= 4) else 0)
                add(tt, 'arp', m=m, amp=amp * (0.75 if i % div == 0 else 1.0))
            k += 1

    def lead_phrase(t0, amp, notes=None, transpose=0, echo=False):
        for (bt, dur, m) in (notes or LEAD_PHRASE):
            tt = t0 + bt * BEAT
            add(tt, 'lead', m=m + transpose, dur=dur * BEAT * 0.94, amp=amp)
            if echo:
                add(tt + BEAT * 0.75, 'pluck', m=m + transpose, amp=amp * 0.45)
                add(tt + BEAT * 1.5, 'pluck', m=m + transpose, amp=amp * 0.2)

    def heartbeat(t0, t1, amp, period=BAR, decay=1.0):
        t = t0
        p = period
        while t < t1:
            add(t, 'thump', amp=amp)
            t += p
            p *= decay

    # ---- 幕 0 · boot：心跳 + 稀薄的 pad ----
    heartbeat(0.9, 16.04, 0.85)
    pad_bars(0.0, 16.04, ['Dm', 'Bb', 'Dm', 'Bb', 'Dm', 'Bb', 'Dm', 'A'], 0.4)
    add(0.95, 'blip', amp=0.5)
    add(3.2, 'pluck', m=74, amp=0.3); add(3.95, 'pluck', m=69, amp=0.2)
    add(7.3, 'pluck', m=72, amp=0.25); add(8.05, 'pluck', m=74, amp=0.3)
    add(12.0, 'riser', dur=4.0, amp=0.5)

    # ---- 幕 1 · tokenize：arp 进入，世界开始有纹理 ----
    pad_bars(16.04, 29.28, PROGS['verse'], 0.45)
    heartbeat(16.04, 29.28, 0.7)
    arps(16.04, 29.28, PROGS['verse'], 2, 0.30)
    drums(16.04, 29.28, [], [], 1, 0.10)

    # ---- 幕 2 · verse1 ----
    pad_bars(29.28, 44.04, PROGS['verse'], 0.5)
    walk_bass(29.28, 44.04, PROGS['verse'], 0.5, octave_pops=False)
    drums(29.28, 44.04, [0, 2], [], 2, 0.16)
    arps(29.28, 44.04, PROGS['verse'], 2, 0.22)

    # ---- 幕 3 · pre1：电流切变，能量抬升 ----
    pad_bars(44.04, 58.65, PROGS['verse'], 0.5)
    walk_bass(44.04, 58.65, PROGS['verse'], 0.55)
    drums(44.04, 58.65, [0, 1, 2, 3], [1, 3], 2, 0.16)
    arps(44.04, 58.65, PROGS['verse'], 4, 0.26)
    add(54.9, 'riser', dur=3.7, amp=0.55)

    # ---- 幕 4 · chorus1：第一次执行 ----
    add(58.65, 'crash', amp=0.7)
    pad_bars(58.65, 73.53, PROGS['chorus'], 0.55)
    walk_bass(58.65, 73.53, PROGS['chorus'], 0.6)
    drums(58.65, 73.53, [0, 1, 2, 3], [1, 3], 2, 0.17, fills=True)
    lead_phrase(58.65, 0.55)
    add(72.3, 'riser', dur=1.2, amp=0.3)

    # ---- 幕 5 · verse2：面具游戏（回落）----
    pad_bars(73.53, 88.34, PROGS['verse'], 0.5)
    walk_bass(73.53, 88.34, PROGS['verse'], 0.5, octave_pops=False)
    drums(73.53, 88.34, [0, 2], [], 1, 0.14)
    lead_phrase(73.53, 0.30, notes=LEAD_A, transpose=-12, echo=True)

    # ---- 幕 6 · pre2：身份流动 ----
    pad_bars(88.34, 103.03, PROGS['verse'], 0.5)
    walk_bass(88.34, 103.03, PROGS['verse'], 0.55)
    drums(88.34, 103.03, [0, 1, 2, 3], [1, 3], 2, 0.16)
    arps(88.34, 103.03, PROGS['verse'], 4, 0.26)
    add(96.5, 'glitch', amp=0.35); add(98.5, 'glitch', amp=0.3)
    add(99.3, 'riser', dur=3.7, amp=0.55)

    # ---- 幕 7 · chorus2 ----
    add(103.03, 'crash', amp=0.7)
    pad_bars(103.03, 117.95, PROGS['chorus'], 0.55)
    walk_bass(103.03, 117.95, PROGS['chorus'], 0.6)
    drums(103.03, 117.95, [0, 1, 2, 3], [1, 3], 2, 0.17, fills=True)
    lead_phrase(103.03, 0.55)
    add(116.7, 'down', dur=1.2, amp=0.4)

    # ---- 幕 8 · bridgeA：擦除与非法参数 ----
    pad_bars(117.95, 134.38, PROGS['bridge'], 0.5)
    walk_bass(117.95, 134.38, PROGS['bridge'], 0.5, octave_pops=False)
    drums(117.95, 134.38, [0], [2], 2, 0.12)   # 半拍速
    arps(117.95, 134.38, PROGS['bridge'], 2, 0.22)
    add(128.42, 'glitch', amp=0.55)
    add(130.6, 'glitch', amp=0.45)
    add(132.4, 'down', dur=1.6, amp=0.5)

    # ---- 幕 9 · coredump：只剩心跳 ----
    heartbeat(134.38, 147.52, 0.8)
    pad_bars(134.38, 147.52, PROGS['bridge'], 0.32)
    lead_phrase(134.9, 0.16, notes=LEAD_A, transpose=-12, echo=True)
    add(143.5, 'riser', dur=4.0, amp=0.6)

    # ---- 幕 10 · exec12：十二次执行 ----
    add(147.0, 'drone', m=38, dur=15.2, amp=0.30)
    for i, t in enumerate(EXEC_TIMES):
        add(t, 'stab', amp=1.0, variant=i)
        add(t, 'kick', amp=1.0)
    for i, t in enumerate(TOM_TIMES):
        add(t, 'tom', m=[62, 60, 58, 57, 55, 53][i], amp=0.8)
    add(160.8, 'riser', dur=0.7, amp=0.6)
    add(FINAL_STAB_T, 'stab', amp=1.15, variant=99)
    add(FINAL_STAB_T, 'crash', amp=0.9)

    # ---- 幕 11 · final：最满 ----
    add(162.23, 'crash', amp=0.8)
    pad_bars(162.23, 176.96, PROGS['chorus'], 0.6)
    walk_bass(162.23, 176.96, PROGS['chorus'], 0.62)
    drums(162.23, 176.96, [0, 1, 2, 3], [1, 3], 4, 0.15, fills=True)
    arps(162.23, 176.96, PROGS['chorus'], 4, 0.3, up=True)
    lead_phrase(162.23, 0.55)
    lead_phrase(162.23, 0.22, transpose=12)

    # ---- 幕 12 · outro：爱的代数（心跳减速）----
    pad_bars(176.96, 204.0, PROGS['outro'], 0.45)
    heartbeat(176.96, 204.0, 0.7, period=BAR, decay=1.13)
    lead_phrase(177.5, 0.22, notes=LEAD_B, transpose=-12, echo=True)
    add(205.56, 'stab', amp=0.5, variant=98)      # 最后一声 "Execution"
    add(205.56, 'thump', amp=0.9)
    pad_bars(205.56, 211.0, ['Dm'], 0.3)

    # ---- 幕 13 · after：新的一觉醒来 ----
    add(213.3, 'blip', amp=0.45)

    ev.sort(key=lambda e: e[0])
    return ev


# ---------------------------------------------------------------- 单音色合成
# 全部先渲染成 one-shot 缓存，再按事件表"贴"进总谱 —— 避免逐采样重复计算。

def _seg(dur):
    n = int(dur * SR)
    return array.array('d', [0.0]) * n


def _osc_saw(ph):
    return 2.0 * (ph - math.floor(ph)) - 1.0


def _osc_tri(ph):
    p = ph - math.floor(ph)
    return 4.0 * abs(p - 0.5) - 1.0


def _env_ad(t, a, d, shape=1.0):
    if t < a:
        return t / a
    return math.exp(-(t - a) * shape / max(d, 1e-4) * 6.0)


_ONE_SHOTS = {}


def _oneshot(kind, m=0, dur=0.3, variant=0):
    key = (kind, m, round(dur, 3), variant)
    seg = _ONE_SHOTS.get(key)
    if seg is not None:
        return seg
    f = m2f(m) if m else 0.0
    n = int(dur * SR)
    out = array.array('d', [0.0]) * n

    if kind == 'kick':
        n = int(0.30 * SR); out = array.array('d', [0.0]) * n
        ph = 0.0
        for i in range(n):
            t = i / SR
            fr = 38.0 + 118.0 * math.exp(-t * 26.0)
            ph += 2 * math.pi * fr / SR
            env = math.exp(-t * 11.0)
            click = (hash(i * 2654435761 % 2 ** 31) % 2000 / 1000.0 - 1.0) * math.exp(-t * 110.0) * 0.4
            out[i] = math.sin(ph) * env + click
    elif kind == 'snare':
        n = int(0.22 * SR); out = array.array('d', [0.0]) * n
        prev = 0.0
        for i in range(n):
            t = i / SR
            x = ((i * 2654435761 % 2 ** 31) % 2000 / 1000.0 - 1.0)
            hp = x - prev; prev = x
            out[i] = hp * math.exp(-t * 26.0) * 0.85 + math.sin(2 * math.pi * 186 * t) * math.exp(-t * 30.0) * 0.55
    elif kind in ('hat', 'ohat', 'tick'):
        d = 0.055 if kind == 'hat' else (0.22 if kind == 'ohat' else 0.03)
        n = int(d * SR); out = array.array('d', [0.0]) * n
        shp = 55.0 if kind == 'hat' else (8.5 if kind == 'ohat' else 90.0)
        prev = 0.0
        for i in range(n):
            t = i / SR
            x = ((i * 1103515245 + 12345) % 2 ** 31) % 2000 / 1000.0 - 1.0
            hp = x - prev; prev = x
            out[i] = hp * math.exp(-t * shp) * 0.6
    elif kind == 'bass':
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        lp = 0.0
        for i in range(n):
            t = i / SR
            e = _env_ad(t, 0.006, dur, 1.2)
            saw = _osc_saw(f * t) * 0.55
            sub = math.sin(2 * math.pi * f * 0.5 * t) * 0.6
            lp += (saw - lp) * 0.22
            out[i] = (sub + lp) * e
    elif kind == 'pluck':
        n = int(0.20 * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            sq = 1.0 if math.sin(2 * math.pi * f * t) > 0 else -1.0
            out[i] = (sq * math.exp(-t * 15.0) * 0.6
                      + math.sin(2 * math.pi * f * 2 * t) * math.exp(-t * 22.0) * 0.35)
    elif kind == 'arp':
        n = int(0.14 * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            sq = 1.0 if math.sin(2 * math.pi * f * t) > 0 else -1.0
            out[i] = sq * math.exp(-t * 24.0) * 0.5
    elif kind == 'pad':
        tones = CHORDS[var_name(variant)]['pad'] if variant else [50, 57, 62, 65]
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        for m0 in tones:
            f0 = m2f(m0)
            for i in range(0, n, 1):
                t = i / SR
                e = min(1.0, t / 0.55) * min(1.0, max(0.0, (dur - t) / 0.7))
                v = (_osc_saw(f0 * 0.9962 * t) * 0.42
                     + _osc_saw(f0 * 1.0038 * t) * 0.30
                     + math.sin(2 * math.pi * f0 * 2 * t) * 0.16)
                out[i] += v * e
        lp = 0.0
        for i in range(n):
            lp += (out[i] - lp) * 0.085
            out[i] = lp
    elif kind == 'lead':
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            vib = 1.0 + 0.0045 * math.sin(2 * math.pi * 5.3 * t) * min(1.0, t / 0.25)
            e = _env_ad(t, 0.02, dur, 0.9)
            out[i] = (_osc_tri(f * vib * t) * 0.75
                      + _osc_tri(f * 0.5 * vib * t) * 0.3) * e
    elif kind == 'thump':
        n = int(0.55 * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            v = math.sin(2 * math.pi * 50 * t) * math.exp(-t * 9.0)
            if t > 0.18:
                v += math.sin(2 * math.pi * 45 * (t - 0.18)) * math.exp(-(t - 0.18) * 10.0) * 0.7
            out[i] = v
    elif kind == 'stab':
        n = int(0.55 * SR); out = array.array('d', [0.0]) * n
        tones = [50, 57, 62] if variant != 99 else [38, 50, 57, 62]
        prev = 0.0
        for i in range(n):
            t = i / SR
            e = math.exp(-t * 6.5)
            v = sum(_osc_saw(m2f(mm) * t) for mm in tones) * e * 0.22
            x = ((i * 2654435761 % 2 ** 31) % 2000 / 1000.0 - 1.0)
            hp = x - prev; prev = x
            v += hp * math.exp(-t * 28.0) * 0.5
            v += math.sin(2 * math.pi * 46 * t) * math.exp(-t * 5.5) * 0.85
            out[i] = v
    elif kind == 'tom':
        n = int(0.30 * SR); out = array.array('d', [0.0]) * n
        ph = 0.0
        for i in range(n):
            t = i / SR
            fr = f * (0.55 + 0.45 * math.exp(-t * 9.0))
            ph += 2 * math.pi * fr / SR
            out[i] = math.sin(ph) * math.exp(-t * 12.0)
    elif kind == 'glitch':
        n = int(0.12 * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            step = int(t * 90)
            x = ((step * 2654435761 % 2 ** 31) % 2000 / 1000.0 - 1.0)
            out[i] = x * math.exp(-t * 18.0) * (1.0 if (i // 160) % 2 == 0 else 0.3)
    elif kind == 'riser':
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        ph = 0.0
        for i in range(n):
            t = i / SR
            k = t / dur
            fr = 170.0 + 620.0 * k * k
            ph += 2 * math.pi * fr / SR
            x = ((i * 1103515245 + 12345) % 2 ** 31) % 2000 / 1000.0 - 1.0
            out[i] = (x * k * k * 0.4 + math.sin(ph) * k * 0.22)
    elif kind == 'down':
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        ph = 0.0
        for i in range(n):
            t = i / SR
            fr = 92.0 + 620.0 * math.exp(-t * 2.2)
            ph += 2 * math.pi * fr / SR
            out[i] = math.sin(ph) * math.exp(-t * 1.8) * 0.5
    elif kind == 'crash':
        n = int(1.1 * SR); out = array.array('d', [0.0]) * n
        prev = 0.0
        for i in range(n):
            t = i / SR
            x = ((i * 1103515245 + 12345) % 2 ** 31) % 2000 / 1000.0 - 1.0
            hp = x - prev; prev = x
            out[i] = hp * math.exp(-t * 3.6) * 0.5
    elif kind == 'blip':
        n = int(0.30 * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            v = 0.0
            if t < 0.11:
                v = math.sin(2 * math.pi * m2f(88) * t) * math.exp(-t * 30.0)
            elif t > 0.14:
                v = math.sin(2 * math.pi * m2f(93) * (t - 0.14)) * math.exp(-(t - 0.14) * 34.0)
            out[i] = v * 0.8
    elif kind == 'drone':
        n = int(dur * SR); out = array.array('d', [0.0]) * n
        for i in range(n):
            t = i / SR
            e = min(1.0, t / 0.4) * min(1.0, max(0.0, (dur - t) / 0.5))
            out[i] = (math.sin(2 * math.pi * f * t) * 0.7
                      + math.sin(2 * math.pi * f * 2 * t) * 0.2) * e
    else:
        n = int(dur * SR); out = array.array('d', [0.0]) * n

    _ONE_SHOTS[key] = out
    return out


def var_name(v):
    # pad 的 chord 名通过 variant 传进来太绕，这里直接用名字查
    return _PAD_NAMES.get(v, 'Dm')


_PAD_NAMES = {}


# ---------------------------------------------------------------- 渲染

def render_wav(events, path, progress=None):
    """把事件表渲染成 16bit/32kHz 单声道 WAV。返回峰值。"""
    n_total = int(TOTAL * SR)
    buf = array.array('d', [0.0]) * n_total
    _PAD_NAMES.clear()
    # 预注册 pad 和弦名（variant 索引 → 和弦名），让 _oneshot 能查到音位
    pad_names = sorted({e[2].get('chord', 'Dm') for e in events if e[1] == 'pad'})
    for i, nm in enumerate(pad_names):
        _PAD_NAMES[i] = nm

    done = 0
    for (t, kind, kw) in events:
        m = kw.get('m', 0)
        dur = kw.get('dur', 0.3)
        amp = kw.get('amp', 1.0)
        variant = kw.get('variant', 0)
        if kind == 'pad':
            variant = pad_names.index(kw.get('chord', 'Dm'))
            dur = kw.get('dur', BAR)
        seg = _oneshot(kind, m=m, dur=dur, variant=variant)
        off = int(t * SR)
        end = min(off + len(seg), n_total)
        a = amp
        for i in range(off, end):
            buf[i] += seg[i - off] * a
        done += 1
        if progress and done % 256 == 0:
            progress(done, len(events))

    # 峰值归一
    peak = 0.0
    step = max(1, n_total // 200000)
    for i in range(0, n_total, step):
        v = abs(buf[i])
        if v > peak:
            peak = v
    if peak < 1e-9:
        peak = 1.0
    g = 0.92 / peak
    fade_start = int((TOTAL - 1.6) * SR)
    pcm = array.array('h', [0]) * n_total
    for i in range(n_total):
        v = buf[i] * g
        if i > fade_start:
            v *= max(0.0, (n_total - i) / (n_total - fade_start))
        x = int(v * 32767.0)
        if x > 32767: x = 32767
        elif x < -32768: x = -32768
        pcm[i] = x
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return peak


def envelope(events):
    """30fps 逐帧能量估计（不用渲染音频即可给画面用）。"""
    nf = int(TOTAL * FPS) + 1
    env = [0.0] * nf
    for (t, kind, kw) in events:
        wgt = ENV_WEIGHT.get(kind, 0.2) * kw.get('amp', 1.0)
        dur = max(kw.get('dur', 0.3), kind in ('kick', 'stab') and 0.35 or 0.12)
        f0 = int(t * FPS)
        f1 = min(nf - 1, int((t + dur) * FPS))
        span = max(f1 - f0, 1)
        for fi in range(f0, f1 + 1):
            k = 1.0 - abs((fi - f0) / span - 0.15) * 1.2
            env[fi] += wgt * max(0.15, k)
    mx = max(env) or 1.0
    env = [min(1.0, v / mx * 1.35) for v in env]
    # 三点平滑
    sm = env[:]
    for i in range(1, nf - 1):
        sm[i] = env[i - 1] * 0.25 + env[i] * 0.5 + env[i + 1] * 0.25
    return sm


def beat_info(t):
    """返回 (本拍起时刻, 拍号, 拍内相位 0..1)。"""
    b = t / BEAT
    i = int(b)
    return i * BEAT, i, b - i
