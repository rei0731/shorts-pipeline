"""台本生成: Wikipedia検索 → Gemini台本生成 → ファクトチェック → 校閲"""
import json
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import google.generativeai as genai

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    GEMINI_API_KEY, GEMINI_MODEL, HISTORY_FILE,
    JACCARD_THRESHOLD, TARGET_SCENE_COUNT, TOPIC_GENRES,
    LONG_ITEM_COUNT, CHANNEL_TYPE, OUTPUT_DIR,
    GEMINI_MODEL_FALLBACKS,
    BANNED_THEME_KEYWORDS, AUTO_BANNED_TITLE_KEYWORDS,
)

# Wikipedia API は User-Agent 無しのリクエストを 403 で弾く。明示UAを付けるのが403の根本対策。
_HTTP_HEADERS = {"User-Agent": "ShortsPipelineBot/1.0 (+contact@example.com)"}


def _urlopen(url, timeout=10):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=_HTTP_HEADERS), timeout=timeout
    )


@dataclass
class Scene:
    index: int
    narration: str        # 漢字混じり（字幕用）
    narration_kana: str   # ひらがな（VOICEVOX用）
    image_prompt: str     # Pollinations用英語プロンプト
    chapter: str = ""     # 章の開始シーンだけ見出しが入る（アイキャッチの位置）
    image_file: str = ""  # 実物の写真（ローカルの jpg）。あれば image_prompt より優先（2026-09-29 src/image_sources.py）
    image_credit: str = ""  # その写真の出典1行（説明欄用）
    image_extra: list = field(default_factory=list)  # 2枚目以降のテロップ用の実物写真 [{"file","credit"}]（2026-10-07）


@dataclass
class Script:
    title: str
    description: str
    tags: list[str]
    genre: str
    scenes: list[Scene] = field(default_factory=list)
    image_credits: list[str] = field(default_factory=list)  # 説明欄「画像出典」（2026-09-29）


# 現在アクティブなモデルとフォールバックの状態（_gen_with_retry が日次枠切れ時に自動切替）
_MODEL_CHAIN: list[str] = []
_model_pos: int = 0
_active_model: "genai.GenerativeModel | None" = None


def _setup_gemini() -> genai.GenerativeModel:
    """1本目のモデルを構築。GEMINI_MODEL を先頭に、フォールバックを重複なく連結する。"""
    global _MODEL_CHAIN, _model_pos, _active_model
    genai.configure(api_key=GEMINI_API_KEY)
    chain = [GEMINI_MODEL] + [m for m in GEMINI_MODEL_FALLBACKS if m != GEMINI_MODEL]
    _MODEL_CHAIN = chain
    _model_pos = 0
    _active_model = genai.GenerativeModel(chain[0])
    return _active_model


def _switch_to_next_model() -> bool:
    """次のフォールバックモデルへ切替。これ以上なければ False。"""
    global _model_pos, _active_model
    if _model_pos + 1 >= len(_MODEL_CHAIN):
        return False
    _model_pos += 1
    name = _MODEL_CHAIN[_model_pos]
    _active_model = genai.GenerativeModel(name)
    print(f"    日次枠切れ → モデル自動切替: {name}")
    return True


def _gen_with_retry(prompt: str, max_attempts: int = 6) -> str:
    """generate_content を 429 対応で実行。
    - 分あたり制限(retry in Xs): APIの指示秒数を尊重して待って再試行。
    - 日次枠切れ(PerDay): 待っても回復しないので即フォールバックモデルへ自動切替。
    全モデルの日次枠が尽きたら例外を送出する。"""
    import time as _time
    delay = 8.0
    attempt = 0
    while True:
        attempt += 1
        try:
            return _active_model.generate_content(prompt).text
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            is_429 = "429" in msg or "quota" in msg.lower() or "ResourceExhausted" in msg
            if not is_429:
                raise
            is_daily = "PerDay" in msg or "GenerateRequestsPerDay" in msg
            if is_daily:
                # 日次枠は待っても無駄 → 次モデルへ。尽きたら諦める
                if _switch_to_next_model():
                    attempt = 0
                    delay = 8.0
                    continue
                raise
            # 分あたり制限: 指示秒数だけ待つ。上限に達したら最後にモデル切替を試みる
            if attempt >= max_attempts:
                if _switch_to_next_model():
                    attempt = 0
                    delay = 8.0
                    continue
                raise
            m = re.search(r"retry in ([0-9.]+)s", msg)
            wait = float(m.group(1)) + 1.0 if m else delay
            print(f"    レート制限(429) 試行{attempt} → {wait:.0f}秒待機")
            _time.sleep(wait)
            delay = min(delay * 1.7, 60.0)


def _wikipedia_search(query: str, n: int = 3) -> str:
    """複数ソースから情報収集:
    1. Wikipedia日本語版 上位n件
    2. Wikipedia英語版 上位1件（日本語版が薄い場合の補強）
    3. Wikipedia日本語版 ランダム関連記事 1件（意外性のあるネタ発掘）
    """
    results = []

    # --- 1. 日本語版 ---
    encoded = urllib.parse.quote(query)
    ja_search = (
        f"https://ja.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={encoded}&srlimit={n}&format=json"
    )
    try:
        with _urlopen(ja_search, timeout=10) as r:
            data = json.loads(r.read())
        for item in data["query"]["search"]:
            page_id = item["pageid"]
            content_url = (
                f"https://ja.wikipedia.org/w/api.php?action=query&prop=extracts"
                f"&exintro&explaintext&pageids={page_id}&format=json"
            )
            with _urlopen(content_url, timeout=10) as r:
                page = json.loads(r.read())
            extract = list(page["query"]["pages"].values())[0].get("extract", "")
            if extract:
                results.append(f"【{item['title']}（日本語版）】\n{extract[:1500]}")
    except Exception as e:
        print(f"  Wikipedia日本語版取得エラー: {e}")

    # --- 2. 英語版（補強） ---
    try:
        en_search = (
            f"https://en.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={encoded}&srlimit=1&format=json"
        )
        with _urlopen(en_search, timeout=10) as r:
            data = json.loads(r.read())
        items = data["query"]["search"]
        if items:
            page_id = items[0]["pageid"]
            content_url = (
                f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
                f"&exintro&explaintext&pageids={page_id}&format=json"
            )
            with _urlopen(content_url, timeout=10) as r:
                page = json.loads(r.read())
            extract = list(page["query"]["pages"].values())[0].get("extract", "")
            if extract:
                results.append(f"【{items[0]['title']}（英語版・Geminiが日本語で活用）】\n{extract[:1000]}")
    except Exception as e:
        print(f"  Wikipedia英語版取得エラー: {e}")

    # --- 3. ランダム関連記事（意外性ネタ発掘） ---
    try:
        rand_url = (
            f"https://ja.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={encoded}&srlimit=10&sroffset=3&format=json"
        )
        with _urlopen(rand_url, timeout=10) as r:
            data = json.loads(r.read())
        items = data["query"]["search"]
        if items:
            pick = random.choice(items)
            page_id = pick["pageid"]
            content_url = (
                f"https://ja.wikipedia.org/w/api.php?action=query&prop=extracts"
                f"&exintro&explaintext&pageids={page_id}&format=json"
            )
            with _urlopen(content_url, timeout=10) as r:
                page = json.loads(r.read())
            extract = list(page["query"]["pages"].values())[0].get("extract", "")
            if extract:
                results.append(f"【{pick['title']}（派生ネタ候補）】\n{extract[:800]}")
    except Exception as e:
        print(f"  Wikipediaランダム記事取得エラー: {e}")

    return "\n\n".join(results) if results else "（情報取得失敗）"


def _load_history() -> list[str]:
    p = Path(HISTORY_FILE)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_history(title: str) -> None:
    history = _load_history()
    history.append(title)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_duplicate(title: str, history: list[str]) -> bool:
    return any(_jaccard(title, h) >= JACCARD_THRESHOLD for h in history)


# 題材を表す固有語の抽出に使う。煽り定型語/汎用語は題材ではないので除外する。
_THEME_STOPWORDS = {
    "都市", "伝説", "怪談", "真実", "秘密", "正体", "恐怖", "禁断", "禁忌",
    "閲覧", "注意", "国家", "歴史", "衝撃", "本当", "理由", "最恐", "最凶",
    "存在", "事件", "事実", "謎", "闇", "噂", "呪い", "呪術", "起源", "全貌",
    "世界", "日本", "現代", "発見", "封印", "消えた", "隠蔽", "完全", "実験",
}


def _extract_theme_keywords(text: str) -> set[str]:
    """タイトル/本文から題材を表す固有語（2〜8字の漢字塊・3〜10字のカタカナ塊）を抽出。
    煽り定型語は _THEME_STOPWORDS で除外する。本文ベースの被り判定に使う。"""
    kws: set[str] = set()
    for m in re.findall(r"[一-龥]{2,8}|[ァ-ヴー]{3,10}", text or ""):
        if m not in _THEME_STOPWORDS and len(m) >= 2:
            kws.add(m)
    return kws


def _load_stock_themes() -> list[tuple[str, set[str]]]:
    """生成済み在庫（assets/output/*/script.json・pending.json）のタイトル＋本文から
    題材キーワード集合を作る。--no-upload 生成は history.json に載らないため、
    在庫を直接走査して同一バッチ・既存在庫との被りを検出する。"""
    stock: list[tuple[str, set[str]]] = []
    out = Path(OUTPUT_DIR)
    if not out.exists():
        return stock
    for d in out.glob("*"):
        meta = d / "script.json"
        if not meta.exists():
            meta = d / "pending.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        scenes = data.get("scenes", [])
        # ロング(まとめ18選)は1本に多数の題材を含みキーワードの塊になる。
        # どんなショートとも一致して誤検出を量産するため、被り判定の対象から除外する。
        if len(scenes) > 12:
            continue
        title = data.get("title", "")
        body = title + "".join(s.get("narration", "") for s in scenes)
        kws = _extract_theme_keywords(body)
        if kws:
            stock.append((title, kws))
    return stock


def _is_theme_duplicate(title: str, scenes: list[dict],
                        stock: list[tuple[str, set[str]]]) -> str | None:
    """新台本の題材が既存在庫と被るか判定。被るなら被り相手のタイトルを返す。
    タイトルJaccardでは捕まらない『煽りだけ違う同一ネタ』(ディバックの箱×3 事故)を本文語で検出する。"""
    body = title + "".join(s.get("narration", "") for s in scenes)
    new_kws = _extract_theme_keywords(body)
    if len(new_kws) < 3:
        return None
    for other_title, kws in stock:
        if len(kws) < 3:
            continue
        shared = new_kws & kws
        # 対称Jaccard（和集合が分母）で判定。小さい側を分母にする overlap は汎用語2語で誤爆するため不可。
        # 別ネタ同士のJaccardは低く（<0.2）、同一ネタ（煽りだけ違う）は高い（>0.35）。
        jac = len(shared) / max(1, len(new_kws | kws))
        if len(shared) >= 5 or (jac >= 0.35 and len(shared) >= 3):
            return other_title
    return None


def _format_history_block(history: list[str], limit: int = 200) -> str:
    """過去タイトルを Gemini に渡す形式に整形。直近limit件＋頻出題材を禁止リスト化してネタ被りを防ぐ。"""
    if not history:
        return "（なし）"
    import re as _re
    from collections import Counter
    _ban_words = {"都市", "伝説", "怪談", "真実", "秘密", "謎", "恐怖", "正体",
                  "まさか", "実は", "衝撃", "禁断", "闇", "最恐", "理由"}
    counts: Counter = Counter()
    for title in history:
        for m in _re.findall(r"[一-龥]{2,4}|[ァ-ヴー]{3,8}", title):
            if m not in _ban_words and len(m) >= 2:
                counts[m] += 1
    frequent = sorted([(w, c) for w, c in counts.items() if c >= 4], key=lambda x: -x[1])
    forbidden = "、".join(f"{w}({c}回)" for w, c in frequent[:30]) or "（なし）"
    recent = history[-limit:]
    lines = "\n".join(f"- {t}" for t in recent)
    return (f"## 絶対に避けるべき頻出題材（過去に多数作成済み・題材にするの禁止）\n{forbidden}\n\n"
            f"## 過去動画タイトル（直近{len(recent)}件・被り禁止）\n{lines}")


def _load_recommended_keywords() -> list[str]:
    """channel_diagnose.py の診断結果から高パフォーマー頻出語を読む（プロンプト誘導用）"""
    from config.settings import AUTO_BANNED_DIR
    name = "雑学" if CHANNEL_TYPE == "zatsugaku" else "都市伝説"
    f = Path(AUTO_BANNED_DIR) / f"{name}_banned_auto.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("recommended_keywords", []) or []
    except Exception:
        return []


def _build_steering_block(stock_themes: list[tuple[str, set[str]]],
                          rejected: list[str]) -> str:
    """リトライガチャを減らすための誘導ブロック。
    - 未投稿在庫のタイトル（history.json に載らない分）を事前に「被り禁止」提示
    - BANNED語（手動＋診断由来）を事前提示（生成後に弾いて捨てるより先に避けさせる）
    - 診断のRECOMMENDED語で伸びる題材へ誘導
    - 直前に却下された案を明示（同じ題材の再提出を防ぐ）
    """
    parts = []
    if stock_themes:
        titles = [t for t, _ in stock_themes if t][-100:]
        if titles:
            parts.append("## 未投稿在庫のタイトル（被り禁止・煽り違いの同一ネタも禁止）\n"
                         + "\n".join(f"- {t}" for t in titles))
    banned_all = list(dict.fromkeys((BANNED_THEME_KEYWORDS or []) + (AUTO_BANNED_TITLE_KEYWORDS or [])))
    if banned_all:
        parts.append("## 使用禁止ワード（タイトルに含めたら失格・再生成になる）\n"
                     + "、".join(banned_all))
    recommended = _load_recommended_keywords()
    if recommended:
        parts.append("## このチャンネルで実績のある題材要素（分析データ由来・積極的に活用推奨）\n"
                     + "、".join(recommended[:15]))
    if rejected:
        parts.append("## 直前に却下された案（同じ題材・似た題材を再提出するな）\n"
                     + "\n".join(f"- {r}" for r in rejected[-6:]))
    return "\n\n".join(parts)


def _generate_script_raw(model: genai.GenerativeModel, genre: str, wiki_context: str,
                         history: list[str], long_mode: bool = False,
                         steering: str = "") -> dict:
    history_str = _format_history_block(history, limit=200)
    if steering:
        history_str = history_str + "\n\n" + steering

    # チャンネル別プロンプト設定
    is_zatsugaku = CHANNEL_TYPE == "zatsugaku"
    channel_role = "雑学・豆知識チャンネルの台本ライター" if is_zatsugaku else "YouTube都市伝説チャンネルの台本ライター"
    tone_short = "驚き・ためになる・親しみやすい語り口。「実はこんな事実が！」「知らなかった！」と思わせる" if is_zatsugaku else "神秘的・緊迫感あり・「信じるか信じないかはあなた次第」風。単なる歴史/事実紹介に留めず、秘密結社・黒幕・呪い・隠蔽といったオカルト/陰謀論的なミステリーの香りを必ず漂わせる（ただし事実はWikipedia情報に忠実に・捏造は禁止）"
    tone_long = "驚き・ためになる・知的好奇心をくすぐる教養ドキュメンタリー調" if is_zatsugaku else "神秘的・緊迫感あり・ドキュメンタリー風の深掘り調"
    img_style_short = "bright colorful educational photo, vibrant, clear subject" if is_zatsugaku else "cinematic dark mysterious scene of"
    img_style_long = "bright colorful wide educational photo, vibrant, 16:9" if is_zatsugaku else "cinematic wide horizontal scene of"
    title_hint = "驚き・ためになるタイトル（「実は〜だった！」「知らなかった〜」など）" if is_zatsugaku else "25字以内。【閲覧注意】【古代の謎】等の角括弧を冒頭に付け、題材の固有名詞を必ず入れて『〜の謎』のように未知を明確に提示する型にする（実測で最も伸びる型）。『〜かもしれない』で終わる曖昧な煽りタイトルは禁止（実測で下位共通要因）"

    # ターゲット視聴者（2026-07-23 YouTube Analytics実測: 両チャンネルとも45歳以上男性が中心）
    if is_zatsugaku:
        audience_block = """
# ターゲット視聴者（実測データに基づく・重要）
- 視聴者の中心は45歳以上の男性（45歳以上が62%・男性75%。24歳以下は4%しかいない）
- この世代に響く題材を優先する: 健康・人体の数字/昭和の生活の知恵/地名や伝統の由来/自然・生き物の驚き/教養として語れる原理
- 若者の流行・SNSネタ・学校ノリは優先しない。落ち着いた知的な語り口で「へえ」と唸らせる
"""
    else:
        audience_block = """
# ターゲット視聴者（実測データに基づく・重要）
- 視聴者の中心は45〜65歳以上の男性（45歳以上が66%・男性73%。24歳以下は3%しかいない）
- この世代の教養に響く題材を優先する: 古代文明・オーパーツ/未確認生物・怪物・怪人（ある程度認知度のある存在）/実在の事件・場所・遺物の未解明の謎
- 昭和ノスタルジー系（口裂け女・予言・パニック等の定番ネタ）は実測で下位のため優先しない。定番より「知られざる固有名詞の謎」を選ぶ
- 若者の流行語・ネットスラング・学校ノリは使わない。落ち着いた大人向けの語りで知的好奇心を刺激する
- 煽って怖がらせるより「実在する固有名詞の謎を深掘りする」姿勢で書く（この層には煽りが陳腐に映る）
"""

    if long_mode:
        total_scenes = LONG_ITEM_COUNT + 2  # イントロ + 本編 + アウトロ
        prompt = f"""
あなたは{channel_role}です。
ジャンル「{genre}」の中から**1つの題材を選び**、約10分の横型ドキュメンタリー動画の台本を作成してください。
事例を次々紹介する「◯◯選」の羅列形式は禁止。1つの謎を章立てで深掘りする物語型です。
（実測: 羅列型は視聴維持率16〜29%で2分台離脱。物語型への転換が目的）

# 構成（合計{total_scenes}シーン・物語型）
1. イントロ（1シーン・**20秒＝60文字以内**）: この動画で解き明かす「3つの謎」を一気に予告。挨拶・チャンネル紹介・「今回は〜を紹介します」型の平坦な導入は禁止。1文目から謎を突きつける
   ※実測: 視聴者の3〜4割は開始45秒までに離脱を判断する。イントロを引き延ばさず、**開始40秒以内（第1章の頭）で1つ目の謎の核心的事実を提示**すること
2. 本編（{LONG_ITEM_COUNT}シーン）: 1つの題材を**3〜5章の物語**として掘り下げる。1章=連続する複数シーン
   - 章の流れの例: 「謎の提示 → 記録に残る事実 → 定説 → 定説を覆す矛盾 → 深層の考察 → 残された謎」
   - **全体の中盤（3分前後）に「しかし、ここからが本題です」にあたる転換章を必ず置く**（2〜3分で離脱が集中するため、中間で興味をリセットする）
   - 各シーンは前のシーンから話が連続する。「第○位」「その○」のような番号形式は禁止
   - 各シーンの末尾に「次への引き」を入れる（例:「しかし、公式記録には、奇妙な空白がありました。」）。章の切れ目で疑問を投げ、次章の頭で答える
   - 各章の開始シーンには "chapter" フィールドで章見出し（12字以内）を付ける。イントロは "chapter": "導入"
3. アウトロ（1シーン）: 冒頭の予告への答えを一言で回収し、「信じるか信じないかはあなた次第」系の余韻で締める。最後に「チャンネル登録と高評価をお願いします」という趣旨の呼びかけを明確に入れる

# 制約
- 各本編シーンのナレーション: 100〜130文字（ひらがな換算）。冗長にしない
- 全体で約3000〜3300文字（**約8分**。実測で終盤まで残る視聴者は1割のため、10分より8分の方が平均視聴率が上がる）
- 語調: {tone_long}
- ハルシネーション禁止: 下記Wikipedia情報を軸に事実を積み上げる
- 過去タイトルと被らない題材を選ぶ。RECOMMENDEDキーワードや直近で伸びているショートの題材（怪物・オーパーツ・古代文明等）の深掘り版が望ましい
{audience_block}

# 固有名詞の明示（最重要・絶対厳守）
- 各本編シーンは、必ず「実際の固有名詞」を明示すること。ぼかし表現だけで終わらせるのは厳禁
- ❌悪い例:「地球を裏で支配する一族」「とある秘密結社」「ある国家機関」「謎の人物」だけで誰/何か分からない
- ⭕良い例:「世界を裏で操るとされる、ロスチャイルド家」「秘密結社、イルミナティ」「アメリカの諜報機関、CIA」
- 黒幕・組織・人物・場所・事件は、必ず具体名（ロスチャイルド、フリーメイソン、イルミナティ、ロックフェラー、CIA、エリア51 等）を本文中に書く
- 神秘的にぼかす演出は「具体名を出した上で」その背後の謎を語る形にする（具体名そのものを隠さない）
- 各シーン冒頭の「第○位/その○」の直後に、その事案の主役となる固有名詞を必ず登場させること
- ただし固有名詞も含めWikipedia情報に忠実に。実在しない団体名・人名の捏造は禁止

# 字幕の読みやすさ（重要）
- narrationは字幕として画面に表示される。読点「、」が字幕の改行・区切り位置になる
- 意味の区切り（文節）ごとに読点「、」を打つこと。例:「これは天の動きと、地の出来事が対応しており、」
- 読点で区切られた1区切りは概ね11文字以内を目安にする（長すぎる区切りを作らない）
- **じゅうぶんの意味では「充分」と書く**。「十分」と書くと字幕で「10分」に化け、読みも じゅっぷん とぶれる（2026-08-27 全チャンネルで発生）
- **narration_kana は、読点なしで20字以上続けない**。27字を超えると音声合成が語の切れ目を誤り、抑揚と間が崩れる（2026-08-25 実測: 心理学の投稿済み5本で52件）
- 1文が長くなりすぎないよう句点「。」でこまめに区切る（1文は概ね30文字以内）
- narration_kana にも同じ位置に読点を打ち、narration と区切りを一致させる
- 開き括弧「「」「（」などの直前には読点「、」を打つ（引用・セリフの前に間ができて聴きやすい）。例:「その名は、「エメラルド」」
- 長い区切りは作らず、こまめに読点で改行する（1行に収まらない長文を避ける）
- 括弧「」で囲む引用・名称は10文字以内にする。10文字を超える名称は括弧を使わず地の文で言い換える
- 英語表記の名称・単語は書かない。必ずカタカナに置き換える（例: Dream Link → ドリームリンク）。字幕で英単語は分断されて読めなくなる
- 「しかし」「さらに」「そして」「特に」「また」等の短い接続詞・副詞を、開き括弧・長いカタカナ語・固有名詞（字幕で分断できない語）の直前に置かない。その短語だけが1行に取り残されて不自然になる。文中へ移すか説明語を足す。例:×「さらに、フリーメイソンの暗号説」→○「フリーメイソンの暗号説も、さらに囁かれている」／×「特に「後ろの正面」という歌詞」→○「歌詞の中でも「後ろの正面」は、特に矛盾している」

# 過去タイトル
{history_str}

# Wikipedia参考情報
{wiki_context}

# 出力形式（JSONのみ・余分なテキスト不要）
{{
  "title": "動画タイトル（{title_hint}。1テーマ深掘り型・「◯◯選」等の数字羅列は禁止）",
  "description": "概要欄テキスト（200字程度）",
  "tags": ["タグ1", ...（10個）],
  "scenes": [
    {{
      "chapter": "章見出し（12字以内。章の開始シーンのみ付ける。それ以外は省略）",
      "narration": "漢字混じりナレーション（字幕用）",
      "narration_kana": "ひらがなナレーション（VOICEVOX用）",
      "image_prompt": "{img_style_long} ... (English, for image AI)"
    }}
    // × {total_scenes}シーン（イントロ1 + 本編{LONG_ITEM_COUNT} + アウトロ1）
  ]
}}
"""
    else:
        prompt = f"""
あなたは{channel_role}です。
ジャンル「{genre}」で、視聴者を引き込むYouTube Shortsの台本を作成してください。

# 尺（最重要・YouTube Shortsアルゴリズム最適化）
- **必ず60秒未満（理想は45〜52秒）に収める**。中途半端な70秒超はショートで露出が落ちる
- そのため情報を詰め込みすぎず、最も恐ろしい/不可解な核心に絞ってテンポよく語る

# 制約
- シーン数: {TARGET_SCENE_COUNT}
- ナレーション合計: 340〜410文字（ひらがな換算・60秒未満厳守。安全マージンのため絶対に410を超えない）
- 語調: {tone_short}
- ハルシネーション禁止: 下記Wikipedia情報のみを事実として使用
- 以下の過去タイトルと被らないこと
{audience_block}

# 冒頭2秒のフック（最重要・離脱防止）
- 1シーン目の1文目は、最初の2秒で視聴者の指を止める「フック」にする。12〜18字の短い一撃
- 都市伝説の不気味さ・恐怖・謎を凝縮し、ゾッとさせる or 続きを見ずにいられなくする
- **必ずこの題材ならではの具体語（固有名詞・場所・数字・物体）を1つ入れる**。抽象的な定型句にしない
- 型のバリエーション（毎回違う型を選び、表現も使い回さない）:
  1) 問いかけ型: 「その橋の下に、"何"がいると思いますか。」
  2) 断定・警告型: 「絶対に、その絵を家に飾ってはいけない。」
  3) 異常提示型: 「その村では、毎年、一人ずつ消えていく。」
  4) 数字インパクト型: 「239人を乗せた飛行機が、消えた。」
  5) 逆説・矛盾型: 「燃えたのは、家具だけではなかった。」
  6) 前提を揺るがす型: 「あなたが知るその歴史は、誰かに作られた嘘だった。」（※「〜かもしれない」でぼかさず断定する。曖昧な煽りは実測で下位共通要因）
- **【重要】上の例文・以下の汎用句を"そのまま"使うのは禁止**:
  - ❌「あなたは、これを見たことがありますか。」 ❌「これを見たことがありますか。」
  - ❌「あなたは、この音が聞こえますか。」 ❌「もし、これを見てしまったら…」
  これらは題材を問わず量産され被るので、必ず題材固有の語に置き換えること
- 禁止: 「今回は〜を紹介します」「皆さんは知っていますか」のような平凡で薄い導入
- フックの直後（2文目以降）で、背景や詳細を語り始める流れにする

# 字幕の読みやすさ（重要）
- narrationは字幕として画面に表示される。読点「、」が字幕の改行・区切り位置になる
- 意味の区切り（文節）ごとに読点「、」を打つこと。例:「これは天の動きと、地の出来事が対応しており、」
- 読点で区切られた1区切りは概ね11文字以内を目安にする（長すぎる区切りを作らない）
- **narration_kana は、読点なしで20字以上続けない**。27字を超えると音声合成が語の切れ目を誤り、抑揚と間が崩れる（2026-08-25 実測: 心理学の投稿済み5本で52件）
- 1文が長くなりすぎないよう句点「。」でこまめに区切る（1文は概ね30文字以内）
- narration_kana にも同じ位置に読点を打ち、narration と区切りを一致させる
- 開き括弧「「」「（」などの直前には読点「、」を打つ（引用・セリフの前に間ができて聴きやすい）。例:「その名は、「エメラルド」」
- 長い区切りは作らず、こまめに読点で改行する（1行に収まらない長文を避ける）
- 括弧「」で囲む引用・名称は10文字以内にする。10文字を超える名称は括弧を使わず地の文で言い換える
- 英語表記の名称・単語は書かない。必ずカタカナに置き換える（例: Dream Link → ドリームリンク）。字幕で英単語は分断されて読めなくなる
- 「しかし」「さらに」「そして」「特に」「また」等の短い接続詞・副詞を、開き括弧・長いカタカナ語・固有名詞（字幕で分断できない語）の直前に置かない。その短語だけが1行に取り残されて不自然になる。文中へ移すか説明語を足す。例:×「さらに、フリーメイソンの暗号説」→○「フリーメイソンの暗号説も、さらに囁かれている」／×「特に「後ろの正面」という歌詞」→○「歌詞の中でも「後ろの正面」は、特に矛盾している」

# 締めの定型句＋コメント誘発（重要）
- 最終シーンは「信じるか、信じないかは、あなた次第です。」の定型句で締める
- 「信じるかは」のように一部を省略しない。必ず「信じるか、信じないかは、あなた次第です。」と完全な形で書く
- その**直後に、コメントを促す短い一文を1つだけ加える**。題材に応じて変える（例:「あなたは、どう思いますか。コメントで教えてください。」「あなたの身近にも、こんな話はありませんか。」）。次回予告は絶対に書かない
- narration_kana も同じ内容を完全なひらがなで書く（例:「しんじるか、しんじないかは、あなたしだいです。あなたは、どうおもいますか。こめんとでおしえてください。」）

# 外来語・カタカナ語の読み
- narration_kana のカタカナ外来語は長音「ー」を使った自然な発音にする
- 例: データ→「でーた」、システム→「しすてむ」、エネルギー→「えねるぎー」

# 過去タイトル
{history_str}

# Wikipedia参考情報
{wiki_context}

# 出力形式（JSONのみ・余分なテキスト不要）
{{
  "title": "{title_hint}",
  "description": "概要欄テキスト（150字程度）",
  "tags": ["タグ1", "タグ2", ...（10個）],
  "scenes": [
    {{
      "narration": "漢字混じりナレーション（字幕用）",
      "narration_kana": "ひらがなナレーション（VOICEVOX用）",
      "image_prompt": "{img_style_short} ... (English, for image AI)"
    }}
    // × {TARGET_SCENE_COUNT}シーン
  ]
}}
"""
    text = _gen_with_retry(prompt).strip()
    # ```json ... ``` ブロックを除去
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Geminiが文字列値内に生の改行/制御文字を入れることがあるので strict=False で許容
    return json.loads(text, strict=False)


def _factcheck(model: genai.GenerativeModel, script_json: dict, wiki_context: str) -> dict:
    genre_label = "雑学・豆知識" if CHANNEL_TYPE == "zatsugaku" else "都市伝説"
    prompt = f"""
以下の{genre_label}台本をファクトチェックしてください。
Wikipedia情報と照合し、明らかに誤り・不確かな表現を修正してください。

# Wikipedia参考情報
{wiki_context}

# 台本JSON
{json.dumps(script_json, ensure_ascii=False)}

# 指示
- 事実と矛盾する記述は修正または削除
- 「〜と言われている」「〜という説がある」など断言を避ける表現に変換
- 修正後のJSONのみ出力（余分なテキスト不要）
"""
    text = _gen_with_retry(prompt).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return script_json  # パース失敗時は元のまま


def _proofread(model: genai.GenerativeModel, script_json: dict) -> dict:
    genre_label = "雑学・豆知識" if CHANNEL_TYPE == "zatsugaku" else "都市伝説"
    prompt = f"""
以下の{genre_label}台本を校閲してください。

# 指示
- narration_kana が正しいひらがなか厳密に確認・修正する
- 文章の流れを改善（ただし内容は変えない）
- 各シーンの image_prompt フィールドは必ず保持する（削除・改変しない）
- title / description / tags / scenes の全フィールドを保持した完全なJSONを出力
- 修正後のJSONのみ出力

# narration_kana の誤読チェックリスト（よくある間違い）
- 世論: よろん → せろん
- 早急: そうきゅう → さっきゅう
- 重複: じゅうふく → ちょうふく
- 発足: はっそく → ほっそく
- 完遂: かんつい → かんすい
- 踏襲: ふみしゅう → とうしゅう
- 相殺: そうさつ → そうさい
- 代替: だいがえ → だいたい
- 一段落: ひとだんらく → いちだんらく
- 汎用: はんぎょう → はんよう
- 前代未聞: ぜんだいみぶん → ぜんだいみもん
- 御利益: おりやく → ごりやく
- 言質: ことばじち → げんち
- 割愛: わりあい → かつあい
- 遵守: じゅんじゅ → じゅんしゅ
- 年俸: ねんきゅう → ねんぽう
- 伝播: でんぱん → でんぱ
- 造詣: ぞうけい（正）→ そのまま
- 逆鱗: ぎゃくりん → げきりん
- 境内: きょうない → けいだい
- 五月雨: ごがつあめ → さみだれ
- 時雨: ときあめ → しぐれ
- 他人事: たにんごと → ひとごと
- 巣窟: すくつ → そうくつ

# 台本JSON
{json.dumps(script_json, ensure_ascii=False)}
"""
    text = _gen_with_retry(prompt).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return script_json


def _repair_scene_kana(model: genai.GenerativeModel, scene: dict, issues: list[str]) -> str | None:
    """誤読疑いのシーンの narration_kana だけを再生成する（1回だけ・失敗はNoneを返す）"""
    prompt = f"""以下のナレーション文の読み仮名（純ひらがな）を正確に書き直してください。

# ナレーション
{scene.get('narration', '')}

# 指摘されている読みの問題
{chr(10).join('- ' + x for x in issues)}

# 指示
- 純ひらがな＋読点句点のみ。漢字・カタカナ・英字は使わない
- 読点「、」の位置は元のナレーションと同じ位置に打つ
- 修正後の読み仮名だけを出力（余分なテキスト不要）
"""
    try:
        text = _gen_with_retry(prompt).strip()
        text = re.sub(r"^```.*?\n|```$", "", text, flags=re.S).strip()
        # 出力が仮名文でなければ失敗扱い
        if text and not re.search(r"[一-龥A-Za-z]", text):
            return text
    except Exception:
        pass
    return None


def generate_script(genre: str | None = None, max_retry: int = 6, long_mode: bool = False) -> Script:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY が設定されていません")

    model = _setup_gemini()
    history = _load_history()

    if genre is None:
        genre = random.choice(TOPIC_GENRES)

    print(f"  ジャンル: {genre}{'（ロング10分）' if long_mode else ''}")
    wiki_context = _wikipedia_search(genre)
    print(f"  Wikipedia取得完了")

    # 生成済み在庫（--no-upload で history に載らない分）の題材を走査し、本文ベースで被りを弾く
    stock_themes = _load_stock_themes()
    rejected: list[str] = []  # 却下案を次の試行のプロンプトに反映（同じ題材ガチャの防止）

    for attempt in range(1, max_retry + 1):
        print(f"  台本生成 (試行 {attempt}/{max_retry})...")
        steering = _build_steering_block(stock_themes, rejected)
        raw = _generate_script_raw(model, genre, wiki_context, history,
                                   long_mode=long_mode, steering=steering)

        title = raw.get("title", "")
        if _is_duplicate(title, history):
            print(f"  タイトル重複: {title} → リトライ")
            rejected.append(f"{title}（過去タイトルと重複）")
            continue

        # 手動BANNED題材はタイトル/本文の両方をブロック
        _banned = BANNED_THEME_KEYWORDS or []
        if _banned:
            _alltext = title + " " + " ".join(
                (sc.get("subtitle") or "") + (sc.get("narration") or "")
                for sc in raw.get("scenes", [])
            )
            _hit = next((kw for kw in _banned if kw and kw in _alltext), None)
            if _hit:
                print(f"  BANNED題材検出: {title} に「{_hit}」→ リトライ")
                rejected.append(f"{title}（禁止ワード「{_hit}」を使用）")
                continue

        # 診断由来の自動BANNED（低パフォーマー頻出語）はタイトルのみブロック
        # （本文まで見ると一般語の誤爆で正常生成が止まるため）
        _hit = next((kw for kw in AUTO_BANNED_TITLE_KEYWORDS if kw and kw in title), None)
        if _hit:
            print(f"  自動BANNED題材検出（タイトル）: {title} に「{_hit}」→ リトライ")
            rejected.append(f"{title}（禁止ワード「{_hit}」を使用）")
            continue

        # 本文キーワードで在庫と被っていないか（煽りだけ違う同一ネタを検出）
        dup_with = _is_theme_duplicate(title, raw.get("scenes", []), stock_themes)
        if dup_with:
            print(f"  題材重複（本文）: {title} ≒「{dup_with}」 → リトライ")
            rejected.append(f"{title}（在庫「{dup_with}」と同一題材）")
            continue

        print(f"  ファクトチェック...")
        raw = _factcheck(model, raw, wiki_context)

        print(f"  校閲...")
        raw = _proofread(model, raw)

        # 読み間違いガード: 音声合成の前に誤読を検出・修復（投稿後差し替えの根絶）
        from src.reading_guard import check_and_fix
        reading_ng = False
        for si, sc in enumerate(raw.get("scenes", [])):
            fixed_kana, fixes, issues = check_and_fix(
                sc.get("narration", ""), sc.get("narration_kana", ""))
            if fixes:
                print(f"  読み自動修正 シーン{si}: {', '.join(fixes)}")
                sc["narration_kana"] = fixed_kana
            if issues:
                # かな修復を1回だけ試みる（対象シーンのみ・安いリクエスト）
                repaired = _repair_scene_kana(model, sc, issues)
                if repaired:
                    sc["narration_kana"] = repaired
                    _, _, issues = check_and_fix(sc.get("narration", ""), repaired)
            if issues:
                print(f"  誤読疑い シーン{si}: {'; '.join(issues)} → リトライ")
                reading_ng = True
                break
        if reading_ng:
            rejected.append(f"{title}（読み仮名の誤読疑いが修復不能）")
            continue

        # 数字の読みガード（2026-08-05 追加）。辞書照合は漢字語しか見ないため
        # 「32倍→さんじゅうばい(=30倍)」のような数字の誤読が素通りしていた。
        from src.reading_guard import check_script_numbers
        num_ng = check_script_numbers(raw)
        if num_ng:
            print(f"  数字の誤読疑い: {'; '.join(num_ng)} → リトライ")
            rejected.append(f"{title}（数字の誤読疑い）")
            continue

        def _clean(t: str) -> str:
            # Geminiが入れる改行・全角空白を除去（字幕・音声に混入すると不具合の元）
            return t.replace("\n", "").replace("\r", "").replace("　", "").strip()

        scenes = [
            Scene(
                index=i,
                narration=_clean(s["narration"]),
                narration_kana=_clean(s["narration_kana"]),
                image_prompt=s["image_prompt"],
                chapter=(s.get("chapter") or "").strip(),
            )
            for i, s in enumerate(raw["scenes"])
        ]

        script = Script(
            title=raw["title"],
            description=raw["description"],
            tags=raw.get("tags", []),
            genre=genre,
            scenes=scenes,
        )
        print(f"  タイトル: {script.title}")
        return script

    raise RuntimeError(f"{max_retry}回試行しましたが台本生成に失敗しました")


def save_script_to_history(script: Script) -> None:
    _save_history(script.title)
