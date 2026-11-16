"""字幕改行の独立検証リンター

改行エンジン（video_builder_ffmpeg の _wrap_chars 等）と**別実装**のルールで
実際のレンダリング結果を検査する複式簿記方式。エンジン側にバグや考慮漏れが
あっても、ここで捕まえて投稿前にブロックする（preflight_check.py から呼ばれる）。

検査対象は「エンジンが実際に出す改行」なので、シミュレーションには本物の
_subtitle_timings / _wrap_chars を使い、判定だけを独立ルールで行う。

事故が起きたら:
  1. config/no_split_phrases.json にフレーズ追加（エンジン・リンター両方に効く）
  2. ここの独立ルールにパターンを追加（同種の事故を汎化して捕まえる）
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_KATA = re.compile(r"[ァ-ヴー・]")
_KANJI = re.compile(r"[一-龥々]")
_HIRA = re.compile(r"[ぁ-ん]")

# 行頭に来てはいけない文字（小書き・長音・句読点・閉じ括弧・助動詞断片）
_NO_HEAD = set("ゃゅょぁぃぅぇぉっんー、。！？」』）】…‥・")
# 行末に来てはいけない文字（開き括弧）
_NO_TAIL = set("「『（【")

# 分断してはいけない機能語連結（行末最後の字 + 行頭先頭の字）。
# 過去事故: とい|う, でしょ|う, てい|る 等。エンジンの_bad_breakと独立に列挙する。
_BAD_BOUNDARY_BIGRAMS = {
    ("い", "う"),   # とい|う
    ("ょ", "う"),   # でしょ|う
    ("し", "ょ"),   # で・し|ょう
    ("て", "い"),   # て|いる・て|いた
    ("で", "き"),   # で|きる・で|きない
    ("だ", "ろ"),   # だ|ろう
    ("ま", "し"),   # ま|した
    ("ま", "す"),   # 〜ま|す
    ("ま", "せ"),   # ありま|せん
    ("な", "い"),   # 〜な|い（形容詞・助動詞の分断）
}


def _load_no_split_phrases() -> list[str]:
    f = Path(__file__).parent.parent / "config" / "no_split_phrases.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("phrases", []) or []
    except Exception:
        return []


_PHRASES = _load_no_split_phrases()


def _disp(s: str) -> str:
    """表示文字数評価用（読点・空白を除く）"""
    return re.sub(r"[、。\s]", "", s)


def _kanji_run_back(s: str) -> int:
    """末尾から連続する漢字の数"""
    n = 0
    for ch in reversed(s):
        if _KANJI.match(ch):
            n += 1
        else:
            break
    return n


def _kanji_run_fwd(s: str) -> int:
    """先頭から連続する漢字の数"""
    n = 0
    for ch in s:
        if _KANJI.match(ch):
            n += 1
        else:
            break
    return n


def _check_boundary(a_line: str, b_line: str,
                    warnings: list[str] | None = None) -> list[str]:
    """連続する2行の境界を検査"""
    issues = []
    at = a_line.rstrip("、 ")
    if not at or not b_line:
        return issues
    a, b = at[-1], b_line[0]
    # 読点で終わる行は意味境界なので常にOK
    if a_line.rstrip().endswith("、"):
        # ただし行頭禁則だけは読点でも許されない
        if b in _NO_HEAD:
            issues.append(f"行頭禁則文字「{b}」: 「{a_line}｜{b_line}」")
        return issues
    if _KATA.match(a) and _KATA.match(b):
        issues.append(f"カタカナ語の分断: 「…{at[-3:]}｜{b_line[:3]}…」")
    if _KANJI.match(a) and _KANJI.match(b):
        # 熟語の途中で次行頭に漢字1字が孤立する型（操｜作 / 乗客乗｜員）だけNG。
        # 「26個｜紹介し」「眠る中｜彼女は」のような接尾辞＋新単語の境界は自然→警告に留める
        right_run = _kanji_run_fwd(b_line)
        if right_run == 1:
            issues.append(f"漢字熟語の分断: 「…{at[-3:]}｜{b_line[:3]}…」")
        elif warnings is not None:
            warnings.append(f"漢字連続の行またぎ(単語境界なら可): 「…{at[-3:]}｜{b_line[:3]}…」")
    if b in _NO_HEAD:
        issues.append(f"行頭禁則文字「{b}」: 「…{at[-2:]}｜{b_line[:3]}…」")
    if a in _NO_TAIL:
        issues.append(f"行末に開き括弧「{a}」: 「…{at[-3:]}｜…」")
    if (a, b) in _BAD_BOUNDARY_BIGRAMS:
        issues.append(f"機能語の分断「{a}｜{b}」: 「…{at[-3:]}｜{b_line[:3]}…」")
    return issues


def _check_brackets(lines: list[str]) -> list[str]:
    """開き括弧と閉じ括弧が別の行に割れていないか（読点改行の例外を除く）"""
    issues = []
    pairs = {"「": "」", "『": "』", "（": "）", "【": "】"}
    for li, line in enumerate(lines):
        for op, cl in pairs.items():
            # この行で開いて閉じていない括弧がある
            opens = line.count(op)
            closes = line.count(cl)
            if opens > closes:
                # 読点で終わる行なら意味境界の分割として許容（原則5の例外）
                if not line.rstrip().endswith("、"):
                    issues.append(f"括弧「{op}」が行をまたぐ: 「{line}…」")
    return issues


# 行末に置き去りにすると次の語と切り離されて不自然な語（連体詞など）
_DANGLING_TAILS = ("その", "この", "あの", "どの", "同じ", "とある")


def lint_lines(lines: list[str], line_n: int,
               warnings: list[str] | None = None) -> list[str]:
    """レンダリング済みの行リストを検査して違反リストを返す。
    warnings リストを渡すと軽微な問題（バランスの悪い短行など）をそちらに積む。"""
    issues = []
    for i in range(len(lines) - 1):
        issues += _check_boundary(lines[i], lines[i + 1], warnings)
        # 行末が連体詞で終わる＝次の語と泣き別れ（「…過程でその｜姿を…」事故）
        at = lines[i].rstrip("、 ")
        if at.endswith(_DANGLING_TAILS) and not lines[i].rstrip().endswith("、"):
            issues.append(f"行末に連体詞の置き去り: 「…{at[-4:]}｜{lines[i+1][:3]}…」")
    issues += _check_brackets(lines)
    for line in lines:
        d = _disp(line)
        if len(d) == 1 and len(lines) > 1:
            issues.append(f"1文字だけの行: 「{line}」")
        elif len(d) == 2 and len(lines) > 1:
            # 2字行は読めるが不格好 → 警告（ブロックしない）
            if warnings is not None:
                warnings.append(f"バランスの悪い短行(2字): 「{line}」")
        if len(d) > line_n + 3:
            # 分割不能な塊（括弧引用まるごと・カタカナ/英語の連なり）は
            # 描画側オートフィット縮小が設計仕様（原則5）→ 警告に留める
            core = line.strip("、。 ")
            unsplittable = (
                (core.startswith(tuple("「『（【")) and core.endswith(tuple("」』）】")))
                # カタカナ語（中黒を含む人名も）＋末尾の助詞/助動詞。
                # 「ブルーマ・ツァイガルニックは」のように助詞が1字つくだけで
                # fullmatch が外れてNG扱いになっていた（2026-08-25）。
                # カタカナ部分は _bad_break が絶対に割らないので、実際は分割不能。
                or re.fullmatch(r"[ァ-ヴー・]{4,}[はがをにでとのもへや]{0,2}", core) is not None
                or re.fullmatch(r"[A-Za-z0-9 .\-]+[はがをにでとのもへや]{0,2}", core) is not None
            )
            if unsplittable and warnings is not None:
                warnings.append(f"縮小表示になる長い行({len(d)}字): 「{line}」")
            else:
                issues.append(f"行が長すぎ({len(d)}字 > {line_n}字+縮小余地): 「{line}」")
    # 登録フレーズが行をまたいでいないか
    joined = "".join(lines)
    for ph in _PHRASES:
        if ph in _disp(joined):
            if not any(ph in _disp(line) for line in lines):
                issues.append(f"保護フレーズ「{ph}」が行をまたいでいる")
    return issues


def _restore_commas(card_text: str, lines: list[str]) -> list[str]:
    """_wrap_chars が表示用に落とした読点を、境界判定のためだけに復元する。

    _check_boundary は「読点で終わる行＝意味境界」を正しく許可するが、
    渡される行から読点が消えているため判定できなかった。
    「幹の色や葉の形、|枝の伸び方も…」が漢字熟語の分断として誤検出されていた
    （2026-08-25）。表示テキストは変えない。"""
    out: list[str] = []
    pos = 0
    for ln in lines:
        for ch in ln:
            idx = card_text.find(ch, pos)
            if idx < 0:
                return lines          # 対応が取れない → 復元をあきらめる
            pos = idx + 1
        if card_text[pos:pos + 1] == "、":
            out.append(ln + "、")
            pos += 1
        else:
            out.append(ln)
    return out


def lint_narration(text: str, warnings: list[str] | None = None,
                   long_mode: bool = False) -> list[str]:
    """narration 1シーン分を実際の改行エンジンに通し、結果を独立ルールで検査。
    カード間の境界（カードまたぎの文節分断）も検査する。
    long_mode=True で横型ロングの画面幅（rebuild_video.py --long と同じ差し替え）で検査。"""
    import src.video_builder_ffmpeg as vb
    from src.video_builder_ffmpeg import _subtitle_timings, _wrap_chars, _scaled_line_n
    issues = []
    old_w, old_h = vb.W, vb.H
    if long_mode:
        try:
            from config.settings import LONG_VIDEO_WIDTH, LONG_VIDEO_HEIGHT
            vb.W, vb.H = LONG_VIDEO_WIDTH, LONG_VIDEO_HEIGHT
        except ImportError:
            vb.W, vb.H = 1920, 1080
    try:
        line_n = _scaled_line_n()
        cards = _subtitle_timings(text, 10.0, None)
    except Exception as e:
        vb.W, vb.H = old_w, old_h
        return [f"改行シミュレーション失敗: {e}"]
    try:
        all_cards = [c for c, _s, _e in cards]
        for card_text in all_cards:
            # 描画は救済あり(_relief)で折るので、検査も同じ条件で見る。
            # 付け忘れると実際は11字の行を19字と判定する（2026-09-17）
            lines = _wrap_chars(card_text, line_n, _relief=True)
            issues += lint_lines(_restore_commas(card_text, lines), line_n, warnings)
        # カードまたぎ: 前カード末尾が連体詞で終わる等（カード切替は表示が完全に入れ替わるため行より重症）
        for i in range(len(all_cards) - 1):
            a = all_cards[i].rstrip("、。 ")
            if a.endswith(_DANGLING_TAILS):
                issues.append(f"カード末尾に連体詞の置き去り: 「…{a[-4:]}」→次カード「{all_cards[i+1][:4]}…」")
    finally:
        vb.W, vb.H = old_w, old_h
    return issues


def lint_script(script_path: str | Path) -> dict[int, list[str]]:
    """script.json 全シーンを検査。{scene_index: [違反...]} を返す（違反なしは空dict）"""
    data = json.loads(Path(script_path).read_text(encoding="utf-8"))
    result: dict[int, list[str]] = {}
    for i, sc in enumerate(data.get("scenes", [])):
        narr = (sc.get("narration") or "").strip()
        if not narr:
            continue
        issues = lint_narration(narr)
        if issues:
            result[i] = issues
    return result


if __name__ == "__main__":
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="字幕改行リンター")
    parser.add_argument("target", nargs="?", default="", help="script.json またはテキスト")
    args = parser.parse_args()
    if args.target.endswith(".json"):
        r = lint_script(args.target)
        if not r:
            print("✅ 違反なし")
        for i, issues in r.items():
            print(f"シーン{i}:")
            for x in issues:
                print(f"  ✗ {x}")
        sys.exit(1 if r else 0)
    elif args.target:
        issues = lint_narration(args.target)
        print("\n".join(f"✗ {x}" for x in issues) or "✅ 違反なし")
        sys.exit(1 if issues else 0)
    else:
        print("usage: subtitle_lint.py <script.json | テキスト>")
