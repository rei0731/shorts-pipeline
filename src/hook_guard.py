# -*- coding: utf-8 -*-
"""ショートの冒頭フックとサムネ見出しが途中で切れていないかを見る（2026-10-31）。

フックは video_builder_ffmpeg の _hook_phrase → _wrap_hook_lines(…, 10) で作られ、
2行（_HOOK_MAX_LINES）を超えた分や27字を超える1文目は「…」で切られる。
セキュリティ回11本中9本・在庫25本中2本がこれで「1284年ドイツの / 町から…」のように切れていたが、
preflight も check_claude_script も見ていなかった。

サムネ見出しは thumbnail_generator.extract_headline_ex の3つ目（切ったかどうか）で分かる。
切られても「…」は付かず、「自由の女神の約」「〜こす長さ」のように後ろに数が要る語で止まると意味が通らない。

check_claude_script.py（焼く前）と preflight_check.py（焼いた後）の両方から呼ぶ。判定はここだけに書く。
"""
import re

# 後ろに数や語が続かないと意味が通らない語。見出しがここで終わったら NG
_NEED_NUMBER = ("約", "およそ", "長さ", "高さ", "深さ", "重さ", "幅", "広さ", "大きさ", "太さ", "厚さ",
                "直径", "全長", "体長", "最大", "推定", "面積", "距離", "速さ", "温度")


def hook_problem(first_narration: str) -> str:
    """1文目からフックを作り、「…」で切れる・3行以上になるなら理由を返す（問題なければ空文字）。"""
    from src.video_builder_ffmpeg import _hook_phrase, _wrap_hook_lines, _HOOK_MAX_LINES
    h = _hook_phrase(first_narration or "")
    lines = _wrap_hook_lines(h, 10)
    if "…" in h or len(lines) > _HOOK_MAX_LINES:
        shown = " / ".join(lines[:_HOOK_MAX_LINES])
        return f"冒頭フックが切れる（「{shown}」）→ 1文目を2行（1行10字・読点を除き約20字）に収まる短文にする"
    return ""


def thumb_problem(title: str) -> tuple[str, str]:
    """(NG理由, 警告理由) を返す。見出しが切られて、後ろに数が要る語で終わるなら NG、切られただけなら警告。"""
    from src.thumbnail_generator import extract_headline_ex
    _head, sub, cut = extract_headline_ex(title or "")
    if not cut or not sub:
        return "", ""
    tail = re.sub(r"[0-9０-９.,]+$", "", sub)
    if sub.endswith(_NEED_NUMBER) or tail.endswith(_NEED_NUMBER):
        return f"サムネ見出しが「{sub}」で切れる（後ろに数が要る語で止まる）→ 題名を短くする", ""
    return "", f"サムネ見出しが途中で切られる「{sub}」→ 題名を短くするとよい"
