"""音声合成: gTTS または VOICEVOX → 音声ファイル + 尺/タイミング情報"""
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    TTS_ENGINE, GTTS_LANG, GTTS_TLD, GTTS_SLOW,
    VOICEVOX_URL, VOICEVOX_SPEAKER_ID, VOICEVOX_SPEED,
    VOICEVOX_INTONATION, VOICEVOX_PITCH, VOICEVOX_PAUSE_SCALE,
    VOICEVOX_REGEX_FIXES,
)


# 文頭の接続詞の直後に読点を入れて自然な間を作る（文頭/句点直後のみ・安全）
_CONNECTIVES = ["しかし", "そして", "ところが", "なんと", "じつは", "実は", "だが", "やがて", "そのとき", "その時"]
_CONNECTIVE_PATTERN = re.compile(
    r"(^|[。、！？\n])(" + "|".join(_CONNECTIVES) + r")(?![、。])"
)


@dataclass
class MoraTime:
    text: str
    start: float
    end: float


@dataclass
class VoiceResult:
    wav_path: str
    duration: float
    mora_times: list[MoraTime]


# kana誤読補正辞書（narration_kana中の誤った読みを正しい読みに置換）
_KANA_FIXES: list[tuple[str, str]] = [
    # ビジネス・日常語
    ("よろん",          "せろん"),        # 世論
    ("そうきゅう",      "さっきゅう"),    # 早急
    ("じゅうふく",      "ちょうふく"),    # 重複
    ("はっそく",        "ほっそく"),      # 発足
    ("かんつい",        "かんすい"),      # 完遂
    ("ふみしゅう",      "とうしゅう"),    # 踏襲
    # 「そうさつ→そうさい」(相殺) は 2026-09-25 に削除（ユーザー指示）。
    # 「だんそう|さつえい」(断層撮影) の途中に当たり「さいええ」と読ませていた。
    # 正しく効いた例0件・誤爆2件（投稿済みロング「アンティキティラ島の機械の謎」）。
    # 相殺の誤読は reading_dict.json の words（字幕に「相殺」があるときだけ効く）で検出できる。
    ("だいがえ",        "だいたい"),      # 代替
    ("ひとだんらく",    "いちだんらく"),  # 一段落
    # 「わりあい」→「かつあい」(割愛) は 2026-09-11 に削除。
    # 割愛より割合のほうが桁違いに多く、全リポの台本に67件あった『割合』が
    # すべてカツアイと読まれていた（channel D 20260906_071515 でユーザー指摘）。
    # reading_dict.json 側は 2026-07-03 に同じ理由で外してあったが、
    # このハードコード表のほうが先に適用されるため効いていなかった。
    # 割愛を読ませたい台本は narration_kana に「かつあい」と直接書く。
    ("じゅんじゅ",      "じゅんしゅ"),   # 遵守
    ("ねんきゅう",      "ねんぽう"),      # 年俸
    ("ことばじち",      "げんち"),        # 言質
    # 理科・自然
    ("ひかりごうせい",  "こうごうせい"),  # 光合成
    ("しゅっせいりつ",  "しゅっしょうりつ"), # 出生率
    ("でんぱん",        "でんぱ"),        # 伝播
    # 難読・雑学
    ("はんぎょう",      "はんよう"),      # 汎用
    ("ぜんだいみぶん",  "ぜんだいみもん"), # 前代未聞
    ("おりやく",        "ごりやく"),      # 御利益
    ("せきかん",        "せっけん"),      # 席巻
    ("ぎゃくりん",      "げきりん"),      # 逆鱗
    ("きょうない",      "けいだい"),      # 境内
    ("げんごどうだん",  "ごんごどうだん"), # 言語道断
    ("ごがつあめ",      "さみだれ"),      # 五月雨
    ("ときあめ",        "しぐれ"),        # 時雨
    ("たにんごと",      "ひとごと"),      # 他人事
    ("すくつ",          "そうくつ"),      # 巣窟
    ("すいちょう",      "ふいちょう"),    # 吹聴
    ("けんりつ",        "こんりゅう"),    # 建立
    ("おしいん",        "おういん"),      # 押印
    ("ゆうせつ",        "ゆうぜい"),      # 遊説
    ("ごようたつ",      "ごようたし"),    # 御用達
    ("ふうさい",        "ふさい"),        # 夫妻
    ("のなわ",          "のなは"),        # 〜の名は（名は→なわ誤読）
    ("ちとされる",      "ちちとされる"),  # 父とされる（父→ち誤読）
    ("のちであ",        "のちちであ"),    # 〜の父である
    ("のちという",      "のちちという"),  # 〜の父という
    # 外来語のかな誤り（Geminiが濁点・長音を誤るケース）
    ("だーた",          "でーた"),        # データ
    ("せきゅりてぃ",    "せきゅりてぃー"),# セキュリティ
    ("えねるぎ",        "えねるぎー"),    # エネルギー（長音抜け）
]


def _load_master_kana_fixes() -> list[tuple[str, str]]:
    """reading_dict.json（一元マスター）の kana_fixes セクションを読む。
    「いろは→いろわ」のような かな→かな の合成前置換。事故時はJSONに1行追加。"""
    f = Path(__file__).parent.parent / "config" / "reading_dict.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return [(e["wrong"], e["correct"]) for e in data.get("kana_fixes", [])
                if e.get("wrong") and e.get("correct")]
    except Exception:
        return []


_MASTER_KANA_FIXES: list[tuple[str, str]] = _load_master_kana_fixes()


def _fix_particle_wa(text: str) -> str:
    """助詞「は」「へ」の読み保証（別チャンネル側の_particles_to_phoneticと同方式）。
    純ひらがな入力ではVOICEVOXが「いろは」等を一語に誤解析して
    助詞の は を『ハ』と発音することがある。読点・文末の直前の「は/へ」は
    ほぼ確実に助詞なので、発音を直接固定する「わ/え」に置換する
    （では→でわ / とは→とわ 等も同音なので副作用なし）。
    文中の助詞は文脈が要るため kana_fixes 辞書（いろは→いろわ等）で個別対応する。"""
    text = re.sub(r"は(?=[、。！？\s])", "わ", text)
    text = re.sub(r"へ(?=[、。！？\s])", "え", text)
    text = re.sub(r"は$", "わ", text)
    text = re.sub(r"へ$", "え", text)
    return text


# ---------------------------------------------------------------------------
# 読みオラクル（2026-09-11）: かな中の「は/へ」を、漢字narrationをVOICEVOX自身に
# 読ませた結果に合わせる。
#
# なぜ必要か
#   純ひらがなを渡すと、VOICEVOXは語頭の「は」を係助詞と誤解析して ワ と読む。
#     その八、あしょか… → ソノ「ワ」チ    はくいき → 「ワ」クイキ
#     はちゅうるい → 「ワ」チュウルイ      母(はは) → 「ワワ」
#   全4chの台本51,721シーンを総当たりしたところ、かな経路の誤読780件のうち
#   1,205件(延べ)がこの型だった。語ごとに kana_fixes に足しても切りがなく、
#   「はち」のような文字列置換は「これは＋ちいさな」(ワチが正しい)を逆に壊す。
#   漢字なら助詞と語を取り違えないので、漢字経路の読みを正解にして
#   かな側の該当文字だけを直す。
#
# やること
#   K = audio_query(漢字narration) の mora 列、A = audio_query(かな) の mora 列。
#   両者を difflib で対応づけ、かなの は/へ に対応する mora が
#     A=ハ, K=ワ → は→わ（助詞だった）      A=ワ, K=ハ → は→ハ（語の一部。カタカナで固定）
#     A=ヘ, K=エ → へ→え                     A=エ, K=ヘ → へ→ヘ
#   に置き換える。触るのは は/へ だけ（漢字経路が誤読しうる他の語には手を出さない）。
#   片方を直すと隣の解析が動くことがあるので、変化がなくなるまで最大3回まわす。
#
# 失敗時は元のかなを返す（VOICEVOX未起動・対応づけ不能・例外）。
# 検査やキャッシュ判定も synthesis_text を通るので、この変換も自動で追随する。
# ---------------------------------------------------------------------------
READING_ORACLE = True
_ORACLE_MORAS = ("ハ", "ワ", "ヘ", "エ")


def _oracle_moras(text: str) -> list[str]:
    q = _audio_query(text)
    return [m["text"] for ph in q["accent_phrases"] for m in ph["moras"]]


_ORACLE_SMALL = "ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ"


def _kana_moras(kana: str):
    """かなを VOICEVOX と同じ粒度のモーラに切り、各モーラの先頭文字位置を返す。
    小書き仮名は直前に付く（しぇ=1モーラ）。VOICEVOX が「しぇ」を シ+エ に割ることがあるので、
    対応づけは文字数の一致に頼らず difflib で取る。"""
    out = []
    for i, ch in enumerate(kana):
        if ch in _ORACLE_SMALL and out:
            out[-1] = (out[-1][0] + ch, out[-1][1])
        elif re.match("[ぁ-ゔァ-ヶー]", ch):
            out.append((ch, i))
    return out


def _align(a: list[str], b: list[str]) -> dict:
    """a[i] → b[j] の対応（equal と同長 replace のみ）。"""
    import difflib
    m = {}
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            for d in range(i2 - i1):
                m[i1 + d] = j1 + d
    return m


_H2K_ORACLE = {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}


_ORACLE_EROW = set("エケセテネヘメレゲゼデベペェ")
_ORACLE_OROW = set("オコソトノホモヨロヲゴゾドボポョ")


def _norm_moras(ms: list[str]) -> list[str]:
    """長音の揺れを揃える（個数は変えない）: エ段＋イ→エ、オ段＋ウ→オ。
    漢字経路は「平気」を ヘ,エ、かな経路は エ,イ と返す。揃えないと difflib が エ同士を結び、
    へ の相手が ヘ でなく エ になって見逃す（2026-09-11 preflight で発覚）。"""
    out = list(ms)
    for i in range(1, len(out)):
        p = out[i - 1][-1] if out[i - 1] else ""
        if out[i] == "イ" and p in _ORACLE_EROW: out[i] = "エ"
        elif out[i] == "ウ" and p in _ORACLE_OROW: out[i] = "オ"
    return out


def _oracle_once(kana: str, K: list[str], allow: frozenset = frozenset()) -> str:
    """allow: テロップのひらがな「は／へ」に対応する kana の文字位置（reading_certify.particle_kana_positions）。
    「両経路がワ」の は→わ はこの位置に限る。"""
    A = _oracle_moras(kana)
    km = _kana_moras(kana)
    kata = ["".join(_H2K_ORACLE.get(c, c) for c in t) for t, _ in km]
    k2a = _align(_norm_moras(kata), _norm_moras(A))   # かなモーラ → A のモーラ
    a2k = _align(_norm_moras(A), _norm_moras(K))      # A のモーラ → K のモーラ
    out = list(kana)
    for idx, (text, pos) in enumerate(km):
        # 長音（2026-09-16）: かなの「い」「う」が、漢字経路では長音のエ/オになる位置を直す。
        # 「がくせい」→ セ+イ（イが立つ）を セ+エ（がくせー）にする。判断の基準は漢字側なので
        # 「思う」「言う」のような語尾は漢字経路でも ウ/イ のままで、書き換わらない。
        if text in ("い", "う") and _ORACLE_LONG_VOWEL:
            ai = k2a.get(idx)
            if ai is not None and ai in a2k and idx > 0:
                m, k = A[ai], K[a2k[ai]]
                prev = km[idx - 1][0][-1:]
                pk = _H2K_ORACLE.get(prev, prev)
                if text == "い" and m == "イ" and k == "エ" and pk in _ORACLE_EROW:
                    out[pos] = "え"
                elif text == "う" and m == "ウ" and k == "オ" and pk in _ORACLE_OROW:
                    out[pos] = "お"
            continue
        if text not in ("は", "へ", "ヘ"):
            continue
        ai = k2a.get(idx)
        if ai is None or ai not in a2k:
            continue
        m, k = A[ai], K[a2k[ai]]
        if text == "は" and m == "ハ" and k == "ワ":
            out[pos] = "わ"
        elif text == "は" and m == "ワ" and k == "ワ" and pos in allow:
            # 両経路がワで、テロップでもその位置がひらがなの「は」＝助詞と判断して わ に確定（2026-09-28）。
            # なぜ必要か: 読み保証（reading_certify）は合成テキストを文字どおりの目標にする。助詞を「は」と
            #   書いたままの台本（全台本走査で Claude 執筆 204 場面＋Gemini 150 場面。0927 のロングにもある）
            #   を「ハ」で確定させると、今は正しく読めている音声を直接指定で「ハ」に変えて悪化させる。
            # なぜ allow で絞るか: 「中には入れません」の かな「なかにわはいれません」は両経路がワワと読み、
            #   difflib では どちらのワが助詞か決められない。テロップで「入」（漢字）に当たる は は候補外。
            # 代償: テロップもひらがなの語「その場ではねつける」（跳ねつける）は両経路がワと誤読するので、
            #   この規則で「わ」に確定し、読み保証でも直らない（＝今と同じ）。直すにはテロップを漢字にする
            #   か辞書に入れる。「悪化しない」側を取った。
            out[pos] = "わ"
        elif text == "へ" and m == "エ" and k == "エ" and pos in allow:
            out[pos] = "え"                      # 同上（へ）
        elif text == "は" and m == "ワ" and k == "ハ":
            out[pos] = "ハ"                      # は はカタカナ1文字で固定できる（実測: ハち/ハくいき/ハわい）
        elif text == "へ" and m == "ヘ" and k == "エ":
            out[pos] = "え"
        elif text == "へ" and m == "エ" and k == "ヘ":
            # へ はカタカナ1文字「ヘ」でもエと読まれる（実測: ヘいきん→エイキン、ヘや→エヤ）。
            # 次の1モーラまでカタカナにすると直る（ヘイきん→ヘイキン、ヘヤにわ→ヘヤニワ）。
            out[pos] = "ヘ"
            if idx + 1 < len(km):
                nt, npos = km[idx + 1]
                if re.fullmatch("[ぁ-ゔ]+", nt):
                    for j, ch in enumerate(nt):
                        out[npos + j] = _H2K_ORACLE.get(ch, ch)
        elif text == "ヘ" and m == "エ" and k == "ヘ" and _ORACLE_HE_EXTEND:
            # 前の回で「ヘ＋1モーラ」をカタカナにしても、まだエと読まれている（2026-09-28）。
            #   ろくヘクたーる → ロクエ|クタアル（投稿済み「軍艦島」の冒頭）
            #   ろくヘクターる → ロクヘクタアル  ← カタカナをもう1モーラ延ばすと直る
            # カタカナの続きの次のひらがなモーラを1つカタカナにする（回すたびに1つずつ延びる）。
            for nt, npos in km[idx + 1:]:
                if re.fullmatch("[ぁ-ゔ]+", nt):
                    for j, ch in enumerate(nt):
                        out[npos + j] = _H2K_ORACLE.get(ch, ch)
                    break
                if not re.fullmatch("[ァ-ヶー]+", nt):
                    break
    return "".join(out)


def _oracle_phrases(text: str) -> tuple[list[str], set[int]]:
    """audio_query のモーラ列と、句の切れ目（句の先頭モーラの番号。最初の句は除く）。"""
    q = _audio_query(text)
    ms, cuts = [], set()
    for i, ph in enumerate(q["accent_phrases"]):
        if i:
            cuts.add(len(ms))
        ms += [m["text"] for m in ph["moras"]]
    return ms, cuts


def _guard_long_vowel_cuts(orig: str, res: str) -> str:
    """長音の補正が語を割るなら、その字を元に戻す（2026-10-31・channel Dロングの最終版 logs/eiee_guard_1031.diff と同じ）。

    「けいけん」→「けえけん」と書き換えると、VOICEVOX が「ケ／エケン」と句を切って読むことがある。
    同じ形で 冷静（レ／エセエ）・関係・傾向・整理・丁寧（テ／エネエ）・透明・設定・脳は（ノ／オワ）。
    都市伝説でも 不正（フセ／エナ）・命令（メ／エレエ）・大勢・本当は（ホント／オワ）で出た（2026-10-31 eiee_check）。

    比べる相手は、最後の結果から長音の書き換えだけを全部戻した文（base）。
    1回ずつの途中の形と比べると、「ていねいに」→「ていねえに」（テ／イネエ）のように途中で切れ目ができ、
    次の回がそれを「前からある切れ目」と見なして通してしまう（10/31 睡眠ロングで実測）。
    元のかなそのものと比べると、は→わ などほかの書き換えで切れ目が動き、割れていない「がくせえ」まで戻してしまう。
    base に無かった句の切れ目が、書き換えた字の近く（3モーラ以内）にできたら、いちばん近い
    書き換えを1つずつ戻して読み直す。切れ目ができない所（長音がイと読まれる元の問題）は、今までどおり直す。
    元から切れている所（ひらがなの「のうわ」→ノ／ウワ、「かのうせい」→カノ／ウセイ）は対象外。
    """
    if len(orig) != len(res):
        return res
    fixes = [i for i, (a, b) in enumerate(zip(orig, res)) if (a, b) in (("い", "え"), ("う", "お"))]
    if not fixes:
        return res
    base = "".join(orig[i] if i in fixes else c for i, c in enumerate(res))
    km = _kana_moras(base)
    pos2m = {pos: idx for idx, (_, pos) in enumerate(km)}
    kata = lambda s: ["".join(_H2K_ORACLE.get(c, c) for c in t) for t, _ in _kana_moras(s)]
    Ab, cb = _oracle_moras_retry(base, fn=_oracle_phrases)
    mb = _align(_norm_moras(kata(base)), _norm_moras(Ab))
    out = list(res)
    for _ in range(len(fixes)):
        cur = "".join(out)
        Aa, ca = _oracle_moras_retry(cur, fn=_oracle_phrases)
        ma = _align(_norm_moras(kata(cur)), _norm_moras(Aa))
        inv = {v: k for k, v in ma.items()}
        new = [inv[j] for j in ca if j in inv and mb.get(inv[j]) not in cb]
        live = [i for i in fixes if out[i] != orig[i] and i in pos2m]
        if not new or not live:
            break
        tgt = min(live, key=lambda i: min(abs(pos2m[i] - n) for n in new))
        if min(abs(pos2m[tgt] - n) for n in new) > 3:
            break                       # 書き換えから離れた切れ目は、この補正のせいではない
        out[tgt] = orig[tgt]
    return "".join(out)


def _oracle_distance(text: str, K: list[str]) -> int:
    """text を VOICEVOX に読ませた結果と、漢字経路の読み K とのずれ（モーラ単位の編集量）。"""
    import difflib
    A = _norm_moras(_oracle_moras_retry(text))
    Kn = _norm_moras(K)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2
               in difflib.SequenceMatcher(None, A, Kn, autojunk=False).get_opcodes() if op != "equal")


# オラクル失敗時の振る舞い（2026-09-11）
#   STRICT=True : 例外で止める。対話的な再生成向け（悪い音を1本も作らない）
#   STRICT=False: 印 READING_ORACLE_FAILED.txt をフォルダに残して合成は続ける（既定・無人の夜間ビルド向け）。
#                 preflight がこの印を NG にするので、誤読音声が投稿されることはない。
#                 「例外で落ちて誰も気づかない」を避けるための形。
READING_CERTIFY = True      # 読み保証（2026-09-28）。合成に使う音列を目標と照合し、外れたら音を直接指定
_ORACLE_LONG_VOWEL = True   # 長音の補正（2026-09-16）。新旧を比べるための切り替え口
_ORACLE_LV_CUT_GUARD = True   # 長音の補正が語を割るなら戻す（2026-10-31・08:55 入）。同上
_ORACLE_HE_EXTEND = True    # ヘのカタカナ延長（2026-09-28）。同上
_ORACLE_GUARD = True        # 直した結果が元より悪ければ採らない（2026-09-28）。同上
READING_ORACLE_STRICT = False
READING_ORACLE_MARKER = "READING_ORACLE_FAILED.txt"


def _oracle_moras_retry(text: str, tries: int = 4, fn=None):
    """VOICEVOX が混み合っていると audio_query がタイムアウトする。間を置いて再試行する。
    fn: 問い合わせの関数（既定 _oracle_moras。切れ目も要るときは _oracle_phrases）。"""
    import time as _t
    last = None
    for n in range(tries):
        try:
            return (fn or _oracle_moras)(text)
        except Exception as e:          # noqa: BLE001
            last = e
            _t.sleep(1.5 * (n + 1))
    raise RuntimeError(f"読みオラクル: audio_query が{tries}回失敗 ({type(last).__name__}: {last})")


def apply_reading_oracle(kana: str, narration: str | None) -> str:
    """漢字narrationの読みを正解に、かなの は/へ を直す。

    2026-09-11 の事故: 走査と再生成を同時に走らせて VOICEVOX が混み、問い合わせが失敗すると
    旧実装は「元のかなを返す」だけで黙っていた。その結果、再生成した32シーンのうち
    ロング側の大半が オラクル無しの誤読音声（決め話す→キメワナス 等）で焼き上がった。
    失敗は音声にせず止める（READING_ORACLE_STRICT）。検査側は例外を捕まえて警告にする。
    """
    if not READING_ORACLE or not narration or not kana or narration == kana:
        return kana
    if not re.search(r"[一-鿿ァ-ヶ]", narration):
        return kana                      # 漢字もカタカナも無い＝正解側の情報が無い
    try:
        K = _oracle_moras_retry(narration)
        from src.reading_certify import particle_kana_positions
        cur = kana
        seen = [kana]
        for _ in range(3):
            new = _oracle_once(cur, K, frozenset(particle_kana_positions(narration, cur)))
            if new == cur:
                break
            cur = new
            seen.append(cur)
        if len(seen) == 1 or not _ORACLE_GUARD:
            return _guard_long_vowel_cuts(kana, cur) if _ORACLE_LV_CUT_GUARD else cur
        # 直した結果が元より漢字の読みから遠ければ採らない（2026-09-28）。
        #   投稿済み「軍艦島」: かなのまま「たんこうわへいざん」ならヘイザンと正しく読めたのに、
        #   長音補正で「へえざん」→ へ補正で「ヘエざん」と書き換わり、実音がエ|エザンに崩れた。
        #   各回の書き換えは1モーラずつ見て決めるので、書き換えた結果の読みは確かめていない。
        #   全候補を読ませて一番近いものを使う。同点なら後の回（従来どおりの結果）。
        dist = [_oracle_distance(t, K) for t in seen]
        best = min(range(len(seen)), key=lambda i: (dist[i], -i))
        return _guard_long_vowel_cuts(kana, seen[best]) if _ORACLE_LV_CUT_GUARD else seen[best]
    except Exception as e:               # noqa: BLE001
        # 呼び出し側（_generate_voicevox）が印を残して続けるか、STRICT なら止める。
        raise RuntimeError(f"読みオラクル失敗: {e}") from e


def _apply_kana_fix(text: str, wrong: str, correct: str) -> str:
    """かな置換を1回。correct が wrong の後ろに足すだけの形（えねるぎ→えねるぎー）なら、
    もう足してある所には足さない。_KANA_FIXES は合成前に2回かかるので、素の replace だと
    「えねるぎー」→「えねるぎーーー」となり、声が「エネルギイイイ」と伸びていた（2026-10-26）。"""
    if correct.startswith(wrong) and len(correct) > len(wrong):
        return re.sub(re.escape(wrong) + "(?!" + re.escape(correct[len(wrong):]) + ")", correct, text)
    return text.replace(wrong, correct)


def _fix_reading(text: str) -> str:
    """誤読補正: kana辞書置換 + 文脈依存regex修正 + 係助詞は保証 + 文頭接続詞の後に読点挿入"""
    for wrong, correct in _KANA_FIXES:
        text = _apply_kana_fix(text, wrong, correct)
    # 一元マスター辞書（reading_dict.json の kana_fixes）
    for wrong, correct in _MASTER_KANA_FIXES:
        text = _apply_kana_fix(text, wrong, correct)
    # 文脈依存の読み修正（生え/映え等を誤爆させない正規表現）
    for pat, repl in VOICEVOX_REGEX_FIXES:
        text = re.sub(pat, repl, text)
    text = _fix_particle_wa(text)
    text = _CONNECTIVE_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}、", text)
    return text


def _voicevox_request(path: str, method: str = "GET", body: bytes | None = None) -> bytes:
    url = f"{VOICEVOX_URL}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _audio_query(text: str) -> dict:
    encoded = urllib.parse.quote(text)
    data = _voicevox_request(
        f"/audio_query?text={encoded}&speaker={VOICEVOX_SPEAKER_ID}",
        method="POST",
        body=b"",
    )
    query = json.loads(data)
    query["speedScale"] = VOICEVOX_SPEED
    query["intonationScale"] = VOICEVOX_INTONATION
    query["pitchScale"] = VOICEVOX_PITCH
    query["pauseLengthScale"] = VOICEVOX_PAUSE_SCALE
    # シーン結合時の境界無音を最小化（前後無音が積み重なると途切れ途切れに聞こえる）
    query["prePhonemeLength"] = 0.0
    query["postPhonemeLength"] = 0.05
    return query


def _synthesize(query: dict) -> bytes:
    body = json.dumps(query).encode("utf-8")
    return _voicevox_request(
        f"/synthesis?speaker={VOICEVOX_SPEAKER_ID}",
        method="POST",
        body=body,
    )


def _extract_mora_times(query: dict) -> tuple[float, list[MoraTime]]:
    mora_times: list[MoraTime] = []
    current = 0.0
    speed = query.get("speedScale", 1.0)

    for accent_phrase in query.get("accent_phrases", []):
        for mora in accent_phrase.get("moras", []):
            duration = (mora.get("consonant_length") or 0.0) + (mora.get("vowel_length") or 0.0)
            duration /= speed
            mora_times.append(MoraTime(
                text=mora.get("text", ""),
                start=current,
                end=current + duration,
            ))
            current += duration
        pause = accent_phrase.get("pause_mora")
        if pause:
            duration = ((pause.get("consonant_length") or 0.0) + (pause.get("vowel_length") or 0.0)) / speed
            mora_times.append(MoraTime(text="　", start=current, end=current + duration))
            current += duration

    return current, mora_times


def _audio_duration(path: str) -> float:
    """音声ファイルの尺を取得（wav/mp3両対応）"""
    if path.lower().endswith(".wav"):
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    # mp3等はffmpegで取得
    import imageio_ffmpeg, subprocess, re as _re
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def _generate_gtts(text: str, out_path_base: str) -> VoiceResult:
    """gTTSでMP3を生成"""
    from gtts import gTTS
    mp3_path = str(Path(out_path_base).with_suffix(".mp3"))
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)
    # gTTSは漢字混じりテキストの方が読みが正確
    tts = gTTS(text=text, lang=GTTS_LANG, tld=GTTS_TLD, slow=GTTS_SLOW)
    tts.save(mp3_path)
    duration = _audio_duration(mp3_path)
    return VoiceResult(wav_path=mp3_path, duration=duration, mora_times=[])


_ITEM_LABEL = re.compile("(その|だい)(いち|に|さん|し|よん|ご|ろく|なな|しち|はち|きゅう|く|じゅう)+$")


def _risky_join(left: str, right: str) -> bool:
    """読点を消して left+right をつなぐと VOICEVOX が語を切り直す境界か。

    2026-09-11 全4chの台本を audio_query で総当たりして見つかった型:
      その八、あしょか…      → そのはちあしょか → ソノ「ワ」チ   （は が助詞に化ける）
      その十八、ゆうれいざか  → はちゆう        → ハ「チュウ」   （ち＋ゆ が拗音に化ける）
      その十一、よつやかいだん → いちよつ        → イ「チョッ」   （ち＋よ が拗音に化ける）
    項目番号（その八、第十八 など）の直後の読点だけをのこす。数詞は語単位で照合する
    （文字クラスだと「だいじな」まで一致した）。
    """
    if not left or not right:
        return False
    # 走査で見つかった結合事故（はち＋ゆう→ハチュウ、いち＋よつ→イチョッ、よん＋いわて→ヨンユワテ）は
    # いずれも項目番号の直後だった。は/へ の境界は最終段の読みオラクルが確定するので読点では守らない
    # （守ると「けむりの、へやから」のように語頭が へ/は の普通の語まで細切れになる。在庫346シーンで確認）。
    return bool(_ITEM_LABEL.search(left))


def _thin_commas(kana: str, min_len: int = 9) -> str:
    """音声用かなの短い区切りの読点を間引く（字幕用の読点とは別に、音声が細切れになるのを防ぐ）。

    ⚠ 間引きすぎると逆に読みが壊れる。読点なしのかなが NG_RUN 字以上つづくと
       VOICEVOX が形態素を誤分割して抑揚と間が崩れるので、
       **結合した結果がその長さに達するなら読点をのこす**。
       この歯止めが無いと、台本を直しても合成側で長い連続が作られてしまう
       （台本の生テキストには出てこないので検査でも見つからない。
         2026-08-26 にchannel Cで発覚し、こちらへ展開）。
    """
    import re
    from src.comma_guard import NG_RUN
    result = []
    for sent in re.split(r"(?<=[。！？])", kana):
        if not sent:
            continue
        parts = sent.split("、")
        buf = parts[0]
        for p in parts[1:]:
            last_seg = buf.rsplit("、", 1)[-1]
            if (len(last_seg) < min_len and len(last_seg) + len(p) < NG_RUN
                    and not _risky_join(last_seg, p)):
                buf += p              # 直近区切りが短い → 読点を除去して結合
            else:
                buf += "、" + p       # 短くても、つなぐと長すぎる／境界が危険なら のこす
        result.append(buf)
    return "".join(result)


# 補助動詞の手前の読点を外す（2026-09-24）。「わけて、いる」→ ワケテ［0.36秒の無音］イル になっていた。
# channel Dロング7本で281か所。新旧を比べるための切り替え口。
_JOIN_AUX_COMMA = True
_AUX_COMMA_RE = re.compile(
    r"(?:(?<=て)|(?<=んで)|(?<=いで))、(?="
    r"い(?:る|た|て|ま[すしせ]|な[いかく])"      # ている／ていた／ていて／ています／ていない
    r"|しま[うっいえお]"                          # てしまう
    r"|み(?:る|た|ま[すしせ])"                    # てみる
    r"|お(?:く|い[たて]|き[まな])"                # ておく
    r"|い(?:く(?![つらぶさ])|き[まな]|った)"      # ていく（いくつ・いくら は除く）
    r"|く(?:る|れ)(?!ま)"                         # てくる／てくれる（くるま は除く）
    r"|あ(?:る|った|り[まな])"                    # てある
    r")")


def _join_aux_commas(s: str) -> str:
    if not _JOIN_AUX_COMMA:
        return s
    for m in reversed(list(_AUX_COMMA_RE.finditer(s))):
        i = m.start()
        left = re.split(r"[、。！？]", s[:i])[-1]
        right = re.split(r"[、。！？]", s[i + 1:])[0]
        if len(left) + len(right) <= 26:          # 27字超の連続は VOICEVOX が誤分割する
            s = s[:i] + s[i + 1:]
    return s


def synthesis_text(narration_kana: str, narration: str | None = None) -> str:
    """VOICEVOXへ実際にわたす文字列をかえす（合成はしない）。

    なぜ外に出すか
    --------------
    1) 品質検査を**合成にわたる実体**に対して行うため。台本の生かなを測ると、
       `_thin_commas` の結合で新しくできた長い連続を見のがす。
    2) ビルドの再合成判定（wavの横に置く `voice_NNN.txt`）に置く文字列にするため。
       生テキストを置くと、読み辞書や読点の間引きかたを直しても
       「変わっていない」と判定され、古い音声がのこりつづける。
    """
    # 助詞「は/へ」の発音保証: 漢字narrationの形態素解析で助詞位置を特定して わ/え 化。
    # （純ひらがな入力だと「かれは→カレハ(枯れ葉)」等の誤解析が起きる。2026-07-04実測）
    if narration:
        try:
            from src.particle_wa import convert_particle_wa
            narration_kana = convert_particle_wa(narration, narration_kana)
        except Exception:
            pass  # janome未導入等でも音声生成自体は続行（読点前regexの保険は_fix_readingにある）
    fixed = _thin_commas(_fix_reading(narration_kana))
    # 補助動詞の手前の読点を外す（わけて、いる → わけている・2026-09-24）
    fixed = _join_aux_commas(fixed)
    # 括弧は音声合成で不自然な間・読みになるため除去（字幕用narrationには残る）
    fixed = fixed.translate(str.maketrans("", "", "「」『』（）【】〈〉《》"))
    # 単語区切りの空白はVOICEVOXが間として読むため除去（読みが途切れすぎる対策）。
    # 読点(、)は意図した間として残す。
    fixed = fixed.replace("　", "").replace(" ", "")
    # 最後に読みオラクル（2026-09-11。READING_ORACLE の説明を参照）
    return apply_reading_oracle(fixed, narration)


def _generate_voicevox(narration_kana: str, out_path_base: str,
                       narration: str | None = None) -> VoiceResult:
    """VOICEVOXでWAVを生成"""
    wav_path = str(Path(out_path_base).with_suffix(".wav"))
    # 前処理は synthesis_text に一本化してある（検査・再合成判定と同じ文字列にするため）
    try:
        fixed = synthesis_text(narration_kana, narration)
    except RuntimeError as e:
        # 読みオラクル失敗。STRICT なら synthesis_text がそのまま例外を投げているのでここには来ない。
        # 既定ではフォルダに印を残し、オラクル無しの文字列で合成を続ける（投稿は preflight が止める）。
        global READING_ORACLE
        marker = Path(out_path_base).parent / READING_ORACLE_MARKER
        with open(marker, "a", encoding="utf-8") as f:
            f.write(f"{Path(out_path_base).name}: {e}" + chr(10))
        print(f"⚠ 読みオラクル失敗。{marker.name} を残してオラクル無しで合成: {Path(out_path_base).name}", file=sys.stderr)
        _prev = READING_ORACLE
        READING_ORACLE = False
        try:
            fixed = synthesis_text(narration_kana, narration)
        finally:
            READING_ORACLE = _prev
    query = _audio_query(fixed)
    # 読み保証（2026-09-28・src/reading_certify.py）。VOICEVOX が合成テキストを文字どおりに
    # 読んでいなければ、音を直接指定して読ませ直す。それでも一致しなければ例外で止める
    # （音声を作らない＝動画にならない＝投稿されない）。無人ビルドでは build_queue.ps1 が
    # その台本を FAIL として記録し、次の台本へ進む。
    read_info = None
    if READING_CERTIFY:
        from src.reading_certify import certify_query
        query, read_info = certify_query(fixed, query, narration)
    wav_data = _synthesize(query)
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
    with open(wav_path, "wb") as f:
        f.write(wav_data)
    if read_info is not None:
        # 何を目標に・どの経路で・何と読んだかの記録（監査用。voice_NN.read.json）
        with open(Path(out_path_base).with_suffix(".read.json"), "w", encoding="utf-8") as f:
            json.dump(read_info, f, ensure_ascii=False)
    _, mora_times = _extract_mora_times(query)
    # 実測値で duration を上書き（mora計算値はpre/postPhoneme無音を含まないためズレる）
    actual_duration = _audio_duration(wav_path)
    # タイミングデータをJSONに保存（リビルド時に字幕同期で使用）
    timing_path = str(Path(out_path_base).with_suffix(".timing.json"))
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump([{"text": m.text, "start": m.start, "end": m.end} for m in mora_times], f)
    return VoiceResult(wav_path=wav_path, duration=actual_duration, mora_times=mora_times)


def generate_voice(narration_kana: str, out_path: str, narration: str | None = None) -> VoiceResult:
    """設定のTTS_ENGINEに応じて音声生成。
    narration_kana: ひらがな（VOICEVOX用）
    narration: 漢字混じり（gTTS用・無ければkanaを使用）
    """
    if TTS_ENGINE == "gtts":
        text = narration if narration else narration_kana
        return _generate_gtts(text, out_path)
    return _generate_voicevox(narration_kana, out_path, narration=narration)


def check_voicevox() -> bool:
    try:
        _voicevox_request("/version")
        return True
    except Exception:
        return False


def ensure_voicevox() -> bool:
    """VOICEVOXが起動していなければ自動起動し、API応答可能になるまで待つ。
    生成系スクリプト(main/regen_voices)は音声生成の前に必ずこれを呼ぶ。
    """
    import os
    import time
    import subprocess

    if check_voicevox():
        print("✓ VOICEVOX 起動確認OK")
        return True

    try:
        from config.settings import VOICEVOX_EXE_CANDIDATES, VOICEVOX_BOOT_TIMEOUT
    except Exception:
        VOICEVOX_EXE_CANDIDATES, VOICEVOX_BOOT_TIMEOUT = [], 120

    exe = next((p for p in VOICEVOX_EXE_CANDIDATES if p and os.path.exists(p)), None)
    if not exe:
        print("✗ VOICEVOXが起動しておらず、実行ファイルも見つかりません。"
              " 手動で起動するか settings.VOICEVOX_EXE_CANDIDATES にパスを追加してください。")
        return False

    print(f"… VOICEVOX未起動。自動起動します: {exe}")
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([exe], creationflags=flags, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        print(f"✗ VOICEVOX自動起動に失敗: {e}")
        return False

    deadline = time.time() + VOICEVOX_BOOT_TIMEOUT
    while time.time() < deadline:
        time.sleep(2)
        if check_voicevox():
            print("✓ VOICEVOX 起動完了（自動起動）")
            return True
    print(f"✗ VOICEVOX起動待機タイムアウト（{VOICEVOX_BOOT_TIMEOUT}秒）")
    return False
