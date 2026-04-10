# =========================================================
# SIGNAL / LIFECYCLE
# =========================================================
MIN_SIGNAL_GAP_MINUTES = 15          # (25 → 15) daha akıcı sinyal
SIGNAL_TIMEOUT_MINUTES = 90
MAX_ACTIVE_SIGNAL_AGE_MINUTES = 180

MIN_PRICE_DISTANCE_PCT = 0.30        # (0.45 → 0.30) aynı bölgeyi kaçırma
REVERSE_SIGNAL_STRENGTH_BONUS = 1.40 # (1.60 → 1.40) aşırı agresif ters kesme azaltıldı
SAME_DIRECTION_SCORE_BONUS = 2.00    # (2.25 → 2.00) daha dengeli

# =========================================================
# THRESHOLDS
# =========================================================
LONG_SCORE_THRESHOLD = 8.8           # (9.7 → 8.8) daha fazla ama kaliteli sinyal
SHORT_SCORE_THRESHOLD = 8.8

NEUTRAL_USDTD_EXTRA_SCORE = 0.4      # (1.0 → 0.4) nötrde sinyal boğma azaltıldı
MIN_RR_TO_TP2 = 1.30                 # (1.35 → 1.30) daha fazla fırsat

# =========================================================
# RISK
# =========================================================
ATR_SL_MULTIPLIER = 1.20
ATR_TP1_MULTIPLIER = 1.20
ATR_TP2_MULTIPLIER = 2.20
ATR_TP3_MULTIPLIER = 3.20
TRAIL_AFTER_TP2_ATR = 1.00

# =========================================================
# FILTERS
# =========================================================
USE_BTC_FILTER = True
BTC_15M_TREND_THRESHOLD = 0.10       # (0.12 → 0.10) BTC filtre biraz esnedi

USE_USDTD_FILTER = True
USDTD_FAST_EMA = 6
USDTD_SLOW_EMA = 18
USDTD_HISTORY_LIMIT = 240
CG_CACHE_TTL_SECONDS = 180

STRICT_USDTD_BLOCK = False           # 🔥 EN ÖNEMLİ AYAR → artık bloklamıyor

MAX_HTTP_RETRY = 3
HTTP_TIMEOUT = 15

# =========================================================
# ENTRY / MESSAGE
# =========================================================
ENTRY_NEAR_PCT = 0.20               # (0.18 → 0.20) entry yakalama kolaylaştı
CANCEL_IF_TP1_BEFORE_ENTRY = True

# =========================================================
# SMART RESEND
# =========================================================
RESEND_IF_SCORE_IMPROVED_BY = 0.8   # (1.0 → 0.8) daha erken update
RESEND_IF_ENTRY_MOVED_PCT = 0.20    # (0.25 → 0.20)
RESEND_IF_REASON_CHANGED = True
ALLOW_SMART_REPEAT_SIGNAL = True

# =========================================================
# AUTO REPORTS
# =========================================================
AUTO_REPORTS = True
