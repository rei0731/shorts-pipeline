# -*- coding: utf-8 -*-
"""実物の写真を Wikimedia Commons から探して取り込む（2026-09-29）。

なぜ必要か
  ダイオウイカの動画に「いい加減な画像ばかり」というコメント。Pexels は汎用のストック写真しか
  無いので、遺物・遺跡・実在の生物のような「実物がある題材」では内容と合わない画像になる。

やること
  1. Commons を検索し、権利が明確な画像だけを候補にする（パブリックドメイン／CC0／CC BY を優先、
     CC BY-SA は最後の手段。それ以外＝候補にしない）
  2. 作者・ライセンス・URL を記録する（説明欄の「画像出典」に使う。ユーザー判断: 画面焼き込みは不要）
  3. 縦動画用に整える: 横写真をそのまま切り抜くと被写体が切れるので、ぼかした背景の上に写真全体を収める

使い方
  from src.image_sources import search_commons, prepare_portrait
  cands = search_commons(["Kamo Iwakura dotaku", "銅鐸"], limit=30)
  for c in cands: download → prepare_portrait(src, dst)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
UA = "shorts-pipeline/1.0 (contact: contact@example.com) python-urllib"

# 許可するライセンス（LicenseShortName の前方一致）。順番＝優先度
LICENSE_RANK = [
    ("Public domain", 0), ("CC0", 0), ("PDM", 0),
    ("CC BY 1.0", 1), ("CC BY 2.0", 1), ("CC BY 2.5", 1), ("CC BY 3.0", 1), ("CC BY 4.0", 1),
    ("CC BY-SA 1.0", 2), ("CC BY-SA 2.0", 2), ("CC BY-SA 2.5", 2), ("CC BY-SA 3.0", 2), ("CC BY-SA 4.0", 2),
]
_BAD_TITLE = re.compile(r"\b(map|locator|diagram|logo|flag|chart|plan|scheme|sketch|drawing|coat of arms|"
                        r"seal|icon|banner|stamp|coin|poster|cover|screenshot)\b|\.svg$|\.pdf$|\.gif$|\.tif"
                        r"|thumbs?[ _-]?down|dislike|disapprov", re.I)   # 10/29 逆サムズアップは使わない（ユーザー指示）


def _get(params: dict) -> dict:
    params = dict(params, format="json", formatversion="2")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for n in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception:                                   # noqa: BLE001
            if n == 2:
                raise
            time.sleep(2 * (n + 1))
    return {}


def _license_rank(short: str) -> int | None:
    s = (short or "").strip()
    for name, rank in LICENSE_RANK:
        if s.lower().startswith(name.lower()):
            return rank
    return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s).strip()
    # Commons の Artist は「表示名＋リンク文字」が重なって「Unknown authorUnknown author」になることがある
    h = len(s) // 2
    if len(s) >= 6 and len(s) % 2 == 0 and s[:h] == s[h:]:
        s = s[:h]
    if s.lower() in ("unknown author", "unknown", "anonymous"):
        s = "作者不詳"
    return s


def search_commons(queries: list[str], limit: int = 30, min_width: int = 700,
                   allow_sa: bool = True) -> list[dict]:
    """検索語ごとに Commons を引き、権利が明確な画像だけを返す（重複除去・ライセンス順→大きさ順）。
    戻り値の各要素: title, url(ページ), image_url(元), thumb_url(幅1200), width, height, mime,
                  author, license, license_url, rank, query"""
    seen, out = set(), []
    for q in queries:
        data = _get({"action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": 6,
                     "gsrlimit": limit, "prop": "imageinfo",
                     "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280})
        for page in data.get("query", {}).get("pages", []):
            title = page.get("title", "")
            if title in seen or _BAD_TITLE.search(title):
                continue
            ii = (page.get("imageinfo") or [{}])[0]
            if not ii or ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if (ii.get("width") or 0) < min_width:
                continue
            em = ii.get("extmetadata") or {}
            short = (em.get("LicenseShortName") or {}).get("value", "")
            rank = _license_rank(short)
            if rank is None or (rank == 2 and not allow_sa):
                continue
            if (em.get("Restrictions") or {}).get("value"):
                continue                                    # 商標・人格権などの制限つき
            author = _strip_html((em.get("Artist") or {}).get("value", "")) or \
                _strip_html((em.get("Credit") or {}).get("value", "")) or "不明"
            seen.add(title)
            out.append({
                "title": title, "url": ii.get("descriptionurl") or "",
                "image_url": ii.get("url"), "thumb_url": ii.get("thumburl") or ii.get("url"),
                "width": ii.get("width"), "height": ii.get("height"), "mime": ii.get("mime"),
                "author": author[:80], "license": short,
                "license_url": (em.get("LicenseUrl") or {}).get("value", ""),
                "rank": rank, "query": q,
            })
    out.sort(key=lambda c: (c["rank"], -(c["width"] * c["height"])))
    return out


def download(url: str, dst: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for n in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
                f.write(r.read())
            return dst
        except Exception:                                   # noqa: BLE001
            if n == 2:
                raise
            time.sleep(2 * (n + 1))
    return dst


def prepare_portrait(src: str, dst: str, W: int = 1080, H: int = 1920) -> str:
    """横写真を 1080x1920 に整える: ぼかして暗くした背景の上に、写真全体を収めて置く。
    縦長の写真はそのまま cover で使う（builder が中央を切り抜く）。"""
    from PIL import Image, ImageFilter, ImageEnhance
    im = Image.open(src)
    im = im.convert("RGB")
    w, h = im.size
    if w / h <= W / H * 1.05:                 # 縦長〜ほぼ縦: そのまま（cover）
        im.save(dst, "JPEG", quality=92)
        return dst
    # 背景: cover で拡大 → ぼかし → 暗く
    scale = max(W / w, H / h)
    bg = im.resize((int(w * scale) + 1, int(h * scale) + 1), Image.LANCZOS)
    bx, by = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((bx, by, bx + W, by + H)).filter(ImageFilter.GaussianBlur(36))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    # 前景: 幅いっぱい（高さは 1500 まで）
    fs = min(W / w, 1500 / h)
    fg = im.resize((int(w * fs), int(h * fs)), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    bg.save(dst, "JPEG", quality=92)
    return dst


def credit_line(c: dict) -> str:
    """説明欄用の1行。例: 「加茂岩倉遺跡 銅鐸」 撮影: Saigen Jiro / CC BY-SA 4.0 / Wikimedia Commons https://..."""
    name = re.sub(r"^File:", "", c.get("title", ""))
    name = re.sub(r"\.(jpe?g|png)$", "", name, flags=re.I)
    lic = c.get("license") or ""
    return f"・{name}（{c.get('author', '不明')} / {lic} / Wikimedia Commons）{c.get('url', '')}"


def fetch_for_topic(queries: list[str], out_dir: str, want: int = 6, allow_sa: bool = True,
                    exclude: set | None = None) -> list[dict]:
    """題材の写真を want 枚まで取り込み、縦動画用に整えた jpg と出典を返す。"""
    os.makedirs(out_dir, exist_ok=True)
    cands = search_commons(queries, allow_sa=allow_sa)
    got = []
    for c in cands:
        if exclude and c["title"] in exclude:
            continue
        key = hashlib.md5(c["title"].encode("utf-8")).hexdigest()[:10]
        raw = os.path.join(out_dir, f"raw_{key}.jpg")
        fin = os.path.join(out_dir, f"img_{key}.jpg")
        try:
            try:
                download(c["thumb_url"], raw)
            except Exception:                               # noqa: BLE001
                # Commons はサムネイル幅を許可リストに限る（HTTP 400）。元画像で取り直す
                download(c["image_url"], raw)
            prepare_portrait(raw, fin)
        except Exception as e:                              # noqa: BLE001
            print(f"  取得失敗 {c['title'][:40]}: {e}")
            continue
        c = dict(c, file=fin)
        got.append(c)
        if len(got) >= want:
            break
    return got
