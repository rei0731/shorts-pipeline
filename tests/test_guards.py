import json
from pathlib import Path

from src import comma_guard, reading_guard, subtitle_lint

ROOT = Path(__file__).resolve().parent.parent


# ---- reading_guard -------------------------------------------------------
def test_auto_fix_kana_applies_only_when_word_in_narration(monkeypatch):
    monkeypatch.setattr(reading_guard, "_WORDS", [
        {"word": "二人", "wrong": ["ににん"], "correct": ["ふたり"]},
    ])
    fixed, fixes = reading_guard.auto_fix_kana("ににんでいく", "二人で行く")
    assert fixed == "ふたりでいく" and len(fixes) == 1
    # 本文に語が無ければ誤爆しない（辞書が無関係な語を巻き込まない設計）
    fixed2, fixes2 = reading_guard.auto_fix_kana("とうはににんぎょう", "灯は人形")
    assert fixed2 == "とうはににんぎょう" and fixes2 == []


def test_reading_dict_is_well_formed():
    d = json.loads((ROOT / "config" / "reading_dict.json").read_text(encoding="utf-8"))
    assert len(d["words"]) > 100
    for e in d["words"]:
        assert e.get("word") and isinstance(e.get("correct"), list)


def test_check_numbers_accepts_correct_reading_and_flags_wrong():
    ok = reading_guard.check_numbers("3人で行く", "さんにんでいく", force=True)
    assert isinstance(ok, list)
    # 数字が本文に無ければ何も言わない
    assert reading_guard.check_numbers("人で行く", "ひとでいく") == []


def test_norm_kana_absorbs_sokuon_and_choon_variants():
    # 二十: 「にじゅう」「にじっ」どちらで書いても同じに正規化される
    assert reading_guard._norm_kana("にじゅうきろ") == reading_guard._norm_kana("にじっきろ")


# ---- comma_guard ---------------------------------------------------------
def test_long_runs_detects_unpunctuated_run():
    kana = "あ" * 25 + "、" + "い" * 5 + "。"
    runs = comma_guard.long_runs(kana, limit=20)
    assert len(runs) == 1 and runs[0][0] == 0 and len(runs[0][1]) == 25


def test_check_long_runs_thresholds():
    assert comma_guard.check_long_runs("あ" * 10 + "。") == []
    warn = comma_guard.check_long_runs("あ" * 22 + "。")
    assert len(warn) == 1 and "目安" in warn[0]
    ng = comma_guard.check_long_runs("あ" * 30 + "。")
    assert len(ng) == 1 and "誤分割" in ng[0]


def test_nakaguro_counts_as_break():
    # カタカナ人名の中黒は区間の切れ目として扱う
    assert comma_guard.long_runs("ア" * 15 + "・" + "ア" * 15, limit=20) == []


# ---- subtitle_lint -------------------------------------------------------
def test_disp_strips_commas():
    assert subtitle_lint._disp("あ、い") == "あい"


def test_lint_lines_flags_single_char_line():
    issues = subtitle_lint.lint_lines(["これは長めの行です", "あ"], 11)
    assert any("1文字だけの行" in i for i in issues)


def test_lint_lines_flags_protected_phrase_split():
    # 辞書の「口裂け女」が行をまたいだら違反
    issues = subtitle_lint.lint_lines(["夜道で口裂", "け女に会った"], 11)
    assert any("口裂け女" in i for i in issues)


def test_lint_lines_clean_input_has_no_issues():
    assert subtitle_lint.lint_lines(["静かな夜に", "風が吹いた"], 11) == []
