"""サムネイル生成: 縦1080x1920。背景画像＋上部に大きなタイトル文字（赤系の縁取り）"""
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    FONT_PATH, VIDEO_WIDTH, VIDEO_HEIGHT,
    LONG_THUMB_WIDTH, LONG_THUMB_HEIGHT,
)


def _target_dims() -> tuple[int, int, bool]:
    """サムネのサイズと向きを決定。video_builderのW/Hが横長ならロング(1280x720)。
    戻り値: (幅, 高さ, is_horizontal)"""
    try:
        import src.video_builder_ffmpeg as vb   # 本番ビルダー（旧 src.video_builder はMoviePy版で未使用）
        if vb.W >= vb.H:
            return LONG_THUMB_WIDTH, LONG_THUMB_HEIGHT, True
    except Exception:
        pass
    return VIDEO_WIDTH, VIDEO_HEIGHT, False


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    iw, ih = img.size
    if iw / ih > w / h:
        nh = h
        nw = int(iw * (h / ih))
    else:
        nw = w
        nh = int(ih * (w / iw))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _bad_break(text: str, i: int) -> bool:
    """字幕と同じ禁則エンジンを流用（no_split_phrases.json 含む）。
    サムネ専用の追加禁則もここに足す。"""
    from src.video_builder_ffmpeg import _bad_break as _vb_bad_break
    return _vb_bad_break(text, i)


# 切り詰めた見出しの末尾に残ると不格好な中途半端な語尾（助動詞・接続表現）。
# 「〜かもしれない」を14字で切ると「かもし」になる等の割れを防ぐため、末尾から丸ごと落とす。
_DANGLING_TAILS = (
    # 複合語尾は長い形を先に試す。短い「かもしれない」だけだと
    # 「駒なのかもしれない」→「駒なの」→末尾「の」除去→「駒な」と語中が残る事故になる。
    # 「なの/の＋語尾」をまとめて落として「駒」まで戻し、そこまで短いなら
    # （_strip_dangling_tail の2字未満チェックで）剥がさず完全形を残す。
    "なのかもしれない", "なのかもしれません", "のかもしれない", "のかもしれません",
    "なのでしょうか", "なのでしょう", "のでしょうか", "のでしょう",
    "なのだろうか", "なのだろう", "のだろうか", "のだろう",
    "かもしれない", "かもしれません", "かもしれ", "かもし", "かも",
    "でしょうか", "でしょう", "でしょ", "だろうか", "だろう",
    "という", "といわれ", "とされ", "ではない", "なのか", "のか",
    # 「ている」は入れない。X＋ている から剥がすと必ず動詞の連用形／イ音便語幹が残り
    # （「返している」→「返し」、「歩いている」→「歩い」）、完成した語にならないため。
)


# 語中でしか現れない字（促音・撥音・長音）。剥がした結果がこれで終わるなら
# 動詞のて形語幹などを途中で切っている（「生き残っている」→「生き残っ」）ので剥がさない。
_BROKEN_END = "っッんンー"

# 動詞の活用途中（連用形・て形の語幹）にしか現れない送り仮名（い段・え段）。
# 「返しているのか」→「返している」→「返し」のように連鎖で剥がすと、
# 促音・撥音でないため _BROKEN_END をすり抜けて未完成な連用形が残る。
# ※「い」「え」は「怖い」「答え」のようにそれ自体で完結する語末になるので含めない。
_RENYOU_END = "きぎしじちにひびみりけげせぜてでねへべめれ"


def _incomplete_end(s: str) -> bool:
    """s が語中・活用途中で終わっていて、見出しとして未完成に見えるか。"""
    return (not s) or (s[-1] in _BROKEN_END) or (s[-1] in _RENYOU_END)


def _is_question(s: str) -> bool:
    """問いかけの形で終わっているか（終助詞「か」または疑問符）。"""
    return s.endswith(("か", "か？", "か?", "？", "?"))


# 形式名詞など、末尾の1字が助詞と同じ字でできている語。rstrip は末尾の該当文字を
# まとめて落とすので、これらを守らないと「書かれていたこと」→「…いたこ」のように
# 語が壊れる（末尾が「こ」なので _incomplete_end もすり抜ける。2026-08-13 死海文書）。
#   2026-08-24 追加「まで」: 「古代の街道から夜汽車まで」→「…夜汽車ま」になっていた
#   （末尾が「ま」なので _incomplete_end もすり抜ける。睡眠用 旅と道 のサムネで発覚）。
#   rstrip が落とす字は「のにをはがへとでもや、」なので、この字で終わる2字の語だけが
#   壊れうる。かなで書く語では「まで」がその最後の抜け穴だった。
_KEEP_TAILS = ("こと", "もの", "ひと", "とき", "ため", "はず", "わけ", "ところ", "あと", "もと",
               "まで")


def _breaks_keep_tail(s: str, s2: str) -> bool:
    """剥がし（s→s2）が保護語尾の途中で切っているか。"""
    return any(s.endswith(k) and not s2.endswith(k) for k in _KEEP_TAILS)


def _strip_dangling_tail(s: str) -> str:
    """末尾の助詞＋中途半端な語尾を繰り返し落として、意味の切れる位置で終わらせる。
    ただし剥がした結果が語中（促音・撥音・長音止まり）や活用途中（連用形止まり）に
    なる場合はその剥がしを行わず、元の完全形を残す。
    長すぎても make_thumbnail 側の _fit_wrap がフォントを縮小して全文表示するので、
    無理に短くするより完全な形を優先する。"""
    changed = True
    while changed:
        changed = False
        s2 = s.rstrip("のにをはがへとでもや、")
        if s2 != s and not _incomplete_end(s2) and not _breaks_keep_tail(s, s2):
            s, changed = s2, True
        for t in _DANGLING_TAILS:
            if s.endswith(t):
                # 最長一致した時点で打ち切る。短い語尾に落ちると
                # 「駒なのかもしれない」→「駒な」のように語中が残るため。
                cand = s[: -len(t)]
                # 疑問形を剥がすと「実在したのか」→「実在した」のように
                # 問いかけが断定に変わり、意味が反転する。UMA・都市伝説では
                # 「いる前提」の断定になってしまうため、この剥がしは行わない
                # （2026-08-12 ジャッカロープのサムネで発覚）。
                if _is_question(s) and not _is_question(cand):
                    break
                if len(cand) >= 2 and not _incomplete_end(cand):
                    s, changed = cand, True
                # 短すぎる／語中・活用途中で終わる剥がしは不採用（元の完全形を残す）
                break
    return s


def _cut_natural_ex(text: str, limit: int) -> tuple[str, bool]:
    """limit字以内で語の途中を切らない自然な位置まで縮める。
    自然な位置が無ければ従来どおり limit で切る（後方互換）。
    切り詰め後の末尾に残った助詞・中途半端な語尾（〜の/〜かもし 等）は不格好なので落とす。
    戻り値: (結果, 長さ制限で末尾を切り落としたか)。
    末尾語尾の剥がしは語中で終わらないよう保証済みなので、切り落とし扱いにしない。"""
    if len(text) <= limit:
        return (_strip_dangling_tail(text) if len(text) > 6 else text), False
    cut = None
    for i in range(limit, max(1, limit - 8), -1):
        if not _bad_break(text, i):
            cut = i
            break
    s = text[:cut] if cut else text[:limit]
    return _strip_dangling_tail(s), True


def _cut_natural(text: str, limit: int) -> str:
    return _cut_natural_ex(text, limit)[0]


def extract_headline_ex(title: str) -> tuple[str, str, bool]:
    """タイトルからサムネ用の短いキャッチを抽出。
    戻り値: (大見出し, サブ見出し, サブ見出しを長さ制限で切り詰めたか)。
    切り詰めていなければ、末尾が促音/撥音/長音でも完全な語（「セーリング・ストーン」等）である。"""
    # 【...】があればそれを大見出しに
    m = re.search(r"[【「](.+?)[】」]", title)
    if m:
        head = m.group(1)
        rest = title.replace(m.group(0), "")
    else:
        # 句読点・記号で区切って先頭の塊を見出しに
        parts = re.split(r"[、。…！？「」『』\s]+", title)
        parts = [p for p in parts if p]
        head = parts[0] if parts else title
        rest = "".join(parts[1:])
    # 残りに別の【…】が入っていることがある（【約60分・作業用BGM】のような尺・用途の注記）。
    # title.replace は大見出しにした1つ目しか外さないので、2つ目以降がサブ見出しの材料に
    # 混ざってしまう。【】の中身は見出しではないので全部落とす。
    # ⚠ 落とさないと「星と月の神話｜眠れる夜空の物語【約60分・作業用BGM】」が
    #   24字制限で「…の物語【約60分・作業用」と切れる（2026-08-24 発見）。
    rest = re.sub(r"【[^】]*】", "", rest)
    # 大見出しが長すぎる場合は語境界で自然に短縮（語中切り捨て禁止）
    head = _cut_natural(head, 12)
    # サブ見出しはインパクトある短い語を1つ
    sub = ""
    sub_cut = False
    sub_parts = re.split(r"[、。…！？「」『』\s]+", rest)
    sub_parts = [p for p in sub_parts if len(p) >= 3]
    if sub_parts:
        # 先頭が強い格助詞(を/に/へ/が)で始まる断片は「に秘められた…」のように
        # 助詞始まりの不自然な見出しになるため優先度を下げる。全滅時のみ許容。
        good = [p for p in sub_parts if p[0] not in "をにへが"]
        pool = good if good else sub_parts
        # サブは1つの節（句読点で区切った塊）をそのまま採用し、原則 truncate しない。
        # 節が収まらない時は make_thumbnail 側の _fit_wrap がフォントを縮小して全文表示する
        # （途中で「…いけ」と切れる事故の恒久防止）。24字超の異常に長い節のみ語境界で短縮。
        sub, sub_cut = _cut_natural_ex(max(pool, key=len), 24)
    return head, sub, sub_cut


def _extract_headline(title: str) -> tuple[str, str]:
    head, sub, _cut = extract_headline_ex(title)
    return head, sub


_GOOD_TAIL = "てしはをがにでともへや、。"
_PARTICLE_HEAD = "のはがをにへとも"   # これらが行頭に来ると不自然（助詞の置き去り）

# 未然形＋助動詞の分断検出用。「語ら｜ない」「さら｜われる」のように、
# 語幹があ段で終わり次が助動詞で始まる位置は、語の途中なので折りたくない。
# _incomplete_end（連用形・促音止まり）では拾えないのでこちらで見る。
_A_ROW = "かがさざただなはばまやらわ"
_AUX_START = "なれらせしそわ"


def _splits_conjugation(first: str, second: str) -> bool:
    return bool(first and second and first[-1] in _A_ROW and second[0] in _AUX_START)


def _wrap_ex(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> tuple[list[str], int]:
    """ピクセル幅に収まる範囲で、禁則を避けた自然な位置で改行する。
    字幕と同じ _bad_break（カタカナ語・熟語・助詞・保護フレーズ）を参照。
    戻り値: (行リスト, 強制切断した回数)。強制切断>0なら呼び出し側でフォント縮小を検討。"""
    lines: list[str] = []
    forced = 0
    rest = text
    while rest:
        # ピクセル幅に収まる最大文字数を求める
        fit = len(rest)
        while fit > 1 and font.getbbox(rest[:fit])[2] > max_w:
            fit -= 1
        if fit >= len(rest):
            lines.append(rest)
            break
        # fit位置から後ろへ改行点を探す（最大6文字戻る）。
        # ① 最も詰まる非禁則点（greedy）を求め、② 意味の切れ目（助詞・て形・読点の直後）
        # を優先候補として求める。②が「均衡を悪化させず・1行目が短すぎず・行頭に助詞を
        # 残さない」場合のみ②を採用する。これで「予言して死ぬ｜獣」(5/1)を「予言して｜死ぬ獣」
        # に是正しつつ、「宇宙へ｜の手紙」(への分断)等の副作用を防ぐ。
        lo = max(1, fit - 6)
        greedy = next((i for i in range(fit, lo, -1) if not _bad_break(rest, i)), None)
        good = next(
            (i for i in range(fit, lo, -1)
             if not _bad_break(rest, i)
             and rest[i - 1] in _GOOD_TAIL
             and i >= 3                                   # 1行目が短すぎない
             and (i >= len(rest) or rest[i] not in _PARTICLE_HEAD)),  # 行頭に助詞を残さない
            None,
        )
        cut = greedy
        if good is not None:
            rem = len(rest)
            if greedy is None or abs(good - (rem - good)) <= abs(greedy - (rem - greedy)):
                cut = good
        if cut is None:
            cut = _forced_cut(rest, fit, font, max_w)  # 全部禁則（長いカタカナ語・保護フレーズ）→ 強制切断を記録
            forced += 1
        lines.append(rest[:cut])
        rest = rest[cut:].lstrip("、")
    return [ln for ln in lines if ln], forced


# 長いカタカナ語を割るしかないときの切れ目（2026-11-14 ユーザー指示）。
# 「インディアナポリ｜ス」と1文字だけ落ちたのを「インディアナ｜ポリス」にする。
# 語の区切りが分かるものは config/kata_break_points.json に「インディアナ|ポリス」の形で足す。
def _kata_break_points() -> dict[str, int]:
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / "config" / "kata_break_points.json"
    try:
        items = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {w.replace("|", ""): w.index("|") for w in items if "|" in w}


_NO_HEAD_KATA = set("ァィゥェォャュョッヮヵヶー・")


def _forced_cut(rest: str, fit: int, font: ImageFont.FreeTypeFont, max_w: int) -> int:
    """禁則だらけで切れ目が無いときの切断位置。
    ① 辞書の語の区切りが入る範囲にあればそこ ② 残りが1行に入るなら行長をそろえる
    ③ どちらも無理なら従来どおり fit。"""
    for word, k in _kata_break_points().items():
        start = rest.find(word)
        while start >= 0:
            pos = start + k
            if 0 < pos <= fit:
                return pos
            start = rest.find(word, start + 1)
    if font.getbbox(rest[fit:])[2] <= max_w and len(rest) - fit <= 2:
        dot = rest.rfind("・", 0, fit)
        if dot >= 1 and font.getbbox(rest[dot + 1:])[2] <= max_w:
            return dot + 1                      # 「ヘンリー・｜ハドソン」
        half = (len(rest) + 1) // 2
        while half < fit and rest[half] in _NO_HEAD_KATA:
            half += 1                           # 「チャコ・キ｜ャニオン」を作らない
        if half < fit and font.getbbox(rest[half:])[2] <= max_w:
            return half
    return fit


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    return _wrap_ex(text, font, max_w)[0]


def _balance2(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str] | None:
    """2行に折るときの行長差が最小になる分割を返す（見つからなければ None）。

    _wrap_ex は1行目を max_w まで greedy に詰めるので、そのまま2行にすると
    「男が独りで積んだ千トンの / 石」のように2行目が1文字だけ残る。さらに
    切れ目の探索窓が fit-6 文字しかないため、その外側にある語境界を跨げず
    「フライング・ダッチマ / ンの伝承」とカタカナ語の途中で割れる。
    幅を狭めて呼び直すと fit 自体が前へ動き、語境界が探索窓に入る。
    """
    tw = font.getbbox(text)[2]
    if tw <= max_w:
        return None                      # 1行で入るなら折らない
    best: tuple[int, list[str]] | None = None
    for r in range(45, 80, 5):           # 全体幅の45%〜75%を1行目の目安にする
        w = min(int(tw * r / 100), max_w)
        if w < 10:
            continue
        lines, forced = _wrap_ex(text, font, w)
        if forced or len(lines) < 2:
            continue
        # 狭めた幅は「1行目をどこで折るか」を動かすためだけのもの。2行目にも
        # その幅を課すと3行に割れて候補から落ちるので、残り全体を2行目とする。
        first = lines[0]
        second = text[len(first):].lstrip("、")
        if not second:
            continue
        a, b = font.getbbox(first)[2], font.getbbox(second)[2]
        if max(a, b) > max_w:            # 片方がはみ出す分割は捨てる
            continue
        diff = abs(a - b)
        if second[0] in _PARTICLE_HEAD:  # 行頭の助詞置き去りは行長より嫌う
            diff += 400
        # 語の途中で折るくらいなら行長の偏りを取る。ただし _incomplete_end は
        # 「見出しを切り詰めた語尾」用の判定で、い段・え段を一律に未完成とみなす。
        # 助詞の「に」「で」「て」で終わるのは正しい改行位置なので _GOOD_TAIL で除外する
        # （でないと「石はどこから来て｜何のために」を罰して悪い分割を選んでしまう）。
        if (first[-1] not in _GOOD_TAIL and _incomplete_end(first)) \
                or _splits_conjugation(first, second):
            diff += 400
        if best is None or diff < best[0]:
            best = (diff, [first, second])
    return best[1] if best else None


# ショート(縦)のサムネだけ太字にする（2026-08-31・ユーザー判断）。
# NotoSansJP-Bold.ttf は名前に反して可変フォントで既定インスタンスが Thin。
# 細字だとスマホのショート棚(幅180px)で白い芯が赤フチに食われて赤い塊に見える
# （実測: 文字高62px=表示10px・下限14px割れ）。Blackにすると158px=表示26pxまで回復する。
# ロングのサムネと動画内の字幕は細字のまま（2026-08-28のユーザー判断を維持）。
_USE_BLACK = False


def _font(size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT_PATH, size)
    if _USE_BLACK:
        try:
            f.set_variation_by_name("Black")
        except Exception:
            pass          # 固定ウェイトのフォントに差し替わった場合は何もしない
    return f


def _fit_wrap(text: str, init_size: int, min_size: int, max_w: int,
              max_lines: int, balance: bool = False) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """語を切らずに max_lines 行以内へ収まるまでフォントを縮小する。
    切れない語（保護フレーズ・長カタカナ語）があればサイズを下げて1行に収める。
    balance=True かつ2行指定なら、行長が揃う分割を優先する。
    戻り値: (フォント, 行リスト, 確定サイズ)"""
    size = init_size
    while True:
        font = _font(size)
        lines, forced = _wrap_ex(text, font, max_w)
        if balance and max_lines == 2:
            bal = _balance2(text, font, max_w)
            if bal is not None:
                lines, forced = bal, 0
        if (forced == 0 and len(lines) <= max_lines) or size <= min_size:
            return font, lines, size
        size -= 8


def _draw_outlined(draw, xy, text, font, fill, outline, outline_w, shadow=True, rim=True):
    x, y = xy
    if rim and outline_w:
        # いちばん外側に黒縁。縮小したとき文字の形がつぶれずに残る
        draw.text((x, y), text, font=font, fill=fill,
                  stroke_width=outline_w + RIM_W, stroke_fill=(0, 0, 0))
    if shadow:
        # 黒のドロップシャドウ
        draw.text((x + 6, y + 6), text, font=font, fill=(0, 0, 0, 200),
                  stroke_width=outline_w, stroke_fill=(0, 0, 0, 200))
    draw.text((x, y), text, font=font, fill=fill,
              stroke_width=outline_w, stroke_fill=outline)


# --- ロング見出しの配色（2026-09-01 に調整できるようにした） ---
# 旧: 白文字＋赤フチ12px。ところがフチが太すぎて白い芯が3.3%しか残らず、
#     見た目は「赤い文字」になっていた。夕空の背景とのコントラスト比は2.58:1で、
#     読みやすさの目安4.5:1を下回っていた（実測）。
HEAD_FILL = (255, 255, 255)
# ロングの見出しだけ黄色にする（2026-09-06 サムネABCテストの結果）。
#   C 黄(255,205,24)＋赤フチ10px  42.1%  ← 勝ち
#   B 白(255,255,255)＋赤フチ7px  32.5%
#   D 黄(255,214,0)  ＋黒フチ9px  25.4%
# CとDは塗りが同じでフチの色だけが違い、17ポイント差がついた。暗い背景では
# 黒フチは背景に溶け、赤フチは色として分離する。輝度コントラストはDが最高
# (14.50:1)だったのに最下位＝輝度比だけでは背景からの分離を測れない。
# 母数259回なので確定ではない。次の数本で追試する。
# ⚠ ショートは別セッションの資産なので白のまま（HEAD_FILL）を使う。
HEAD_FILL_LONG = (255, 205, 24)
HEAD_OUTLINE = (200, 0, 0)
HEAD_OUTLINE_W = 10          # ロング用。ショートは下の分岐で常に10
SUB_OUTLINE_W = 4
# 赤フチだけだと、スマホの168px表示で白い芯と赤ハローが中間色に溶けて輪郭が消える
# （2026-08-31実測: 縮小後の明暗差31〜34。基準は40）。外周に黒縁を足して形を残す。
RIM_W = 8


def _is_kata_ch(c: str) -> bool:
    return "゠" <= c <= "ヿ"


LAST_LAYOUT: dict = {}   # 直前に作ったサムネの行（検査用）


def make_thumbnail(image_path: str, title: str, out_path: str) -> str:
    global _USE_BLACK
    W, H, is_h = _target_dims()
    _USE_BLACK = not is_h          # 縦=ショートのときだけ太字
    # 向きに応じた文字サイズ（ロングは横1280x720。大きく・上下中央配置）
    # ショート(縦)の下限90pxは、スマホのショート棚(幅180px)で表示11pxにしかならず読めない
    # （2026-08-31実測: 10本中7本が下限割れ）。表示14px＝実寸120px以上を確保する。
    head_init = 190 if is_h else 176
    head_min = 120 if is_h else 120
    sub_init = 88 if is_h else 78
    top_y = 90            # 縦（ショート）用の上寄せ開始位置
    head_outline = HEAD_OUTLINE_W if is_h else 10

    bg = Image.open(image_path).convert("RGB")
    bg = _cover(bg, W, H)

    # 全体を少し暗く＋グラデーション暗幕（文字を読みやすく）
    bg = ImageEnhance.Brightness(bg).enhance(0.68 if is_h else 0.75)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    if not is_h:
        grad_h = int(H * 0.5)
        for y in range(grad_h):
            a = int(180 * (1 - y / grad_h))
            od.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    base = Image.alpha_composite(bg.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(base)

    head, sub = _extract_headline(title)

    # 大見出しフォント確定（語を切らず2行以内・収まらなければ縮小）
    head_font, head_lines, head_size = _fit_wrap(head, head_init, head_min, W - 120, 2)
    line_h = int(head_size * 1.22)

    # サブ見出しフォント確定（語を切らず2行以内・保護フレーズは縮小して1行維持）
    sub_lines = []
    sub_size = sub_init
    sub_min2 = 62 if is_h else 54
    SUB_MIN_1LINE = 52        # ロングを1行にそろえるときの下限。これ以上は縮めない
    if sub:
        # 大きさ優先で、入らなければ2段に折り返す（2026-08-13 指摘「文字小さすぎ」）。
        # 旧実装は「帯を1本に保つ」ため1行に押し込む方向へ縮小しており、在庫ショート
        # 21本すべてが sub_init を維持できず平均51px・最小38pxまで潰れていた
        # （_fit_wrap は size-=8 のあとに下限判定するので、min 44 指定でも 38 まで落ちる）。
        # まず縮小なしで1行に入るかを見て、入らなければ2行側で大きさを取り戻す。
        if is_h:
            # ロングは赤帯を1本にそろえる。横1280あるので1行でも大きさは足りる。
            sub_font, sub_lines, sub_size = _fit_wrap(sub, sub_init, SUB_MIN_1LINE, W - 140, 1)
        else:
            sub_font, sub_lines, sub_size = _fit_wrap(sub, sub_init, sub_init, W - 140, 1)
            if len(sub_lines) > 1:
                sub_font, sub_lines, sub_size = _fit_wrap(sub, sub_init, sub_min2, W - 140, 2,
                                                          balance=True)

    # 文字ブロック総高さ
    def _block(hl, hs, sl, ss):
        g = 40 if sl else 0
        return int(hs * 1.22) * len(hl) + g + int(ss * 1.5) * len(sl)

    gap = 40 if sub_lines else 0
    total = _block(head_lines, head_size, sub_lines, sub_size)

    # ⚠ 高さが画面に収まるかを見ずに中央寄せしていたため、大見出し2行＋サブ2行に
    #   なると下がはみ出していた（2026-09-01「ピラミッドのサムネ切れてない？」）。
    #   収まるまで大見出しから縮める。サブは読ませたい情報なので後から削る。
    MARGIN = 28
    _hi, _si = head_init, sub_init
    while total > H - MARGIN * 2 and _hi > head_min:
        _hi = max(head_min, _hi - 10)
        head_font, head_lines, head_size = _fit_wrap(head, _hi, head_min, W - 120, 2)
        if sub:
            if is_h:
                sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, SUB_MIN_1LINE, W - 140, 1)
            else:
                sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, _si, W - 140, 1)
                if len(sub_lines) > 1:
                    sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, sub_min2, W - 140, 2,
                                                              balance=True)
        total = _block(head_lines, head_size, sub_lines, sub_size)
    while total > H - MARGIN * 2 and sub_lines and _si > SUB_MIN_1LINE:
        _si = max(SUB_MIN_1LINE, _si - 6)
        if is_h:
            sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, SUB_MIN_1LINE, W - 140, 1)
        else:
            sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, _si, W - 140, 1)
            if len(sub_lines) > 1:
                sub_font, sub_lines, sub_size = _fit_wrap(sub, _si, min(sub_min2, _si), W - 140, 2,
                                                          balance=True)
        total = _block(head_lines, head_size, sub_lines, sub_size)
    line_h = int(head_size * 1.22)
    gap = 40 if sub_lines else 0

    # ロングは上下中央、ショートは従来どおり上寄せ
    if is_h:
        start_y = max(30, (H - total) // 2)
        # 中央の文字帯を暗くして可読性を確保
        band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        bpad = 46
        bd.rectangle([0, start_y - bpad, W, start_y + total + bpad], fill=(0, 0, 0, 115))
        base = Image.alpha_composite(base, band)
        draw = ImageDraw.Draw(base)
    else:
        start_y = top_y

    y = start_y
    for line in head_lines:
        w = head_font.getbbox(line)[2]
        x = (W - w) // 2
        _draw_outlined(draw, (x, y), line, head_font,
                       fill=(HEAD_FILL_LONG if is_h else HEAD_FILL),
                       outline=HEAD_OUTLINE, outline_w=head_outline)
        y += line_h

    # サブ見出し（赤背景帯＋白文字）
    if sub_lines:
        y += gap
        for line in sub_lines:
            w = sub_font.getbbox(line)[2]
            x = (W - w) // 2
            pad = 28
            draw.rectangle(
                [x - pad, y, x + w + pad, y + int(sub_size * 1.34)],
                fill=(180, 0, 0, 230),
            )
            _draw_outlined(draw, (x, y + 4), line, sub_font,
                           fill=(255, 255, 255), outline=(0, 0, 0), outline_w=SUB_OUTLINE_W, shadow=False)
            y += int(sub_size * 1.5)

    # 2行目に1〜2文字だけ落ちる改行を見張る（2026-11-14「インディアナポリ｜ス」「…なかった｜軍艦」）
    LAST_LAYOUT.clear()
    LAST_LAYOUT.update(head=list(head_lines), sub=list(sub_lines))
    # サブの「…なかった｜軍艦」は読みやすいので見ない（同日ユーザー）。見出しのカタカナ語割れだけ。
    if len(head_lines) >= 2 and head_lines[-2][-1:] and _is_kata_ch(head_lines[-2][-1])             and _is_kata_ch(head_lines[-1][0]) and len(head_lines[-1]) <= 2:
        print(f"⚠ サムネ見出しのカタカナ語が割れて{len(head_lines[-1])}文字だけ残った: "
              f"{' | '.join(head_lines)} → config/kata_break_points.json に区切りを足す")

    final = base.convert("RGB")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # 2MB以内に収める
    q = 92
    final.save(out_path, "JPEG", quality=q)
    while Path(out_path).stat().st_size > 2 * 1024 * 1024 and q > 60:
        q -= 6
        final.save(out_path, "JPEG", quality=q)
    return out_path


if __name__ == "__main__":
    # 単体テスト用: python thumbnail_generator.py <image> <title> <out>
    make_thumbnail(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"✓ {sys.argv[3]}")
