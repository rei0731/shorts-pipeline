# -*- coding: utf-8 -*-
"""narration_kana の「読点なし長文」を検出し、安全な位置に読点を補う。

読点のないひらがなが長く続くと、VOICEVOX が形態素を誤分割して抑揚と間が崩れる
（2026-08-25 実測: 投稿済み5本で52件。27〜53字の無読点区間が温床）。

⚠ 全かなを形態素解析に掛けてはいけない。janome に全かなを渡すと
   「ばあい（場合）」→「ば|あい」、「かんきょう（環境）」→「かん|きょう」と割れ、
   いま直そうとしている誤分割そのものを台本に焼き付ける（実測で適用直前に発見）。
   必ず narration（漢字かな交じり）側で文節境界を取り、particle_wa と同じ
   カーソル対応で kana 側の位置へ写像する。対応が取れなければ何もしない（安全側）。

使い方:
    from src.comma_guard import check_long_runs, add_commas
    issues = check_long_runs(kana)             # 検査（preflight/reading_guard から）
    fixed  = add_commas(narration, kana)       # 修正案（適用は人の確認後）
"""
from __future__ import annotations

import re

# 警告と NG のしきい値（読点・句点なしで続くかな文字数）
WARN_RUN = 20
NG_RUN = 27

_STOPS = "、。！？"
# 中黒はカタカナ人名の区切り。読点を打つ場所がないので、区間の切れ目として扱う
_BREAKS = _STOPS + "・"
_PUNCT = "、。！？「」『』（）【】…‥・ 　"
_JIRITSU = ("名詞", "動詞", "形容詞", "副詞", "接続詞", "連体詞", "感動詞")


def long_runs(kana: str, limit: int = WARN_RUN) -> list[tuple[int, str]]:
    """読点・句点で区切られていない区間のうち limit 字を超えるものを返す。"""
    out, start = [], 0
    for i, ch in enumerate(kana):
        if ch in _BREAKS:
            seg = kana[start:i]
            if len(seg) > limit:
                out.append((start, seg))
            start = i + 1
    seg = kana[start:]
    if len(seg) > limit:
        out.append((start, seg))
    return out


def check_long_runs(kana: str, warn: int = WARN_RUN, ng: int = NG_RUN) -> list[str]:
    """preflight / reading_guard から呼ぶ検査。NG文字列のリストを返す。"""
    msgs = []
    for _, seg in long_runs(kana, warn):
        level = "読点なしで%d字" % len(seg)
        head = seg[:24] + ("…" if len(seg) > 24 else "")
        if len(seg) > ng:
            msgs.append("%s（%d字超は誤分割の温床）: 「%s」" % (level, ng, head))
        else:
            msgs.append("%s（%d字目安を超過）: 「%s」" % (level, warn, head))
    return msgs


def _tokenizer():
    from src.particle_wa import _get_tokenizer
    return _get_tokenizer()


def kana_bunsetsu_positions(narration: str, kana: str) -> list[int] | None:
    """かな文字列上の「文節の切れ目」インデックス一覧。対応不能なら None。"""
    from src.particle_wa import _k2h
    try:
        tokens = list(_tokenizer().tokenize(narration))
    except Exception:
        return None
    chars = list(kana)
    n = len(chars)
    cursor = 0
    cuts: list[int] = []

    def skip_punct():
        nonlocal cursor
        while cursor < n and chars[cursor] in _PUNCT:
            cursor += 1

    for k, tok in enumerate(tokens):
        pos = tok.part_of_speech.split(",")[0]
        if pos == "記号":
            skip_punct()
            continue
        skip_punct()
        if cursor >= n:
            break
        reading = tok.reading if tok.reading != "*" else None
        r = _k2h(reading) if reading else None
        if r is None:
            return None                      # 読み不明（数字等）→ 諦める
        if "".join(chars[cursor:cursor + len(r)]) == r:
            cursor += len(r)
        else:
            idx = "".join(chars).find(r, cursor)
            if idx < 0 or idx - cursor > 25:
                return None                  # ずれた→諦める
            cursor = idx + len(r)
        if pos in ("助詞", "助動詞"):
            # 次が自立語のときだけ文節の切れ目とみなす。助詞が続く位置で切ると
            # 「〜など、の方法を」のように助詞が浮く（2026-08-25 実測）
            nxt = tokens[k + 1] if k + 1 < len(tokens) else None
            if nxt is None:
                continue
            nsub = nxt.part_of_speech.split(",")
            sub = tok.part_of_speech.split(",")
            # 補助動詞（〜ている/〜てしまう等）の前では切らない。POS上は動詞＝自立語
            # なので素通りし、「はたらいて、いる」と述語を割る（2026-08-25 実測）
            if tok.surface in ("て", "で") and nsub[0] == "動詞":
                continue
            # 連体化の「の」の直後で切らない。修飾とその被修飾語が離れて
            # 「〜などの、ほうほうを」のように不自然になる（2026-08-25 実測）
            if tok.surface == "の" and (len(sub) > 1 and sub[1] == "連体化"):
                continue
            # 連体修飾節と被修飾語のあいだで切らない（「あんていした、ぱたーんを」）。
            # 助動詞の直後が名詞なら、その名詞に係っている可能性が高い
            if pos == "助動詞" and nsub[0] == "名詞":
                continue
            if len(nsub) > 1 and nsub[1] == "非自立":
                continue
            if nsub[0] in _JIRITSU:
                cuts.append(cursor)
    return cuts


def add_commas(narration: str, kana: str, max_run: int = WARN_RUN,
               min_chunk: int = 7) -> str:
    """読点なしで max_run 字を超える区間に、文節境界で読点を足した kana を返す。

    位置が取れないときは元の kana をそのまま返す（安全側）。"""
    cuts = kana_bunsetsu_positions(narration, kana)
    if not cuts:
        return kana
    stops = [m.start() + 1 for m in re.finditer(r"[、。！？]", kana)]
    bounds = sorted(set([0] + stops + [len(kana)]))
    insert_at: list[int] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= max_run:
            continue
        seg_cuts = [c for c in cuts if a + min_chunk <= c <= b - min_chunk]
        if not seg_cuts:
            continue
        mid = (a + b) // 2
        insert_at.append(min(seg_cuts, key=lambda c: abs(c - mid)))
    if not insert_at:
        return kana
    out, prev = [], 0
    for p in sorted(insert_at):
        out.append(kana[prev:p])
        prev = p
    out.append(kana[prev:])
    return "、".join(x for x in out if x)


def safe_check(narration: str, kana: str) -> list[str]:
    """検査＋『安全に直せるかどうか』まで返す。直せない場合は手直しが要る。"""
    msgs = check_long_runs(kana)
    if not msgs:
        return []
    fixed = add_commas(narration, kana)
    if fixed == kana:
        msgs.append("→ 自動では直せない（文節対応が取れず）。手で読点を足すこと")
    else:
        msgs.append("→ 自動修正案: " + fixed[:60] + ("…" if len(fixed) > 60 else ""))
    return msgs


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    TESTS = [
        ("多くの性格検査は、主観的な自己申告に基づく質問紙などの方法を用いて行われます。",
         "おおくのせいかくけんさは、しゅかんてきなじこしんこくにもとづくしつもんし"
         "などのほうほうをもちいておこなわれます。"),
        ("同じ状況にいても、人によって受け止め方が異なる場合があります。",
         "おなじじょうきょうにいても、ひとによってうけとめかたがことなるばあいがあります。"),
        ("生物学的要因や環境的要因などから形成され、比較的安定したパターンを保つとされています。",
         "せいぶつがくてきよういんやかんきょうてきよういんなどからけいせいされ、"
         "ひかくてきあんていしたぱたーんをたもつとされています。"),
    ]
    for nar, kn in TESTS:
        print("narration:", nar)
        print("  before:", kn)
        print("  after :", add_commas(nar, kn))
        for m in check_long_runs(kn):
            print("  検査  :", m)
        print()


def check_juubun(narration: str, kana: str) -> list[str]:
    """「充分」を「十分」と書いた誤字を拾う（2026-08-27 ユーザー指摘）。

    かなが じゅうぶん なのに narration が「十分」だと、字幕で「10分」に化け、
    読みも じゅっぷん とぶれる。意味が「足りている」なら「充分」と書く。
    本当に10分の意味（かなが じゅっぷん / じっぷん）は対象外。
    """
    if "十分" not in narration:
        return []
    if "じゅうぶん" not in (kana or ""):
        return []
    body = narration
    # 除外1: 「不十分」は熟語として定着しており、字幕の変換ガードも効く
    body = body.replace("不十分", "")
    # 除外2: 分数の「十分の一」「五十分の一」。かなに じゅうぶん が出るが誤字ではない
    body = re.sub(r"[一二三四五六七八九十百千]*十分の", "", body)
    # 除外3: 漢数字が前に付く「五十分」「三十分」は時間。単独の「十分」だけを見る
    body = re.sub(r"[一二三四五六七八九百千]十分", "", body)
    if "十分" not in body:
        return []
    return ["「十分」は じゅうぶん の意味なら「充分」と書く"
            "（字幕で10分に化け、読みも じゅっぷん とぶれる）"]
