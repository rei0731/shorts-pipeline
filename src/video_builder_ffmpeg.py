"""FFmpegベースの動画編集（MoviePy版の高速代替）
MoviePyと同じシグネチャ・同じ見た目を保ちつつ5〜10倍高速。
字幕はPILでPNG生成→FFmpeg overlayフィルタで合成（スタイル完全互換）。
"""
import glob
import json
import os
import random
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    BGM_DIR, BGM_FILE, BGM_VOLUME,
    VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH,
    KEN_BURNS_ZOOM_FROM, KEN_BURNS_ZOOM_TO,
    SUBTITLE_BOTTOM_MARGIN as _DEF_SUB_MARGIN,
    SUBTITLE_FONT_SIZE,
)
try:
    from config.settings import HOOK_SE_FILE, HOOK_SE_VOLUME
    from config.settings import (EYECATCH_ENABLED, EYECATCH_SE_FILE, EYECATCH_SE_VOLUME,
                                 EYECATCH_SEC, EYECATCH_COUNT, EYECATCH_MIN_SCENES,
                                 EYECATCH_SE_DELAY)
except ImportError:
    HOOK_SE_FILE, HOOK_SE_VOLUME = "", 0.9
    EYECATCH_ENABLED, EYECATCH_SE_FILE, EYECATCH_SE_VOLUME = False, "", 0.28
    EYECATCH_SEC, EYECATCH_COUNT, EYECATCH_MIN_SCENES = 2.2, 3, 16
    EYECATCH_SE_DELAY = 0.35
from src.voice_generator import VoiceResult

# モジュール変数（ロングモード時に上書き可）
W = VIDEO_WIDTH
H = VIDEO_HEIGHT
SUB_FONT_SIZE = SUBTITLE_FONT_SIZE
SUB_BOTTOM_MARGIN = _DEF_SUB_MARGIN


def _ffmpeg() -> str:
    """ffmpeg実行ファイルのパスを取得"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


_NVENC_AVAILABLE: bool | None = None  # 起動後1回だけ判定してキャッシュ


def _video_codec_args() -> list[str]:
    """映像エンコーダ引数。NVIDIA GPUがあれば h264_nvenc（数倍高速）、なければ libx264。
    品質は libx264 crf18 相当（nvenc vbr cq19）。環境変数 FORCE_LIBX264=1 でCPUに固定可。"""
    global _NVENC_AVAILABLE
    if os.environ.get("FORCE_LIBX264"):
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    if _NVENC_AVAILABLE is None:
        try:
            r = subprocess.run(
                [_ffmpeg(), "-hide_banner", "-y", "-f", "lavfi",
                 "-i", "testsrc=duration=0.1:size=320x240:rate=30",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, timeout=30,
            )
            _NVENC_AVAILABLE = (r.returncode == 0)
        except Exception:
            _NVENC_AVAILABLE = False
        print(f"  エンコーダ: {'h264_nvenc (GPU)' if _NVENC_AVAILABLE else 'libx264 (CPU)'}")
    if _NVENC_AVAILABLE:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]


def _get_bgm() -> str | None:
    """BGMファイルパスを返す"""
    if BGM_FILE:
        fixed = os.path.join(BGM_DIR, BGM_FILE)
        if os.path.exists(fixed):
            return fixed
    files = glob.glob(os.path.join(BGM_DIR, "*.mp3")) + glob.glob(os.path.join(BGM_DIR, "*.wav"))
    return random.choice(files) if files else None


# --- 字幕ヘルパー（video_builder.pyから流用） ---

from config.settings import FONT_PATH, SUBTITLE_COLOR, SUBTITLE_STROKE_COLOR, SUBTITLE_STROKE_WIDTH


def _sub_font(size: int):
    """テロップ・フック・アイキャッチ用のフォント。NotoSansJP-Bold.ttf は中身が可変フォントで、
    そのまま開くと既定の Thin（極細）で描かれていた。太字（Bold）を明示する
    （2026-10-26 channel Dのユーザー「字幕がちょっと読みにくい」→ channel A にも同じ直し）。"""
    from PIL import ImageFont
    f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_name("Bold")
    except Exception:
        pass
    return f


SUB_BAND_ALPHA = 215   # テロップの黒帯の濃さ（0〜255）。130→215（2026-10-26 同上）

SUB_CHARS_PER_LINE = 10  # 1行の最大文字数（10文字以内は改行せず1行。長い行は2行に割る）
SUB_MAX_LINES = 2
SUB_MAX_CHARS = 16   # 1カードの最大表示文字数（超えたら分割し次の字幕へ送る）
SUB_MIN_DURATION = 3.5


def _scaled_line_n() -> int:
    """1行の最大文字数。ショート(W=1080)は SUB_CHARS_PER_LINE のまま、
    ロング(横1920)はchannel Bと統一して20字を床とする
    （ロング用フォントは60ptと小さいため20字でも横幅に収まる）。"""
    base = SUB_CHARS_PER_LINE
    n = max(base, int(base * W / 1080))
    if W > 1080:
        n = max(n, 20)
    return n


def _scaled_max_chars() -> int:
    """1カードの最大表示文字数。ショートは SUB_MAX_CHARS のまま、
    ロングはchannel Bと統一して40字（=20字×2行）を床とする。
    中央に合法な改行点が無い長文は _scaled_max_lines()=3 と _wrap_chars の
    行数調整で3行に均等分割され、2行目のはみ出しは防がれる。"""
    base = SUB_MAX_CHARS
    n = max(base, int(base * W / 1080))
    if W > 1080:
        n = max(n, 40)
    return n


# 難読・読みにくい漢字は字幕でだけひらがな表示にする（音声・台本は不変）。
_SUBTITLE_KANJI_TO_HIRAGANA = {
    # --- 汎用（channel B から流用）---
    "宛も": "あたかも", "宛かも": "あたかも", "恰も": "あたかも",
    "何卒": "なにとぞ", "夙に": "つとに", "悉く": "ことごとく",
    "強ち": "あながち", "態と": "わざと", "態々": "わざわざ",
    "予て": "かねて", "兎角": "とかく", "兎に角": "とにかく",
    "屹度": "きっと", "頻りに": "しきりに", "若しくは": "もしくは",
    "且つ": "かつ", "然し": "しかし", "然も": "しかも",
    "尤も": "もっとも", "稍々": "やや", "甚だ": "はなはだ",
    "概ね": "おおむね", "予め": "あらかじめ",
    # --- 都市伝説向け（怪奇・神秘系の難読語）---
    # 活用語は語幹で置換し、連用形・終止形など全活用形に対応する。
    "禍々": "まがまが",   # 禍々しい / 禍々しく
    "忌まわ": "いまわ",   # 忌まわしい
    "彷徨": "さまよ",     # 彷徨う / 彷徨い
    "蠢": "うごめ",       # 蠢く / 蠢き
    "囁": "ささや",       # 囁く / 囁き
    "呟": "つぶや",       # 呟く / 呟き
    "佇": "たたず",       # 佇む / 佇まい
    "祟": "たた",         # 祟り / 祟る
    "穢": "けが",         # 穢れ / 穢らわしい
    "呪詛": "じゅそ", "瘴気": "しょうき",  # 「怨念」は読める語なので漢字のまま表示（2026-07-10 ユーザー指示）
    "魑魅魍魎": "ちみもうりょう", "幽世": "かくりよ", "黄泉": "よみ",
    "凄惨": "せいさん", "戦慄": "せんりつ", "畏怖": "いふ",
    "怪訝": "けげん", "得体": "えたい", "形相": "ぎょうそう",
}


def _sanitize_subtitle(text: str) -> str:
    """難読漢字を字幕でひらがなに（視聴者が一目で読めるよう）。

    直前が漢字なら熟語の一部とみなして置換しない。単純な部分文字列置換だと
    「事態となりました」の中の「態と」が「わざと」に化け、
    「事わざとなりました」と表示される事故が起きた（2026-08-07 ラクイラ）。
    """
    for k, v in _SUBTITLE_KANJI_TO_HIRAGANA.items():
        if k not in text:
            continue
        out, i = [], 0
        while True:
            j = text.find(k, i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j])
            # 直前が漢字＝「事態と」のような熟語の途中なので置換しない
            if j > 0 and _is_kanji(text[j - 1]):
                out.append(k)
            else:
                out.append(v)
            i = j + len(k)
        text = "".join(out)
    return text


def _remove_punctuation(text: str) -> str:
    return text.replace("、", "").replace("。", "")


def _strip_period(text: str) -> str:
    """文末の句点・感嘆符・疑問符を除去（読点「、」は改行ヒントとして残す）。
    ただし括弧内（作品タイトル・引用、例「あの子はだあれ？」）の「？！」は
    内容の一部なので残す（原則5：タイトルを改変しない）。"""
    out = []
    for i, ch in enumerate(text):
        if ch in "。！？" and not _in_bracket(text, i):
            continue
        out.append(ch)
    return "".join(out).strip()


def _name_like(s: str) -> bool:
    """固有名詞（人名・地名など）らしい短い塊か。純漢字2〜6字 or 純カタカナ。
    列挙（清水崇、黒沢清、中田秀夫）の読点を保持するかの判定に使う。"""
    s = s.strip("、。！？「」『』（）【】〈〉《》")
    if not (2 <= len(s) <= 6):
        return False
    return all(_is_kanji(c) for c in s) or all(_is_kata(c) for c in s)


def _thin_sub_commas(text: str, min_len: int = 7) -> str:
    """字幕用に過剰な読点を間引く。Geminiが文節ごとに細かく打ちすぎると、
    文頭近くの読点で極端に短い行ができ→結合で1行縮小、細切れカードの原因になる。
    直近の区切りがmin_len未満なら、その読点を除去して前後をつなぐ。
    ただし結合するとカード上限(SUB_MAX_CHARS)を超える場合は読点を残す。読点はカード分割の
    最良の境界なので、消すと長文が機械分割されて語の途中(例「衰｜退」)で割れる事故になる。"""
    import re as _re
    out = []
    for sent in _split_sentences(text):
        if not sent:
            continue
        parts = sent.split("、")
        buf = parts[0]
        for p in parts[1:]:
            last = buf.rsplit("、", 1)[-1]
            merged_len = len(_remove_punctuation(last + p))
            enum = _name_like(last) and _name_like(p)   # 固有名詞の列挙（原則4例外）は読点を残す
            # 名詞｜名詞（人、鳥、魚／19日後、客が）も残す。消すと「人鳥魚」とくっつく（2026-09-24）
            _l, _r = last.rstrip(), p.lstrip()
            if _KEEP_GLUE_COMMA and _l and _r and _noun_ch(_l[-1]) and _noun_ch(_r[0]):
                enum = True
            # 極端に短い区切り（4字以下）は、結合するとカード上限を超えても読点を捨てる。
            # 残すと「これは、」0.62秒のような読めないカードになるため。上限超過分は
            # このあとカード分割が自然な位置で割り直す（2026-08-24）。
            # 2026-08-26: しきい値を6→4に下げた。6字の「あなたは最近、」まで
            # 読点を捨てていたため、カード上限を超えた文が機械分割され
            # 「あなたは最近何を頭に｜残してしまっていますか」と意味の切れ目を無視していた。
            # 「これは、」(3字)のような読めない断片だけを対象にする。
            _stub = len(_remove_punctuation(last)) <= 4
            if len(last) < min_len and (merged_len <= SUB_MAX_CHARS or _stub) and not enum:
                buf += p              # 直近区切りが短い → 読点を捨てて結合
            else:
                buf += "、" + p       # 短い/列挙/上限超過 → 分割点として読点を残す
        out.append(buf)
    return "".join(out)


def _split_subtitle(text: str) -> list[str]:
    import re
    text = text.replace("\n", "").strip()
    sentences = _split_sentences(text)
    chunks: list[str] = []
    for sent in sentences:
        if len(sent) <= SUB_MAX_CHARS:
            chunks.append(sent)
            continue
        units = [u for u in re.split(r"(?<=、)", sent) if u]
        buf = ""
        for u in units:
            if len(buf) + len(u) <= SUB_MAX_CHARS:
                buf += u
            else:
                if buf:
                    chunks.append(buf)
                if len(u) <= SUB_MAX_CHARS:
                    buf = u
                else:
                    for i in range(0, len(u), SUB_MAX_CHARS):
                        chunks.append(u[i:i + SUB_MAX_CHARS])
                    buf = ""
        if buf:
            chunks.append(buf)
    # 短い主題カード（「1957年には、」「あなたは、」等）を単独で残さず次のカードへ結合する
    # （2026-08-26 ユーザー指摘「1957年にはを後ろのテロップに統合」。読点＝音声は変えず
    # テロップの結合だけ行う）。結合後が2行×10字（=20字）を超えるものは結合しない。
    i = 0
    while i < len(chunks) - 1:
        p = _remove_punctuation(chunks[i]).strip()
        if (p.endswith("は") and len(p) <= 8
                and len(_remove_punctuation(chunks[i] + chunks[i + 1])) <= 20):
            chunks[i] = chunks[i] + chunks[i + 1]
            del chunks[i + 1]
            continue
        i += 1
    return chunks or [text]


_OPENERS = "「『（【〈《"
# 改行のヒント（この文字の直後で改行＝行末に置きたい助詞・接続）。「て」も含め「〜て/いる」を許容
# 「も」は「もの・もし」等の自立語が多く誤分割の元になるため含めない
_PARTICLES = "をにはがへとのでやかねるたいのらばて"
# 字幕の縁取り設定（比較・調整用にモジュール変数化。2026-08-24）。
# 黒縁は太いほど「影」に見える。日本語は画数が多いので、白縁なら4px、黒縁なら5pxが目安。

# 1行の最小文字数。これ以下の行（「夢は」「捉え」等）は読みのリズムを壊すので避ける。
_MIN_LINE_CHARS = 4

# 1カードの最小表示秒。カード分割の時間配分がこれを下回らないようにする。
# preflightのNG閾値(0.7秒)に少し余裕を持たせた値（2026-08-25）。
_MIN_CARD_SEC = 0.75

# 行末に置き去りにすると次の体言と泣き別れになる連体詞（subtitle_lint と同一の定義）。
_DANGLING_TAILS = ("その", "この", "あの", "どの", "同じ", "とある")

# 行頭に来てはいけない文字（小書き仮名・長音・中黒・閉じ括弧・行末助詞は/が/を）。開き括弧は行頭OK
_NO_LINE_HEAD = "ーぁぃぅぇぉっゃゅょゎ々・」』）】〉》、。！？をはが"

# 括弧ペア（引用・作品タイトルを途中で割らないための対応表）
_CLOSERS = "」』）】〉》"
_BRACKET_OPEN = set(_OPENERS)
_BRACKET_CLOSE = set(_CLOSERS)


def _in_bracket(text: str, i: int) -> bool:
    """位置iが括弧ペアの内側（開き括弧と閉じ括弧の間）にあるか。
    括弧内の引用・作品タイトル（例『学校の怪談』「あの子はだあれ？」）を
    途中で改行しないための判定。開き括弧の前・閉じ括弧の後では割ってよい。"""
    depth = 0
    for ch in text[:i]:
        if ch in _BRACKET_OPEN:
            depth += 1
        elif ch in _BRACKET_CLOSE and depth > 0:
            depth -= 1
    if depth <= 0:
        return False
    # 開き括弧がこのテキスト内で閉じる場合のみ保護する。閉じ括弧が後方に無い＝
    # カードをまたぐ長い引用（「村の入り口に…越えたら）は、末尾を保護すると
    # 改行不能で1行はみ出しになるため、通常の文節改行に委ねる。
    return any(ch in _BRACKET_CLOSE for ch in text[i:])


def _split_sentences(text: str) -> list[str]:
    """句点・感嘆符・疑問符で文分割。ただし括弧内（作品タイトル・引用）の
    「。！？」では分割しない。「あの子はだあれ？」の？で文が切れて閉じ括弧「」」が
    孤立するのを防ぐ（原則5）。文末の区切り文字は各文の末尾に残す。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        buf.append(ch)
        if ch in _BRACKET_OPEN:
            depth += 1
        elif ch in _BRACKET_CLOSE and depth > 0:
            depth -= 1
        elif ch in "。！？" and depth == 0:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [s for s in out if s.strip()]


_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG = {"万": 10000, "億": 100000000, "兆": 1000000000000}
_KANJI_NUM_CHARS = set(_KANJI_DIGIT) | set(_KANJI_UNIT) | set(_KANJI_BIG)
# 単位・助数詞（単一の漢数字でも数字化してよい後続文字）。条/丁等の固有名詞化しやすい字は除外。
_COUNTERS = set("人日月年回個本円度倍割歳才名件階位番匹頭羽台時分秒週夜歩発話章巻号杯枚冊機隻軒棟点組")
# 複合動詞の後項（調べ|直す・燃え|出す・引き|込む）。前項の送り仮名から切り離さない（2026-09-14）
_COMPOUND_VERB_HEADS = set("直出込始続合切上下回返過替換付取立入落戻抜終忘去尽渡飛越掛詰着重寄離")
_COMPOUND_NOUN_TAILS = set("口物方事先目手場道声話気主人様")
_NOUN_SUFFIX_WORDS = ("自体", "同士", "以外", "以上", "以下", "以内", "以来", "以降", "未満", "程度", "次第", "向け")   # 付き/込み は動詞（付きません）と衝突するので外した


def _kanji_word_boundary(text: str, i: int) -> bool:
    """漢字｜漢字の位置 i が形態素境界で、かつ次の語が用言・副詞・連体詞・接続詞か。
    「年|新しい」「日|訪れた」は熟語ではないので折ってよい。判定できないときは False。"""
    toks = _morph_tokens(text)
    if not toks:
        return False
    pos = 0
    for surf, parts, _infl in toks:
        if pos == i:
            return parts[0] in ("形容詞", "動詞", "副詞", "連体詞", "接続詞")
        if pos > i:
            return False
        pos += len(surf)
    return False


def _parse_kanji_num(run: str) -> int:
    """漢数字の連なりを整数に変換。〇混じり(二〇二六)は桁連結、十百千万は位取り。"""
    if any(c in run for c in _KANJI_UNIT) or any(c in run for c in _KANJI_BIG):
        total, section, current = 0, 0, 0
        for ch in run:
            if ch in _KANJI_DIGIT:
                current = _KANJI_DIGIT[ch]
            elif ch in _KANJI_UNIT:
                section += (current or 1) * _KANJI_UNIT[ch]
                current = 0
            elif ch in _KANJI_BIG:
                section += current
                total += (section or 1) * _KANJI_BIG[ch]
                section = current = 0
        return total + section + current
    # 桁連結（二〇二六→2026, 五→5）
    return int("".join(str(_KANJI_DIGIT[c]) for c in run))


def _format_jp_number(v: int) -> str:
    """整数を読みやすい日本語表記に。1万以上は万/億/兆を漢字で残す（200000→20万）。"""
    if v < 10000:
        return str(v)
    parts = []
    rem = v
    for base, name in ((10**12, "兆"), (10**8, "億"), (10**4, "万")):
        if rem >= base:
            parts.append(f"{rem // base}{name}")
            rem %= base
    if rem:
        parts.append(str(rem))
    return "".join(parts)


# 漢数字を含むが数値化してはいけない固有名詞（地名・人名等）。
# 連続漢数字やカウンタ的な字を含み誤変換されやすいものを優先的に保護する。
_PROPER_NOUNS = (
    "千駄ヶ谷", "千駄ケ谷", "八千代", "四万十", "五十嵐", "九十九里", "三千院",
    "四日市", "三軒茶屋", "千歳", "千代田", "二子玉川", "十三", "二条城",
    "三条", "四条", "五条", "六本木", "八百屋", "八百長", "五反田", "三浦",
    "四谷", "千葉", "三重", "千住", "八王子", "九段下", "七夕", "五輪",
    # 都市伝説・伝承で出やすい数字入りの固有名詞（2026-08-08 八百比丘尼→800比丘尼 の事故）
    "八百比丘尼", "八岐大蛇", "八尺様", "七人みさき", "四国八十八ヶ所",
    "千手観音", "千羽鶴", "百物語", "八幡", "三途の川", "十字架", "七福神",
    "六道", "四天王", "二十四節気", "千里眼", "百鬼夜行", "九尾", "八咫烏",
    # 数字ではなく慣用句として読む語（一夜→1夜 等の不自然な表示を防ぐ）
    "一夜", "一軒家", "一軒", "一歩", "一言", "一目", "一部", "一体",
    "一方", "一面", "一帯", "一切", "一種", "一時", "一気", "一同",
    "一族", "一行", "一心", "一瞬", "一生", "一度", "一角", "一因",
    "いろは四十八組", "二度と", "二人組", "千載一遇", "一部始終",
    # 一人ひとり→1人ひとり／一番古い→1番古い／一日中→1日中 と化けていた（2026-10-25 ユーザー指示「今後ないように」）
    # channel D側（10/25）と同じ並びにそろえる
    "一人ひとり", "一人一人", "一番", "一日中", "一年中", "一晩中", "一本道", "一回り",
    "一人前", "一点張り", "一日一日", "一歩一歩", "一段一段", "一つひとつ", "一つ一つ",
    "三日坊主", "十人十色", "一石二鳥", "一緒", "一斉", "一致", "一応", "一層",
    # 人名の数字（山本五十六→山本56 と出ていた。2026-10-29 伊400のロング）
    "山本五十六", "五十六",
)
# 長いものから照合（部分一致の取りこぼし防止）
_PROPER_NOUNS = tuple(sorted(set(_PROPER_NOUNS), key=len, reverse=True))


def _kanji_to_arabic(text: str) -> str:
    """字幕表示用に漢数字を算用数字へ。固有名詞/熟語(一緒・一条等)を壊さないよう、
    複数字の数列、または単一数字+単位/カウンタ/カタカナ(メートル等)の場合のみ変換する。
    地名等(_PROPER_NOUNS)はそのまま据え置く。"""
    # 算用数字のすぐ後ろの千・百は、先にまとめて数にする（2026-10-26）。
    # これが無いと「4千人」が 4＋1000 で「41000人」、「1万4千人」が「1万41000人」、「1千万円」が「11000万円」と壊れていた
    import re as _re
    text = _re.sub(r"(\d+)千(\d{1,3})?(?!\d)", lambda m: str(int(m.group(1)) * 1000 + int(m.group(2) or 0)), text)
    text = _re.sub(r"(\d+)百(\d{1,2})?(?!\d)", lambda m: str(int(m.group(1)) * 100 + int(m.group(2) or 0)), text)
    out = []
    i, n = 0, len(text)
    while i < n:
        # 固有名詞を最優先で照合し、丸ごと据え置く
        matched = next((w for w in _PROPER_NOUNS if text.startswith(w, i)), None)
        if matched:
            out.append(matched)
            i += len(matched)
            continue
        if text[i] in _KANJI_NUM_CHARS:
            j = i
            while j < n and text[j] in _KANJI_NUM_CHARS:
                j += 1
            run = text[i:j]
            nxt = text[j] if j < n else ""
            do = False
            if all(c in _KANJI_BIG for c in run):
                # 万/億/兆だけの並びは「数」ではなく単位語。数値化すると
                # _parse_kanji_num("万")=10000 → "1万" に展開され、算用数字の
                # 後ろで 200万→2001万 / 1万年前→11万年前 と壊れる。据え置く。
                do = False
            elif len(run) >= 2:
                do = True                                   # 三千/十五/二〇二六/千九百…
            elif nxt in _COUNTERS or _is_kata(nxt):
                do = True                                   # 五日/三人/十人/千円/三メートル
            # 「五、六歳」等の範囲表現は字幕で読点が消えるため、片側だけ数字化すると
            # 「五6歳」になる。直前が「漢数字＋読点」なら範囲とみなし据え置く。
            if do and len(run) == 1 and run in _KANJI_DIGIT and i >= 2:
                if text[i - 1] in "、,・" and text[i - 2] in _KANJI_DIGIT:
                    do = False
            # 直前が「数/何/約」等の概数を表す漢字なら、続く漢数字は具体的な数値ではない。
            # 数十年→数10年 / 数十万年→数10万年 / 数百億円→数100億円 と壊れるため据え置く。
            # （2026-08-07 ラクイラ、2026-08-08 オーパーツ・未解決事件で桁数2以上も判明）
            if do and i > 0 and text[i - 1] in "数何":
                do = False
            # 「十分」は じゅうぶん（足りている）の可能性があるので数値化しない。
            # 「十分な睡眠」→「10分な睡眠」に化ける事故が全チャンネルで出ていた（2026-08-27）。
            # 本当に10分の意味でも、数字にならないだけで表示は正しいまま。
            if do and run == "十" and nxt == "分":
                do = False
            # 直前が「数字でない漢字」なら熟語の一部（不十分/第十回 等）。単独の漢数字が対象。
            if do and len(run) == 1 and i > 0 \
                    and _is_kanji(text[i - 1]) and text[i - 1] not in _KANJI_NUM_CHARS:
                do = False
            # 単独の十/百/千/万で後ろが非カウンタ漢字(千駄ヶ谷/千葉/三重 等の固有名詞)は変換しない
            if do:
                try:
                    val = _parse_kanji_num(run)
                    # 万/億/兆を含む数は単位を漢字で残す（二十万→20万）。
                    # 千以下のみ・桁連結(年号等)はそのまま数字化（三千→3000, 二〇二六→2026）。
                    if any(c in run for c in _KANJI_BIG):
                        out.append(_format_jp_number(val))
                    else:
                        out.append(str(val))
                except Exception:
                    out.append(run)
            else:
                out.append(run)                             # 一緒/一条/一方 等は据え置き
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _is_kata(ch: str) -> bool:
    """カタカナ（長音・中黒含む）か"""
    return ch in "ー・" or ("゠" <= ch <= "ヿ")


def _is_kanji(ch: str) -> bool:
    return "一" <= ch <= "鿿" or ch in "々〆ヶ"


def _is_hira(ch: str) -> bool:
    return "ぁ" <= ch <= "ん" or ch in "ー"


# 文節末（行末に置きたい）になりやすい助詞・助動詞・接続助詞。
# ここで終わる＝直後が新しい文節の頭になりやすい。
_BUNSETSU_TAIL = set("をにはがへとのでやかもらばてりつたなだいくしすむぬる")
# 単独1文字で文節末になりやすい主要格助詞・係助詞（最も信頼度が高い）
_STRONG_PARTICLE = set("をにはがへとでや")
# 文節頭に来やすいひらがな自立語の開始字（接続詞・指示詞・副詞など）。
# これらが行頭に来るのは自然。
_HIRA_OPENERS = set("そこあどなみまよ")


def _new_phrase_start(text: str, i: int) -> bool:
    """位置iの文字が新しい文節（自立語）の頭になりやすいか。
    直前→直後の文字種遷移と助詞境界から推定する。"""
    if i <= 0 or i >= len(text):
        return False
    a, b = text[i - 1], text[i]
    # 開き括弧の前は常に文節頭
    if b in _OPENERS:
        return True
    # 「〜て／しまう」は改行してよい位置（2026-08-26 ユーザー指摘）。
    # 「反復してしまう｜ケースが」より「反復して｜しまうケースが」のほうが読みやすい。
    # ひらがなが続くだけなので文字種遷移では拾えず、ここで明示する。
    if a in "てで" and text[i:i + 2] == "しま":
        return True
    # 漢字・カタカナ語の開始は新しい自立語の頭になりやすい（送り仮名の途中は除く）
    if _is_kanji(b) or _is_kata(b):
        # 直前が漢字で直後が漢字（複合語）の場合は弱い境界
        if _is_kanji(a) and _is_kanji(b):
            return False
        # 直前がひらがな助詞 or カタカナ→漢字/漢字→カタカナ等の遷移は文節頭
        return True
    return False


def _load_no_split_phrases() -> list[str]:
    """分断禁止フレーズの一元辞書（config/no_split_phrases.json）を読む。
    事故発生時はJSONに1行追加するだけで改行エンジンと検証(subtitle_lint)の両方に効く。"""
    import json as _json
    # マスターとローカルの**両方**を読んで統合する（2026-08-29）。
    # ローカルだけを見ていたため、他チャンネルがマスターへ追加した語
    # （そのまま/このまま/ひっくり返/されてしまいます 等）が届いていなかった。
    # 辞書はchannel Aがマスターという運用なので、そちらを必ず含める。
    phrases: list[str] = []
    _master = Path(os.environ.get("NO_SPLIT_MASTER", str(Path(__file__).parent.parent / "config" / "no_split_phrases.json")))
    _local = Path(__file__).parent.parent / "config" / "no_split_phrases.json"
    for f in (_master, _local):
        if not f.exists():
            continue
        try:
            phrases += _json.loads(f.read_text(encoding="utf-8")).get("phrases", []) or []
        except Exception:
            continue
    # 長い語から先に当てたいので長さ降順（部分一致で短い語に食われるのを防ぐ）
    return sorted(dict.fromkeys(phrases), key=len, reverse=True)


_NO_SPLIT_PHRASES: list[str] = _load_no_split_phrases()


def _in_no_split_phrase(text: str, i: int) -> bool:
    """位置iでの改行が登録済み分断禁止フレーズの内部を割るか"""
    for ph in _NO_SPLIT_PHRASES:
        start = 0
        while True:
            k = text.find(ph, start)
            if k < 0:
                break
            if k < i < k + len(ph):
                return True
            start = k + 1
    return False


_MORPH_TOKENIZER = None
_MORPH_CACHE: dict = {}


def _morph_bounds(text: str):
    """text の形態素境界（オフセット集合）。janome が無ければ None。

    辞書に1語ずつ足す方式では「お|願い」「飲み|込んで」「とい|われて」を
    取りこぼし続ける。境界そのものを見るほうが確実（2026-08-29、
    channel Bで先に導入されたものを移植）。
    """
    global _MORPH_TOKENIZER
    if _MORPH_TOKENIZER is None:
        try:
            from janome.tokenizer import Tokenizer
            _MORPH_TOKENIZER = Tokenizer()
        except Exception:
            _MORPH_TOKENIZER = False
    if _MORPH_TOKENIZER is False:
        return None                     # janome 未導入なら従来動作のまま
    hit = _MORPH_CACHE.get(text)
    if hit is not None:
        return hit
    try:
        bounds, pos = {0, len(text)}, 0
        for tok in _MORPH_TOKENIZER.tokenize(text):
            pos += len(tok.surface)
            bounds.add(pos)
    except Exception:
        return None
    fs = frozenset(bounds)
    if len(_MORPH_CACHE) < 4096:
        _MORPH_CACHE[text] = fs
    return fs


# 助詞・接尾辞・連体修飾を行頭に落とさないガード（2026-08-31・channel Aから移植）。
# 都市伝説で「北陸では雪｜とともに」「アメリカワイオミング｜州です」「外側には深い｜溝が」
# 「祭りでその姿が担が｜れる」がユーザー指摘で7本続けて出た。いずれも preflight は
# NG 0件で通していた。_NO_LINE_HEAD が を/は/が の3文字しか見ておらず、
# _DANGLING_TAILS が6語のハードコードだったため。文字列ではなく品詞で判定する。
# 新旧比較用の切り替え口（在庫全カードで差分を測ってから True にすること）
_PARTICLE_GUARD = True


def _morph_tokens(text: str):
    """(表層形, 品詞リスト, 活用形) の並び。janome が無ければ None。"""
    b = _morph_bounds(text)          # トークナイザの初期化と janome 有無の判定を共用
    if b is None:
        return None
    try:
        return [(t.surface, t.part_of_speech.split(","), t.infl_form)
                for t in _MORPH_TOKENIZER.tokenize(text)]
    except Exception:
        return None


def _guard_heads(text: str) -> frozenset:
    """行頭に来ると読みにくい位置をまとめて返す。

    ① 助詞（雪｜とともに）— 文字で禁止すると「する｜ときには」の と まで
       巻き添えになるので品詞で見る
    ② カタカナ＋接尾辞（ワイオミング｜州）— 接尾辞を一律禁止にすると
       janome が「楼蘭」を 楼(接尾)+蘭 と誤解析して正しい改行まで潰れるため、
       直前がカタカナのときだけに絞る
    ③ 形容詞＋名詞（深い｜溝）と連体詞の直後（大きな｜石板）
    ④ 受身・使役の接尾動詞（担が｜れる）— janome では独立トークンなので
       _in_morph_interior では守れない
    """
    toks = _morph_tokens(text)
    if toks is None:
        return frozenset()
    out, pos, prev_adj, prev_rentai = set(), 0, False, False
    prev_verb = False
    for surf, parts, infl in toks:
        if pos > 0:
            if parts[0] == "助詞":
                out.add(pos)
            if len(parts) > 1 and parts[1] == "接尾" and _is_kata(text[pos - 1]):
                out.add(pos)
            # 動詞の直後の接尾辞（過ごし｜方、歩き｜方）も割らない。カタカナ限定だけでは
            # 「夜の過ごし｜方だった」が通っていた（2026-09-05・述語尾の結合で
            # カードが伸び、再分割がここに入った）。「その都を｜楼蘭」の楼(接尾)は
            # 直前が助詞なので巻き添えにならない。
            if _VERB_SUFFIX_GUARD and prev_verb and len(parts) > 1 and parts[1] == "接尾":
                out.add(pos)
            if len(parts) > 1 and parts[0] == "動詞" and parts[1] == "接尾":
                out.add(pos)
            if prev_adj and parts[0] == "名詞" and (len(parts) < 2 or parts[1] not in ("接尾", "非自立")):
                out.add(pos)
            if prev_rentai:
                out.add(pos)
        prev_verb = parts[0] == "動詞"
        prev_adj = parts[0] == "形容詞" and infl in ("基本形", "体言接続")
        prev_rentai = parts[0] == "連体詞"
        pos += len(surf)
    return frozenset(out)


def _in_morph_interior(text: str, p: int) -> bool:
    """位置 p が形態素の内部（＝語中）か。判定できないときは False。"""
    b = _morph_bounds(text)
    return b is not None and p not in b


def _bad_break(text: str, i: int) -> bool:
    """位置iの直前で改行すると不自然か（カタカナ語分断・行頭禁則）"""
    if i <= 0 or i >= len(text):
        return True
    # 登録済みフレーズ（死者の丘 等の固有複合語）の内部では絶対に割らない
    if _in_no_split_phrase(text, i):
        return True
    # 登録フレーズの「先頭」での改行は許可する（2026-08-24）。
    # 例: 「交通事故の方が|はるかに多いのです」は、は が行頭禁則(_NO_LINE_HEAD)に
    # 引っかかって禁止され、1行11字になってカードごと割れていた。
    # 辞書に載せた語は1つのまとまりなので、その頭で折り返すのはむしろ自然。
    # ただし「だけで」のように付属語で始まる登録語が漢字・カタカナの名詞に付いている場合は、
    # その頭で折ると「信号|だけで」と名詞から切り離されるので許可しない（2026-09-14）
    if (any(text.startswith(ph, i) for ph in _NO_SPLIT_PHRASES)
            and not (text[i] in "だでとにのはがをもかへや" and (_is_kanji(text[i - 1]) or _is_kata(text[i - 1])))
            # カタカナが続く位置では許可しない。辞書に「ニューメキシコ州」を入れた結果
            # 「アメリカ|ニューメキシコ」が作られ、リンターのカタカナ語分断に当たった（2026-09-17）
            and not (_is_kata(text[i - 1]) and _is_kata(text[i]))):
        return False
    # 形態素の内部では折らない（2026-08-29 channel Bから移植）。
    # 「イイねをお|願いします」「飲み|込んで」「開発されたとい|われて」のように、
    # 見た目の文字種だけでは防げない語中割れが在庫45件で実在した。
    # janome が無い環境では _morph_bounds が None を返し、従来動作に落ちる。
    # 2026-10-02: 0914 の「動詞のあとの『という』は行頭に置ける」例外は外した
    # （ユーザー指示「と を頭に置かない」・0824 の「という を行頭に置かない」と同じ趣旨）。
    if _in_morph_interior(text, i):
        return True
    # 助詞・接尾辞・連体修飾の行頭（2026-08-31 移植）。登録フレーズの先頭は上で
    # 許可済みなので、辞書で意図的に開けた改行位置は塞がない
    if _PARTICLE_GUARD and i in _guard_heads(text):
        return True
    a, b = text[i - 1], text[i]
    # 行末に連体詞を置き去りにしない（「同じ服同じ|道筋を」「…過程でその|姿を」）。
    # 連体詞は直後の体言と一体なので切り離すと読めない。subtitle_lint が NG にしている
    # ルールを改行エンジン側にも入れて、そもそも作らないようにする（2026-08-25）。
    # 読点で区切られている場合（「その、」）は意味境界なので許可する。
    if a != "、" and text[:i].rstrip("、 ").endswith(_DANGLING_TAILS):
        return True
    # 括弧内（引用・作品タイトル）の途中では割らない（原則5）。
    # 「学校の｜怪談』」「あの｜子はだあれ？」のような分断と閉じ括弧の孤立を防ぐ。
    # ただし括弧内でも読点の直後は意味境界なので改行を許可（「人類を、｜五億人以下に保て」）。
    if _in_bracket(text, i) and a != "、":
        return True
    # カタカナ複合語（中黒含む）の途中では割らない
    if _is_kata(a) and _is_kata(b):
        return True
    # 英単語・数字列の途中では割らない（Dream|Link のような轢殺防止）
    if a.isascii() and b.isascii() and a.isalnum() and b.isalnum():
        return True
    # 数字と単位・助数詞の間では割らない。数字だけが行末に取り残されると読めない
    # （「しかし深さ30｜メートルで」「たった1｜度だけ」「200｜万ポンド」）。
    # 2026-09-14: 助数詞の表に無い漢字（1|行）や小数点（5|.9メートル）でも割れていたので、
    # 数字の直後は漢字全部・小数点・桁区切りを一体として扱う
    if a.isascii() and a.isdigit() and (_is_kata(b) or _is_kanji(b) or b in _COUNTERS or b in ".．,，"):
        return True
    # 漢字のあとに続くカタカナの単位（立方|キロ・平方|メートル）を割らない（2026-09-14）
    if _is_kanji(a) and text.startswith(("キロ", "メートル", "センチ", "ミリ", "グラム", "リットル", "トン", "パーセント"), i):
        return True
    # 小数点の直後（7.|5）も割らない（2026-09-14）
    if a in ".．,，" and i >= 2 and text[i - 2].isascii() and text[i - 2].isdigit() and b.isascii() and b.isdigit():
        return True
    # 日付「7月|6日」を割らない（2026-09-14）
    if a == "月" and b.isascii() and b.isdigit():
        return True
    # 漢字どうしの間では割らない（二字熟語「操作」を 操｜作 と割る事故を防ぐ）。
    # 漢字複合語は1語として扱い、改行は前後のかな境界に寄せる。
    if _is_kanji(a) and _is_kanji(b):
        # 2026-09-14 に「1975年|新しいトンネル」を許可したが、subtitle_lint が
        # 漢字熟語の分断として弾くため撤回した（2026-09-17）。ゲートとの不一致のほうが害が大きい。
        return True
    # 「という」を割らない（とい｜う 禁止）。引用・伝聞の定型。
    if b == "う" and a == "い":
        return True
    # 連体詞「その/この/あの/どの」は次の語にかかる。直後で割ると
    # 「…過程でその｜姿を…」のような置き去りになる（2026-07-03 リンター発見事故）。
    if i >= 2 and text[i - 2:i] in ("その", "この", "あの", "どの"):
        return True
    # 丁寧語尾「ます/ました/ません」を割らない。
    # 「〜｜ます」（bがま）も「ありま｜せん」（aがま）も両方禁止。
    if text[i:i + 2] in ("ます", "ませ") or (b == "ま" and i + 1 < len(text) and text[i + 1] in "すせし"):
        return True
    if a == "ま" and b in "すせし":
        return True
    # 行頭禁則文字が2行目の頭に来ない
    # 「はっ/はー」で始まる語（はっきり 等）は助詞の は ではない（2026-09-14）
    if b in _NO_LINE_HEAD and not (b == "は" and i + 1 < len(text) and text[i + 1] in "っー"):
        return True
    # 「において(は)」を行頭に置かない。「脳活動｜においては」と切ると行頭が
    # 「におい(匂い)」に見えて誤読を誘う（2026-08-26 ユーザーレビュー）。
    # 折るなら「〜に｜おいては」＝直前の名詞に「に」を残す側で折る。
    if text[i:i + 4] == "において":
        return True
    # 複合助詞「には/にも/では/とは」等を行頭に置かない。「1957年｜には夢は主に」と
    # 名詞から切り離すと読めない（2026-08-26 ユーザーレビューで発覚）。_NO_LINE_HEAD の
    # は/が/を は1字目しか見ないため「に」「で」「と」始まりの複合助詞がすり抜けていた。
    # 語頭の「に」（新しい等）を巻き込まないよう、直前が漢字・カタカナ・数字のときに限る。
    if text[i:i + 2] in ("には", "にも", "では", "とは", "とも", "へは", "へも") and (
            _is_kanji(a) or _is_kata(a) or a in "0123456789０１２３４５６７８９"):
        return True
    # 連体助詞「の」を行頭に置かない。「記憶と脳｜のしくみ」と名詞から切り離すと読めない
    # （2026-08-26 サムネで発覚）。直前が漢字・カタカナのときだけ＝連体助詞と確定できる場合に限る。
    # 「〜する｜のです」「たとえば｜のちに」のように語頭の の は巻き込まない。
    if b == "の" and (_is_kanji(a) or _is_kata(a)):
        # ただし直前が長いカタカナ固有名詞のときは譲る。ここで折れないと
        # 「ゴードン・オールポートの」12字が1行のまま縮小してもはみ出す
        # （2026-08-26 この規則を入れた直後の回帰で発覚）。
        if _is_kata(a):
            j = i - 1
            while j > 0 and (_is_kata(text[j - 1]) or text[j - 1] == "・"):
                j -= 1
            if i - j >= 6:
                return False
        return True
    # 「という」を行頭に置かない＝前の語にくっつける（2026-08-24 ユーザー指摘）。
    # 「「利用可能性ヒューリスティック」|という、」のように、閉じ括弧のあとで割れて
    # 「という」だけが次行の頭に落ちるのを防ぐ。
    # 2026-09-14: 「分かってもらえた|という感覚も」のような動詞のあとは行頭に置けるほうが読みやすい。
    # 禁止は閉じ括弧の直後と、「という」だけが取り残される場合に絞る
    # 2026-10-02: 条件を外し、いつでも行頭に置かない（上の 0914 の絞り込みは撤回）
    if text[i:i + 3] == "という":
        return True
    # 補助動詞「ている/ていた/ており」等を割らない（「てい」の直後で改行しない）
    if a == "い" and i >= 2 and text[i - 2] == "て":
        return True
    # 補助動詞「しまう/しまい/しまっ/しまわ」を割らない。
    # 「鮮明に思い出されてし｜まうことが」と割れていた（2026-08-26 ユーザー指摘）。
    # 割るなら「〜て｜しまう」であって語の内側ではない。
    if a == "し" and text[i:i + 2] in ("まう", "まい", "まっ", "まわ", "まえ"):
        return True
    if b == "う" and i >= 2 and text[i - 2:i] == "しま":
        return True                        # 「しま｜う」も同じ語の内側
    # 逆に「〜て｜しまう」は文節境界なので改行してよい。ここで明示的に許可しないと
    # 末尾の「ひらがなクラスタは割らない」規則が し を付属語末とみなして塞いでしまう。
    if a in "てで" and text[i:i + 2] == "しま":
        # ただし「残して｜しまっていますか」のような3字行は作らない。
        # 早期returnは短行フィルタを素通りするので、ここで自分で見る（スキルの原則2）
        return i < _MIN_LINE_CHARS
    # 「できる/できない」を割らない（で｜き で改行しない）
    if a == "で" and b == "き":
        return True
    # 程度の補助語尾「すぎる/すぎた/すぎない」を割らない（静かす｜ぎる の轢殺を防ぐ）。
    # サムネ主見出し「宇宙は静かすぎる」で発覚（2026-07-19）。
    if a == "す" and b == "ぎ":
        return True
    # 推量・意志の語尾「でしょう/だろう/ましょう」の「う」を割らない（でしょ｜う 禁止）
    if b == "う" and a in "ょろよこそとせしだ":
        return True
    # 名詞化「のでしょう/のです」は「の」を前の語に残し「で」の前で改行する＝「いったの｜でしょうか」。
    # の の直後＋次が し/す（でしょう/です）なら、この の｜で 境界での改行を許可する。
    if a == "の" and b == "で" and i + 1 < len(text) and text[i + 1] in "しす":
        return False
    # 明確な係助詞・格助詞（は/が/を/に/へ）の直後は文節境界なので改行OK（クラスタ誤判定を防ぐ）
    # ── 2026-09-14 追加（全チャンネルの在庫55本の改行を実測して洗い出した型）──
    # 否定「ない/なく/なかっ/なけれ」を動詞の連用形から切り離さない（止まら|ない・見つから|なくなった）。
    # 「では|ない」「も|ない」のように助詞のあとは文節境界なので許す
    if b == "な" and i + 1 < len(text) and text[i + 1] in "いくかけ" \
            and _is_hira(a) and (a not in "はもでとにへ" or text[i - 2:i] == "では"):
        return True
    # 複合動詞の後項（調べ|直す・燃え|出す）を前項の送り仮名から切り離さない。
    # 直前が「漢字＋送り仮名」で、その仮名が助詞でないときに限る
    if i >= 2 and _is_kanji(text[i - 2]) and _is_hira(a) and a not in "てでのをにはがへともや" \
            and b in _COMPOUND_VERB_HEADS:
        return True
    # 連用形＋名詞の複合名詞（出し入れ|口・作り|方）も同じ扱い（2026-09-14）
    if (i >= 2 and _is_kanji(text[i - 2]) and _is_hira(a) and a not in "てでのをにはがへともや"
            and b in _COMPOUND_NOUN_TAILS):
        return True
    # 名詞に後置する語（こと|自体・人|同士・3人|以上）は前の名詞から切り離さない（2026-09-14）
    if text[i:i + 2] in _NOUN_SUFFIX_WORDS:
        return True
    # 「だ/だけ/だと」は直前の名詞にくっつく（信号|だけで・ウナギ|だという）
    if b == "だ" and (_is_kanji(a) or _is_kata(a)):
        return True
    if a in "はがをにへ":
        return False
    # 漢字の直後のひらがなが助詞以外なら、送り仮名・活用語尾（例「倒れる」の「れ」）とみなし分断しない。
    # 助詞(を/に/は/が/の/へ/も/と/で)の直後は文節境界なので改行OK。
    if _is_kanji(a) and _is_hira(b) and b not in "をにはがのへもとで":
        return True
    # 複数字の助詞・助動詞クラスタ（から/ので/ても/ては/では/ない/けない等）を割らない。
    # 直前がひらがなで直後も「文節頭になりにくいひらがな」なら語の途中とみなす。
    if _is_hira(a) and _is_hira(b) and b not in _HIRA_OPENERS \
            and (b in _BUNSETSU_TAIL or b in "らりるれろっんもはを"):
        return True
    return False


def _line_break(text: str, n: int, target: int) -> int:
    """先頭から1行分の改行位置を返す。targetに近い自然・非禁則・カタカナ非分断位置を選ぶ。
    カタカナ語＋格助詞（例「エメラルドに」）は1文節としてまとめ、その直後を優先する。"""
    length = len(text)
    # 行の長さは表示文字数（読点を除く）で数える。読点込みで数えると
    # 「降りたことのない駅に、」(10字+読点)の読点直後が上限超えと誤判定され、
    # 3字+13字のような割りに落ちていた（2026-09-14）
    def _vis(k: int) -> int:
        return len(text[:k].replace("、", ""))
    def _vis_rest(k: int) -> int:
        return len(text[k:].replace("、", ""))

    # 極端に短い行（3字以下）を作る改行位置を弾くフィルタ。
    # スコア経路には元から +50 の減点があるが、読点・カタカナ経路は早期returnで
    # そこを素通りしていたため「夢は|創造性や…」のような2字の行が出ていた（2026-08-24）。
    def _not_stubby(i: int) -> bool:
        return _vis(i) >= _MIN_LINE_CHARS and _vis_rest(i) >= _MIN_LINE_CHARS

    # 2行目が上限を超える改行位置を弾くフィルタ。スコア経路には元から +40 の減点が
    # あるが、読点・カタカナ経路は早期returnでそこを素通りしていたため
    # 「コメントと|イイねで教えてください」(11字>10)のように2行目がはみ出し、
    # 3行扱い→_split_overlong_cardsでカード分割→0.67秒の断片、という連鎖が
    # 在庫25本で発生していた（2026-08-25）。
    # 収まる位置が1つも無い長文では従来どおり優先パスを使う（下の _fit_exists）。
    def _fits_rest(i: int) -> bool:
        return _vis_rest(i) <= n

    _fit_exists = any(
        (not _bad_break(text, i)) and _vis(i) <= n and _fits_rest(i) and _not_stubby(i)
        for i in range(1, length)
    )

    def _prefer_fitting(cands: list[int]) -> list[int]:
        if not _fit_exists:
            return cands
        return [i for i in cands if _fits_rest(i)]

    # 読点「、」の直後を最優先の改行点に（台本の意味区切りを尊重）
    comma_cuts = [i for i in range(1, length) if text[i - 1] == "、" and not _bad_break(text, i)]
    within_comma = [i for i in comma_cuts if _vis(i) <= n]
    _good_comma = _prefer_fitting([i for i in within_comma if _not_stubby(i)])
    if _good_comma:
        return min(_good_comma, key=lambda i: (abs(i - target), i))
    # 読点位置がどれも短行を作るなら、読点を諦めて文節評価に委ねる
    if within_comma and length <= _MIN_LINE_CHARS * 2 + 2:
        return min(within_comma, key=lambda i: (abs(i - target), i))
    # 読点が n を超える位置にしか無い場合は、そこで強制せず通常の文節評価に委ねる
    # （横型ロングで「、」が行幅を超えた位置にあると1行目がはみ出すのを防ぐ）。

    # カタカナ語＋直後の格助詞の後ろを優先の改行点に（固有名詞の文節をまとめる）
    _KAKU = "にをがはのとへも"
    kata_cuts: list[int] = []
    for i in range(1, length):
        if _is_kata(text[i - 1]) and not _is_kata(text[i]) and text[i] in _KAKU:
            j = i
            while j < length and text[j] in _KAKU:
                j += 1
            if 0 < j < length and not _bad_break(text, j):
                kata_cuts.append(j)
    within_kata = _prefer_fitting([j for j in kata_cuts if _vis(j) <= n and _not_stubby(j)])
    if within_kata:
        return min(within_kata, key=lambda j: (abs(j - target), j))

    # スコアで最良の改行点を選ぶ。
    # 文節境界（自立語＋付属語のまとまりの切れ目）を最優先し、target近接で微調整する。
    # スコアは小さいほど良い。距離は文字数（1字≒1点）、境界品質はボーナス（減点回避）。
    best, best_score = None, None
    for i in range(1, length):
        if _bad_break(text, i) or _vis(i) > n:
            continue
        left, right = _vis(i), _vis_rest(i)
        a, b = text[i - 1], text[i]
        # 距離は弱めに効かせ、文節境界の品質を支配的にする。
        score = abs(i - target) * 0.6

        # --- 文節境界の品質ボーナス（良い境界ほど score を下げる） ---
        tail_ok = a in _BUNSETSU_TAIL          # 直前が付属語（助詞/助動詞）末
        head_ok = _new_phrase_start(text, i)   # 直後が新しい自立語の頭

        if a in _STRONG_PARTICLE and head_ok:
            score -= 7   # 格助詞末＋新文節頭：理想的な文節境界（主語＋助詞など）
        elif a in _STRONG_PARTICLE:
            score -= 4   # 主要格助詞の直後（行末助詞として自然）
        elif tail_ok and head_ok:
            score -= 5   # 付属語末＋新文節頭：自然な文節境界
        elif b in _OPENERS:
            score -= 4   # 開き括弧の前
        elif head_ok and a in _PARTICLES:
            score -= 3   # 助詞末＋自立語頭
        elif head_ok:
            score -= 1   # 文字種遷移による弱い文節頭
        else:
            score += 4   # 文節の途中（付属語内・送り仮名内など）は減点

        # 行頭が助詞・助動詞単体だと付属語が頭に来て不自然（軽い減点）
        if _is_hira(b) and b not in _HIRA_OPENERS and not head_ok:
            score += 2

        # 行の偏りは軽く減点する。4字行を許した結果「2つの言語の石板が|壊れます」(10+4)が
        # 「2つの言語の|石板が壊れます」(6+8)に勝っていた（2026-09-14）。文節ボーナスより弱い重さ
        score += abs(left - right) * 0.35
        # 漢字＋の＋漢字（頭の骨・取っ手の熱さ）は1つの名詞句。ここで折ると
        # 「残ったのは頭の|骨と背骨の一部」になる（2026-09-14・フック用の規則と同じ考え）
        if a == "の" and i >= 2 and _is_kanji(text[i - 2]) and _is_kanji(b):
            score += 6
        # 「〜て|しまう」は文節境界だが、読む側は「買えてしまいませんか」でひとまとまり。軽く減点
        if a in "てで" and text[i:i + 2] == "しま":
            score += 3
        if right > n:
            score += 40           # 残り(2行目)が上限超 → はみ出し回避（中央寄りの分割を優先）

        if left < _MIN_LINE_CHARS or right < _MIN_LINE_CHARS:
            score += 50           # 極端に短い行（3字以下）を強く回避

        if best_score is None or score < best_score:
            best, best_score = i, score
    if best is not None:
        return best
    # n以内に候補なし（長いカタカナ語など）→ 非禁則の最小位置
    for i in range(1, length):
        if not _bad_break(text, i):
            return i
    return min(n, length - 1)


# 行の途中で読点を消すと名詞どうしがくっついて別の語に読める所は、読点を画面に残す（2026-09-24）。
#   人、鳥、魚、植物、道具 → 「人鳥魚植物道具」／ 4、7、赤、青 → 「47赤青」／ 19日後、客が → 「19日後客が」
# 固有名詞の列挙（_name_like）は以前から読点を残していた。それを名詞どうし全般に広げる。
# ひらがなの後ろ（参加者に、三つの）は消しても誤読しないので消す。新旧を比べるための切り替え口。
_KEEP_GLUE_COMMA = True


def _noun_ch(ch: str) -> bool:
    return _is_kanji(ch) or (_is_kata(ch) and ch != "・") or ch.isdigit()


def _strip_display_commas(line: str) -> str:
    """表示用に読点を落とす。ただし名詞｜名詞をつなぐ読点は残す。"""
    if not _KEEP_GLUE_COMMA:
        return line.replace("、", "")
    out = []
    for i, ch in enumerate(line):
        if ch == "、":
            a = line[i - 1] if i > 0 else ""
            b = line[i + 1] if i + 1 < len(line) else ""
            if a and b and _noun_ch(a) and _noun_ch(b):
                out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


def _wrap_chars(text: str, n: int, _relief: bool = False) -> list[str]:
    """テキストを最大SUB_MAX_LINES行に改行。開き括弧前・助詞後を優先し、
    カタカナ語・送り仮名・補助動詞の途中では割らず、各行をバランスよく分ける。"""
    import math
    text = text.rstrip("、")
    # 固有名詞の列挙（人名・地名など）は読点を区切りとして残し、視覚的に分ける（原則4例外）。
    # 例「清水崇、黒沢清、中田秀夫」→「清水崇、黒沢清 / 中田秀夫」。連結して読みにくくしない。
    eparts = [p for p in text.split("、") if p]
    if len(eparts) >= 2 and all(_name_like(p) for p in eparts):
        if len(text) <= n:
            return [text]                        # 読点込みで1行に収まる（区切りを残す）
        best_k, best_diff = 1, 10 ** 9
        for k in range(1, len(eparts)):
            diff = abs(len("、".join(eparts[:k])) - len("、".join(eparts[k:])))
            if diff < best_diff:
                best_k, best_diff = k, diff
        return ["、".join(eparts[:best_k]), "、".join(eparts[best_k:])]
    # 表示文字数（読点を除く）が n 以下なら必ず1行（10字以内は1行・ユーザー指定）。
    # ただし読点を落とすと「黒は危険緑は天然材料」のように漢字（カタカナ・数字）が節をまたいで
    # 連結して別の語に見えるときは、短くても読点で2行にする（2026-09-14）
    def _glue_bad(x: str, y: str) -> bool:
        if not x or not y:
            return False
        p, q = x[-1], y[0]
        return ((_is_kanji(p) and _is_kanji(q)) or (_is_kata(p) and _is_kata(q))
                or (p.isdigit() and q.isdigit()))
    _glued = any(_glue_bad(eparts[j], eparts[j + 1]) for j in range(len(eparts) - 1))
    _disp1 = _strip_display_commas(text)
    if _KEEP_GLUE_COMMA:
        # 読点を画面に残すので、くっつき回避の2行化は要らない。読点込みの幅で1行かを決める（2026-09-24）
        if len(_disp1) <= n:
            return [_disp1]
    elif len(text.replace("、", "")) <= n and not _glued:
        return [_disp1]
    length = len(text)
    # 割れる位置が全く無い（長いカタカナ固有名詞・中黒複合語など）→ 1行のまま。
    # 語の途中で無理に割る（例「ジョージア・ガイドス｜トーン」）より、描画側の
    # オートフィット縮小で1行に収めるほうが読みやすい（原則5）。
    if not any(not _bad_break(text, i) for i in range(1, length)):
        return [_strip_display_commas(text)]

    # 必要行数（n字/行で収まる最小行数。上限SUB_MAX_LINES=2）。
    # 2行に収まらない長文は _split_overlong_cards でカードを分割（次/前のテロップへ回す）ため、
    # ここでは3行に増やさない。
    nlines = min(SUB_MAX_LINES, max(2, math.ceil(length / n)))

    lines: list[str] = []
    rest = text
    for k in range(nlines - 1):
        remaining_lines = nlines - k
        target = math.ceil(len(rest) / remaining_lines)   # 残りを均等割り
        b = _line_break(rest, n, target)
        lines.append(rest[:b])
        rest = rest[b:]
    lines.append(rest)

    # 極端に短い行（3字以下）を隣接行へ寄せてバランス改善。
    # ただし結合で行長が n を超える場合は結合しない（はみ出し防止。短い行を許容）。
    def _dl(s: str) -> int:
        return len(s.replace("、", ""))
    k = 0
    while len(lines) > 1 and k < len(lines):
        if _dl(lines[k]) <= 3:
            if k > 0 and _dl(lines[k - 1]) + _dl(lines[k]) <= n:
                lines[k - 1] += lines.pop(k)
                continue
            elif k == 0 and _dl(lines[0]) + _dl(lines[1]) <= n:
                lines[1] = lines[0] + lines[1]
                lines.pop(0)
                continue
        k += 1
    # 折り位置が乏しくて偏るとき、2字だけ上限を緩めて折り直す。
    #  ・「よく｜見えてくるためだとされます」(2字+13字) → 11字+4字
    #  ・「実力ではなく｜偶然のせいにしてしまう状態です」(6字+15字) → 12字+9字
    #    後者は有効な折り位置(12字目)が上限10字を超えるために落ちていた
    #    （2026-09-06 ユーザー指摘「実力ではなく偶然のせいに/してしまう状態です」）。
    # 緩めても改善しないときは採用しない。
    if _WRAP_OVERFLOW_RELIEF and _relief and len(lines) > 1:
        _stub = any(_dl(ln) < _MIN_LINE_CHARS for ln in lines)
        _over = max(_dl(ln) for ln in lines) > n + 2
        if _stub or _over:
            alt = _wrap_chars(text, n + 2, _relief=False)
            _ok = (len(alt) <= len(lines)
                   and not any(_dl(ln) < _MIN_LINE_CHARS for ln in alt))
            if _over:
                # 長すぎる行を縮めるのが目的。短くならないなら採らない
                _ok = _ok and max(_dl(ln) for ln in alt) < max(_dl(ln) for ln in lines)
            else:
                # 短行を消すのが目的。はみ出しは n+2 までに収める
                _ok = _ok and max(_dl(ln) for ln in alt) <= n + 2
            if _ok:
                lines = alt
    # 改行ヒントに使った読点を表示から除去（名詞｜名詞をつなぐ読点だけは残す・2026-09-24）
    return [_strip_display_commas(ln) for ln in lines]


def _render_subtitle_png(text: str, out_path: str) -> tuple[int, int]:
    """字幕をフルフレーム(W x H)のRGBA PNGで生成。
    透明背景+下部に字幕バー。overlay=(0,0)で常に固定位置。白文字・白縁・黒背景。"""
    from PIL import Image, ImageDraw, ImageFont

    pad_x, pad_y = 44, 28   # 左右余白を少し詰めて10文字を1行に収める
    font_size = int(SUB_FONT_SIZE * 1.3)
    font = _sub_font(font_size)
    lines = _wrap_chars(text, _scaled_line_n(), _relief=True)   # 表示だけ短行を救済
    if not lines:
        lines = [""]

    # 通常は縮小しない（10字以下1行・長文は2行/カード分割で収める方針）。
    # ただし安全装置: それでも行が画面幅を超える場合だけ最小限フォントを縮小して
    # 「はみ出し」を必ず防ぐ（です結合等で稀に上限超のカードができても見切れない）。
    limit_w = W - 20 - pad_x * 2
    max_line_w = max((font.getbbox(ln)[2] for ln in lines if ln), default=1)
    if max_line_w > limit_w:
        font_size = max(int(font_size * limit_w / max_line_w), 40)
        font = _sub_font(font_size)

    line_h = int(font_size * 1.5)
    text_w = max((font.getbbox(ln)[2] for ln in lines if ln), default=font_size)
    band_w = min(W - 20, text_w + pad_x * 2)
    band_h = line_h * len(lines) + pad_y * 2

    # フルフレームサイズで透明キャンバス
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 字幕バーY座標（画面下から上へ）
    bar_y = H - band_h - SUB_BOTTOM_MARGIN
    band_x0 = (W - band_w) // 2
    draw.rounded_rectangle(
        [band_x0, bar_y, band_x0 + band_w, bar_y + band_h],
        radius=16, fill=(0, 0, 0, SUB_BAND_ALPHA)
    )
    y = bar_y + pad_y
    for ln in lines:
        tw = font.getbbox(ln)[2] if ln else 0
        x = (W - tw) // 2
        # 黒縁＋白文字
        draw.text((x, y), ln, font=font, fill=(255, 255, 255),
                  stroke_width=8, stroke_fill=(0, 0, 0))
        y += line_h

    img.save(out_path, "PNG")
    return 0, 0  # overlay位置は常に(0,0)


def _phrase_timings_from_mora(mora_times) -> list[tuple[float, float]]:
    """mora_timesのポーズを区切りにフレーズタイミングリストを返す"""
    if not mora_times:
        return []
    phrases: list[tuple[float, float]] = []
    phrase_start = mora_times[0].start
    last_end = mora_times[0].end
    for m in mora_times:
        if m.text == "　":
            if last_end > phrase_start:
                phrases.append((phrase_start, last_end))
            phrase_start = m.end
        else:
            last_end = m.end
    if last_end > phrase_start:
        phrases.append((phrase_start, last_end))
    return [p for p in phrases if p[1] > p[0] + 0.05]


_PRED_TAIL_RE = re.compile(
    r"^(の|ん)?(です|ます|でした|ました|でしょう|ましょう|ません|ですか|ますか|"
    r"である|だった|なのだ|のだ|なのです|ですね|ますね)$"
)


# 述語尾の判定を品詞で行うかどうか（新旧比較用スイッチ）。
# 文字列の完全一致リスト(_PRED_TAIL_RE)だけでは「されます」「あります」のように
# 動詞が付いた形が漏れ、4文字だけのカードが残っていた
# （2026-09-05 ユーザー指摘「覚えたことの動画、されますが短文」）。
_WRAP_OVERFLOW_RELIEF = True  # 短行救済のために1行2字までのはみ出しを許す（新旧比較用）
_PRED_TAIL_MORPH = True
_VERB_SUFFIX_GUARD = True   # 動詞＋接尾（過ごし|方）を割らない（新旧比較用スイッチ）
_PRED_TAIL_MAX_CHARS = 6      # これより長いカードは単独でも読めるので吸収しない
_PRED_TAIL_FREE_POS = ("名詞", "副詞", "形容詞", "連体詞", "接続詞", "感動詞")


def _is_pred_tail(s: str) -> bool:
    """カードが「です/ます」等の文末述語だけかどうか（単独テロップにしたくない）。"""
    return bool(_PRED_TAIL_RE.match(_remove_punctuation(s).strip()))


def _is_pred_tail_morph(s: str) -> bool:
    """自立語を含まず助動詞で終わる短いカード（されます/あります/考えられます等）。

    「ほどの詰め込みかもしれません」は名詞「詰め込み」を含むので対象外、
    「整えられていきます」は9字あるので対象外。どちらも単独カードとして成立する。
    """
    if not _PRED_TAIL_MORPH:
        return False
    t = _remove_punctuation(s).strip()
    if not t or len(t) > _PRED_TAIL_MAX_CHARS:
        return False
    toks = _morph_tokens(t)
    if not toks or toks[-1][1][0] != "助動詞":
        return False
    for _surf, parts, _infl in toks:
        if parts[0] in _PRED_TAIL_FREE_POS:
            # 「なるのです」の「の」(名詞,非自立)や接尾辞は自立語とみなさない
            if not (parts[0] == "名詞" and len(parts) > 1
                    and parts[1] in ("非自立", "接尾")):
                return False
    return True


def _merge_short_cards(cards, ends=None, min_dur: float = 1.6,
                       max_chars: int = SUB_MAX_CHARS):
    """表示時間が短すぎるカードを隣接カードと結合し、読み切れる長さを確保する。
    ends[i]=True（そのカードが文末＝句点で終わる）の場合は、その境界を越えて結合しない。
    「です/ます」だけのカードは時間に関わらず必ず前カードへ結合する。"""
    if not cards:
        return cards
    merged = list(cards)
    e = list(ends) if ends else [False] * len(cards)
    # 文末述語だけのカードを前カードへ吸収（単独「です」テロップを防ぐ）。
    # 結合時は前カード末尾の句読点を除去する（「〜にもかかわらず、です」→「〜にもかかわらずです」）。
    i = 1
    while i < len(merged):
        # 品詞判定側は「文の終わりで、かつ直前カードが文末でない」ときだけ吸収する。
        # 文中の「眠れない」(→ほどの詰め込み…に係る)を前へ繋いでしまわないため。
        _morph_hit = (_is_pred_tail_morph(merged[i][0])
                      and (e[i] or i == len(merged) - 1) and not e[i - 1])
        if _is_pred_tail(merged[i][0]) or _morph_hit:
            pd, pts, _pte = merged[i - 1]
            merged[i - 1] = (pd.rstrip("、。！？ ") + merged[i][0], pts, merged[i][2])
            e[i - 1] = e[i]
            del merged[i]; del e[i]
            continue
        i += 1
    i = 0
    while i < len(merged):
        disp, ts, te = merged[i]
        if te - ts < min_dur:
            # 極端に短いカード（1秒未満 or 4字以下）は、max_chars を超えてでも結合する。
            # このあと _split_overlong_cards が2行に収まる形へ割り直すので、結果として
            # 「脳は、|その状況を…」→「脳はその状況を|対処すべき課題だと考え」のように
            # 読める長さへ再配分される。上限で結合を拒むと0.45秒のカードが残っていた
            # （2026-08-24 ユーザー指摘「短すぎて読めない」）。
            _tiny = (te - ts) < 1.0 or len(_remove_punctuation(disp).strip()) <= 4
            _cap = max_chars * 2 if _tiny else max_chars
            # 後ろと結合（現カードが文末でない場合のみ）
            if i + 1 < len(merged) and not e[i] and len(disp) + len(merged[i + 1][0]) <= _cap:
                nd, nts, nte = merged[i + 1]
                merged[i] = (disp + nd, ts, nte)
                e[i] = e[i + 1]
                del merged[i + 1]; del e[i + 1]
                continue
            # 前と結合（前カードが文末でない場合のみ）
            if i > 0 and not e[i - 1] and len(merged[i - 1][0]) + len(disp) <= _cap:
                pd, pts, pte = merged[i - 1]
                merged[i - 1] = (pd + disp, pts, te)
                e[i - 1] = e[i]
                del merged[i]; del e[i]
                continue
        i += 1
    return merged


def _split_overlong_cards(cards, line_n: int):
    """2行(SUB_MAX_LINES)に収まらないカードを、自然な境界で複数カードに分割する。
    3行表示やフォント縮小を避け、「3行使うくらいなら次/前のテロップに回す」方針。
    分割後の各カードの表示時間は文字数比で按分する。"""
    out: list[tuple[str, float, float]] = []
    for disp, ts, te in cards:
        out.extend(_split_card_fit(disp, ts, te, line_n))
    return out


def _is_unreadable_card(card) -> bool:
    """preflightの「読めない速さ」と同じ基準（0.7秒未満 or 12字/秒超）でカードを判定する。"""
    disp, ts, te = card
    sec = te - ts
    if sec <= 0:
        return True
    return sec < 0.7 or len(_remove_punctuation(disp)) / sec > 12


_TO_HEAD_CARD_SPLIT = True   # 2026-10-02 第3段: 行頭の と 系を避けられないカードは読点で2枚に割る


def _line_head_to_particle(lines) -> bool:
    """2行目以降の行頭が助詞の と（と／という／として／とも 等）か。
    行をつないだ表示文を janome で解析し、行頭から始まる語の品詞で見る（語頭の と＝とても 等は対象外）。"""
    text, heads = "", []
    for k, ln in enumerate(lines):
        if k:
            heads.append(len(text))
        text += _remove_punctuation(ln)
    if not heads:
        return False
    toks = _morph_tokens(text)
    if not toks:
        return False
    pos, starts = 0, {}
    for surf, tags, _infl in toks:
        starts[pos] = (surf, tags)
        pos += len(surf)
    for h in heads:
        t = starts.get(h)
        if t and t[1][0] == "助詞" and t[0].startswith("と"):
            return True
    return False


def _to_head_comma_split(disp: str, ts: float, te: float, line_n: int):
    """2行に収まるが行頭が と 系になるカード（「草も木も石も1つ/として持ち出せない」）を、
    中の読点で2枚に割る。割れない・割ると短すぎるときは None（2026-10-02 第3段）。"""
    if not _TO_HEAD_CARD_SPLIT or "、" not in disp:
        return None
    if not _line_head_to_particle(_wrap_chars(disp, line_n, _relief=True)):   # 描画と同じ折りで見る
        return None
    _gh = _guard_heads(disp)
    cands = []
    for m in re.finditer("、", disp):
        c = m.end()
        if not (0 < c < len(disp)) or c in _gh:
            continue
        ok = True
        for half in (disp[:c], disp[c:]):
            if not _remove_punctuation(half).strip():
                ok = False
                break
            hl = _wrap_chars(half, line_n, _relief=True)
            if (len(hl) > SUB_MAX_LINES or any(len(_remove_punctuation(x)) > line_n for x in hl)
                    or _line_head_to_particle(hl)):
                ok = False
                break
        if ok:
            cands.append(c)
    if not cands:
        return None
    c = min(cands, key=lambda q: abs(q - len(disp) // 2))
    lp, rp = len(_remove_punctuation(disp[:c])), len(_remove_punctuation(disp[c:]))
    tm = ts + (te - ts) * lp / (lp + rp)
    if tm - ts < _MIN_CARD_SEC or te - tm < _MIN_CARD_SEC:      # 短すぎるなら割らない
        return None
    return [(disp[:c], ts, tm), (disp[c:], tm, te)]


def _split_card_fit(disp: str, ts: float, te: float, line_n: int):
    """1カードが2行×line_nに収まるか判定し、収まらなければ中央付近の自然な境界で
    2分割して再帰的に処理する。"""
    plain = _remove_punctuation(disp)
    lines = _wrap_chars(disp, line_n)
    fits = len(plain) <= line_n or all(len(_remove_punctuation(l)) <= line_n for l in lines)
    if fits:
        _th = _to_head_comma_split(disp, ts, te, line_n)    # 行頭の と 系を避けられないなら読点で割る（2026-10-02 第3段）
        return _th if _th else [(disp, ts, te)]
    # 収まらない原因が「それ自体を割れない長い行」（長いカタカナ人名など）なら、
    # カードを分けても解決しない。分けると「心理学者の」(0.8秒)だけの一瞬のカードが
    # できてしまうので、1カードのままオートフィット縮小に任せる（2026-08-26 ユーザー指摘）。
    _over = [l for l in lines if len(_remove_punctuation(l)) > line_n]
    if _over and all(all(_bad_break(l, i) for i in range(1, len(l))) for l in _over):
        return [(disp, ts, te)]
    # 有効な改行位置（禁則でない）が無ければ、これ以上割れない（長い固有名詞・括弧内引用など）。
    # 1文字カードへの崩壊を防ぐため1カードのまま返し、描画側オートフィットで縮小する（原則5）。
    if not any(not _bad_break(disp, i) for i in range(1, len(disp))):
        return [(disp, ts, te)]
    # 2行に収まらない → まず読点で割る。仕様書の「読点が最優先の改行点」は
    # 行内改行にしか効いておらず、カード分割は中央付近を見ていた。そのため
    # 「あなたは最近、何を頭に残して…」が「あなたは最近何を頭に｜残して…」と
    # 意味の切れ目を無視して割れていた（2026-08-26 ユーザー指摘）。
    # 割った両側がそれぞれ2行に収まる読点だけを候補にする。
    _cm = [m.end() for m in re.finditer("、", disp)]
    _gh = _guard_heads(disp)           # 読点の直後が助詞（「、と考え…」）ならそこでは割らない（2026-10-02）
    _ok = [c for c in _cm
           if 0 < c < len(disp) and c not in _gh   # 「、という」でも割らない（2026-10-02 第2段）
           and len(_remove_punctuation(disp[:c])) <= line_n * SUB_MAX_LINES
           and len(_remove_punctuation(disp[c:])) <= line_n * SUB_MAX_LINES
           # 短い主題（「1957年には、」等）だけのカードを作る読点では割らない。
           # _split_subtitle の主題結合をここで巻き戻さないため（2026-08-26）
           and not (len(_remove_punctuation(disp[:c]).strip()) <= 8
                    and _remove_punctuation(disp[:c]).strip().endswith("は"))]
    if _ok:
        b = min(_ok, key=lambda c: abs(c - len(disp) // 2))
    else:
        b = _line_break(disp, len(disp) - 1, len(disp) // 2)
        if not (0 < b < len(disp)):
            b = len(disp) // 2
        # 中央付近で割ると「処理しようとする傾向が｜あると…」のように3字行を作ることがある
        # （_merge_short_cards が上限を超えて結合したカードで起きる。2026-08-26 ユーザー指摘）。
        # 3字行を作らない位置が近くにあればそちらを採る。

        def _stub_free(q: int) -> bool:
            for half in (disp[:q], disp[q:]):
                for line in _wrap_chars(half, line_n):
                    if 0 < len(_remove_punctuation(line)) < _MIN_LINE_CHARS:
                        return False
            return True

        if not _stub_free(b):
            mid = len(disp) // 2
            alt = next((q for off in range(1, len(disp))
                        for q in (mid - off, mid + off)
                        if 0 < q < len(disp) and not _bad_break(disp, q) and _stub_free(q)), None)
            if alt:
                b = alt
    left, right = disp[:b], disp[b:]
    lp, rp = len(_remove_punctuation(left)), len(_remove_punctuation(right))
    if lp == 0 or rp == 0:          # 分割が進展しない場合の保険（無限再帰防止）
        return [(disp, ts, te)]
    tm = ts + (te - ts) * lp / (lp + rp)
    # 文字数比のままだと片側が読み切れない短さになることがある。
    # 「石は、|100個以上ありました」で2字0.25秒、「…だったと|されています」で0.69秒が
    # 出ていた（2026-08-25）。2行に収まる改行位置が無く分割が避けられないケースなので、
    # 配分側で最低表示時間を確保する（長いほうから借りる）。
    _span = te - ts
    _min_side = min(_MIN_CARD_SEC, _span / 2)
    if tm - ts < _min_side:
        tm = ts + _min_side
    elif te - tm < _min_side:
        tm = te - _min_side
    out = _split_card_fit(left, ts, tm, line_n) + _split_card_fit(right, tm, te, line_n)
    # 分割しても読み切れないカード（0.7秒未満 or 12字/秒超）が残るなら、割らないほうが読める。
    # はみ出しが1行あたり2字までの軽微なケースに限り1カードのまま返し、
    # 描画側のオートフィット縮小に任せる（原則5。2026-08-25）。
    # 述語尾だけのカード（されます/ありません等）を作る分割も、同じ理由で見送る。
    # _merge_short_cards が「繰り返し再生していると、されます」へ正しく結合しても、
    # ここが読点で割り直して4字の「されます」を復活させていた
    # （2026-09-05 ユーザー指摘「覚えたことの動画、されますが短文」）。
    _tail_out = any(_is_pred_tail(c[0]) or _is_pred_tail_morph(c[0]) for c in out)
    # 「メキシコの|オルメカ文明の遺物です」のように5字以下のカードが一瞬映るくらいなら、
    # 2字はみ出しの縮小で1枚に収める（2026-09-14）
    _short_out = any(len(_remove_punctuation(c[0]).strip()) <= 5 for c in out)
    if max((len(_remove_punctuation(l)) for l in _wrap_chars(disp, line_n, _relief=True)), default=0) <= line_n + 2 \
            and (any(_is_unreadable_card(c) for c in out) or _tail_out or _short_out):
        if _tail_out:
            # 述語尾を同じカードに残すときは、その直前の読点をテロップから外す。
            # 「繰り返し再生していると、されます」の読点は割るための切れ目であって、
            # 1枚に収めた時点で不要（2026-09-05 ユーザー指摘「句点いらない」）。
            # 表示文字列だけの変更で、音声(synthesis_text)は動かさない。
            _d = ""
            for _cd, _a, _b in out:
                if _d and (_is_pred_tail(_cd) or _is_pred_tail_morph(_cd)):
                    _d = _d.rstrip("、 ")
                _d += _cd
            return [(_d, ts, te)]
        return [(disp, ts, te)]
    return out


def _reading_len(text: str) -> int:
    """narration の断片が何モーラぶんかを、janome の読みで数える。

    2026-08-26: 字幕タイミングは「narrationの文字数比 → moraインデックス」で
    当たりを付けていたが、漢字は1文字が複数モーラになるため系統的にずれる
    （実測で最大1.0秒。ユーザー指摘「読みとテロップのタイミングあってない」）。
    文字数ではなく読みの長さで測れば、この圧縮ぶんを正しく吸収できる。
    janome が使えない場合は文字数にフォールバックする（従来動作）。
    """
    if not text:
        return 0
    try:
        from src.particle_wa import _get_tokenizer, _k2h
        n = 0
        for t in _get_tokenizer().tokenize(text):
            if t.part_of_speech.split(",")[0] == "記号":
                continue
            r = t.reading if (t.reading and t.reading != "*") else t.surface
            n += len(_k2h(r))
        return n or len(text)
    except Exception:
        return len(text)


_BALANCED_PACK = True   # カード長を均等に配る（新旧比較用スイッチ）


def _attach_leading_particles(parts: list[str]) -> list[str]:
    """読点で切った断片の頭が助詞の「と／という／として／とも／とは」なら、前の断片の末尾へ移す
    （2026-10-02 ユーザー指示「AとBの と が次のテロップの頭に来て読みにくい。と は前のテロップの終わりに付ける」）。
    ・読点の分割は意味の切れ目として _bad_break を通らないので、ここで直す
    ・品詞は**前の断片とつないだ文で**見る。断片だけで解析すると「と範囲を切ります」の文頭の と が
      「フィラー」と判定されて動かなかった（channel D 083127・2026-10-02）
    ・「という」も前へ（「〜人だという、｜証拠」）。0914 の「という は行頭に置ける」例外は外した
    ・「とても」「とき」のような語頭の と は品詞が助詞でないので動かさない
    ・読点は移した助詞の後ろに残す（音声の間の位置はそのまま）"""
    out = list(parts)
    for j in range(1, len(out)):
        p = out[j]
        if not p.startswith("と") or not out[j - 1].rstrip(" ").endswith("、"):
            continue
        prev = out[j - 1].rstrip("、 ")
        toks = _morph_tokens(prev + p)
        if not toks:
            continue
        b, pos, lead = len(prev), 0, None
        for k, (surf, _tags, _infl) in enumerate(toks):
            if pos == b:
                lead = ""
                for surf2, tags2, _i2 in toks[k:]:
                    if tags2[0] != "助詞":
                        break
                    lead += surf2
                break
            if pos > b:
                break                  # 境目が語の途中＝前とつながって別の語になった。動かさない
            pos += len(surf)
        if lead is None or not lead.startswith("と"):
            continue
        if lead == "と" and p.startswith("という"):
            lead = "という"            # janome が と＋いう(動詞) に分けても「という」ごと移す
        if not p.startswith(lead) or len(lead) >= len(_remove_punctuation(p)):
            continue
        out[j - 1] = prev + lead + "、"
        out[j] = p[len(lead):]
    return out


def _balance_pack(parts: list[str], max_chars: int) -> list[str] | None:
    """読点で切った断片を、カード長ができるだけ揃うように配り直す。

    貪欲に上限まで詰めると余りが最後に寄り、「16字/7字/15字」のように
    真ん中だけ1秒未満で消えるカードができる
    （2026-09-06 ユーザー指摘「意志力の動画 字幕の切り替わりのテンポが悪い」）。
    グループ数は最小のまま、各カードの長さの二乗偏差が最小になる区切りを選ぶ。
    断片は順序を変えない（音声と対応が崩れるため）。
    """
    n = len(parts)
    if n <= 1:
        return None
    d = [len(_remove_punctuation(p)) for p in parts]
    pre = [0]
    for x in d:
        pre.append(pre[-1] + x)
    INF = float("inf")
    for k in range(1, n + 1):
        target = pre[n] / k
        dp = [[INF] * (k + 1) for _ in range(n + 1)]
        back = [[0] * (k + 1) for _ in range(n + 1)]
        dp[0][0] = 0.0
        for i in range(1, n + 1):
            for g in range(1, min(k, i) + 1):
                for j in range(g - 1, i):
                    if dp[j][g - 1] == INF:
                        continue
                    w = pre[i] - pre[j]
                    # 上限超えは「断片1個だけ」のときに限り許す（後段の
                    # _force_split が割る）。2個以上を上限超えで束ねない。
                    if w > max_chars and i - j > 1:
                        continue
                    c = dp[j][g - 1] + (w - target) ** 2
                    if c < dp[i][g]:
                        dp[i][g] = c
                        back[i][g] = j
        if dp[n][k] < INF:
            out, i, g = [], n, k
            while g > 0:
                j = back[i][g]
                out.append("".join(parts[j:i]))
                i, g = j, g - 1
            return out[::-1]
    return None


def _align_chunk_starts(chunks: list[str], speech_moras) -> list:
    """各チャンクの頭の文字が、発話モーラ列のどこで読まれ始めるかを返す（対応がつかなければ None）。
    表示文を janome で読み（数字は reading_guard._num2kana で読みにする）、モーラ列と difflib で突き合わせる。
    頭の文字が対応しないときは、後ろ3字までを見て、その手前のモーラ数を引いて推定する。"""
    import difflib
    try:
        from src.particle_wa import _get_tokenizer
        from src.reading_guard import _num2kana
    except Exception:
        return [None] * len(chunks)

    def kata(s):
        return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)

    def norm(s):
        out = []
        for c in kata(s):
            if c == "ー" and out:   # 長音は直前の母音のゆれとして同じ字に寄せる
                c = out[-1]
            out.append(c)
        return "".join(out)

    rd, pos = "", []   # pos[k] = 表示文 k 文字目の読みの開始位置（記号は None）
    text = "".join(chunks)
    for t in _get_tokenizer().tokenize(text):
        sf = t.surface
        if t.part_of_speech.split(",")[0] == "記号" and not re.fullmatch(r"[0-9０-９]+", sf):
            pos += [None] * len(sf)
            continue
        if re.fullmatch(r"[0-9０-９]+", sf):
            v = int(sf.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
            k = _num2kana(v) if v < 100000 else []
            r = kata(k[0]) if k else ""
        elif t.part_of_speech.startswith("助詞") and sf in ("は", "へ", "を"):
            r = {"は": "ワ", "へ": "エ", "を": "オ"}[sf]   # janome はハ/ヘ/ヲと読むが、声はワ/エ/オ
        else:
            r = t.reading if (t.reading and t.reading != "*") else kata(sf)
        r = norm(r)
        st = len(rd)
        pos += [st + len(r) * j // max(1, len(sf)) for j in range(len(sf))]
        rd += r
    ms, owner = "", []
    for mi, m in enumerate(speech_moras):
        for ch in m.text:
            ms += ch; owner.append(mi)
    ms = norm(ms)
    amap = {}
    for a, b, n in difflib.SequenceMatcher(None, rd, ms, autojunk=False).get_matching_blocks():
        for k in range(n):
            amap[a + k] = b + k
    out, acc = [], 0
    for c in chunks:
        got = None
        for ci in range(acc, min(acc + 3, len(text))):
            p = pos[ci] if ci < len(pos) else None
            if p is not None and p in amap:
                back = sum(1 for x in range(acc, ci) if pos[x] is not None)
                got = max(0, owner[amap[p]] - back) if back else owner[amap[p]]
                break
        out.append(got)
        acc += len(c)
    return out


def _realign_cards(cards, speech_moras, min_dur: float = 0.7):
    """カードの境目を、次のカードの頭の言葉が読まれ始める時刻へ寄せる（ロング用・2026-10-29）。
    前後のカードが min_dur 秒より短くなる動かし方はしない。"""
    try:
        starts = _align_chunk_starts([c[0] for c in cards], speech_moras)
    except Exception:
        return cards
    out = [list(c) for c in cards]
    for k in range(1, len(out)):
        a = starts[k] if k < len(starts) else None
        if a is None:
            continue
        t = speech_moras[a].start
        if out[k - 1][1] + min_dur <= t <= out[k][2] - min_dur:
            out[k - 1][2] = t
            out[k][1] = t
    return [tuple(c) for c in out]


def _subtitle_timings(text: str, duration: float, mora_times=None) -> list[tuple[str, float, float]]:
    """台本の文章を自然な句で分割し、mora実測比率でタイミングを割り当てる。
    テキスト文字数 → 発話moraのインデックス に比例マッピングすることでズレを最小化。"""
    import re
    text = _sanitize_subtitle(text.replace("\n", "").strip())
    text = _kanji_to_arabic(text)   # 漢数字→算用数字（表示用・読みやすさ。音声はnarration_kana管理で不変）
    text = _thin_sub_commas(text)   # 過剰な読点を間引く（短行縮小・細切れカード対策）

    # 句点・感嘆符で分割（文単位でカード化。読点はカード内の改行ヒントに使う）
    raw_chunks = _split_sentences(text)
    if not raw_chunks:
        raw_chunks = [text]

    # 各文を1カードに。SUB_MAX_CHARSを超える長文は読点で貪欲にパッキングして分割
    def _dlen(s: str) -> int:
        return len(_remove_punctuation(s))

    max_chars = _scaled_max_chars()   # ショート=16 / ロング=36（W/1080スケール）
    chunks: list[str] = []
    for sent in raw_chunks:
        if _dlen(sent) <= max_chars:
            chunks.append(sent)
            continue
        parts = [p for p in re.split(r"(?<=、)", sent) if p]
        parts = _attach_leading_particles(parts)   # 「、と考え…」の と を前の断片へ（2026-10-02）
        # 短い主題（「1957年には、」「あなたは、」等）は次の句とつなげてから配る。
        # 単独カードにすると主題だけが一瞬映る（2026-08-26 ユーザー指摘
        # 「1957年にはを後ろのテロップに統合」）。読点＝音声側は動かさない。
        _merged = []
        for p in parts:
            if _merged:
                _b = _remove_punctuation(_merged[-1]).strip()
                if (_b.endswith("は") and _dlen(_merged[-1]) <= 8
                        and _dlen(_merged[-1] + p) <= max_chars + 4):
                    _merged[-1] += p
                    continue
            _merged.append(p)
        packed = _balance_pack(_merged, max_chars) if _BALANCED_PACK else None
        if packed is not None:
            chunks.extend(packed)
        else:
            buf = ""
            for p in _merged:
                if buf and _dlen(buf + p) > max_chars:
                    chunks.append(buf)
                    buf = p
                else:
                    buf += p
            if buf:
                chunks.append(buf)

    # SUB_MAX_CHARSを超えるチャンクは強制分割（3行溢れ防止）。
    # 長カタカナ語を含む文も助詞境界で割り、各カードが上限に収まるまで再帰的に分割する。
    def _force_split(c: str) -> list[str]:
        _plain = len(_remove_punctuation(c))
        if _plain <= max_chars:
            return [c]
        # 少しの超過なら、2行に収まる限り割らない。割ると
        # 「心理学者の」(0.8秒)＋「ブルーマ・ツァイガルニックは」のように
        # 修飾語だけが一瞬映るカードになる（2026-08-26 ユーザー指摘）。
        # 長い固有名詞は描画側のオートフィット縮小に任せるほうが読みやすい。
        if _plain <= max_chars + 4 and len(_wrap_chars(c, _scaled_line_n())) <= SUB_MAX_LINES:
            return [c]
        # 読点が複数あれば読点で二分
        parts = [p for p in c.split("、") if p]
        if len(parts) > 1:
            half = len(parts) // 2
            left = "、".join(parts[:half]) + "、"
            right = "、".join(parts[half:])
            left, right = _attach_leading_particles([left, right])   # 「、と〜」の と は左へ（2026-10-02）
        else:
            # mid付近で「助詞・記号の直後」かつ「カタカナ語を割らない」位置を探す
            mid = len(c) // 2
            particles = _PARTICLES + "」』）】。！？"
            split_at = None
            # 第1優先: 文節境界（付属語末＋新文節頭）かつ非禁則
            # 分割した結果どちらかが3字以下の行になる位置は避ける。中央に近いことより
            # 行の質を優先する。「処理しようとする傾向が｜あると…」が「傾向が」(3字)を
            # 作っていた（2026-08-26 ユーザー指摘）。該当が無ければ従来どおり中央優先。
            def _no_stub(q: int) -> bool:
                for half in (c[:q], c[q:]):
                    for line in _wrap_chars(half, SUB_CHARS_PER_LINE):
                        if 0 < len(_remove_punctuation(line)) < _MIN_LINE_CHARS:
                            return False
                return True

            for _strict in (True, False):
                for off in range(len(c)):
                    for p in (mid - off, mid + off):
                        if 0 < p < len(c) and not _bad_break(c, p) \
                                and c[p - 1] in _BUNSETSU_TAIL and _new_phrase_start(c, p) \
                                and (not _strict or _no_stub(p)):
                            split_at = p
                            break
                    if split_at is not None:
                        break
                if split_at is not None:
                    break
            # 第2優先: 助詞・記号の直後かつ非禁則（カタカナ語の連結「と/や/の」で別カード化）
            if split_at is None:
                for off in range(len(c)):
                    for p in (mid - off, mid + off):
                        if 0 < p < len(c) and not _bad_break(c, p) and c[p - 1] in particles:
                            split_at = p
                            break
                    if split_at is not None:
                        break
            # 第3優先: 非禁則の位置（中央に近い順）
            if split_at is None:
                for off in range(len(c)):
                    for p in (mid - off, mid + off):
                        if 0 < p < len(c) and not _bad_break(c, p):
                            split_at = p
                            break
                    if split_at is not None:
                        break
            if split_at is None:
                # 有効な分割位置が無い＝長い括弧引用・英語名など「割れない塊」。
                # 中央で強制切断すると「Dream|Link E|ntertai|nment」のような
                # 語の轢殺になる（2026-07-03発見）。上限超過のまま1カードで返し、
                # 描画側のオートフィット縮小に任せる方が確実に読める。
                return [c]
            left, right = c[:split_at], c[split_at:]
        # 分割が進まない場合は無限再帰を防いでそのまま返す
        if not left or not right:
            return [c]
        return _force_split(left) + _force_split(right)

    split_chunks: list[str] = []
    for c in chunks:
        split_chunks.extend(_force_split(c))
    chunks = split_chunks

    # 各チャンクが文末（句点で終わる）かを記録 → カード結合時に文境界を越えない
    sentence_ends = [c.rstrip()[-1:] in "。！？" for c in chunks]

    # 表示テキストは句点のみ除去（読点は_wrap_charsで改行ヒントに使い、最後に除去）
    display_chunks = [_strip_period(c) for c in chunks]
    n = len(display_chunks)

    if mora_times and len(mora_times) > 1:
        # 発話moraのみ抽出（ポーズ除外）。あわせて「ポーズ直後に始まる発話mora」=
        # ナレーションのフレーズ境界(切れ目)のインデックスを集める。
        speech_moras = []
        phrase_start_idx = []   # speech_moras内でのフレーズ開始位置（実音声の切れ目）
        prev_pause = True
        for m in mora_times:
            if m.text == "　":
                prev_pause = True
                continue
            if prev_pause:
                phrase_start_idx.append(len(speech_moras))
                prev_pause = False
            speech_moras.append(m)
        total_moras = len(speech_moras)
        if total_moras == 0:
            speech_moras = list(mora_times)
            total_moras = len(speech_moras)
            phrase_start_idx = [0]

        # 文字数ではなく「読みの長さ」で比率を取る（漢字のmora圧縮対策・2026-08-26）
        chunk_lens = [_reading_len(_remove_punctuation(c)) for c in chunks]
        total_chars = sum(chunk_lens) or 1
        # char比率で当たりを付け、最寄りの「実音声の切れ目」にスナップして
        # ナレーションの終わりにカード切り替えを合わせる（漢字のmora圧縮による早切り替え対策）。
        # ただしスナップは「直前カードが句読点で終わる＝実際に音声ポーズがある」境界に限る。
        # 句読点の無い強制分割境界(長文を割った継ぎ目)はポーズが無いので char比率のままにする
        # （そうしないとポーズ位置へ吸い寄せられてカードが潰れる）。
        avg_phrase = total_moras / max(1, len(phrase_start_idx))
        snap_tol = max(2.0, avg_phrase * 0.6)   # 切れ目に明確に対応する時だけスナップ
        start_indices = []
        cumulative = 0
        prev = -1
        for i, chunk in enumerate(chunks):
            guess = min(int(cumulative / total_chars * total_moras), total_moras - 1)
            if prev < 0:
                idx = 0
            else:
                prev_has_pause = chunks[i - 1].rstrip()[-1:] in "、。！？"
                cands = [p for p in phrase_start_idx if p > prev] if prev_has_pause else []
                if cands:
                    nearest = min(cands, key=lambda p: abs(p - guess))
                    idx = nearest if abs(nearest - guess) <= snap_tol else max(guess, prev + 1)
                else:
                    idx = max(guess, prev + 1)
                idx = min(idx, total_moras - 1)
                if idx <= prev:
                    idx = min(prev + 1, total_moras - 1)
            start_indices.append(idx)
            prev = idx
            cumulative += chunk_lens[i]

        # ロングは、カードの頭の言葉が実際に読まれ始めるモーラへ合わせ直す（2026-10-29）。
        # 読みの長さの比で当たりを付けると、数字（1946年＝4字と数える）や、声の側で消えた読点の間のぶん
        # 切り替えが早まり、声が前の言葉を読み終える前にテロップと画面が替わっていた（在庫6本で18か所・最大2.4秒）。
        # 表示文の読みとモーラ列を突き合わせ、対応がついた境目だけ置き換える。ショートは今のまま。
        if W > 1080:
            aligned = _align_chunk_starts(chunks, speech_moras)
            prev = 0
            for i in range(1, len(start_indices)):
                a = aligned[i] if i < len(aligned) else None
                if a is not None and prev < a < total_moras:
                    start_indices[i] = a
                elif start_indices[i] <= prev:
                    start_indices[i] = min(prev + 1, total_moras - 1)
                prev = start_indices[i]

        result = []
        for i, display in enumerate(display_chunks):
            if i == 0:
                ts = 0.0  # 冒頭の無音も含めてシーン開始から字幕表示
            else:
                ts = speech_moras[start_indices[i]].start
            if i == n - 1:
                te = duration  # シーン終端まで（次シーンとの境界もカバー）
            else:
                te = speech_moras[start_indices[i + 1]].start
            te = max(te, ts + 0.15)
            result.append((display, ts, te))
        merged = _merge_short_cards(
            result, sentence_ends,
            min_dur=(2.8 if W > 1080 else 1.6),   # ロングは余韻重視で長め
            max_chars=_scaled_max_chars())
        final = _split_overlong_cards(merged, _scaled_line_n())
        if W > 1080 and len(final) > 1:
            # 2行に収まらず割ったカードは、文字数の比で時刻を分けているので、そこも声に合わせ直す
            final = _realign_cards(final, speech_moras)
        return final

    # フォールバック: mora_timesなし → 文字数比で均等配分
    speech_end = duration - 0.15
    total_chars = sum(len(c) for c in chunks) or 1
    result, start = [], 0.0
    for i, display in enumerate(display_chunks):
        if i == n - 1:
            end = speech_end
        else:
            end = start + max(0.3, speech_end * len(chunks[i]) / total_chars)
        result.append((display, start, end))
        start = end
    merged = _merge_short_cards(
        result, sentence_ends,
        min_dur=(2.8 if W > 1080 else 1.6),   # ロングは余韻重視で長め
        max_chars=_scaled_max_chars())
    return _split_overlong_cards(merged, _scaled_line_n())


def _hook_balanced_break(s: str, target: int, lo: int | None = None, hi: int | None = None) -> int:
    """フック用: 読みやすさ最優先で改行点を選ぶ。
    中央均等より「自然な文節区切り（助詞の直後・意味のまとまり）」を強く優先し、
    距離はゆるく効かせる。極端に短い行と禁則は避ける。
    lo/hi を与えると探索をその範囲に限定する（行間の極端な偏りを防ぐ）。"""
    start = 2 if lo is None else max(2, lo)
    end = len(s) - 1 if hi is None else min(len(s) - 1, hi + 1)
    best, best_score = None, None
    for i in range(start, end):
        if _bad_break(s, i):
            continue
        a, b = s[i - 1], s[i]
        score = abs(i - target) * 0.4          # 距離はゆるく（読みやすさを支配的に）
        # 連体「の」で漢字名詞同士をつなぐ複合語（例「死者の丘」）は1語として割らない。
        # 漢字＋の＋漢字 は名詞句のまとまりなので、ここでの改行を強く減点する。
        if a == "の" and i >= 2 and _is_kanji(s[i - 2]) and _is_kanji(b):
            score += 12
        elif a in _STRONG_PARTICLE and _new_phrase_start(s, i):
            score -= 8                          # 格助詞末＋新文節頭：最も読みやすい区切り
        elif a in _STRONG_PARTICLE:
            score -= 6                          # 主要格助詞の直後
        elif a in _BUNSETSU_TAIL and _new_phrase_start(s, i):
            score -= 5                          # 付属語末＋新文節頭
        elif _new_phrase_start(s, i) and (_is_kanji(b) or _is_kata(b)):
            score -= 2                          # 漢字/カタカナ語の頭
        else:
            score += 4                          # 文節の途中は減点
        if i <= 2 or len(s) - i <= 2:
            score += 30                         # 2字以下の極端に短い行を回避
        if best_score is None or score < best_score:
            best, best_score = i, score
    return best if best is not None else target


def _wrap_hook_lines(text: str, n: int = 10) -> list[str]:
    """フックテロップ用の折り返し。読点は無視し、必ず1〜2行に収める（ユーザー方針）。
    3行に割ると余りが「届い」「た」のように語中で千切れるため、2行固定にして
    探索窓(30〜70%)で最も自然な文節区切りを選ぶ。長すぎる文は_hook_phrase側で制限済み。"""
    s = text.replace("、", "").strip()
    if len(s) <= n:
        return [s]
    lo, hi = max(2, int(len(s) * 0.3)), min(len(s) - 2, int(len(s) * 0.7))
    b = _hook_balanced_break(s, round(len(s) / 2), lo, hi)
    return [ln for ln in (s[:b], s[b:]) if ln]


_HOOK_MAX_LINES = 2        # フックバッジの最大行数（2行固定・3行は語中分断の原因）
_HOOK_MAX_CHARS = 27       # 約9字×3行。これを超える長い1文目は冒頭一節だけをフック化


def _hook_phrase(text: str) -> str:
    """フックに使う「冒頭の一節」を返す。漢数字変換のうえ、文末(。！？)や三点リーダ(…)で
    区切った最初の塊を取り、長すぎる場合は読点境界で詰めて最大3行に収める。
    長い1文目を丸ごとフックにして極小6行になる事故を防ぐ。"""
    import re as _re
    t = _kanji_to_arabic(text.strip())
    # 文末・三点リーダ・読点直前の強い区切りで最初の塊を取る
    first = next((s for s in _re.split(r"[。！？\?!…‥]", t) if s.strip()), t).strip()
    plain = first.replace("、", "")
    if len(plain) <= _HOOK_MAX_CHARS:
        return first
    # 長い → 上限内の最後の読点で切る。読点が無ければハード切り。末尾に「…」
    cut = first[:_HOOK_MAX_CHARS + 2]
    if "、" in cut:
        cut = cut.rsplit("、", 1)[0]
    else:
        cut = first[:_HOOK_MAX_CHARS]
    return cut.rstrip("、") + "…"


def _render_hook_telop_png(text: str, out_path: str) -> None:
    """フック専用テロップをフルフレーム(W×H)PNGで生成。画面中央上部に大型テキストを配置。"""
    from PIL import Image, ImageDraw, ImageFont

    hook_text = _hook_phrase(text)

    # 1行最大10字・読点無視で均等折り返し、最大3行に制限（超過分は切り詰めて「…」）
    lines = _wrap_hook_lines(hook_text, 10)
    if len(lines) > _HOOK_MAX_LINES:
        lines = lines[:_HOOK_MAX_LINES]
        lines[-1] = lines[-1].rstrip("、") + "…"
    if not lines:
        lines = [""]

    pad_x, pad_y = 44, 30
    limit_w = int(W * 0.75) - pad_x * 2
    # フックのみ段階的縮小を許可: 基本フォントの 1.45→1.30→1.15→1.00 倍を順に試し、
    # 最長行が画面幅75%に収まる最大サイズを選ぶ（連続縮小ではなく離散4段階）。
    base = int(SUB_FONT_SIZE * 1.3)
    font_size = base
    for mult in (1.45, 1.30, 1.15, 1.00):
        fs = int(base * mult)
        f = _sub_font(fs)
        w = max((f.getbbox(ln)[2] for ln in lines if ln), default=fs)
        if w <= limit_w or mult == 1.00:
            font_size, font = fs, f
            break
    text_w = max((font.getbbox(ln)[2] for ln in lines if ln), default=font_size)

    # 安全装置（最後の砦）: 1.00倍でも画面内に収まらない長い1文は、
    # 画面安全幅(W-40の内側)を超える分だけさらに縮小して必ず収める（はみ出し防止）。
    safe_w = (W - 40) - pad_x * 2
    if text_w > safe_w:
        font_size = max(int(font_size * safe_w / text_w), 44)
        font = _sub_font(font_size)
        text_w = max((font.getbbox(ln)[2] for ln in lines if ln), default=font_size)

    line_h = int(font_size * 1.45)
    band_w = min(W - 40, text_w + pad_x * 2)
    band_h = line_h * len(lines) + pad_y * 2

    # フルフレームPNG（透明背景）
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 画面の上1/3付近に配置
    bar_y = H // 3 - band_h // 2
    bar_x = (W - band_w) // 2
    draw.rounded_rectangle(
        [bar_x, bar_y, bar_x + band_w, bar_y + band_h],
        radius=20, fill=(192, 18, 18, 235)  # 赤バッジ #C01212
    )

    y = bar_y + pad_y
    for ln in lines:
        tw = font.getbbox(ln)[2] if ln else 0
        x = (W - tw) // 2
        # 赤バッジ＋白文字＋黒フチ（確定）
        draw.text((x, y), ln, font=font, fill=(255, 255, 255),
                  stroke_width=8, stroke_fill=(0, 0, 0))
        y += line_h

    img.save(out_path, "PNG")


_SANITIZED_IMG_CACHE: set[str] = set()


def _ensure_ffmpeg_readable(img_path: str) -> None:
    """ffmpegが確実にデコードできるよう、画像をRGBベースラインJPEGとして再保存する。

    PILでは開けるがffmpegデコーダがクラッシュする不正JPEG
    （progressive / CMYK / 拡張子偽装等。2026-06-22のロング動画化失敗の原因）への保険。
    画像生成側(image_generator._to_rgb)で正規化済みでも、既存素材の再ビルドや
    外部から差し込まれた画像に備えてビルド入口でも防御する。
    同一パスは一度だけ処理する（縦/横ともサイズは変更しない）。"""
    if not img_path or img_path in _SANITIZED_IMG_CACHE:
        return
    _SANITIZED_IMG_CACHE.add(img_path)
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            im.load()
            rgb = im.convert("RGB")
        rgb.save(img_path, "JPEG", quality=90, progressive=False, optimize=False)
    except Exception:
        pass  # PILでも開けない画像はffmpeg側のエラーに委ねる（per-video try/exceptで継続）


def _build_card_ffmpeg(
    ffmpeg_exe: str,
    img_path: str,
    wav_path: str,
    audio_start: float,
    card_dur: float,
    sub_png: str | None,
    out_path: str,
    hook_png: str | None = None,
    hook_full: bool = False,
    slide_in: bool = False,
    motion: int = 0,
) -> None:
    """1テロップカード分のクリップを生成。画像・音声・字幕PNG・フックテロップを合成。

    sub_png=None なら下部字幕を出さない（フック専用カードで使用）。
    hook_full=True なら hook をカード全尺で表示（フック文の読み終わりまで引き延ばす）。
    slide_in=True なら字幕にフェード＋スライドアップを付ける。
    motion: 0=ズームイン 1=右パン 2=左パン 3=上パン 4=下パン。"""
    _ensure_ffmpeg_readable(img_path)   # 不正JPEGでffmpegが落ちるのを防ぐ（入口の保険）
    frames = max(1, int(card_dur * VIDEO_FPS))
    z0, z1 = KEN_BURNS_ZOOM_FROM, KEN_BURNS_ZOOM_TO
    zpf = (z1 - z0) / max(frames - 1, 1)
    n_prog = f"(on/{max(frames - 1, 1)})"
    margin_x, margin_y = "(iw-iw/zoom)", "(ih-ih/zoom)"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == 0:      # ズームイン（中央固定）
        z_expr = f"min({z0:.4f}+{zpf:.6f}*on,{z1:.4f})"
        x_expr, y_expr = cx, cy
    else:                # パン: ズーム固定で直線移動
        z_expr = f"{z1:.4f}"
        if motion == 1:   x_expr, y_expr = f"{margin_x}*{n_prog}", cy          # 右
        elif motion == 2: x_expr, y_expr = f"{margin_x}*(1-{n_prog})", cy      # 左
        elif motion == 3: x_expr, y_expr = cx, f"{margin_y}*(1-{n_prog})"      # 上
        else:             x_expr, y_expr = cx, f"{margin_y}*{n_prog}"          # 下
    zoompan = (
        f"[0:v]scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
        f"crop={W * 2}:{H * 2},"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={W}x{H},"
        f"fps={VIDEO_FPS}[bg]"
    )

    # inputs: 0=img, 1=wav(trimmed), [hook], [sub]
    inputs = [
        "-loop", "1", "-t", str(card_dur), "-i", img_path,
        "-ss", str(audio_start), "-t", str(card_dur), "-i", wav_path,
    ]

    filter_parts = [zoompan]
    prev = "bg"
    next_idx = 2

    # フックテロップ
    if hook_png and os.path.exists(hook_png):
        hook_dur = card_dur if hook_full else min(card_dur, 2.0)
        inputs += ["-loop", "1", "-t", str(hook_dur), "-i", hook_png]
        hook_idx = next_idx
        next_idx += 1
        filter_parts.append(
            f"[{hook_idx}:v]format=yuva420p,fade=t=in:st=0:d=0.35:alpha=1[hook_f]"
        )
        filter_parts.append(
            f"[{prev}][hook_f]overlay=0:0:enable='between(t,0,{hook_dur:.3f})'[hook_out]"
        )
        prev = "hook_out"

    # 下部字幕（sub_png が None ならスキップ）
    if sub_png:
        inputs += ["-i", sub_png]
        sub_idx = next_idx
        if slide_in:
            filter_parts.append(
                f"[{sub_idx}:v]format=yuva420p,fade=t=in:st=0:d=0.25:alpha=1[fsub]"
            )
            slide_y = "if(lt(t,0.25),80*(1-t/0.25),0)"
            filter_parts.append(f"[{prev}][fsub]overlay=x=0:y='{slide_y}'[vout]")
        else:
            filter_parts.append(f"[{prev}][{sub_idx}:v]overlay=0:0[vout]")
    else:
        filter_parts.append(f"[{prev}]null[vout]")

    cmd = (
        [ffmpeg_exe, "-y"]
        + inputs
        + ["-filter_complex", ";".join(filter_parts),
           "-map", "[vout]",
           "-map", "1:a",
           "-t", str(card_dur)]
        + _video_codec_args()
        + ["-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
           "-pix_fmt", "yuv420p",
           out_path]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg card failed:\n{result.stderr[-1000:]}")


def _build_scene_ffmpeg(
    ffmpeg_exe: str,
    img_paths: list[str],          # 各カードに使う画像リスト（カード数と同じ長さ）
    wav_path: str,
    duration: float,
    subtitle_timings: list[tuple[str, float, float]],
    out_path: str,
    tmp_dir: str,
    scene_idx: int = -1,
    hook_png: str | None = None,
    hook_text: str | None = None,   # フック文（第1文）。これを構成するカードはバッジのみ表示
    motion_base: int = 0,           # 動き循環用のグローバルカード番号オフセット
) -> None:
    """1シーンをFFmpegで生成。テロップカードごとに画像を切り替えてconcatする。"""
    # --- 字幕PNG事前生成 ---
    sub_files = []
    for i, (chunk, ts, te) in enumerate(subtitle_timings):
        sub_png = os.path.join(tmp_dir, f"sub_{os.path.basename(out_path)}_{i}.png")
        _render_subtitle_png(chunk, sub_png)
        sub_files.append((sub_png, chunk, ts, te))

    # --- フック専用カードの判定（scene0のみ）---
    # フック文(第1文)を構成する先頭カード群を1枚のフックカードに統合し、
    # その間は赤バッジのみ表示・下字幕は出さず、フック文の読み終わりまで引き延ばす。
    hook_n = 0
    if scene_idx == 0 and hook_png and hook_text:
        import re as _re
        plain_hook = _re.sub(r"[、。！？…‥\s]", "", hook_text)
        acc = ""
        for (_sp, chunk, _ts, _te) in sub_files:
            acc += _re.sub(r"[、。！？\s]", "", chunk)
            hook_n += 1
            if len(acc) >= len(plain_hook):
                break

    # --- カードごとにクリップ生成 ---
    card_clips: list[str] = []
    if hook_n > 0:
        # 統合フックカード: [先頭カードts, 末尾フックカードte] を赤バッジのみで再生
        h_ts = sub_files[0][2]
        h_te = sub_files[hook_n - 1][3]
        h_dur = h_te - h_ts
        hook_out = os.path.join(tmp_dir, f"card_{os.path.basename(out_path)}_hook.mp4")
        _build_card_ffmpeg(
            ffmpeg_exe, img_paths[0], wav_path, h_ts, h_dur,
            None, hook_out, hook_png=hook_png, hook_full=True, motion=0,
        )
        card_clips.append(hook_out)

    for i, (sub_png, _chunk, ts, te) in enumerate(sub_files):
        if i < hook_n:
            continue  # フックカードに統合済み
        card_dur = te - ts
        if card_dur < 0.05:
            continue
        img_path = img_paths[i % len(img_paths)]
        motion = (motion_base + i) % 5
        card_out = os.path.join(tmp_dir, f"card_{os.path.basename(out_path)}_{i:03d}.mp4")
        _build_card_ffmpeg(
            ffmpeg_exe, img_path, wav_path, ts, card_dur,
            sub_png, card_out, motion=motion,
        )
        card_clips.append(card_out)

    # --- カードクリップを結合してシーン出力 ---
    list_file = os.path.join(tmp_dir, f"cards_{os.path.basename(out_path)}.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for clip in card_clips:
            f.write(f"file '{clip}'\n")

    cmd = [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0",
           "-i", list_file, "-c", "copy", out_path]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg scene concat failed:\n{result.stderr[-1000:]}")


def _eyecatch_pool() -> list[str]:
    """アイキャッチ用の背景画像（人物なし）。無ければ空リスト。"""
    d = Path(__file__).parent.parent / "assets" / "eyecatch_pool"
    return sorted(str(p) for p in d.glob("*.jpg")) if d.exists() else []


def _render_eyecatch_card(title: str, out_png: str, seq: int = 0) -> bool:
    """章タイトルを、サムネと同じ様式の文字テンプレートで焼いたPNGを作る。

    ・背景は人物の写っていない画像プール（assets/eyecatch_pool）から順に使う
    ・文字はサムネと同じ「中央・大きく・紺の下線」。読みやすさ優先で暗幕を敷く
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    try:
        from config.settings import FONT_PATH
    except Exception:
        return False
    pool = _eyecatch_pool()
    if not pool:
        return False
    bg = Image.open(pool[seq % len(pool)]).convert("RGB")
    # 画面いっぱいに切り出し
    r = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * r + 0.5), int(bg.height * r + 0.5)), Image.LANCZOS)
    x, y = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((x, y, x + W, y + H))
    bg = ImageEnhance.Brightness(bg).enhance(0.62)   # 画像が見える明るさを残す
    base = bg.convert("RGBA")
    d = ImageDraw.Draw(base)

    # 文字サイズは幅に収まるまで落とす（章見出しは12字以内の想定）
    size = 128
    while size > 60:
        f = _sub_font(size)
        if d.textlength(title, font=f) <= W - 260:
            break
        size -= 6
    f = _sub_font(size)
    tw = d.textlength(title, font=f)
    tx, ty = (W - tw) / 2, (H - size * 1.2) / 2

    # 中央に薄い暗幕（サムネと同じ考え方。帯は出しすぎない）
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rectangle([0, int(ty - 56), W, int(ty + size * 1.2 + 56)], fill=(0, 0, 0, 135))
    base = Image.alpha_composite(base, band)
    d = ImageDraw.Draw(base)

    # 本文（白・細い縁取り）
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
        d.text((tx + dx, ty + dy), title, font=f, fill=(0, 0, 0, 190))
    d.text((tx, ty), title, font=f, fill=(248, 250, 255))
    # 紺のアンダーライン（チャンネルのアクセント色）
    uy = int(ty + size * 1.28)
    d.rectangle([int(W / 2 - tw / 2), uy, int(W / 2 + tw / 2), uy + 8],
                fill=(150, 30, 30, 235))
    base.convert("RGB").save(out_png, quality=95)
    return True


def _probe_audio_spec(ffmpeg_exe: str, scene_files: list[str]) -> tuple[str, str]:
    """既存シーンの音声仕様を読む。concatは仕様が揃っていないと音声を落とすため。"""
    import re as _re
    ar, cl = "24000", "mono"
    if scene_files:
        pb = subprocess.run([ffmpeg_exe, "-i", scene_files[0]],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = _re.search(r"Audio:.*?(\d+) Hz, (\w+)", pb.stderr)
        if m:
            ar = m.group(1)
            cl = "stereo" if m.group(2) == "stereo" else "mono"
    return ar, cl


def _build_eyecatch_ffmpeg(ffmpeg_exe: str, image_path: str, out_path: str,
                           ar: str, cl: str, sec: float) -> bool:
    """章の変わり目に挟む短いカットを作る（画像＋ピアノの単音、テロップなし）。

    なぜ独立クリップにするか
    ------------------------
    シーンの中に差し込むと字幕タイミング（mora基準）とずれる。
    シーンと同じ解像度・SAR・コーデック・音声仕様で別クリップを作り、
    concatの並びに割り込ませるほうが、同期を一切崩さない。
    """
    se = EYECATCH_SE_FILE if (EYECATCH_SE_FILE and os.path.exists(EYECATCH_SE_FILE)) else None
    # ゆっくり寄るズームと、前後のフェード。暗めにして本編と区別する
    is_card = image_path.lower().endswith(".png")   # 文字カードは描画時に調整済み
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          + ("" if is_card else "eq=brightness=-0.12:saturation=0.85,") +
          f"fade=t=in:st=0:d=0.45,fade=t=out:st={max(0.0, sec - 0.6):.2f}:d=0.6")
    cmd = [ffmpeg_exe, "-y", "-loop", "1", "-t", f"{sec}", "-i", image_path]
    if se:
        # SEは頭で鳴らし、残りは無音で埋める（apad）。音量は控えめに。
        # SEはフェードインが終わって画が見えてから鳴らす（0秒に置くと暗転中に鳴って締まらない）
        delay_ms = int(EYECATCH_SE_DELAY * 1000)
        cmd += ["-i", se,
                "-filter_complex",
                f"[1:a]volume={EYECATCH_SE_VOLUME},aformat=sample_rates={ar}:channel_layouts={cl},"
                f"adelay={delay_ms}|{delay_ms},apad,atrim=0:{sec},asetpts=N/SR/TB[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-f", "lavfi", "-t", f"{sec}", "-i", f"anullsrc=r={ar}:cl={cl}"]
    cmd += ["-vf", vf] + _video_codec_args() + [
        "-pix_fmt", "yuv420p", "-r", str(VIDEO_FPS), "-c:a", "pcm_s16le", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  ⚠ アイキャッチ生成失敗: {r.stderr[-300:]}")
        return False
    return True


def _eyecatch_positions(n_scenes: int, chapter_starts=None) -> set[int]:
    """アイキャッチを差し込むシーン番号（そのシーンの「前」に入れる）。

    アイキャッチは**章の区切り**に入れるもの（2026-08-27 ユーザー指示）。
    章の情報が無い台本には入れない。等間隔で機械的に挟むと、話の途中で切れる。
    """
    if not EYECATCH_ENABLED or W < 1920:
        return set()
    if not chapter_starts:
        return set()
    if isinstance(chapter_starts, dict):
        # 同じ章名が続く場面は章の始まりではない（0924: 全場面に章名を持つ台本で、60場面に59回
        # アイキャッチが入り+130秒になった）。呼び出し側が全場面を渡しても、章名が変わる所だけに絞る。
        _d = {int(k): (v or "").strip() for k, v in chapter_starts.items()}
        chapter_starts = [k for k, v in _d.items() if not (v and _d.get(k - 1) == v)]
    # 先頭（掴み）と最終シーン直前は除く
    return {int(p) for p in chapter_starts if 0 < int(p) < n_scenes}


def build_video(
    scene_images: list[str],
    voice_results: list[VoiceResult],
    scene_narrations: list[str],
    out_path: str,
    thumbnail_path: str | None = None,
    scene_card_images: list[list[str]] | None = None,
    chapter_starts: set[int] | None = None,
) -> str:
    """FFmpegでシーンを合成して動画を生成。

    scene_card_images: 各シーンのカード単位画像リスト（実カード数ぴったり）。
        指定時はカードごとに対応画像を使う（循環なし）。
        未指定なら scene_images をカード循環で割り当てる（旧仕様・後方互換）。"""
    ffmpeg_exe = _ffmpeg()
    bgm_path = _get_bgm()

    with tempfile.TemporaryDirectory(prefix="ytauto_ffmpeg_") as tmp_dir:
        scene_files: list[str] = []

        # フックテロップPNG（scene 0 冒頭用）。
        # ロング(横1920)は「まとめ動画風」の静謐なトーンを優先し、黄色フックを無効化する。
        hook_png_path = os.path.join(tmp_dir, "hook_telop.png")
        hook_text = None
        use_hook = bool(scene_narrations) and W <= 1080
        if use_hook:
            _render_hook_telop_png(scene_narrations[0], hook_png_path)
            # フックカード統合の対象も、バッジと同じ「冒頭一節」に揃える
            # （長い1文目の場合、残りは通常字幕として表示される）
            hook_text = _hook_phrase(scene_narrations[0])

        # global_card_offset は動き(motion)循環用に維持
        global_card_offset = 0
        n_scenes = len(voice_results)
        # 章の変わり目に挟むアイキャッチ（2026-08-27 追加）
        eye_positions = _eyecatch_positions(len(voice_results), chapter_starts)
        if eye_positions:
            print(f"  アイキャッチ: 章の区切り シーン{sorted(eye_positions)} の前に挿入")
        eye_n = 0
        # mp4 に焼いた字幕の並びを控える。preflight はこれと現行エンジンの計算結果を
        # 突き合わせて「古いエンジンで焼かれた在庫」を見つける。
        # 従来 preflight は台本から毎回計算し直していたので、エンジンを直しても
        # 在庫の mp4 が古いままであることを検出できなかった（2026-08-31）。
        burned_layout: list[list[list[str]]] = []
        for i, (vr, narration) in enumerate(zip(voice_results, scene_narrations)):
            duration = vr.duration
            sub_timings = _subtitle_timings(narration, duration, vr.mora_times)
            n_cards = len(sub_timings)
            # 描画(_render_subtitle_png)と同じ _relief=True で記録する。channel D は正しかったが
            # channel A だけ抜けていて、焼いた字幕と記録が食い違っていた（2026-09-24）
            burned_layout.append([_wrap_chars(_c, _scaled_line_n(), _relief=True) for _c, _a, _b in sub_timings])
            # カード画像の割り当て: scene_card_images があればカード単位の実画像を使用、
            # 無ければ scene_images をグローバル循環で割り当て（後方互換）
            if scene_card_images and i < len(scene_card_images) and scene_card_images[i]:
                imgs = scene_card_images[i]
                card_img_paths = [imgs[j % len(imgs)] for j in range(n_cards)]
            else:
                N = len(scene_images)
                card_img_paths = [
                    scene_images[(global_card_offset + j) % N] for j in range(n_cards)
                ]
            scene_out = os.path.join(tmp_dir, f"scene_{i:02d}.mp4")
            print(f"  シーン {i + 1}/{n_scenes}: {n_cards}カード")
            _build_scene_ffmpeg(
                ffmpeg_exe, card_img_paths, vr.wav_path, duration,
                sub_timings, scene_out, tmp_dir, scene_idx=i,
                hook_png=hook_png_path if (use_hook and i == 0) else None,
                hook_text=hook_text if (use_hook and i == 0) else None,
                motion_base=global_card_offset,
            )
            if i in eye_positions:
                _ar, _cl = _probe_audio_spec(ffmpeg_exe, scene_files)
                eye_out = os.path.join(tmp_dir, f"eyecatch_{eye_n:02d}.mp4")
                _title = (chapter_starts.get(i) or "").strip() \
                    if isinstance(chapter_starts, dict) else ""
                _img = card_img_paths[0]
                if _title:
                    _card = os.path.join(tmp_dir, f"eyecatch_{eye_n:02d}.png")
                    if _render_eyecatch_card(_title, _card, eye_n):
                        _img = _card
                if _build_eyecatch_ffmpeg(ffmpeg_exe, _img, eye_out, _ar, _cl, EYECATCH_SEC):
                    scene_files.append(eye_out)
                    eye_n += 1
            scene_files.append(scene_out)
            global_card_offset += n_cards

        # サムネイルフレーム（ショートのみ）
        if thumbnail_path and Path(thumbnail_path).exists():
            from src.video_builder import _cover_resize
            import numpy as np
            from PIL import Image
            thumb_png = os.path.join(tmp_dir, "thumb_frame.png")
            pil_img = _cover_resize(thumbnail_path)
            pil_img.save(thumb_png)
            # 仕様値0.6秒（append_thumbnail_frame.py の TAIL_SEC と同じ）。
            # 3フレーム(0.1秒)だと、あとの BGM 合成の -shortest で丸ごと削られる。
            # 2026-09-18 の新規30本中27本がそれで末尾サムネを失っていた。
            dur = 0.6
            thumb_out = os.path.join(tmp_dir, "scene_thumb.mp4")
            # 他シーンと同じ音声仕様(コーデック・サンプルレート・チャンネル)で無音トラックを付ける。
            # これらが一致しないとconcat(無劣化結合)で音声ストリームが弾かれ、サムネが結合されない。
            import re as _re
            ar, cl = "24000", "mono"
            if scene_files:
                _pb = subprocess.run([ffmpeg_exe, "-i", scene_files[0]],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _m = _re.search(r"Audio:.*?(\d+) Hz, (\w+)", _pb.stderr)
                if _m:
                    ar = _m.group(1)
                    cl = "stereo" if _m.group(2) == "stereo" else "mono"
            cmd = ([ffmpeg_exe, "-y",
                   "-loop", "1", "-t", str(dur), "-i", thumb_png,
                   "-f", "lavfi", "-t", str(dur), "-i", f"anullsrc=r={ar}:cl={cl}",
                   "-vf", f"scale={W}:{H},setsar=1"]   # 解像度・SARをシーンと一致させないとconcatで弾かれる
                   + _video_codec_args()               # シーンとコーデックを揃えないとconcat(-c copy)で不整合
                   + ["-pix_fmt", "yuv420p", "-r", str(VIDEO_FPS),
                   "-c:a", "pcm_s16le", "-shortest",
                   thumb_out])
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode != 0:
                print(f"  ⚠ サムネフレーム生成失敗: {res.stderr[-300:]}")
            else:
                scene_files.append(thumb_out)

        # --- concat（PTS再生成つき再エンコード） ---
        # 旧実装は concatデマクサ -c copy で無再エンコード連結していたが、各シーンmp4の
        # PTS/フレーム間隔の微小な不揃いが累積し、映像(=焼き込みテロップ)が音声より先行して
        # 後半でテロップがズレる/欠ける事故を起こす（project_concat_av_drift_fix）。
        # concat「フィルタ」で再エンコードし、-r/-fps_mode cfr でPTSを作り直して同期を保つ。
        list_file = os.path.join(tmp_dir, "concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for sf in scene_files:
                f.write(f"file '{sf}'\n")
        concat_out = os.path.join(tmp_dir, "concat.mp4")
        # 連結は寛容な concatデマクサで行い、映像だけ -fps_mode:v cfr で再エンコードして
        # PTSを一定間隔に作り直す。-c copy 無再エンコードだとPTS/フレーム間隔の不揃いが累積して
        # 映像(=焼き込みテロップ)が音声より先行しズレる/欠ける（project_concat_av_drift_fix）。
        cmd = ([ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                "-r:v", str(VIDEO_FPS), "-fps_mode:v", "cfr"]
               + _video_codec_args()
               + ["-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", concat_out])
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg concat(reencode cfr) failed:\n{result.stderr[-800:]}")

        # --- BGM＋冒頭フックSE 合成 ---
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        hook_se = HOOK_SE_FILE if (HOOK_SE_FILE and os.path.exists(HOOK_SE_FILE)) else None

        if bgm_path and hook_se:
            # ナレーション(0:a) + BGM(1:a, ループ・小音量) + フックSE(2:a, 0秒位置)
            cmd = [ffmpeg_exe, "-y",
                   "-i", concat_out,
                   "-stream_loop", "-1", "-i", bgm_path,
                   "-i", hook_se,
                   "-filter_complex",
                   (f"[1:a]volume={BGM_VOLUME}[bgm];"
                    f"[2:a]volume={HOOK_SE_VOLUME}[se];"
                    f"[0:a][bgm][se]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]"),
                   "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-shortest", out_path]
        elif bgm_path:
            cmd = [ffmpeg_exe, "-y",
                   "-i", concat_out,
                   "-stream_loop", "-1", "-i", bgm_path,
                   "-filter_complex",
                   f"[1:a]volume={BGM_VOLUME}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                   "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-shortest", out_path]
        elif hook_se:
            cmd = [ffmpeg_exe, "-y",
                   "-i", concat_out,
                   "-i", hook_se,
                   "-filter_complex",
                   (f"[1:a]volume={HOOK_SE_VOLUME}[se];"
                    f"[0:a][se]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"),
                   "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-shortest", out_path]
        else:
            cmd = [ffmpeg_exe, "-y", "-i", concat_out,
                   "-c", "copy", out_path]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg BGM/SE mix failed:\n{result.stderr[-500:]}")

    # 焼いた字幕の並びを mp4 の横に残す（preflight の再ビルド要否判定に使う）
    try:
        import json as _json
        _side = os.path.join(os.path.dirname(out_path), "subtitle_layout.json")
        with open(_side, "w", encoding="utf-8") as _f:
            _json.dump({"line_n": _scaled_line_n(), "scenes": burned_layout},
                       _f, ensure_ascii=False)
    except Exception as _e:      # 記録に失敗しても動画生成は止めない
        print(f"  ! 字幕レイアウトの記録に失敗: {_e}")

    print(f"  ✓ FFmpeg完成: {out_path}")
    return out_path
