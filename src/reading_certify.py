# -*- coding: utf-8 -*-
"""読み保証（2026-09-28）: VOICEVOX が合成テキストを文字どおりに読んだことを、合成に使う実体で保証する。

なぜ必要か
----------
投稿済みショート「軍艦島」で「へくたーる→エクタアル」「へいざん→エエザン」、「仁徳天皇陵」で
「はいれません→ワイレマセン」が起きた。台本の検査・合成テキストの検査・読みオラクルはすべて
「VOICEVOX に渡す前」を見ており、VOICEVOX が実際に何と読んだかは誰も見ていなかった。

仕組み
------
1. 合成テキスト（synthesis_text の結果）から「目標の音列」を機械的に作る。
   ひらがな・カタカナはそのまま音になる。漢字は読み辞書（kana_fixes）が読みを固定するために
   入れたものだけなので、辞書を逆引きして元のかなに戻す（辞書外の漢字が残れば止める）。
2. 通常経路（文章→形態素解析→音）の audio_query の音列が目標と一致すれば、それをそのまま使う
   （抑揚が自然）。
3. 一致しなければ、AquesTalk風記法（/accent_phrases?is_kana=true）で**音を直接指定**して
   読ませ直す。この経路は形態素解析を通らないので「は→ワ」「へ→エ」の読み替えが起きない。
   抑揚は、通常経路の結果（合成テキスト経路・漢字テロップ経路のうち目標に近いほう）から写す。
4. 直接指定の結果の音列を目標と照合し、一致しなければ例外で止める（音声は作らない）。
5. 合成に使った音列は voice_NN.timing.json に残るので、preflight が焼き上がり後にもう一度
   目標と突き合わせる（preflight_check.py の 3.56）。

「一致」の定義（_norm）
----------------------
長音の書き方（エ段＋イ／エ段＋エ、オ段＋ウ／オ段＋オ）は同じ音とみなす。漢字経路は「平気」を
ヘ,エ と返し、かな経路は エ,イ と返すため（2026-09-16 の長音の知見）。ヲ/オ・ヂ/ジ・ヅ/ズ も同一視。
"""
from __future__ import annotations

import difflib
import json
import re
import urllib.parse

READING_CERTIFY = True          # 切り替え口（新旧比較用）。False で 0928 以前の挙動
ENGINE_TAG = "certify-0928"

_SMALL = set("ァィゥェォャュョヮ")
_ROWS = {
    "ア": "アカサタナハマヤラワガザダバパァャヮ",
    "イ": "イキシチニヒミリギジヂビピィ",
    "ウ": "ウクスツヌフムユルグズヅブプゥュヴ",
    "エ": "エケセテネヘメレゲゼデベペェ",
    "オ": "オコソトノホモヨロヲゴゾドボポォョ",
}
_VOWEL = {c: v for v, cs in _ROWS.items() for c in cs}
_VOWEL["ン"] = "ン"
_H2K = {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}      # ぁ-ゖ → ァ-ヶ（ゔ→ヴ も同じ差）
_SAME = {"ヲ": "オ", "ヂ": "ジ", "ヅ": "ズ"}
_KANJI = re.compile(r"[一-鿿々]")


class ReadingCertifyError(RuntimeError):
    """目標の音列どおりに読ませられなかった。音声は作らない。"""


def vowel_of(mora: str) -> str:
    return _VOWEL.get(mora[-1], "")


def split_moras(kata: str) -> list[str]:
    """カタカナ文字列をモーラに切る。長音「ー」は直前の母音に置き換える（記法にーは無い）。
    かな以外（読点など）は捨てる。"""
    out: list[str] = []
    for ch in kata:
        if ch in _SMALL and out:
            out[-1] += ch
        elif ch == "ー":
            if out and vowel_of(out[-1]):
                out.append(vowel_of(out[-1]))
        elif "ァ" <= ch <= "ヶ":
            out.append(ch)
    return out


def _norm(moras: list[str]) -> list[str]:
    out: list[str] = []
    for m in moras:
        m = _SAME.get(m, m)
        if out:
            pv = vowel_of(out[-1])
            if m == "イ" and pv == "エ":
                m = "エ"
            elif m == "ウ" and pv == "オ":
                m = "オ"
            elif m == "ウ" and out[-1] == "イ":
                # 「そういう／という」を VOICEVOX は ソオユウ／トユウ と読む。自然な発音なので同じ音とみなす
                # （全台本走査で150件。文字どおり イウ を強いると不自然になる）
                out[-1] = "ユ"
        out.append(m)
    return out


_ALLOWED = re.compile(r"[ぁ-ゖァ-ヶー、。！？!?,.\s　「」『』（）()〜…・]")


def _check_chars(text: str) -> None:
    """かな以外が残っていたら止める。数字・英字は目標の音列に入れられず、黙って落とすと音声から
    その語が消える（古い台本の「1970年代」がかなに残っていた例）。"""
    bad = "".join(dict.fromkeys(c for c in text if not _ALLOWED.match(c)))
    if bad:
        raise ReadingCertifyError(f"合成テキストにかな以外の文字がある（読みを保証できない）: 「{bad}」 in {text}")


def _reverse_kanji_map() -> list[tuple[str, str]]:
    """辞書が入れた漢字（読み固定用）を元のかなに戻す表。長いものから当てる。"""
    from src import voice_generator as V
    pairs = []
    for wrong, correct in list(V._KANA_FIXES) + list(V._MASTER_KANA_FIXES):
        if _KANJI.search(correct):
            pairs.append((correct, wrong))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def intent_text(synth: str) -> str:
    """合成テキストの漢字（辞書が読み固定のために入れたもの）を元のかなに戻した文字列。
    目標の音列と、テロップとの文字対応はこの文字列を座標にする。"""
    text = synth
    if _KANJI.search(text):
        for kanji, kana in _reverse_kanji_map():
            text = text.replace(kanji, kana)
        if _KANJI.search(text):
            raise ReadingCertifyError(f"辞書で戻せない漢字が合成テキストに残っている: {text}")
    _check_chars(text)
    return text


def split_moras_pos(kata: str) -> list[tuple[str, int]]:
    """split_moras と同じ切り方で、各モーラの先頭文字位置も返す。"""
    out: list[tuple[str, int]] = []
    for i, ch in enumerate(kata):
        if ch in _SMALL and out:
            out[-1] = (out[-1][0] + ch, out[-1][1])
        elif ch == "ー":
            if out and vowel_of(out[-1][0]):
                out.append((vowel_of(out[-1][0]), i))
        elif "ァ" <= ch <= "ヶ":
            out.append((ch, i))
    return out


def target_moras(synth: str) -> list[str]:
    """合成テキスト → 目標の音列。辞書外の漢字が残れば例外。"""
    text = intent_text(synth)
    kata = "".join(_H2K.get(c, c) for c in text)
    return split_moras(kata)


# --- 助詞「は／へ」の扱い ---------------------------------------------------------------
# 台本の規則では助詞は「わ／え」と書くが、助詞を「は」と書いたままの台本が実在する（全台本走査で
# Claude 執筆 204 場面・0927 のロングにも）。そこを文字どおり「ハ」で確定させると、今は正しく読めて
# いる音声を「ハ」に変えて悪化させる。逆に「中には入れません」の「入」の は を助詞扱いすると
# 「ナカニワワイレマセン」を正しいと認めてしまう（今日の事故そのもの）。
# 音の並び（difflib）では「ワワ」のどちらが助詞か決められないので、**テロップの文字で決める**:
# テロップのひらがな「は／へ」に対応する かな位置だけを助詞候補とし、そこに限って通常経路の
# ワ／エ を受け入れる。「入れません」の は はテロップでは漢字なので候補にならない。
_PUNCT = set("、。！？!?,.・…〜「」『』（）()　 \n\t")


def _strip_punct_map(s: str) -> tuple[str, list[int]]:
    chars, idx = [], []
    for i, c in enumerate(s):
        if c in _PUNCT:
            continue
        chars.append(c)
        idx.append(i)
    return "".join(chars), idx


def particle_kana_positions(narration: str | None, kana: str) -> set[int]:
    """テロップのひらがな「は／へ」に対応する、kana 側の文字位置の集合。
    ひらがなを錨にし、漢字・カタカナ・数字は任意長として突き合わせる。対応が取れなければ空集合
    （＝どこも助詞扱いしない＝文字どおり）。オラクルが書き換えた形（は→ハ、い→え、う→お、
    へ＋次モーラのカタカナ化）も同じ文字とみなす。"""
    if not narration or not kana:
        return set()
    n, _ = _strip_punct_map(narration)
    k, kidx = _strip_punct_map(kana)
    if len(k) > 600:
        return set()
    pat, groups, wild = [], 0, False
    for c in n:
        if c == "は":
            pat.append("([はわハ])"); groups += 1; wild = False
        elif c == "へ":
            pat.append("([へえヘ])"); groups += 1; wild = False
        elif "ぁ" <= c <= "ゖ":
            pat.append(_hira_class(c))
            wild = False
        else:
            if not wild:
                pat.append(".+?")
                wild = True
    if not groups or pat.count(".+?") > 60:
        return set()
    rx = "".join(pat)
    try:
        m = re.fullmatch(rx, k) or re.match(rx, k)
        # 最短一致（.+?）は漢字の読みが「は」で終わる語で手前にずれる（「母は」→ はは の2字目を助詞に）。
        # 最長一致（.+）でも同じ位置になる助詞だけ採用し、食い違う位置は候補にしない。
        m2 = None
        if m:
            rx2 = rx.replace(".+?", ".+")
            m2 = re.fullmatch(rx2, k) if m.end() == len(k) else re.match(rx2, k)
    except re.error:
        m = m2 = None
    out = set()
    undecided = set()                     # 最短・最長で食い違った助詞（テロップ側の文字位置）
    if m:
        gi = 0
        for i, c in enumerate(n):
            if c not in "はへ":
                continue
            gi += 1
            if m.start(gi) < 0:
                continue
            if m2 is None or m.start(gi) == m2.start(gi):
                out.add(kidx[m.start(gi)])
            else:
                undecided.add(i)
        if not undecided:
            return out
    # 文全体の対応が取れない（かなとテロップの語が違う・かなの助詞抜け 等）とき、または
    # 最短・最長で食い違った助詞は、前後のひらがなだけで局所照合する。前後合わせて2文字以上あり、
    # かな側に1か所だけ見つかるときに限る（「弱さではなく」「今ではたやすく」「なゐは大地」を救う）。
    for i, c in enumerate(n):
        if c not in "はへ" or (m and i not in undecided):
            continue
        pre = []
        j = i - 1
        while j >= 0 and "ぁ" <= n[j] <= "ゖ" and n[j] not in "はへ" and len(pre) < 3:
            pre.insert(0, n[j]); j -= 1
        post = []
        j = i + 1
        while j < len(n) and "ぁ" <= n[j] <= "ゖ" and n[j] not in "はへ" and len(post) < 2:
            post.append(n[j]); j += 1
        if len(pre) + len(post) < 2:
            continue
        rx = "".join(_hira_class(x) for x in pre) + ("([はわハ])" if c == "は" else "([へえヘ])") \
            + "".join(_hira_class(x) for x in post)
        hits = [mm.start(1) for mm in re.finditer(rx, k)]
        if len(hits) == 1:
            out.add(kidx[hits[0]])
    return out


_HIRA_ALT = {"い": "えエ", "う": "おオ", "ゐ": "いイ", "ゑ": "えエ", "づ": "ずズ", "ぢ": "じジ", "を": "おオ"}


def _hira_class(c: str) -> str:
    """テロップのひらがな1字に対応する、かな側の許容文字クラス（オラクルの書き換え・旧仮名・揺れ込み）。"""
    alts = {c, _H2K.get(c, c)} | set(_HIRA_ALT.get(c, ""))
    return "[" + "".join(re.escape(a) for a in sorted(alts)) + "]"


def relax_particles(target: list[str], actual: list[str], synth: str,
                    narration: str | None) -> tuple[list[str], list[int]]:
    """助詞候補の位置で、目標 ハ／ヘ と実音 ワ／エ が向き合っていれば、実音側を受け入れる。
    戻り値: (直した目標, 受け入れたモーラ番号)。"""
    if not narration:
        return target, []
    text = intent_text(synth)
    pos = particle_kana_positions(narration, text)
    if not pos:
        return target, []
    mp = split_moras_pos("".join(_H2K.get(c, c) for c in text))
    if len(mp) != len(target):
        return target, []
    t2a = _align(_norm(target), _norm(actual))
    new, relaxed = list(target), []
    for mi, (_m, ci) in enumerate(mp):
        if ci in pos and target[mi] in ("ハ", "ヘ") and mi in t2a:
            a = actual[t2a[mi]]
            if (target[mi], a) in {("ハ", "ワ"), ("ヘ", "エ")}:
                new[mi] = a
                relaxed.append(mi)
    return new, relaxed


def _align(a: list[str], b: list[str]) -> dict:
    m = {}
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            for d in range(i2 - i1):
                m[i1 + d] = j1 + d
    return m


def query_moras(query_or_phrases) -> list[str]:
    phrases = query_or_phrases["accent_phrases"] if isinstance(query_or_phrases, dict) else query_or_phrases
    return [m["text"] for ap in phrases for m in ap["moras"]]


def _distance(a: list[str], b: list[str]) -> int:
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2
               in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != "equal")


def build_notation(phrases: list[dict], target: list[str], interrogative: bool) -> str:
    """骨組み（通常経路の accent_phrases）の音を目標に合わせて差し替え、AquesTalk風記法にする。
    句の切れ目・アクセント核・間・無声化は骨組みから写す。"""
    flat = [(pi, m) for pi, ap in enumerate(phrases) for m in ap["moras"]]
    Mn = _norm([m["text"] for _, m in flat])
    Tn = _norm(target)
    assign: list[tuple[int, dict | None]] = []          # 目標モーラごとに (句番号, 写す元のモーラ)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, Mn, Tn, autojunk=False).get_opcodes():
        if op == "equal":
            for d in range(i2 - i1):
                assign.append((flat[i1 + d][0], flat[i1 + d][1]))
        elif op == "delete":
            continue
        else:                                            # replace / insert
            if op == "replace" and (i2 - i1) == (j2 - j1):
                for d in range(i2 - i1):
                    assign.append((flat[i1 + d][0], None))
                continue
            if flat:
                pi = flat[min(i1, len(flat) - 1)][0] if i1 < len(flat) else flat[-1][0]
            else:
                pi = 0
            for _ in range(j1, j2):
                assign.append((pi, None))
    assert len(assign) == len(target), (len(assign), len(target))
    # 句ごとにまとめる
    groups: dict[int, list[tuple[str, bool]]] = {}
    for (pi, src), t in zip(assign, target):
        # 骨組みと同じ音（_norm で一致）なら骨組みの書き方を写す。長音は「ドオ」「ヘエ」の形で
        # 書いたほうが自然で、かなの「ドウ」「ヘイ」を文字どおり読ませると ウ／イ が立つ（2026-09-16）。
        text = src["text"] if src else t
        unvoiced = bool(src) and src.get("vowel", "") in "AIUEO"
        groups.setdefault(pi, []).append((text, unvoiced))
    parts = []
    idxs = sorted(groups)
    for n, pi in enumerate(idxs):
        ms = groups[pi]
        acc = phrases[pi]["accent"] if pi < len(phrases) else 1
        acc = max(1, min(int(acc), len(ms)))
        s = ""
        for k, (t, unv) in enumerate(ms, 1):
            s += ("_" if unv else "") + t
            if k == acc:
                s += "'"
        sep = ""
        if n < len(idxs) - 1:
            sep = "、" if (pi < len(phrases) and phrases[pi].get("pause_mora")) else "/"
        parts.append(s + sep)
    notation = "".join(parts)
    if interrogative:
        notation += "？"
    return notation


def accent_phrases_kana(notation: str, tries: int = 4) -> list[dict]:
    """記法の誤り（400）は即座に返す。通信の失敗（VOICEVOX が混んでいる等）は間を置いて再試行。"""
    import time
    import urllib.error
    from src import voice_generator as V
    q = urllib.parse.urlencode({"text": notation, "speaker": V.VOICEVOX_SPEAKER_ID, "is_kana": "true"})
    last = None
    for n in range(tries):
        try:
            return json.loads(V._voicevox_request(f"/accent_phrases?{q}", method="POST", body=b""))
        except urllib.error.HTTPError:
            raise
        except Exception as e:                                # noqa: BLE001
            last = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"accent_phrases が{tries}回失敗 ({type(last).__name__}: {last})")


def _diff_text(target: list[str], actual: list[str]) -> str:
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, target, actual, autojunk=False).get_opcodes():
        if op != "equal":
            out.append(f"目標「{''.join(target[i1:i2])}」→実音「{''.join(actual[j1:j2])}」")
    return " / ".join(out) or "（差なし）"


def certify_query(synth: str, query: dict, narration: str | None = None) -> tuple[dict, dict]:
    """合成に使う audio_query を、目標の音列どおりのものにして返す。

    戻り値: (query, info)。info["route"] は "text"（通常経路のまま）か "kana"（音を直接指定）。
    どうしても一致しなければ ReadingCertifyError。
    """
    from src import voice_generator as V
    target = target_moras(synth)
    info = {"engine": ENGINE_TAG, "synth": synth, "target": target}
    if not target:
        info.update(route="text", actual=query_moras(query))
        return query, info
    actual = query_moras(query)
    if _norm(actual) != _norm(target):
        target, relaxed = relax_particles(target, actual, synth, narration)
        if relaxed:
            info.update(target=target, relaxed=relaxed)
    if _norm(actual) == _norm(target):
        info.update(route="text", actual=actual)
        return query, info

    # --- 直接指定 ---
    # 骨組みは、合成テキスト経路と漢字テロップ経路のうち目標に近いほう（抑揚の写し元）
    # 同点なら漢字テロップ経路（語のまとまりで句が切れる。「ナカニワ/ハイレマセン」）を優先する。
    skeletons = []
    if narration and re.search(r"[一-鿿ァ-ヶ]", narration):
        try:
            skeletons.append(("narration", V._audio_query(narration)["accent_phrases"]))
        except Exception:
            pass
    skeletons.append(("synth", query["accent_phrases"]))
    Tn = _norm(target)
    name, phrases = min(skeletons, key=lambda s: _distance(_norm(query_moras(s[1])), Tn))
    interrogative = synth.rstrip("。」』）").endswith(("？", "?"))
    notation = build_notation(phrases, target, interrogative)
    try:
        new_phrases = accent_phrases_kana(notation)
    except Exception as e:                                   # noqa: BLE001
        raise ReadingCertifyError(f"音の直接指定を VOICEVOX が受け付けない: {notation} ({e})") from e
    got = query_moras(new_phrases)
    if _norm(got) != Tn:
        raise ReadingCertifyError(
            f"直接指定でも目標どおりに読めない: {_diff_text(Tn, _norm(got))} 記法={notation}")
    q2 = dict(query)
    q2["accent_phrases"] = new_phrases
    q2["kana"] = notation
    info.update(route="kana", actual=got, notation=notation, skeleton=name,
                text_route_actual=actual, text_route_diff=_diff_text(Tn, _norm(actual)))
    return q2, info


def verify_timing(synth: str, timing: list[dict], narration: str | None = None) -> tuple[bool, str]:
    """焼き上がり後の照合: voice_NN.timing.json のモーラ（間「　」を除く）と目標を比べる。
    差分の表示も _norm 後で行う（ヲ/オ・ヅ/ズ・長音の揺れは差として出さない）。
    narration を渡すと、テロップの助詞「は／へ」に対応する位置の ワ／エ は受け入れる（relax_particles）。"""
    target = target_moras(synth)
    actual = [m["text"] for m in timing if m.get("text") and m["text"] != "　"]
    if _norm(actual) != _norm(target):
        target, _ = relax_particles(target, actual, synth, narration)
    Tn, An = _norm(target), _norm(actual)
    ok = An == Tn
    return ok, "" if ok else _diff_text(Tn, An)


if __name__ == "__main__":
    # 自己テスト: 投稿事故の4場面と、記法の端。VOICEVOX 起動が必要。
    import os
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import voice_generator as V
    cases = [
        ("6ヘクタールの島に、5000人が暮らした。", "ろくへくたーるのしまに、ごせんにんがくらした。"),
        ("1974年、炭鉱は閉山します。", "せんきゅうひゃくななじゅうよねん、たんこうわへいざんします。"),
        ("調べようにも、中には入れません。", "しらべようにも、なかにわはいれません。"),
        ("洞窟は今、一般の人は入れません。", "どうくつわいま、いっぱんのひとわはいれません。"),
        ("多数派と話す。反対派は平方メートルで測った。", "たすうはとはなす。はんたいはわへいほうめーとるではかった。"),
        ("あなたはどう思いますか？", "あなたわどうおもいますか？"),
        ("ヴィレンドルフのヴィーナスは、11センチ。", "ゔぃれんどるふのゔぃーなすわ、じゅういっせんち。"),
        ("断層撮影で中を見た。", "だんそうさつえいでなかをみた。"),
        ("その八、飛鳥の羽衣。", "そのはち、あすかのはごろも。"),
    ]
    cases += [
        ("ペルセウスは盾を鏡にして、その首をはね落としました。", "ぺるせうすはたてをかがみにして、そのくびをはねおとしました。"),
        ("パンドは雄の木で、かつては種でも増えた。", "ぱんどはおすのきで、かつてはたねでもふえた。"),
        ("だらしないから、ではありません。", "だらしないからではありません。"),
        ("夜な夜な現れては人々を脅かした。", "よなよなあらわれてはひとびとをおびやかした。"),
        ("その場ではねつけることで。", "そのばではねつけることで。"),
        ("母は、初めて箱を開けた。", "ははわ、はじめてはこをあけた。"),
        ("中には入れません。中には入れません。", "なかにわはいれません。なかにはいれません。"),
    ]
    for narr, kana in cases:
        synth = V.synthesis_text(kana, narr)
        q = V._audio_query(synth)
        try:
            q2, info = certify_query(synth, q, narr)
            print(f"[{info['route']:4}] {narr}\n       合成 {synth}\n       目標 {''.join(info['target'])}{'  受入' + str(info['relaxed']) if info.get('relaxed') else ''}\n       実音 {''.join(info['actual'])}"
                  + (f"\n       記法 {info['notation']}  (骨組み={info['skeleton']}, 通常経路: {info['text_route_diff']})" if info["route"] == "kana" else ""))
        except ReadingCertifyError as e:
            print(f"[FAIL] {narr}\n       {e}")
