"""
Pulsar — unified desktop GUI.
Controls capture, browses screenshots, and runs/displays analysis.
Launch with run_pulsar.bat
"""

import configparser
import json
import math
import os
import queue
import re
import subprocess
import threading
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import anthropic
import psutil
from PIL import Image, ImageTk

from analyze import (
    append_knowledge,
    build_initial_content,
    format_session_table,
    load_knowledge,
    load_screenshots,
    parse_sessions,
    select_keyframes,
    split_response,
)

BASE_DIR = Path(__file__).parent
PYTHON   = str(BASE_DIR / 'env' / 'Scripts' / 'python.exe')
CAPTURE  = str(BASE_DIR / 'capture.py')

# ── Palette — Ebury Light ────────────────────────────────────────────────
BG       = '#F4F6FA'       # off-white canvas
BG_CARD  = '#FFFFFF'       # white card surface
BG_SIDE  = '#ECEEF5'       # sidebar — slight cool tint
BG_CHAT  = '#F4F6FA'
BG_INPUT = '#E2E6F0'       # recessed / input surface
FG       = '#00313D'       # Ebury darkest — primary text
FG_DIM   = '#6B7A8E'       # secondary text
FG_MED   = '#3A5068'       # tertiary text
ACCENT   = '#0097BD'       # Ebury medium cyan — primary action
GREEN    = '#00965A'       # success green
RED      = '#D93535'       # error red
YELLOW   = '#D97B06'       # amber
MAUVE    = '#6648CC'       # purple
PEACH    = '#D96030'       # warm coral
TEAL     = '#00BEF0'       # Ebury primary cyan (lighter)
SKY      = '#0070A0'       # deep Ebury blue
PINK     = '#C03570'       # muted pink
EDGE_COL = '#D4DAE8'       # hairline separator
BG_DEEP  = '#C4CCDE'       # shadow / depth
OFF_COL  = '#B8C4D4'       # off-time / inactive blocks

PALETTE  = [ACCENT, TEAL, GREEN, MAUVE, PEACH, SKY, RED, '#00D4C8', PINK]

FONT     = ('Segoe UI', 10)
FONT_B   = ('Segoe UI', 10, 'bold')
FONT_S   = ('Segoe UI', 9)
FONT_M   = ('Segoe UI', 12)
FONT_L   = ('Segoe UI', 14, 'bold')
FONT_XL  = ('Segoe UI', 24, 'bold')
FONT_H   = ('Segoe UI', 32, 'bold')
FONT_XS  = ('Segoe UI', 8)
FONT_XSB = ('Segoe UI', 7, 'bold')

# Mermaid fallback layout
BOX_W, BOX_H, H_GAP, V_GAP, D_PAD = 160, 52, 50, 70, 40


class PulsarApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Pulsar')
        self.root.geometry('1100x820')
        self.root.configure(bg=BG)
        self.root.minsize(800, 600)

        self._q: queue.Queue = queue.Queue()
        self._photo_refs: list = []
        self._shot_refs:  list = []
        self._state      = 'idle'      # idle | analyzing | waiting | done
        self._api_msgs:  list = []
        self._questions  = ''
        self._cur_date   = ''
        self._sessions:  list = []
        self._cap_proc: subprocess.Popen | None = None
        self._tip:       tk.Toplevel | None = None
        self._chart_tips: list = []    # [(x1,y1,x2,y2,tip,proj_name|None), ...]
        self._last_tip:  str | None = None
        self._date_btns: dict = {}     # date_str → (frame, label, status_label)
        self._proj_canvas_items: dict = {}  # proj → [(id, kind, col, orig_fill, orig_outline, orig_w), ...]
        self._hovered_proj: str | None = None
        self._chart_time_data:     list = []
        self._chart_timeline_data: list = []
        self._chart_sessions:      list = []
        self._chart_subtask_data:  list = []
        self._today           = date.today().strftime('%Y-%m-%d')
        self._shots_loaded_date = ''          # lazy: load only when tab opened
        self._dates_cache: list = []          # cached _scan_dates() result
        self._dates_cache_key = ''            # invalidation key

        self._cfg = configparser.ConfigParser()
        self._cfg.read(BASE_DIR / 'config.ini')

        self._build_ui()
        self._poll()
        self._proj_pump()
        self._check_capture()
        self._refresh_dates()
        self.root.after(400, self._auto_select_initial)

    # ══════════════════════════════════════════════════════════════════════
    # UI construction
    # ══════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        for ori in ('Vertical', 'Horizontal'):
            sn   = f'Slim.{ori}.TScrollbar'
            axis = 'ns' if ori == 'Vertical' else 'ew'
            style.configure(sn, gripcount=0, background='#A8B5C8',
                            troughcolor=BG_SIDE, arrowsize=0, width=4,
                            borderwidth=0, relief='flat')
            style.layout(sn, [(f'{ori}.Scrollbar.trough', {
                'sticky': axis,
                'children': [(f'{ori}.Scrollbar.thumb',
                               {'sticky': 'nswe', 'expand': '1'})],
            })])
        style.configure('TPanedwindow', background=BG_SIDE)

        # ── Title bar ─────────────────────────────────────────────────────
        top = tk.Frame(self.root, bg=BG_SIDE)
        top.pack(fill='x')

        left_top = tk.Frame(top, bg=BG_SIDE)
        left_top.pack(side='left', padx=20, pady=12)
        tk.Label(left_top, text='PULSAR', bg=BG_SIDE, fg=ACCENT,
                 font=('Segoe UI', 11, 'bold')).pack(side='left')
        tk.Label(left_top, text='  ·  ', bg=BG_SIDE, fg=EDGE_COL,
                 font=('Segoe UI', 11)).pack(side='left')
        self._date_lbl = tk.Label(left_top, text='', bg=BG_SIDE,
                                  fg=FG_DIM, font=('Segoe UI', 11))
        self._date_lbl.pack(side='left')

        right_top = tk.Frame(top, bg=BG_SIDE)
        right_top.pack(side='right', padx=20, pady=12)
        self._status = tk.Label(right_top, text='', bg=BG_SIDE,
                                fg=FG_DIM, font=('Segoe UI', 9))
        self._status.pack(side='right', padx=(10, 0))

        # Recording control — minimal text link with a tiny dot indicator.
        cap_wrap = tk.Frame(right_top, bg=BG_SIDE, cursor='hand2')
        cap_wrap.pack(side='right')
        self._cap_dot = tk.Label(cap_wrap, text='○', bg=BG_SIDE, fg=FG_DIM,
                                 font=('Segoe UI', 10), padx=0,
                                 cursor='hand2')
        self._cap_dot.pack(side='left', padx=(0, 4))
        self._cap_btn = tk.Label(cap_wrap, text='Record', bg=BG_SIDE,
                                 fg=FG_DIM, font=('Segoe UI', 9),
                                 cursor='hand2', padx=2, pady=2)
        self._cap_btn.pack(side='left')

        def _cap_enter(_e):
            self._cap_btn.configure(fg=FG_MED)
        def _cap_leave(_e):
            self._cap_btn.configure(fg=FG_DIM)
        for _w in (cap_wrap, self._cap_dot, self._cap_btn):
            _w.bind('<Button-1>', lambda e: self._toggle_capture())
            _w.bind('<Enter>',    _cap_enter)
            _w.bind('<Leave>',    _cap_leave)

        tk.Frame(self.root, bg=EDGE_COL, height=1).pack(fill='x')

        # ── Paned layout: sidebar | main ──────────────────────────────────
        self._pane = tk.PanedWindow(self.root, orient='horizontal',
                                    bg=EDGE_COL, sashwidth=5,
                                    sashrelief='flat', handlesize=0,
                                    sashpad=0, opaqueresize=True)
        self._pane.pack(fill='both', expand=True)

        # ── Sidebar ───────────────────────────────────────────────────────
        side_outer = tk.Frame(self._pane, bg=BG_SIDE, width=200)
        self._pane.add(side_outer, minsize=160)

        # History entry — top-level navigation, separate from day list
        hb_wrap = tk.Frame(side_outer, bg=BG_SIDE)
        hb_wrap.pack(fill='x', pady=(10, 0))
        self._hist_btn = tk.Label(
            hb_wrap, text='  History', bg=BG_SIDE, fg=FG,
            font=('Segoe UI', 10, 'bold'),
            padx=14, pady=10, cursor='hand2', anchor='w'
        )
        self._hist_btn.pack(fill='x', padx=10)
        self._hist_btn.bind('<Button-1>', lambda e: self._show_history())
        self._hist_btn.bind(
            '<Enter>',
            lambda e: (self._hist_btn.configure(bg=BG_INPUT)
                       if not self._hist_active() else None))
        self._hist_btn.bind(
            '<Leave>',
            lambda e: self._hist_btn.configure(
                bg=BG_INPUT if self._hist_active() else BG_SIDE))

        # Projects entry — top-level navigation, separate from day list
        pb_wrap = tk.Frame(side_outer, bg=BG_SIDE)
        pb_wrap.pack(fill='x', pady=(2, 0))
        self._proj_btn = tk.Label(
            pb_wrap, text='  Projects', bg=BG_SIDE, fg=FG,
            font=('Segoe UI', 10, 'bold'),
            padx=14, pady=10, cursor='hand2', anchor='w'
        )
        self._proj_btn.pack(fill='x', padx=10)
        self._proj_btn.bind('<Button-1>', lambda e: self._show_projects())
        self._proj_btn.bind(
            '<Enter>',
            lambda e: (self._proj_btn.configure(bg=BG_INPUT)
                       if not self._proj_active() else None))
        self._proj_btn.bind(
            '<Leave>',
            lambda e: self._proj_btn.configure(
                bg=BG_INPUT if self._proj_active() else BG_SIDE))

        tk.Frame(side_outer, bg=EDGE_COL, height=1).pack(
            fill='x', padx=14, pady=(10, 0))

        tk.Label(side_outer, text='D A Y S', bg=BG_SIDE, fg=FG_DIM,
                 font=('Segoe UI', 7, 'bold')).pack(
                 fill='x', padx=16, pady=(14, 8))

        side_cv = tk.Canvas(side_outer, bg=BG_SIDE, highlightthickness=0)
        side_sb = ttk.Scrollbar(side_outer, orient='vertical',
                                command=side_cv.yview,
                                style='Slim.Vertical.TScrollbar')
        side_cv.configure(yscrollcommand=side_sb.set)
        side_sb.pack(side='right', fill='y')
        side_cv.pack(side='left', fill='both', expand=True)

        self._side_inner = tk.Frame(side_cv, bg=BG_SIDE)
        self._side_win   = side_cv.create_window(
            (0, 0), window=self._side_inner, anchor='nw')
        self._side_inner.bind('<Configure>',
            lambda e: side_cv.configure(scrollregion=side_cv.bbox('all')))
        side_cv.bind('<Configure>',
            lambda e: side_cv.itemconfig(self._side_win, width=e.width))
        side_cv.bind('<MouseWheel>',
            lambda e: side_cv.yview_scroll(-1*(e.delta//120), 'units'))

        # ── Main area ─────────────────────────────────────────────────────
        main_outer = tk.Frame(self._pane, bg=BG)
        self._pane.add(main_outer, minsize=500)

        self._main_outer = main_outer

        # iOS-style underline tab bar
        self._tab_bar = tk.Frame(main_outer, bg=BG)
        self._tab_bar.pack(fill='x', padx=24, pady=(12, 0))

        self._tab_frames  = []
        self._tab_btns    = []
        self._tab_inds    = []
        self._cur_tab_idx = -1

        for i, name in enumerate(('Analysis', 'Chart', 'Shots')):
            tc  = tk.Frame(self._tab_bar, bg=BG, cursor='hand2')
            tc.pack(side='left', padx=(0, 8))
            lbl = tk.Label(tc, text=name, bg=BG, fg=FG_DIM,
                           font=('Segoe UI', 10), padx=4, pady=9,
                           cursor='hand2')
            lbl.pack()
            ind = tk.Frame(tc, bg=BG, height=2)
            ind.pack(fill='x')
            lbl.bind('<Button-1>', lambda e, idx=i: self._switch_tab(idx))
            tc.bind('<Button-1>',  lambda e, idx=i: self._switch_tab(idx))
            self._tab_btns.append(lbl)
            self._tab_inds.append(ind)

        self._tab_div = tk.Frame(main_outer, bg=EDGE_COL, height=1)
        self._tab_div.pack(fill='x')

        self._tab_host = tk.Frame(main_outer, bg=BG)
        self._tab_host.pack(fill='both', expand=True)

        # ── Analysis tab ──────────────────────────────────────────────────
        chat_tab = tk.Frame(self._tab_host, bg=BG_CHAT)
        self._tab_frames.append(chat_tab)

        chat_sb = ttk.Scrollbar(chat_tab, style='Slim.Vertical.TScrollbar')
        self._chat = tk.Text(
            chat_tab, bg=BG_CHAT, fg=FG, font=FONT,
            wrap='word', state='disabled', relief='flat',
            padx=24, pady=16, spacing3=6, cursor='arrow',
            yscrollcommand=chat_sb.set,
        )
        chat_sb.configure(command=self._chat.yview)
        chat_sb.pack(side='right', fill='y')
        self._chat.pack(side='left', fill='both', expand=True)

        self._chat.tag_configure('h',          foreground=ACCENT,
                                               font=('Segoe UI', 12, 'bold'))
        self._chat.tag_configure('body',       foreground=FG)
        self._chat.tag_configure('claude_lbl', foreground=ACCENT,  font=FONT_B)
        self._chat.tag_configure('user_lbl',   foreground=GREEN,   font=FONT_B)
        self._chat.tag_configure('user_txt',   foreground=FG)
        self._chat.tag_configure('question',   foreground=YELLOW)
        self._chat.tag_configure('dim',        foreground=FG_DIM,  font=FONT_S)

        # ── Chart tab ─────────────────────────────────────────────────────
        diag_tab = tk.Frame(self._tab_host, bg=BG_CHAT)
        self._tab_frames.append(diag_tab)

        d_vsb = ttk.Scrollbar(diag_tab, orient='vertical',
                              style='Slim.Vertical.TScrollbar')
        d_hsb = ttk.Scrollbar(diag_tab, orient='horizontal',
                              style='Slim.Horizontal.TScrollbar')
        self._diag = tk.Canvas(diag_tab, bg=BG_CHAT, relief='flat',
                               highlightthickness=0,
                               yscrollcommand=d_vsb.set,
                               xscrollcommand=d_hsb.set)
        d_vsb.configure(command=self._diag.yview)
        d_hsb.configure(command=self._diag.xview)
        d_hsb.pack(side='bottom', fill='x')
        d_vsb.pack(side='right',  fill='y')
        self._diag.pack(side='left', fill='both', expand=True)
        self._diag.bind('<MouseWheel>',
            lambda e: self._diag.yview_scroll(-1*(e.delta//120), 'units'))
        self._diag.bind('<Motion>', self._on_diag_motion)
        self._diag.bind('<Leave>',  lambda e: (self._hide_tooltip(), self._unhighlight_proj()))
        self._diag.create_text(24, 24, text='Select a day to begin.',
                               fill=FG_DIM, font=FONT_S, anchor='nw')

        # ── Screenshots tab ───────────────────────────────────────────────
        shots_tab = tk.Frame(self._tab_host, bg=BG_CHAT)
        self._tab_frames.append(shots_tab)

        shots_cv = tk.Canvas(shots_tab, bg=BG_CHAT, highlightthickness=0)
        shots_sb = ttk.Scrollbar(shots_tab, orient='vertical',
                                 command=shots_cv.yview,
                                 style='Slim.Vertical.TScrollbar')
        shots_cv.configure(yscrollcommand=shots_sb.set)
        shots_sb.pack(side='right', fill='y')
        shots_cv.pack(side='left', fill='both', expand=True)
        shots_cv.bind('<MouseWheel>',
            lambda e: shots_cv.yview_scroll(-1*(e.delta//120), 'units'))

        self._shots_inner = tk.Frame(shots_cv, bg=BG_CHAT)
        self._shots_win   = shots_cv.create_window(
            (0, 0), window=self._shots_inner, anchor='nw')
        self._shots_inner.bind('<Configure>',
            lambda e: shots_cv.configure(scrollregion=shots_cv.bbox('all')))
        shots_cv.bind('<Configure>',
            lambda e: shots_cv.itemconfig(self._shots_win, width=e.width))

        # ── History view (separate from per-day tabs) ─────────────────────
        self._hist_frame = tk.Frame(main_outer, bg=BG_CHAT)
        # Not packed by default — _show_history() shows it

        # Top bar: title + range presets
        hist_top = tk.Frame(self._hist_frame, bg=BG_CHAT)
        hist_top.pack(fill='x', padx=20, pady=(14, 10))
        tk.Label(hist_top, text='HISTORY', bg=BG_CHAT, fg=FG,
                 font=('Segoe UI', 14, 'bold')).pack(side='left')

        self._hist_range_days = 30   # default
        self._hist_range_btns: dict = {}
        range_box = tk.Frame(hist_top, bg=BG_CHAT)
        range_box.pack(side='right')
        for label, days in [('7d', 7), ('30d', 30), ('90d', 90), ('All', 0)]:
            sel = (days == self._hist_range_days)
            b = tk.Label(range_box, text=label,
                         bg=ACCENT if sel else BG_INPUT,
                         fg='#ffffff' if sel else FG,
                         font=('Segoe UI', 9, 'bold'),
                         padx=14, pady=6, cursor='hand2')
            b.pack(side='left', padx=(0, 4))
            b.bind('<Button-1>', lambda e, d=days: self._set_hist_range(d))
            self._hist_range_btns[days] = b

        tk.Frame(self._hist_frame, bg=EDGE_COL, height=1).pack(fill='x')

        # Canvas
        hist_inner = tk.Frame(self._hist_frame, bg=BG_CHAT)
        hist_inner.pack(fill='both', expand=True)
        hist_vsb = ttk.Scrollbar(hist_inner, orient='vertical',
                                 style='Slim.Vertical.TScrollbar')
        self._hist_cv = tk.Canvas(hist_inner, bg=BG_CHAT, relief='flat',
                                  highlightthickness=0,
                                  yscrollcommand=hist_vsb.set)
        hist_vsb.configure(command=self._hist_cv.yview)
        hist_vsb.pack(side='right', fill='y')
        self._hist_cv.pack(side='left', fill='both', expand=True)
        self._hist_cv.bind('<MouseWheel>',
            lambda e: self._hist_cv.yview_scroll(-1*(e.delta//120), 'units'))
        self._hist_cv.bind('<Configure>', self._on_hist_resize)
        self._hist_cv.bind('<Motion>',    self._on_hist_motion)
        self._hist_cv.bind('<Leave>',     lambda e: self._hide_tooltip())

        self._hist_tips: list = []
        self._hist_resize_after = None

        # ── Projects view (separate from per-day tabs) ────────────────────
        self._proj_frame = tk.Frame(main_outer, bg=BG_CHAT)
        # Not packed by default — _show_projects() shows it

        # Top bar: title + range presets
        proj_top = tk.Frame(self._proj_frame, bg=BG_CHAT)
        proj_top.pack(fill='x', padx=20, pady=(14, 10))
        tk.Label(proj_top, text='PROJECTS', bg=BG_CHAT, fg=FG,
                 font=('Segoe UI', 14, 'bold')).pack(side='left')

        self._proj_range_days = 0          # default: All
        self._proj_range_btns: dict = {}
        prange_box = tk.Frame(proj_top, bg=BG_CHAT)
        prange_box.pack(side='right')
        for label, days in [('7d', 7), ('30d', 30), ('90d', 90), ('All', 0)]:
            sel = (days == self._proj_range_days)
            b = tk.Label(prange_box, text=label,
                         bg=ACCENT if sel else BG_INPUT,
                         fg='#ffffff' if sel else FG,
                         font=('Segoe UI', 9, 'bold'),
                         padx=14, pady=6, cursor='hand2')
            b.pack(side='left', padx=(0, 4))
            b.bind('<Button-1>', lambda e, d=days: self._set_proj_range(d))
            self._proj_range_btns[days] = b

        # Project picker row (horizontal scrollable pills)
        pick_wrap = tk.Frame(self._proj_frame, bg=BG_CHAT)
        pick_wrap.pack(fill='x', padx=20, pady=(0, 10))
        self._proj_pick_cv = tk.Canvas(pick_wrap, bg=BG_CHAT, height=34,
                                       highlightthickness=0)
        self._proj_pick_cv.pack(fill='x')
        self._proj_pick_inner = tk.Frame(self._proj_pick_cv, bg=BG_CHAT)
        self._proj_pick_win = self._proj_pick_cv.create_window(
            (0, 0), window=self._proj_pick_inner, anchor='nw')
        self._proj_pick_inner.bind('<Configure>',
            lambda e: self._proj_pick_cv.configure(
                scrollregion=self._proj_pick_cv.bbox('all')))

        tk.Frame(self._proj_frame, bg=EDGE_COL, height=1).pack(fill='x')

        # Vertical paned: insights (top) + chat (bottom), draggable sash.
        proj_split = tk.PanedWindow(self._proj_frame, orient='vertical',
                                    bg=EDGE_COL, sashwidth=5,
                                    sashrelief='flat', handlesize=0,
                                    sashpad=0, opaqueresize=True)
        proj_split.pack(fill='both', expand=True)
        self._proj_split = proj_split

        # ── Insights canvas (top pane) ────────────────────────────────────
        proj_inner = tk.Frame(proj_split, bg=BG_CHAT)
        proj_split.add(proj_inner, minsize=160, stretch='always')
        proj_vsb = ttk.Scrollbar(proj_inner, orient='vertical',
                                 style='Slim.Vertical.TScrollbar')
        self._proj_cv = tk.Canvas(proj_inner, bg=BG_CHAT, relief='flat',
                                  highlightthickness=0,
                                  yscrollcommand=proj_vsb.set)
        proj_vsb.configure(command=self._proj_cv.yview)
        proj_vsb.pack(side='right', fill='y')
        self._proj_cv.pack(side='left', fill='both', expand=True)
        self._proj_cv.bind('<MouseWheel>',
            lambda e: self._proj_cv.yview_scroll(-1*(e.delta//120), 'units'))
        self._proj_cv.bind('<Configure>', self._on_proj_resize)

        # ── Chat panel (bottom pane, draggable height) ────────────────────
        chat_pane = tk.Frame(proj_split, bg=BG_CHAT)
        proj_split.add(chat_pane, minsize=140, height=300, stretch='never')

        chat_head = tk.Frame(chat_pane, bg=BG_CHAT)
        chat_head.pack(fill='x', padx=20, pady=(10, 4))
        self._proj_chat_title = tk.Label(
            chat_head, text='Chat about this project',
            bg=BG_CHAT, fg=FG, font=('Segoe UI', 10, 'bold'), anchor='w')
        self._proj_chat_title.pack(side='left')
        self._proj_chat_status = tk.Label(
            chat_head, text='', bg=BG_CHAT, fg=FG_DIM,
            font=('Segoe UI', 8))
        self._proj_chat_status.pack(side='right')

        chat_inp = tk.Frame(chat_pane, bg=BG, padx=20, pady=8)
        chat_inp.pack(side='bottom', fill='x')
        inp_row = tk.Frame(chat_inp, bg=BG_INPUT)
        inp_row.pack(fill='x')
        self._proj_inp = tk.Text(inp_row, height=2, bg=BG_INPUT, fg=FG,
                                 insertbackground=FG, relief='flat', font=FONT,
                                 padx=12, pady=8, wrap='word')
        self._proj_inp.pack(side='left', fill='x', expand=True)
        self._proj_inp.bind('<Return>', self._on_proj_enter)
        self._proj_send = tk.Button(
            inp_row, text='Send', command=self._proj_send_msg,
            bg=ACCENT, fg='#ffffff', font=('Segoe UI', 9, 'bold'),
            relief='flat', padx=18, pady=6, cursor='hand2', bd=0)
        self._proj_send.pack(side='right', padx=4, pady=4)

        chat_body = tk.Frame(chat_pane, bg=BG_CHAT)
        chat_body.pack(fill='both', expand=True, padx=20, pady=(0, 6))
        chat_sb = ttk.Scrollbar(chat_body, style='Slim.Vertical.TScrollbar')
        self._proj_chat_text = tk.Text(
            chat_body, bg=BG_CHAT, fg=FG, font=FONT,
            wrap='word', state='disabled', relief='flat',
            padx=4, pady=4, spacing3=6, cursor='arrow',
            yscrollcommand=chat_sb.set, height=4,
        )
        chat_sb.configure(command=self._proj_chat_text.yview)
        chat_sb.pack(side='right', fill='y')
        self._proj_chat_text.pack(side='left', fill='both', expand=True)
        self._proj_chat_text.tag_configure('user_lbl',   foreground=GREEN, font=FONT_B)
        self._proj_chat_text.tag_configure('user_txt',   foreground=FG)
        self._proj_chat_text.tag_configure('claude_lbl', foreground=ACCENT, font=FONT_B)
        self._proj_chat_text.tag_configure('claude_txt', foreground=FG)
        self._proj_chat_text.tag_configure('dim',        foreground=FG_DIM, font=FONT_S)
        self._proj_chat_text.tag_configure('memo',       foreground=MAUVE, font=FONT_S)

        self._proj_resize_after = None
        self._proj_pick_btns: dict = {}
        self._proj_selected: str | None = None
        self._proj_data: dict | None = None    # cached aggregation
        self._proj_q: queue.Queue = queue.Queue()
        self._proj_busy: bool = False
        self._proj_loaded_chat: str | None = None  # which project's chat is shown

        self._switch_tab(1)

        # ── Input bar (hidden until needed) ───────────────────────────────
        self._inp_bar = tk.Frame(self.root, bg=BG, padx=20, pady=10)
        inp_row = tk.Frame(self._inp_bar, bg=BG_INPUT)
        inp_row.pack(fill='x')

        self._inp = tk.Text(inp_row, height=2, bg=BG_INPUT, fg=FG,
                            insertbackground=FG, relief='flat', font=FONT,
                            padx=14, pady=10, wrap='word')
        self._inp.pack(side='left', fill='x', expand=True)
        self._inp.bind('<Return>', self._on_enter)

        self._send = tk.Button(inp_row, text='Send',
                               command=self._send_answer,
                               bg=ACCENT, fg='#ffffff', font=FONT_B,
                               relief='flat', padx=14, cursor='hand2', bd=0)
        self._send.pack(side='right', padx=(0, 4), pady=4, fill='y')

    def _switch_tab(self, idx: int):
        if idx == self._cur_tab_idx:
            return
        self._cur_tab_idx = idx
        for i in range(len(self._tab_frames)):
            sel = (i == idx)
            self._tab_btns[i].configure(
                fg=FG if sel else FG_DIM,
                font=('Segoe UI', 10, 'bold') if sel else ('Segoe UI', 10),
            )
            self._tab_inds[i].configure(bg=ACCENT if sel else BG)
            if sel:
                self._tab_frames[i].pack(fill='both', expand=True)
            else:
                self._tab_frames[i].pack_forget()
        # Screenshots tab (idx 2) — lazy load for the current date
        if idx == 2 and self._cur_date and self._shots_loaded_date != self._cur_date:
            self._load_screenshots(self._cur_date)
            self._shots_loaded_date = self._cur_date

    def _hist_active(self) -> bool:
        try:
            return bool(self._hist_frame.winfo_ismapped())
        except Exception:
            return False

    def _show_history(self):
        # Hide day-tab UI and projects view
        self._tab_bar.pack_forget()
        self._tab_div.pack_forget()
        self._tab_host.pack_forget()
        if self._proj_active():
            self._proj_frame.pack_forget()
            self._proj_btn.configure(bg=BG_SIDE)
        # Show history view
        self._hist_frame.pack(fill='both', expand=True)
        self._hist_btn.configure(bg=BG_INPUT)
        # Clear sidebar day selection visually
        for d, (_, all_bg, _, accent_fr) in self._date_btns.items():
            for w in all_bg:
                try: w.configure(bg=BG_SIDE)
                except Exception: pass
            try: accent_fr.configure(bg=BG_SIDE)
            except Exception: pass
        self.root.after(20, self._render_history)

    def _show_day_view(self):
        if not (self._hist_active() or self._proj_active()):
            return
        if self._hist_active():
            self._hist_frame.pack_forget()
            self._hist_btn.configure(bg=BG_SIDE)
        if self._proj_active():
            self._proj_frame.pack_forget()
            self._proj_btn.configure(bg=BG_SIDE)
        self._tab_bar.pack(fill='x', padx=24, pady=(12, 0))
        self._tab_div.pack(fill='x')
        self._tab_host.pack(fill='both', expand=True)

    def _set_hist_range(self, days: int):
        if days == self._hist_range_days:
            return
        self._hist_range_days = days
        for d, b in self._hist_range_btns.items():
            sel = (d == days)
            b.configure(bg=ACCENT if sel else BG_INPUT,
                        fg='#ffffff' if sel else FG)
        self._render_history()

    def _on_hist_resize(self, event):
        if self._hist_resize_after:
            try: self.root.after_cancel(self._hist_resize_after)
            except Exception: pass
        self._hist_resize_after = self.root.after(80, self._render_history)

    # ── Projects view ─────────────────────────────────────────────────────

    def _proj_active(self) -> bool:
        try:
            return bool(self._proj_frame.winfo_ismapped())
        except Exception:
            return False

    def _show_projects(self):
        # Hide other top-level views
        self._tab_bar.pack_forget()
        self._tab_div.pack_forget()
        self._tab_host.pack_forget()
        if self._hist_active():
            self._hist_frame.pack_forget()
            self._hist_btn.configure(bg=BG_SIDE)
        # Show
        self._proj_frame.pack(fill='both', expand=True)
        self._proj_btn.configure(bg=BG_INPUT)
        # Clear sidebar day-row visual selection
        for d, (_, all_bg, _, accent_fr) in self._date_btns.items():
            for w in all_bg:
                try: w.configure(bg=BG_SIDE)
                except Exception: pass
            try: accent_fr.configure(bg=BG_SIDE)
            except Exception: pass
        # Force fresh aggregation, then render
        self._proj_data = None
        self.root.after(20, self._rebuild_projects)

    def _set_proj_range(self, days: int):
        if days == self._proj_range_days:
            return
        self._proj_range_days = days
        for d, b in self._proj_range_btns.items():
            sel = (d == days)
            b.configure(bg=ACCENT if sel else BG_INPUT,
                        fg='#ffffff' if sel else FG)
        self._proj_data = None
        self._rebuild_projects()

    def _on_proj_resize(self, event):
        if self._proj_resize_after:
            try: self.root.after_cancel(self._proj_resize_after)
            except Exception: pass
        self._proj_resize_after = self.root.after(80, self._render_project)

    def _select_project(self, name: str):
        self._proj_selected = name
        # Update pill states
        for n, b in self._proj_pick_btns.items():
            sel = (n == name)
            b.configure(bg=ACCENT if sel else BG_INPUT,
                        fg='#ffffff' if sel else FG)
        self._render_project()
        self._proj_chat_show_history(name)

    def _proj_aggregate(self) -> dict:
        """Walk analyzed days within the active range and build per-project metrics."""
        dates = self._scan_dates()
        # Newest-first → oldest-first, then range filter (last N)
        ordered = list(reversed([d for d in dates if d['analyzed']]))
        n_range = self._proj_range_days
        if n_range > 0 and len(ordered) > n_range:
            ordered = ordered[-n_range:]

        proj_total:    dict = {}                 # name -> minutes
        proj_days:     dict = {}                 # name -> set(date)
        proj_day_min:  dict = {}                 # (name, date) -> minutes
        proj_app_min:  dict = {}                 # (name, app)  -> minutes
        proj_hour_min: dict = {}                 # (name, hour) -> minutes
        proj_sessions: dict = {}                 # name -> [(date, length_min)]
        proj_subtasks: dict = {}                 # name -> [(date, ts, te, title, length_min)]
        proj_struggles:dict = {}                 # name -> [(date, ts, te, kind, summary)]
        all_dates: list = []                     # in chronological order

        for d in ordered:
            ds = d['date']
            af = BASE_DIR / 'logs' / ds / 'analysis.md'
            sd = BASE_DIR / 'logs' / ds / 'screenshots'
            if not af.exists():
                continue
            text     = af.read_text(encoding='utf-8')
            timeline = _parse_timeline_data(text)
            if not timeline:
                continue
            all_dates.append(ds)

            # Convert timeline to (start_min, end_min, proj) for fast lookup
            blocks: list = []
            for ts, te, proj in timeline:
                try:
                    s = _hhmm_to_min(ts)
                    e = _hhmm_to_min(te)
                except Exception:
                    continue
                if e <= s:
                    continue
                length = e - s
                blocks.append((s, e, proj))
                proj_total[proj] = proj_total.get(proj, 0) + length
                proj_days.setdefault(proj, set()).add(ds)
                key = (proj, ds)
                proj_day_min[key] = proj_day_min.get(key, 0) + length
                proj_sessions.setdefault(proj, []).append((ds, length))

                # Time-of-day buckets per hour
                t = s
                while t < e:
                    h = int(t // 60) % 24
                    nxt = min((h + 1) * 60, e)
                    if nxt <= t:
                        nxt = t + 1
                    proj_hour_min[(proj, h)] = (
                        proj_hour_min.get((proj, h), 0) + (nxt - t))
                    t = nxt

            # Map sessions (apps) to projects by timeline lookup
            shots    = sorted(sd.glob('*.jpg')) if sd.exists() else []
            sessions = parse_sessions(shots)
            for s_row in sessions:
                tmin = s_row['tmin']
                proj = None
                for bs, be, p in blocks:
                    if bs <= tmin < be:
                        proj = p
                        break
                if proj is None:
                    continue
                app = s_row['app'] or 'unknown'
                key = (proj, app)
                proj_app_min[key] = proj_app_min.get(key, 0) + s_row['dur']

            # Sub-tasks
            for ts, te, proj, title in _parse_subtask_data(text):
                try:
                    s_m = _hhmm_to_min(ts)
                    e_m = _hhmm_to_min(te)
                except Exception:
                    continue
                length = max(0, e_m - s_m)
                proj_subtasks.setdefault(proj, []).append(
                    (ds, ts, te, title, length))

            # Struggles & reconciliations
            for ts, te, proj, kind, summary in _parse_struggle_data(text):
                proj_struggles.setdefault(proj, []).append(
                    (ds, ts, te, kind, summary))

        return {
            'projects':   proj_total,
            'days':       proj_days,
            'day_min':    proj_day_min,
            'app_min':    proj_app_min,
            'hour_min':   proj_hour_min,
            'sessions':   proj_sessions,
            'subtasks':   proj_subtasks,
            'struggles':  proj_struggles,
            'all_dates':  all_dates,
        }

    def _rebuild_projects(self):
        """Aggregate data, rebuild project picker pills, then render."""
        data = self._proj_aggregate()
        self._proj_data = data

        # Rebuild picker row
        for w in self._proj_pick_inner.winfo_children():
            w.destroy()
        self._proj_pick_btns.clear()

        # Rank projects by total minutes desc, exclude off-time tags
        ranked = sorted(
            ((n, m) for n, m in data['projects'].items() if not _is_off_time(n)),
            key=lambda x: -x[1])

        if not ranked:
            self._proj_selected = None
            self._render_project()
            return

        # Default selection: persist if still present, else top-ranked
        if self._proj_selected not in {n for n, _ in ranked}:
            self._proj_selected = ranked[0][0]

        for name, mins in ranked:
            sel = (name == self._proj_selected)
            hours = mins / 60.0
            pill = tk.Label(self._proj_pick_inner,
                            text=f'  {name}  · {hours:.1f}h  ',
                            bg=ACCENT if sel else BG_INPUT,
                            fg='#ffffff' if sel else FG,
                            font=('Segoe UI', 9, 'bold'),
                            padx=4, pady=6, cursor='hand2')
            pill.pack(side='left', padx=(0, 6), pady=(0, 0))
            pill.bind('<Button-1>', lambda e, n=name: self._select_project(n))
            self._proj_pick_btns[name] = pill

        self._render_project()
        if self._proj_selected:
            self._proj_chat_show_history(self._proj_selected)

    def _render_project(self):
        c = self._proj_cv
        c.delete('all')

        W = max(c.winfo_width() - 4, 760)
        PAD = 24

        if not self._proj_data or not self._proj_selected:
            c.create_text(PAD, 40,
                          text='No analyzed days yet — analyze at least one day to see project insights.',
                          fill=FG_DIM, font=FONT_S, anchor='w')
            c.configure(scrollregion=(0, 0, W, 80))
            return

        data    = self._proj_data
        name    = self._proj_selected
        total   = data['projects'].get(name, 0)
        n_days  = len(data['days'].get(name, set()))
        sess    = data['sessions'].get(name, [])
        n_sess  = len(sess)
        avg     = (sum(L for _, L in sess) / n_sess) if n_sess else 0
        longest = max((L for _, L in sess), default=0)

        # Color: pick a stable hue based on rank position
        ranked = sorted(data['projects'].items(), key=lambda x: -x[1])
        try:
            rank = next(i for i, (n, _) in enumerate(ranked) if n == name)
        except StopIteration:
            rank = 0
        col = PALETTE[rank % len(PALETTE)]

        y = PAD

        # ── Header ───────────────────────────────────────────────────────
        c.create_rectangle(PAD, y + 6, PAD + 4, y + 38, fill=col, outline='')
        c.create_text(PAD + 14, y + 8, text=name, anchor='nw',
                      fill=FG, font=('Segoe UI', 16, 'bold'))

        def _fmt_hm(mins: float) -> str:
            mins = int(mins)
            h, m = divmod(mins, 60)
            return f'{h}h {m:02d}m' if h else f'{m}m'

        kpis = [
            ('Total time',       _fmt_hm(total)),
            ('Days active',      str(n_days)),
            ('Sessions',         str(n_sess)),
            ('Avg session',      _fmt_hm(avg)),
            ('Longest session',  _fmt_hm(longest)),
        ]
        kx = PAD + 14
        ky = y + 44
        for label, val in kpis:
            c.create_text(kx, ky, text=label.upper(), anchor='nw',
                          fill=FG_DIM, font=('Segoe UI', 7, 'bold'))
            c.create_text(kx, ky + 12, text=val, anchor='nw',
                          fill=FG, font=('Segoe UI', 12, 'bold'))
            kx += 130

        y = ky + 44

        # ── Section: Hours per day ───────────────────────────────────────
        y = self._draw_section_title(c, PAD, y, 'Hours per day')

        # Use all dates as the X axis so gaps in activity are visible
        all_dates = data['all_dates']
        if all_dates:
            usable_w = W - 2 * PAD
            n        = len(all_dates)
            bar_w    = max(6, min(28, (usable_w - (n - 1) * 4) // max(1, n)))
            gap      = 4
            chart_h  = 80
            max_min  = max((data['day_min'].get((name, ds), 0) for ds in all_dates),
                           default=0) or 1

            x = PAD
            for ds in all_dates:
                v = data['day_min'].get((name, ds), 0)
                h = (v / max_min) * chart_h if v else 0
                # Track
                c.create_rectangle(x, y, x + bar_w, y + chart_h,
                                   fill=BG_INPUT, outline='')
                if h > 0:
                    c.create_rectangle(x, y + chart_h - h,
                                       x + bar_w, y + chart_h,
                                       fill=col, outline='')
                x += bar_w + gap
                if x > W - PAD:
                    break

            # X-axis bookends
            try:
                d0 = datetime.strptime(all_dates[0],  '%Y-%m-%d').strftime('%b %d')
                d1 = datetime.strptime(all_dates[-1], '%Y-%m-%d').strftime('%b %d')
            except Exception:
                d0, d1 = all_dates[0], all_dates[-1]
            c.create_text(PAD, y + chart_h + 6, text=d0, anchor='nw',
                          fill=FG_DIM, font=('Segoe UI', 8))
            c.create_text(W - PAD, y + chart_h + 6, text=d1, anchor='ne',
                          fill=FG_DIM, font=('Segoe UI', 8))
            c.create_text(W - PAD, y - 14,
                          text=f'peak {_fmt_hm(max_min)}', anchor='ne',
                          fill=FG_DIM, font=('Segoe UI', 8))
            y += chart_h + 24
        else:
            y += 8

        # ── Section: Apps used ───────────────────────────────────────────
        y = self._draw_section_title(c, PAD, y, 'Apps used inside this project')

        app_rows = sorted(
            ((a, m) for (p, a), m in data['app_min'].items() if p == name),
            key=lambda x: -x[1])[:8]
        if app_rows:
            row_h = 24
            label_col_w = 160
            bar_x  = PAD + label_col_w
            bar_w_max = W - PAD - bar_x - 80
            top_min = app_rows[0][1] or 1
            for app, m in app_rows:
                c.create_text(PAD, y + row_h / 2, text=app, anchor='w',
                              fill=FG, font=('Segoe UI', 9))
                w = (m / top_min) * bar_w_max
                c.create_rectangle(bar_x, y + 5, bar_x + bar_w_max, y + row_h - 3,
                                   fill=BG_INPUT, outline='')
                if w > 0:
                    _rrect(c, bar_x, y + 5, bar_x + max(2, w), y + row_h - 3,
                           r=4, fill=col, outline='')
                pct = (m / total * 100) if total else 0
                c.create_text(bar_x + bar_w_max + 6, y + row_h / 2,
                              text=f'{_fmt_hm(m)}  ({pct:.0f}%)',
                              anchor='w', fill=FG_DIM, font=('Segoe UI', 8))
                y += row_h
            y += 16
        else:
            c.create_text(PAD, y, text='No app data captured for this project.',
                          fill=FG_DIM, font=FONT_S, anchor='nw')
            y += 28

        # ── Section: Time of day ─────────────────────────────────────────
        y = self._draw_section_title(c, PAD, y, 'Time of day')

        usable_w = W - 2 * PAD
        cell_w   = usable_w / 24
        cell_h   = 36
        max_h    = max((data['hour_min'].get((name, h), 0) for h in range(24)),
                       default=0) or 1
        for h in range(24):
            v = data['hour_min'].get((name, h), 0)
            x1 = PAD + h * cell_w
            x2 = PAD + (h + 1) * cell_w - 2
            # Track
            c.create_rectangle(x1, y, x2, y + cell_h, fill=BG_INPUT, outline='')
            if v > 0:
                hh  = (v / max_h) * cell_h
                col_h = _lerp_color(BG_INPUT, col, 0.85)
                c.create_rectangle(x1, y + cell_h - hh, x2, y + cell_h,
                                   fill=col_h, outline='')
        # Hour ticks (every 3 hours)
        for h in (0, 3, 6, 9, 12, 15, 18, 21):
            x = PAD + h * cell_w
            c.create_text(x, y + cell_h + 4, text=f'{h:02d}',
                          anchor='nw', fill=FG_DIM, font=('Segoe UI', 7))
        y += cell_h + 26

        # ── Section: Session length distribution ────────────────────────
        y = self._draw_section_title(c, PAD, y, 'Session lengths')

        buckets = [
            ('< 15m',      lambda L: L < 15),
            ('15 – 30m',   lambda L: 15 <= L < 30),
            ('30 – 60m',   lambda L: 30 <= L < 60),
            ('1 – 2h',     lambda L: 60 <= L < 120),
            ('2h+',        lambda L: L >= 120),
        ]
        counts = [sum(1 for _, L in sess if pred(L)) for _, pred in buckets]
        bmax = max(counts) or 1
        bar_h = 80
        slot_w = (W - 2 * PAD) / len(buckets)
        for i, ((label, _), n) in enumerate(zip(buckets, counts)):
            cx = PAD + i * slot_w
            bw = min(60, slot_w * 0.65)
            bx = cx + (slot_w - bw) / 2
            h  = (n / bmax) * bar_h
            c.create_rectangle(bx, y, bx + bw, y + bar_h, fill=BG_INPUT, outline='')
            if h > 0:
                c.create_rectangle(bx, y + bar_h - h, bx + bw, y + bar_h,
                                   fill=col, outline='')
            c.create_text(cx + slot_w / 2, y - 8, text=str(n),
                          fill=FG, font=('Segoe UI', 9, 'bold'))
            c.create_text(cx + slot_w / 2, y + bar_h + 6, text=label,
                          anchor='n', fill=FG_DIM, font=('Segoe UI', 8))
        y += bar_h + 32

        # ── Section: Top sub-tasks ───────────────────────────────────────
        sub_rows = data.get('subtasks', {}).get(name, [])
        if sub_rows:
            y = self._draw_section_title(c, PAD, y, 'Top sub-tasks')
            # Aggregate by title (sum minutes, count occurrences)
            agg: dict = {}
            for ds, ts, te, title, length in sub_rows:
                rec = agg.setdefault(title, {'min': 0, 'n': 0, 'last': ds})
                rec['min']  += length
                rec['n']    += 1
                if ds > rec['last']:
                    rec['last'] = ds
            ranked = sorted(agg.items(), key=lambda x: -x[1]['min'])[:8]
            top_min = ranked[0][1]['min'] or 1
            row_h = 24
            label_col_w = min(360, int((W - 2 * PAD) * 0.55))
            bar_x = PAD + label_col_w
            bar_w_max = W - PAD - bar_x - 110
            for title, rec in ranked:
                t_short = (title[:60] + '…') if len(title) > 61 else title
                c.create_text(PAD, y + row_h / 2, text=t_short,
                              anchor='w', fill=FG, font=('Segoe UI', 9))
                w = (rec['min'] / top_min) * bar_w_max
                c.create_rectangle(bar_x, y + 5, bar_x + bar_w_max, y + row_h - 3,
                                   fill=BG_INPUT, outline='')
                if w > 0:
                    _rrect(c, bar_x, y + 5, bar_x + max(2, w), y + row_h - 3,
                           r=4, fill=col, outline='')
                meta = (f'{_fmt_hm(rec["min"])}  ·  '
                        f'{rec["n"]}× · last {rec["last"][5:]}')
                c.create_text(bar_x + bar_w_max + 6, y + row_h / 2,
                              text=meta,
                              anchor='w', fill=FG_DIM, font=('Segoe UI', 8))
                y += row_h
            y += 16

        # ── Section: Struggles & reconciliations ─────────────────────────
        strg_rows = data.get('struggles', {}).get(name, [])
        if strg_rows:
            y = self._draw_section_title(
                c, PAD, y, 'Struggles & reconciliations')
            n_recon = sum(1 for _, _, _, k, _ in strg_rows if k == 'reconciliation')
            n_strg  = sum(1 for _, _, _, k, _ in strg_rows if k == 'struggle')
            n_blk   = sum(1 for _, _, _, k, _ in strg_rows if k == 'blocker')
            summary_parts = []
            if n_recon: summary_parts.append(f'{n_recon} reconciliation{"s" if n_recon != 1 else ""}')
            if n_strg:  summary_parts.append(f'{n_strg} struggle{"s" if n_strg != 1 else ""}')
            if n_blk:   summary_parts.append(f'{n_blk} blocker{"s" if n_blk != 1 else ""}')
            if summary_parts:
                c.create_text(PAD, y, text=' · '.join(summary_parts),
                              anchor='nw', fill=FG_DIM, font=('Segoe UI', 9))
                y += 22

            kind_color = {
                'reconciliation': MAUVE,
                'struggle':       YELLOW,
                'blocker':        RED,
            }
            # Most recent first, cap at 12
            for ds, ts, te, kind, summary in sorted(
                    strg_rows, key=lambda r: (r[0], r[1]), reverse=True)[:12]:
                kc = kind_color.get(kind, FG_DIM)
                # Date + range tag
                date_lbl = ds[5:]
                c.create_text(PAD, y + 8, text=date_lbl,
                              anchor='nw', fill=FG_DIM,
                              font=('Segoe UI', 8))
                c.create_text(PAD + 50, y + 8, text=f'{ts}–{te}',
                              anchor='nw', fill=FG_DIM,
                              font=('Segoe UI', 8))
                # Kind chip
                kx1 = PAD + 110
                kw  = max(58, len(kind) * 6 + 14)
                _rrect(c, kx1, y + 4, kx1 + kw, y + 20, r=4,
                       fill=_lerp_color(kc, BG_CHAT, 0.65), outline='')
                c.create_text(kx1 + kw / 2, y + 12, text=kind,
                              fill=kc, font=('Segoe UI', 7, 'bold'))
                # Summary
                sx = kx1 + kw + 10
                sw = W - PAD - sx
                # Truncate to fit roughly
                approx_chars = max(20, int(sw / 6.4))
                s_short = summary if len(summary) <= approx_chars \
                    else summary[:approx_chars - 1] + '…'
                c.create_text(sx, y + 8, text=s_short,
                              anchor='nw', fill=FG, font=('Segoe UI', 9))
                y += 26
            y += 12

        c.configure(scrollregion=(0, 0, W, y))

    def _draw_section_title(self, c, x, y, text):
        c.create_text(x, y, text=text.upper(), anchor='nw',
                      fill=FG_DIM, font=('Segoe UI', 8, 'bold'))
        c.create_line(x, y + 18, x + 28, y + 18, fill=ACCENT, width=2)
        return y + 28

    # ══════════════════════════════════════════════════════════════════════
    # Project chat (per-project conversation with persistent memory)
    # ══════════════════════════════════════════════════════════════════════

    def _proj_slug(self, name: str) -> str:
        s = re.sub(r'[^a-zA-Z0-9]+', '-', name.strip().lower()).strip('-')
        return s or 'untitled'

    def _proj_dir(self, name: str) -> Path:
        p = BASE_DIR / 'logs' / '_projects' / self._proj_slug(name)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _proj_memory_path(self, name: str) -> Path:
        return self._proj_dir(name) / 'memory.md'

    def _proj_chat_path(self, name: str) -> Path:
        return self._proj_dir(name) / 'chat.jsonl'

    def _proj_load_memory(self, name: str) -> str:
        p = self._proj_memory_path(name)
        return p.read_text(encoding='utf-8') if p.exists() else ''

    def _proj_load_turns(self, name: str, n_recent: int = 30) -> list:
        p = self._proj_chat_path(name)
        if not p.exists():
            return []
        turns = []
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except Exception:
                continue
        return turns[-n_recent:]

    def _proj_save_turn(self, name: str, role: str, content: str):
        rec = {
            'ts':      datetime.now().isoformat(timespec='seconds'),
            'role':    role,
            'content': content,
        }
        with self._proj_chat_path(name).open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    def _proj_append_memo(self, name: str, memo: str):
        p = self._proj_memory_path(name)
        today = date.today().strftime('%Y-%m-%d')
        block = f'\n## {today}\n{memo.strip()}\n'
        existing = p.read_text(encoding='utf-8') if p.exists() else f'# Memory — {name}\n'
        p.write_text(existing + block, encoding='utf-8')

    def _proj_extract_memo(self, text: str) -> tuple[str, str]:
        """Strip <MEMO>...</MEMO> from response. Returns (clean_text, memo_text)."""
        m = re.search(r'<MEMO>([\s\S]*?)</MEMO>', text, re.IGNORECASE)
        if not m:
            return text.strip(), ''
        memo = m.group(1).strip()
        clean = re.sub(r'<MEMO>[\s\S]*?</MEMO>', '', text, flags=re.IGNORECASE).strip()
        return clean, memo

    def _proj_chat_clear(self):
        self._proj_chat_text.configure(state='normal')
        self._proj_chat_text.delete('1.0', 'end')
        self._proj_chat_text.configure(state='disabled')

    def _proj_chat_write(self, text: str, tag: str = 'claude_txt'):
        self._proj_chat_text.configure(state='normal')
        self._proj_chat_text.insert('end', text, tag)
        self._proj_chat_text.configure(state='disabled')
        self._proj_chat_text.see('end')

    def _proj_chat_show_history(self, name: str):
        if self._proj_loaded_chat == name:
            return
        self._proj_chat_clear()
        self._proj_chat_title.configure(text=f'Chat about {name}')
        memory = self._proj_load_memory(name).strip()
        notes = sum(1 for line in memory.splitlines() if line.strip().startswith('-'))
        if notes:
            self._proj_chat_write(
                f'{notes} note{"s" if notes != 1 else ""} in project memory · '
                f'history saved per turn.\n\n', 'dim')
        turns = self._proj_load_turns(name, n_recent=20)
        if not turns:
            self._proj_chat_write(
                f'No previous chat for {name}. Ask anything — what did I work on, '
                f'what should I focus on next, what is this project about, etc.\n',
                'dim')
        else:
            for t in turns:
                if t.get('role') == 'user':
                    self._proj_chat_write('You\n',                    'user_lbl')
                    self._proj_chat_write(t.get('content', '').strip() + '\n\n', 'user_txt')
                else:
                    self._proj_chat_write('Claude\n',                 'claude_lbl')
                    self._proj_chat_write(t.get('content', '').strip() + '\n\n', 'claude_txt')
        self._proj_loaded_chat = name

    def _proj_build_context(self, name: str) -> str:
        data = self._proj_data or self._proj_aggregate()
        total   = data['projects'].get(name, 0)
        n_days  = len(data['days'].get(name, set()))
        sess    = data['sessions'].get(name, [])
        n_sess  = len(sess)
        avg     = (sum(L for _, L in sess) / n_sess) if n_sess else 0
        longest = max((L for _, L in sess), default=0)

        apps = sorted(
            ((a, m) for (p, a), m in data['app_min'].items() if p == name),
            key=lambda x: -x[1])

        recent = sorted(data['days'].get(name, set()))[-14:]
        daily = []
        for ds in recent:
            m = data['day_min'].get((name, ds), 0)
            if m > 0:
                h, mm = divmod(int(m), 60)
                daily.append(f'  - {ds}: {h}h {mm:02d}m')

        hours = []
        for h in range(24):
            v = data['hour_min'].get((name, h), 0)
            if v > 5:
                hh, mm = divmod(int(v), 60)
                hours.append(f'{h:02d}:00→{hh}h{mm:02d}m')

        memory = self._proj_load_memory(name).strip()

        parts = [
            f'You are an assistant helping the user analyze their work on a '
            f'project they call "{name}". Pulsar tracks their screen activity '
            f'and groups time into projects automatically.',
            '',
            'Tracked metrics for this project:',
            f'  - total time:      {int(total/60)}h {int(total%60):02d}m',
            f'  - days active:     {n_days}',
            f'  - sessions:        {n_sess}',
            f'  - avg session:     {int(avg)}m',
            f'  - longest session: {int(longest)}m',
        ]
        if apps:
            parts.append('  - top apps inside this project:')
            for a, m in apps[:6]:
                parts.append(f'      * {a}: {int(m)}m')
        if daily:
            parts.append('  - recent daily breakdown:')
            parts.extend(daily)
        if hours:
            parts.append(f'  - active hours: {", ".join(hours[:8])}')

        parts.append('')
        parts.append('User-curated notes accumulated over past conversations '
                     '(persistent project memory):')
        parts.append(memory if memory else '  (none yet)')
        parts.append('')
        parts.append(
            'Be concise, specific, and reference the data above when relevant. '
            'Skip pleasantries.'
        )
        parts.append('')
        parts.append(
            'When the user shares anything worth remembering long-term about '
            "this project — goals, decisions, blockers, deadlines, technical "
            "details, people's names, links, conclusions — wrap a short summary "
            'as bullet points inside <MEMO>...</MEMO> at the very end of your '
            'reply. Each bullet must start with "- " and be a single short fact. '
            'Put nothing except those bullets inside the tags. The memo block '
            'is hidden from the user; it gets appended to persistent project '
            'memory. Only output a memo when there is something concrete and '
            'new to remember; omit the tags otherwise.'
        )
        return '\n'.join(parts)

    def _on_proj_enter(self, event):
        if event.state & 0x0001:    # Shift+Enter → newline
            return
        self._proj_send_msg()
        return 'break'

    def _proj_send_msg(self):
        if self._proj_busy or not self._proj_selected:
            return
        text = self._proj_inp.get('1.0', 'end').strip()
        if not text:
            return
        name = self._proj_selected
        self._proj_inp.delete('1.0', 'end')
        self._proj_busy = True
        self._proj_send.configure(state='disabled')
        self._proj_chat_status.configure(text='thinking…')

        self._proj_chat_write('You\n',          'user_lbl')
        self._proj_chat_write(text + '\n\n',    'user_txt')
        self._proj_chat_write('Claude\n',       'claude_lbl')

        threading.Thread(target=self._proj_thread,
                         args=(name, text), daemon=True).start()

    def _proj_thread(self, name: str, user_text: str):
        try:
            client = self._client()
            sys_prompt = self._proj_build_context(name)
            history = self._proj_load_turns(name, n_recent=20)
            msgs = [{'role': t['role'], 'content': t['content']} for t in history
                    if t.get('role') in ('user', 'assistant')]
            msgs.append({'role': 'user', 'content': user_text})

            resp = client.messages.create(
                model='claude-opus-4-7',
                max_tokens=4096,
                system=sys_prompt,
                messages=msgs,
            )
            full = ''
            if resp.content:
                # Concatenate any text blocks
                full = ''.join(getattr(b, 'text', '') for b in resp.content)
            clean, memo = self._proj_extract_memo(full)

            self._proj_save_turn(name, 'user',      user_text)
            self._proj_save_turn(name, 'assistant', clean)
            if memo:
                self._proj_append_memo(name, memo)

            self._proj_q.put(('reply', clean, memo))
        except Exception as exc:
            self._proj_q.put(('error', str(exc)))

    def _proj_pump(self):
        try:
            while True:
                evt = self._proj_q.get_nowait()
                kind = evt[0]
                if kind == 'reply':
                    _, clean, memo = evt
                    self._proj_chat_write((clean or '(empty reply)').strip() + '\n',
                                          'claude_txt')
                    if memo:
                        n = sum(1 for line in memo.splitlines()
                                if line.strip().startswith('-'))
                        if n:
                            self._proj_chat_write(
                                f'  · saved {n} note{"s" if n != 1 else ""} to project memory\n',
                                'memo')
                    self._proj_chat_write('\n', 'claude_txt')
                    self._proj_busy = False
                    self._proj_send.configure(state='normal')
                    self._proj_chat_status.configure(text='')
                elif kind == 'error':
                    self._proj_chat_write(f'\n[error: {evt[1]}]\n\n', 'dim')
                    self._proj_busy = False
                    self._proj_send.configure(state='normal')
                    self._proj_chat_status.configure(text='')
        except queue.Empty:
            pass
        self.root.after(80, self._proj_pump)

    # ══════════════════════════════════════════════════════════════════════
    # Capture management
    # ══════════════════════════════════════════════════════════════════════

    def _capture_running(self) -> bool:
        if self._cap_proc and self._cap_proc.poll() is None:
            return True
        # Also check if capture.py is running from another source (Task Scheduler etc.)
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower():
                    if any('capture.py' in (a or '') for a in (p.info.get('cmdline') or [])):
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def _start_capture(self):
        if self._capture_running():
            return
        self._cap_proc = subprocess.Popen(
            [PYTHON, CAPTURE],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Surface today's date in the sidebar immediately, before the first shot lands
        today_str = date.today().strftime('%Y-%m-%d')
        try:
            (BASE_DIR / 'logs' / today_str / 'screenshots').mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._scan_dates(force=True)
        self._render_dates()
        if today_str != self._cur_date:
            self._select_date(today_str)
        # Follow-ups so the first screenshots show up promptly
        for delay in (5_000, 15_000, 60_000):
            self.root.after(delay, self._render_dates)

    def _stop_capture(self):
        if self._cap_proc:
            self._cap_proc.terminate()
            self._cap_proc = None
        # Also stop any external instances
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower():
                    if any('capture.py' in (a or '') for a in (p.info.get('cmdline') or [])):
                        p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _toggle_capture(self):
        if self._capture_running():
            self._stop_capture()
        else:
            self._start_capture()

    def _check_capture(self):
        running = self._capture_running()
        if running:
            # Muted indicator: small dot in a soft red, text stays neutral
            soft_red = _lerp_color(RED, BG_SIDE, 0.35)
            self._cap_dot.configure(text='●', fg=soft_red)
            self._cap_btn.configure(text='Recording')
        else:
            if self._cap_proc is not None:
                self._cap_proc = None
            self._cap_dot.configure(text='○', fg=FG_DIM)
            self._cap_btn.configure(text='Record')
        self.root.after(2000, self._check_capture)

    # ══════════════════════════════════════════════════════════════════════
    # Date sidebar
    # ══════════════════════════════════════════════════════════════════════

    def _scan_dates(self, force: bool = False) -> list[dict]:
        logs = BASE_DIR / 'logs'
        if not logs.exists():
            return []
        today_str = date.today().strftime('%Y-%m-%d')
        # Cache key includes each screenshots dir mtime + analysis.md mtime
        # so live shot additions AND new/changed analyses both invalidate
        try:
            parts = [str(logs.stat().st_mtime)]
            for d in logs.iterdir():
                if d.is_dir():
                    sd = d / 'screenshots'
                    if sd.exists():
                        parts.append(f'{d.name}:s:{sd.stat().st_mtime}')
                    af = d / 'analysis.md'
                    parts.append(
                        f'{d.name}:a:{af.stat().st_mtime}' if af.exists()
                        else f'{d.name}:a:none')
            key = '|'.join(parts)
        except OSError:
            key = ''
        if not force and key == self._dates_cache_key and self._dates_cache:
            return self._dates_cache
        result = []
        for d in sorted(logs.iterdir(), reverse=True):
            if not (d.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}', d.name)):
                continue
            shots_dir = d / 'screenshots'
            shots     = list(shots_dir.glob('*.jpg')) if shots_dir.exists() else []
            # Always include today (so it shows the moment recording starts, before the first shot lands)
            if not shots and d.name != today_str:
                continue
            result.append({
                'date':     d.name,
                'n_shots':  len(shots),
                'analyzed': (d / 'analysis.md').exists(),
            })
        self._dates_cache     = result
        self._dates_cache_key = key
        return result

    def _refresh_dates(self):
        self._render_dates()
        self.root.after(30_000, self._refresh_dates)

    def _render_dates(self):
        dates = self._scan_dates()

        current = {d['date'] for d in dates}
        for ds in list(self._date_btns):
            if ds not in current:
                self._date_btns[ds][0].destroy()
                del self._date_btns[ds]

        today_str = date.today().strftime('%Y-%m-%d')
        yest_str  = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')

        for info in dates:
            ds       = info['date']
            analyzed = info['analyzed']
            n        = info['n_shots']

            if ds in self._date_btns:
                fr, all_bg, count_lbl, accent_fr = self._date_btns[ds]
                count_lbl.configure(
                    text='✓' if analyzed else str(n),
                    fg=GREEN if analyzed else FG_DIM,
                )
            else:
                # Human-readable primary label
                if ds == today_str:
                    prim = 'Today'
                elif ds == yest_str:
                    prim = 'Yesterday'
                else:
                    try:
                        dt   = datetime.strptime(ds, '%Y-%m-%d')
                        prim = dt.strftime('%A')
                    except Exception:
                        prim = ds

                try:
                    dt    = datetime.strptime(ds, '%Y-%m-%d')
                    day_n = str(int(dt.strftime('%d')))
                    sec   = f"{dt.strftime('%b')} {day_n}, {dt.strftime('%Y')}"
                except Exception:
                    sec = ds

                fr = tk.Frame(self._side_inner, bg=BG_SIDE, cursor='hand2')
                fr.pack(fill='x')

                accent_fr = tk.Frame(fr, bg=BG_SIDE, width=3)
                accent_fr.pack(side='left', fill='y')
                accent_fr.pack_propagate(False)

                inner = tk.Frame(fr, bg=BG_SIDE, cursor='hand2', padx=10)
                inner.pack(side='left', fill='x', expand=True, pady=10)

                prim_lbl = tk.Label(inner, text=prim, bg=BG_SIDE, fg=FG,
                                    font=('Segoe UI', 10), anchor='w')
                prim_lbl.pack(fill='x')
                sec_lbl  = tk.Label(inner, text=sec, bg=BG_SIDE, fg=FG_DIM,
                                    font=('Segoe UI', 8), anchor='w')
                sec_lbl.pack(fill='x')

                count_lbl = tk.Label(fr,
                    text='✓' if analyzed else str(n),
                    bg=BG_SIDE, fg=GREEN if analyzed else FG_DIM,
                    font=('Segoe UI', 9), padx=10)
                count_lbl.pack(side='right')

                all_bg = [fr, inner, prim_lbl, sec_lbl, count_lbl]
                enter_fn, leave_fn = _make_row_cbs(ds, all_bg, accent_fr, self)
                for w in all_bg + [accent_fr]:
                    w.bind('<Button-1>', lambda e, d=ds: self._select_date(d))
                    w.bind('<Enter>', enter_fn)
                    w.bind('<Leave>', leave_fn)

                self._date_btns[ds] = (fr, all_bg, count_lbl, accent_fr)

        # Midnight crossing: auto-switch to the new day when it gets screenshots
        new_today = date.today().strftime('%Y-%m-%d')
        if new_today != self._today:
            self._today = new_today
            if any(d['date'] == new_today for d in dates):
                self._select_date(new_today)

    def _select_date(self, ds: str):
        self._show_day_view()
        for d, (fr, all_bg, count_lbl, accent_fr) in self._date_btns.items():
            sel    = (d == ds)
            row_bg = BG_INPUT if sel else BG_SIDE
            acc_bg = ACCENT   if sel else BG_SIDE
            for w in all_bg:
                try: w.configure(bg=row_bg)
                except Exception: pass
            try: accent_fr.configure(bg=acc_bg)
            except Exception: pass

        self._cur_date = ds
        self._state    = 'idle'
        self._hide_input()

        # Update title bar date label
        try:
            today_s = date.today().strftime('%Y-%m-%d')
            yest_s  = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
            if ds == today_s:
                lbl_txt = 'Today'
            elif ds == yest_s:
                lbl_txt = 'Yesterday'
            else:
                dt    = datetime.strptime(ds, '%Y-%m-%d')
                day_n = str(int(dt.strftime('%d')))
                lbl_txt = f"{dt.strftime('%A')}, {day_n} {dt.strftime('%B')}"
            self._date_lbl.configure(text=lbl_txt)
        except Exception:
            self._date_lbl.configure(text=ds)

        info = next((x for x in self._scan_dates() if x['date'] == ds), None)
        if not info:
            return

        # Clear stale thumbnails immediately; actual loading deferred to tab open
        self._shots_loaded_date = ''
        for w in self._shots_inner.winfo_children():
            w.destroy()
        self._shot_refs.clear()

        if info['analyzed']:
            self._load_existing_analysis(ds)
        else:
            self._show_analyze_prompt(ds)

    def _auto_select_initial(self):
        dates = self._scan_dates()
        if not dates:
            return
        today = date.today().strftime('%Y-%m-%d')
        target = next((d['date'] for d in dates if d['date'] == today), None)
        if target is None:
            target = dates[0]['date']  # most recent available day
        self._select_date(target)

    # ══════════════════════════════════════════════════════════════════════
    # Content display
    # ══════════════════════════════════════════════════════════════════════

    def _load_existing_analysis(self, ds: str):
        analysis_file = BASE_DIR / 'logs' / ds / 'analysis.md'
        shots_dir     = BASE_DIR / 'logs' / ds / 'screenshots'

        self._clear_chat()
        self._diag.delete('all')
        text = analysis_file.read_text(encoding='utf-8') if analysis_file.exists() else ''
        for line in text.split('\n'):
            if line.startswith('#'):
                self._write(line + '\n', 'h')
            else:
                self._write(line + '\n', 'body')

        time_data     = _parse_time_data(text)
        timeline_data = _parse_timeline_data(text)
        subtask_data  = _parse_subtask_data(text)
        if time_data:
            shot_paths = sorted(shots_dir.glob('*.jpg')) if shots_dir.exists() else []
            sessions   = parse_sessions(shot_paths) if shot_paths else []
            self._render_charts(time_data, timeline_data, sessions, subtask_data)
        else:
            # Fallback for old Mermaid-format analyses
            mmd_file = BASE_DIR / 'logs' / ds / 'workflow.mmd'
            mmd = (mmd_file.read_text(encoding='utf-8')
                   if mmd_file.exists() else _extract_mermaid(text))
            if mmd:
                self._render_mermaid(mmd)

        self._switch_tab(1)
        self._status.configure(text=f'{ds} — loaded')

    def _show_analyze_prompt(self, ds: str):
        self._clear_chat()
        self._diag.delete('all')
        self._write(f'\nNo analysis yet for {ds}.\n\n', 'dim')

        btn = tk.Button(self._chat, text=f'Analyze {ds}',
                        command=lambda: self._start_analysis(ds),
                        bg=ACCENT, fg='#ffffff',
                        font=('Segoe UI', 10, 'bold'),
                        relief='flat', padx=20, pady=8, cursor='hand2', bd=0)
        self._chat.configure(state='normal')
        self._chat.window_create('end', window=btn)
        self._chat.configure(state='disabled')
        self._switch_tab(0)

    def _load_screenshots(self, ds: str):
        shots_dir = BASE_DIR / 'logs' / ds / 'screenshots'
        shots     = sorted(shots_dir.glob('*.jpg')) if shots_dir.exists() else []

        for w in self._shots_inner.winfo_children():
            w.destroy()
        self._shot_refs.clear()

        THUMB_W = 150
        COLS    = 3
        for i, shot in enumerate(shots):
            row, col = divmod(i, COLS)
            try:
                img   = Image.open(shot)
                ratio = THUMB_W / img.width
                img   = img.resize((THUMB_W, int(img.height * ratio)), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._shot_refs.append(photo)

                cell = tk.Frame(self._shots_inner, bg=BG_CARD,
                                padx=2, pady=2)
                cell.grid(row=row, column=col, padx=8, pady=8, sticky='n')

                btn = tk.Button(cell, image=photo, bg=BG_CARD, relief='flat',
                                cursor='hand2', bd=0,
                                command=lambda p=shot: self._view_screenshot(p))
                btn.pack()

                ts = shot.stem.split('__')[0].replace('-', ':')
                tk.Label(cell, text=ts, bg=BG_CARD, fg=FG_DIM,
                         font=('Segoe UI', 8)).pack(pady=(2, 0))
            except Exception:
                continue

    def _view_screenshot(self, path: Path):
        top = tk.Toplevel(self.root)
        top.title(path.stem)
        top.configure(bg=BG)

        img = Image.open(path)
        # Fit to 80% of screen
        sw  = int(self.root.winfo_screenwidth()  * 0.8)
        sh  = int(self.root.winfo_screenheight() * 0.8)
        img.thumbnail((sw, sh), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)

        lbl = tk.Label(top, image=photo, bg=BG)
        lbl.image = photo   # prevent GC
        lbl.pack(padx=8, pady=8)
        top.bind('<Escape>', lambda e: top.destroy())

    # ══════════════════════════════════════════════════════════════════════
    # Analysis workers
    # ══════════════════════════════════════════════════════════════════════

    def _start_analysis(self, ds: str):
        if self._state not in ('idle',):
            return
        shots_dir = BASE_DIR / 'logs' / ds / 'screenshots'
        if not shots_dir.exists() or not any(shots_dir.glob('*.jpg')):
            self._status.configure(text=f'No screenshots for {ds}')
            return
        self._state    = 'analyzing'
        self._cur_date = ds
        self._clear_chat()
        self._diag.delete('all')
        self._status.configure(text=f'Analyzing {ds}…')
        threading.Thread(target=self._phase1,
                         args=(ds, shots_dir), daemon=True).start()

    def _phase1(self, ds: str, shots_dir: Path):
        try:
            client     = self._client()
            kf_n       = self._cfg.getint('analysis', 'keyframes_per_day', fallback=30)
            shot_paths = sorted(shots_dir.glob('*.jpg'))
            sessions   = parse_sessions(shot_paths)
            session_table = format_session_table(sessions)
            shots      = load_screenshots(shots_dir)
            kframes    = select_keyframes(shots, kf_n)
            know       = load_knowledge()

            self._sessions = sessions   # store for _phase2
            self._q.put(('lbl', 'Claude'))
            msgs     = [{'role': 'user',
                         'content': build_initial_content(kframes, ds, know, session_table)}]
            response = self._stream(client, msgs)
            analysis, questions = split_response(response)
            api_msgs = msgs + [{'role': 'assistant', 'content': response}]

            if questions:
                self._emit_questions(questions, shots_dir)
                self._q.put(('waiting', questions, api_msgs))
            else:
                _save(ds, analysis)
                time_data     = _parse_time_data(analysis)
                timeline_data = _parse_timeline_data(analysis)
                subtask_data  = _parse_subtask_data(analysis)
                if time_data:
                    self._q.put(('chart', time_data, timeline_data, sessions, subtask_data))
                self._q.put(('done', ds))

        except Exception as exc:
            self._q.put(('error', str(exc)))

    def _send_answer(self):
        if self._state != 'waiting':
            return
        text = self._inp.get('1.0', 'end').strip()
        if not text:
            return
        self._hide_input()
        self._inp.delete('1.0', 'end')
        self._q.put(('user_echo', text))
        self._status.configure(text='Finalizing…')
        threading.Thread(target=self._phase2, args=(text,), daemon=True).start()

    def _phase2(self, answer: str):
        try:
            client   = self._client()
            api_msgs = self._api_msgs + [{
                'role': 'user',
                'content': (
                    f'Clarifications:\n{answer}\n\n'
                    'Now produce the final complete analysis incorporating these answers. '
                    'Do not include a Questions section.'
                ),
            }]
            self._q.put(('lbl', 'Claude'))
            final = self._stream(client, api_msgs)
            append_knowledge(self._cur_date, self._questions, answer)
            _save(self._cur_date, final)
            time_data     = _parse_time_data(final)
            timeline_data = _parse_timeline_data(final)
            subtask_data  = _parse_subtask_data(final)
            if time_data:
                self._q.put(('chart', time_data, timeline_data, self._sessions, subtask_data))
            self._q.put(('done', self._cur_date))
        except Exception as exc:
            self._q.put(('error', str(exc)))

    def _stream(self, client: anthropic.Anthropic, messages: list) -> str:
        result = ''
        with client.messages.stream(
            model='claude-opus-4-7',
            max_tokens=16000,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                result += chunk
                self._q.put(('chunk', chunk))
        self._q.put(('nl',))
        return result

    def _emit_questions(self, section: str, shots_dir: Path):
        self._q.put(('nl',))
        self._q.put(('lbl', 'Claude — questions'))
        for line in section.split('\n'):
            if line.startswith('##') or 'reference the specific' in line.lower():
                continue
            for ts in re.findall(r'\b(\d{1,2}:\d{2})\b', line):
                shot = _closest_shot(shots_dir, ts)
                if shot:
                    thumb = _thumbnail(shot, 300)
                    if thumb:
                        self._q.put(('thumb', thumb, shot))
            if line.strip():
                self._q.put(('q_line', line))

    # ══════════════════════════════════════════════════════════════════════
    # Diagram
    # ══════════════════════════════════════════════════════════════════════

    def _render_mermaid(self, mmd: str):
        nodes, edges = _parse_mermaid(mmd)
        if not nodes:
            return
        pos = _layout(nodes, edges)
        if not pos:
            return

        min_x = min(x for x, _ in pos.values())
        min_y = min(y for _, y in pos.values())
        pos = {n: (x - min_x + D_PAD + BOX_W // 2,
                   y - min_y + D_PAD + BOX_H // 2)
               for n, (x, y) in pos.items()}

        c = self._diag
        c.delete('all')

        for a, b, label in edges:
            if a not in pos or b not in pos:
                continue
            ax, ay = pos[a]
            bx, by = pos[b]
            y1, y2 = ay + BOX_H // 2, by - BOX_H // 2
            mid_y  = (y1 + y2) / 2
            c.create_line(ax, y1, ax, mid_y, bx, mid_y, bx, y2,
                          fill=EDGE_COL, width=1.5, arrow='last',
                          joinstyle='round')
            if label:
                c.create_text((ax + bx) // 2 + 4, mid_y - 8,
                              text=label, fill=FG_DIM, font=FONT_S, anchor='w')

        for nid, (x, y) in pos.items():
            x1, y1 = x - BOX_W // 2, y - BOX_H // 2
            x2, y2 = x + BOX_W // 2, y + BOX_H // 2
            c.create_rectangle(x1, y1, x2, y2,
                               fill=BG_INPUT, outline=ACCENT, width=1)
            c.create_text(x, y, text=nodes.get(nid, nid),
                          fill=FG, font=FONT_S,
                          width=BOX_W - 14, justify='center')

        c.configure(scrollregion=c.bbox('all'))

    def _render_time_chart(self, time_data):
        self._render_charts(time_data, [], [], [])

    def _render_charts(self, time_data, timeline_data, sessions, subtask_data=None):
        c = self._diag
        c.delete('all')
        self._chart_tips.clear()
        self._proj_canvas_items.clear()
        self._hovered_proj = None
        self._chart_time_data     = list(time_data)
        self._chart_timeline_data = list(timeline_data)
        self._chart_sessions      = list(sessions)
        self._chart_subtask_data  = list(subtask_data or [])
        if not time_data:
            c.create_text(24, 24, text='No chart data yet.',
                          fill=FG_DIM, font=FONT_S, anchor='nw')
            return

        W = max(self._diag.winfo_width() - 8, 760)

        # Work projects → PALETTE; off-time (lunch/break/away) → muted gray-blue
        proj_color = {}
        work_i = 0
        for name, _ in time_data:
            if _is_off_time(name):
                proj_color[name] = OFF_COL
            else:
                proj_color[name] = PALETTE[work_i % len(PALETTE)]
                work_i += 1

        # 1. Compact stats strip — always visible at the top
        y = 16
        y = self._draw_brief(c, W, y, time_data, sessions, proj_color)
        y += 16

        # 2. Timeline + rhythm — primary view, no scrolling needed
        y = self._draw_timeline_section(c, W, y, sessions, timeline_data,
                                        proj_color, self._chart_subtask_data)
        y += 8
        y = self._draw_rhythm(c, W, y, sessions, timeline_data)
        y += 28

        c.create_line(16, y, W-16, y, fill=EDGE_COL, width=1)
        y += 20

        # 3. Time breakdown bars — secondary
        y = self._draw_bars(c, 16, y, W-32, time_data, sessions, timeline_data, proj_color)
        y += 18

        c.configure(scrollregion=(0, 0, W, y))

    # ── Tooltip ────────────────────────────────────────────────────────────

    def _show_tooltip(self, text: str, rx: int, ry: int):
        self._hide_tooltip()
        self._tip = tk.Toplevel(self.root)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_attributes('-topmost', True)

        # Card: thin EDGE_COL border → white interior → accent left stripe
        outer = tk.Frame(self._tip, bg=EDGE_COL, padx=1, pady=1)
        outer.pack(fill='both', expand=True)
        inner = tk.Frame(outer, bg=BG_CARD)
        inner.pack(fill='both', expand=True)
        tk.Frame(inner, bg=ACCENT, width=3).pack(side='left', fill='y')
        tk.Label(inner, text=text, bg=BG_CARD, fg=FG,
                 font=('Segoe UI', 9), padx=12, pady=9,
                 wraplength=280, justify='left').pack(side='left')

        # Measure then clamp to screen so it never overflows
        self._tip.update_idletasks()
        tw = self._tip.winfo_reqwidth()
        th = self._tip.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = rx + 16
        y  = ry + 16
        if x + tw > sw - 8:
            x = rx - tw - 10
        if y + th > sh - 8:
            y = ry - th - 10
        self._tip.geometry(f'+{x}+{y}')

    def _hide_tooltip(self):
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
        self._last_tip = None

    def _on_diag_motion(self, event):
        cx = self._diag.canvasx(event.x)
        cy = self._diag.canvasy(event.y)

        hit_proj = None
        hit_tip  = None
        for x1, y1, x2, y2, tip, proj_name in self._chart_tips:
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                hit_proj = proj_name
                hit_tip  = tip
                break

        # Project cross-section highlighting
        if hit_proj != self._hovered_proj:
            if self._hovered_proj is not None:
                self._unhighlight_proj()
            if hit_proj is not None:
                self._highlight_proj(hit_proj)
            self._hovered_proj = hit_proj

        # Tooltip: rich for projects, simple for other zones, hide when empty
        if hit_proj is not None:
            rich = self._build_proj_tip(hit_proj)
            if rich != self._last_tip:
                self._last_tip = rich
                self._show_tooltip(rich, event.x_root, event.y_root)
        elif hit_tip is not None:
            if hit_tip != self._last_tip:
                self._last_tip = hit_tip
                self._show_tooltip(hit_tip, event.x_root, event.y_root)
        elif self._last_tip is not None:
            self._hide_tooltip()

    def _highlight_proj(self, proj: str):
        c = self._diag
        for p, items in self._proj_canvas_items.items():
            for item_id, kind, col, orig_fill, orig_outline, orig_w in items:
                if p == proj:
                    if kind == 'rect':
                        bright = _lerp_color(col, BG_INPUT, 0.25)
                        c.itemconfig(item_id, fill=bright,
                                     outline=col, width=2)
                    elif kind == 'text':
                        c.itemconfig(item_id, fill=FG)
                else:
                    if kind == 'rect':
                        dim_fill = _lerp_color(orig_fill, BG_INPUT, 0.65)
                        dim_out  = (_lerp_color(orig_outline, BG_INPUT, 0.65)
                                    if orig_outline else '')
                        c.itemconfig(item_id, fill=dim_fill,
                                     outline=dim_out, width=orig_w)
                    elif kind == 'text':
                        c.itemconfig(item_id, fill=FG_DIM)

    def _unhighlight_proj(self):
        c = self._diag
        for items in self._proj_canvas_items.values():
            for item_id, kind, col, orig_fill, orig_outline, orig_w in items:
                if kind == 'rect':
                    c.itemconfig(item_id, fill=orig_fill,
                                 outline=orig_outline, width=orig_w)
                elif kind == 'text':
                    c.itemconfig(item_id, fill=orig_fill)
        self._hovered_proj = None

    def _build_proj_tip(self, proj: str) -> str:
        mins  = next((m for n, m in self._chart_time_data if n == proj), 0)
        total = sum(m for _, m in self._chart_time_data) or 1
        pct   = mins / total * 100
        hh, mm = divmod(mins, 60)
        t_str  = f'{hh}h {mm:02d}m' if hh else f'{mm}m'

        app_m: dict = {}
        for ts, te, p in self._chart_timeline_data:
            if p != proj:
                continue
            try:
                t_s, t_e = _hhmm_to_min(ts), _hhmm_to_min(te)
            except Exception:
                continue
            for s in self._chart_sessions:
                ov = min(s['tmin'] + s['dur'], t_e) - max(s['tmin'], t_s)
                if ov > 0:
                    app_m[s['app']] = app_m.get(s['app'], 0) + ov

        lines = [proj, f'{t_str}  ·  {pct:.0f}% of day']
        if app_m:
            lines.append('')
            for app, am in sorted(app_m.items(), key=lambda x: -x[1])[:5]:
                ah, amm = divmod(int(am), 60)
                a_str = f'{ah}h {amm:02d}m' if ah else f'{amm}m'
                lines.append(f'  {app}  {a_str}')
        return '\n'.join(lines)

    # ── History tab ───────────────────────────────────────────────────────

    def _render_history(self):
        c = self._hist_cv
        c.delete('all')
        self._hist_tips.clear()

        W   = max(c.winfo_width() - 4, 760)
        PAD = 20

        # Load all analyzed days oldest→newest
        dates = self._scan_dates()
        day_data = []
        for d in reversed(dates):
            if not d['analyzed']:
                continue
            ds  = d['date']
            af  = BASE_DIR / 'logs' / ds / 'analysis.md'
            sd  = BASE_DIR / 'logs' / ds / 'screenshots'
            if not af.exists():
                continue
            text     = af.read_text(encoding='utf-8')
            timeline = _parse_timeline_data(text)
            if not timeline:
                continue
            subtasks = _parse_subtask_data(text)
            shots    = sorted(sd.glob('*.jpg')) if sd.exists() else []
            sessions = parse_sessions(shots)
            day_data.append({'date': ds, 'timeline': timeline,
                             'subtasks': subtasks, 'sessions': sessions})

        # Apply range filter (last N days; 0 = All)
        n_range = self._hist_range_days
        if n_range > 0 and len(day_data) > n_range:
            day_data = day_data[-n_range:]

        if not day_data:
            c.create_text(PAD, 40, text='No analyzed days yet.',
                          fill=FG_DIM, font=FONT_S, anchor='w')
            c.configure(scrollregion=(0, 0, W, 80))
            return

        # Consistent project & app colors across all days
        proj_color: dict = {}
        app_color:  dict = {}
        work_i = app_i = 0
        for d in day_data:
            for _, _, proj in d['timeline']:
                if proj not in proj_color:
                    if _is_off_time(proj):
                        proj_color[proj] = OFF_COL
                    else:
                        proj_color[proj] = PALETTE[work_i % len(PALETTE)]
                        work_i += 1
            for s in d['sessions']:
                if s['app'] not in app_color:
                    app_color[s['app']] = PALETTE[app_i % len(PALETTE)]
                    app_i += 1

        # Adaptive layout — mirrors the Chart tab's lane proportions
        USABLE    = W - 2 * PAD
        N_PER_ROW = max(1, min(8, len(day_data), USABLE // 150))
        DAY_W     = USABLE // N_PER_ROW
        LABEL_H   = 22
        AXIS_H    = 12          # mini hour-axis like the Chart's daily timeline
        PROJ_H    = 36
        ST_H      = 24
        APP_H     = 26
        LANE_GAP  = 4
        ROW_GAP   = 18
        ROW_H     = (LABEL_H + AXIS_H + PROJ_H + LANE_GAP + ST_H + LANE_GAP
                     + APP_H + ROW_GAP)

        y = PAD - 4

        for row_start in range(0, len(day_data), N_PER_ROW):
            row   = day_data[row_start: row_start + N_PER_ROW]
            row_y = y

            for ci, d in enumerate(row):
                dx = PAD + ci * DAY_W

                if ci > 0:
                    c.create_line(dx, row_y + 4, dx, row_y + ROW_H - 12,
                                  fill=EDGE_COL, width=1)

                # Date label
                try:
                    dt   = datetime.strptime(d['date'], '%Y-%m-%d')
                    lbl  = f"{dt.strftime('%a')} {int(dt.strftime('%d'))} {dt.strftime('%b')}"
                except Exception:
                    lbl = d['date']
                c.create_text(dx + 8, row_y + LABEL_H // 2 + 2, text=lbl,
                              fill=FG, font=('Segoe UI', 9, 'bold'),
                              anchor='w')

                # Time bounds — round to whole hours so the axis matches the Chart tab
                tmins = []
                for ts, te, _ in d['timeline']:
                    try:
                        tmins += [_hhmm_to_min(ts), _hhmm_to_min(te)]
                    except Exception:
                        pass
                for s in d['sessions']:
                    tmins += [s['tmin'], s['tmin'] + s['dur']]
                if not tmins:
                    continue
                t_s    = (min(tmins) // 60) * 60
                t_e    = ((max(tmins) // 60) + 1) * 60
                t_span = t_e - t_s or 1
                lane_x1 = dx + 4
                lane_x2 = dx + DAY_W - 4
                iw      = lane_x2 - lane_x1

                def _tx(t, _x1=lane_x1, _ts=t_s, _sp=t_span, _iw=iw):
                    return _x1 + (t - _ts) / _sp * _iw

                axis_y = row_y + LABEL_H
                proj_y = axis_y + AXIS_H
                st_y   = proj_y + PROJ_H + LANE_GAP
                app_y  = st_y + ST_H + LANE_GAP

                # Mini hour axis (matches Chart tab's hour ticks)
                c.create_line(lane_x1, axis_y + AXIS_H - 1,
                              lane_x2, axis_y + AXIS_H - 1,
                              fill=EDGE_COL, width=1)
                hour_step = max(1, int(t_span // 60) // 4)
                for h_i in range(int(t_s // 60), int(t_e // 60) + 1):
                    hx = _tx(h_i * 60)
                    if (h_i - int(t_s // 60)) % hour_step != 0:
                        continue
                    c.create_line(hx, axis_y + AXIS_H - 5, hx, axis_y + AXIS_H - 1,
                                  fill=EDGE_COL, width=1)
                    c.create_text(hx, axis_y + AXIS_H - 6, text=f'{h_i:02d}',
                                  fill=FG_DIM, font=('Segoe UI', 7), anchor='s')

                def _hour_grid(yy, hh):
                    for h_i in range(int(t_s // 60) + 1, int(t_e // 60) + 1):
                        hx = _tx(h_i * 60)
                        c.create_line(hx, yy, hx, yy + hh, fill=EDGE_COL, width=1)

                # Lane backgrounds (rounded, BG_INPUT) — same as Chart
                _rrect(c, lane_x1, proj_y, lane_x2, proj_y + PROJ_H, r=4,
                       fill=BG_INPUT, outline='')
                _rrect(c, lane_x1, st_y,   lane_x2, st_y   + ST_H,   r=4,
                       fill=BG_INPUT, outline='')
                _rrect(c, lane_x1, app_y,  lane_x2, app_y  + APP_H,  r=4,
                       fill=BG_INPUT, outline='')
                _hour_grid(proj_y, PROJ_H)
                _hour_grid(st_y,   ST_H)
                _hour_grid(app_y,  APP_H)

                # ── Projects lane (matches Chart styling) ─────────────────
                for ts, te, proj in d['timeline']:
                    try:
                        x1 = _tx(_hhmm_to_min(ts))
                        x2 = max(_tx(_hhmm_to_min(te)), x1 + 2)
                    except Exception:
                        continue
                    col      = proj_color.get(proj, ACCENT)
                    off      = _is_off_time(proj)
                    blk_col  = BG_CARD            if off else _lerp_color(col, BG_INPUT, 0.55)
                    out_col  = EDGE_COL           if off else col
                    txt_col  = FG_DIM             if off else FG
                    _rrect(c, x1, proj_y + 3, x2, proj_y + PROJ_H - 3, r=4,
                           fill=blk_col, outline=out_col, width=1)
                    bw = x2 - x1
                    if bw > 26:
                        lbl2 = _fit_label(proj, bw - 8, 6.0)
                        if lbl2:
                            c.create_text((x1 + x2) / 2, proj_y + PROJ_H // 2,
                                          text=lbl2, fill=txt_col,
                                          font=('Segoe UI', 8), anchor='center')
                    try:
                        h, m = divmod(int(_hhmm_to_min(te) - _hhmm_to_min(ts)), 60)
                        dur_s = f'{h}h {m:02d}m' if h else f'{m}m'
                    except Exception:
                        dur_s = ''
                    self._hist_tips.append(
                        (x1, proj_y, x2, proj_y + PROJ_H,
                         f'{proj}\n{d["date"]}  {ts} - {te}  ({dur_s})'))

                # ── Sub-tasks lane ─────────────────────────────────────────
                for ts, te, proj, title in d.get('subtasks', []):
                    try:
                        x1 = _tx(_hhmm_to_min(ts))
                        x2 = max(_tx(_hhmm_to_min(te)), x1 + 2)
                    except Exception:
                        continue
                    col     = proj_color.get(proj, ACCENT)
                    if _is_off_time(proj):
                        blk_col, out_col, txt_col = BG_CARD, EDGE_COL, FG_DIM
                    else:
                        blk_col = _lerp_color(col, BG_CARD, 0.35)
                        out_col = col
                        txt_col = FG
                    _rrect(c, x1, st_y + 2, x2, st_y + ST_H - 2, r=3,
                           fill=blk_col, outline=out_col, width=1)
                    bw = x2 - x1
                    if bw > 30:
                        lbl3 = _fit_label(title, bw - 8, 5.6)
                        if lbl3:
                            c.create_text((x1 + x2) / 2, st_y + ST_H // 2,
                                          text=lbl3, fill=txt_col,
                                          font=('Segoe UI', 7), anchor='center')
                    self._hist_tips.append(
                        (x1, st_y, x2, st_y + ST_H,
                         f'{title}\n{proj}\n{d["date"]}  {ts} - {te}'))

                # ── Apps lane ──────────────────────────────────────────────
                for s in d['sessions']:
                    if s['dur'] < 0.4:
                        continue
                    x1 = _tx(s['tmin'])
                    x2 = max(_tx(s['tmin'] + s['dur']), x1 + 2)
                    col  = app_color.get(s['app'], FG_DIM)
                    fill = _lerp_color(col, BG_INPUT, 0.62)
                    _rrect(c, x1, app_y + 3, x2, app_y + APP_H - 3, r=3,
                           fill=fill, outline=col, width=1)
                    bw = x2 - x1
                    if bw > 36:
                        lbl4 = _fit_label(s['app'], bw - 8, 5.8)
                        if lbl4:
                            c.create_text((x1 + x2) / 2, app_y + APP_H // 2,
                                          text=lbl4, fill=FG,
                                          font=FONT_XS, anchor='center')
                    self._hist_tips.append(
                        (x1, app_y, x2, app_y + APP_H,
                         f"{s['app']}\n{d['date']}  {s['time']}  ({s['dur']:.0f} min)"))

            y += ROW_H
            c.create_line(PAD, y - ROW_GAP // 2, W - PAD, y - ROW_GAP // 2,
                          fill=EDGE_COL, width=1)

        # Project legend
        y += 8
        c.create_text(PAD, y, text='PROJECTS', fill=FG_DIM,
                      font=FONT_XSB, anchor='nw')
        y += 18
        lx = PAD
        for proj, col in sorted(proj_color.items()):
            if _is_off_time(proj):
                continue
            needed = min(len(proj), 22) * 6 + 22
            if lx + needed > W - PAD:
                lx  = PAD
                y  += 18
            c.create_rectangle(lx, y + 2, lx + 9, y + 11, fill=col, outline='')
            c.create_text(lx + 14, y + 6, text=proj[:22],
                          fill=FG_DIM, font=FONT_XS, anchor='w')
            lx += needed

        y += 28
        c.configure(scrollregion=(0, 0, W, y))

    def _on_hist_motion(self, event):
        cx = self._hist_cv.canvasx(event.x)
        cy = self._hist_cv.canvasy(event.y)
        for x1, y1, x2, y2, tip in self._hist_tips:
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                if tip != self._last_tip:
                    self._last_tip = tip
                    self._show_tooltip(tip, event.x_root, event.y_root)
                return
        if self._last_tip is not None:
            self._hide_tooltip()

    # ── Visual sections ────────────────────────────────────────────────────

    def _draw_brief(self, c, W: int, y: int,
                    time_data: list, sessions: list, proj_color: dict) -> int:
        PAD = 16
        H   = 52

        # Slim card with subtle shadow
        _rrect(c, PAD+3, y+4, W-PAD+3, y+H+4, r=10, fill=BG_DEEP, outline='')
        _rrect(c, PAD, y, W-PAD, y+H, r=10, fill=BG_CARD, outline=EDGE_COL, width=1)

        # Left: date
        try:
            dt    = datetime.strptime(self._cur_date, '%Y-%m-%d')
            day_n = str(int(dt.strftime('%d')))
            dstr  = f"{dt.strftime('%A')},  {day_n} {dt.strftime('%B')}"
        except Exception:
            dstr = self._cur_date
        c.create_text(PAD+18, y + H//2, text=dstr,
                      fill=FG, font=('Segoe UI', 10, 'bold'), anchor='w')

        # Right: stat chips (value over label, separated by hairlines)
        total  = sum(m for _, m in time_data)
        ht, mt = divmod(total, 60)
        n_work = sum(1 for n, _ in time_data if not _is_off_time(n))
        mets   = _calc_metrics(sessions)
        score, sw = mets['score'], mets['switches']
        sc_col = GREEN if score >= 65 else YELLOW if score >= 40 else RED

        stats = [
            (f'{ht}h {mt:02d}m' if ht else f'{mt}m', 'TOTAL',    FG),
            (str(n_work),                              'PROJECTS', ACCENT),
            (f'{score}%',                              'FOCUS',    sc_col),
            (str(sw),                                  'SWITCHES', FG_DIM),
        ]
        chip_x = W - PAD - 16
        for val, lbl, col in reversed(stats):
            vw = max(len(val) * 7, len(lbl) * 5) + 16
            c.create_text(chip_x, y + H//2 - 7, text=val, fill=col,
                          font=('Segoe UI', 10, 'bold'), anchor='e')
            c.create_text(chip_x, y + H//2 + 9, text=lbl, fill=FG_DIM,
                          font=('Segoe UI', 7), anchor='e')
            chip_x -= vw
            c.create_line(chip_x, y+14, chip_x, y+H-14, fill=EDGE_COL, width=1)
            chip_x -= 12

        return y + H

    def _draw_bars(self, c, x0: int, y0: int, W: int,
                   time_data: list, sessions: list,
                   timeline_data: list, proj_color: dict) -> int:
        LABEL_W = 190
        BAR_H   = 32
        GAP     = 7
        BAR_W   = W - LABEL_W - 80

        total = sum(m for _, m in time_data) or 1
        max_m = max(m for _, m in time_data) or 1

        y = y0 + 4
        c.create_text(x0, y, text='TIME BREAKDOWN',
                      fill=FG_DIM, font=FONT_XSB, anchor='nw')
        y += 20

        for i, (name, mins) in enumerate(time_data):
            bw  = max(6, int((mins / max_m) * BAR_W))
            col = proj_color.get(name, PALETTE[i % len(PALETTE)])
            off = _is_off_time(name)

            # Subtle alternating row tint
            if i % 2 == 0:
                c.create_rectangle(x0-4, y-2, x0+W+4, y+BAR_H+2,
                                   fill=BG_CARD, outline='')

            # Label (fits up to LABEL_W-28 px)
            disp = _fit_label(name, LABEL_W - 28, 6.0) or (name[:22] + '…')
            c.create_text(x0+LABEL_W-20, y+BAR_H//2,
                          text=disp,
                          fill=FG_DIM if off else FG,
                          font=FONT_S, anchor='e')

            # Color dot
            dr = 4
            c.create_oval(x0+LABEL_W-10-dr, y+BAR_H//2-dr,
                          x0+LABEL_W-10+dr, y+BAR_H//2+dr,
                          fill=col, outline='')

            # Bar: muted fill + accent left cap
            bx, by1, by2 = x0+LABEL_W, y+5, y+BAR_H-5
            bar_fill = BG_INPUT      if off else _lerp_color(col, BG_INPUT, 0.60)
            bar_cap  = OFF_COL       if off else col
            bar_id = _rrect(c, bx, by1, bx+bw, by2, r=3,
                            fill=bar_fill, outline='')
            cap_id = _rrect(c, bx, by1, bx+5, by2, r=2, fill=bar_cap, outline='')
            self._proj_canvas_items.setdefault(name, []).append(
                (bar_id, 'rect', col, bar_fill, '', 0))
            self._proj_canvas_items[name].append(
                (cap_id, 'rect', col, bar_cap, '', 0))

            # Labels right of bar
            hh, mm = divmod(mins, 60)
            pct    = mins / total * 100
            t_str  = f'{hh}h {mm:02d}m' if hh else f'{mm}m'
            lx     = bx + bw + 10
            c.create_text(lx, by1 + (by2-by1)//2 - 6,
                          text=t_str,
                          fill=FG_DIM if off else FG,
                          font=FONT_XS, anchor='w')
            c.create_text(lx, by1 + (by2-by1)//2 + 7,
                          text=f'{pct:.0f}%', fill=FG_DIM,
                          font=FONT_XS, anchor='w')

            apps = _apps_for_project(name, timeline_data, sessions)
            tip  = f'{name}\n{t_str}  ·  {pct:.0f}%\nApps: {", ".join(apps) or "—"}'
            self._chart_tips.append((bx, y, bx+bw, y+BAR_H, tip, name))
            y += BAR_H + GAP

        th, tm = divmod(total, 60)
        y += 6
        c.create_line(x0+LABEL_W, y, x0+LABEL_W+BAR_W+60, y,
                      fill=EDGE_COL, width=1)
        c.create_text(x0+LABEL_W+8, y+14,
                      text=f'Total  —  {th}h {tm:02d}m',
                      fill=FG, font=FONT_B, anchor='w')
        return y + 26

    def _draw_timeline_section(self, c, W: int, y0: int,
                               sessions: list, timeline_data: list,
                               proj_color: dict, subtask_data: list | None = None) -> int:
        if not sessions and not timeline_data:
            return y0

        PAD    = 16
        T_LEFT = 80          # narrower label column → more timeline space
        T_W    = W - PAD - T_LEFT - 4
        LANE_H = 46

        tmins = ([s['tmin'] for s in sessions]
                 + [s['tmin'] + s['dur'] for s in sessions])
        for ts, te, _ in timeline_data:
            try:
                tmins += [_hhmm_to_min(ts), _hhmm_to_min(te)]
            except Exception:
                pass
        if not tmins:
            return y0

        t_s  = (min(tmins) // 60) * 60
        t_e  = ((max(tmins) // 60) + 1) * 60
        t_sp = t_e - t_s or 1

        def tx(t):
            return T_LEFT + (t - t_s) / t_sp * T_W

        y = y0 + 6
        c.create_text(PAD, y, text='DAILY TIMELINE',
                      fill=FG_DIM, font=FONT_XSB, anchor='nw')
        y += 20

        # Time axis with hour ticks
        ax_y = y
        c.create_line(T_LEFT, ax_y, W-PAD, ax_y, fill=EDGE_COL, width=1)
        for h_i in range(int(t_s//60), int(t_e//60) + 1):
            hx = tx(h_i * 60)
            c.create_line(hx, ax_y-4, hx, ax_y+3, fill=EDGE_COL, width=1)
            c.create_text(hx, ax_y-8, text=f'{h_i:02d}:00',
                          fill=FG_DIM, font=FONT_XS, anchor='s')
        y = ax_y + 10

        def _hour_grid(lane_y, lane_h):
            for h_i in range(int(t_s//60)+1, int(t_e//60)+1):
                hx = tx(h_i * 60)
                c.create_line(hx, lane_y, hx, lane_y+lane_h,
                              fill=EDGE_COL, width=1)

        # ── Projects lane (primary) ───────────────────────────────────────
        if timeline_data:
            c.create_text(T_LEFT-6, y+LANE_H//2,
                          text='Projects', fill=FG_DIM,
                          font=('Segoe UI', 8), anchor='e')
            c.create_rectangle(T_LEFT, y, W-PAD, y+LANE_H,
                               fill=BG_INPUT, outline='')
            _hour_grid(y, LANE_H)

            for ts, te, proj in timeline_data:
                try:
                    x1, x2 = tx(_hhmm_to_min(ts)), tx(_hhmm_to_min(te))
                except Exception:
                    continue
                x2      = max(x2, x1 + 2)
                col     = proj_color.get(proj, ACCENT)
                off     = _is_off_time(proj)
                blk_col = BG_CARD          if off else _lerp_color(col, BG_INPUT, 0.55)
                out_col = EDGE_COL         if off else col
                txt_col = FG_DIM           if off else FG

                rid = _rrect(c, x1, y+4, x2, y+LANE_H-4, r=4,
                             fill=blk_col, outline=out_col, width=1)
                self._proj_canvas_items.setdefault(proj, []).append(
                    (rid, 'rect', col, blk_col, out_col, 1))

                bw = x2 - x1
                if bw > 22:
                    lbl = _fit_label(proj, bw - 10, 6.4)
                    if lbl:
                        tid = c.create_text((x1+x2)/2, y+LANE_H//2,
                                            text=lbl, fill=txt_col,
                                            font=('Segoe UI', 9), anchor='center')
                        self._proj_canvas_items[proj].append(
                            (tid, 'text', col, txt_col, '', 0))

                self._chart_tips.append(
                    (x1, y, x2, y+LANE_H, f'{ts} → {te}\n{proj}', proj))
            y += LANE_H + 8

        # ── Sub-tasks lane ────────────────────────────────────────────────
        if subtask_data:
            ST_H = 30
            c.create_text(T_LEFT-6, y+ST_H//2,
                          text='Sub-tasks', fill=FG_DIM,
                          font=('Segoe UI', 8), anchor='e')
            c.create_rectangle(T_LEFT, y, W-PAD, y+ST_H,
                               fill=BG_INPUT, outline='')
            _hour_grid(y, ST_H)

            for ts, te, proj, title in subtask_data:
                try:
                    x1, x2 = tx(_hhmm_to_min(ts)), tx(_hhmm_to_min(te))
                except Exception:
                    continue
                x2  = max(x2, x1 + 2)
                col = proj_color.get(proj, ACCENT)
                if _is_off_time(proj):
                    blk_col = BG_CARD
                    out_col = EDGE_COL
                    txt_col = FG_DIM
                else:
                    blk_col = _lerp_color(col, BG_CARD, 0.35)
                    out_col = col
                    txt_col = FG

                rid = _rrect(c, x1, y+3, x2, y+ST_H-3, r=3,
                             fill=blk_col, outline=out_col, width=1)
                # Hovering the parent project should highlight sub-tasks too
                self._proj_canvas_items.setdefault(proj, []).append(
                    (rid, 'rect', col, blk_col, out_col, 1))

                bw = x2 - x1
                if bw > 22:
                    lbl = _fit_label(title, bw - 8, 5.6)
                    if lbl:
                        tid = c.create_text((x1+x2)/2, y+ST_H//2,
                                            text=lbl, fill=txt_col,
                                            font=('Segoe UI', 8), anchor='center')
                        self._proj_canvas_items[proj].append(
                            (tid, 'text', col, txt_col, '', 0))

                self._chart_tips.append(
                    (x1, y, x2, y+ST_H, f'{ts} → {te}\n{proj}\n{title}', proj))
            y += ST_H + 8

        # ── Apps lane (secondary) ─────────────────────────────────────────
        if sessions:
            c.create_text(T_LEFT-6, y+LANE_H//2,
                          text='Apps', fill=FG_DIM,
                          font=('Segoe UI', 8), anchor='e')
            c.create_rectangle(T_LEFT, y, W-PAD, y+LANE_H,
                               fill=BG_INPUT, outline='')
            _hour_grid(y, LANE_H)

            app_c: dict = {}
            for s in sessions:
                app_c.setdefault(s['app'], PALETTE[len(app_c) % len(PALETTE)])

            for s in sessions:
                if s['dur'] < 0.3:
                    continue
                x1, x2 = tx(s['tmin']), tx(s['tmin'] + s['dur'])
                x2  = max(x2, x1 + 2)
                col = app_c[s['app']]
                _rrect(c, x1, y+4, x2, y+LANE_H-4, r=3,
                       fill=_lerp_color(col, BG_INPUT, 0.62),
                       outline=col, width=1)

                bw = x2 - x1
                if bw > 20:
                    lbl = _fit_label(s['app'], bw - 8, 5.5)
                    if lbl:
                        c.create_text((x1+x2)/2, y+LANE_H//2,
                                      text=lbl, fill=FG,
                                      font=FONT_XS, anchor='center')

                tip = (f"{s['time']}  {s['app']}\n"
                       f"{(s['title'] or '')[:60]}\n{s['dur']:.0f} min")
                self._chart_tips.append((x1, y, x2, y+LANE_H, tip, None))

            y += LANE_H + 6

            # App legend
            lx = T_LEFT
            for app, col in list(app_c.items()):
                needed = min(len(app), 14) * 5.5 + 18
                if lx + needed > W - PAD:
                    break
                c.create_rectangle(lx, y+2, lx+8, y+10, fill=col, outline='')
                c.create_text(lx+12, y+6, text=app[:14],
                              fill=FG_DIM, font=FONT_XS, anchor='w')
                lx += needed
            y += 14

        return y + 6

    def _draw_rhythm(self, c, W: int, y0: int,
                     sessions: list, timeline_data: list | None = None) -> int:
        if not sessions:
            return y0
        PAD    = 16
        T_LEFT = 80              # match _draw_timeline_section
        T_W    = W - PAD - T_LEFT - 4
        MAX_H  = 44

        # Focus tiers — driven by idle seconds at each capture.
        # Capture interval is ~2 min, so any real working pattern (thinking
        # between keystrokes, scrolling, brief tab-switches) lands well within
        # the deep threshold. Only sustained no-input presence (a meeting you
        # are watching, a long doc you are reading) tips into mild.
        # active < 3 min of idle  → deep (working on it)
        # 3 min ≤ idle < 30 min   → mild (present, no input — meeting / reading)
        # else                     → away (gap; capture.py pauses past 30 min)
        DEEP_MAX_IDLE = 180
        MILD_MAX_IDLE = 1800

        deep_col = _lerp_color(GREEN,   BG_INPUT, 0.30)
        mild_col = _lerp_color(YELLOW,  BG_INPUT, 0.40)
        away_col = _lerp_color(OFF_COL, BG_INPUT, 0.30)

        h_deep = MAX_H
        h_mild = int(MAX_H * 0.55)
        h_away = int(MAX_H * 0.20)

        def classify(idle):
            if idle is None:
                return 'mild'                    # old data — assume presence
            if idle < DEEP_MAX_IDLE:
                return 'deep'
            if idle < MILD_MAX_IDLE:
                return 'mild'
            return 'away'

        kind_col = {'deep': deep_col, 'mild': mild_col, 'away': away_col}
        kind_h   = {'deep': h_deep,   'mild': h_mild,   'away': h_away}

        y = y0 + 6
        c.create_text(PAD, y, text='ACTIVITY RHYTHM',
                      fill=FG_DIM, font=FONT_XSB, anchor='nw')
        y += 16
        c.create_text(PAD, y,
                      text='one bar per screen sample · colour = focus level at '
                           'that moment · gap = away from computer',
                      fill=FG_DIM, font=('Segoe UI', 8), anchor='nw')
        y += 14

        # Coloured-swatch legend (right-aligned)
        legend = [
            ('deep — actively working',   deep_col),
            ('mild — present, no input',  mild_col),
            ('away',                      away_col),
        ]
        FONT_LEG = ('Segoe UI', 8)
        SW       = 9
        gap      = 14
        item_w   = [SW + 4 + len(lbl) * 5.6 for lbl, _ in legend]
        leg_total = sum(item_w) + gap * (len(legend) - 1)
        lx = W - PAD - leg_total
        for (lbl, col), iw in zip(legend, item_w):
            c.create_rectangle(lx, y - 14, lx + SW, y - 14 + 9,
                               fill=col, outline='')
            c.create_text(lx + SW + 4, y - 14 + 4, text=lbl,
                          anchor='w', fill=FG_DIM, font=FONT_LEG)
            lx += iw + gap

        # Same time bounds as the timelines above so the x-axes line up
        tmins  = [s['tmin'] for s in sessions]
        tmins += [s['tmin'] + s['dur'] for s in sessions]
        for ts, te, _ in (timeline_data or []):
            try:
                tmins += [_hhmm_to_min(ts), _hhmm_to_min(te)]
            except Exception:
                pass
        t_s  = (min(tmins) // 60) * 60
        t_e  = ((max(tmins) // 60) + 1) * 60
        t_sp = t_e - t_s or 1
        base_y = y + MAX_H

        c.create_rectangle(T_LEFT, y, W-PAD, base_y, fill=BG_INPUT, outline='')

        # Lane label in the gutter
        c.create_text(T_LEFT - 6, (y + base_y) // 2,
                      text='Rhythm', fill=FG_DIM,
                      font=('Segoe UI', 8), anchor='e')

        def rx(t):
            return T_LEFT + (t - t_s) / t_sp * T_W

        for s in sessions:
            if s['dur'] < 0.2:
                continue
            x1, x2 = rx(s['tmin']), rx(s['tmin'] + s['dur'])
            x2   = max(x2, x1 + 1.5)
            kind = classify(s.get('idle'))
            bh   = kind_h[kind]
            col  = kind_col[kind]
            r    = min(2, max(1, int((x2-x1)//4)))
            _rrect(c, x1, base_y-bh, x2, base_y, r=r, fill=col, outline='')

            idle = s.get('idle')
            if idle is None:
                idle_lbl = 'idle: unknown'
            elif idle < 60:
                idle_lbl = f'idle: {idle}s'
            else:
                idle_lbl = f'idle: {idle // 60}m {idle % 60:02d}s'
            self._chart_tips.append(
                (x1, base_y-bh, x2, base_y,
                 f"{s['time']}  {s['app']}\n{kind} focus · {idle_lbl}", None))

        c.create_line(T_LEFT, base_y, W-PAD, base_y, fill=EDGE_COL, width=1)
        for h_i in range(int(t_s//60), int(t_e//60)+1):
            hx = rx(h_i * 60)
            c.create_line(hx, base_y, hx, base_y+4, fill=EDGE_COL, width=1)
            c.create_text(hx, base_y+8, text=f'{h_i:02d}:00',
                          fill=FG_DIM, font=FONT_XS, anchor='n')

        return base_y + 22

    # ══════════════════════════════════════════════════════════════════════
    # Queue
    # ══════════════════════════════════════════════════════════════════════

    def _poll(self):
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _handle(self, item):
        k = item[0]
        if k == 'lbl':
            self._write(f'\n{item[1]}\n', 'claude_lbl')
        elif k == 'chunk':
            self._write(item[1], 'body')
        elif k == 'nl':
            self._write('\n')
        elif k == 'q_line':
            self._write(item[1] + '\n', 'question')
        elif k == 'thumb':
            photo = ImageTk.PhotoImage(item[1])
            self._embed_clickable(photo, item[2])
            self._write('\n')
        elif k == 'user_echo':
            self._write('\nYou\n', 'user_lbl')
            self._write(item[1] + '\n\n', 'user_txt')
        elif k == 'waiting':
            self._state     = 'waiting'
            self._questions = item[1]
            self._api_msgs  = item[2]
            self._show_input()
        elif k == 'chart':
            sub = item[4] if len(item) > 4 else None
            self._render_charts(item[1], item[2], item[3], sub)
        elif k == 'diagram':
            self._render_mermaid(item[1])
        elif k == 'done':
            self._state = 'idle'
            self._hide_input()
            self._status.configure(text=f'Saved → {item[1]}')
            self._scan_dates(force=True)
            self._render_dates()
            self._switch_tab(1)
        elif k == 'error':
            self._write(f'\n{item[1]}\n', 'dim')
            self._state = 'idle'
            self._hide_input()
            self._status.configure(text='Error')

    # ══════════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════════

    def _on_enter(self, event):
        if event.state & 0x1:
            return
        self._send_answer()
        return 'break'

    def _show_input(self):
        self._inp_bar.pack(fill='x', before=self._pane)
        self._inp.focus_set()

    def _hide_input(self):
        self._inp_bar.pack_forget()

    def _write(self, text: str, tag: str = 'body'):
        self._chat.configure(state='normal')
        self._chat.insert('end', text, tag)
        self._chat.configure(state='disabled')
        self._chat.see('end')

    def _embed(self, photo: ImageTk.PhotoImage):
        self._photo_refs.append(photo)
        self._chat.configure(state='normal')
        self._chat.image_create('end', image=photo, padx=6, pady=6)
        self._chat.configure(state='disabled')
        self._chat.see('end')

    def _embed_clickable(self, photo: ImageTk.PhotoImage, path: Path):
        self._photo_refs.append(photo)
        self._chat.configure(state='normal')
        lbl = tk.Label(self._chat, image=photo, bg=BG_CHAT,
                       cursor='hand2', relief='flat', bd=0)
        lbl.bind('<Button-1>', lambda e, p=path: self._view_screenshot(p))
        self._chat.window_create('end', window=lbl, padx=6, pady=6)
        self._chat.configure(state='disabled')
        self._chat.see('end')

    def _clear_chat(self):
        self._chat.configure(state='normal')
        self._chat.delete('1.0', 'end')
        self._chat.configure(state='disabled')
        self._photo_refs.clear()

    def _client(self) -> anthropic.Anthropic:
        key = (self._cfg.get('api', 'anthropic_api_key', fallback='').strip()
               or os.environ.get('ANTHROPIC_API_KEY', ''))
        if not key:
            raise RuntimeError('API key not configured in config.ini.')
        return anthropic.Anthropic(api_key=key)


# ══════════════════════════════════════════════════════════════════════════
# Mermaid parser + layout
# ══════════════════════════════════════════════════════════════════════════

def _clean(s: str) -> str:
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
    return re.sub(r'<[^>]+>', '', s).strip()


def _parse_mermaid(mmd: str) -> tuple[dict, list]:
    nodes: dict[str, str] = {}
    edges: list = []
    for line in mmd.split('\n'):
        line = line.strip()
        if (not line or line.startswith('flowchart') or line.startswith('graph')
                or line.startswith('%%') or line.startswith('subgraph')
                or line == 'end'):
            continue
        for m in re.finditer(r'(\w+)\[([^\]]+)\]', line):
            nodes[m.group(1)] = _clean(m.group(2))
        for m in re.finditer(r'(\w+)\(([^)]+)\)', line):
            nodes.setdefault(m.group(1), _clean(m.group(2)))
        for m in re.finditer(r'(\w+)\{([^}]+)\}', line):
            nodes.setdefault(m.group(1), _clean(m.group(2)))
        em = re.search(r'(\w+)\s*--[->xo]*(?:\|([^|]*)\|)?\s*(\w+)', line)
        if em:
            edges.append((em.group(1), em.group(3), _clean(em.group(2) or '')))
    for a, b, _ in edges:
        nodes.setdefault(a, a)
        nodes.setdefault(b, b)
    return nodes, edges


def _layout(nodes: dict, edges: list) -> dict:
    if not nodes:
        return {}
    children: dict[str, list] = {n: [] for n in nodes}
    parents:  dict[str, set]  = {n: set() for n in nodes}
    for a, b, _ in edges:
        if b not in children[a]:
            children[a].append(b)
        parents[b].add(a)
    layer: dict[str, int] = {n: 0 for n in nodes}
    in_deg = {n: len(parents[n]) for n in nodes}
    q: deque = deque(n for n in nodes if in_deg[n] == 0) or deque([next(iter(nodes))])
    visited: set = set()
    while q:
        n = q.popleft()
        if n in visited:
            continue
        visited.add(n)
        for c in children[n]:
            layer[c] = max(layer[c], layer[n] + 1)
            in_deg[c] -= 1
            if in_deg[c] == 0:
                q.append(c)
    groups: dict[int, list] = {}
    for n, l in layer.items():
        groups.setdefault(l, []).append(n)
    for l in sorted(groups):
        if l == 0:
            continue
        prev_idx = {n: i for i, n in enumerate(groups.get(l - 1, []))}
        def centroid(n, pi=prev_idx):
            ps = [pi[p] for p in parents[n] if p in pi]
            return sum(ps) / len(ps) if ps else 0
        groups[l].sort(key=centroid)
    pos: dict[str, tuple] = {}
    for l, group in groups.items():
        total_w = len(group) * BOX_W + (len(group) - 1) * H_GAP
        start_x = -total_w / 2 + BOX_W / 2
        y = l * (BOX_H + V_GAP)
        for i, n in enumerate(group):
            pos[n] = (start_x + i * (BOX_W + H_GAP), y)
    return pos


# ══════════════════════════════════════════════════════════════════════════
# File helpers
# ══════════════════════════════════════════════════════════════════════════

_OFF_KW = frozenset({
    'lunch', 'break', 'off', 'away', 'personal', 'commute',
    'idle', 'afk', 'sleep', 'dinner', 'coffee', 'pause',
    'rest', 'walk', 'gym', 'sport', 'chores', 'eating', 'food',
})

def _is_off_time(name: str) -> bool:
    n = name.lower()
    return bool(re.search(
        r'\b(lunch|break|off(\s+time)?|away|personal|commute|'
        r'idle|afk|sleep|dinner|coffee|pause|rest|gym|sport)\b', n))


def _fit_label(text: str, px_width: float, char_px: float = 6.2) -> str:
    """Truncate text to fit px_width. Returns '' if even 2 chars don't fit."""
    chars = int(px_width / char_px)
    if chars <= 1:
        return ''
    if len(text) <= chars:
        return text
    if chars <= 2:
        return ''
    return text[:chars - 1] + '…'


def _make_row_cbs(d_key, bgs_list, accent_widget, app):
    """Factory for sidebar-row hover callbacks (avoids closure-in-loop bugs)."""
    HOVER_BG = '#D8DFF0'
    def enter(e):
        for w in bgs_list:
            try: w.configure(bg=HOVER_BG)
            except Exception: pass
    def leave(e):
        sel = (d_key == app._cur_date)
        bg_ = BG_INPUT if sel else BG_SIDE
        for w in bgs_list:
            try: w.configure(bg=bg_)
            except Exception: pass
        try: accent_widget.configure(bg=ACCENT if sel else BG_SIDE)
        except Exception: pass
    return enter, leave


def _lerp_color(a: str, b: str, t: float) -> str:
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = int(ra + (rb - ra) * t)
    g = int(ga + (gb - ga) * t)
    b_ = int(ba + (bb - ba) * t)
    return f'#{r:02x}{g:02x}{b_:02x}'


def _rrect(c, x1, y1, x2, y2, r: int = 8, **kw):
    r = min(r, int((x2 - x1) // 2), int((y2 - y1) // 2))
    if r < 1:
        return c.create_rectangle(x1, y1, x2, y2, **kw)
    pts = [x1+r, y1,  x2-r, y1,  x2, y1,   x2, y1+r,
           x2, y2-r,  x2, y2,    x2-r, y2,  x1+r, y2,
           x1, y2,    x1, y2-r,  x1, y1+r,  x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def _gradient_h(c, x1, y1, x2, y2, col_a: str, col_b: str, steps: int = 36):
    w = (x2 - x1) / steps
    for i in range(steps):
        t = i / (steps - 1)
        c.create_rectangle(x1 + i * w, y1, x1 + (i + 1) * w + 0.5, y2,
                           fill=_lerp_color(col_a, col_b, t), outline='')


def _calc_metrics(sessions: list) -> dict:
    if not sessions:
        return {'score': 0, 'switches': 0}
    switches = sum(1 for i in range(1, len(sessions))
                   if sessions[i]['app'] != sessions[i-1]['app'])
    total = sum(s['dur'] for s in sessions) or 1
    deep  = sum(s['dur'] for s in sessions if s['dur'] >= 18)
    rate  = switches / len(sessions)
    score = max(0, min(100, int((deep / total) * 65 + (1 - min(rate * 1.5, 1)) * 35)))
    return {'score': score, 'switches': switches}


def _apps_for_project(project: str, timeline_data: list, sessions: list) -> list:
    if not timeline_data or not sessions:
        return []
    app_m: dict = {}
    for ts, te, proj in timeline_data:
        if proj != project:
            continue
        try:
            t_s, t_e = _hhmm_to_min(ts), _hhmm_to_min(te)
        except Exception:
            continue
        for s in sessions:
            ov = min(s['tmin'] + s['dur'], t_e) - max(s['tmin'], t_s)
            if ov > 0:
                app_m[s['app']] = app_m.get(s['app'], 0) + ov
    return sorted(app_m, key=lambda k: app_m[k], reverse=True)[:3]


def _closest_shot(shots_dir: Path, time_str: str) -> Path | None:
    try:
        h, m = map(int, time_str.split(':'))
    except ValueError:
        return None
    target = h * 60 + m
    best, best_d = None, float('inf')
    for s in shots_dir.glob('*.jpg'):
        try:
            t      = s.stem.split('__')[0].replace('-', ':')
            sh, sm = int(t.split(':')[0]), int(t.split(':')[1])
            d      = abs(sh * 60 + sm - target)
            if d < best_d:
                best, best_d = s, d
        except (ValueError, IndexError):
            continue
    return best if best_d <= 5 else None


def _thumbnail(path: Path, width: int) -> Image.Image | None:
    try:
        img   = Image.open(path)
        ratio = width / img.width
        return img.resize((width, int(img.height * ratio)), Image.LANCZOS)
    except Exception:
        return None


def _extract_mermaid(text: str) -> str | None:
    if '```mermaid' not in text:
        return None
    s   = text.index('```mermaid') + 10
    try:
        e   = text.index('```', s)
        mmd = text[s:e].strip()
    except ValueError:
        mmd = text[s:].strip()   # truncated response — take what we have
    return mmd or None


def _parse_time_data(text: str) -> list[tuple[str, int]]:
    """Parse ## TIME_DATA block → [(project, minutes)]."""
    marker = '## TIME_DATA'
    if marker not in text:
        return []
    start   = text.index(marker) + len(marker)
    section = text[start:]
    result  = []
    for line in section.split('\n'):
        line = line.strip()
        if line.startswith('##'):
            break
        if '|' in line:
            parts = line.split('|', 1)
            try:
                name = parts[0].strip()
                mins = int(parts[1].strip())
                if name:
                    result.append((name, mins))
            except (ValueError, IndexError):
                continue
    return result


def _parse_timeline_data(text: str) -> list[tuple[str, str, str]]:
    """Parse ## TIMELINE_DATA block → [(start HH:MM, end HH:MM, project)]."""
    marker = '## TIMELINE_DATA'
    if marker not in text:
        return []
    start   = text.index(marker) + len(marker)
    section = text[start:]
    result  = []
    for line in section.split('\n'):
        line = line.strip()
        if line.startswith('##'):
            break
        parts = line.split('|')
        if len(parts) >= 3:
            t_s = parts[0].strip()
            t_e = parts[1].strip()
            proj = parts[2].strip()
            if t_s and t_e and proj and ':' in t_s and ':' in t_e:
                result.append((t_s, t_e, proj))
    return result


def _parse_subtask_data(text: str) -> list[tuple[str, str, str, str]]:
    """Parse ## SUBTASK_DATA block → [(HH:MM, HH:MM, project, title)]."""
    marker = '## SUBTASK_DATA'
    if marker not in text:
        return []
    start   = text.index(marker) + len(marker)
    section = text[start:]
    result  = []
    for line in section.split('\n'):
        line = line.strip()
        if line.startswith('##'):
            break
        parts = line.split('|')
        if len(parts) >= 4:
            t_s, t_e, proj, title = (p.strip() for p in parts[:4])
            if t_s and t_e and proj and title and ':' in t_s and ':' in t_e:
                result.append((t_s, t_e, proj, title))
    return result


def _parse_struggle_data(text: str) -> list[tuple[str, str, str, str, str]]:
    """Parse ## STRUGGLE_DATA block → [(HH:MM, HH:MM, project, kind, summary)]."""
    marker = '## STRUGGLE_DATA'
    if marker not in text:
        return []
    start   = text.index(marker) + len(marker)
    section = text[start:]
    result  = []
    valid_kinds = {'reconciliation', 'struggle', 'blocker'}
    for line in section.split('\n'):
        line = line.strip()
        if line.startswith('##'):
            break
        parts = line.split('|')
        if len(parts) >= 5:
            t_s, t_e, proj, kind, summary = (p.strip() for p in parts[:5])
            kind = kind.lower()
            if (t_s and t_e and proj and summary
                    and kind in valid_kinds
                    and ':' in t_s and ':' in t_e):
                result.append((t_s, t_e, proj, kind, summary))
    return result


def _hhmm_to_min(s: str) -> float:
    h, m = map(int, s.split(':'))
    return h * 60.0 + m


def _save(ds: str, analysis: str) -> None:
    out = BASE_DIR / 'logs' / ds
    out.mkdir(parents=True, exist_ok=True)
    (out / 'analysis.md').write_text(analysis, encoding='utf-8')


def _make_icon() -> ImageTk.PhotoImage:
    """Pulsar icon: dark-navy background, cyan jets, white glowing core."""
    from PIL import ImageDraw, ImageFilter
    size = 64
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    s  = size / 256

    pad = max(1, int(size * 0.04))
    draw.ellipse([pad, pad, size - pad - 1, size - pad - 1],
                 fill=(0, 49, 61, 255))           # #00313D Ebury darkest

    jet_len = int(cx * 0.88)
    for angle_deg in (270, 90):
        angle = math.radians(angle_deg)
        for r in range(2, jet_len):
            t     = r / jet_len
            alpha = int(230 * (1 - t) ** 1.3)
            w     = max(1, int((6 - t * 5) * s))
            x     = cx + math.cos(angle) * r
            y     = cy + math.sin(angle) * r
            draw.ellipse([x - w, y - w, x + w, y + w],
                         fill=(0, 190, 240, alpha))  # #00BEF0 Ebury cyan

    core_r = max(2, int(size * 0.11))
    for r in range(core_r, 0, -1):
        t = r / core_r
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(255, 255, 255, int(255 * (1 - t ** 0.55))))

    c2 = max(1, int(size * 0.032))
    draw.ellipse([cx - c2, cy - c2, cx + c2, cy + c2], fill=(255, 255, 255, 255))

    return ImageTk.PhotoImage(img)


if __name__ == '__main__':
    root = tk.Tk()
    icon = _make_icon()
    root.iconphoto(True, icon)
    PulsarApp(root)
    root.mainloop()
