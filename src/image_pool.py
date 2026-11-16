# -*- coding: utf-8 -*-
"""全チャンネル共有の画像プールから、検索意図に合う画像を返す。

背景は build_image_pool.py の docstring を参照。要点だけ書くと、
Pexels の API 上限引き上げが却下された（2026-08-28）ため、
**すでに手元にある画像を使い回して API 呼び出しそのものを減らす**のが狙い。
Pexels のライセンスはダウンロード済み写真の保存・再利用・改変・商用利用を
認めているので、使い回しはライセンス上問題ない。

選び方は Pixabay 経路と同じ考え方にそろえてある。すなわち
**検索語の過半数が一致したものだけを候補にする**。
1〜2語の一致で拾うと「night が合っただけの星空」のような無関係な写真が
混ざることが実測で分かっている（2026-08-28）。

使い方:
    from src.image_pool import find_in_pool
    path = find_in_pool("A person lying awake in bed at night", used_ids=used)
    # 見つからなければ None → 呼び出し側は従来どおり API を叩く
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
from pathlib import Path

POOL = Path(os.environ.get("IMAGE_POOL_DIR", str(Path(__file__).parent.parent / "assets" / "image_pool")))
INDEX = POOL / "index.json"

_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to",
         "for", "from", "by", "into", "over", "under", "his", "her", "their",
         "its", "is", "are", "was", "were", "be", "being", "been",
         # main.py がカードごとに付け足す装飾語。検索意図ではないので数に入れない
         # （2026-08-28: これが混ざって語数が増え、過半数の条件を満たせずプールが
         #  1枚もヒットしなかった。「, subtle variation 1, focus on ...」の形で付く）
         "subtle", "variation", "focus"}

_NG_POOL_WORDS = {"thumb", "thumbs", "dislike", "disapproval", "disapprove"}

_INDEX_CACHE: dict | None = None
BLOCKED = POOL / "blocked.json"   # 全チャンネル共通の使わない写真（逆サムズアップなど・2026-10-29・channel Dが作成）


def dhash(path) -> int:
    """見た目のハッシュ（9x8 の横差分・64bit）。同じ写真を縮小・保存し直しても近い値になる"""
    from PIL import Image
    with Image.open(path) as im:
        g = im.convert("L").resize((9, 8))
    px = list(g.getdata())
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def is_blocked(path) -> bool:
    """sha1 が一致するか、見た目のハッシュが6ビット以内なら使わない写真とみなす（channel D の image_pool.py と同じ）。
    sha1 だけだと、保存し直した同じ写真を通してしまう。blocked.json が読めなければ止めずに False"""
    import hashlib
    try:
        d = json.loads(BLOCKED.read_text(encoding="utf-8"))
    except Exception:
        return False
    p = Path(path)
    if hashlib.sha1(p.read_bytes()).hexdigest() in d.get("images", {}):
        return True
    near = [int(x, 16) for x in d.get("dhash", [])]
    if not near:
        return False
    v = dhash(p)
    return any(bin(v ^ n).count("1") <= 6 for n in near)


def _keywords(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]+", (prompt or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOP]


def _load() -> dict:
    """インデックスは1プロセス内で1回だけ読む（1万件超のJSONなので毎回開かない）"""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        try:
            _INDEX_CACHE = json.loads(INDEX.read_text(encoding="utf-8")).get("images", {})
        except Exception:
            _INDEX_CACHE = {}
    return _INDEX_CACHE


def pool_size() -> int:
    return len(_load())


def find_in_pool(prompt: str, out_path: str | None = None,
                 used_ids: set | None = None) -> str | None:
    """プールから prompt に合う画像を1枚選ぶ。out_path を渡すとそこへコピーする。

    見つからなければ None を返す（呼び出し側は API へ回る）。
    used_ids には "pool:<sha1>" を積む。Pexels/Pixabay と同じ集合を共有しても
    衝突しないよう接頭辞を分けてある。
    """
    terms = _keywords(prompt)
    if not terms:
        return None
    images = _load()
    if not images:
        return None

    need = max(2, (len(terms) + 1) // 2)      # 過半数（最低2語）
    best: list[tuple[int, str, dict]] = []
    for h, e in images.items():
        if used_ids and f"pool:{h}" in used_ids:
            continue
        kws = e.get("keywords") or []
        if _NG_POOL_WORDS & set(kws):
            continue                # 10/29 thumbs up で探した写真が逆サムズアップだった（channel D で2件）。親指の写真は使わない
        score = sum(1 for t in terms if t in kws)
        if score >= min(need, len(terms)):
            best.append((score, h, e))
    if not best:
        return None

    top = max(b[0] for b in best)
    top_hits = [b for b in best if b[0] == top]
    random.shuffle(top_hits)

    for _score, h, e in top_hits[:5]:
        src = POOL / e["file"]
        if not src.exists() or is_blocked(src):
            continue
        if out_path:
            try:
                shutil.copy2(src, out_path)
            except Exception:
                continue
        if used_ids is not None:
            used_ids.add(f"pool:{h}")
        return out_path or str(src)
    return None
