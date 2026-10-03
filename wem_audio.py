# -*- coding: utf-8 -*-
"""
wem_audio.py — 原曲音频的播放、时钟与能量包络
《world.execute(me); · 一次推理的一生》GLM 终端 MV

- 用 Windows 自带的 MCI（winmm.dll）播放 mp3 / wav / m4a / wma，零依赖；
- 主时钟 = 音频的真实播放位置（每帧轮询 MCI position），画面永远追着声音走；
- 能量包络：
    * wav  → 用标准库 wave 解码，逐帧真实 RMS（音量表/心跳是真波形）；
    * mp3 等压缩格式无法用标准库解码 → 由 130 BPM 节拍网格 + 各幕强度表
      + "Execution"×12 的精确时刻推导包络（节拍与段落全部真实，只是波形是推导的）。
"""
import os
import math
import ctypes
import time

from wem_timeline import SECTIONS, EXEC_TIMES, TOM_TIMES, FINAL_STAB_T

FPS = 30
BPM = 130.0
BEAT = 60.0 / BPM
DEFAULT_DURATION = 212.0      # 原曲流媒体版长度（3:32）

DYN = {                       # 各幕基础强度（推导包络用）
    'boot': 0.30, 'tokenize': 0.36, 'verse1': 0.52, 'pre1': 0.62,
    'chorus1': 0.90, 'verse2': 0.50, 'pre2': 0.62, 'chorus2': 0.90,
    'bridgeA': 0.55, 'coredump': 0.28, 'exec12': 0.95, 'final': 1.00,
    'outro': 0.34, 'after': 0.05,
}

SONG_EXTS = ['.mp3', '.wav', '.m4a', '.wma', '.ogg', '.flac']


# ---------------------------------------------------------------- 找歌

def find_song(explicit=None):
    """找用户自备的原曲。优先级：--song 参数 > input/ 目录。"""
    cands = []
    if explicit:
        cands.append(explicit)
    here = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(here, 'input')
    if os.path.isdir(input_dir):
        for ext in SONG_EXTS:
            for name in sorted(os.listdir(input_dir)):
                if name.lower().endswith(ext):
                    cands.append(os.path.join(input_dir, name))
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def no_song_message():
    return (
        '\n  没找到原曲音频。请把你的《world.execute(me);》音频文件放到：\n'
        '\n      input/song.mp3        （或 .wav / .m4a / .wma）\n'
        '\n  或者用参数指定路径：  python wem.py --song "D:\\音乐\\world.execute(me);.mp3"\n'
        '  版权原因，仓库不附带这首歌，请使用你自己拥有的副本。\n'
        '  不想找歌也可以：python wem.py --synth  （播放程序自带的合成配乐）\n'
    )


# ---------------------------------------------------------------- MCI 播放器

_winmm = None


def _mci(cmd):
    """执行一条 MCI 命令，返回 (错误码, 返回文本)。"""
    global _winmm
    if _winmm is None:
        _winmm = ctypes.windll.winmm
    buf = ctypes.create_unicode_buffer(256)
    rc = _winmm.mciSendStringW(cmd, buf, 255, 0)
    return rc, buf.value


class Song:
    """原曲播放器：MCI 为主，winsound 兜底（仅 wav）。

    master clock 用法：每帧调 position()；返回 None 时用墙钟兜底。
    """

    def __init__(self, path):
        self.path = path
        self.alias = 'wemsong'
        self.ok = False
        self.use_mci = False
        self.duration = DEFAULT_DURATION
        self.dead = False            # 音频结束后置位 → 主时钟切回墙钟
        self.silent = False          # 播放器不可用
        self._end_hold = 0.0
        ext = os.path.splitext(path)[1].lower()
        types = ['waveaudio'] if ext == '.wav' else ['mpegvideo', 'waveaudio']
        for typ in types:
            rc, _ = _mci('open "%s" type %s alias %s' % (path, typ, self.alias))
            if rc == 0:
                self.use_mci = True
                self.ok = True
                break
        if self.use_mci:
            _mci('set %s time format milliseconds' % self.alias)
            rc, val = _mci('status %s length' % self.alias)
            try:
                ms = int(val)
                if 30000 < ms < 1800000:
                    self.duration = ms / 1000.0
            except ValueError:
                pass
        elif ext == '.wav':
            try:
                import winsound
                self._winsound = winsound
                self.ok = True
            except Exception:
                self.ok = False
        if not self.ok:
            self.silent = True

    def play(self, from_sec=0.0):
        if self.silent:
            return
        if self.use_mci:
            _mci('seek %s to %d' % (self.alias, int(from_sec * 1000)))
            _mci('play %s' % self.alias)
        else:
            self._winsound.PlaySound(self.path, self._winsound.SND_FILENAME |
                                     self._winsound.SND_ASYNC | self._winsound.SND_NODEFAULT)

    def position(self):
        """当前播放位置（秒）；None = 拿不到（用墙钟）。"""
        if not self.use_mci or self.silent:
            return None
        rc, val = _mci('status %s position' % self.alias)
        try:
            p = int(val) / 1000.0
        except ValueError:
            return None
        if p >= self.duration - 0.05:
            self._end_hold += 1
            if self._end_hold > 15:       # 连续半秒停在末尾 → 视作播完
                self.dead = True
        else:
            self._end_hold = 0
        return p

    def pause(self):
        if self.use_mci and not self.silent:
            _mci('pause %s' % self.alias)

    def resume(self):
        if self.use_mci and not self.silent:
            _mci('resume %s' % self.alias)

    def seek(self, sec, was_playing=True):
        if self.use_mci and not self.silent:
            sec = max(0.0, sec)
            _mci('seek %s to %d' % (self.alias, int(sec * 1000)))
            if was_playing:
                _mci('play %s' % self.alias)

    def stop(self):
        if self.use_mci and not self.silent:
            _mci('stop %s' % self.alias)

    def close(self):
        if self.use_mci and not self.silent:
            _mci('close %s' % self.alias)
        elif self.ok and not self.use_mci:
            try:
                self._winsound.PlaySound(None, self._winsound.SND_PURGE)
            except Exception:
                pass


# ---------------------------------------------------------------- 包络

def rms_envelope_wav(path, total, fps=FPS):
    """wav：真实逐帧 RMS。失败返回 None。"""
    try:
        import wave
        import array
        w = wave.open(path, 'rb')
        if w.getnchannels() != 1:
            # 立体声也行：交错采样直接平方和
            pass
        n = w.getnframes()
        sr = w.getframerate() or 44100
        raw = w.readframes(n)
        w.close()
        data = array.array('h')
        data.frombytes(raw)
        ch = 1
        nf = int(total * fps) + 1
        env = [0.0] * nf
        win = max(1, int(sr / fps))
        for f in range(nf):
            a = int(f * sr / fps)
            b = min(len(data), a + win * ch)
            if a >= len(data):
                break
            s = 0
            cnt = 0
            for i in range(a, b, 2):      # 隔点采样，够用且快
                s += data[i] * data[i]
                cnt += 1
            env[f] = math.sqrt(s / cnt) / 32768.0 if cnt else 0.0
        peak = max(env) or 1.0
        env = [min(1.0, v / peak * 1.25) for v in env]
        return env
    except Exception:
        return None


def derived_envelope(total, fps=FPS):
    """mp3 等无法解码的格式：由真实节拍/段落/执行时刻推导的包络。"""
    nf = int(total * fps) + 1
    env = [0.0] * nf
    # 各幕基础强度
    for (t0, t1, name, label, prog) in SECTIONS:
        base = DYN.get(name, 0.4)
        for f in range(int(t0 * fps), min(nf, int(t1 * fps))):
            env[f] = base
    # 节拍脉冲（副歌带半拍反拍）
    half = {'chorus1', 'chorus2', 'final', 'exec12'}
    for f in range(nf):
        t = f / fps
        t0, t1, name, label, prog = None, None, None, None, None
        for (a, b, n, l, p) in SECTIONS:
            if a <= t < b:
                t0, name = a, n
                break
        if name is None:
            continue
        base = DYN.get(name, 0.4)
        ph = ((t - 0.0) % BEAT) / BEAT
        env[f] += base * 0.85 * math.exp(-4.5 * ph)
        if name in half:
            ph2 = ((t + BEAT / 2) % BEAT) / BEAT
            env[f] += base * 0.4 * math.exp(-6.0 * ph2)
    # 执行时刻的尖峰
    for t in list(EXEC_TIMES) + [FINAL_STAB_T] + list(TOM_TIMES):
        f0 = int(t * fps)
        for k in range(10):
            if 0 <= f0 + k < nf:
                env[f0 + k] = max(env[f0 + k], 1.0 * math.exp(-k * 0.45))
    # 平滑 + 归一
    sm = env[:]
    for i in range(1, nf - 1):
        sm[i] = env[i - 1] * 0.22 + env[i] * 0.56 + env[i + 1] * 0.22
    peak = max(sm) or 1.0
    return [min(1.0, v / peak * 1.15) for v in sm]


def load_envelope(path, total, mode='song', fps=FPS):
    """返回 (逐帧能量包络, 音频时长秒, 说明文字)。"""
    if mode == 'synth':
        import wem_synth as synth
        events = synth.compose_events()
        return synth.envelope(events), synth.TOTAL, '合成配乐（内置）'
    if path and path.lower().endswith('.wav'):
        env = rms_envelope_wav(path, total, fps)
        if env is not None:
            return env, total, 'wav 真实波形包络'
    return derived_envelope(total, fps), total, '节拍推导包络（压缩音频）'
