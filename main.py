# -*- coding: utf-8 -*-
"""
FRETSTORM - an original five-lane touchscreen guitar rhythm game.

Single-file Kivy application (Android / Buildozer compatible).

* All graphics are drawn with Kivy canvas primitives (no image files).
* All music and sound effects are synthesised procedurally from code
  (sine / square / triangle / saw / noise) into WAV files stored in the
  application's private cache folder the first time a song is needed.
* 20 original songs, each >= 2:30, each with its own deterministic chart.

Sections of this file
---------------------
  1. Imports, configuration and constants
  2. Small utilities
  3. Save data (SaveManager)
  4. Music theory + the 20 song definitions
  5. Song (arrangement, structure and note-chart generation) / SongLibrary
  6. Audio synthesis (Synth, AudioGenerator, SfxGenerator)
  7. Game rules engine (Note, GameEngine, SongClock)
  8. Audio playback (AudioEngine) and vibration
  9. UI helpers / widgets (labels, buttons, backdrop, stars)
 10. Gameplay renderer (GameView) and GameplayScreen
 11. Other screens (Splash, Menu, Song select, Loading, Results,
     Settings, How to play, Credits)
 12. Application class
"""

# ============================================================================
# 1. IMPORTS, CONFIGURATION AND CONSTANTS
# ============================================================================
import os
import sys
import math
import json
import time
import struct
import random
import threading
import traceback
from array import array

SELFTEST = '--selftest' in sys.argv      # Kivy would try to parse this flag itself
if SELFTEST:
    sys.argv.remove('--selftest')

try:                                   # C-speed mixing when available
    import audioop
except Exception:                      # pragma: no cover (Python >= 3.13)
    audioop = None

from kivy.utils import platform
from kivy.config import Config

Config.set('kivy', 'exit_on_escape', '0')
Config.set('graphics', 'maxfps', '60')
Config.set('graphics', 'vsync', '1')
if platform not in ('android', 'ios'):
    Config.set('input', 'mouse', 'mouse,disable_multitouch')
    Config.set('graphics', 'width', '1000')
    Config.set('graphics', 'height', '520')
    Config.set('graphics', 'resizable', '1')

APP_NAME = 'FRETSTORM'
GEN_VERSION = 4            # bump to invalidate every cached song
RATE = 22050               # synthesis sample rate (Hz)
COUNT_IN_MIN = 2.9         # seconds of count-in before the first bar
LANES = 5
LANE_NAMES = ['GREEN', 'RED', 'YELLOW', 'BLUE', 'ORANGE']
LANE_COLORS = [
    (0.10, 0.92, 0.35),
    (0.98, 0.20, 0.28),
    (1.00, 0.88, 0.12),
    (0.20, 0.52, 1.00),
    (1.00, 0.55, 0.08),
]

# hit windows in seconds (absolute timing error)
WIN_PERFECT = 0.050
WIN_GREAT = 0.095
WIN_GOOD = 0.140
GRADE_NAMES = ['PERFECT', 'GREAT', 'GOOD']
GRADE_POINTS = [50, 35, 20]
GRADE_HEALTH = [0.030, 0.022, 0.012]
GRADE_ACC = [1.0, 0.85, 0.6]
MISS_HEALTH = 0.055
SUSTAIN_TICK = 0.10
STORM_CHARGE_PER_NOTE = 0.02
STORM_MIN_TO_ACTIVATE = 0.5
STORM_FULL_SECONDS = 16.0
INITIAL_UNLOCKED = 4

DEFAULT_SETTINGS = {
    'master': 0.9, 'music': 0.9, 'sfx': 0.7, 'speed': 1.0,
    'cal': 60 if platform == 'android' else 0,
    'vibration': True, 'particles': True, 'failure': True,
    'unlock_all': False,
}


# ============================================================================
# 2. SMALL UTILITIES
# ============================================================================
def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def lerp(a, b, t):
    return a + (b - a) * t


def fmt_time(sec):
    sec = int(max(0, sec))
    return '%d:%02d' % (sec // 60, sec % 60)


def fmt_score(n):
    return '{:,}'.format(int(n))


def log(*a):
    try:
        print('[FRETSTORM]', *a)
    except Exception:
        pass


# ============================================================================
# 3. SAVE DATA
# ============================================================================
def _num(v, lo, hi, default):
    try:
        v = float(v)
        if v != v:
            return default
        return clamp(v, lo, hi)
    except Exception:
        return default


class SaveManager(object):
    """JSON persistence for settings, high scores and unlock progress."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, 'fretstorm_save.json')
        self.settings = dict(DEFAULT_SETTINGS)
        self.scores = {}
        self.completed = set()
        self.load()

    # -- loading -----------------------------------------------------------
    def load(self):
        data = None
        try:
            with open(self.path, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = None
        except Exception:
            data = None
        if data is None:
            return
        try:
            s = data.get('settings', {})
            if isinstance(s, dict):
                d = self.settings
                d['master'] = _num(s.get('master'), 0, 1, d['master'])
                d['music'] = _num(s.get('music'), 0, 1, d['music'])
                d['sfx'] = _num(s.get('sfx'), 0, 1, d['sfx'])
                d['speed'] = _num(s.get('speed'), 0.6, 1.8, d['speed'])
                d['cal'] = int(_num(s.get('cal'), -300, 400, d['cal']))
                for k in ('vibration', 'particles', 'failure', 'unlock_all'):
                    if isinstance(s.get(k), bool):
                        d[k] = s[k]
            sc = data.get('scores', {})
            if isinstance(sc, dict):
                for k, v in sc.items():
                    try:
                        i = int(k)
                        if 0 <= i < 20 and isinstance(v, dict):
                            self.scores[i] = {
                                'score': int(_num(v.get('score'), 0, 10 ** 9, 0)),
                                'stars': int(_num(v.get('stars'), 0, 5, 0)),
                                'acc': _num(v.get('acc'), 0, 100, 0),
                                'combo': int(_num(v.get('combo'), 0, 99999, 0)),
                            }
                    except Exception:
                        pass
            cp = data.get('completed', [])
            if isinstance(cp, list):
                for i in cp:
                    try:
                        if 0 <= int(i) < 20:
                            self.completed.add(int(i))
                    except Exception:
                        pass
        except Exception:
            log('save parse problem', traceback.format_exc())

    # -- saving ------------------------------------------------------------
    def save(self):
        try:
            os.makedirs(self.folder, exist_ok=True)
            data = {
                'version': 1,
                'settings': self.settings,
                'scores': dict((str(k), v) for k, v in self.scores.items()),
                'completed': sorted(self.completed),
            }
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:
            log('save failed', traceback.format_exc())

    # -- progress ----------------------------------------------------------
    def unlocked_count(self):
        if self.settings.get('unlock_all'):
            return 20
        return min(20, INITIAL_UNLOCKED + len(self.completed))

    def is_unlocked(self, idx):
        return idx < self.unlocked_count()

    def record(self, idx, score, stars, acc, combo, completed):
        prev = self.scores.get(idx)
        new_best = False
        if completed:
            self.completed.add(idx)
        if prev is None or score > prev['score']:
            self.scores[idx] = {'score': int(score), 'stars': int(stars),
                                'acc': round(float(acc), 2), 'combo': int(combo)}
            new_best = True
        elif prev is not None and stars > prev['stars']:
            prev['stars'] = int(stars)
        self.save()
        return new_best

    def best(self, idx):
        return self.scores.get(idx)


# ============================================================================
# 4. MUSIC THEORY DATA AND THE 20 SONG DEFINITIONS
# ============================================================================
NOTE_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']

SCALES = {
    'MAJOR': [0, 2, 4, 5, 7, 9, 11],
    'MINOR': [0, 2, 3, 5, 7, 8, 10],
    'DORIAN': [0, 2, 3, 5, 7, 9, 10],
    'MIXO': [0, 2, 4, 5, 7, 9, 10],
    'PHRYG': [0, 1, 3, 5, 7, 8, 10],
    'LYD': [0, 2, 4, 6, 7, 9, 11],
    'HARM': [0, 2, 3, 5, 7, 8, 11],
    'MINPENT': [0, 3, 5, 7, 10],
    'MAJPENT': [0, 2, 4, 7, 9],
    'BLUES': [0, 3, 5, 6, 7, 10],
}

# chord qualities -> semitone stacks (three voices maximum)
CHORD_TONES = {
    'p': (0, 7, 12), 'M': (0, 7, 16), 'm': (0, 7, 15), 'D': (0, 10, 16),
    's': (0, 5, 7),
}
TRIAD = {'p': (0, 7, 12), 'M': (0, 4, 7), 'm': (0, 3, 7), 'D': (0, 4, 10),
         's': (0, 5, 7)}


def parse_prog(text):
    """'0m 8M 3M' -> [(0,'m'), (8,'M'), (3,'M')]"""
    out = []
    for tok in text.split():
        q = tok[-1]
        out.append((int(tok[:-1]), q))
    return out


def P(s):
    s = s.replace(' ', '')
    assert len(s) == 16, s
    return s


# drum patterns: k = kick, s = snare, h = hi-hat, o = open hat ('x' hit,
# 'X' accent, 'g' ghost, '.' rest).  swing is measured in 16th steps.
DRUMS = {
    'rock': dict(k=P('x... .... x.x. ....'), s=P('.... x... .... x...'), h=P('X.x. X.x. X.x. X.x.')),
    'hard': dict(k=P('x... ..x. x... ..x.'), s=P('.... x... .... x...'), h=P('X.x. x.x. X.x. x.x.')),
    'punk': dict(k=P('x.x. ..x. x.x. ..x.'), s=P('.... x... .... x...'), h=P('x.x. x.x. x.x. x.x.')),
    'alt': dict(k=P('x... ..x. ..x. ....'), s=P('.... .... x... ....'), h=P('x.x. x.x. x.x. x.x.')),
    'metal': dict(k=P('x.x. xxx. x.x. xxx.'), s=P('.... x... .... x...'), h=P('X... x... X... x...')),
    'power': dict(k=P('x..x x..x x..x x..x'), s=P('.... x... .... x...'), h=P('X.x. x.x. X.x. x.x.')),
    'prog': dict(k=P('x..x ..x. ..x. x...'), s=P('.... x... .... x..x'), h=P('x.x. x.x. x.x. x.xx')),
    'blues': dict(k=P('x... .... x... ....'), s=P('.... x... .... x...'), h=P('x.x. x.x. x.x. x.x.'), swing=0.66),
    'surf': dict(k=P('x.x. .... x.x. ....'), s=P('.... x... .... x...'), h=P('x.x. x.x. x.x. x.x.')),
    'funk': dict(k=P('x..x ..x. ..x. ...x'), s=P('.... x..g .g.. x..g'), h=P('xgxg xgxg xgxg xgxg'), swing=0.12),
    'electro': dict(k=P('x... x... x... x...'), s=P('.... x... .... x...'), h=P('.... .... .... ....'), o=P('..x. ..x. ..x. ..x.')),
    'arena': dict(k=P('x... .... x... ..x.'), s=P('.... x... .... x...'), h=P('X... x... X... x...')),
    'garage': dict(k=P('x... ..x. x... ....'), s=P('.... x... .... x...'), h=P('X.x. X.x. X.x. X.xx')),
    'south': dict(k=P('x... .... x.x. ....'), s=P('.... x... .... x...'), h=P('x.x. x.x. x.x. x.x.'), swing=0.3),
    'indust': dict(k=P('x... x... x... x..x'), s=P('.... x... .... x...'), h=P('x.xx x.xx x.xx x.xx')),
    'melodic': dict(k=P('x.x. x.x. x.x. x.xx'), s=P('.... x... .... x...'), h=P('X... x... X... x...')),
    'psych': dict(k=P('x... .... ..x. ....'), s=P('.... .... x... ....'), h=P('x... x... x... x...')),
    'speed': dict(k=P('x... x.x. x... x.x.'), s=P('.... x... .... x...'), h=P('x.x. x.x. x.x. x.x.')),
    'epic': dict(k=P('x.x. x.xx x.x. x.xx'), s=P('.... x... .... x...'), h=P('X... x... X... x...')),
}
TOM_FILL_STYLES = ('arena', 'epic', 'psych', 'prog', 'power', 'melodic')

# bass patterns: (step, semitones above chord root, length in steps)
BASS = {
    'r8': [(s, 0, 2) for s in range(0, 16, 2)],
    'r4': [(s, 0, 4) for s in (0, 4, 8, 12)],
    'sync': [(0, 0, 3), (3, 0, 3), (6, 0, 2), (8, 0, 3), (11, 0, 3), (14, 7, 2)],
    'walk': [(0, 0, 4), (4, 4, 4), (8, 7, 4), (12, 9, 4)],
    'oct': [(s, 0 if (s // 2) % 2 == 0 else 12, 2) for s in range(0, 16, 2)],
    'gal': [(s, 0, 1) for s in (0, 3, 4, 7, 8, 11, 12, 15)],
    'p16': [(s, 0, 1) for s in range(16)],
    'pulse': [(0, 0, 2), (2, 0, 2), (4, 0, 2), (6, 7, 2), (8, 0, 2), (10, 0, 2), (12, 0, 2), (14, 10, 2)],
    'funk': [(0, 0, 2), (3, 0, 1), (6, 12, 1), (8, 0, 2), (10, 7, 2), (13, 0, 1), (14, 10, 2)],
    'drive': [(0, 0, 2), (2, 0, 2), (4, 0, 2), (6, 12, 2), (8, 0, 2), (10, 0, 2), (12, 0, 2), (14, 12, 2)],
    'long': [(0, 0, 16)],
    'dot': [(0, 0, 6), (6, 0, 2), (8, 0, 4), (12, 0, 2), (14, 0, 2)],
}

# riff rhythm templates (step numbers at which a guitar hit starts)
RHYTHMS = {
    'straight': [0, 2, 4, 6, 8, 10, 12, 14],
    'push': [0, 2, 4, 7, 8, 10, 12, 15],
    'gallop': [0, 2, 3, 4, 6, 7, 8, 10, 11, 12, 14, 15],
    'sync': [0, 3, 6, 8, 11, 14],
    'quarter': [0, 4, 8, 12],
    'offbeat': [0, 2, 6, 8, 10, 14],
    'sixteenth': list(range(16)),
    'dot': [0, 3, 6, 10, 12, 14],
    'broken': [0, 1, 4, 6, 8, 9, 12, 14],
    'stab': [0, 3, 4, 8, 11, 12],
}

OPEN_PATTERNS = [
    [(0, 8), (8, 8)],
    [(0, 6), (6, 2), (8, 6), (14, 2)],
    [(0, 4), (4, 4), (8, 4), (12, 4)],
]

ROLE_ID = {'intro': 1, 'verse': 2, 'pre': 3, 'chorus': 4, 'solo': 5,
           'bridge': 6, 'outro': 7}
ROLE_LEVEL = {'intro': 0, 'verse': 1, 'pre': 2, 'chorus': 3, 'solo': 3,
              'bridge': 1, 'outro': 0}

# song layouts: (section name, role, bars, chord-progression key)
LAYOUTS = [
    [('INTRO', 'intro', 4, 'intro'), ('VERSE', 'verse', 8, 'verse'),
     ('PRE-CHORUS', 'pre', 4, 'pre'), ('CHORUS', 'chorus', 8, 'chorus'),
     ('VERSE 2', 'verse', 8, 'verse'), ('PRE-CHORUS', 'pre', 4, 'pre'),
     ('CHORUS', 'chorus', 8, 'chorus'), ('SOLO', 'solo', 8, 'bridge'),
     ('FINAL CHORUS', 'chorus', 8, 'chorus'), ('OUTRO', 'outro', 4, 'intro')],
    [('INTRO', 'intro', 8, 'intro'), ('VERSE', 'verse', 8, 'verse'),
     ('CHORUS', 'chorus', 8, 'chorus'), ('VERSE 2', 'verse', 8, 'verse'),
     ('PRE-CHORUS', 'pre', 4, 'pre'), ('CHORUS', 'chorus', 8, 'chorus'),
     ('BRIDGE', 'bridge', 8, 'bridge'), ('SOLO', 'solo', 8, 'verse'),
     ('FINAL CHORUS', 'chorus', 8, 'chorus'), ('OUTRO', 'outro', 4, 'intro')],
    [('INTRO', 'intro', 4, 'intro'), ('VERSE', 'verse', 8, 'verse'),
     ('PRE-CHORUS', 'pre', 4, 'pre'), ('CHORUS', 'chorus', 8, 'chorus'),
     ('BRIDGE', 'bridge', 8, 'bridge'), ('VERSE 2', 'verse', 8, 'verse'),
     ('CHORUS', 'chorus', 8, 'chorus'), ('SOLO', 'solo', 12, 'bridge'),
     ('FINAL CHORUS', 'chorus', 12, 'chorus'), ('OUTRO', 'outro', 4, 'intro')],
]
GROW_ORDER = ('SOLO', 'FINAL CHORUS', 'VERSE 2', 'BRIDGE')

# fmt: off
SONG_DEFS = [
    dict(title='Neon Roadhouse', style='Classic Rock', bpm=108, key=9, mode='Major', lscale='MAJPENT',
         diff=1, target=158, layout=0, drum='rock', bass='r8', gm='riff', cm='open', rt='straight',
         iv=[0, 0, 0, 7, 5], lead='gtr', swing=0.0, pad=False, dens=0.30, drive=3.0, mute=0.5, seed=101,
         progs=dict(intro='0M 5M', verse='0M 5M 0M 7M', pre='9m 5M 7M 7M', chorus='0M 7M 5M 0M', bridge='9m 5M 0M 7M')),
    dict(title='Rust & Thunder', style='Hard Rock', bpm=118, key=4, mode='Minor', lscale='MINPENT',
         diff=2, target=160, layout=1, drum='hard', bass='drive', gm='riff', cm='open', rt='push',
         iv=[0, 0, 7, 12, 3], lead='gtr', swing=0.0, pad=False, dens=0.40, drive=4.0, mute=0.6, seed=202,
         progs=dict(intro='0p 0p 10p 10p', verse='0m 0m 10M 8M', pre='5m 7m 8M 10M', chorus='0p 10p 8p 7p', bridge='8M 10M 0m 0m')),
    dict(title='Safety Pin Sunrise', style='Punk Rock', bpm=168, key=2, mode='Major', lscale='MAJPENT',
         diff=2, target=155, layout=2, drum='punk', bass='r8', gm='riff', cm='riff', rt='straight',
         iv=[0, 0, 0, 0, 7], lead='gtr', swing=0.0, pad=False, dens=0.45, drive=3.5, mute=0.3, seed=303,
         progs=dict(intro='0p 7p', verse='0p 0p 5p 7p', pre='5p 5p 7p 7p', chorus='0p 7p 9p 5p', bridge='9p 5p 0p 7p')),
    dict(title='Static Bloom', style='Alternative Rock', bpm=96, key=6, mode='Minor', lscale='MINOR',
         diff=3, target=165, layout=0, drum='alt', bass='sync', gm='arp', cm='open', rt='sync',
         iv=[0, 7, 3, 10], lead='gtr', swing=0.0, pad=False, dens=0.45, drive=2.5, mute=0.4, seed=404,
         progs=dict(intro='0m 8M', verse='0m 8M 3M 10M', pre='5m 8M 5m 7M', chorus='3M 10M 0m 8M', bridge='5m 0m 8M 10M')),
    dict(title='Iron Cathedral', style='Metal', bpm=132, key=4, mode='Phrygian', lscale='PHRYG',
         diff=4, target=165, layout=1, drum='metal', bass='gal', gm='riff', cm='riff', rt='gallop',
         iv=[0, 0, 1, 0, 3, 6], lead='gtr', swing=0.0, pad=False, dens=0.55, drive=6.0, mute=0.85, seed=505,
         progs=dict(intro='0p 0p 0p 1p', verse='0p 0p 1p 0p', pre='3p 1p 0p 6p', chorus='0p 8p 1p 10p', bridge='8p 6p 1p 0p')),
    dict(title='Dragon Lantern Sky', style='Power Metal', bpm=164, key=2, mode='Harmonic Minor', lscale='HARM',
         diff=5, target=156, layout=2, drum='power', bass='gal', gm='riff', cm='open', rt='straight',
         iv=[0, 7, 12, 0, 5], lead='gtr', swing=0.0, pad=False, dens=0.70, drive=5.0, mute=0.7, seed=606,
         progs=dict(intro='0m 8M 7M 0m', verse='0m 5m 8M 7M', pre='0m 3M 5m 7M', chorus='8M 10M 7M 0m', bridge='5m 10M 3M 7M')),
    dict(title='Paper Moon Odyssey', style='Progressive Rock', bpm=124, key=7, mode='Dorian', lscale='DORIAN',
         diff=5, target=175, layout=1, drum='prog', bass='sync', gm='arp', cm='riff', rt='broken',
         iv=[0, 7, 3, 5], lead='syn', swing=0.0, pad=True, dens=0.55, drive=3.0, mute=0.5, seed=707,
         progs=dict(intro='0m 5M', verse='0m 5M 0m 10M', pre='2m 5M 3M 7m', chorus='3M 10M 5M 0m', bridge='8M 3M 10M 5M')),
    dict(title='Delta Mile Marker', style='Blues Rock', bpm=100, key=4, mode='Blues', lscale='BLUES',
         diff=4, target=162, layout=0, drum='blues', bass='walk', gm='riff', cm='open', rt='dot',
         iv=[0, 0, 4, 7], lead='gtr', swing=0.66, pad=False, dens=0.50, drive=2.2, mute=0.3, seed=808,
         progs=dict(intro='0D 5D', verse='0D 5D 0D 0D 5D 5D 0D 0D 7D 5D 0D 7D', pre='5D 5D 0D 0D', chorus='0D 5D 7D 5D', bridge='5D 0D 7D 5D')),
    dict(title='Tidal Chrome', style='Surf Rock', bpm=148, key=9, mode='Minor', lscale='MINOR',
         diff=6, target=158, layout=2, drum='surf', bass='r8', gm='trem', cm='riff', rt='straight',
         iv=[0, 0, 7, 12, 10], lead='gtr', swing=0.0, pad=False, dens=0.80, drive=1.6, mute=0.5, seed=909,
         progs=dict(intro='0m 7M', verse='0m 0m 7M 7M', pre='3M 3M 7M 7M', chorus='0m 10M 8M 7M', bridge='5m 0m 7M 0m')),
    dict(title='Velvet Funk Riot', style='Funk Rock', bpm=106, key=7, mode='Dorian', lscale='DORIAN',
         diff=6, target=170, layout=1, drum='funk', bass='funk', gm='skank', cm='open', rt='stab',
         iv=[0, 7, 3, 10], lead='gtr', swing=0.12, pad=False, dens=0.65, drive=2.0, mute=0.6, seed=1010,
         progs=dict(intro='0D 5D', verse='0D 0D 5D 5D', pre='2m 5D 0D 0D', chorus='0D 3M 5D 10M', bridge='10M 5D 0D 7D')),
    dict(title='Circuit Halo', style='Synth Rock', bpm=122, key=0, mode='Minor', lscale='MINOR',
         diff=6, target=172, layout=0, drum='electro', bass='oct', gm='arp', cm='open', rt='offbeat',
         iv=[0, 7, 12, 3], lead='syn', swing=0.0, pad=True, dens=0.60, drive=2.5, mute=0.5, seed=1111,
         progs=dict(intro='0m 3M', verse='0m 3M 8M 10M', pre='5m 3M 5m 7M', chorus='8M 3M 10M 7M', bridge='3M 8M 0m 7M')),
    dict(title='Stadium of Ghosts', style='Arena Rock', bpm=104, key=3, mode='Major', lscale='MAJOR',
         diff=7, target=168, layout=2, drum='arena', bass='pulse', gm='riff', cm='open', rt='quarter',
         iv=[0, 7, 12, 5], lead='gtr', swing=0.0, pad=True, dens=0.55, drive=3.2, mute=0.4, seed=1212,
         progs=dict(intro='0M 7M', verse='0M 7M 9m 5M', pre='2m 5M 9m 7M', chorus='5M 0M 7M 9m', bridge='9m 7M 5M 7M')),
    dict(title='Garage Door Gospel', style='Garage Rock', bpm=138, key=7, mode='Major', lscale='MAJPENT',
         diff=7, target=160, layout=1, drum='garage', bass='r8', gm='riff', cm='riff', rt='push',
         iv=[0, 0, 5, 7, 12], lead='gtr', swing=0.0, pad=False, dens=0.65, drive=4.5, mute=0.5, seed=1313,
         progs=dict(intro='0p 5p', verse='0p 5p 0p 5p', pre='9p 5p 7p 7p', chorus='0p 10p 5p 0p', bridge='5p 7p 0p 0p')),
    dict(title='Bayou Wildfire', style='Southern Rock', bpm=112, key=2, mode='Mixolydian', lscale='MIXO',
         diff=7, target=168, layout=0, drum='south', bass='pulse', gm='riff', cm='open', rt='dot',
         iv=[0, 0, 4, 7, 12], lead='gtr', swing=0.3, pad=False, dens=0.65, drive=2.8, mute=0.4, seed=1414,
         progs=dict(intro='0M 10M', verse='0M 10M 5M 0M', pre='7m 5M 0M 0M', chorus='0M 5M 10M 0M', bridge='5M 0M 10M 5M')),
    dict(title='Rivet Heart', style='Industrial Rock', bpm=128, key=1, mode='Phrygian', lscale='PHRYG',
         diff=8, target=166, layout=2, drum='indust', bass='p16', gm='riff', cm='riff', rt='sixteenth',
         iv=[0, 0, 0, 6, 1], lead='syn', swing=0.0, pad=True, dens=0.70, drive=7.0, mute=0.9, seed=1515,
         progs=dict(intro='0p 0p 6p 5p', verse='0p 6p 0p 5p', pre='1p 1p 6p 6p', chorus='0p 3p 6p 5p', bridge='6p 5p 1p 0p')),
    dict(title='Aurora Requiem', style='Melodic Metal', bpm=148, key=4, mode='Minor', lscale='MINOR',
         diff=8, target=162, layout=1, drum='melodic', bass='gal', gm='riff', cm='open', rt='gallop',
         iv=[0, 7, 12, 3], lead='gtr', swing=0.0, pad=True, dens=0.80, drive=5.0, mute=0.7, seed=1616,
         progs=dict(intro='0m 8M 10M 7M', verse='0m 5m 8M 10M', pre='3M 8M 5m 7M', chorus='8M 3M 10M 5m', bridge='0m 7M 8M 3M')),
    dict(title='Kaleidoscope Drift', style='Psychedelic Rock', bpm=100, key=2, mode='Lydian', lscale='LYD',
         diff=8, target=176, layout=0, drum='psych', bass='dot', gm='arp', cm='open', rt='broken',
         iv=[0, 4, 7, 9], lead='syn', swing=0.0, pad=True, dens=0.85, drive=1.8, mute=0.3, seed=1717,
         progs=dict(intro='0M 2M', verse='0M 2M 0M 2M', pre='9m 7M 2M 0M', chorus='0M 2M 7M 9m', bridge='5M 4m 9m 2M')),
    dict(title='Velocity Junkie', style='Speed Rock', bpm=176, key=9, mode='Minor', lscale='MINPENT',
         diff=9, target=158, layout=2, drum='speed', bass='r8', gm='riff', cm='riff', rt='straight',
         iv=[0, 0, 7, 3, 12], lead='gtr', swing=0.0, pad=False, dens=0.90, drive=4.5, mute=0.6, seed=1818,
         progs=dict(intro='0p 3p 5p 7p', verse='0p 3p 5p 7p 0p 3p 8p 7p', pre='8p 7p 8p 7p', chorus='0p 8p 10p 7p', bridge='5p 3p 7p 0p')),
    dict(title='Pulse Protocol', style='Electronic Rock', bpm=132, key=5, mode='Minor', lscale='MINOR',
         diff=9, target=170, layout=1, drum='electro', bass='p16', gm='arp', cm='riff', rt='sixteenth',
         iv=[0, 7, 12, 10], lead='syn', swing=0.0, pad=True, dens=0.85, drive=3.5, mute=0.8, seed=1919,
         progs=dict(intro='0m 10M', verse='0m 10M 8M 10M', pre='3M 3M 10M 10M', chorus='0m 8M 7M 10M', bridge='8M 7M 0m 0m')),
    dict(title='Ascension of the Storm', style='Epic Rock / Metal', bpm=152, key=2, mode='Minor', lscale='HARM',
         diff=10, target=190, layout=2, drum='epic', bass='gal', gm='riff', cm='open', rt='gallop',
         iv=[0, 7, 12, 3, 5], lead='gtr', swing=0.0, pad=True, dens=0.95, drive=5.5, mute=0.75, seed=2020,
         progs=dict(intro='0p 0p 8p 7p', verse='0m 0m 8M 10M 0m 0m 5m 7M', pre='0m 10M 8M 7M',
                    chorus='8M 10M 7M 0m 8M 3M 10M 7M', bridge='5m 3M 8M 7M')),
]
# fmt: on


def scale_note(base, scale, deg):
    octv, idx = divmod(deg, len(scale))
    return base + 12 * octv + scale[idx]


def midi_hz(m):
    return 440.0 * (2.0 ** ((m - 69) / 12.0))


# ============================================================================
# 5. SONG: STRUCTURE, ARRANGEMENT AND CHART GENERATION
# ============================================================================
class Song(object):
    """One playable track.  All data is derived deterministically from its
    definition dict, so audio and chart always agree with each other."""

    def __init__(self, idx, d):
        self.idx = idx
        self.d = d
        self.title = d['title']
        self.style = d['style']
        self.bpm = d['bpm']
        self.diff = d['diff']
        self.seed = d['seed']
        self.key_pc = d['key']
        self.key_name = '%s %s' % (NOTE_NAMES[d['key']], d['mode'])
        self.scale = SCALES[d['lscale']]
        self.swing = d.get('swing', 0.0) or DRUMS[d['drum']].get('swing', 0.0)
        # sample-exact timing: a bar is a whole number of 16ths
        self.bar_samples = (int(round(240.0 / self.bpm * RATE)) // 16) * 16
        self.step_samples = self.bar_samples // 16
        self.bar_sec = self.bar_samples / float(RATE)
        self.progs = dict((k, parse_prog(v)) for k, v in d['progs'].items())
        self._build_layout(d['layout'], d['target'])
        self.count_in = max(1, int(math.ceil(COUNT_IN_MIN / self.bar_sec)))
        self.lead_time = self.count_in * self.bar_sec
        self.duration = self.total_bars * self.bar_sec
        self.total_samples = (self.count_in + self.total_bars) * self.bar_samples
        self.total_time = self.total_samples / float(RATE)
        self.riffs = [self._make_riff(random.Random(self.seed * 3 + j), j == 2)
                      for j in range(3)]
        self.open_pat = OPEN_PATTERNS[self.seed % 3]
        self._chart = None

    # -- structure -----------------------------------------------------------
    def _build_layout(self, layout_id, target):
        secs = [dict(name=n, role=r, bars=b, prog=p) for (n, r, b, p) in LAYOUTS[layout_id]]
        order = []
        for name in GROW_ORDER:
            for i, s in enumerate(secs):
                if s['name'] == name:
                    order.append(i)
                    break
        n = 0
        while sum(s['bars'] for s in secs) * self.bar_sec < target and order:
            secs[order[n % len(order)]]['bars'] += 4
            n += 1
        start = 0
        self.bar_info = []
        for si, s in enumerate(secs):
            s['start'] = start
            for k in range(s['bars']):
                self.bar_info.append((si, k))
            start += s['bars']
        self.sections = secs
        self.total_bars = start

    def chord_at(self, sec, k):
        prog = self.progs[sec['prog']]
        return prog[k % len(prog)]

    def gbase(self, off):
        """MIDI note of a chord root in the guitar register (E2..D#3)."""
        return 40 + ((self.key_pc + off - 40) % 12)

    @staticmethod
    def voice(root, quality):
        t = CHORD_TONES.get(quality, CHORD_TONES['p'])
        return (root + t[0], root + t[1], root + t[2])

    def section_at_time(self, t):
        """Section name for a song time (audio-file seconds)."""
        b = int((t - self.lead_time) / self.bar_sec)
        if b < 0:
            return 'COUNT-IN'
        b = min(b, self.total_bars - 1)
        return self.sections[self.bar_info[b][0]]['name']

    # -- riff / melody generation -------------------------------------------
    def _make_riff(self, rng, sparse):
        starts = list(RHYTHMS[self.d['rt']])
        if sparse:
            starts = [s for s in starts if s % 4 == 0] or [0, 8]
        else:
            starts = [s for s in starts if s == 0 or s % 4 == 0 or rng.random() > 0.12]
        iv = self.d['iv']
        out = []
        for i, s in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else 16
            ln = nxt - s
            ivl = 0 if s == 0 else rng.choice(iv)
            muted = ln <= 2 and rng.random() < self.d['mute']
            out.append((s, ivl, ln, muted))
        return out

    def riff_bar(self, sec, k, b):
        role = sec['role']
        if role in ('intro', 'outro'):
            return self.riffs[2]
        if k % 4 == 3:
            return self.riffs[1]
        return self.riffs[0]

    def _deg_of(self, off):
        sc = self.scale
        best, bd = 0, 99
        o = off % 12
        for i, v in enumerate(sc):
            dist = min(abs(v - o), 12 - abs(v - o))
            if dist < bd:
                best, bd = i, dist
        return best

    def lead_base(self):
        return 55 + ((self.key_pc - 55) % 12) + 12

    def lead_bar(self, sec, k, chord):
        """Melody for one bar: list of (step, midi, length_in_steps)."""
        role = sec['role']
        ph = k % 4
        var = 1 if (k % 8) >= 4 else 0
        if k % 8 == 7:
            var = 2
        rng = random.Random(self.seed * 10007 + ROLE_ID[role] * 977 + ph * 61 + var * 17)
        factor = {'solo': 1.0, 'chorus': 0.75, 'bridge': 0.45}.get(role, 0.5)
        p_short = clamp(self.d['dens'] * factor, 0.05, 0.95)
        sc = self.scale
        base = self.lead_base()
        deg_root = self._deg_of(chord[0])
        lo, hi = -2, len(sc) + 3
        deg = deg_root + rng.choice((0, 0, 1, 2, len(sc)))
        deg = clamp(deg, lo, hi)
        steps = (-2, -1, -1, 0, 1, 1, 2, 2, -3, 3)
        out = []
        step = 0
        while step < 16:
            if step > 0 and rng.random() < 0.10 * (1.0 - p_short):
                step += 2
                continue
            if rng.random() < p_short:
                ln = rng.choice((1, 1, 2, 2, 2, 3, 4))
            else:
                ln = rng.choice((4, 4, 6, 8, 3, 2))
            ln = min(ln, 16 - step)
            if role == 'chorus' and step % 4 == 0:
                ln = max(ln, 2)
            out.append((step, base + scale_note(0, sc, deg), ln))
            step += ln
            deg = clamp(deg + rng.choice(steps), lo, hi)
        if var == 2 and out:
            s, _m, ln = out[-1]
            out[-1] = (s, base + scale_note(0, sc, deg_root), ln)
        return out

    # -- audio arrangement ----------------------------------------------------
    def count_in_events(self, i):
        ev = [('hat', s, (), 0, 1.0 if s == 0 else 0.75) for s in (0, 4, 8, 12)]
        if i == self.count_in - 1:
            ev += [('snare', 12, (), 0, 0.75), ('snare', 14, (), 0, 0.9)]
        return ev

    def arrange_bar(self, b):
        """All sound events of song bar b: (inst, step, notes, len, vel)."""
        si, k = self.bar_info[b]
        sec = self.sections[si]
        chord = self.chord_at(sec, k)
        role = sec['role']
        ev = []
        last = (b == self.total_bars - 1)
        ev += self._drums(sec, k, b, last)
        ev += self._bass(sec, k, chord, last)
        ev += self._guitar(sec, k, b, chord, last)
        if role in ('chorus', 'solo', 'bridge') and not last:
            lead_inst = 'syn' if self.d['lead'] == 'syn' else 'glead'
            for (st, m, ln) in self.lead_bar(sec, k, chord):
                ev.append((lead_inst, st, (m,), ln, 0.9 if st % 4 == 0 else 0.75))
        if self.d['pad'] and role in ('chorus', 'bridge', 'pre') and not last:
            tri = TRIAD.get(chord[1], TRIAD['M'])
            r = self.gbase(chord[0]) + 12
            ev.append(('pad', 0, (r + tri[0], r + tri[1], r + tri[2]), 16, 0.6))
        return ev

    def _drums(self, sec, k, b, last):
        st = DRUMS[self.d['drum']]
        role = sec['role']
        lvl = ROLE_LEVEL[role]
        ev = []
        if last:
            return [('kick', 0, (), 0, 1.0), ('crash', 0, (), 0, 1.0)]
        end_of_sec = (k == sec['bars'] - 1)
        kp, sp, hp = st['k'], st['s'], st['h']
        op = st.get('o', '.' * 16)
        vmap = {'x': 0.75, 'X': 1.0, 'g': 0.4}
        for s in range(16):
            if end_of_sec and s >= 12:
                continue
            if lvl == 0:
                if hp[s] != '.' and s % 4 == 0:
                    ev.append(('hat', s, (), 0, 0.6))
                if s == 0 and k % 2 == 0:
                    ev.append(('kick', s, (), 0, 0.8))
                continue
            if kp[s] != '.':
                ev.append(('kick', s, (), 0, vmap[kp[s]]))
            if sp[s] != '.':
                ev.append(('snare', s, (), 0, vmap[sp[s]]))
            if hp[s] != '.' and (lvl >= 2 or s % 2 == 0):
                ev.append(('hat', s, (), 0, vmap[hp[s]] * 0.9))
            if lvl >= 2 and op[s] != '.':
                ev.append(('ohat', s, (), 0, 0.8))
        if lvl >= 1 and k == 0 and role != 'intro':
            ev.append(('crash', 0, (), 0, 0.9))
        elif lvl == 3 and k % 4 == 0:
            ev.append(('crash', 0, (), 0, 0.8))
        if end_of_sec:
            toms = self.d['drum'] in TOM_FILL_STYLES
            fills = [(12, 'snare', 0.6), (13, 'snare', 0.6), (14, 'snare', 0.8), (15, 'snare', 1.0)]
            if toms:
                fills = [(12, 'tom1', 0.9), (13, 'tom1', 0.8), (14, 'tom2', 0.9), (15, 'tom2', 1.0)]
            if lvl == 0:
                fills = fills[2:]
            for (s, inst, v) in fills:
                ev.append((inst, s, (), 0, v))
        return ev

    def _bass(self, sec, k, chord, last):
        role = sec['role']
        root = self.gbase(chord[0]) - 12
        if last:
            return [('bass', 0, (root,), 16, 1.0)]
        if role == 'intro' and sec['bars'] >= 4 and k < 2:
            return []
        style = self.d['bass']
        if role == 'bridge':
            style = 'long' if self.d['pad'] else 'r4'
        elif role == 'outro':
            style = 'r4'
        ev = []
        for (s, semi, ln) in BASS[style]:
            ev.append(('bass', s, (root + semi,), ln, 1.0 if s == 0 else 0.8))
        return ev

    def _guitar(self, sec, k, b, chord, last):
        role = sec['role']
        off, q = chord
        root = self.gbase(off)
        if last:
            return [('gtr', 0, self.voice(root, 'p'), 16, 1.0)]
        if role == 'intro' or role == 'outro':
            mode = 'riff'
        elif role in ('verse', 'solo'):
            mode = self.d['gm']
        elif role == 'pre':
            mode = 'riff'
        elif role == 'chorus':
            mode = self.d['cm']
        else:
            mode = 'arp'
        ev = []
        if mode == 'riff':
            for (s, ivl, ln, muted) in self.riff_bar(sec, k, b):
                if role == 'pre' and s % 2 == 1:
                    continue
                notes = self.voice(root + ivl, 'p')
                ev.append(('gtrm' if muted else 'gtr', s, notes, max(1, ln), 1.0 if s % 4 == 0 else 0.8))
        elif mode == 'open':
            for (s, ln) in self.open_pat:
                ev.append(('gtr', s, self.voice(root, q if q in 'MmD' else 'p'), ln, 1.0 if s == 0 else 0.85))
        elif mode == 'arp':
            tri = TRIAD.get(q, TRIAD['M'])
            order = (0, 1, 2, 1, 0, 1, 2, 1)
            for i in range(8):
                m = root + 12 + tri[order[i]]
                ev.append(('pluck', i * 2, (m,), 3, 0.9 if i % 4 == 0 else 0.7))
        elif mode == 'trem':
            for s in range(16):
                ev.append(('gtrm', s, self.voice(root + 12, 'p'), 1, 0.95 if s % 4 == 0 else 0.7))
        elif mode == 'skank':
            for s in (0, 3, 6, 10, 12, 14):
                ev.append(('stab', s, self.voice(root, q if q in 'MmD' else 'p'), 1, 0.9))
        else:
            for s in (2, 6, 10, 14):
                ev.append(('stab', s, self.voice(root, 'p'), 1, 0.9))
        return ev

    # -- chart -----------------------------------------------------------------
    def get_chart(self):
        """List of (time, lane, duration, storm) sorted by time."""
        if self._chart is None:
            self._chart = self._build_chart()
        return self._chart

    def step_time(self, b, step):
        st = step
        if step % 4 == 2 and self.swing:
            st = step + self.swing
        return self.lead_time + b * self.bar_sec + st * self.step_samples / float(RATE)

    @staticmethod
    def _pri(step):
        if step == 0:
            return 4
        if step % 8 == 0:
            return 3
        if step % 4 == 0:
            return 2
        if step % 2 == 0:
            return 1
        return 0

    def _lane_from_midi(self, m):
        base = self.lead_base()
        return int(clamp((m - (base - 3)) / 4.0, 0, 4))

    def _lane_from_riff(self, off, ivl):
        lane0 = int((off % 12) * 5 / 12)
        shift = {0: 0, 7: 1, 12: 2, 5: 1, 3: 1, 10: 2, 4: 1, 8: 2, 1: 1, 6: 2, 9: 2}.get(ivl, 0)
        return (lane0 + shift) % 5

    def _raw_events(self, b):
        """Candidate notes for bar b: (step, lane, len, pri, kind)."""
        si, k = self.bar_info[b]
        sec = self.sections[si]
        role = sec['role']
        chord = self.chord_at(sec, k)
        off = chord[0]
        ev = []
        if b == self.total_bars - 1:
            return [(0, self._lane_from_riff(off, 0), 16, 4, 'c')]
        if role in ('chorus', 'solo', 'bridge'):
            if role == 'chorus':
                lane = self._lane_from_riff(off, 0)
                for (s, ln) in self.open_pat:
                    if s % 4 == 0:
                        ev.append((s, lane, ln, 4 if s == 0 else 3, 'c'))
            for (s, m, ln) in self.lead_bar(sec, k, chord):
                ev.append((s, self._lane_from_midi(m), ln, self._pri(s), 'm'))
        else:
            for (s, ivl, ln, muted) in self.riff_bar(sec, k, b):
                if role == 'pre' and s % 2 == 1:
                    continue
                ev.append((s, self._lane_from_riff(off, ivl), ln, self._pri(s), 'r'))
        if k == sec['bars'] - 1 and role != 'outro':
            for j, s in enumerate((12, 13, 14, 15)):
                ev.append((s, 1 + j if j < 4 else 4, 1, self._pri(s), 'f'))
        return ev

    def _build_chart(self):
        rng = random.Random(self.seed * 77 + 5)
        d = float(self.diff)
        role_f = {'intro': 0.55, 'outro': 0.55, 'verse': 0.85, 'bridge': 0.85}
        min_gap = 0.100
        picked = []      # (t, lane, len_sec, storm-candidate, bar, kind)
        last_t = -1.0
        for b in range(self.total_bars):
            si, k = self.bar_info[b]
            role = self.sections[si]['role']
            rf = role_f.get(role, 1.0)
            raw = sorted(self._raw_events(b), key=lambda e: (e[0], 0 if e[4] == 'c' else 1))
            seen = set()
            for (s, lane, ln, pri, kind) in raw:
                if s in seen:
                    continue
                if kind == 'f':
                    keep = d >= 3 and (s in (14, 15) or d >= 6)
                    if s == 13 and d < 8:
                        keep = False
                elif pri >= 3:
                    keep = rng.random() < (0.93 if rf < 1 else 1.0)
                elif pri == 2:
                    keep = rng.random() < (0.45 + 0.055 * d) * rf
                elif pri == 1:
                    keep = rng.random() < clamp((d - 1.2) / 7.5, 0, 1) * rf
                else:
                    keep = rng.random() < clamp((d - 5.0) / 5.5, 0, 1) * rf
                if not keep:
                    continue
                t = self.step_time(b, s)
                if t - last_t < min_gap and t != last_t:
                    continue
                seen.add(s)
                picked.append([t, lane, ln * self.step_samples / float(RATE), b, kind, s])
                last_t = t
        # chords, sustains, storm phrases ----------------------------------------
        notes = []
        n = len(picked)
        bar_last_idx = {}
        for i, p in enumerate(picked):
            t, lane, lsec, b, kind, s = p
            nxt = picked[i + 1][0] if i + 1 < n else t + 99.0
            dur = 0.0
            if lsec >= 0.42 and kind in ('c', 'm') and rng.random() < 0.6:
                room = nxt - t - 0.13
                dur = min(lsec - 0.06, room)
                if dur < 0.30:
                    dur = 0.0
            lanes = [lane]
            pri = self._pri(s)
            if kind != 'f' and dur == 0.0:
                pc = clamp((d - 2.5) / 8.0, 0, 0.85)
                if kind == 'c':
                    pc = min(1.0, pc + 0.25)
                if pri >= 2 and rng.random() < pc:
                    other = lane + rng.choice((1, 1, 2)) if lane < 4 else lane - rng.choice((1, 1, 2))
                    lanes.append(int(clamp(other, 0, 4)))
                    if lanes[1] == lanes[0]:
                        lanes.pop()
                    elif d >= 8 and pri == 4 and rng.random() < 0.3:
                        third = lanes[1] + (1 if lanes[1] < 4 else -1)
                        if third not in lanes and 0 <= third <= 4:
                            lanes.append(third)
            if d < 3:
                lanes = lanes[:1]
            for ln_ in lanes:
                notes.append([t, ln_, dur if ln_ == lane else 0.0, 0, b, s])
            bar_last_idx.setdefault(b, []).append(t)
        # storm phrases: last four onsets of every fourth bar
        storm_times = set()
        for b, times in bar_last_idx.items():
            si, k = self.bar_info[b]
            if b % 4 == 3 and self.sections[si]['role'] != 'outro' and b > 4:
                for t in sorted(set(times))[-4:]:
                    storm_times.add(round(t, 4))
        out = []
        for t, lane, dur, _s, b, s in notes:
            out.append((t, lane, dur, 1 if round(t, 4) in storm_times else 0))
        out.sort(key=lambda x: (x[0], x[1]))
        return out


class SongLibrary(object):
    songs = [Song(i, d) for i, d in enumerate(SONG_DEFS)]


# ============================================================================
# 6. AUDIO SYNTHESIS
# ============================================================================
_TWO_PI = 2.0 * math.pi
_ENV_CACHE = {}


def _env(n, tau, atk=0.004, rel=0.02):
    """Amplitude envelope: linear attack, exponential decay, linear release."""
    key = (n, round(tau, 4), atk, rel)
    e = _ENV_CACHE.get(key)
    if e is not None:
        return e
    a = max(1, int(atk * RATE))
    r = max(1, min(int(rel * RATE), n // 2))
    dec = math.exp(-1.0 / (max(tau, 1e-3) * RATE))
    v = 1.0
    e = []
    for i in range(n):
        if i < a:
            g = (i + 1) / float(a)
        else:
            v *= dec
            g = v
        if i >= n - r:
            g *= (n - i) / float(r)
        e.append(g)
    _ENV_CACHE[key] = e
    return e


def _to_bytes(vals):
    return array('h', vals).tobytes()


def mix_into(dst, src, off):
    """Add 16-bit mono PCM `src` (bytes) into bytearray `dst` at sample `off`."""
    n = len(src) // 2
    total = len(dst) // 2
    if off < 0:
        return
    if off + n > total:
        n = total - off
        src = src[:2 * n]
    if n <= 0:
        return
    a = off * 2
    b = a + 2 * n
    if audioop is not None:
        dst[a:b] = audioop.add(bytes(dst[a:b]), src, 2)
        return
    mv = memoryview(dst).cast('h')
    sv = memoryview(src).cast('h')
    for i in range(n):
        v = mv[off + i] + sv[i]
        mv[off + i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    mv.release()
    sv.release()


class Synth(object):
    """Tiny software synthesiser.  Notes and drum hits are rendered once and
    cached so a whole song only synthesises its unique sounds."""

    AMP = {'gtr': 0.17, 'gtrm': 0.17, 'stab': 0.15, 'glead': 0.16,
           'syn': 0.15, 'bass': 0.22, 'pad': 0.10, 'pluck': 0.16}

    def __init__(self, drive=3.0):
        self.drive = drive
        self.note_cache = {}
        self.drum_cache = {}
        self.rng = random.Random(12345)

    # -- pitched instruments ---------------------------------------------------
    def note(self, inst, notes, n, vel):
        key = (inst, notes, n, vel)
        r = self.note_cache.get(key)
        if r is None:
            r = self._render_note(inst, notes, n, vel)
            self.note_cache[key] = r
        return r

    def _render_note(self, inst, notes, n, vel):
        amp = 32767.0 * vel * self.AMP.get(inst, 0.15)
        fs = [midi_hz(m) / RATE for m in notes]
        while len(fs) < 3:
            fs.append(fs[-1] * 1.004)
        f1, f2, f3 = fs[0], fs[1], fs[2]
        tanh = math.tanh
        sin = math.sin
        if inst in ('gtr', 'gtrm', 'stab'):
            tau = {'gtr': 1.4, 'gtrm': 0.075, 'stab': 0.10}[inst]
            drv = self.drive if inst != 'stab' else max(1.0, self.drive * 0.45)
            env = _env(n, tau)
            vals = [int(amp * e * tanh(drv * (((i * f1) % 1.0 + (i * f2) % 1.0 + (i * f3) % 1.0) * 0.6667 - 1.0)))
                    for i, e in enumerate(env)]
        elif inst == 'syn':
            env = _env(n, 0.9, atk=0.008)
            w = _TWO_PI * 5.5 / RATE
            f = f1
            vals = [int(amp * e * (0.55 * (1.0 if (i * f + 0.004 * sin(i * w)) % 1.0 < 0.5 else -1.0)
                                   + 0.45 * (2.0 * ((i * f * 1.006) % 1.0) - 1.0)))
                    for i, e in enumerate(env)]
        elif inst == 'bass':
            env = _env(n, 0.35, atk=0.006)
            w = _TWO_PI * f1
            vals = [int(amp * e * tanh(1.6 * (sin(w * i) + 0.42 * sin(2.0 * w * i))))
                    for i, e in enumerate(env)]
        elif inst == 'pad':
            env = _env(n, 3.0, atk=0.06, rel=0.05)
            w1, w2, w3 = _TWO_PI * f1, _TWO_PI * f2, _TWO_PI * f3
            vals = [int(amp * e * 0.34 * (sin(w1 * i) + sin(w2 * i * 1.002) + sin(w3 * i * 0.998)))
                    for i, e in enumerate(env)]
        elif inst == 'pluck':
            env = _env(n, 0.22, atk=0.002)
            w = _TWO_PI * f1
            vals = [int(amp * e * (sin(w * i) + 0.5 * sin(2 * w * i) + 0.25 * sin(3 * w * i)))
                    for i, e in enumerate(env)]
        else:                                   # lead guitar (distorted saw + vibrato)
            env = _env(n, 0.8, atk=0.006)
            w = _TWO_PI * 5.2 / RATE
            f = f1
            vals = [int(amp * e * tanh(3.0 * (2.0 * ((i * f + 0.006 * sin(i * w)) % 1.0) - 1.0)))
                    for i, e in enumerate(env)]
        return _to_bytes(vals)

    # -- drums ---------------------------------------------------------------------
    def drum(self, inst, vel):
        vq = 0.5 if vel < 0.6 else (0.75 if vel < 0.9 else 1.0)
        key = (inst, vq)
        r = self.drum_cache.get(key)
        if r is None:
            r = self._render_drum(inst, vq)
            self.drum_cache[key] = r
        return r

    def _render_drum(self, inst, v):
        rnd = self.rng.random
        exp = math.exp
        sin = math.sin
        if inst == 'kick':
            n = int(0.26 * RATE)
            vals = []
            for i in range(n):
                t = i / float(RATE)
                ph = _TWO_PI * (48.0 * t + 95.0 * 0.045 * (1.0 - exp(-t / 0.045)))
                vals.append(int(32767 * 0.42 * v * sin(ph) * exp(-t / 0.10)))
        elif inst == 'snare':
            n = int(0.20 * RATE)
            vals = []
            for i in range(n):
                t = i / float(RATE)
                x = 0.7 * (rnd() * 2 - 1) + 0.5 * sin(_TWO_PI * 190.0 * t)
                vals.append(int(32767 * 0.27 * v * x * exp(-t / 0.055)))
        elif inst in ('hat', 'ohat'):
            n = int((0.05 if inst == 'hat' else 0.24) * RATE)
            tau = 0.013 if inst == 'hat' else 0.075
            vals = []
            prev = 0.0
            for i in range(n):
                t = i / float(RATE)
                x = rnd() * 2 - 1
                vals.append(int(32767 * 0.10 * v * (x - prev) * exp(-t / tau)))
                prev = x
        elif inst == 'crash':
            n = int(1.0 * RATE)
            vals = []
            prev = 0.0
            for i in range(n):
                t = i / float(RATE)
                x = rnd() * 2 - 1
                vals.append(int(32767 * 0.12 * v * (x - 0.6 * prev) * exp(-t / 0.33)))
                prev = x
        else:                                   # tom1 / tom2
            f0 = 170.0 if inst == 'tom1' else 115.0
            n = int(0.28 * RATE)
            vals = []
            for i in range(n):
                t = i / float(RATE)
                ph = _TWO_PI * (f0 * t - 30.0 * 0.06 * (1.0 - exp(-t / 0.06)))
                vals.append(int(32767 * 0.32 * v * sin(ph) * exp(-t / 0.11)))
        return _to_bytes(vals)

    # -- bars ------------------------------------------------------------------------
    def render_bar(self, events, song):
        buf = bytearray(song.bar_samples * 2)
        ss = song.step_samples
        for (inst, step, notes, ln, vel) in events:
            off = step * ss
            if step % 4 == 2 and song.swing:
                off += int(song.swing * ss)
            if off >= song.bar_samples:
                continue
            if not notes:
                mix_into(buf, self.drum(inst, vel), off)
            else:
                n = int(ln * ss * 0.96)
                n = min(n, song.bar_samples - off)
                if n < 48:
                    continue
                vq = 0.5 if vel < 0.6 else (0.75 if vel < 0.9 else 1.0)
                mix_into(buf, self.note(inst, notes, n, vq), off)
        return bytes(buf)


def wav_header(nbytes, rate=RATE):
    return (b'RIFF' + struct.pack('<I', 36 + nbytes) + b'WAVE' + b'fmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', nbytes))


WAV_HEADER_SIZE = 44


class AudioGenerator(object):
    """Renders a full song to a WAV file in the cache folder."""

    @staticmethod
    def path_for(cache_dir, song):
        return os.path.join(cache_dir, 'song%02d_v%d_r%d.wav' % (song.idx + 1, GEN_VERSION, RATE))

    @staticmethod
    def is_valid(path, song):
        try:
            if os.path.getsize(path) != WAV_HEADER_SIZE + song.total_samples * 2:
                return False
            with open(path, 'rb') as f:
                head = f.read(12)
            return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
        except Exception:
            return False

    @staticmethod
    def render(song, path, progress=None, cancel=None):
        """Synthesise `song` into `path`.  Returns True on success."""
        synth = Synth(song.d['drive'])
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        # remove stale versions of this song
        try:
            prefix = 'song%02d_' % (song.idx + 1)
            for fn in os.listdir(folder):
                if fn.startswith(prefix) and os.path.join(folder, fn) != path:
                    os.remove(os.path.join(folder, fn))
        except Exception:
            pass
        total = song.count_in + song.total_bars
        bars = {}
        parts = []
        for i in range(total):
            if cancel is not None and cancel.is_set():
                return False
            if i < song.count_in:
                events = song.count_in_events(i)
            else:
                events = song.arrange_bar(i - song.count_in)
            key = tuple(events)
            buf = bars.get(key)
            if buf is None:
                buf = synth.render_bar(events, song)
                bars[key] = buf
            parts.append(buf)
            if progress is not None:
                progress((i + 1) / float(total))
            time.sleep(0)
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(wav_header(song.total_samples * 2))
            for p in parts:
                f.write(p)
        os.replace(tmp, path)
        _ENV_CACHE.clear()
        return True


class SfxGenerator(object):
    """Small generated sound effects."""
    NAMES = ('click', 'hit', 'miss', 'storm', 'results')

    @staticmethod
    def _tone(freq_fn, dur, amp, tau, wave='sin', noise=0.0):
        n = int(dur * RATE)
        rnd = random.Random(7).random
        vals = []
        ph = 0.0
        for i in range(n):
            t = i / float(RATE)
            ph += freq_fn(t) / RATE
            if wave == 'sin':
                x = math.sin(_TWO_PI * ph)
            elif wave == 'tri':
                x = 4.0 * abs((ph % 1.0) - 0.5) - 1.0
            else:
                x = 1.0 if (ph % 1.0) < 0.5 else -1.0
            if noise:
                x = x * (1 - noise) + (rnd() * 2 - 1) * noise
            g = min(1.0, i / 60.0) * math.exp(-t / tau)
            if i > n - 80:
                g *= (n - i) / 80.0
            vals.append(int(32767 * amp * g * x))
        return vals

    @staticmethod
    def build(folder):
        os.makedirs(folder, exist_ok=True)
        T = SfxGenerator._tone
        makers = {
            'click': lambda: T(lambda t: 1300.0 - 3000.0 * t, 0.05, 0.5, 0.02, 'tri'),
            'hit': lambda: T(lambda t: 720.0, 0.06, 0.28, 0.025, 'tri'),
            'miss': lambda: T(lambda t: 140.0 - 200.0 * t, 0.16, 0.6, 0.06, 'sin', 0.45),
            'storm': lambda: T(lambda t: 220.0 * (2.0 ** (t * 3.2)), 0.75, 0.45, 0.5, 'sq', 0.08),
            'results': lambda: (T(lambda t: 523.25, 0.16, 0.4, 0.12, 'tri') +
                                T(lambda t: 659.25, 0.16, 0.4, 0.12, 'tri') +
                                T(lambda t: 783.99, 0.16, 0.4, 0.12, 'tri') +
                                T(lambda t: 1046.5, 0.6, 0.4, 0.3, 'tri')),
        }
        paths = {}
        for name in SfxGenerator.NAMES:
            p = os.path.join(folder, 'sfx_%s_v%d.wav' % (name, GEN_VERSION))
            ok = False
            try:
                ok = os.path.getsize(p) > 200
            except Exception:
                ok = False
            if not ok:
                try:
                    data = _to_bytes(makers[name]())
                    tmp = p + '.tmp'
                    with open(tmp, 'wb') as f:
                        f.write(wav_header(len(data)))
                        f.write(data)
                    os.replace(tmp, p)
                except Exception:
                    log('sfx failed', name, traceback.format_exc())
                    continue
            paths[name] = p
        return paths


# ============================================================================
# 7. GAME RULES ENGINE (no Kivy dependency; unit-testable)
# ============================================================================
class Note(object):
    __slots__ = ('t', 'lane', 'dur', 'storm', 'state', 'held', 'tick')

    def __init__(self, t, lane, dur, storm):
        self.t = t
        self.lane = lane
        self.dur = dur
        self.storm = storm
        self.state = 0          # 0 pending, 1 hit, 2 missed
        self.held = False
        self.tick = 0.0


class GameEngine(object):
    """Scoring, combo, multiplier, health, storm power and hit judgement."""

    def __init__(self):
        self.events = []
        self.reset([], True)

    def reset(self, chart, fail_enabled):
        self.notes = [Note(t, l, d, s) for (t, l, d, s) in chart]
        self.lane_notes = [[] for _ in range(LANES)]
        for n in self.notes:
            self.lane_notes[n.lane].append(n)
        self.lane_ptr = [0] * LANES
        self.holding = [None] * LANES
        self.down = [False] * LANES
        self.total = len(self.notes)
        self.score = 0
        self.combo = 0
        self.best_combo = 0
        self.mult = 1
        self.hits = 0
        self.misses = 0
        self.grades = [0, 0, 0]
        self.health = 0.5
        self.storm = 0.0
        self.storm_active = False
        self.fail_enabled = fail_enabled
        self.failed = False
        self.events = []

    # -- helpers ------------------------------------------------------------------
    @property
    def resolved(self):
        return self.hits + self.misses

    def accuracy(self, over_total=False):
        denom = self.total if over_total else self.resolved
        if denom <= 0:
            return 100.0 if not over_total else 0.0
        acc = sum(self.grades[i] * GRADE_ACC[i] for i in range(3))
        return 100.0 * acc / denom

    def stars(self):
        a = self.accuracy(True)
        if a >= 95:
            return 5
        if a >= 88:
            return 4
        if a >= 78:
            return 3
        if a >= 62:
            return 2
        return 1

    def _add_score(self, base):
        self.score += base * self.mult * (2 if self.storm_active else 1)

    # -- input ----------------------------------------------------------------------
    def set_down(self, lane, down):
        self.down[lane] = down

    def press(self, lane, t):
        if self.failed:
            return
        lst = self.lane_notes[lane]
        p = self.lane_ptr[lane]
        if p >= len(lst):
            return
        n = lst[p]
        dt = t - n.t
        if dt < -WIN_GOOD or dt > WIN_GOOD:
            return
        a = abs(dt)
        grade = 0 if a <= WIN_PERFECT else (1 if a <= WIN_GREAT else 2)
        n.state = 1
        self.lane_ptr[lane] = p + 1
        self.hits += 1
        self.grades[grade] += 1
        self.combo += 1
        if self.combo > self.best_combo:
            self.best_combo = self.combo
        self.mult = min(4, 1 + self.combo // 10)
        self._add_score(GRADE_POINTS[grade])
        self.health = min(1.0, self.health + GRADE_HEALTH[grade])
        if n.storm:
            self.storm = min(1.0, self.storm + STORM_CHARGE_PER_NOTE)
        if n.dur > 0:
            n.held = True
            n.tick = t + SUSTAIN_TICK
            self.holding[lane] = n
        self.events.append(('hit', lane, grade, n.storm, self.combo))

    def release(self, lane, t):
        pass  # sustain release is evaluated in update() from down[]

    def storm_ready(self):
        return (not self.storm_active) and self.storm >= STORM_MIN_TO_ACTIVATE

    def activate_storm(self):
        if self.storm_ready():
            self.storm_active = True
            self.events.append(('storm_on', 0, 0, 0, 0))
            return True
        return False

    # -- frame update ----------------------------------------------------------------
    def update(self, t, dt):
        for lane in range(LANES):
            lst = self.lane_notes[lane]
            p = self.lane_ptr[lane]
            m = len(lst)
            while p < m and lst[p].t + WIN_GOOD < t:
                lst[p].state = 2
                p += 1
                self.misses += 1
                self.combo = 0
                self.mult = 1
                self.health = max(0.0, self.health - MISS_HEALTH)
                self.events.append(('miss', lane, 0, 0, 0))
            self.lane_ptr[lane] = p
            n = self.holding[lane]
            if n is not None:
                end = n.t + n.dur
                if t >= end:
                    n.held = False
                    self.holding[lane] = None
                elif not self.down[lane]:
                    n.held = False
                    self.holding[lane] = None
                else:
                    while t >= n.tick and n.tick < end:
                        self._add_score(3)
                        n.tick += SUSTAIN_TICK
        if self.storm_active:
            self.storm -= dt / STORM_FULL_SECONDS
            if self.storm <= 0.0:
                self.storm = 0.0
                self.storm_active = False
                self.events.append(('storm_off', 0, 0, 0, 0))
        if self.fail_enabled and self.health <= 0.0 and not self.failed:
            self.failed = True
            self.events.append(('fail', 0, 0, 0, 0))

    def all_resolved(self):
        return self.resolved >= self.total


class SongClock(object):
    """Wall clock that is gently steered towards the audio position."""

    def __init__(self):
        self.pos = 0.0
        self.wall = None
        self._last_audio = None

    def start(self, pos):
        self.pos = pos
        self.wall = time.perf_counter()
        self._last_audio = None

    def stop(self):
        if self.wall is not None:
            self.pos = self.now()
            self.wall = None

    @property
    def running(self):
        return self.wall is not None

    def now(self):
        if self.wall is None:
            return self.pos
        return self.pos + (time.perf_counter() - self.wall)

    def sync(self, audio_pos):
        """Audio position is the authority; only trust fresh, plausible values."""
        if self.wall is None or audio_pos is None or audio_pos <= 0.02:
            return
        if self._last_audio is not None and audio_pos == self._last_audio:
            return
        self._last_audio = audio_pos
        err = audio_pos - self.now()
        if abs(err) > 0.30:
            self.pos += err
        else:
            self.pos += err * 0.06


# ============================================================================
# 8. AUDIO PLAYBACK AND VIBRATION
# ============================================================================
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.core.audio import SoundLoader
from kivy.graphics import (Color, Rectangle, RoundedRectangle, Ellipse, Line,
                           Quad, Triangle, Mesh)
from kivy.graphics.texture import Texture
from kivy.metrics import dp
from kivy.uix.widget import Widget
from kivy.uix.label import Label
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition


class AudioEngine(object):
    """Music + SFX playback.  Every call is wrapped so that a missing or
    failing audio backend can never crash the game."""

    def __init__(self, app):
        self.app = app
        self.cache_dir = os.path.join(app.user_data_dir, 'cache')
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception:
            log('cache dir failed', traceback.format_exc())
        self.sfx = {}
        self.sfx_i = {}
        self.sfx_last = {}
        self.full_path = None
        self.cur = None
        self.offset = 0.0
        self.tail_n = 0

    # -- volume ------------------------------------------------------------------
    def music_volume(self):
        s = self.app.settings
        return clamp(s['master'] * s['music'], 0.0, 1.0)

    def sfx_volume(self):
        s = self.app.settings
        return clamp(s['master'] * s['sfx'], 0.0, 1.0)

    def apply_volumes(self):
        try:
            if self.cur is not None:
                self.cur.volume = self.music_volume()
        except Exception:
            pass

    # -- sfx -------------------------------------------------------------------------
    def load_sfx(self, paths):
        for name, p in paths.items():
            pool = []
            for _ in range(2 if name in ('hit', 'click') else 1):
                try:
                    snd = SoundLoader.load(p)
                    if snd is not None:
                        pool.append(snd)
                except Exception:
                    pass
            if pool:
                self.sfx[name] = pool
                self.sfx_i[name] = 0

    def play_sfx(self, name, scale=1.0, min_gap=0.0):
        pool = self.sfx.get(name)
        if not pool:
            return
        now = time.time()
        if now - self.sfx_last.get(name, 0.0) < min_gap:
            return
        self.sfx_last[name] = now
        try:
            i = self.sfx_i[name] = (self.sfx_i[name] + 1) % len(pool)
            snd = pool[i]
            snd.volume = clamp(self.sfx_volume() * scale, 0.0, 1.0)
            if snd.state == 'play':
                snd.stop()
            snd.play()
        except Exception:
            pass

    # -- music -----------------------------------------------------------------------
    def load_music(self, path):
        self.stop_music()
        self.full_path = path if (path and os.path.exists(path)) else None
        return self.full_path is not None

    def _make_tail(self, start):
        """Write the part of the song after `start` seconds to a temp WAV so
        resuming never depends on the backend supporting seek()."""
        s0 = max(0, int(start * RATE))
        with open(self.full_path, 'rb') as f:
            f.seek(WAV_HEADER_SIZE + s0 * 2)
            data = f.read()
        self.tail_n = 1 - self.tail_n
        p = os.path.join(self.cache_dir, 'resume_%d.wav' % self.tail_n)
        with open(p, 'wb') as f:
            f.write(wav_header(len(data)))
            f.write(data)
        return p, s0 / float(RATE)

    def play_music(self, start=0.0):
        self.stop_music()
        if not self.full_path:
            return False
        try:
            if start > 0.05:
                path, self.offset = self._make_tail(start)
            else:
                path, self.offset = self.full_path, 0.0
            snd = SoundLoader.load(path)
            if snd is None:
                return False
            snd.volume = self.music_volume()
            snd.play()
            self.cur = snd
            return True
        except Exception:
            log('play_music failed', traceback.format_exc())
            self.cur = None
            return False

    def music_pos(self):
        snd = self.cur
        if snd is None:
            return None
        try:
            p = snd.get_pos()
            if p is None or p <= 0.0:
                return None
            return p + self.offset
        except Exception:
            return None

    def stop_music(self):
        snd, self.cur = self.cur, None
        if snd is not None:
            try:
                snd.stop()
            except Exception:
                pass
            try:
                snd.unload()
            except Exception:
                pass


class Vibrator(object):
    """Android vibration through pyjnius; silently disabled if unavailable."""

    def __init__(self):
        self.ok = False
        self.v = None
        self.last = 0.0
        if platform == 'android':
            try:
                from jnius import autoclass
                activity = autoclass('org.kivy.android.PythonActivity').mActivity
                ctx = autoclass('android.content.Context')
                self.v = activity.getSystemService(ctx.VIBRATOR_SERVICE)
                self.ok = bool(self.v is not None and self.v.hasVibrator())
            except Exception:
                self.ok = False

    def pulse(self, ms=12):
        if not self.ok:
            return
        now = time.time()
        if now - self.last < 0.05:
            return
        self.last = now
        try:
            self.v.vibrate(int(ms))
        except Exception:
            self.ok = False


# ============================================================================
# 9. UI HELPERS AND WIDGETS
# ============================================================================
WHITE = (1, 1, 1, 1)
COL_PURPLE = (0.62, 0.25, 1.0)
COL_CYAN = (0.10, 0.85, 1.0)
COL_PINK = (1.0, 0.22, 0.62)
COL_GOLD = (1.0, 0.82, 0.20)
_GRAD = {}


def gradient_texture(name='bg', stops=None):
    tex = _GRAD.get(name)
    if tex is not None:
        return tex
    stops = stops or [(0.17, 0.03, 0.27), (0.08, 0.03, 0.18), (0.025, 0.02, 0.08), (0.01, 0.01, 0.03)]
    n = 64
    buf = bytearray()
    for i in range(n):
        f = i / float(n - 1) * (len(stops) - 1)
        a = int(f)
        b = min(a + 1, len(stops) - 1)
        u = f - a
        for c in range(3):
            buf.append(int(255 * lerp(stops[a][c], stops[b][c], u)))
    tex = Texture.create(size=(1, n), colorfmt='rgb')
    tex.blit_buffer(bytes(buf), colorfmt='rgb', bufferfmt='ubyte')
    tex.mag_filter = 'linear'
    tex.min_filter = 'linear'
    _GRAD[name] = tex
    return tex


class FLabel(Label):
    """Label whose font size is a fraction of the window height."""

    def __init__(self, frac=0.05, wrap=False, **kw):
        kw.setdefault('color', WHITE)
        kw.setdefault('halign', 'center')
        kw.setdefault('valign', 'middle')
        super(FLabel, self).__init__(**kw)
        self._frac = frac
        self._wrap = wrap
        self.bind(size=self._on_size)
        Window.bind(height=self._on_h)
        self._on_h()
        self._on_size()

    def _on_h(self, *a):
        self.font_size = max(8.0, self._frac * Window.height)

    def _on_size(self, *a):
        self.text_size = (self.width, None if self._wrap else self.height)


class NeonButton(ButtonBehavior, Label):
    """Rounded neon button drawn with canvas primitives."""

    def __init__(self, text='', rgb=COL_PURPLE, frac=0.05, sound=True, **kw):
        kw.setdefault('color', WHITE)
        kw.setdefault('bold', True)
        super(NeonButton, self).__init__(text=text, **kw)
        self.rgb = rgb
        self.sound = sound
        self._frac = frac
        with self.canvas.before:
            self._glow_c = Color(rgb[0], rgb[1], rgb[2], 0.22)
            self._glow = RoundedRectangle(pos=self.pos, size=self.size, radius=[10])
            self._bg_c = Color(0.06, 0.04, 0.13, 0.94)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[10])
            self._ln_c = Color(rgb[0], rgb[1], rgb[2], 1)
            self._ln = Line(rounded_rectangle=(0, 0, 10, 10, 5), width=1.5)
        self.bind(pos=self._redraw, size=self._redraw, state=self._redraw)
        Window.bind(height=self._on_h)
        self._on_h()
        self._redraw()

    def _on_h(self, *a):
        self.font_size = max(8.0, self._frac * Window.height)

    def set_rgb(self, rgb):
        self.rgb = rgb
        self._glow_c.rgb = rgb
        self._ln_c.rgb = rgb
        self._redraw()

    def _redraw(self, *a):
        x, y, w, h = self.x, self.y, self.width, self.height
        if w < 4 or h < 4:
            return
        r = max(3.0, min(h * 0.28, 26.0))
        pad = max(2.0, h * 0.05)
        self._glow.pos = (x - pad, y - pad)
        self._glow.size = (w + 2 * pad, h + 2 * pad)
        self._glow.radius = [r + pad]
        self._bg.pos = (x, y)
        self._bg.size = (w, h)
        self._bg.radius = [r]
        self._ln.rounded_rectangle = (x, y, w, h, r)
        if self.state == 'down':
            c = self.rgb
            self._bg_c.rgba = (c[0] * 0.55, c[1] * 0.55, c[2] * 0.55, 0.98)
        else:
            self._bg_c.rgba = (0.06, 0.04, 0.13, 0.94)

    def on_press(self):
        if self.sound:
            app = App.get_running_app()
            if app is not None and getattr(app, 'audio', None) is not None:
                app.audio.play_sfx('click')


class ToggleButton2(NeonButton):
    """ON / OFF switch button."""

    def __init__(self, value=False, callback=None, **kw):
        super(ToggleButton2, self).__init__(**kw)
        self.value = value
        self.callback = callback
        self._show()
        self.bind(on_release=self._flip)

    def _show(self):
        self.text = 'ON' if self.value else 'OFF'
        self.set_rgb((0.15, 0.95, 0.45) if self.value else (0.85, 0.25, 0.35))

    def set_value(self, v):
        self.value = bool(v)
        self._show()

    def _flip(self, *a):
        self.set_value(not self.value)
        if self.callback:
            self.callback(self.value)


class StarRow(Widget):
    """Five stars drawn with Mesh triangle fans."""

    def __init__(self, stars=0, **kw):
        super(StarRow, self).__init__(**kw)
        self.stars = stars
        self._trig = Clock.create_trigger(self._draw, -1)
        self.bind(pos=self._retrigger, size=self._retrigger)
        self._draw()

    def _retrigger(self, *a):
        self._trig()

    def set_stars(self, n):
        self.stars = int(n)
        self._draw()

    @staticmethod
    def _star(cx, cy, r):
        pts = [cx, cy, 0, 0]
        for i in range(11):
            ang = math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.42
            pts += [cx + math.cos(ang) * rr, cy + math.sin(ang) * rr, 0, 0]
        return pts

    def _draw(self, *a):
        self.canvas.clear()
        if self.width < 5 or self.height < 5:
            return
        size = min(self.height, self.width / 5.0)
        x0 = self.center_x - size * 2.5
        with self.canvas:
            for i in range(5):
                on = i < self.stars
                if on:
                    Color(1.0, 0.85, 0.15, 1)
                else:
                    Color(0.25, 0.22, 0.32, 1)
                verts = self._star(x0 + size * (i + 0.5), self.center_y, size * 0.46)
                Mesh(vertices=verts, indices=list(range(12)), mode='triangle_fan')


class DiffBars(Widget):
    """Ten little bars showing song difficulty."""

    def __init__(self, level=1, **kw):
        super(DiffBars, self).__init__(**kw)
        self.level = level
        self._trig = Clock.create_trigger(self._draw, -1)
        self.bind(pos=self._retrigger, size=self._retrigger)
        self._draw()

    def _retrigger(self, *a):
        self._trig()

    def _draw(self, *a):
        self.canvas.clear()
        if self.width < 5 or self.height < 5:
            return
        w = self.width / 10.0
        with self.canvas:
            for i in range(10):
                if i < self.level:
                    f = i / 9.0
                    Color(lerp(0.2, 1.0, f), lerp(0.95, 0.2, f), 0.3, 1)
                else:
                    Color(0.2, 0.18, 0.28, 1)
                hh = self.height * (0.35 + 0.065 * i)
                Rectangle(pos=(self.x + i * w + w * 0.12, self.y), size=(w * 0.76, hh))


class Backdrop(Widget):
    """Dark neon stage background with drifting light beams."""

    BEAM_COLORS = [(0.6, 0.2, 1.0), (0.15, 0.55, 1.0), (1.0, 0.2, 0.65), (0.1, 0.9, 1.0), (0.7, 0.3, 1.0)]

    def __init__(self, **kw):
        super(Backdrop, self).__init__(**kw)
        self.t = 0.0
        with self.canvas:
            Color(1, 1, 1, 1)
            try:
                self.bg = Rectangle(texture=gradient_texture(), pos=self.pos, size=self.size)
            except Exception:
                self.bg = Rectangle(pos=self.pos, size=self.size)
            self.beam_c = []
            self.beams = []
            for i in range(5):
                c = self.BEAM_COLORS[i]
                self.beam_c.append(Color(c[0], c[1], c[2], 0.07))
                self.beams.append(Triangle(points=[0, 0, 0, 0, 0, 0]))
        self.bind(pos=self._layout, size=self._layout)
        self._layout()

    def _layout(self, *a):
        self.bg.pos = self.pos
        self.bg.size = self.size
        self.tick(0.0)

    def tick(self, dt):
        self.t += dt
        W, H = self.width, self.height
        for i in range(5):
            ang = self.t * (0.35 + 0.07 * i) + i * 1.3
            ax = self.x + W * (0.1 + 0.2 * i)
            ay = self.y + H * 1.02
            bx = ax + math.sin(ang) * W * 0.32
            wdt = W * 0.07
            self.beams[i].points = [ax, ay, bx - wdt, self.y + H * 0.05, bx + wdt, self.y + H * 0.05]
            self.beam_c[i].a = 0.05 + 0.035 * (1 + math.sin(self.t * 1.7 + i))


class PopLabel(FLabel):
    """Label that shows a message and fades out by itself."""

    def __init__(self, **kw):
        super(PopLabel, self).__init__(**kw)
        self.opacity = 0.0
        self.life = 0.0

    def pop(self, text, rgb=(1, 1, 1), life=0.9):
        self.text = text
        self.color = (rgb[0], rgb[1], rgb[2], 1)
        self.life = life
        self.opacity = 1.0

    def tick(self, dt):
        if self.life > 0:
            self.life -= dt
            self.opacity = clamp(self.life * 3.0, 0.0, 1.0)
        elif self.opacity > 0:
            self.opacity = 0.0


# ============================================================================
# 10. GAMEPLAY: RENDERER (GameView) AND GameplayScreen
# ============================================================================
N_GEMS = 80
N_SUST = 28
N_BEATS = 26
N_PART = 96


class GameView(Widget):
    """Pseudo-3D note highway.  All canvas instructions are created once and
    reused every frame; touch input is mapped to lanes here."""

    K = 2.0            # perspective strength

    def __init__(self, screen, **kw):
        super(GameView, self).__init__(**kw)
        self.screen = screen
        self.touch_lane = {}
        self.lane_count = [0] * LANES
        self.press_glow = [0.0] * LANES
        self.hit_flash = [0.0] * LANES
        self.miss_flash = 0.0
        self.storm_flash = 0.0
        self.pulse = 0.0
        self.storm_vis = 0.0
        self.gems_used = 0
        self.sust_used = 0
        self.beats_used = 0
        self.part_active = 0
        self.part_i = 0
        self.px = [0.0] * N_PART
        self.py = [0.0] * N_PART
        self.pvx = [0.0] * N_PART
        self.pvy = [0.0] * N_PART
        self.plife = [0.0] * N_PART
        self.pmax = [1.0] * N_PART
        self.prng = random.Random(3)
        self.geom_ok = False
        self._compute_geometry()
        self._build()
        self.bind(size=self._on_layout, pos=self._on_layout)
        self._on_layout()

    # -- geometry -------------------------------------------------------------------
    def _compute_geometry(self):
        W, H = max(1.0, self.width), max(1.0, self.height)
        self.cx = self.x + W * 0.5
        self.hwb = W * 0.375
        self.lw = 2.0 * self.hwb / LANES
        self.y_hit = self.y + H * 0.30
        self.y_top = self.y + H * 0.975
        self.fret_h = H * 0.235
        self.base_r = min(self.lw * 0.36, H * 0.078)
        self.s_far = 1.0 / (1.0 + self.K)

    def _y_of(self, s):
        return self.y_hit + (self.y_top - self.y_hit) * (1.0 - s) / (1.0 - self.s_far)

    def _lane_x(self, lane, s):
        return self.cx + (lane - 2) * self.lw * s

    def _lane_at(self, x, y):
        if y > self.y_top:
            return -1
        if y <= self.y_hit:
            s = 1.0
        else:
            f = (y - self.y_hit) / (self.y_top - self.y_hit)
            s = 1.0 - f * (1.0 - self.s_far)
        half = self.hwb * s
        dx = x - self.cx
        if abs(dx) > half:
            return -1
        lane = int((dx + half) / (2.0 * half) * LANES)
        return int(clamp(lane, 0, LANES - 1))

    # -- canvas construction -------------------------------------------------------------
    def _build(self):
        c = self.canvas
        c.clear()
        with c:
            self.c_bg = Color(1, 1, 1, 1)
            try:
                self.r_bg = Rectangle(texture=gradient_texture(), pos=self.pos, size=self.size)
            except Exception:
                self.r_bg = Rectangle(pos=self.pos, size=self.size)
            self.beam_c, self.beams = [], []
            for i in range(4):
                self.beam_c.append(Color(0.6, 0.2, 1.0, 0.06))
                self.beams.append(Triangle(points=[0] * 6))
            self.pulse_c = Color(0.5, 0.2, 1.0, 0.0)
            self.pulse_e = Ellipse(pos=(0, 0), size=(1, 1), segments=32)
            # highway
            self.hw_c = Color(0.03, 0.03, 0.08, 0.93)
            self.hw_quad = Quad(points=[0] * 8)
            self.hw_rect = Rectangle(pos=(0, 0), size=(1, 1))
            self.lane_c, self.lane_q = [], []
            for i in range(LANES):
                col = LANE_COLORS[i]
                self.lane_c.append(Color(col[0], col[1], col[2], 0.07))
                self.lane_q.append(Quad(points=[0] * 8))
            self.storm_c = Color(0.3, 0.9, 1.0, 0.0)
            self.storm_q = Quad(points=[0] * 8)
            self.div_c = Color(0.55, 0.45, 0.9, 0.55)
            self.div_l = [Line(points=[0, 0, 0, 0], width=1.2) for _ in range(LANES + 1)]
            # beat lines
            self.beat_c, self.beat_l = [], []
            for i in range(N_BEATS):
                self.beat_c.append(Color(0.7, 0.6, 1.0, 0.0))
                self.beat_l.append(Line(points=[0, 0, 0, 0], width=1.0))
            # hit line
            self.hitglow_c = Color(0.8, 0.7, 1.0, 0.18)
            self.hitglow = Rectangle(pos=(0, 0), size=(1, 1))
            self.hitline_c = Color(1, 1, 1, 0.95)
            self.hitline = Line(points=[0, 0, 0, 0], width=2.5)
            # sustain trails
            self.sust_c, self.sust_q = [], []
            for i in range(N_SUST):
                self.sust_c.append(Color(1, 1, 1, 0))
                self.sust_q.append(Quad(points=[0] * 8))
            # gems
            self.g_glow_c, self.g_glow, self.g_body_c, self.g_body = [], [], [], []
            self.g_core_c, self.g_core = [], []
            for i in range(N_GEMS):
                self.g_glow_c.append(Color(1, 1, 1, 0))
                self.g_glow.append(Ellipse(pos=(0, 0), size=(0, 0), segments=20))
            for i in range(N_GEMS):
                self.g_body_c.append(Color(1, 1, 1, 0))
                self.g_body.append(Ellipse(pos=(0, 0), size=(0, 0), segments=24))
                self.g_core_c.append(Color(1, 1, 1, 0))
                self.g_core.append(Ellipse(pos=(0, 0), size=(0, 0), segments=18))
            # lane hit flashes
            self.flash_c, self.flash_e = [], []
            for i in range(LANES):
                col = LANE_COLORS[i]
                self.flash_c.append(Color(col[0], col[1], col[2], 0))
                self.flash_e.append(Ellipse(pos=(0, 0), size=(0, 0), segments=24))
            # fret buttons
            self.fret_c, self.fret_r, self.fret_ic, self.fret_i = [], [], [], []
            self.fret_gc, self.fret_g = [], []
            for i in range(LANES):
                col = LANE_COLORS[i]
                self.fret_gc.append(Color(col[0], col[1], col[2], 0))
                self.fret_g.append(Ellipse(pos=(0, 0), size=(0, 0), segments=28))
                self.fret_c.append(Color(col[0] * 0.4, col[1] * 0.4, col[2] * 0.4, 1))
                self.fret_r.append(RoundedRectangle(pos=(0, 0), size=(1, 1), radius=[12]))
                self.fret_ic.append(Color(col[0], col[1], col[2], 0.9))
                self.fret_i.append(Ellipse(pos=(0, 0), size=(1, 1), segments=28))
            # meters
            self.m_bg_c = Color(0.1, 0.09, 0.16, 0.9)
            self.hm_bg = Rectangle(pos=(0, 0), size=(1, 1))
            self.sm_bg = Rectangle(pos=(0, 0), size=(1, 1))
            self.hm_c = Color(0.2, 0.9, 0.4, 1)
            self.hm_fill = Rectangle(pos=(0, 0), size=(1, 1))
            self.sm_c = Color(0.2, 0.9, 1.0, 1)
            self.sm_fill = Rectangle(pos=(0, 0), size=(1, 1))
            self.sm_mark_c = Color(1, 1, 1, 0.8)
            self.sm_mark = Line(points=[0, 0, 0, 0], width=1.5)
            self.pg_bg_c = Color(0.15, 0.12, 0.22, 0.9)
            self.pg_bg = Rectangle(pos=(0, 0), size=(1, 1))
            self.pg_c = Color(0.7, 0.35, 1.0, 1)
            self.pg_fill = Rectangle(pos=(0, 0), size=(1, 1))
            # particles
            self.part_c, self.part_e = [], []
            for i in range(N_PART):
                self.part_c.append(Color(1, 1, 1, 0))
                self.part_e.append(Ellipse(pos=(0, 0), size=(0, 0), segments=10))
            # screen feedback
            self.miss_c = Color(1, 0.1, 0.1, 0)
            self.miss_r = Rectangle(pos=(0, 0), size=(1, 1))
            self.flash_all_c = Color(0.6, 1, 1, 0)
            self.flash_all = Rectangle(pos=(0, 0), size=(1, 1))

    def _on_layout(self, *a):
        self._compute_geometry()
        W, H = self.width, self.height
        if W < 10 or H < 10:
            return
        self.r_bg.pos = self.pos
        self.r_bg.size = self.size
        cx, hwb, lw, yh, yt, sf = self.cx, self.hwb, self.lw, self.y_hit, self.y_top, self.s_far
        self.hw_quad.points = [cx - hwb, yh, cx + hwb, yh, cx + hwb * sf, yt, cx - hwb * sf, yt]
        self.hw_rect.pos = (cx - hwb, self.y)
        self.hw_rect.size = (2 * hwb, yh - self.y)
        for i in range(LANES):
            xl, xr = (i - 2.5) * lw, (i - 1.5) * lw
            pts = [cx + xl, yh, cx + xr, yh, cx + xr * sf, yt, cx + xl * sf, yt]
            self.lane_q[i].points = pts
        self.storm_q.points = [cx - hwb, self.y, cx + hwb, self.y, cx + hwb * sf, yt, cx - hwb * sf, yt]
        for i in range(LANES + 1):
            xb = (i - 2.5) * lw
            self.div_l[i].points = [cx + xb, self.y, cx + xb * sf, yt]
        gh = H * 0.05
        self.hitglow.pos = (cx - hwb, yh - gh / 2)
        self.hitglow.size = (2 * hwb, gh)
        self.hitline.points = [cx - hwb, yh, cx + hwb, yh]
        pad = lw * 0.06
        for i in range(LANES):
            x0 = cx + (i - 2.5) * lw + pad
            w = lw - 2 * pad
            y0 = self.y + H * 0.012
            h = self.fret_h
            self.fret_r[i].pos = (x0, y0)
            self.fret_r[i].size = (w, h)
            self.fret_r[i].radius = [min(w, h) * 0.18]
            d = min(w, h) * 0.56
            self.fret_i[i].pos = (x0 + w / 2 - d / 2, y0 + h / 2 - d * 0.4)
            self.fret_i[i].size = (d, d * 0.8)
        # meters
        mw = W * 0.02
        my0, my1 = self.y + H * 0.36, self.y + H * 0.80
        self.hm_x = self.x + W * 0.062
        self.sm_x = self.x + W * 0.918
        self.m_y0, self.m_h, self.m_w = my0, my1 - my0, mw
        self.hm_bg.pos = (self.hm_x, my0)
        self.hm_bg.size = (mw, my1 - my0)
        self.sm_bg.pos = (self.sm_x, my0)
        self.sm_bg.size = (mw, my1 - my0)
        self.sm_mark.points = [self.sm_x - mw * 0.3, my0 + (my1 - my0) * STORM_MIN_TO_ACTIVATE,
                               self.sm_x + mw * 1.3, my0 + (my1 - my0) * STORM_MIN_TO_ACTIVATE]
        self.pg_bg.pos = (self.x, self.y + H * 0.988)
        self.pg_bg.size = (W, H * 0.012)
        self.miss_r.pos = self.pos
        self.miss_r.size = self.size
        self.flash_all.pos = self.pos
        self.flash_all.size = self.size
        pr = W * 0.5
        self.pulse_e.pos = (cx - pr, yh - pr * 0.35)
        self.pulse_e.size = (2 * pr, pr * 1.3)

    # -- input ---------------------------------------------------------------------------------
    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        if not self.screen.accepting():
            return False
        lane = self._lane_at(touch.x, touch.y)
        if lane < 0:
            return False
        touch.grab(self)
        self.touch_lane[touch.uid] = lane
        self._press(lane)
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is not self or touch.uid not in self.touch_lane:
            return False
        lane = self._lane_at(touch.x, touch.y)
        cur = self.touch_lane[touch.uid]
        if lane >= 0 and lane != cur and self.screen.accepting():
            self._release(cur)
            self.touch_lane[touch.uid] = lane
            self._press(lane)
        return True

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            lane = self.touch_lane.pop(touch.uid, None)
            if lane is not None:
                self._release(lane)
            return True
        return False

    def _press(self, lane):
        self.lane_count[lane] += 1
        self.press_glow[lane] = 1.0
        if self.lane_count[lane] == 1:
            self.screen.engine.set_down(lane, True)
        self.screen.on_lane_press(lane)

    def _release(self, lane):
        if self.lane_count[lane] > 0:
            self.lane_count[lane] -= 1
        if self.lane_count[lane] == 0:
            self.screen.engine.set_down(lane, False)

    def release_all(self):
        self.touch_lane.clear()
        for i in range(LANES):
            self.lane_count[i] = 0
            self.screen.engine.set_down(i, False)

    def reset(self):
        self.release_all()
        self.press_glow = [0.0] * LANES
        self.hit_flash = [0.0] * LANES
        self.miss_flash = 0.0
        self.storm_flash = 0.0
        self.storm_vis = 0.0
        self.plife = [0.0] * N_PART
        self.part_active = 0

    # -- effects ------------------------------------------------------------------------------------
    def on_hit(self, lane, grade, storm, combo, particles):
        self.hit_flash[lane] = 1.0
        if combo and combo % 50 == 0:
            self.pulse = 1.0
        if not particles:
            return
        x = self._lane_x(lane, 1.0)
        y = self.y_hit
        col = LANE_COLORS[lane]
        rnd = self.prng.random
        count = 9 if not storm else 14
        for _ in range(count):
            i = self.part_i = (self.part_i + 1) % N_PART
            ang = rnd() * math.pi
            sp = (0.25 + rnd() * 0.75) * self.height * 0.9
            self.px[i], self.py[i] = x, y
            self.pvx[i] = math.cos(ang) * sp * 0.8
            self.pvy[i] = math.sin(ang) * sp
            self.plife[i] = self.pmax[i] = 0.35 + rnd() * 0.3
            c = self.part_c[i]
            if storm:
                c.rgb = (0.6 + 0.4 * rnd(), 1.0, 1.0)
            else:
                c.rgb = (min(1, col[0] + 0.35), min(1, col[1] + 0.35), min(1, col[2] + 0.35))
        self.part_active = N_PART

    def on_miss(self):
        self.miss_flash = 1.0

    def on_storm(self):
        self.storm_flash = 1.0

    # -- per-frame rendering ------------------------------------------------------------------------------
    def draw(self, t, dt):
        scr = self.screen
        eng = scr.engine
        song = scr.song
        if song is None or self.width < 10:
            return
        travel = scr.travel
        K = self.K
        cx, lw, yh = self.cx, self.lw, self.y_hit
        H, W = self.height, self.width
        spb = 60.0 / song.bpm
        beat_pos = t / spb
        beat_frac = beat_pos - math.floor(beat_pos)
        pulse_env = max(0.0, 1.0 - beat_frac * 3.2)
        # smooth storm visual state
        target = 1.0 if eng.storm_active else 0.0
        self.storm_vis += clamp(target - self.storm_vis, -dt * 3.0, dt * 3.0)
        sv = self.storm_vis
        # decays
        for i in range(LANES):
            self.press_glow[i] = max(0.0, self.press_glow[i] - dt * 5.0)
            self.hit_flash[i] = max(0.0, self.hit_flash[i] - dt * 5.5)
        self.miss_flash = max(0.0, self.miss_flash - dt * 3.0)
        self.storm_flash = max(0.0, self.storm_flash - dt * 2.2)
        self.pulse = max(0.0, self.pulse - dt * 2.0)

        # background stage lights
        for i in range(4):
            ang = t * (0.4 + 0.08 * i) + i * 1.7
            ax = self.x + W * (0.12 + 0.25 * i)
            ay = self.y + H * 1.03
            bx = ax + math.sin(ang) * W * 0.30
            wd = W * 0.06
            self.beams[i].points = [ax, ay, bx - wd, self.y + H * 0.1, bx + wd, self.y + H * 0.1]
            if sv > 0.01:
                self.beam_c[i].rgba = (0.3, 0.95, 1.0, 0.09 + 0.08 * sv * (0.5 + 0.5 * math.sin(t * 6 + i)))
            else:
                self.beam_c[i].rgba = (0.6 - 0.1 * i, 0.2 + 0.1 * i, 1.0, 0.05 + 0.05 * pulse_env)
        self.pulse_c.rgba = (0.5 + 0.4 * sv, 0.2 + 0.7 * sv, 1.0, 0.05 + 0.10 * pulse_env + 0.15 * self.pulse + 0.08 * sv)

        # highway tint
        self.hw_c.rgba = (0.03 + 0.02 * sv, 0.03 + 0.10 * sv, 0.08 + 0.12 * sv, 0.93)
        self.storm_c.a = sv * (0.10 + 0.07 * math.sin(t * 9.0))
        self.div_c.rgba = (0.55 + 0.4 * sv, 0.45 + 0.5 * sv, 0.9 + 0.1 * sv, 0.55 + 0.3 * sv)
        for i in range(LANES):
            self.lane_c[i].a = 0.07 + 0.22 * self.press_glow[i] + 0.05 * pulse_env
        # beat lines
        n = 0
        b0 = int(math.floor(t / spb)) + 1
        k = b0
        while n < N_BEATS:
            dtb = k * spb - t
            if dtb > travel:
                break
            if dtb >= -0.02:
                z = max(0.0, dtb) / travel
                s = 1.0 / (1.0 + K * z)
                y = self._y_of(s)
                hw = self.hwb * s
                self.beat_l[n].points = [cx - hw, y, cx + hw, y]
                bar = (k % 4 == 0)
                self.beat_l[n].width = 2.0 if bar else 1.0
                self.beat_c[n].a = (0.55 if bar else 0.25) * min(1.0, (1.0 - z) * 5.0)
                n += 1
            k += 1
        for j in range(n, self.beats_used):
            self.beat_c[j].a = 0.0
        self.beats_used = n
        # hit line
        self.hitglow_c.a = 0.14 + 0.16 * pulse_env + 0.2 * sv
        self.hitline_c.rgba = (0.85 + 0.15 * sv, 0.85 + 0.15 * sv, 1.0, 0.95)

        # sustains + gems
        gi = 0
        si = 0
        br = self.base_r
        for lane in range(LANES):
            col = LANE_COLORS[lane]
            lst = eng.lane_notes[lane]
            hold = eng.holding[lane]
            if hold is not None and si < N_SUST:
                si = self._trail(si, hold, lane, t, travel, col, True)
            i = eng.lane_ptr[lane]
            m = len(lst)
            while i < m and gi < N_GEMS:
                nt = lst[i]
                dtn = nt.t - t
                if dtn > travel:
                    break
                z = dtn / travel
                if z < -0.2:
                    i += 1
                    continue
                s = 1.0 / (1.0 + K * z)
                if nt.dur > 0 and si < N_SUST:
                    si = self._trail(si, nt, lane, t, travel, col, False)
                x = cx + (lane - 2) * lw * s
                y = self._y_of(s)
                r = br * s
                alpha = clamp((1.0 - z) * 6.0, 0.0, 1.0)
                if nt.storm:
                    gl = 2.3 + 0.25 * math.sin(t * 10.0)
                    self.g_glow_c[gi].rgba = (0.45, 0.95, 1.0, 0.30 * alpha)
                    self.g_glow[gi].pos = (x - r * gl / 2 * 1.0, y - r * gl * 0.36)
                    self.g_glow[gi].size = (r * gl, r * gl * 0.72)
                    self.g_body_c[gi].rgba = (1.0, 1.0, 1.0, alpha)
                    self.g_core_c[gi].rgba = (col[0], col[1], col[2], alpha)
                else:
                    self.g_glow_c[gi].a = 0.0
                    self.g_glow[gi].size = (0, 0)
                    self.g_body_c[gi].rgba = (col[0], col[1], col[2], alpha)
                    self.g_core_c[gi].rgba = (min(1, col[0] + 0.55), min(1, col[1] + 0.55), min(1, col[2] + 0.55), alpha)
                self.g_body[gi].pos = (x - r, y - r * 0.7)
                self.g_body[gi].size = (2 * r, 1.4 * r)
                self.g_core[gi].pos = (x - r * 0.55, y - r * 0.38)
                self.g_core[gi].size = (1.1 * r, 0.76 * r)
                gi += 1
                i += 1
        for j in range(gi, self.gems_used):
            self.g_body_c[j].a = 0.0
            self.g_core_c[j].a = 0.0
            self.g_body[j].size = (0, 0)
            self.g_core[j].size = (0, 0)
            self.g_glow_c[j].a = 0.0
            self.g_glow[j].size = (0, 0)
        self.gems_used = gi
        for j in range(si, self.sust_used):
            self.sust_c[j].a = 0.0
        self.sust_used = si

        # hit flashes + frets
        for lane in range(LANES):
            col = LANE_COLORS[lane]
            f = self.hit_flash[lane]
            if f > 0.01:
                rr = self.lw * (0.3 + 0.5 * (1.0 - f))
                x = self._lane_x(lane, 1.0)
                self.flash_c[lane].a = f * 0.85
                self.flash_e[lane].pos = (x - rr, yh - rr * 0.5)
                self.flash_e[lane].size = (2 * rr, rr)
            else:
                self.flash_c[lane].a = 0.0
            g = self.press_glow[lane]
            down = self.lane_count[lane] > 0
            bright = 0.42 + 0.58 * (1.0 if down else g)
            self.fret_c[lane].rgba = (col[0] * bright, col[1] * bright, col[2] * bright, 1)
            self.fret_ic[lane].rgba = (min(1, col[0] + 0.5 * bright), min(1, col[1] + 0.5 * bright), min(1, col[2] + 0.5 * bright), 0.45 + 0.55 * bright)
            if down or g > 0.02:
                gg = max(g, 1.0 if down else 0.0)
                w = self.lw * (1.15 + 0.35 * gg)
                x = self._lane_x(lane, 1.0)
                self.fret_gc[lane].a = 0.35 * gg
                self.fret_g[lane].pos = (x - w / 2, self.y + H * 0.10)
                self.fret_g[lane].size = (w, H * 0.30)
            else:
                self.fret_gc[lane].a = 0.0

        # particles
        if self.part_active > 0:
            alive = 0
            for i in range(N_PART):
                life = self.plife[i]
                if life <= 0.0:
                    continue
                life -= dt
                self.plife[i] = life
                if life <= 0.0:
                    self.part_c[i].a = 0.0
                    self.part_e[i].size = (0, 0)
                    continue
                alive += 1
                self.pvy[i] -= H * 1.6 * dt
                self.px[i] += self.pvx[i] * dt
                self.py[i] += self.pvy[i] * dt
                fr = life / self.pmax[i]
                sz = H * 0.022 * (0.4 + fr)
                self.part_c[i].a = fr
                self.part_e[i].pos = (self.px[i] - sz / 2, self.py[i] - sz / 2)
                self.part_e[i].size = (sz, sz)
            self.part_active = alive

        # meters
        h = eng.health
        self.hm_c.rgb = (0.95, 0.2, 0.2) if h < 0.25 else ((1.0, 0.8, 0.15) if h < 0.5 else (0.2, 0.9, 0.4))
        self.hm_fill.pos = (self.hm_x, self.m_y0)
        self.hm_fill.size = (self.m_w, self.m_h * clamp(h, 0.0, 1.0))
        sm = clamp(eng.storm, 0.0, 1.0)
        if eng.storm_active:
            self.sm_c.rgb = (1.0, 1.0, 1.0)
        elif sm >= STORM_MIN_TO_ACTIVATE:
            self.sm_c.rgb = (0.3 + 0.7 * pulse_env, 1.0, 1.0)
        else:
            self.sm_c.rgb = (0.15, 0.6, 0.85)
        self.sm_fill.pos = (self.sm_x, self.m_y0)
        self.sm_fill.size = (self.m_w, self.m_h * sm)
        prog = clamp((t - song.lead_time) / song.duration, 0.0, 1.0)
        self.pg_fill.pos = (self.x, self.y + H * 0.988)
        self.pg_fill.size = (W * prog, H * 0.012)
        # screen feedback
        self.miss_c.a = 0.28 * self.miss_flash
        self.flash_all_c.a = 0.35 * self.storm_flash

    def _trail(self, si, note, lane, t, travel, col, held):
        end = note.t + note.dur
        z0 = max(0.0, note.t - t) / travel
        z1 = min(1.0, (end - t) / travel)
        if z1 <= z0:
            return si
        s0 = 1.0 / (1.0 + self.K * z0)
        s1 = 1.0 / (1.0 + self.K * z1)
        y0, y1 = self._y_of(s0), self._y_of(s1)
        x0 = self._lane_x(lane, s0)
        x1 = self._lane_x(lane, s1)
        w0 = self.lw * 0.14 * s0
        w1 = self.lw * 0.14 * s1
        self.sust_q[si].points = [x0 - w0, y0, x0 + w0, y0, x1 + w1, y1, x1 - w1, y1]
        if held:
            self.sust_c[si].rgba = (min(1, col[0] + 0.4), min(1, col[1] + 0.4), min(1, col[2] + 0.4), 0.95)
        else:
            self.sust_c[si].rgba = (col[0], col[1], col[2], 0.55)
        return si + 1


class PauseOverlay(FloatLayout):
    """Pause menu shown on top of the gameplay screen."""

    def __init__(self, screen, **kw):
        super(PauseOverlay, self).__init__(**kw)
        with self.canvas.before:
            Color(0, 0, 0, 0.72)
            self._dim = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._lay, size=self._lay)
        self.add_widget(FLabel(text='PAUSED', frac=0.10, bold=True, color=(1, 1, 1, 1),
                               size_hint=(0.6, 0.14), pos_hint={'center_x': 0.5, 'top': 0.95}))
        box = BoxLayout(orientation='vertical', spacing=Window.height * 0.02,
                        size_hint=(0.42, 0.62), pos_hint={'center_x': 0.5, 'y': 0.06})
        for text, cb, rgb in (('RESUME', screen.resume, (0.2, 0.95, 0.5)),
                              ('RESTART SONG', screen.restart, COL_CYAN),
                              ('SETTINGS', screen.open_settings, COL_PURPLE),
                              ('QUIT TO SONG SELECT', screen.quit_to_select, COL_PINK)):
            b = NeonButton(text=text, rgb=rgb, frac=0.052)
            b.bind(on_release=lambda inst, f=cb: f())
            box.add_widget(b)
        self.add_widget(box)

    def _lay(self, *a):
        self._dim.pos = self.pos
        self._dim.size = self.size

    def on_touch_down(self, touch):
        super(PauseOverlay, self).on_touch_down(touch)
        return True

    def on_touch_move(self, touch):
        super(PauseOverlay, self).on_touch_move(touch)
        return True

    def on_touch_up(self, touch):
        super(PauseOverlay, self).on_touch_up(touch)
        return True


class GameplayScreen(Screen):
    """Owns a play session: clock, engine, HUD and pause handling."""

    def __init__(self, app, **kw):
        super(GameplayScreen, self).__init__(name='gameplay', **kw)
        self.app = app
        self.engine = GameEngine()
        self.clock = SongClock()
        self.song = None
        self.pending = None
        self.state = 'idle'       # idle | playing | paused | countdown | failed | done
        self.travel = 1.65
        self.cal = 0.0
        self.countdown = 0.0
        self.resume_pos = 0.0
        self._ev = None
        self._last_vals = {}
        self.root_fl = FloatLayout()
        self.add_widget(self.root_fl)
        self.view = GameView(self)
        self.root_fl.add_widget(self.view)
        L = self.root_fl.add_widget
        self.score_lbl = FLabel(text='0', frac=0.08, bold=True, halign='left', size_hint=(0.3, 0.11),
                                pos_hint={'x': 0.028, 'top': 0.985})
        self.title_lbl = FLabel(text='', frac=0.032, halign='left', color=(0.8, 0.75, 1.0, 1),
                                size_hint=(0.28, 0.05), pos_hint={'x': 0.028, 'top': 0.88})
        self.sec_lbl = FLabel(text='', frac=0.03, halign='left', color=(0.5, 0.9, 1.0, 1),
                              size_hint=(0.28, 0.05), pos_hint={'x': 0.028, 'top': 0.835})
        self.acc_lbl = FLabel(text='100.0%', frac=0.04, halign='left', color=(0.85, 0.85, 1.0, 1),
                              size_hint=(0.16, 0.06), pos_hint={'x': 0.028, 'top': 0.78})
        self.combo_lbl = FLabel(text='', frac=0.085, bold=True, size_hint=(0.14, 0.12),
                                pos_hint={'x': 0.0, 'y': 0.20})
        self.mult_lbl = FLabel(text='x1', frac=0.06, bold=True, color=COL_GOLD + (1,),
                               size_hint=(0.12, 0.09), pos_hint={'x': 0.005, 'y': 0.10})
        self.hlabel = FLabel(text='ROCK', frac=0.026, color=(0.7, 0.7, 0.85, 1),
                             size_hint=(0.08, 0.04), pos_hint={'x': 0.041, 'y': 0.31})
        self.slabel = FLabel(text='STORM', frac=0.026, color=(0.6, 0.95, 1, 1),
                             size_hint=(0.08, 0.04), pos_hint={'x': 0.897, 'y': 0.31})
        self.judge_lbl = PopLabel(text='', frac=0.075, bold=True, size_hint=(0.5, 0.12),
                                  pos_hint={'center_x': 0.5, 'y': 0.44})
        self.combo_pop = PopLabel(text='', frac=0.06, bold=True, size_hint=(0.5, 0.10),
                                  pos_hint={'center_x': 0.5, 'y': 0.58})
        self.big_lbl = FLabel(text='', frac=0.14, bold=True, size_hint=(0.6, 0.3),
                              pos_hint={'center_x': 0.5, 'center_y': 0.58})
        for w in (self.score_lbl, self.title_lbl, self.sec_lbl, self.acc_lbl, self.combo_lbl,
                  self.mult_lbl, self.hlabel, self.slabel, self.judge_lbl, self.combo_pop, self.big_lbl):
            L(w)
        self.storm_btn = NeonButton(text='STORM\nPOWER', rgb=COL_CYAN, frac=0.038,
                                    size_hint=(0.105, 0.2), pos_hint={'x': 0.885, 'y': 0.08})
        self.storm_btn.bind(on_press=lambda *a: self.on_storm_pressed())
        self.storm_btn.opacity = 0.0
        self.storm_btn.disabled = True
        L(self.storm_btn)
        self.pause_btn = NeonButton(text='II', rgb=COL_PURPLE, frac=0.05, size_hint=(0.075, 0.11),
                                    pos_hint={'right': 0.975, 'top': 0.965})
        self.pause_btn.bind(on_release=lambda *a: self.pause())
        L(self.pause_btn)
        self.overlay = PauseOverlay(self)
        self.overlay_on = False

    # -- helpers -----------------------------------------------------------------------------
    def accepting(self):
        return self.state == 'playing'

    def judge_time(self):
        return self.clock.now() - self.cal

    def _set(self, key, lbl, text):
        if self._last_vals.get(key) != text:
            self._last_vals[key] = text
            lbl.text = text

    # -- lifecycle -----------------------------------------------------------------------------
    def on_enter(self, *a):
        if self.pending is not None:
            song, path = self.pending
            self.pending = None
            self.start_song(song, path)
        elif self.state == 'countdown' or self.state == 'playing':
            self._schedule()

    def on_leave(self, *a):
        if self.state in ('playing', 'countdown'):
            self.pause()
        self._unschedule()

    def _schedule(self):
        if self._ev is None:
            self._ev = Clock.schedule_interval(self.update, 0)

    def _unschedule(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None

    def start_song(self, song, path):
        s = self.app.settings
        self._hide_overlay()
        self.song = song
        self.song_path = path
        self.engine.reset(song.get_chart(), bool(s['failure']))
        self.travel = 1.65 / clamp(s['speed'], 0.5, 2.0)
        self.cal = s['cal'] / 1000.0
        self.view.reset()
        self._last_vals = {}
        self.title_lbl.text = song.title
        self.big_lbl.text = ''
        self.judge_lbl.opacity = 0.0
        self.combo_pop.opacity = 0.0
        self.storm_btn.opacity = 0.0
        self.storm_btn.disabled = True
        self.app.audio.load_music(path)
        self.app.audio.play_music(0.0)
        self.clock.start(0.0)
        self.state = 'playing'
        self._schedule()
        self.update(0.0)

    def _show_overlay(self):
        if not self.overlay_on:
            self.root_fl.add_widget(self.overlay)
            self.overlay_on = True

    def _hide_overlay(self):
        if self.overlay_on:
            self.root_fl.remove_widget(self.overlay)
            self.overlay_on = False

    # -- pause / resume / restart / quit ---------------------------------------------------------
    def pause(self):
        if self.state not in ('playing', 'countdown'):
            return
        if self.state == 'playing':
            self.clock.stop()
            self.resume_pos = self.clock.pos
        self.app.audio.stop_music()
        self.view.release_all()
        self.state = 'paused'
        self.big_lbl.text = ''
        self._show_overlay()
        self._unschedule()

    def resume(self):
        if self.state != 'paused':
            return
        self._hide_overlay()
        self.state = 'countdown'
        self.countdown = 2.4
        self._schedule()

    def restart(self):
        if self.song is None:
            return
        self.app.audio.stop_music()
        self.clock.stop()
        self.start_song(self.song, self.song_path)

    def open_settings(self):
        self.app.open_settings(return_to='gameplay')

    def quit_to_select(self):
        self.stop_session()
        self.app.goto('songselect')

    def stop_session(self):
        self.state = 'idle'
        self._unschedule()
        self.clock.stop()
        self.app.audio.stop_music()
        self._hide_overlay()
        self.view.release_all()

    def on_back(self):
        if self.state in ('playing', 'countdown'):
            self.pause()
        elif self.state == 'paused':
            self.resume()

    def on_storm_pressed(self):
        if self.state == 'playing' and self.engine.activate_storm():
            self.app.audio.play_sfx('storm')
            self.view.on_storm()

    def on_lane_press(self, lane):
        if self.state == 'playing':
            self.engine.press(lane, self.judge_time())

    # -- frame update ---------------------------------------------------------------------------------
    def update(self, dt):
        dt = min(dt, 0.1)
        eng = self.engine
        song = self.song
        if song is None:
            return
        if self.state == 'countdown':
            self.countdown -= dt
            n = int(math.ceil(self.countdown))
            if n > 0:
                self._set('big', self.big_lbl, str(n))
            else:
                self.big_lbl.text = ''
                self._last_vals.pop('big', None)
                self.app.audio.play_music(self.resume_pos)
                self.clock.start(self.resume_pos)
                self.state = 'playing'
            self.view.draw(self.resume_pos - self.cal, dt)
            return
        if self.state == 'failed':
            self.view.draw(self.clock.now() - self.cal, dt)
            return
        if self.state != 'playing':
            return
        self.clock.sync(self.app.audio.music_pos())
        t = self.clock.now()
        te = t - self.cal
        eng.update(te, dt)
        self._process_events()
        self.view.draw(te, dt)
        self._update_hud(te, dt)
        if eng.failed and self.state == 'playing':
            self._fail()
        elif t >= song.total_time - 0.05:
            self._finish()

    def _process_events(self):
        eng = self.engine
        if not eng.events:
            return
        s = self.app.settings
        for (kind, lane, grade, storm, combo) in eng.events:
            if kind == 'hit':
                self.view.on_hit(lane, grade, storm, combo, s['particles'])
                self.judge_lbl.pop(GRADE_NAMES[grade], (
                    (0.4, 1.0, 1.0), (0.5, 1.0, 0.5), (1.0, 0.9, 0.3))[grade], 0.5)
                self.app.audio.play_sfx('hit', 0.5, 0.05)
                if s['vibration']:
                    self.app.vibe.pulse(10)
                if combo and combo % 25 == 0:
                    self.combo_pop.pop('%d COMBO!' % combo, COL_GOLD, 1.1)
            elif kind == 'miss':
                self.view.on_miss()
                self.judge_lbl.pop('MISS', (1.0, 0.3, 0.3), 0.5)
                self.app.audio.play_sfx('miss', 0.6, 0.12)
            elif kind == 'storm_off':
                self.combo_pop.pop('STORM OVER', (0.7, 0.8, 1.0), 0.8)
        del eng.events[:]

    def _update_hud(self, te, dt):
        eng = self.engine
        self._set('score', self.score_lbl, fmt_score(eng.score))
        self._set('acc', self.acc_lbl, '%.1f%%' % eng.accuracy())
        self._set('combo', self.combo_lbl, str(eng.combo) if eng.combo >= 2 else '')
        mult = eng.mult * (2 if eng.storm_active else 1)
        self._set('mult', self.mult_lbl, 'x%d' % mult)
        self.mult_lbl.color = (0.4, 1.0, 1.0, 1) if eng.storm_active else COL_GOLD + (1,)
        self._set('sec', self.sec_lbl, self.song.section_at_time(te))
        ready = eng.storm_ready()
        if ready and self.storm_btn.disabled:
            self.storm_btn.disabled = False
            self.storm_btn.opacity = 1.0
        elif (not ready) and (not self.storm_btn.disabled):
            self.storm_btn.disabled = True
            self.storm_btn.opacity = 0.0
        self.judge_lbl.tick(dt)
        self.combo_pop.tick(dt)

    # -- end of song ----------------------------------------------------------------------------------------
    def _fail(self):
        self.state = 'failed'
        self.clock.stop()
        self.app.audio.stop_music()
        self.view.release_all()
        self.big_lbl.text = 'SONG FAILED'
        self.big_lbl.color = (1, 0.3, 0.3, 1)
        Clock.schedule_once(lambda dt: self._to_results(True), 1.8)

    def _finish(self):
        self.state = 'done'
        self._unschedule()
        self.clock.stop()
        self.app.audio.stop_music()
        self.view.release_all()
        self._to_results(False)

    def _to_results(self, failed):
        if self.state not in ('failed', 'done'):
            return
        eng = self.engine
        self._unschedule()
        self.big_lbl.text = ''
        self.big_lbl.color = WHITE
        self.state = 'idle'
        song = self.song
        completion = 100.0 if not failed else clamp(
            100.0 * (self.clock.pos - song.lead_time) / song.duration, 0.0, 100.0)
        res = dict(idx=song.idx, failed=failed, score=eng.score, acc=eng.accuracy(True),
                   combo=eng.best_combo, hits=eng.hits, misses=eng.misses + (eng.total - eng.resolved),
                   stars=0 if failed else eng.stars(), completion=completion,
                   perfect=eng.grades[0], great=eng.grades[1], good=eng.grades[2], total=eng.total)
        self.app.show_results(res)


# ============================================================================
# 11. OTHER SCREENS
# ============================================================================
def fit_height(widget, frac):
    """Keep widget.height at a fraction of the window height."""
    def _f(*a):
        widget.height = Window.height * frac
    Window.bind(height=_f)
    _f()


class MenuScreen(Screen):
    """Base for the menu style screens: neon backdrop + optional animation."""

    def __init__(self, app, name, **kw):
        super(MenuScreen, self).__init__(name=name, **kw)
        self.app = app
        self.root_fl = FloatLayout()
        self.add_widget(self.root_fl)
        self.backdrop = Backdrop()
        self.root_fl.add_widget(self.backdrop)
        self._anim_ev = None

    def on_enter(self, *a):
        if self._anim_ev is None:
            self._anim_ev = Clock.schedule_interval(self._anim, 1.0 / 30.0)

    def on_leave(self, *a):
        if self._anim_ev is not None:
            self._anim_ev.cancel()
            self._anim_ev = None

    def _anim(self, dt):
        self.backdrop.tick(dt)

    def on_back(self):
        self.app.goto('menu')

    def add_header(self, text, back=True):
        self.root_fl.add_widget(FLabel(text=text, frac=0.085, bold=True, color=(1, 1, 1, 1),
                                       size_hint=(0.6, 0.13), pos_hint={'center_x': 0.5, 'top': 0.99}))
        if back:
            b = NeonButton(text='BACK', rgb=COL_PINK, frac=0.045, size_hint=(0.14, 0.095),
                           pos_hint={'x': 0.02, 'top': 0.975})
            b.bind(on_release=lambda *a: self.on_back())
            self.root_fl.add_widget(b)


class SplashScreen(MenuScreen):
    def __init__(self, app, **kw):
        super(SplashScreen, self).__init__(app, 'splash', **kw)
        self.glow = FLabel(text=APP_NAME, frac=0.185, bold=True, color=(0.9, 0.2, 1.0, 0.55),
                           size_hint=(1, 0.3), pos_hint={'center_x': 0.5, 'center_y': 0.585})
        self.title = FLabel(text=APP_NAME, frac=0.18, bold=True, color=(1, 1, 1, 1),
                            size_hint=(1, 0.3), pos_hint={'center_x': 0.5, 'center_y': 0.6})
        self.sub = FLabel(text='FIVE-LANE GUITAR STORM', frac=0.04, color=(0.5, 0.9, 1.0, 1),
                          size_hint=(1, 0.08), pos_hint={'center_x': 0.5, 'center_y': 0.4})
        self.status = FLabel(text='TUNING UP...', frac=0.035, color=(0.8, 0.8, 0.95, 1),
                             size_hint=(1, 0.08), pos_hint={'center_x': 0.5, 'center_y': 0.18})
        for w in (self.glow, self.title, self.sub, self.status):
            self.root_fl.add_widget(w)
        self.t = 0.0
        self.ready = False
        self.loaded = False
        self.paths = {}
        self._chk = None
        self._went = False

    def on_enter(self, *a):
        super(SplashScreen, self).on_enter(*a)
        self.t = 0.0
        self._went = False
        threading.Thread(target=self._work, daemon=True).start()
        self._chk = Clock.schedule_interval(self._check, 0.05)

    def _work(self):
        try:
            self.paths = SfxGenerator.build(self.app.audio.cache_dir)
        except Exception:
            log('sfx build failed', traceback.format_exc())
            self.paths = {}
        self.ready = True

    def _check(self, dt):
        self.t += dt
        self.glow.opacity = 0.6 + 0.4 * math.sin(self.t * 4.0)
        if self.ready and not self.loaded:
            self.loaded = True
            try:
                self.app.audio.load_sfx(self.paths)
            except Exception:
                pass
            self.status.text = 'TAP TO START'
        if self.loaded and self.t > 2.2:
            self.go()

    def go(self):
        if self._went:
            return
        self._went = True
        if self._chk is not None:
            self._chk.cancel()
            self._chk = None
        self.app.goto('menu')

    def on_touch_down(self, touch):
        if self.loaded and self.t > 0.6:
            self.go()
            return True
        return super(SplashScreen, self).on_touch_down(touch)

    def on_back(self):
        self.app.stop()


class MainMenuScreen(MenuScreen):
    def __init__(self, app, **kw):
        super(MainMenuScreen, self).__init__(app, 'menu', **kw)
        self.root_fl.add_widget(FLabel(text=APP_NAME, frac=0.155, bold=True, color=(1, 1, 1, 1),
                                       size_hint=(1, 0.25), pos_hint={'center_x': 0.5, 'top': 1.0}))
        self.root_fl.add_widget(FLabel(text='TAP THE FRETS. RIDE THE STORM.', frac=0.034,
                                       color=(0.5, 0.9, 1.0, 1), size_hint=(1, 0.06),
                                       pos_hint={'center_x': 0.5, 'top': 0.77}))
        box = BoxLayout(orientation='vertical', spacing=Window.height * 0.018,
                        size_hint=(0.38, 0.62), pos_hint={'center_x': 0.5, 'y': 0.04})
        items = (('PLAY', lambda: app.goto('songselect'), (0.2, 0.95, 0.5)),
                 ('SETTINGS', lambda: app.open_settings('menu'), COL_PURPLE),
                 ('HOW TO PLAY', lambda: app.goto('howto'), COL_CYAN),
                 ('CREDITS', lambda: app.goto('credits'), COL_GOLD),
                 ('EXIT', lambda: app.stop(), COL_PINK))
        for text, cb, rgb in items:
            b = NeonButton(text=text, rgb=rgb, frac=0.055)
            b.bind(on_release=lambda inst, f=cb: f())
            box.add_widget(b)
        self.root_fl.add_widget(box)
        self.toast = PopLabel(text='', frac=0.035, size_hint=(0.6, 0.07), pos_hint={'center_x': 0.5, 'y': 0.0})
        self.root_fl.add_widget(self.toast)
        self.last_back = 0.0

    def _anim(self, dt):
        super(MainMenuScreen, self)._anim(dt)
        self.toast.tick(dt)

    def on_back(self):
        now = time.time()
        if now - self.last_back < 2.0:
            self.app.stop()
        else:
            self.last_back = now
            self.toast.pop('PRESS BACK AGAIN TO EXIT', (1, 1, 1), 2.0)


class SongRow(ButtonBehavior, FloatLayout):
    """One entry of the song list."""

    def __init__(self, song, on_pick, **kw):
        super(SongRow, self).__init__(size_hint_y=None, **kw)
        self.song = song
        self.on_pick = on_pick
        self.locked = False
        with self.canvas.before:
            self.c_bg = Color(0.07, 0.05, 0.14, 0.92)
            self.bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[12])
            self.c_ln = Color(0.5, 0.3, 0.9, 0.9)
            self.ln = Line(rounded_rectangle=(0, 0, 10, 10, 6), width=1.3)
        self.bind(pos=self._lay, size=self._lay, state=self._lay)
        fit_height(self, 0.2)
        self.num = FLabel(text='%02d' % (song.idx + 1), frac=0.07, bold=True, color=(0.75, 0.5, 1, 1),
                          size_hint=(0.07, 0.9), pos_hint={'x': 0.01, 'center_y': 0.5})
        self.title = FLabel(text=song.title, frac=0.05, bold=True, halign='left',
                            size_hint=(0.42, 0.5), pos_hint={'x': 0.09, 'top': 0.96})
        self.meta = FLabel(text='%s | %d BPM | %s | %s' % (song.style, song.bpm, song.key_name, fmt_time(song.duration)),
                           frac=0.031, halign='left', color=(0.7, 0.72, 0.9, 1),
                           size_hint=(0.44, 0.4), pos_hint={'x': 0.09, 'y': 0.04})
        self.diff_lbl = FLabel(text='DIFFICULTY %d' % song.diff, frac=0.026, color=(0.8, 0.8, 0.95, 1),
                               size_hint=(0.14, 0.25), pos_hint={'x': 0.55, 'top': 0.9})
        self.diff = DiffBars(level=song.diff, size_hint=(0.14, 0.4), pos_hint={'x': 0.55, 'y': 0.12})
        self.best = FLabel(text='', frac=0.04, bold=True, halign='right', color=COL_GOLD + (1,),
                           size_hint=(0.26, 0.4), pos_hint={'right': 0.985, 'top': 0.95})
        self.stars = StarRow(stars=0, size_hint=(0.24, 0.4), pos_hint={'right': 0.985, 'y': 0.06})
        self.lock = FLabel(text='LOCKED', frac=0.05, bold=True, color=(1, 0.4, 0.5, 1),
                           size_hint=(0.26, 0.5), pos_hint={'right': 0.985, 'center_y': 0.5})
        for w in (self.num, self.title, self.meta, self.diff_lbl, self.diff, self.best, self.stars, self.lock):
            self.add_widget(w)
        self.bind(on_release=lambda *a: self.on_pick(self.song.idx))

    def _lay(self, *a):
        self.bg.pos = self.pos
        self.bg.size = self.size
        r = max(4.0, self.height * 0.14)
        self.bg.radius = [r]
        self.ln.rounded_rectangle = (self.x, self.y, self.width, self.height, r)
        self.c_bg.rgba = (0.16, 0.10, 0.30, 0.95) if self.state == 'down' else (0.07, 0.05, 0.14, 0.92)

    def refresh(self, save):
        self.locked = not save.is_unlocked(self.song.idx)
        best = save.best(self.song.idx)
        self.lock.opacity = 1.0 if self.locked else 0.0
        self.best.opacity = 0.0 if self.locked else 1.0
        self.stars.opacity = 0.0 if self.locked else 1.0
        self.best.text = ('BEST %s' % fmt_score(best['score'])) if best else 'NO SCORE YET'
        self.stars.set_stars(best['stars'] if best else 0)
        for w in (self.num, self.title, self.meta, self.diff, self.diff_lbl):
            w.opacity = 0.45 if self.locked else 1.0
        self.c_ln.rgba = (0.35, 0.3, 0.45, 0.8) if self.locked else (0.5, 0.3, 0.9, 0.9)


class SongSelectScreen(MenuScreen):
    def __init__(self, app, **kw):
        super(SongSelectScreen, self).__init__(app, 'songselect', **kw)
        self.add_header('SELECT TRACK')
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(5), scroll_type=['bars', 'content'],
                                 size_hint=(0.94, 0.80), pos_hint={'center_x': 0.5, 'y': 0.02})
        self.grid = GridLayout(cols=1, size_hint_y=None, spacing=Window.height * 0.012,
                               padding=[Window.height * 0.01, Window.height * 0.01])
        self.grid.bind(minimum_height=self.grid.setter('height'))
        self.rows = []
        for song in app.songs:
            row = SongRow(song, self.pick)
            self.rows.append(row)
            self.grid.add_widget(row)
        self.scroll.add_widget(self.grid)
        self.root_fl.add_widget(self.scroll)
        self.toast = PopLabel(text='', frac=0.033, size_hint=(0.9, 0.07), pos_hint={'center_x': 0.5, 'y': 0.0})
        self.root_fl.add_widget(self.toast)

    def on_pre_enter(self, *a):
        for r in self.rows:
            r.refresh(self.app.save_mgr)

    def _anim(self, dt):
        super(SongSelectScreen, self)._anim(dt)
        self.toast.tick(dt)

    def pick(self, idx):
        if not self.app.save_mgr.is_unlocked(idx):
            need = idx - self.app.save_mgr.unlocked_count() + 1
            self.app.audio.play_sfx('miss', 0.6)
            self.toast.pop('LOCKED - COMPLETE %d MORE SONG%s TO UNLOCK' % (need, '' if need == 1 else 'S'),
                           (1, 0.6, 0.6), 2.2)
            return
        self.app.begin_song(idx)


class ProgressBar2(Widget):
    def __init__(self, **kw):
        super(ProgressBar2, self).__init__(**kw)
        self.value = 0.0
        with self.canvas:
            Color(0.15, 0.12, 0.25, 1)
            self.bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[8])
            self.fc = Color(0.7, 0.3, 1.0, 1)
            self.fill = RoundedRectangle(pos=self.pos, size=(0, self.height), radius=[8])
        self.bind(pos=self._lay, size=self._lay)

    def set_value(self, v):
        self.value = clamp(v, 0.0, 1.0)
        self._lay()

    def _lay(self, *a):
        self.bg.pos = self.pos
        self.bg.size = self.size
        self.fill.pos = self.pos
        self.fill.size = (max(0.0, self.width * self.value), self.height)


class LoadingScreen(MenuScreen):
    """Shown while a song is being synthesised for the first time."""

    def __init__(self, app, **kw):
        super(LoadingScreen, self).__init__(app, 'loading', **kw)
        self.title = FLabel(text='', frac=0.075, bold=True, size_hint=(0.9, 0.14),
                            pos_hint={'center_x': 0.5, 'center_y': 0.68})
        self.msg = FLabel(text='RENDERING THE BAND...', frac=0.04, color=(0.6, 0.9, 1, 1),
                          size_hint=(0.9, 0.08), pos_hint={'center_x': 0.5, 'center_y': 0.55})
        self.bar = ProgressBar2(size_hint=(0.6, 0.045), pos_hint={'center_x': 0.5, 'center_y': 0.43})
        self.pct = FLabel(text='0%', frac=0.04, size_hint=(0.3, 0.08), pos_hint={'center_x': 0.5, 'center_y': 0.34})
        self.tip = FLabel(text='First play only - the track is cached afterwards.', frac=0.03,
                          color=(0.7, 0.7, 0.85, 1), size_hint=(0.9, 0.06), pos_hint={'center_x': 0.5, 'center_y': 0.22})
        for w in (self.title, self.msg, self.bar, self.pct, self.tip):
            self.root_fl.add_widget(w)
        self.progress = 0.0
        self.done = False
        self.ok = False
        self.song = None
        self.path = None
        self.cancel = threading.Event()
        self._poll = None
        self._thread = None

    def begin(self, song, path):
        self.song = song
        self.path = path
        self.title.text = song.title
        self.progress = 0.0
        self.done = False
        self.ok = False
        self.cancel = threading.Event()
        self.bar.set_value(0)
        self.pct.text = '0%'
        self.msg.text = 'RENDERING THE BAND...'

    def on_enter(self, *a):
        super(LoadingScreen, self).on_enter(*a)
        self._thread = threading.Thread(target=self._work, args=(self.song, self.path, self.cancel), daemon=True)
        self._thread.start()
        self._poll = Clock.schedule_interval(self._check, 0.05)

    def on_leave(self, *a):
        super(LoadingScreen, self).on_leave(*a)
        if self._poll is not None:
            self._poll.cancel()
            self._poll = None

    def _set_progress(self, v):
        self.progress = v

    def _work(self, song, path, cancel):
        ok = False
        try:
            song.get_chart()
            ok = AudioGenerator.render(song, path, progress=self._set_progress, cancel=cancel)
        except Exception:
            log('render failed', traceback.format_exc())
            ok = False
        if not cancel.is_set():
            self.ok = ok
            self.done = True

    def _check(self, dt):
        self.bar.set_value(self.progress)
        self.pct.text = '%d%%' % int(self.progress * 100)
        if self.done:
            self.done = False
            if self._poll is not None:
                self._poll.cancel()
                self._poll = None
            path = self.path if (self.ok and AudioGenerator.is_valid(self.path, self.song)) else None
            if path is None:
                log('audio unavailable; starting silent')
            self.app.gameplay.pending = (self.song, path)
            self.app.goto('gameplay')

    def on_back(self):
        self.cancel.set()
        if self._poll is not None:
            self._poll.cancel()
            self._poll = None
        self.app.goto('songselect')


class ResultsScreen(MenuScreen):
    def __init__(self, app, **kw):
        super(ResultsScreen, self).__init__(app, 'results', **kw)
        self.res = None
        self.head = FLabel(text='SONG COMPLETE', frac=0.085, bold=True, size_hint=(0.9, 0.13),
                           pos_hint={'center_x': 0.5, 'top': 0.99})
        self.sname = FLabel(text='', frac=0.042, color=(0.6, 0.9, 1, 1), size_hint=(0.9, 0.07),
                            pos_hint={'center_x': 0.5, 'top': 0.865})
        self.stars = StarRow(stars=0, size_hint=(0.5, 0.15), pos_hint={'center_x': 0.5, 'top': 0.79})
        self.score = FLabel(text='0', frac=0.11, bold=True, color=COL_GOLD + (1,), size_hint=(0.8, 0.15),
                            pos_hint={'center_x': 0.5, 'top': 0.64})
        self.best = FLabel(text='', frac=0.04, bold=True, color=(0.4, 1, 0.6, 1), size_hint=(0.6, 0.06),
                           pos_hint={'center_x': 0.5, 'top': 0.50})
        self.l1 = FLabel(text='', frac=0.042, size_hint=(0.95, 0.075), pos_hint={'center_x': 0.5, 'top': 0.44})
        self.l2 = FLabel(text='', frac=0.042, size_hint=(0.95, 0.075), pos_hint={'center_x': 0.5, 'top': 0.36})
        self.l3 = FLabel(text='', frac=0.036, color=(0.75, 0.75, 0.92, 1), size_hint=(0.95, 0.07),
                         pos_hint={'center_x': 0.5, 'top': 0.285})
        for w in (self.head, self.sname, self.stars, self.score, self.best, self.l1, self.l2, self.l3):
            self.root_fl.add_widget(w)
        row = BoxLayout(orientation='horizontal', spacing=Window.height * 0.03,
                        size_hint=(0.86, 0.14), pos_hint={'center_x': 0.5, 'y': 0.03})
        self.b_retry = NeonButton(text='RETRY', rgb=COL_CYAN, frac=0.05)
        self.b_next = NeonButton(text='NEXT SONG', rgb=(0.2, 0.95, 0.5), frac=0.05)
        self.b_sel = NeonButton(text='SONG SELECT', rgb=COL_PURPLE, frac=0.05)
        self.b_retry.bind(on_release=lambda *a: self.app.begin_song(self.res['idx']))
        self.b_next.bind(on_release=lambda *a: self.app.begin_song(self.res['idx'] + 1))
        self.b_sel.bind(on_release=lambda *a: self.app.goto('songselect'))
        for b in (self.b_retry, self.b_next, self.b_sel):
            row.add_widget(b)
        self.root_fl.add_widget(row)

    def show(self, res, new_best):
        self.res = res
        song = self.app.songs[res['idx']]
        failed = res['failed']
        self.head.text = 'SONG FAILED' if failed else 'SONG COMPLETE'
        self.head.color = (1, 0.35, 0.35, 1) if failed else (1, 1, 1, 1)
        self.sname.text = '%s  -  %s' % (song.title, song.style)
        self.stars.set_stars(res['stars'])
        self.score.text = fmt_score(res['score'])
        self.best.text = 'NEW HIGH SCORE!' if (new_best and not failed) else ''
        self.l1.text = 'ACCURACY  %.1f%%      MAX COMBO  %d      COMPLETION  %d%%' % (
            res['acc'], res['combo'], int(res['completion']))
        self.l2.text = 'NOTES HIT  %d      NOTES MISSED  %d      TOTAL  %d' % (
            res['hits'], res['misses'], res['total'])
        self.l3.text = 'PERFECT %d    GREAT %d    GOOD %d' % (res['perfect'], res['great'], res['good'])
        can_next = (not failed) and res['idx'] + 1 < len(self.app.songs) and \
            self.app.save_mgr.is_unlocked(res['idx'] + 1)
        self.b_next.opacity = 1.0 if can_next else 0.0
        self.b_next.disabled = not can_next

    def on_enter(self, *a):
        super(ResultsScreen, self).on_enter(*a)
        self.app.audio.play_sfx('results')

    def on_back(self):
        self.app.goto('songselect')


class SettingsScreen(MenuScreen):
    def __init__(self, app, **kw):
        super(SettingsScreen, self).__init__(app, 'settings', **kw)
        self.return_to = 'menu'
        self.add_header('SETTINGS')
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(5), scroll_type=['bars', 'content'],
                                 size_hint=(0.9, 0.80), pos_hint={'center_x': 0.5, 'y': 0.02})
        self.grid = GridLayout(cols=1, size_hint_y=None, spacing=Window.height * 0.015,
                               padding=[Window.height * 0.01, Window.height * 0.01])
        self.grid.bind(minimum_height=self.grid.setter('height'))
        self.sliders = {}
        self.toggles = {}
        self._slider('MASTER VOLUME', 'master', 0, 1, 0.01, lambda v: '%d%%' % round(v * 100))
        self._slider('MUSIC VOLUME', 'music', 0, 1, 0.01, lambda v: '%d%%' % round(v * 100))
        self._slider('SFX VOLUME', 'sfx', 0, 1, 0.01, lambda v: '%d%%' % round(v * 100))
        self._slider('NOTE SPEED', 'speed', 0.6, 1.8, 0.05, lambda v: 'x%.2f' % v)
        self._slider('CALIBRATION OFFSET', 'cal', -300, 400, 5, lambda v: '%+d ms' % int(v))
        self._toggle('VIBRATION', 'vibration')
        self._toggle('PARTICLE EFFECTS', 'particles')
        self._toggle('SONG FAILURE', 'failure')
        self._toggle('UNLOCK ALL SONGS', 'unlock_all')
        self.note = FLabel(text='Calibration: raise the value if you hear the beat AFTER the gems reach the line, '
                                'lower it if you hear it BEFORE.', frac=0.03, wrap=True,
                           color=(0.7, 0.72, 0.9, 1), size_hint_y=None)
        fit_height(self.note, 0.14)
        self.grid.add_widget(self.note)
        self.scroll.add_widget(self.grid)
        self.root_fl.add_widget(self.scroll)

    def _row(self, label):
        row = BoxLayout(orientation='horizontal', size_hint_y=None, spacing=Window.height * 0.02)
        fit_height(row, 0.115)
        row.add_widget(FLabel(text=label, frac=0.04, bold=True, halign='left', size_hint_x=0.38))
        return row

    def _slider(self, label, key, lo, hi, step, fmt):
        row = self._row(label)
        sl = Slider(min=lo, max=hi, step=step, value=self.app.settings[key], size_hint_x=0.42,
                    cursor_size=(dp(34), dp(34)))
        val = FLabel(text=fmt(self.app.settings[key]), frac=0.04, size_hint_x=0.2, color=COL_GOLD + (1,))

        def changed(inst, v, key=key, val=val, fmt=fmt):
            if key == 'cal':
                v = int(v)
            self.app.settings[key] = v
            val.text = fmt(v)
            self.app.audio.apply_volumes()
        sl.bind(value=changed)
        row.add_widget(sl)
        row.add_widget(val)
        self.grid.add_widget(row)
        self.sliders[key] = (sl, val, fmt)

    def _toggle(self, label, key):
        row = self._row(label)
        tg = ToggleButton2(value=self.app.settings[key], frac=0.045,
                           callback=lambda v, key=key: self.app.settings.__setitem__(key, bool(v)),
                           size_hint_x=0.3)
        row.add_widget(tg)
        row.add_widget(Widget(size_hint_x=0.32))
        self.grid.add_widget(row)
        self.toggles[key] = tg

    def on_pre_enter(self, *a):
        s = self.app.settings
        for key, (sl, val, fmt) in self.sliders.items():
            sl.value = s[key]
            val.text = fmt(s[key])
        for key, tg in self.toggles.items():
            tg.set_value(s[key])

    def on_leave(self, *a):
        super(SettingsScreen, self).on_leave(*a)
        self.app.save_mgr.save()

    def on_back(self):
        self.app.save_mgr.save()
        self.app.goto(self.return_to)


class TextScreen(MenuScreen):
    """Scrollable text page (How To Play / Credits)."""

    def __init__(self, app, name, header, body, **kw):
        super(TextScreen, self).__init__(app, name, **kw)
        self.add_header(header)
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(5), scroll_type=['bars', 'content'],
                                 size_hint=(0.88, 0.80), pos_hint={'center_x': 0.5, 'y': 0.02})
        self.lbl = FLabel(text=body, frac=0.036, wrap=True, halign='left', valign='top', markup=True,
                          size_hint_y=None, color=(0.92, 0.92, 1.0, 1))
        self.lbl.bind(width=self._fit, texture_size=self._fit)
        self.scroll.add_widget(self.lbl)
        self.root_fl.add_widget(self.scroll)

    def _fit(self, *a):
        self.lbl.height = self.lbl.texture_size[1] + Window.height * 0.05


HOWTO_TEXT = (
    "[b][color=66ddff]THE LANES[/color][/b]\n"
    "Five coloured lanes - GREEN, RED, YELLOW, BLUE and ORANGE - run up the highway. "
    "Gems slide toward the glowing hit line. Tap the matching fret button at the bottom "
    "of the screen just as a gem reaches the line. There is no strum button.\n\n"
    "[b][color=66ddff]CHORDS[/color][/b]\n"
    "Gems that arrive together form a chord. Touch all of those frets at the same time - "
    "the screen supports multi-touch. Sliding a finger onto a neighbouring fret also picks that fret.\n\n"
    "[b][color=66ddff]SUSTAINS[/color][/b]\n"
    "A gem with a long tail is a sustain. Hit it, then keep your finger on the fret until the tail ends "
    "for bonus points.\n\n"
    "[b][color=66ddff]TIMING & SCORE[/color][/b]\n"
    "PERFECT, GREAT and GOOD hits score 50, 35 and 20 points. Missed gems break your combo. "
    "Every 10 notes in a row raises your multiplier: x1, x2, x3, up to x4.\n\n"
    "[b][color=66ddff]ROCK METER[/color][/b]\n"
    "The left meter is your performance. Hits fill it, misses drain it. If it hits zero the song fails "
    "(this can be switched off in Settings).\n\n"
    "[b][color=66ddff]STORM POWER[/color][/b]\n"
    "Glowing white gems charge the STORM meter on the right. When it is at least half full, the "
    "STORM POWER button appears. Tap it to double your score for as long as the meter lasts, while "
    "the highway lights up.\n\n"
    "[b][color=66ddff]STARS & UNLOCKS[/color][/b]\n"
    "Accuracy decides your 1-5 star rating. Finish songs to unlock the next tracks.\n\n"
    "[b][color=66ddff]CALIBRATION[/color][/b]\n"
    "Different phones have different audio delay. If the music feels late or early, adjust the "
    "CALIBRATION OFFSET in Settings.\n"
)

CREDITS_TEXT = (
    "[b][color=ffcc33]FRETSTORM[/color][/b]\n"
    "An original five-lane touchscreen guitar rhythm game.\n\n"
    "[b]Game design, code and art[/b]\n"
    "All interface graphics are drawn in code with Kivy canvas primitives.\n\n"
    "[b]Music[/b]\n"
    "All twenty songs are original compositions generated by the built-in software synthesiser "
    "(sine, square, triangle, sawtooth and noise oscillators). Chord progressions, riffs, "
    "melodies and note charts are all created from deterministic algorithms inside the game. "
    "No samples, recordings or third-party music are used.\n\n"
    "[b]Sound effects[/b]\n"
    "Generated procedurally.\n\n"
    "[b]Built with[/b]\n"
    "Python, Kivy and python-for-android.\n\n"
    "FRETSTORM is an independent game and is not affiliated with or endorsed by any other rhythm-game "
    "franchise.\n"
)


# ============================================================================
# 12. APPLICATION
# ============================================================================
class FretStormApp(App):
    title = APP_NAME

    def build(self):
        Window.clearcolor = (0.01, 0.01, 0.03, 1)
        self.save_mgr = SaveManager(self.user_data_dir)
        self.settings = self.save_mgr.settings
        self.songs = SongLibrary.songs
        self.audio = AudioEngine(self)
        self.vibe = Vibrator()
        self.sm = ScreenManager(transition=FadeTransition(duration=0.12))
        self.splash = SplashScreen(self)
        self.menu = MainMenuScreen(self)
        self.songselect = SongSelectScreen(self)
        self.loading = LoadingScreen(self)
        self.gameplay = GameplayScreen(self)
        self.results = ResultsScreen(self)
        self.settings_screen = SettingsScreen(self)
        self.howto = TextScreen(self, 'howto', 'HOW TO PLAY', HOWTO_TEXT)
        self.credits = TextScreen(self, 'credits', 'CREDITS', CREDITS_TEXT)
        for scr in (self.splash, self.menu, self.songselect, self.loading, self.gameplay,
                    self.results, self.settings_screen, self.howto, self.credits):
            self.sm.add_widget(scr)
        self.sm.current = 'splash'
        Window.bind(on_keyboard=self._on_keyboard)
        try:
            Window.bind(focus=self._on_focus)
        except Exception:
            pass
        return self.sm

    # -- navigation --------------------------------------------------------------------------
    def goto(self, name):
        if self.sm.current != name:
            self.sm.current = name

    def open_settings(self, return_to='menu'):
        self.settings_screen.return_to = return_to
        self.goto('settings')

    def begin_song(self, idx):
        if idx < 0 or idx >= len(self.songs):
            return
        song = self.songs[idx]
        path = AudioGenerator.path_for(self.audio.cache_dir, song)
        if AudioGenerator.is_valid(path, song):
            self.gameplay.pending = (song, path)
            self.goto('gameplay')
        else:
            self.loading.begin(song, path)
            self.goto('loading')

    def show_results(self, res):
        new_best = False
        if not res['failed']:
            new_best = self.save_mgr.record(res['idx'], res['score'], res['stars'], res['acc'],
                                            res['combo'], True)
        self.results.show(res, new_best)
        self.goto('results')

    # -- Android integration ----------------------------------------------------------------------
    def _on_keyboard(self, window, key, *args):
        if key == 27:                       # Android back button / Escape
            scr = self.sm.current_screen
            handler = getattr(scr, 'on_back', None)
            if handler is not None:
                handler()
            return True
        return False

    def _on_focus(self, window, focused):
        if not focused and self.sm.current == 'gameplay':
            self.gameplay.pause()

    def on_pause(self):
        try:
            if self.sm.current == 'gameplay':
                self.gameplay.pause()
            self.save_mgr.save()
        except Exception:
            log('on_pause', traceback.format_exc())
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        try:
            self.gameplay.stop_session()
            self.save_mgr.save()
        except Exception:
            pass


# ============================================================================
# SELF TEST  (python main.py --selftest)
# ============================================================================
def self_test():
    ok = True
    songs = SongLibrary.songs
    print('songs:', len(songs))
    ok &= len(songs) == 20
    hashes = set()
    for s in songs:
        ch = s.get_chart()
        hashes.add(hash(tuple(ch)))
        good = s.duration >= 150.0 and len(ch) > 100
        ok &= good
        print('%2d %-24s %3d BPM  %s  notes=%4d  %s' % (s.idx + 1, s.title, s.bpm, fmt_time(s.duration),
                                                          len(ch), 'OK' if good else 'FAIL'))
    ok &= len(hashes) == 20
    print('distinct charts:', len(hashes))
    print('SELFTEST', 'PASSED' if ok else 'FAILED')
    return ok


if __name__ == '__main__':
    if SELFTEST:
        sys.exit(0 if self_test() else 1)
    FretStormApp().run()
