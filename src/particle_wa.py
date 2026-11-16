"""助詞「は/へ」の発音保証（形態素解析ベース）

VOICEVOXは純ひらがな入力だと助詞の判定を時々誤る:
  かれはそこへ → カレハ…（枯れ葉と誤解析、2026-07-04実測）
  いろはあかい → イロハ…（いろは歌と誤解析）

漢字混じりの narration を janome で形態素解析すると助詞は確実に特定できるので、
その位置情報を使って かな側の該当「は/へ」だけを発音固定の「わ/え」に置換する。

アライメント方式:
  narration のトークン読みを かな上で順番に照合してカーソルを進める。
  読みが一致しない（Geminiが別読みを書いた・数字等）場合はリシンク、
  それも失敗した文は無変換で返す（フェイルセーフ: 誤変換より無変換）。

逆方向の誤り（非助詞をワと読む: こわいはなし→コワイワナシ）はここでは直せないため、
reading_dict.json の kana_fixes に「こわいはなし→こわい話」のような
漢字置換エントリを足して対応する（VOICEVOXは漢字なら正しく解析する）。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_tokenizer = None


def _get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from janome.tokenizer import Tokenizer
        _tokenizer = Tokenizer()
    return _tokenizer


_KATA_TO_HIRA = str.maketrans(
    "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトド"
    "ナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヲンヴ",
    "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとど"
    "なにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわをんゔ",
)


def _k2h(s: str) -> str:
    return s.translate(_KATA_TO_HIRA)


def convert_particle_wa(kanji_text: str, kana: str) -> str:
    """narration(漢字)の助詞位置を根拠に、kana中の助詞「は→わ」「へ→え」を置換する。
    照合に失敗した文は元のまま返す（誤変換防止）。"""
    if not kanji_text or not kana:
        return kana
    # 文単位で処理（文の途中で照合が壊れても他の文は救う）
    kanji_sents = [s for s in re.split(r"(?<=[。！？])", kanji_text) if s]
    kana_sents = [s for s in re.split(r"(?<=[。！？])", kana) if s]
    if len(kanji_sents) != len(kana_sents):
        # 文数が合わない場合は全体を1つとして試す
        kanji_sents, kana_sents = [kanji_text], [kana]
    out = []
    for ksent, asent in zip(kanji_sents, kana_sents):
        out.append(_convert_sentence(ksent, asent))
    return "".join(out)


def _convert_by_anchor(kanji_sent: str, kana_sent: str) -> str:
    """全文アライメントが失敗した文の救済（2026-08-24 追加）。

    従来は途中で1トークンでも読みが合わないと文まるごと無変換にしていたため、
    「私たちの脳は、」の は がハのまま読まれる事故が起きた（janomeの読みと
    Geminiのかなが1語でもズレると全滅する設計だった）。

    ここでは助詞ごとに局所照合する: 助詞の直前トークンの読み r に対して、
    かな上で "r+は" が**ちょうど1か所**だけなら、その は を わ にする。
    複数箇所あるとき・読みが見つからないときは触らない（誤変換より無変換）。"""
    try:
        tokens = list(_get_tokenizer().tokenize(kanji_sent))
    except Exception:
        return kana_sent
    chars = list(kana_sent)
    for i, tok in enumerate(tokens):
        if tok.part_of_speech.split(",")[0] != "助詞" or tok.surface not in ("は", "へ"):
            continue
        prev = None
        for j in range(i - 1, -1, -1):
            r = tokens[j].reading
            if r and r != "*":
                prev = _k2h(r)
                break
        if not prev or len(prev) < 2:      # 1字の助詞等はアンカーとして弱いので使わない
            continue
        pat = prev + tok.surface
        text = "".join(chars)
        occ = [m.start() for m in re.finditer(re.escape(pat), text)]
        if len(occ) != 1:                  # 一意に決まらないなら触らない
            continue
        chars[occ[0] + len(prev)] = "わ" if tok.surface == "は" else "え"
    return "".join(chars)


def _convert_sentence(kanji_sent: str, kana_sent: str) -> str:
    try:
        tokens = list(_get_tokenizer().tokenize(kanji_sent))
    except Exception:
        return kana_sent
    chars = list(kana_sent)
    cursor = 0
    n = len(chars)

    def _skip_punct():
        nonlocal cursor
        while cursor < n and chars[cursor] in "、。！？「」『』（）【】…‥・ 　":
            cursor += 1

    # need_resync: 直前に読み不明トークン（数字等）をスキップしており、
    # かな上のカーソルが遅れている可能性がある状態
    need_resync = False

    for tok in tokens:
        pos = tok.part_of_speech.split(",")[0]
        surface = tok.surface
        reading = tok.reading if tok.reading != "*" else None

        if pos == "記号":
            _skip_punct()
            continue

        _skip_punct()
        if cursor >= n:
            break

        # 【入れないこと】接続詞・副詞・感動詞に飲み込まれた助詞「は」を わ にする分岐。
        # 2026-09-14 に channel Bから周知を受けて一度入れたが、実測して外した。
        #   ・channel Aでは は のまま渡せば VOICEVOX が自力で係助詞と解いて正しくワと読む。
        #     台本10,988場面を新旧**別プロセス**で比較 → かなが変わるのは156場面。
        #     その156場面の synthesis_text を新旧とも audio_query に通すと
        #     **153場面は mora 完全一致**＝音が変わらない。
        #   ・残る3場面は改悪2・改善1。改悪は janome が副詞1語で返す「本当は」で、
        #     ほんとうわ にすると長音が壊れる: ホ(o)ン(N)ト(o)オ(o)ワ → ホ(o)ン(N)ト(o)ウ(u)ワ。
        #   ・⚠ この「3場面」は mora 列だけの比較で、**アクセントの変化を見落としている**。
        #     channel Dが accent_phrases のアクセント値まで見て測り直したところ、
        #     249場面中 **14場面で悪化・改善0**（あるいは→(アル)(イワ) に分裂8／
        #     実は アクセント2→3 が3／ほんとうは の長音消失1／まずは・時には で
        #     わ が次語に付く各1）。害は私の数字より大きい。
        # channel Bは末尾に一括カタカナハ化があるため事情が違う。ただしあちらの最終形も
        # **わ に書き換えてはいない**。該当語の語末 は を一括ハ化の対象から外して
        # ひらがなのまま渡す方式（0914に別チャンネルが実測して採用）。別チャンネルから移植するなら
        # わ 書き換え版ではなくそちらを見ること。channel A は一括ハ化が無いので何も要らない。
        # 経緯: docs/notice_20260914_particle_wa.md
        # ⚠ 測定の罠: 同一プロセスで新旧モジュールを import すると旧が新の挙動になる。
        #    mora の部分一致で「デハ」を数えるのも不可（クレタ島で発掘 = デ+ハックツ が当たる）。

        # 助詞「は/へ」: かな側の現在位置が は/へ であることを確認して置換
        if pos == "助詞" and surface in ("は", "へ") and not need_resync:
            if chars[cursor] == "は" and surface == "は":
                chars[cursor] = "わ"
                cursor += 1
                continue
            if chars[cursor] == "へ" and surface == "へ":
                chars[cursor] = "え"
                cursor += 1
                continue
            # 既に わ/え に変換済み（読点前regex等）ならそのまま進める
            if chars[cursor] in ("わ", "え"):
                cursor += 1
                continue
            # 位置ずれ → 全文アライメントは諦め、助詞ごとの局所照合で救済する
            return _convert_by_anchor(kanji_sent, kana_sent)

        # 通常トークン: 読みでカーソルを進める
        r = _k2h(reading) if reading else None
        if r is None:
            # 数字等の読み不明トークン: かな側の進み幅が分からない。
            # スキップして次の既知読みトークンでリシンクする
            need_resync = True
            continue
        if not need_resync and "".join(chars[cursor:cursor + len(r)]) == r:
            cursor += len(r)
            continue
        # リシンク: 読み不明の後 or 読み不一致（Geminiが別読みを書いた等）。
        # 近傍でこのトークンの読みを探して復帰する
        window = 40 if need_resync else 25
        idx = "".join(chars).find(r, cursor)
        if idx >= 0 and idx - cursor <= window:
            cursor = idx + len(r)
            need_resync = False
            continue
        # リシンク不能 → 助詞ごとの局所照合で救済する
        return _convert_by_anchor(kanji_sent, kana_sent)

    return "".join(chars)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        ("彼はそこへ行きました。", "かれはそこへいきました。"),
        ("色は赤色です。", "いろはあかいろです。"),
        ("これは嘘ではありません。", "これはうそではありません。"),
        ("怖い話は本当です。", "こわいはなしはほんとうです。"),
        ("1996年、彼は消えた。", "せんきゅうひゃくきゅうじゅうろくねん、かれはきえた。"),
    ]
    for kj, kn in tests:
        print(f"{kn} → {convert_particle_wa(kj, kn)}")
