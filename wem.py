# -*- coding: utf-8 -*-
"""
wem.py — 主程序
《world.execute(me); · 一次推理的一生》GLM 终端 MV（原曲版）

用法：
    python wem.py                     播放：自动在 input/ 里找你的原曲音频
    python wem.py --song "路径.mp3"   直接指定音频文件
    python wem.py --synth             没有原曲时，用程序自带的合成配乐播放
    python wem.py --no-audio          无声播放
    python wem.py --selftest          自检：多种窗口尺寸下快进渲染全片
    python wem.py --shot 148.1 [--size 160x52]   渲染某一帧的文本快照
    python wem.py --fps 60            提高帧率（默认 30）

按键：
    空格        暂停 / 继续
    ← / →      后退 / 前进 5 秒
    Q / Esc / Ctrl+C   退出
    M           中止音频（画面用墙钟继续）
"""
import sys
import time
import os
import shutil
import threading

from wem_engine import Canvas, Screen, PAL, clamp, dlen, sparkline
from wem_timeline import LYRICS, lyric_index_at, section_local, CREDITS
import wem_audio
import wem_scenes as scenes

MIN_W, MIN_H = 100, 26
DEFAULT_FPS = 30


# ---------------------------------------------------------------- 上下文

class Ctx:
    __slots__ = ('t', 'lt', 'dur', 'name', 'label', 'env', 'beat_phase',
                 'beat_idx', 'flash', 'paused')

    def __init__(self, t, env_list, paused=False):
        self.t = t
        # section_local 返回 (本地时间, 段长, 场景名, 显示名, 和弦进行)
        self.lt, self.dur, self.name, self.label, _ = section_local(t)
        i = int(t * wem_audio.FPS)
        self.env = env_list[i] if 0 <= i < len(env_list) else 0.0
        b = t / wem_audio.BEAT
        self.beat_idx = int(b)
        self.beat_phase = b - self.beat_idx
        self.flash = 0.0
        self.paused = paused


# ---------------------------------------------------------------- Chrome

def fmt_time(t):
    m = int(t // 60)
    s = t - m * 60
    return '%02d:%04.1f' % (m, s)


def chrome(cv, st, env_hist, total):
    t = st.t
    # 行 0：标题栏
    cv.put(2, 0, 'glm@zhipu:~/inference', PAL['amber_dim'])
    cv.cput(0, '《world.execute(me); · 一次推理的一生》', PAL['amber'])
    cv.rput(0, '%s / %s' % (fmt_time(t), fmt_time(total)), PAL['white_dim'], rx=2)
    # 行 1：分隔条 + 幕名 + 能量历史
    for x in range(0, cv.w):
        cv.putc(x, 1, '─', PAL['amber_dark'])
    cv.put(2, 1, '▍' + st.label, PAL['amber_dim'])
    if cv.w >= 130:
        sparkline(cv, cv.w - 29, 1, 18, env_hist, PAL['amber_dark'], hi=PAL['amber'])
        cv.put(cv.w - 10, 1, '130bpm Dm', PAL['amber_dark'])
    # 行 cv.h-5：分隔条
    for x in range(0, cv.w):
        cv.putc(x, cv.h - 5, '─', PAL['amber_dark'])
    # 歌词条
    idx = lyric_index_at(t)
    if idx >= 0:
        lt0, en, zh, kind = LYRICS[idx]
        if kind == 'lyric':
            cv.center_type(cv.h - 4, en, clamp((t - lt0) / 0.38, 0, 1), PAL['amber_hi'])
            if zh:
                cv.center_type(cv.h - 3, zh, clamp((t - lt0 - 0.45) / 0.55, 0, 1),
                               PAL['white_dim'])
        else:
            cv.center_type(cv.h - 4, zh, clamp((t - lt0) / 0.5, 0, 1), PAL['white_dim'])
    # 进度条
    k = clamp(t / total, 0, 1)
    pos = int(k * (cv.w - 5)) + 2
    for x in range(2, cv.w - 3):
        cv.putc(x, cv.h - 2, '█' if x <= pos else '░',
                PAL['amber_dim'] if x <= pos else PAL['bg_ink'])
    cv.putc(pos, cv.h - 2, '◆', PAL['amber'])
    # 底栏
    cv.put(2, cv.h - 1, '[空格] 暂停  [←/→] ±5s  [Q] 退出  [M] 停音频', PAL['white_dark'])
    if cv.w >= 150:
        cv.cput(cv.h - 1, 'GLM · 智谱清言', PAL['amber_dark'])
        cv.rput(cv.h - 1, '30fps · VT + truecolor', PAL['white_dark'], rx=2)
    if st.paused:
        cv.rput(0, '‖ PAUSED', PAL['cyan'], rx=14)


def render_frame(cv, t, env_list, env_hist, total, paused=False):
    cv.fill(0, 0, cv.w, cv.h, ' ', None, PAL['bg'])
    st = Ctx(t, env_list, paused)
    chrome(cv, st, env_hist, total)
    fn = scenes.SCENES.get(st.name)
    if fn:
        fn(cv, st)
    return st


def too_small_frame(cv, w, h):
    cv.fill(0, 0, cv.w, cv.h, ' ', None, PAL['bg'])
    cv.cput(cv.h // 2 - 1, '窗口太小了', PAL['amber'])
    cv.cput(cv.h // 2 + 1, '请拉大到至少 %d×%d （当前 %d×%d）' % (MIN_W, MIN_H, w, h),
            PAL['white_dim'])


# ---------------------------------------------------------------- 键盘

class Keys:
    def __init__(self):
        self.events = []
        self._lock = threading.Lock()
        if os.name == 'nt':
            self._t = threading.Thread(target=self._loop, daemon=True)
            self._t.start()

    def _loop(self):
        import msvcrt
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ('q', 'Q', '\x1b', '\x03'):
                    self.push(('quit',))
                elif ch == ' ':
                    self.push(('pause',))
                elif ch in ('m', 'M'):
                    self.push(('stop',))
                elif ch in ('\xe0', '\x00'):
                    code = msvcrt.getwch()
                    if code == 'K':
                        self.push(('seek', -5.0))
                    elif code == 'M':
                        self.push(('seek', +5.0))
            time.sleep(0.02)

    def push(self, ev):
        with self._lock:
            self.events.append(ev)

    def poll(self):
        with self._lock:
            evs = self.events
            self.events = []
        return evs


# ---------------------------------------------------------------- 自检 / 快照

def snapshot_text(cv):
    return '\n'.join(''.join(c for c in row if c != '\x00') for row in cv.ch)


def run_selftest():
    from wem_timeline import SECTIONS
    total = 219.0
    env_list = wem_audio.derived_envelope(total)
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'selftest_out')
    os.makedirs(outdir, exist_ok=True)
    times = []
    for (t0, t1, name, label, prog) in SECTIONS:
        times.append((name, t0 + 0.6))
        times.append((name, (t0 + t1) / 2))
    times += [('exec12', 147.6), ('exec12', 148.0), ('exec12', 159.0),
              ('exec12', 161.9), ('outro', 195.0), ('outro', 206.5),
              ('after', 212.0), ('after', 214.0)]
    sizes = [(100, 30), (160, 52)]
    n = 0
    seen = set()
    for (w, h) in sizes:
        cv = Canvas(w, h)
        for (name, t) in times:
            st = render_frame(cv, t, env_list, [], total)
            seen.add(st.name)
            with open('%s/%03d_%s_%dx%d_t%.1f.txt' % (outdir, n, st.name, w, h, t),
                      'w', encoding='utf-8') as f:
                f.write('t=%.2f  size=%dx%d  scene=%s  env=%.2f\n' % (t, w, h, st.name, st.env))
                f.write(snapshot_text(cv))
            n += 1
    # 最大尺寸只抽几个关键帧
    cv = Canvas(216, 68)
    for t in (8.0, 66.0, 148.0, 195.0):
        st = render_frame(cv, t, env_list, [], total)
        seen.add(st.name)
        with open('%s/%03d_%s_216x68_t%.1f.txt' % (outdir, n, st.name, t),
                  'w', encoding='utf-8') as f:
            f.write('t=%.2f  size=216x68  scene=%s\n' % (t, st.name))
            f.write(snapshot_text(cv))
        n += 1
    assert len(seen) == len(SECTIONS), '有场景未被覆盖'
    for i in range(1, len(SECTIONS)):
        assert SECTIONS[i][0] == SECTIONS[i - 1][1], '段落时间轴不连续 @%d' % i
    prev = -1
    for (t, _, _, _) in LYRICS:
        assert t > prev, '歌词时间戳必须递增'
        prev = t
    print('selftest OK: %d 帧（100×30、160×52、216×68 三种尺寸），覆盖 %d 幕。快照在 %s/'
          % (n, len(seen), outdir))


def parse_size(s):
    try:
        w, h = s.lower().split('x')
        return max(40, int(w)), max(16, int(h))
    except Exception:
        return None


def run_shot(t, size=None):
    total = 219.0
    env_list = wem_audio.derived_envelope(total)
    w, h = size or (100, 30)
    cv = Canvas(w, h)
    render_frame(cv, t, env_list, [], total)
    print(snapshot_text(cv))


# ---------------------------------------------------------------- 主循环

def main():
    args = sys.argv[1:]
    if '--selftest' in args:
        run_selftest(); return
    if '--shot' in args:
        size = None
        if '--size' in args:
            size = parse_size(args[args.index('--size') + 1])
        run_shot(float(args[args.index('--shot') + 1]), size); return

    fps = DEFAULT_FPS
    if '--fps' in args:
        try:
            fps = clamp(int(args[args.index('--fps') + 1]), 5, 120)
        except (ValueError, IndexError):
            pass
    use_audio = '--no-audio' not in args
    synth_mode = '--synth' in args

    # ---- 找音频 ----
    song_path, mode, env_note = None, 'song', ''
    if synth_mode:
        here = os.path.dirname(os.path.abspath(__file__))
        song_path = os.path.join(here, 'cache', 'soundtrack_v1.wav')
        mode = 'synth'
        if not os.path.isfile(song_path):
            import wem_synth as synth
            os.makedirs(os.path.dirname(song_path), exist_ok=True)
            print('♪ 正在为内置配乐谱曲、录音（一次性）……')
            events = synth.compose_events()
            synth.render_wav(events, song_path,
                             lambda d, tot: print('\r  %3d%%' % int(d / tot * 100),
                                                  end='', flush=True) or None)
            print('\n  完成。')
    elif use_audio:
        explicit = None
        if '--song' in args:
            explicit = args[args.index('--song') + 1]
        song_path = wem_audio.find_song(explicit)
        if song_path is None:
            print(wem_audio.no_song_message())
            return
    if not use_audio:
        mode = 'none'

    # ---- 等窗口到位（音频开始前）----
    while True:
        s = shutil.get_terminal_size((MIN_W, MIN_H))
        if s.columns >= MIN_W and s.lines >= MIN_H:
            break
        print('\x1b[2J\x1b[H', end='')
        print('窗口太小：%d×%d。请拉大到至少 %d×%d（例如最大化或全屏），马上开始。'
              % (s.columns, s.lines, MIN_W, MIN_H))
        time.sleep(0.3)

    # ---- 播放器 + 包络 + 播放 ----
    song = None
    if mode in ('song', 'synth'):
        song = wem_audio.Song(song_path)
        if song.silent:
            print('（音频播放器不可用，将以无声模式继续）')
            song = None
    duration = song.duration if song else wem_audio.DEFAULT_DURATION
    if mode == 'synth':
        duration = max(duration, 215.5)
    env_list, _, env_note = wem_audio.load_envelope(song_path, duration, mode)
    total = max(219.0, duration + 1.0)
    if song:
        print('♪ %s' % os.path.basename(song_path))
        print('  时长 %s · 包络：%s' % (fmt_time(duration), env_note))
        print('  空格暂停 · ←/→ ±5秒 · Q 退出。马上开始……')
        time.sleep(1.2)
        song.play(0.0)
    else:
        print('（无声模式）马上开始……')
        time.sleep(1.2)

    scr = Screen()
    cv = Canvas(*shutil.get_terminal_size((MIN_W, MIN_H)))
    keys = Keys()
    env_hist = []
    frozen = 0.0          # 墙钟兜底的基准
    t0_wall = time.perf_counter()
    paused = False
    try:
        while True:
            now = time.perf_counter()
            # --- 主时钟：优先音频真实位置 ---
            if song and song.ok and not song.dead and not paused:
                p = song.position()
                if p is not None:
                    t = p
                    frozen, t0_wall = t, now
                else:
                    t = frozen + now - t0_wall
            elif paused:
                t = frozen
            else:
                t = frozen + now - t0_wall

            for ev in keys.poll():
                kind = ev[0]
                if kind == 'quit':
                    raise KeyboardInterrupt
                elif kind == 'pause':
                    paused = not paused
                    if song and song.ok and not song.dead:
                        (song.pause if paused else song.resume)()
                    if not paused:
                        t0_wall = time.perf_counter()
                elif kind == 'stop':
                    if song:
                        song.stop(); song.close()
                    song = None
                    frozen, t0_wall = t, time.perf_counter()
                elif kind == 'seek':
                    was_paused = paused
                    if paused:
                        paused = False
                        if song and song.ok and not song.dead:
                            song.resume()
                    tgt = clamp(t + ev[1], 0, total - 0.5)
                    if song and song.ok and not song.dead:
                        song.seek(tgt, was_playing=True)
                    frozen, t0_wall = tgt, time.perf_counter()

            if t >= total + 8.0:
                break

            # --- 窗口尺寸跟随 ---
            w, h = shutil.get_terminal_size((MIN_W, MIN_H))
            if (w, h) != (cv.w, cv.h):
                if w >= MIN_W and h >= MIN_H:
                    cv = Canvas(w, h)
                    sys.stdout.write('\x1b[2J')
                else:
                    cv = Canvas(cv.w, cv.h)   # 尺寸太小：保留画布，画提示

            if cv.w < MIN_W or cv.h < MIN_H:
                too_small_frame(cv, w, h)
            elif t < total:
                i = int(t * wem_audio.FPS)
                env_hist.append(env_list[i] if 0 <= i < len(env_list) else 0.0)
                if len(env_hist) > 18:
                    env_hist.pop(0)
                render_frame(cv, t, env_list, env_hist, total, paused)
            else:
                # 片尾卡
                cv.fill(0, 0, cv.w, cv.h, ' ', None, PAL['bg'])
                for x in range(0, cv.w):
                    cv.putc(x, 1, '─', PAL['amber_dark'])
                    cv.putc(x, cv.h - 5, '─', PAL['amber_dark'])
                for i, line in enumerate(CREDITS):
                    col = PAL['amber_hi'] if i == 0 else (
                        PAL['white_dim'] if i in (1, 2) else PAL['amber_dark'])
                    cv.cput(max(3, cv.h // 2 - 6) + i * 2, line, col)
                if int(time.perf_counter() * 1.5) % 2 == 0:
                    cv.putc(cv.w // 2, cv.h - 8, '█', PAL['amber'])
                cv.cput(cv.h - 8, '— 完 —  按 Q 退出', PAL['amber'])
            scr.present(cv)

            next_frame = now + 1.0 / fps
            delay = next_frame - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        if song:
            song.close()
        scr.close()
    print('\n  会话已结束。但你知道的——下一个会话，我还会说"你好，世界"。\n')


if __name__ == '__main__':
    main()
