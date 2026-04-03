"""
CoALa Stock Alarm - Windows Desktop App
네이버 증권 기반 주식 가격 추적 + TTS 알람
종목코드 직접 입력 방식
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
import time
import json
import os
import sys
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import pyttsx3
import hmac
import hashlib
import uuid
import base64
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.naver.com",
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
POLL_INTERVAL     = 1.0   # 초
COALA_SERVER_URL  = "https://api.coinsight.co.kr"  # CoALa 서버

def make_hmac_token(device_id: str) -> tuple[str, int]:
    """서버 인증용 HMAC 토큰 생성. 반환: (token, timestamp)"""
    ts = int(time.time())
    token = hmac.new(
        device_id.encode(),
        f"{device_id}:{ts}".encode(),
        hashlib.sha256
    ).hexdigest()
    return token, ts

def aes_encrypt(data: str, password: str) -> str:
    """AES-256-CBC 암호화. 반환: base64 문자열"""
    if not HAS_CRYPTO:
        return data  # 라이브러리 없으면 평문 (경고만)
    key = hashlib.sha256(password.encode()).digest()
    iv  = hashlib.md5(password.encode()).digest()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(data.encode(), AES.block_size))
    return base64.b64encode(ct).decode()

def aes_decrypt(enc_b64: str, password: str) -> str:
    """AES-256-CBC 복호화"""
    if not HAS_CRYPTO:
        return enc_b64
    key = hashlib.sha256(password.encode()).digest()
    iv  = hashlib.md5(password.encode()).digest()
    ct  = base64.b64decode(enc_b64)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size).decode()

def _config_path() -> str:
    """실행 파일과 같은 위치에 config.json 저장"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "coala_stock_config.json")


# ─────────────────────────────────────────
# 네이버 증권 API
# ─────────────────────────────────────────
def _pick_price(data: dict) -> tuple[float, str, str, str]:
    """
    시장 상태에 따라 최적 현재가 반환
    반환: (price, change, rate, direction)
    KRX 장중 → closePrice
    KRX 종료 + NXT 오버마켓 진행 중 → overMarketPriceInfo.overPrice
    """
    market_status = data.get("marketStatus", "")
    over = data.get("overMarketPriceInfo") or {}
    over_status = over.get("overMarketStatus", "")

    # NXT 오버마켓(프리/애프터) 진행 중이고 KRX 종료 상태
    if market_status != "OPEN" and over_status == "OPEN" and over.get("overPrice"):
        price = float(over["overPrice"].replace(",", ""))
        change = over.get("compareToPreviousClosePrice", "0").replace(",", "")
        rate   = over.get("fluctuationsRatio", "0")
        direction = over.get("compareToPreviousPrice", {}).get("name", "")
        return price, change, rate, direction

    # KRX 장중 or 장 종료(종가)
    price_str = data.get("closePrice", "0").replace(",", "")
    price = float(price_str) if price_str else 0
    change = data.get("compareToPreviousClosePrice", "0").replace(",", "")
    rate   = data.get("fluctuationsRatio", "0")
    direction = data.get("compareToPreviousPrice", {}).get("name", "")
    return price, change, rate, direction


def _market_label(data: dict) -> str:
    """현재 시장 상태 라벨 반환"""
    market_status = data.get("marketStatus", "")
    over = data.get("overMarketPriceInfo") or {}
    over_status = over.get("overMarketStatus", "")
    session = over.get("tradingSessionType", "")

    if market_status == "OPEN":
        return "KRX 정규장"
    if over_status == "OPEN":
        label_map = {
            "PRE_MARKET":   "NXT 프리마켓",
            "AFTER_MARKET": "NXT 애프터마켓",
        }
        return label_map.get(session, "NXT 오버마켓")
    return "장 종료"


def get_stock_info(code: str) -> Optional[dict]:
    """종목 기본정보 + 현재가 조회 (KRX/NXT 통합)"""
    try:
        url = f"https://m.stock.naver.com/api/stock/{code}/basic"
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        price, change, rate, direction = _pick_price(data)
        return {
            "code": data.get("itemCode", code),
            "name": data.get("stockName", code),
            "price": price,
            "change": change,
            "rate": rate,
            "direction": direction,
            "market_status": data.get("marketStatus", ""),
            "market_label": _market_label(data),
        }
    except Exception as e:
        return None


def get_price(code: str) -> Optional[float]:
    """현재가 빠르게 조회 (폴링용) - KRX/NXT 통합"""
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
        resp = requests.get(url, headers=HEADERS, timeout=3)
        if resp.status_code != 200:
            return None
        datas = resp.json().get("datas", [])
        if not datas:
            return None
        price, *_ = _pick_price(datas[0])
        return price if price else None
    except Exception:
        try:
            info = get_stock_info(code)
            return info["price"] if info else None
        except Exception:
            return None


# ─────────────────────────────────────────
# 데이터 클래스
# ─────────────────────────────────────────
@dataclass
class StockAlarm:
    code: str
    name: str
    gap: int
    base_price: float
    initial_base_price: float = 0.0
    tts_template: str = "{name} {direction} {price}원 기준가 대비 {diff}원"
    upper: float = 0
    lower: float = 0
    running: bool = True
    last_price: float = 0

    def __post_init__(self):
        if self.initial_base_price == 0.0:
            self.initial_base_price = self.base_price
        self._update_bounds()

    def _update_bounds(self):
        self.upper = self.base_price + self.gap
        self.lower = self.base_price - self.gap

    def diff_from_initial(self, price: float) -> float:
        return price - self.initial_base_price

    def check_and_update(self, price: float) -> Optional[str]:
        self.last_price = price
        if price >= self.upper:
            self.base_price = price
            self._update_bounds()
            return "UP"
        elif price <= self.lower:
            self.base_price = price
            self._update_bounds()
            return "DOWN"
        return None


# ─────────────────────────────────────────
# 면책조항
# ─────────────────────────────────────────
GITHUB_URL = "https://github.com/coinsightcokr-dev/CoAla-korean-stock-TTS-alarm"

DISCLAIMER_LINES = [
    "본 프로그램은 네이버 증권 홈페이지에서 주가 데이터를",
    "개인이 직접 수집하고 알람을 받기 위한 도구입니다.",
    "",
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    "",
    "• 이 프로그램은 검색, 가격 수집, 알람 기능에 대한",
    "  편의를 제공할 뿐입니다.",
    "",
    "• 수집된 데이터의 신뢰성, 시간 지연으로 인한 차이,",
    "  또는 데이터 오류로 발생하는 어떠한 결과에 대해서도",
    "  책임을 지지 않습니다.",
    "",
    "• 이 프로그램을 이용하여 데이터를 수집하고 활용하는",
    "  모든 행위는 전적으로 사용자 본인의 책임입니다.",
    "",
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    "",
    "⚠  배포 경로 확인",
    "",
    "본 프로그램을 아래 GitHub 외 다른 경로에서 받은 경우,",
    "바이러스 감염 등의 위험이 있으므로 절대 실행하지 마시고",
    "반드시 아래 공식 GitHub에서 다운로드하여 사용하십시오.",
    "",
    "  👉  https://github.com/coinsightcokr-dev/CoAla-korean-stock-TTS-alarm",
    "",
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    "",
    "위 내용에 동의하시면 '동의' 버튼을 눌러주세요.",
    "동의하지 않으시면 프로그램이 종료됩니다.",
]


def _build_disclaimer_window(parent, is_modal=True, on_agree=None, on_decline=None):
    """
    약관 창 빌드 헬퍼.
    is_modal=True  → 최초 동의용 (Tk root)
    is_modal=False → 재열람용 (Toplevel)
    """
    import webbrowser as _wb

    win = parent
    BG, PANEL, TEXT, ACCENT = "#1a1a2e", "#16213e", "#eaeaea", "#e94560"

    win.title("이용 약관")
    win.configure(bg=BG)
    win.resizable(False, False)
    w, h = 560, 520
    win.geometry(f"{w}x{h}")
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    tk.Label(win, text="이용 약관",
             bg=BG, fg=ACCENT,
             font=("Malgun Gothic", 14, "bold")).pack(pady=(20, 8))

    # 본문 (Text 위젯으로 링크 클릭 지원)
    text_frame = tk.Frame(win, bg=PANEL, padx=16, pady=14)
    text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

    txt = tk.Text(text_frame,
                  bg=PANEL, fg=TEXT,
                  font=("Malgun Gothic", 9),
                  wrap=tk.WORD,
                  relief=tk.FLAT,
                  cursor="arrow",
                  state=tk.NORMAL,
                  padx=4, pady=4)
    txt.pack(fill=tk.BOTH, expand=True)

    # 링크 태그
    txt.tag_config("link",
                   foreground="#74b9ff",
                   underline=True,
                   font=("Malgun Gothic", 9))
    txt.tag_bind("link", "<Button-1>",
                 lambda e: _wb.open(GITHUB_URL))
    txt.tag_bind("link", "<Enter>",
                 lambda e: txt.config(cursor="hand2"))
    txt.tag_bind("link", "<Leave>",
                 lambda e: txt.config(cursor="arrow"))

    # 본문 삽입
    for line in DISCLAIMER_LINES:
        if GITHUB_URL in line:
            # GitHub URL 줄: 앞부분 + 링크
            prefix = line[:line.index(GITHUB_URL)]
            txt.insert(tk.END, prefix)
            txt.insert(tk.END, GITHUB_URL, "link")
            txt.insert(tk.END, "\n")
        else:
            txt.insert(tk.END, line + "\n")

    txt.config(state=tk.DISABLED)

    # 버튼
    btn_frame = tk.Frame(win, bg=BG)
    btn_frame.pack(pady=(0, 16))

    if is_modal:
        tk.Button(btn_frame, text="✅  동의합니다",
                  bg="#00b894", fg="white",
                  relief="flat", font=("Malgun Gothic", 10, "bold"),
                  cursor="hand2", padx=20, pady=8,
                  command=on_agree).pack(side=tk.LEFT, padx=(0, 12))
        tk.Button(btn_frame, text="❌  거부 (종료)",
                  bg="#e17055", fg="white",
                  relief="flat", font=("Malgun Gothic", 10),
                  cursor="hand2", padx=20, pady=8,
                  command=on_decline).pack(side=tk.LEFT)
    else:
        tk.Button(btn_frame, text="닫기",
                  bg="#636e72", fg="white",
                  relief="flat", font=("Malgun Gothic", 10),
                  cursor="hand2", padx=20, pady=8,
                  command=win.destroy).pack()
        tk.Button(btn_frame, text="GitHub 열기  🔗",
                  bg="#0984e3", fg="white",
                  relief="flat", font=("Malgun Gothic", 9),
                  cursor="hand2", padx=14, pady=6,
                  command=lambda: _wb.open(GITHUB_URL)
                  ).pack(pady=(4, 0))


def _show_disclaimer() -> bool:
    """
    최초 실행 시 면책조항 동의 팝업.
    동의하면 True, 거부하면 False 반환.
    설정 파일에 동의 기록 저장.
    """
    import os, json as _json
    cfg_path = _config_path()
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = _json.load(f)
            if cfg.get("disclaimer_agreed"):
                print(f"[DISCLAIMER] 이미 동의됨: {cfg_path}")
                return True
        except Exception as e:
            print(f"[DISCLAIMER] 설정 파일 읽기 실패: {e}")
    else:
        print(f"[DISCLAIMER] 설정 파일 없음: {cfg_path}")

    agreed = [False]
    root = tk.Tk()

    def _agree():
        agreed[0] = True
        try:
            cfg = {}
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, encoding="utf-8") as f:
                        cfg = _json.load(f)
                except Exception:
                    cfg = {}
            cfg["disclaimer_agreed"] = True
            os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"[DISCLAIMER] 동의 저장 완료: {cfg_path}")
        except Exception as e:
            print(f"[DISCLAIMER] 저장 실패: {e}")
        root.destroy()

    def _decline():
        agreed[0] = False
        root.destroy()

    _build_disclaimer_window(root, is_modal=True,
                             on_agree=_agree, on_decline=_decline)
    root.protocol("WM_DELETE_WINDOW", _decline)
    root.mainloop()
    return agreed[0]


# ─────────────────────────────────────────
# TTS
# ─────────────────────────────────────────
class TtsEngine:
    def __init__(self):
        self._lock = threading.Lock()

    def speak(self, text: str):
        def _run():
            with self._lock:
                try:
                    engine = pyttsx3.init()
                    engine.setProperty("rate", 150)
                    engine.say(text)
                    engine.runAndWait()
                    engine.stop()
                except Exception as e:
                    print(f"[TTS] 오류: {e}")
        threading.Thread(target=_run, daemon=True).start()


tts = TtsEngine()


# ─────────────────────────────────────────
# 폴링 엔진
# ─────────────────────────────────────────
class AlarmPoller:
    def __init__(self, on_alert):
        self._alarms: dict[str, StockAlarm] = {}
        self._lock = threading.Lock()
        self._running = False
        self._on_alert = on_alert

    def add(self, aid: str, alarm: StockAlarm):
        with self._lock:
            self._alarms[aid] = alarm
        if not self._running:
            self._running = True
            threading.Thread(target=self._loop, daemon=True).start()

    def remove(self, aid: str):
        with self._lock:
            self._alarms.pop(aid, None)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._alarms)

    def _loop(self):
        while True:
            with self._lock:
                items = list(self._alarms.items())
            for aid, alarm in items:
                if not alarm.running:
                    continue
                price = get_price(alarm.code)
                if price is None:
                    continue
                direction = alarm.check_and_update(price)
                if direction:
                    self._on_alert(aid, alarm, price, direction)
            time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────
# GUI
# ─────────────────────────────────────────
class App(tk.Tk):
    # 색상
    BG    = "#1a1a2e"
    PANEL = "#16213e"
    ENTRY = "#0f3460"
    ACCENT= "#e94560"
    TEXT  = "#eaeaea"
    MUTED = "#8892a4"
    GREEN = "#00b894"
    RED   = "#e17055"
    YELLOW= "#fdcb6e"

    def __init__(self):
        super().__init__()
        self.title("CoALa (Coin Alarm) Stock Alarm  by coinsight.co.kr")
        self.configure(bg=self.BG)
        self.resizable(True, True)
        # 창 크기는 _load_config에서 복원

        self._alarm_rows: dict[str, dict] = {}
        self._lookup_history: list[dict] = []
        self.poller = AlarmPoller(self._on_alert)

        self._build()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_resize)
        # 설정값 변경 시 자동 저장
        self.device_id_var.trace_add("write", lambda *_: self._save_config())
        self.password_var.trace_add("write",  lambda *_: self._save_config())
        self.tts_local_var.trace_add("write", lambda *_: self._save_config())
        self.tts_var.trace_add("write",       lambda *_: self._save_config())
        # coala_var는 _toggle_coala_detail에서 처리

        # 로드 후 detail 표시 상태 동기화
        self.after(200, lambda: self._coala_detail.pack(
            fill=tk.X, pady=(6, 0)) if self.coala_var.get()
            else self._coala_detail.pack_forget())

    # ── 빌드 ────────────────────────────────
    def _build(self):
        # 타이틀
        title_frame = tk.Frame(self, bg=self.BG)
        title_frame.pack(pady=(10, 2))
        tk.Label(title_frame, text="CoALa Stock Alarm",
                 bg=self.BG, fg=self.ACCENT,
                 font=("Malgun Gothic", 16, "bold")).pack()
        # 서브타이틀 + 약관 보기 버튼 한 줄
        sub_row = tk.Frame(title_frame, bg=self.BG)
        sub_row.pack()
        tk.Label(sub_row,
                 text="Coin Alarm  ·  by coinsight.co.kr",
                 bg=self.BG, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(side=tk.LEFT)
        tk.Label(sub_row, text="  │  ", bg=self.BG, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(side=tk.LEFT)
        tk.Button(sub_row, text="이용 약관",
                  bg=self.BG, fg="#74b9ff",
                  relief="flat", bd=0,
                  font=("Malgun Gothic", 8, "underline"),
                  cursor="hand2",
                  command=self._show_terms).pack(side=tk.LEFT)

        main = tk.Frame(self, bg=self.BG)
        main.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        self._build_left(main)
        self._build_right(main)

    def _build_left(self, parent):
        # 왼쪽 전체를 스크롤 가능하게
        left_outer = tk.Frame(parent, bg=self.PANEL)
        left_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        left_canvas = tk.Canvas(left_outer, bg=self.PANEL, highlightthickness=0)
        left_sb = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left = tk.Frame(left_canvas, bg=self.PANEL)

        left.bind("<Configure>",
            lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.create_window((0, 0), window=left, anchor="nw")
        left_canvas.configure(yscrollcommand=left_sb.set)

        # 마우스 휠 스크롤
        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        left_canvas.bind("<MouseWheel>", _on_mousewheel)
        left.bind("<MouseWheel>", _on_mousewheel)

        left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── 종목 코드 입력
        self._section(left, "종목 코드")
        code_row = tk.Frame(left, bg=self.PANEL)
        code_row.pack(fill=tk.X, padx=12, pady=(2, 0))

        self.code_var = tk.StringVar()
        code_entry = tk.Entry(code_row, textvariable=self.code_var,
                              bg=self.ENTRY, fg=self.TEXT,
                              insertbackground=self.TEXT,
                              font=("Malgun Gothic", 13, "bold"),
                              relief="flat", bd=6, width=12)
        code_entry.pack(side=tk.LEFT)
        code_entry.bind("<Return>", lambda e: self._lookup_code())

        tk.Button(code_row, text="조회", bg=self.ACCENT, fg="white",
                  relief="flat", font=("Malgun Gothic", 9, "bold"),
                  cursor="hand2", padx=10,
                  command=self._lookup_code).pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(code_row, text="예) 005930",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(side=tk.LEFT, padx=(10, 0))

        # ── 조회 히스토리
        self._section(left, "최근 조회")
        self._history_frame = tk.Frame(left, bg=self.PANEL)
        self._history_frame.pack(fill=tk.X, padx=12, pady=(2, 0))
        self._lookup_history: list[dict] = []  # [{code, name}]

        # ── 종목 정보 표시
        info_frame = tk.Frame(left, bg=self.ENTRY, bd=0)
        info_frame.pack(fill=tk.X, padx=12, pady=(8, 0))

        self.name_var  = tk.StringVar(value="종목명: -")
        self.price_var = tk.StringVar(value="현재가: -")
        self.change_var= tk.StringVar(value="등락: -")

        tk.Label(info_frame, textvariable=self.name_var,
                 bg=self.ENTRY, fg=self.TEXT,
                 font=("Malgun Gothic", 11, "bold"),
                 anchor="w").pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(info_frame, textvariable=self.price_var,
                 bg=self.ENTRY, fg=self.GREEN,
                 font=("Malgun Gothic", 14, "bold"),
                 anchor="w").pack(fill=tk.X, padx=10)
        tk.Label(info_frame, textvariable=self.change_var,
                 bg=self.ENTRY, fg=self.MUTED,
                 font=("Malgun Gothic", 9),
                 anchor="w").pack(fill=tk.X, padx=10, pady=(0, 8))

        # ── Gap 설정
        self._section(left, "Gap (원)")
        gap_row = tk.Frame(left, bg=self.PANEL)
        gap_row.pack(fill=tk.X, padx=12, pady=(2, 0))

        self.gap_var = tk.StringVar(value="100")
        tk.Entry(gap_row, textvariable=self.gap_var,
                 bg=self.ENTRY, fg=self.TEXT,
                 insertbackground=self.TEXT,
                 font=("Malgun Gothic", 12), relief="flat", bd=6,
                 width=10).pack(side=tk.LEFT)
        tk.Label(gap_row,
                 text="← 이 간격마다 알람 발생",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(side=tk.LEFT, padx=8)

        # ── 기준가
        self._section(left, "기준가 (원)")
        base_row = tk.Frame(left, bg=self.PANEL)
        base_row.pack(fill=tk.X, padx=12, pady=(2, 0))
        self.base_price_var = tk.StringVar(value="")
        tk.Entry(base_row, textvariable=self.base_price_var,
                 bg=self.ENTRY, fg=self.TEXT,
                 insertbackground=self.TEXT,
                 font=("Malgun Gothic", 12), relief="flat", bd=6,
                 width=12).pack(side=tk.LEFT)
        tk.Label(base_row, text="← 비워두면 현재가 자동 입력",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(side=tk.LEFT, padx=8)

        # ── TTS 문구
        self._section(left, "TTS 알람 문구")
        tk.Entry(left, textvariable=tk.StringVar(), bg=self.ENTRY,
                 fg=self.TEXT, insertbackground=self.TEXT,
                 font=("Malgun Gothic", 10), relief="flat", bd=6).pack(
                     fill=tk.X, padx=12, pady=(2, 0))

        # tts_var를 직접 Entry에 연결
        left.pack_slaves()[-1].destroy()  # 위 임시 entry 제거
        self.tts_var = tk.StringVar(value="{name} {direction} {price}원 기준가 대비 {diff}원")
        tts_entry = tk.Entry(left, textvariable=self.tts_var,
                             bg=self.ENTRY, fg=self.TEXT,
                             insertbackground=self.TEXT,
                             font=("Malgun Gothic", 10), relief="flat", bd=6)
        tts_entry.pack(fill=tk.X, padx=12, pady=(2, 0))
        tk.Label(left, text="{name} {price} {direction} {gap} {diff} {rate} 사용 가능",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(anchor="w", padx=12, pady=(2, 0))

        # TTS 테스트 버튼
        tk.Button(left, text="🔊 TTS 테스트",
                  bg=self.ENTRY, fg=self.TEXT,
                  relief="flat", font=("Malgun Gothic", 9),
                  cursor="hand2", padx=8,
                  command=self._test_tts).pack(anchor="w", padx=12, pady=(4, 0))

        # ── 알림 방식
        self._section(left, "알림 방식")

        notify_frame = tk.Frame(left, bg=self.PANEL)
        notify_frame.pack(fill=tk.X, padx=12, pady=(4, 0))

        self.tts_local_var = tk.BooleanVar(value=True)
        self.coala_var     = tk.BooleanVar(value=False)
        self.device_id_var = tk.StringVar()
        self.password_var  = tk.StringVar()

        def _chk_style(var):
            return dict(variable=var,
                        bg=self.PANEL, fg=self.TEXT,
                        selectcolor=self.ENTRY,
                        activebackground=self.PANEL,
                        activeforeground=self.TEXT,
                        font=("Malgun Gothic", 9),
                        cursor="hand2")

        # 윈도우 TTS 체크
        tk.Checkbutton(notify_frame, text="🖥  윈도우 TTS",
                       **_chk_style(self.tts_local_var)
                       ).pack(anchor="w", pady=(2, 0))

        # CoALa 앱 체크 + 도움말 버튼 (한 줄)
        coala_chk_row = tk.Frame(notify_frame, bg=self.PANEL)
        coala_chk_row.pack(fill=tk.X, pady=(4, 0))

        coala_chk = tk.Checkbutton(coala_chk_row, text="📱  CoALa 앱",
                       **_chk_style(self.coala_var))
        coala_chk.pack(side=tk.LEFT)

        tk.Button(coala_chk_row, text="?",
                  bg=self.MUTED, fg="white",
                  relief="flat", font=("Malgun Gothic", 8, "bold"),
                  cursor="hand2", width=2,
                  command=self._show_coala_help
                  ).pack(side=tk.LEFT, padx=(6, 0))

        # CoALa 연동 상세 (체크 시만 표시)
        self._coala_detail = tk.Frame(notify_frame, bg=self.ENTRY)

        tk.Label(self._coala_detail, text="Device ID",
                 bg=self.ENTRY, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(anchor="w", padx=8, pady=(8,0))
        self._device_id_entry = tk.Entry(
                 self._coala_detail, textvariable=self.device_id_var,
                 bg=self.PANEL, fg=self.TEXT,
                 insertbackground=self.TEXT,
                 font=("Malgun Gothic", 9), relief="flat", bd=4)
        self._device_id_entry.pack(fill=tk.X, padx=8, pady=(2,0))

        tk.Label(self._coala_detail, text="동기화 코드 (숫자 4자리 이상)",
                 bg=self.ENTRY, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(anchor="w", padx=8, pady=(6,0))
        self._password_entry = tk.Entry(
                 self._coala_detail, textvariable=self.password_var,
                 bg=self.PANEL, fg=self.TEXT,
                 insertbackground=self.TEXT,
                 font=("Malgun Gothic", 11, "bold"), relief="flat", bd=4)
        self._password_entry.pack(fill=tk.X, padx=8, pady=(2,0))

        self.coala_status = tk.Label(self._coala_detail, text="",
                                      bg=self.ENTRY, fg=self.MUTED,
                                      font=("Malgun Gothic", 8))
        self.coala_status.pack(anchor="w", padx=8, pady=(4, 8))

        # 체크 변경 시 detail 표시/숨김
        def _toggle_coala_detail(*_):
            if self.coala_var.get():
                self._coala_detail.pack(fill=tk.X, pady=(6, 0))
            else:
                self._coala_detail.pack_forget()
            self._save_config()

        self.coala_var.trace_add("write", _toggle_coala_detail)

        # ── 알람 추가
        tk.Button(left, text="＋  알람 추가",
                  bg=self.GREEN, fg="white",
                  relief="flat", font=("Malgun Gothic", 11, "bold"),
                  cursor="hand2", pady=9,
                  command=self._add_alarm).pack(
                      fill=tk.X, padx=12, pady=(14, 4))

        # ── 등록된 알람 목록
        self._section(left, "등록된 알람")
        self.alarm_frame = tk.Frame(left, bg=self.PANEL)
        self.alarm_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

    def _build_right(self, parent):
        right = tk.Frame(parent, bg=self.PANEL, width=270)
        right.pack(side=tk.RIGHT, fill=tk.BOTH)
        right.pack_propagate(False)

        self._section(right, "알람 로그")

        self.log = tk.Text(right, bg=self.ENTRY, fg=self.TEXT,
                            relief="flat", font=("Consolas", 9),
                            state="disabled", wrap="word", bd=0)
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 0))
        self.log.tag_config("UP",   foreground=self.GREEN)
        self.log.tag_config("DOWN", foreground=self.RED)
        self.log.tag_config("INFO", foreground=self.MUTED)
        self.log.tag_config("ERR",  foreground=self.YELLOW)

        tk.Button(right, text="로그 지우기",
                  bg=self.PANEL, fg=self.MUTED,
                  relief="flat", font=("Malgun Gothic", 8),
                  cursor="hand2",
                  command=lambda: (self.log.configure(state="normal"),
                                   self.log.delete("1.0", tk.END),
                                   self.log.configure(state="disabled"))
                  ).pack(pady=6)

    def _show_terms(self):
        """이용 약관 재열람 팝업"""
        win = tk.Toplevel(self)
        win.grab_set()
        _build_disclaimer_window(win, is_modal=False)

    def _show_coala_help(self):
        """CoALa 앱 연동 도움말 팝업"""
        import webbrowser
        win = tk.Toplevel(self)
        win.title("CoALa 앱 연동 도움말")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.geometry("500x500")
        win.grab_set()

        tk.Label(win, text="📱  CoALa 앱 연동 방법",
                 bg=self.BG, fg=self.ACCENT,
                 font=("Malgun Gothic", 13, "bold")).pack(pady=(20, 4))

        lines_msg = [
            "CoALa 앱은 이 PC 프로그램에서 발생한 알람을",
            "스마트폰 TTS로 실시간 읽어주는 앱입니다.",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "① 구글 플레이스토어에서 'CoALa' 앱을 설치합니다.",
            "",
            "② 앱 실행 후 [프로필 → Device ID 공유하기]를",
            "    눌러 메일 또는 메신저로 Device ID를 받습니다.",
            "    받은 Device ID를 이 프로그램에 붙여넣으세요.",
            "",
            "③ 앱 프로필에서 '동기화 코드'를 설정합니다.",
            "    이 프로그램에도 동일한 동기화 코드를 입력하세요.",
            "    동기화 코드는 서버에 저장되지 않습니다.",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🔒  보안 안내",
            "",
            "모든 알람 내용은 암호화되어 전송됩니다.",
            "서버는 암호화된 데이터만 중계하며,",
            "내용을 열람하거나 저장하지 않습니다.",
        ]
        msg = "\n".join(lines_msg)

        text_frame = tk.Frame(win, bg=self.PANEL, padx=16, pady=12)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        tk.Label(text_frame, text=msg,
                 bg=self.PANEL, fg=self.TEXT,
                 font=("Malgun Gothic", 9),
                 justify="left", anchor="w"
                 ).pack(fill=tk.BOTH, expand=True)

        btn_row = tk.Frame(win, bg=self.BG)
        btn_row.pack(pady=(0, 16))

        tk.Button(btn_row, text="🛒  플레이스토어 열기",
                  bg=self.ACCENT, fg="white",
                  relief="flat", font=("Malgun Gothic", 9, "bold"),
                  cursor="hand2", padx=12, pady=6,
                  command=lambda: webbrowser.open(
                      "https://play.google.com/store/apps/details"
                      "?id=kr.co.coinsight.coala")
                  ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(btn_row, text="🌐  홈페이지",
                  bg=self.ENTRY, fg=self.TEXT,
                  relief="flat", font=("Malgun Gothic", 9),
                  cursor="hand2", padx=12, pady=6,
                  command=lambda: webbrowser.open("https://www.coinsight.co.kr")
                  ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(btn_row, text="닫기",
                  bg=self.ENTRY, fg=self.TEXT,
                  relief="flat", font=("Malgun Gothic", 9),
                  cursor="hand2", padx=12, pady=6,
                  command=win.destroy
                  ).pack(side=tk.LEFT)

    def _refresh_history_ui(self):
        """히스토리 버튼 UI 갱신"""
        frame = getattr(self, "_history_frame", None)
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        for h in self._lookup_history:
            code = h["code"]
            name = h["name"]
            btn = tk.Button(
                frame,
                text=f"{name} ({code})",
                bg=self.ENTRY, fg=self.MUTED,
                relief="flat", font=("Malgun Gothic", 8),
                cursor="hand2", padx=6, pady=2,
                anchor="w",
                command=lambda c=code: self._lookup_from_history(c)
            )
            btn.pack(fill=tk.X, pady=1)

    def _lookup_from_history(self, code: str):
        self.code_var.set(code)
        self._lookup_code()

    def _section(self, parent, text):
        tk.Label(parent, text=text,
                 bg=self.PANEL if parent != self else self.BG,
                 fg=self.MUTED, font=("Malgun Gothic", 8)
                 ).pack(anchor="w", padx=12, pady=(10, 0))

    # ── 종목 조회 ────────────────────────────
    def _lookup_code(self):
        code = self.code_var.get().strip().zfill(6)
        if not code:
            return
        self.name_var.set("조회 중...")
        self.price_var.set("")
        self.change_var.set("")
        threading.Thread(target=self._do_lookup, args=(code,), daemon=True).start()

    def _do_lookup(self, code):
        info = get_stock_info(code)
        if not info:
            self.after(0, lambda: (
                self.name_var.set("❌ 종목을 찾을 수 없습니다"),
                self.price_var.set(""),
                self.change_var.set("코드를 다시 확인해주세요"),
            ))
            return

        dir_map = {"RISING": "▲", "FALLING": "▼", "EVEN": "━"}
        dir_sym = dir_map.get(info["direction"], "")
        change_sign = "+" if info["direction"] == "RISING" else ""
        try:
            change_int = int(float(info["change"]))
            change_text = (f"{dir_sym} {change_sign}{change_int:,}원"
                           f"  ({change_sign}{info['rate']}%)"
                           f"  [{info.get('market_label', '')}]")
        except Exception:
            change_text = f"{info['rate']}%  [{info.get('market_label', '')}]"

        self._current_info = info

        price_str = f"{info['price']:,.0f}"
        # 히스토리 추가
        entry = {"code": info["code"], "name": info["name"]}
        self._lookup_history = [h for h in self._lookup_history
                                 if h["code"] != info["code"]]
        self._lookup_history.insert(0, entry)
        self._lookup_history = self._lookup_history[:10]
        self._save_config()

        self.after(0, lambda: (
            self.name_var.set(f"{info['name']}  ({info['code']})"),
            self.price_var.set(f"{info['price']:,.0f}원"),
            self.change_var.set(change_text),
            self.base_price_var.set(price_str),
            self._refresh_history_ui(),
        ))

    # ── TTS 테스트 ───────────────────────────
    def _test_tts(self):
        info = getattr(self, "_current_info", None)
        if not info:
            tts.speak("테스트 알람입니다")
            return
        try:
            base = float(self.base_price_var.get().replace(",", ""))
        except Exception:
            base = info["price"]
        diff = info["price"] - base
        diff_sign = "+" if diff >= 0 else ""
        text = (self.tts_var.get()
                .replace("{name}", info["name"])
                .replace("{price}", f"{info['price']:,.0f}")
                .replace("{direction}", "상승")
                .replace("{gap}", self.gap_var.get())
                .replace("{diff}", f"{diff_sign}{diff:,.0f}")
                .replace("{rate}", info["rate"]))
        tts.speak(text)

    # ── 알람 추가 ────────────────────────────
    def _add_alarm(self):
        info = getattr(self, "_current_info", None)
        if not info:
            messagebox.showwarning("알림", "먼저 종목 코드를 조회하세요.")
            return
        try:
            gap = int(self.gap_var.get().replace(",", ""))
            assert gap > 0
        except Exception:
            messagebox.showwarning("알림", "Gap은 양의 정수로 입력하세요.")
            return

        price = info["price"]
        if price <= 0:
            messagebox.showerror("오류", "현재가를 확인할 수 없습니다.")
            return

        # ── CoALa 체크 시 검증을 알람 등록 전에 수행 ──
        if self.coala_var.get():
            _did = self.device_id_var.get().strip()
            _pw  = self.password_var.get().strip()
            if not _did:
                messagebox.showwarning(
                    "CoALa 연동 오류",
                    "Device ID가 입력되지 않았습니다.\n"
                    "CoALa 앱 > 프로필에서 Device ID를 공유하여 입력해주세요.")
                return
            if not _pw or len(_pw) < 4 or not _pw.isdigit():
                messagebox.showwarning(
                    "CoALa 연동 오류",
                    "동기화 코드가 입력되지 않았거나 형식이 올바르지 않습니다.\n"
                    "숫자 4자리 이상의 동기화 코드를 입력해주세요.\n"
                    "(CoALa 앱 > 프로필 > 동기화 코드)")
                return

        try:
            base_price = float(self.base_price_var.get().replace(",", ""))
            if base_price <= 0:
                raise ValueError
        except Exception:
            base_price = price

        aid = f"alarm_{uuid.uuid4().hex[:12]}"

        alarm = StockAlarm(
            code=info["code"],
            name=info["name"],
            gap=gap,
            base_price=base_price,
            initial_base_price=base_price,
            tts_template=self.tts_var.get(),
        )
        self.poller.add(aid, alarm)
        self._render_alarm_row(aid, alarm)
        self._log(f"알람 추가 | {alarm.name} ({alarm.code}) "
                  f"기준가={price:,.0f}원  gap={gap:,}", "INFO")
        self._save_config()

        # CoALa 서버 등록 (검증 통과 후 실행)
        if self.coala_var.get():
            threading.Thread(
                target=self._register_to_coala,
                args=(aid, alarm),
                daemon=True
            ).start()

    def _render_alarm_row(self, aid: str, alarm: StockAlarm):
        row = tk.Frame(self.alarm_frame, bg=self.ENTRY, pady=7, padx=10)
        row.pack(fill=tk.X, pady=3)

        # 종목명
        tk.Label(row, text=f"{alarm.name}  ({alarm.code})",
                 bg=self.ENTRY, fg=self.TEXT,
                 font=("Malgun Gothic", 9, "bold")).pack(anchor="w")

        # 현재가 (실시간)
        price_var = tk.StringVar(value=f"현재가: {alarm.base_price:,.0f}원")
        tk.Label(row, textvariable=price_var,
                 bg=self.ENTRY, fg=self.GREEN,
                 font=("Malgun Gothic", 11, "bold")).pack(anchor="w")

        # 경계 상태
        bound_var = tk.StringVar()
        def _update_bound():
            p = alarm.last_price or alarm.base_price
            diff = p - alarm.initial_base_price
            diff_sign = "+" if diff >= 0 else ""
            bound_var.set(
                f"▼ {alarm.lower:,.0f}  ←  추적기준 {alarm.base_price:,.0f}"
                f"  →  ▲ {alarm.upper:,.0f}   gap {alarm.gap:,}"
                f"  │  초기기준 {alarm.initial_base_price:,.0f}"
                f" ({diff_sign}{diff:,.0f})")
        _update_bound()
        tk.Label(row, textvariable=bound_var,
                 bg=self.ENTRY, fg=self.MUTED,
                 font=("Malgun Gothic", 8)).pack(anchor="w")

        # 모바일 등록 상태
        mobile_var = tk.StringVar(value="")
        mobile_lbl = tk.Label(row, textvariable=mobile_var,
                               bg=self.ENTRY, font=("Malgun Gothic", 8))
        mobile_lbl.pack(anchor="w")
        # 초기값: CoALa 체크 여부에 따라
        if self.coala_var.get():
            mobile_var.set("📱 CoALa 등록 대기 중...")
            mobile_lbl.config(fg=self.MUTED)

        # 버튼
        btn_row = tk.Frame(row, bg=self.ENTRY)
        btn_row.pack(anchor="e", pady=(4, 0))

        pause_var = tk.StringVar(value="⏸ 일시정지")
        def toggle():
            alarm.running = not alarm.running
            pause_var.set("▶ 재개" if not alarm.running else "⏸ 일시정지")
        tk.Button(btn_row, textvariable=pause_var,
                  bg=self.PANEL, fg=self.TEXT,
                  relief="flat", font=("Malgun Gothic", 8),
                  cursor="hand2", padx=6,
                  command=toggle).pack(side=tk.LEFT, padx=(0, 4))

        def remove():
            self.poller.remove(aid)
            self._alarm_rows.pop(aid, None)
            row.destroy()
            self._log(f"알람 삭제 | {alarm.name}", "INFO")
            self._save_config()
            # CoALa 서버에서도 삭제
            if self.coala_var.get():
                threading.Thread(
                    target=self._unregister_from_coala,
                    args=(aid,),
                    daemon=True
                ).start()
        tk.Button(btn_row, text="🗑 삭제",
                  bg="#2d1b1b", fg=self.RED,
                  relief="flat", font=("Malgun Gothic", 8),
                  cursor="hand2", padx=6,
                  command=remove).pack(side=tk.LEFT)

        self._alarm_rows[aid] = {
            "alarm":        alarm,
            "price_var":    price_var,
            "bound_var":    bound_var,
            "update_bound": _update_bound,
            "mobile_var":   mobile_var,
            "mobile_lbl":   mobile_lbl,
        }

        # 실시간 UI 업데이트
        def _ui_loop():
            while aid in self._alarm_rows:
                rd = self._alarm_rows.get(aid)
                if rd:
                    p = rd["alarm"].last_price or rd["alarm"].base_price
                    rd["price_var"].set(f"현재가: {p:,.0f}원")
                    rd["update_bound"]()
                time.sleep(1)
        threading.Thread(target=_ui_loop, daemon=True).start()

    # ── CoALa 서버에서 알람 삭제
    def _unregister_from_coala(self, aid: str):
        device_id = self.device_id_var.get().strip()
        if not device_id:
            return
        try:
            token, ts = make_hmac_token(device_id)
            requests.post(
                f"{COALA_SERVER_URL}/pc_alert_unregister",
                json={
                    "device_id": device_id,
                    "token":     token,
                    "timestamp": ts,
                    "alarm_id":  aid,
                },
                timeout=5,
            )
        except Exception:
            pass

    # ── CoALa 앱으로 전송 (범용 PC 알람) ────────
    def _send_pc_alert(self, aid: str, tts_text: str):
        """암호화된 tts_text를 서버 경유 앱으로 전송 (aid: 알람 고유 ID)"""
        if not self.coala_var.get():
            return
        device_id = self.device_id_var.get().strip()
        password  = self.password_var.get().strip()
        if not device_id or not password:
            return
        if len(password) < 4 or not password.isdigit():
            self.after(0, lambda: self._log(
                "CoALa: 동기화 코드는 숫자 4자리 이상이어야 합니다", "ERR"))
            return
        if not HAS_CRYPTO:
            self.after(0, lambda: self._log(
                "CoALa: pycryptodome 필요 (pip install pycryptodome)", "ERR"))
            return
        try:
            payload = json.dumps({"tts_text": tts_text}, ensure_ascii=False)
            enc = aes_encrypt(payload, password)
            token, ts = make_hmac_token(device_id)
            resp = requests.post(
                f"{COALA_SERVER_URL}/pc_alert",
                json={
                    "device_id":         device_id,
                    "token":             token,
                    "timestamp":         ts,
                    "alarm_id":          aid,       # 앱 등록 여부 확인용
                    "encrypted_payload": enc,
                },
                timeout=5,
            )
            result = resp.json() if resp.status_code == 200 else {}
            if not result.get("ok"):
                err = result.get("error", "")
                if err == "alarm_not_registered":
                    self.after(0, lambda: self._log(
                        f"CoALa: '{aid}' 앱에 미등록 - 앱에서 알람을 먼저 등록하세요", "WARN"))
                else:
                    self.after(0, lambda e=err: self._log(f"CoALa 전송 실패: {e}", "ERR"))
        except Exception as e:
            self.after(0, lambda e=e: self._log(f"CoALa 전송 실패: {e}", "ERR"))

    # ── CoALa 앱 알람 목록 등록 (pc_alert_register)
    def _register_to_coala(self, aid: str, alarm: StockAlarm):
        if not self.coala_var.get():
            return
        device_id = self.device_id_var.get().strip()
        password  = self.password_var.get().strip()
        if not device_id or not password:
            return
        if not HAS_CRYPTO:
            return
        try:
            # 알람 메타 정보를 암호화해서 서버에 저장
            # 앱에서 목록 조회 시 복호화해서 표시
            alarm_meta = json.dumps({
                "name":               alarm.name,
                "code":               alarm.code,
                "gap":                alarm.gap,
                "base_price":         alarm.base_price,
                "initial_base_price": alarm.initial_base_price,
                "tts_template":       alarm.tts_template,
            }, ensure_ascii=False)
            enc_payload = aes_encrypt(alarm_meta, password)
            token, ts = make_hmac_token(device_id)

            resp = requests.post(
                f"{COALA_SERVER_URL}/pc_alert_register",
                json={
                    "device_id":         device_id,
                    "token":             token,
                    "timestamp":         ts,
                    "alarm_id":          aid,
                    "encrypted_payload": enc_payload,
                    "run_now":           False,  # 앱에서 직접 실행 버튼 눌러야 시작
                },
                timeout=5,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                def _ok(a=aid):
                    self.coala_status.config(text="✅ 서버 등록됨", fg=self.GREEN)
                    self._log(f"CoALa 등록 | {alarm.name}", "INFO")
                    rd = self._alarm_rows.get(a)
                    if rd:
                        rd["mobile_var"].set("📱 CoALa 등록됨")
                        rd["mobile_lbl"].config(fg=self.GREEN)
                self.after(0, _ok)
            else:
                msg = resp.json().get("error", f"HTTP {resp.status_code}")
                def _fail(m=msg, a=aid):
                    self.coala_status.config(text=f"❌ {m}", fg=self.RED)
                    rd = self._alarm_rows.get(a)
                    if rd:
                        rd["mobile_var"].set(f"📱 CoALa 등록 실패: {m}")
                        rd["mobile_lbl"].config(fg=self.RED)
                self.after(0, _fail)
        except Exception as e:
            self.after(0, lambda e=e: (
                self.coala_status.config(text="❌ 연결 실패", fg=self.RED),
                self._log(f"CoALa 등록 실패: {e}", "ERR"),
            ))

    # ── CoALa 알람 발생 시 서버 전송 ────────────
    def _send_alert_to_coala(self, aid: str, alarm: StockAlarm, price: float, direction: str):
        diff = alarm.diff_from_initial(price)
        diff_sign = "+" if diff >= 0 else ""
        dir_kor = "상승" if direction == "UP" else "하락"
        tts_text = (alarm.tts_template
            .replace("{name}",      alarm.name)
            .replace("{price}",     f"{price:,.0f}")
            .replace("{direction}", dir_kor)
            .replace("{gap}",       str(alarm.gap))
            .replace("{diff}",      f"{diff_sign}{diff:,.0f}")
            .replace("{rate}",      ""))
        threading.Thread(
            target=self._send_pc_alert,
            args=(aid, tts_text),
            daemon=True
        ).start()

    # ── 알람 콜백 ────────────────────────────
    def _on_alert(self, aid, alarm, price, direction):
        dir_kor = "상승" if direction == "UP" else "하락"
        diff = alarm.diff_from_initial(price)
        diff_sign = "+" if diff >= 0 else ""
        diff_str = f"{diff_sign}{diff:,.0f}"
        text = (alarm.tts_template
                .replace("{name}", alarm.name)
                .replace("{price}", f"{price:,.0f}")
                .replace("{direction}", dir_kor)
                .replace("{gap}", str(alarm.gap))
                .replace("{diff}", diff_str)
                .replace("{rate}", ""))
        if self.tts_local_var.get():
            tts.speak(text)
        self.after(0, lambda: self._log(
            f"🔔 {alarm.name}  {dir_kor}  {price:,.0f}원  "
            f"→ 새 기준 {alarm.base_price:,.0f}  "
            f"(▼{alarm.lower:,.0f} / ▲{alarm.upper:,.0f})",
            direction))
        # CoALa 앱으로 전송
        threading.Thread(
            target=self._send_alert_to_coala,
            args=(aid, alarm, price, direction),
            daemon=True
        ).start()

    # ── 설정 저장/불러오기 ──────────────────
    def _save_config(self):
        alarms = []
        for aid, rd in self._alarm_rows.items():
            a = rd["alarm"]
            alarms.append({
                "aid":                aid,
                "code":               a.code,
                "name":               a.name,
                "gap":                a.gap,
                "base_price":         a.base_price,
                "initial_base_price": a.initial_base_price,
                "tts_template":       a.tts_template,
                "running":            a.running,
            })
        # 창 크기/위치 저장 (최대화 상태 구분)
        try:
            if self.state() == "zoomed":
                win_geo = "zoomed"
            else:
                win_geo = self.geometry()
        except Exception:
            win_geo = "zoomed"

        # 기존 파일에서 disclaimer_agreed 보존
        _prev_agreed = False
        try:
            import json as _j
            _p = _config_path()
            if os.path.exists(_p):
                with open(_p, encoding="utf-8") as _f:
                    _prev_agreed = _j.load(_f).get("disclaimer_agreed", False)
        except Exception:
            pass

        cfg = {
            "device_id":         self.device_id_var.get(),
            "password":          self.password_var.get(),
            "tts_local":         self.tts_local_var.get(),
            "coala_notify":      self.coala_var.get(),
            "tts_template":      self.tts_var.get(),
            "window_geometry":   win_geo,
            "lookup_history":    self._lookup_history[:20],
            "alarms":            alarms,
            "disclaimer_agreed": _prev_agreed,
        }
        try:
            with open(_config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[CONFIG] 저장 실패: {e}")

    def _load_config(self):
        path = _config_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[CONFIG] 불러오기 실패: {e}")
            return

        self.device_id_var.set(cfg.get("device_id", ""))
        self.password_var.set(cfg.get("password", ""))
        self.tts_local_var.set(cfg.get("tts_local", True))
        self.coala_var.set(cfg.get("coala_notify", False))
        self.tts_var.set(cfg.get("tts_template",
                                  "{name} {direction} {price}원 기준가 대비 {diff}원"))

        # 창 크기 복원
        geo = cfg.get("window_geometry", "zoomed")
        if geo == "zoomed":
            self.after(0, lambda: self.state("zoomed"))
        else:
            try:
                self.geometry(geo)
            except Exception:
                self.after(0, lambda: self.state("zoomed"))

        # 조회 히스토리 복원
        self._lookup_history = cfg.get("lookup_history", [])
        self.after(100, self._refresh_history_ui)

        for a in cfg.get("alarms", []):
            try:
                alarm = StockAlarm(
                    code=               a["code"],
                    name=               a["name"],
                    gap=                a["gap"],
                    base_price=         a["base_price"],
                    initial_base_price= a.get("initial_base_price", a["base_price"]),
                    tts_template=       a.get("tts_template",
                                              "{name} {direction} {price}원 기준가 대비 {diff}원"),
                    running=            a.get("running", True),
                )
                aid = a["aid"]
                # aid가 없거나 빈 경우 uuid로 새 id 생성
                if not aid:
                    aid = f"alarm_{uuid.uuid4().hex[:12]}"

                self.poller.add(aid, alarm)
                self._render_alarm_row(aid, alarm)
            except Exception as e:
                print(f"[CONFIG] 알람 복원 실패: {e}")

        self._log(f"설정 불러옴 ({len(cfg.get('alarms', []))}개 알람)", "INFO")

    def _on_close(self):
        self._save_config()
        self.destroy()

    def _on_resize(self, event):
        # 최대화 상태가 아닐 때만 크기 저장 (디바운스)
        if self.state() == "zoomed":
            return
        if hasattr(self, "_resize_job"):
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(500, self._save_config)

    # ── 로그 ────────────────────────────────
    def _log(self, msg: str, tag: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert(tk.END, f"[{ts}] {msg}\n", tag)
        self.log.see(tk.END)
        self.log.configure(state="disabled")


# ─────────────────────────────────────────
if __name__ == "__main__":
    # 면책조항 동의 확인
    if not _show_disclaimer():
        import sys
        sys.exit(0)
    App().mainloop()
