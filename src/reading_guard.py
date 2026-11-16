"""読み間違い（誤読）ガード

config/reading_dict.json（マスター）を唯一の情報源として:
  1. check_scene()   : narration中の危険語に対し、kanaに正しい読みが含まれるか検証
  2. auto_fix_kana() : 既知の誤り読みを正しい読みに置換（音声合成の前に適用）

導入ポイント:
  - script_generator: 校閲後・音声合成前に auto_fix → check。違反が残れば
    かな修復（1回だけGeminiに読み仮名を再依頼）→ それでもNGなら生成やり直し。
    → 誤読音声が作られる前に止める＝投稿後差し替えの根絶
  - preflight_check.py: 投稿前の最終検証（過去分・手動生成分も捕捉）

誤読事故が起きたら config/reading_dict.json に1エントリ追加するだけでよい。
"""
import json
import os
import re
import sys
from pathlib import Path

# config/ がマスター。別チャンネル側にコピーされたこのファイルも同じマスターを読む。
_MASTER = Path(os.environ.get("READING_DICT_MASTER", str(Path(__file__).parent.parent / "config" / "reading_dict.json")))
_LOCAL = Path(__file__).parent.parent / "config" / "reading_dict.json"


def _load() -> list[dict]:
    for f in (_LOCAL, _MASTER):
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8")).get("words", []) or []
            except Exception:
                continue
    return []


_WORDS: list[dict] = _load()


def auto_fix_kana(kana: str, narration: str | None = None) -> tuple[str, list[str]]:
    """既知の誤り読みを正しい読みに置換。(修正後kana, 適用した修正リスト)

    narration を渡すと「その語が本文に実在するとき」だけ置換する。
    wrong は短いかな列なので、無条件置換だと無関係な語を巻き込む（2026-08-25）:
      ・二人(ににん→ふたり) が「ぶつだん+に+にんぎょう」を『ぶつだんふたりぎょう』に
      ・背景(はいき→はいけい) が「とう+は+いきのこった」を『とうはいけいのこった』に
    どちらも かな崩れNG として在庫に出ていた。narration に 二人／背景 が無ければ
    そもそも適用しないのが正しい。"""
    fixes = []
    for e in _WORDS:
        correct = (e.get("correct") or [None])[0]
        if not correct:
            continue
        word = e.get("word", "")
        if narration is not None and word and word not in narration:
            continue        # 本文にその語が無い＝誤爆なので置換しない
        for wrong in e.get("wrong") or []:
            if wrong and wrong in kana:
                kana = kana.replace(wrong, correct)
                fixes.append(f"{wrong}→{correct}（{word}）")
    return kana, fixes


_PUNCT_RE = re.compile(r"[、。！？\s]")
# 発音表記の揺れを吸収する（narration_kana は「わたしわ」「たいおうえ」「じゅんびお」のように
# 発音どおりに書かれることがある。VOICEVOXの読みを流用した場合も同じ）。
# は/へ/を は発音どおりの表記ゆれ。ぢ/づ は じ/ず と同音なので、
# 「気づいた/きずいた」「しづ/しず」を崩れと誤検出しないようまとめて吸収する。
_PRON_MAP = str.maketrans("はへをぢづ", "わえおじず")
_HIRA_RUN_RE = re.compile(r"[ぁ-ん]{4,}")

# 長音の表記ゆれを吸収する（2026-08-25 追加）。
# narration_kana は発音どおりに「傾向→けえこお」「ようです→よおです」「一方→いっぽお」と
# 書かれることがあり、narration側の「けいこう/ようです/いっぽう」と字面が食い違って
# かな崩れNG が誤検出されていた（channel Dch 20260825_065458）。
#
# ⚠ 当初「お段＋う→お段＋お」のような文脈つきルールにしたが、これは誤り。
#   送り仮名は直前の音が漢字に隠れるため、kana側だけ変換されて不一致が増える:
#     「防いだ / ふせいだ」→ narration「防いだ」(いのまま) vs kana「ふせえだ」
#     「揃うと / そろうと」→ narration「揃うと」(うのまま) vs kana「そろおと」
#   channel A の在庫で NG 0→4 の回帰を出した（2026-08-25 検出・修正）。
# 文脈を見ずに う↔お・い↔え を同一視すれば、漢字に隠れていても両側が同じ形になる。
# 代償は「うとおの取り違え」自体を検出できないことだが、これは同音に近く実害が小さい。
_PRON_MAP2 = str.maketrans("うい", "おえ")


def _norm_pron(s: str) -> str:
    """照合用に発音表記を正規化する（表示テキストは一切変えない）。"""
    return _PUNCT_RE.sub("", s or "").translate(_PRON_MAP).translate(_PRON_MAP2)


def check_hiragana_runs(narration: str, kana: str) -> list[str]:
    """narration の「ひらがなだけが4字以上続く並び」は読みが一意なので、
    narration_kana にそのまま現れるはず。現れないなら kana が壊れている。

    Gemini が「担っていると」→「になってもとに」、「見直してみてください」→
    「みなおしてください」のように、かな側だけ別の語に化けることがある
    （全部ひらがななので他のガードを素通りする）。実測37シーンで誤検出0・
    実際の壊れ2件を検出できた（2026-08-24）。読点位置のズレは無視するため
    両方から句読点・空白を除いてから照合する。"""
    n = _norm_pron(narration)
    k = _norm_pron(kana)
    if not n or not k:
        return []
    return [f"かなに「{r}」が無い（narrationと不一致・かな崩れの疑い）"
            for r in _HIRA_RUN_RE.findall(n) if r not in k]


def check_kana_subsequence(narration: str, kana: str) -> list[str]:
    """narration のひらがなは、順序を保って narration_kana に必ず現れるはず。
    現れない文字があれば kana が別の語に化けている（2026-08-24 追加）。

    `check_hiragana_runs` は4字以上のまとまりしか見ないため、
    「未解決の問題」→「みかいけつなもんだい」、「創造性や学習」→「そうぞうせいだがくしゅう」の
    ような**1文字の助詞の化け**を取りこぼしていた。こちらは1文字単位で追う。
    実測37シーンで誤検出0・実際の壊れ2件を検出。"""
    n = _norm_pron(narration)
    k = _norm_pron(kana)
    if not n or not k:
        return []
    i = 0
    missing = []
    for ch in n:
        if not ("ぁ" <= ch <= "ん"):
            continue
        j = k.find(ch, i)
        if j < 0:
            missing.append(ch)
        else:
            i = j + 1
    if missing:
        return [f"かなに「{''.join(dict.fromkeys(missing))}」が順番どおりに無い"
                f"（narrationと不一致・かな崩れの疑い）"]
    return []


def check_scene(narration: str, kana: str) -> list[str]:
    """narration中の辞書語に対応する正しい読みがkanaに無ければ違反を返す。
    kanaは auto_fix_kana 適用後を渡すこと。"""
    issues = check_hiragana_runs(narration, kana) + check_kana_subsequence(narration, kana)
    # 読点なしの長い連続は VOICEVOX が誤分割する（2026-08-25 実測52件）。
    # 生成時は27字超だけを弾く（20字台の警告で台本を捨てない）
    #
    # ⚠ 測るのは台本の生かなではなく **VOICEVOXへ実際にわたる文字列**。
    #    `_thin_commas` が短い区切りの読点を間引くので、生かなには無い長い連続が
    #    合成側で新しく生まれる。生かなだけ見ているとそれを取りこぼす
    #    （2026-08-26 にchannel Cで発覚。生42件に対し実体163件）
    try:
        from src.comma_guard import check_long_runs, NG_RUN
        from src.voice_generator import synthesis_text
        issues += check_long_runs(synthesis_text(kana, narration), warn=NG_RUN, ng=NG_RUN)
        from src.comma_guard import check_juubun
        issues += check_juubun(narration, kana)
    except Exception:
        pass
    for e in _WORDS:
        word = e.get("word", "")
        correct = e.get("correct") or []
        if not word or not correct:
            continue
        if word in narration and not any(r in kana for r in correct):
            issues.append(
                f"「{word}」の読み（{'/'.join(correct)}）がkanaに見つからない（誤読の疑い）"
            )
    # 語そのものが別物に入れ替わっていないか（誤読ではなく「化け」）。
    # ⚠ 2026-08-26、narration「知られる話です」に対し kana「しられるつたえです」
    #    のように台本生成の時点で別語が書かれていた事故。ここまでの検査は
    #    読みの正誤しか見ないため、該当3本で検出0件のまま投稿された。
    try:
        from src.kana_drift_guard import check_drift
        issues += check_drift(narration, kana)
    except Exception:
        pass
    return issues


# 数字を1桁ずつ読むときのかな(電話番号・番号読み)。「ひと/ふた」は番号読みでは
# 使われないので入れない(入れると『いちななひとつ』のような誤りを見逃す)。
_DIGIT_KANA = {
    "0": ["ぜろ", "れい", "まる"], "1": ["いち"], "2": ["に"], "3": ["さん"],
    "4": ["よん", "し"], "5": ["ご"], "6": ["ろく"], "7": ["なな", "しち"],
    "8": ["はち"], "9": ["きゅう", "く"],
}


def _norm_kana(s: str) -> str:
    """促音・長音のゆれを吸収（じゅう/じゅっ/じっ、ひゃく/ひゃっ 等）。

    「ゅ」も落とす: 二十を『にじゅう』と『にじっ』(20キロ→にじっきろ、90分→
    きゅうじっぷん)のどちらで書いてもよいため。う/っ だけではこの2つが
    『にじゅ』『にじ』に分かれて一致しない（2026-08-05に誤検出6件の原因だった）。
    """
    return (s.replace("う", "").replace("っ", "")
             .replace("ー", "").replace("ゅ", ""))


def _digitwise_readings(num: str) -> list[str]:
    """「171」→「いちなないち」「いちしちいち」等、1桁ずつ読んだ候補。"""
    outs = [""]
    for ch in num:
        cands = _DIGIT_KANA.get(ch, [])
        if not cands:
            return []
        outs = [o + c for o in outs for c in cands]
        if len(outs) > 96:
            outs = outs[:96]
    return outs


# 位ごとの読み。音便(さんびゃく/ろっぴゃく/はっぴゃく/さんぜん/はっせん)を持つ。
_ONES = {1: ["いち"], 2: ["に"], 3: ["さん"], 4: ["よん", "よ", "し"], 5: ["ご"],
         6: ["ろく"], 7: ["なな", "しち"], 8: ["はち"], 9: ["きゅう", "く"]}
_TENS = {1: ["じゅう"], 2: ["にじゅう"], 3: ["さんじゅう"], 4: ["よんじゅう"], 5: ["ごじゅう"],
         6: ["ろくじゅう"], 7: ["ななじゅう", "しちじゅう"], 8: ["はちじゅう"], 9: ["きゅうじゅう"]}
_HUNDREDS = {1: ["ひゃく"], 2: ["にひゃく"], 3: ["さんびゃく"], 4: ["よんひゃく"], 5: ["ごひゃく"],
             6: ["ろっぴゃく"], 7: ["ななひゃく"], 8: ["はっぴゃく"], 9: ["きゅうひゃく"]}
_THOUSANDS = {1: ["せん", "いっせん"], 2: ["にせん"], 3: ["さんぜん"], 4: ["よんせん"], 5: ["ごせん"],
              6: ["ろくせん"], 7: ["ななせん"], 8: ["はっせん"], 9: ["きゅうせん"]}
# 慣用読み(数値読みの代わりに使われるもの)
_IDIOM = {10: ["とお"], 20: ["はたち", "はつか"], 24: ["にじゅうよ"]}


def _num2kana(n: int) -> list[str]:
    """整数の日本語読み候補(音便・よん/よ 等のゆれ込み)。例 1857→せんはっぴゃくごじゅうなな"""
    if n == 0:
        return ["ぜろ", "れい"]
    if n >= 100000:
        return []
    outs = [""]
    man, rest = divmod(n, 10000)
    if man:
        outs = [o + m + "まん" for o in outs for m in _ONES.get(man, [""])]
    for div, table in ((1000, _THOUSANDS), (100, _HUNDREDS), (10, _TENS)):
        d, rest = divmod(rest, div)
        if d:
            outs = [o + t for o in outs for t in table.get(d, [])]
        if len(outs) > 96:
            outs = outs[:96]
    if rest:
        outs = [o + t for o in outs for t in _ONES.get(rest, [])]
    outs.extend(_IDIOM.get(n, []))
    # 助数詞の前で起きる促音便(206個→にひゃくろっこ、300個→さんびゃっこ)。
    # 末尾の「く/ち/つ」が落ちた形も候補に入れる。
    outs.extend(o[:-1] for o in list(outs) if len(o) > 2 and o[-1] in "くちつ")
    return outs[:120]


# 「番号読み」が要求される文脈(1桁ずつ読む可能性がある文脈)。
_NUMBER_CONTEXT = re.compile(r"(電話番号|ダイヤル|ダイアル|通報|内線)")
# 「171番」のような番号表記(「24番目」は順番なので除く)
_BANGOU = re.compile(r"[0-9０-９]{2,}番(?!目)")


def check_numbers(subtitle: str, kana: str, force: bool = False) -> list[str]:
    """字幕中の「番号」が、かな(ナレーション)で正しく読まれているか検証する。

    2026-08-02: 「171番」を『いちななれい』『いちななひとつ』と書いた台本ミスが
    投稿後に発覚((動画URL省略))。辞書(漢字語→かな)では数字の誤読を
    拾えないためのガード。
    対象: 字幕に出る2桁以上の数字すべて(2026-08-05に対象を拡大。「約32倍」を
    『やくさんじゅうばい』=30倍と読む台本ミスが、番号文脈でないため素通りした)。
    判定: 1桁ずつ読み(いちなないち) か 数値読み(ひゃくななじゅういち) が含まれればOK。
    force は互換のため残しているが、いまは全数字が対象なので判定に影響しない。
    """
    issues = []
    nk = _norm_kana(kana)
    for m in re.finditer(r"[0-9０-９]+", subtitle):
        num = m.group(0).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if len(num) < 2:            # 1桁は候補が短すぎて素通りするので対象外
            continue
        cands = list(_digitwise_readings(num))
        try:
            cands.extend(_num2kana(int(num)))
        except ValueError:
            pass
        if not any(c and _norm_kana(c) in nk for c in cands):
            issues.append(
                f"数字「{num}」の読みがナレーションに見当たらない（誤読の疑い）"
            )
    return issues


def check_script_numbers(data: dict) -> list[str]:
    """script.json 全体の数字の読みを検証する。全シーン・全数字(2桁以上)が対象。

    かなは narration_kana を優先する(Gemini経路は narration が漢字まじり、
    手書き台本経路は narration 自体が全かな)。
    """
    issues = []
    for i, sc in enumerate(data.get("scenes", []) or []):
        kana = (sc.get("narration_kana") or "").strip() or (sc.get("narration") or "")
        for x in check_numbers(sc.get("subtitle", "") or "", kana):
            issues.append(f"シーン{i}: {x}")
    return issues


def check_and_fix(narration: str, kana: str) -> tuple[str, list[str], list[str]]:
    """auto_fix → check をまとめて実行。(修正後kana, 修正リスト, 残違反リスト)"""
    fixed, fixes = auto_fix_kana(kana, narration)
    issues = check_scene(narration, fixed)
    return fixed, fixes, issues


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    parser = argparse.ArgumentParser(description="読み間違いガード（script.json検証）")
    parser.add_argument("script_json")
    args = parser.parse_args()
    data = json.loads(Path(args.script_json).read_text(encoding="utf-8"))
    total = 0
    for i, sc in enumerate(data.get("scenes", [])):
        fixed, fixes, issues = check_and_fix(sc.get("narration", ""), sc.get("narration_kana", ""))
        for f in fixes:
            print(f"シーン{i}: 🔧 自動修正可: {f}")
        for x in issues:
            print(f"シーン{i}: ✗ {x}")
        total += len(issues)
    print("✅ 違反なし" if total == 0 else f"✗ {total}件の誤読疑い")
    sys.exit(1 if total else 0)
