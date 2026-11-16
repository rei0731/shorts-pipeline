# settings.example.py -- 公開用の設定サンプル。
# 実運用では config/settings.py としてコピーし、秘密情報は config/.env（git管理外）か環境変数に置く。
# ID・トークン・個人パスはすべて環境変数から読む。値そのものはこのリポジトリに含まれない。
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

# .env ロード（存在する場合のみ）
_env_path = BASE_DIR / "config" / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        # .env を優先（システム環境変数に古い枯渇キーが残っていても .env の新キーで上書きする）
        load_dotenv(_env_path, override=True)
    except ImportError:
        pass

# Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# gemini-flash-latest は gemini-3.5-flash に解決され無料枠が1日20回と極端に少ない。
# 無料枠が潤沢な gemini-2.5-flash を明示指定（per-modelの日次枠なので別枠）。
GEMINI_MODEL = "gemini-2.5-flash"
# 日次枠(PerDay)が切れた時に自動で順に切り替えるフォールバック。各モデルは別枠なので
# 1つ枯渇しても次で生成を続行できる（script_generator._gen_with_retry が自動切替）。
GEMINI_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]

# 読み上げエンジン: "gtts" または "voicevox"
TTS_ENGINE = "voicevox"

# gTTS
GTTS_LANG = "ja"
GTTS_TLD = "co.jp"   # 日本語ドメイン（自然さ向上）
GTTS_SLOW = False

# VOICEVOX
VOICEVOX_URL = "http://127.0.0.1:50021"
# 自動起動用の実行ファイル候補（上から順に存在チェック）。engine(run.exe)優先=ヘッドレスで軽い
import os as _os
VOICEVOX_EXE_CANDIDATES = [
    _os.path.expandvars(r"%LOCALAPPDATA%\Programs\VOICEVOX\vv-engine\run.exe"),
    _os.path.expandvars(r"%LOCALAPPDATA%\Programs\VOICEVOX\VOICEVOX.exe"),
    r"C:\Program Files\VOICEVOX\VOICEVOX.exe",
]
VOICEVOX_BOOT_TIMEOUT = 120  # 起動後にAPI応答を待つ最大秒数
VOICEVOX_SPEAKER_ID = 13  # 青山龍星
VOICEVOX_SPEED = 1.00       # 話速（1.0が標準。都市伝説は落ち着いた語りが合う）
VOICEVOX_INTONATION = 1.15  # 抑揚（>1で起伏を強調し棒読み感を軽減）
VOICEVOX_PITCH = 0.0        # 音高
VOICEVOX_PAUSE_SCALE = 1.10 # 句読点の間（読点は_thin_commasで間引くため間の長さは標準に戻す）

# VOICEVOX 文脈依存の読み修正（正規表現）。_KANA_FIXES の単純置換では誤爆する語に使う。
# 例: 「はえ」は蝿(ハエ)だが、生え/映え/栄え等は除外する必要がある。
VOICEVOX_REGEX_FIXES = [
    # 前が動詞語幹(生/映/栄)でなく、後ろが助詞/句読点/文末なら蝿としてカナ化
    (r"(?<![生映栄])はえ(?=[、。！？\sがをにのはと])", "ハエ"),
    (r"(?<![生映栄])はえ$", "ハエ"),
    # 「えんすい→しおみず」(塩水) は 2026-09-25 に削除（運用者指示）。
    # 「えんすいはほぼ塩水のみ」は誤りで、円錐（円すい形）を「しおみず」と読ませていた。
    # 全4リポの台本を走査して、正しく効いた例は0件・誤爆1件（火山の円錐形）。
    # 塩水の誤読は reading_dict.json の words（字幕に「塩水」があるときだけ効く）で検出できる。
    # 形容詞+はなし(話)をVOICEVOXが「〜い + は(助詞) + なし」と誤解析して
    # 「コワイワナシ」と発音する対策(2026-07-04実測)。漢字にすると正しく解析される。
    # 離し/放し も同音(はなし)なので誤爆しても発音は変わらない。
    (r"(?<=い)はなし", "話"),
]

# 動画スペック
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

# Ken Burns 風の動き（カードごとに5種を循環: 0=ズームイン 1=右 2=左 3=上 4=下パン）
KEN_BURNS_ZOOM_FROM = 1.00
KEN_BURNS_ZOOM_TO = 1.010   # ゆるやかなズーム/パン量（2026-10-01 運用者「動かすときはもう少しゆっくり」1.015→1.010＝速さ2/3）
TARGET_SCENE_COUNT = 9   # 60秒未満に収めるため12→9に削減（動画分析の改善提案2026-06-23）
# BGMは0.07→0.045に変更（2026-08-27 運用者承認・比較して決めた値）。
# ショート・ロングとも同じ。睡眠用の音量(SLEEP_VOLUME_DB等)とは別。
BGM_VOLUME = 0.045

# YouTube
YOUTUBE_PRIVACY_STATUS = "private"  # public / unlisted / private
YOUTUBE_PLAYLIST_NAME = os.environ.get("YOUTUBE_PLAYLIST_NAME", "Shorts")
YOUTUBE_CATEGORY_ID = "27"  # Education

# パス
FONT_PATH = str(BASE_DIR / "assets" / "fonts" / "NotoSansJP-Bold.ttf")
BGM_DIR = str(BASE_DIR / "assets" / "bgm")
# 特定BGMを固定で使う場合はファイル名（BGM_DIR内）または絶対パスを指定。
# None または空文字なら BGM_DIR からランダム選択。
BGM_FILE = os.environ.get("BGM_FILE", "")

# 冒頭フックSE: シーン1の0秒位置にミックスして掴みを強める（都市伝説の不気味な一撃）。
# 空文字なら無効。音量はHOOK_SE_VOLUME。
HOOK_SE_FILE = os.environ.get("HOOK_SE_FILE", "")
HOOK_SE_VOLUME = 0.2

# --- アイキャッチ（深掘りロング用・2026-08-27 運用者要望）---
# 章の変わり目に、画像＋ピアノの単音だけの短いカットを挟む。
# ナレーションもテロップも無い「間」を作って、話の切り替わりを伝えるのが狙い。
EYECATCH_ENABLED = True                       # ロング(横1920)のみ有効
EYECATCH_SE_FILE = os.environ.get("EYECATCH_SE_FILE", "")
EYECATCH_SE_VOLUME = 0.28                     # フックSEより少し前に出す（単独で鳴るため）
EYECATCH_SEC = 2.2                            # 1カットの長さ（秒）
EYECATCH_SE_DELAY = 0.35                      # SEを鳴らすまでの待ち（フェードイン後に鳴らす）
EYECATCH_COUNT = 3                            # 本編を4つに割る＝3回入れる
EYECATCH_MIN_SCENES = 16                      # これ未満のシーン数なら入れない
OUTPUT_DIR = str(BASE_DIR / "assets" / "output")
LOGS_DIR = str(BASE_DIR / "logs")
HISTORY_FILE = str(BASE_DIR / "history.json")
CLIENT_SECRET_FILE = str(BASE_DIR / "config" / "client_secret.json")
TOKEN_FILE = str(BASE_DIR / "config" / "token.json")

# チャンネル種別（"tosidensetu" or "zatsugaku"）
CHANNEL_TYPE = os.environ.get("CHANNEL_TYPE", "tosidensetu")

# 都市伝説ジャンル
_TOPIC_GENRES_TOSIDENSETU = [
    "日本の都市伝説",
    "世界の都市伝説",
    "学校の怪談",
    "心霊スポット",
    "未解決事件・失踪",
    "陰謀論",
    "UMA・未確認生物",
    "UFO・宇宙人",
    "呪いの物品",
    "怪異・怪現象",
    "禁断の実験",
    "秘密結社",
    "超古代文明",
    "予言・預言",
    "怪談・奇譚",
    "呪術・魔術",
]

# 雑学ジャンル
_TOPIC_GENRES_ZATSUGAKU = [
    "動物の不思議な生態",
    "科学・物理の豆知識",
    "人体の意外な事実",
    "歴史の面白い裏話",
    "食べ物・料理の雑学",
    "宇宙・地球の不思議",
    "言葉・漢字の豆知識",
    "生き物の驚きの能力",
    "世界の文化・風習の雑学",
    "数学・統計の面白い話",
    "発明・発見の意外な歴史",
    "植物の不思議な仕組み",
    "心理学の豆知識",
    "スポーツの意外なルール",
    "乗り物・技術の豆知識",
]

TOPIC_GENRES = _TOPIC_GENRES_ZATSUGAKU if CHANNEL_TYPE == "zatsugaku" else _TOPIC_GENRES_TOSIDENSETU

# 字幕スタイル
SUBTITLE_FONT_SIZE = 75
SUBTITLE_COLOR = "white"
SUBTITLE_STROKE_COLOR = "black"
SUBTITLE_STROKE_WIDTH = 3

# Pollinations画像
IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1920
IMAGE_STYLE_SUFFIX = (
    ", bright colorful educational illustration, clean background, vivid, high quality"
    if CHANNEL_TYPE == "zatsugaku"
    else ", 4k, dramatic lighting, horror atmosphere, photorealistic"
)

# 重複判定
JACCARD_THRESHOLD = 0.4

# === ロング動画モード（横型・約10分・都市伝説まとめ） ===
# Notion セクション10 準拠。--long で有効化（main._apply_long_mode が実行時に上書き）
LONG_VIDEO_WIDTH = 1920
LONG_VIDEO_HEIGHT = 1080
LONG_IMAGE_WIDTH = 1920
LONG_IMAGE_HEIGHT = 1080
LONG_THUMB_WIDTH = 1280
LONG_THUMB_HEIGHT = 720
LONG_ITEM_COUNT = 18                # イントロ+18項目+アウトロ（10分狙い）
LONG_TEST_ITEM_COUNT = 2            # テスト用: イントロ+2項目+アウトロ（約2分）
LONG_SUBTITLE_BOTTOM_MARGIN = 90    # 横型は下マージン小さめ
LONG_SUBTITLE_FONT_SIZE = 60
LONG_PLAYLIST_NAME = os.environ.get("LONG_PLAYLIST_NAME", "Long videos")
# 睡眠用ロングは通常ロングと分ける（2026-08-28 運用者指示）。
# タイトルに「睡眠用」か「聞き流し」を含むものは youtube_uploader が自動でこちらへ入れる
# （2026-09-03 検索型タイトルへ移行。先頭一致では拾えなくなったため含有判定に変更）。
# 名前は既存プレイリスト（作成済み・3本入り）に合わせてある。変えると新規作成される。
SLEEP_PLAYLIST_NAME = os.environ.get("SLEEP_PLAYLIST_NAME", "Sleep long videos")
LONG_IMAGE_STYLE_SUFFIX = ", cinematic wide shot, 16:9, 4k, dramatic lighting, horror atmosphere, photorealistic"

# ショート/ロングで使う共通モジュール変数の初期値（video_builder/image_generatorが参照）
SUBTITLE_BOTTOM_MARGIN = 320

# === メール通知（ギガファイル便リンクを自分に送る） ===
# config/.env に GMAIL_ADDRESS と GMAIL_APP_PASSWORD を保存する
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or GMAIL_ADDRESS  # 未指定なら自分宛
NOTIFY_ENABLED = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)

# =====================================================================
# Notion 分析レポート設定
# =====================================================================
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

# =====================================================================
# 分析対象チャンネル一覧
#   channel_id: 空文字なら認証アカウント本人(mine=True)。
#               別チャンネルは公開データのみ分析可能（チャンネルIDを指定）。
#   notion_database_id: 各チャンネルの出力先Notion DB。
#   token_file: 別Googleアカウントの場合のみ別トークンを指定（省略可）。
# notion_database_id が空のチャンネルは分析スキップされます。
# =====================================================================
CHANNELS = [
    {
        "name": "channel_a",
        "full_name": "Channel A",
        "genre": "都市伝説・ミステリー・オカルト",
        "tone": "恐怖と知的好奇心を煽るミステリアスな語り口",
        "channel_id": os.environ.get("CHANNEL_A_ID", ""),
        "notion_database_id": os.environ.get("NOTION_DATABASE_ID", ""),
        "token_file": "",
    },
    {
        "name": "channel_b",
        "full_name": "Channel B",
        "genre": "雑学・トリビア・生物/歴史/科学の豆知識",
        "tone": "明るく楽しい教養エンタメ",
        "channel_id": os.environ.get("CHANNEL_B_ID", ""),
        "notion_database_id": os.environ.get("CHANNEL_B_NOTION_DATABASE_ID", ""),
        "token_file": os.environ.get("CHANNEL_B_TOKEN_FILE", ""),
        # Analytics API(年齢・性別)はチャンネル所有トークンが必須
        "analytics_token_file": os.environ.get("CHANNEL_B_ANALYTICS_TOKEN", ""),
    },
    {
        "name": "channel_c",
        "full_name": "Channel C",
        "genre": "心理学・行動科学",
        "tone": "落ち着いた解説。実験や研究で具体的に示し、日常に当てはめる",
        "channel_id": os.environ.get("CHANNEL_C_ID", ""),
        "notion_database_id": "",
        "token_file": os.environ.get("CHANNEL_C_TOKEN_FILE", ""),
        "analytics_token_file": os.environ.get("CHANNEL_C_ANALYTICS_TOKEN", ""),
    },
    {
        "name": "channel_d",
        "full_name": "Channel D",
        "genre": "脳トレ・シニア向けクイズ",
        "tone": "やさしくゆっくり。答えを考える間をとる",
        "channel_id": os.environ.get("CHANNEL_D_ID", ""),
        "notion_database_id": "",
        "token_file": os.environ.get("CHANNEL_D_TOKEN_FILE", ""),
        "analytics_token_file": os.environ.get("CHANNEL_D_ANALYTICS_TOKEN", ""),
    },
    {
        "name": "channel_e",
        "full_name": "Channel E",
        "genre": "脳トレ・色さがし",
        "tone": "落ち着いた出題。問題が進むほど難しくなる",
        "channel_id": os.environ.get("CHANNEL_E_ID", ""),
        "notion_database_id": "",
        "token_file": os.environ.get("CHANNEL_E_TOKEN_FILE", ""),
        "analytics_token_file": os.environ.get("CHANNEL_E_ANALYTICS_TOKEN", ""),
    },
]

# チャンネル診断レポートDB（全チャンネル共通・履歴蓄積型）
DIAGNOSTIC_NOTION_DATABASE_ID = os.environ.get(
    "DIAGNOSTIC_NOTION_DATABASE_ID", ""
)

# 自動生成された BANNED キーワードの保存先（channel_diagnose.py が書き、script_generator.py が読む）
AUTO_BANNED_DIR = str(BASE_DIR / "diagnostics")


# ----- 題材ブロックリスト（都市伝説チャンネル用）-----
# 手動リスト: タイトル/本文の両方をチェック（明確に封印したい題材のみ入れる）。
# 実在の政治・戦争・感染症・宗教・皇室など、都市伝説/オカルトの世界観から外れ
# 炎上・不適切リスクのある「現実のセンシティブ題材」を生成前にブロックする。
# (過去に低品質モデルが現実のセンシティブ題材を出した事故を受け追加)
# ※古代兵器・呪術等のオカルト文脈は別語なので誤爆しない。過剰なら適宜削る。
BANNED_THEME_KEYWORDS: list[str] = [
    "ウクライナ", "ロシア", "プーチン", "侵攻", "台湾有事", "北朝鮮",
    "パレスチナ", "ガザ", "イスラエル", "ハマス",
    "生物兵器", "化学兵器", "新型コロナ", "コロナ", "ワクチン", "パンデミック",
    "皇室", "天皇", "統一教会", "自民党", "選挙",
]

# 自動リスト（channel_diagnose.py 由来の低パフォーマー頻出語）: タイトルのみチェック。
# 本文まで見ると一般語の誤爆で正常生成まで止まるため、適用範囲を分けている。
def _load_auto_banned_toshidensetu() -> list[str]:
    import json as _json
    _f = Path(AUTO_BANNED_DIR) / "都市伝説_banned_auto.json"
    if not _f.exists():
        return []
    try:
        with open(_f, encoding="utf-8") as _fh:
            return _json.load(_fh).get("banned_keywords", []) or []
    except Exception:
        return []

AUTO_BANNED_TITLE_KEYWORDS: list[str] = [
    kw for kw in _load_auto_banned_toshidensetu() if kw and kw not in BANNED_THEME_KEYWORDS
]

# Notion DB のプロパティ名マッピング（既存DBのカラム名に合わせて変更してください）
NOTION_PROP_TITLE = os.environ.get("NOTION_PROP_TITLE", "名前")
NOTION_PROP_VIDEO_URL = os.environ.get("NOTION_PROP_VIDEO_URL", "URL")
NOTION_PROP_PUBLISHED_AT = os.environ.get("NOTION_PROP_PUBLISHED_AT", "投稿日")
NOTION_PROP_VIEWS = os.environ.get("NOTION_PROP_VIEWS", "再生回数")
NOTION_PROP_LIKES = os.environ.get("NOTION_PROP_LIKES", "高評価数")
NOTION_PROP_COMMENTS = os.environ.get("NOTION_PROP_COMMENTS", "コメント数")
NOTION_PROP_LIKE_RATE = os.environ.get("NOTION_PROP_LIKE_RATE", "高評価率")
NOTION_PROP_RATING = os.environ.get("NOTION_PROP_RATING", "評価")
NOTION_PROP_COMMENT_ANALYSIS = os.environ.get("NOTION_PROP_COMMENT_ANALYSIS", "コメント傾向")
NOTION_PROP_SUGGESTIONS = os.environ.get("NOTION_PROP_SUGGESTIONS", "改善提案")
NOTION_PROP_ANALYZED_AT = os.environ.get("NOTION_PROP_ANALYZED_AT", "分析日")
