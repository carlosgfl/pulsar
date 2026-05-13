"""
Pulsar — desktop analysis GUI.
Launch with run_analysis.bat (no CMD window).
"""

import configparser
import os
import queue
import re
import threading
from collections import deque
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import anthropic
from PIL import Image, ImageTk

from analyze import (
    append_knowledge,
    build_initial_content,
    load_knowledge,
    load_screenshots,
    select_keyframes,
    split_response,
)

BASE_DIR = Path(__file__).parent

# ── Palette ───────────────────────────────────────────────────────────────
BG       = '#1e1e2e'
BG_CHAT  = '#181825'
BG_INPUT = '#313244'
FG       = '#cdd6f4'
FG_DIM   = '#6c7086'
ACCENT   = '#89b4fa'
GREEN    = '#a6e3a1'
YELLOW   = '#f9e2af'
EDGE_COL = '#585b70'
FONT     = ('Segoe UI', 10)
FONT_B   = ('Segoe UI', 10, 'bold')
FONT_S   = ('Segoe UI', 9)

# ── Diagram layout constants ──────────────────────────────────────────────
BOX_W  = 160
BOX_H  = 52
H_GAP  = 50
V_GAP  = 70
D_PAD  = 40


class PulsarApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Pulsar Analysis')
        self.root.geometry('960x800')
        self.root.configure(bg=BG)
        self.root.minsize(700, 500)

        self._q: queue.Queue = queue.Queue()
        self._photo_refs: list = []
        self._state = 'idle'
        self._api_messages: list = []
        self._questions_section = ''
        self._target_date = ''

        self._cfg = configparser.ConfigParser()
        self._cfg.read(BASE_DIR / 'config.ini')

        self._build_ui()
        self._poll()

    # ── Build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self.root, bg=BG, pady=10, padx=14)
        bar.pack(fill='x')

        tk.Label(bar, text='Date:', bg=BG, fg=FG, font=FONT).pack(side='left')
        self._date_var = tk.StringVar(value=date.today().strftime('%Y-%m-%d'))
        tk.Entry(bar, textvariable=self._date_var, width=12,
                 bg=BG_INPUT, fg=FG, insertbackground=FG,
                 relief='flat', font=FONT).pack(side='left', padx=(4, 14))

        self._btn = tk.Button(bar, text='Analyze', command=self._start,
                              bg=ACCENT, fg=BG, font=FONT_B,
                              relief='flat', padx=14, cursor='hand2', bd=0)
        self._btn.pack(side='left')

        self._status = tk.Label(bar, text='Ready', bg=BG, fg=FG_DIM, font=FONT_S)
        self._status.pack(side='right')

        # Notebook
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TNotebook',      background=BG,       borderwidth=0)
        style.configure('TNotebook.Tab',  background=BG_INPUT, foreground=FG_DIM,
                        padding=[14, 6],  font=FONT_S)
        style.map('TNotebook.Tab',
                  background=[('selected', BG_CHAT)],
                  foreground=[('selected', FG)])

        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill='both', expand=True, padx=14, pady=(0, 0))

        # Chat tab
        chat_tab = tk.Frame(self._nb, bg=BG_CHAT)
        self._nb.add(chat_tab, text='  Chat  ')

        self._chat = tk.Text(
            chat_tab, bg=BG_CHAT, fg=FG, font=FONT,
            wrap='word', state='disabled', relief='flat',
            padx=14, pady=10, spacing3=5, cursor='arrow',
        )
        chat_sb = ttk.Scrollbar(chat_tab, command=self._chat.yview)
        self._chat.configure(yscrollcommand=chat_sb.set)
        chat_sb.pack(side='right', fill='y')
        self._chat.pack(side='left', fill='both', expand=True)

        self._chat.tag_configure('claude_lbl', foreground=ACCENT,  font=FONT_B)
        self._chat.tag_configure('claude_txt', foreground=FG)
        self._chat.tag_configure('user_lbl',   foreground=GREEN,   font=FONT_B)
        self._chat.tag_configure('user_txt',   foreground=FG)
        self._chat.tag_configure('question',   foreground=YELLOW)
        self._chat.tag_configure('dim',        foreground=FG_DIM,  font=FONT_S)

        # Diagram tab
        diag_tab = tk.Frame(self._nb, bg=BG_CHAT)
        self._nb.add(diag_tab, text='  Diagram  ')

        diag_vsb = ttk.Scrollbar(diag_tab, orient='vertical')
        diag_hsb = ttk.Scrollbar(diag_tab, orient='horizontal')
        self._diag = tk.Canvas(diag_tab, bg=BG_CHAT, relief='flat',
                               highlightthickness=0,
                               yscrollcommand=diag_vsb.set,
                               xscrollcommand=diag_hsb.set)
        diag_vsb.configure(command=self._diag.yview)
        diag_hsb.configure(command=self._diag.xview)
        diag_hsb.pack(side='bottom', fill='x')
        diag_vsb.pack(side='right',  fill='y')
        self._diag.pack(side='left', fill='both', expand=True)
        self._diag.bind('<MouseWheel>',
                        lambda e: self._diag.yview_scroll(-1*(e.delta//120), 'units'))
        self._diag.create_text(20, 20,
                               text='Diagram will appear here after analysis.',
                               fill=FG_DIM, font=FONT_S, anchor='nw')

        # Input bar
        inp = tk.Frame(self.root, bg=BG, padx=14, pady=8)
        inp.pack(fill='x')

        self._inp = tk.Text(inp, height=3, bg=BG_INPUT, fg=FG,
                            insertbackground=FG, relief='flat', font=FONT,
                            padx=10, pady=8, wrap='word')
        self._inp.pack(side='left', fill='x', expand=True)
        self._inp.bind('<Return>', self._on_enter)

        self._send = tk.Button(inp, text='Send', command=self._answer,
                               bg=BG_INPUT, fg=FG_DIM, font=FONT,
                               relief='flat', padx=14, cursor='hand2',
                               bd=0, state='disabled')
        self._send.pack(side='left', padx=(8, 0), fill='y')

    # ── Queue ─────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _handle(self, item):
        k = item[0]
        if k == 'label':
            self._write(f'\n{item[1]}\n', 'claude_lbl')
        elif k == 'chunk':
            self._write(item[1])
        elif k == 'nl':
            self._write('\n')
        elif k == 'question_line':
            self._write(item[1] + '\n', 'question')
        elif k == 'thumb':
            photo = ImageTk.PhotoImage(item[1])   # PIL→PhotoImage in main thread
            self._embed(photo)
            self._write('\n')
        elif k == 'waiting':
            self._state            = 'waiting'
            self._questions_section = item[1]
            self._api_messages      = item[2]
            self._inp_enable(True)
            self._status.configure(text='Waiting for your answer…')
        elif k == 'user_echo':
            self._write('\nYou\n', 'user_lbl')
            self._write(item[1] + '\n\n', 'user_txt')
        elif k == 'diagram':
            self._render_mermaid(item[1])
            self._nb.select(1)
        elif k == 'done':
            self._state = 'done'
            self._status.configure(text=f'Saved → logs/{item[1]}/analysis.md')
            self._btn.configure(state='normal')
        elif k == 'error':
            self._write(f'\n{item[1]}\n', 'dim')
            self._state = 'idle'
            self._btn.configure(state='normal')
            self._status.configure(text='Error')

    # ── Chat helpers ──────────────────────────────────────────────────────

    def _on_enter(self, event):
        if event.state & 0x1:
            return
        self._answer()
        return 'break'

    def _inp_enable(self, on: bool):
        s = 'normal' if on else 'disabled'
        self._inp.configure(state=s)
        self._send.configure(state=s, fg=FG if on else FG_DIM)
        if on:
            self._inp.focus_set()

    def _write(self, text: str, tag: str = 'claude_txt'):
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

    def _clear_chat(self):
        self._chat.configure(state='normal')
        self._chat.delete('1.0', 'end')
        self._chat.configure(state='disabled')
        self._photo_refs.clear()

    # ── Analysis workers ──────────────────────────────────────────────────

    def _start(self):
        if self._state not in ('idle', 'done'):
            return
        target_date = self._date_var.get().strip()
        shots_dir   = BASE_DIR / 'logs' / target_date / 'screenshots'
        if not shots_dir.exists() or not any(shots_dir.glob('*.jpg')):
            self._status.configure(text=f'No screenshots for {target_date}')
            return
        self._state       = 'analyzing'
        self._target_date = target_date
        self._btn.configure(state='disabled')
        self._inp_enable(False)
        self._status.configure(text='Analyzing…')
        self._clear_chat()
        self._diag.delete('all')
        self._nb.select(0)
        threading.Thread(target=self._phase1,
                         args=(target_date, shots_dir), daemon=True).start()

    def _phase1(self, target_date: str, shots_dir: Path):
        try:
            client  = self._client()
            kf_n    = self._cfg.getint('analysis', 'keyframes_per_day', fallback=30)
            shots   = load_screenshots(shots_dir)
            kframes = select_keyframes(shots, kf_n)
            know    = load_knowledge()

            self._q.put(('label', 'Claude'))
            messages = [{'role': 'user',
                         'content': build_initial_content(kframes, target_date, know)}]
            response = self._stream(client, messages)
            analysis, questions = split_response(response)
            api_msgs = messages + [{'role': 'assistant', 'content': response}]

            if questions:
                self._emit_questions(questions, shots_dir)
                self._q.put(('waiting', questions, api_msgs))
            else:
                mmd = _save(target_date, analysis)
                if mmd:
                    self._q.put(('diagram', mmd))
                self._q.put(('done', target_date))

        except Exception as exc:
            self._q.put(('error', str(exc)))

    def _answer(self):
        if self._state != 'waiting':
            return
        text = self._inp.get('1.0', 'end').strip()
        if not text:
            return
        self._inp_enable(False)
        self._inp.delete('1.0', 'end')
        self._q.put(('user_echo', text))
        self._status.configure(text='Finalizing…')
        threading.Thread(target=self._phase2, args=(text,), daemon=True).start()

    def _phase2(self, answer: str):
        try:
            client   = self._client()
            api_msgs = self._api_messages + [{
                'role': 'user',
                'content': (
                    f'Clarifications:\n{answer}\n\n'
                    'Now produce the final complete analysis incorporating these answers. '
                    'Do not include a Questions section.'
                ),
            }]
            self._q.put(('label', 'Claude'))
            final = self._stream(client, api_msgs)
            append_knowledge(self._target_date, self._questions_section, answer)
            mmd = _save(self._target_date, final)
            if mmd:
                self._q.put(('diagram', mmd))
            self._q.put(('done', self._target_date))
        except Exception as exc:
            self._q.put(('error', str(exc)))

    def _stream(self, client: anthropic.Anthropic, messages: list) -> str:
        result = ''
        with client.messages.stream(
            model='claude-opus-4-7',
            max_tokens=4096,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                result += chunk
                self._q.put(('chunk', chunk))
        self._q.put(('nl',))
        return result

    def _emit_questions(self, section: str, shots_dir: Path):
        self._q.put(('nl',))
        self._q.put(('label', 'Claude — questions'))
        for line in section.split('\n'):
            if line.startswith('##') or 'reference the specific' in line.lower():
                continue
            for ts in re.findall(r'\b(\d{1,2}:\d{2})\b', line):
                shot = _closest_shot(shots_dir, ts)
                if shot:
                    thumb = _thumbnail(shot, 300)
                    if thumb:
                        self._q.put(('thumb', thumb))
            if line.strip():
                self._q.put(('question_line', line))

    # ── Diagram rendering ─────────────────────────────────────────────────

    def _render_mermaid(self, mmd: str):
        nodes, edges = _parse_mermaid(mmd)
        if not nodes:
            return

        pos = _layout(nodes, edges)
        if not pos:
            return

        # Shift all positions so top-left is at (D_PAD, D_PAD)
        min_x = min(x for x, _ in pos.values())
        min_y = min(y for _, y in pos.values())
        pos = {n: (x - min_x + D_PAD + BOX_W // 2,
                   y - min_y + D_PAD + BOX_H // 2)
               for n, (x, y) in pos.items()}

        c = self._diag
        c.delete('all')

        # Edges — stepped connectors
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

        # Nodes
        for nid, (x, y) in pos.items():
            x1, y1 = x - BOX_W // 2, y - BOX_H // 2
            x2, y2 = x + BOX_W // 2, y + BOX_H // 2
            c.create_rectangle(x1, y1, x2, y2,
                               fill=BG_INPUT, outline=ACCENT, width=1)
            c.create_text(x, y, text=nodes.get(nid, nid),
                          fill=FG, font=FONT_S,
                          width=BOX_W - 14, justify='center')

        c.configure(scrollregion=c.bbox('all'))

    def _client(self) -> anthropic.Anthropic:
        key = (self._cfg.get('api', 'anthropic_api_key', fallback='').strip()
               or os.environ.get('ANTHROPIC_API_KEY', ''))
        if not key:
            raise RuntimeError(
                'API key not configured in config.ini or ANTHROPIC_API_KEY env var.')
        return anthropic.Anthropic(api_key=key)


# ── Mermaid parser ────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
    return re.sub(r'<[^>]+>', '', s).strip()


def _parse_mermaid(mmd: str) -> tuple[dict, list]:
    nodes: dict[str, str] = {}
    edges: list = []

    for line in mmd.split('\n'):
        line = line.strip()
        if (not line
                or line.startswith('flowchart') or line.startswith('graph')
                or line.startswith('%%') or line.startswith('subgraph')
                or line == 'end'):
            continue

        # Collect node label definitions: id[label], id(label), id{label}
        for m in re.finditer(r'(\w+)\[([^\]]+)\]', line):
            nodes[m.group(1)] = _clean(m.group(2))
        for m in re.finditer(r'(\w+)\(([^)]+)\)', line):
            nodes.setdefault(m.group(1), _clean(m.group(2)))
        for m in re.finditer(r'(\w+)\{([^}]+)\}', line):
            nodes.setdefault(m.group(1), _clean(m.group(2)))

        # Collect edge: A --> B or A -->|label| B
        em = re.search(r'(\w+)\s*--[->xo]*(?:\|([^|]*)\|)?\s*(\w+)', line)
        if em:
            edges.append((em.group(1), em.group(3), _clean(em.group(2) or '')))

    for a, b, _ in edges:
        nodes.setdefault(a, a)
        nodes.setdefault(b, b)

    return nodes, edges


# ── Layered layout (Sugiyama-lite) ────────────────────────────────────────

def _layout(nodes: dict, edges: list) -> dict:
    if not nodes:
        return {}

    children: dict[str, list] = {n: [] for n in nodes}
    parents:  dict[str, set]  = {n: set() for n in nodes}
    for a, b, _ in edges:
        if b not in children[a]:
            children[a].append(b)
        parents[b].add(a)

    # Longest-path layering via topological BFS
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

    # Group by layer
    groups: dict[int, list] = {}
    for n, l in layer.items():
        groups.setdefault(l, []).append(n)

    # Order within layer by parent centroid to reduce crossings
    for l in sorted(groups):
        if l == 0:
            continue
        prev_idx = {n: i for i, n in enumerate(groups.get(l - 1, []))}
        def centroid(n, pi=prev_idx):
            ps = [pi[p] for p in parents[n] if p in pi]
            return sum(ps) / len(ps) if ps else 0
        groups[l].sort(key=centroid)

    # Pixel positions (centered around x=0 per layer)
    pos: dict[str, tuple] = {}
    for l, group in groups.items():
        total_w = len(group) * BOX_W + (len(group) - 1) * H_GAP
        start_x = -total_w / 2 + BOX_W / 2
        y = l * (BOX_H + V_GAP)
        for i, n in enumerate(group):
            pos[n] = (start_x + i * (BOX_W + H_GAP), y)

    return pos


# ── File helpers ──────────────────────────────────────────────────────────

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


def _save(target_date: str, analysis: str) -> str | None:
    out = BASE_DIR / 'logs' / target_date
    out.mkdir(parents=True, exist_ok=True)
    (out / 'analysis.md').write_text(analysis, encoding='utf-8')
    if '```mermaid' in analysis:
        s   = analysis.index('```mermaid') + 10
        e   = analysis.index('```', s)
        mmd = analysis[s:e].strip()
        (out / 'workflow.mmd').write_text(mmd, encoding='utf-8')
        return mmd
    return None


if __name__ == '__main__':
    root = tk.Tk()
    PulsarApp(root)
    root.mainloop()
