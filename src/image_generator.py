"""画像生成: Pexels (優先) → Pollinations.ai → Hugging Face (フォールバック)"""
import io
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import requests as _requests
from PIL import Image as _PILImage

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import IMAGE_WIDTH, IMAGE_HEIGHT, IMAGE_STYLE_SUFFIX, FONT_PATH


_NEGATIVE_PREFIX = "dark mysterious cinematic atmosphere, "
# Pollinations 候補 (model, width, height)
_POLLINATIONS_CANDIDATES = [
    ("flux", None, None),
    ("turbo", None, None),
    ("flux-schnell", None, None),
    ("turbo", 1280, 720),
    ("flux", 1280, 720),
]

_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_HF_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
_HF_API_URL = f"https://api-inference.huggingface.co/models/{_HF_MODEL}"

_PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
_PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"

# モジュール変数（ロングモード時に main._apply_long_mode が上書きする）
IMG_W = IMAGE_WIDTH
IMG_H = IMAGE_HEIGHT
STYLE_SUFFIX = IMAGE_STYLE_SUFFIX


# 具体名詞を優先するためのストップワード（一般語・構図語を除外）
_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "by",
    "and", "or", "but", "as", "from", "into", "over", "under", "between",
    "is", "are", "was", "were", "be", "been", "being",
    "showing", "depicting", "featuring", "highlighting", "appearing",
    "subtle", "hint", "hints", "themed", "stylish",
    "appealing", "visually", "awe", "inspiring", "dreamlike",
    "composite", "image", "images", "graphic", "graphics", "visual", "visuals",
    "scene", "atmosphere", "background", "wallpaper", "illustration",
    "concept", "design", "art", "render", "photo", "photograph", "shot",
    "view", "angle", "perspective", "close", "up", "wide", "vibrant",
    "elements", "element", "style", "styles", "thematic",
    # 都市伝説のスタイルサフィックス由来の汎用語
    "dark", "mysterious", "cinematic", "horror", "dramatic", "lighting",
    "photorealistic", "eerie", "ominous", "shadowy",
}

# NGキーワード（Pexels alt textに含まれていたら除外。月経/女性器/下着等）
_NG_KEYWORDS = (
    "vagina", "vulva", "genital", "intimate", "tampon", "menstrual",
    "menstruation", "period product", "feminine hygiene", "panty", "panties",
    "underwear", "lingerie", "bra ", "naked", "nude", "sex", "sexual",
    "erotic", "breast", "cleavage",
    # 10/29 ユーザー「逆サムズアップの画像は今後使わない」（全チャンネル共通）
    "thumbs down", "thumb down", "thumbs-down", "thumb-down", "dislike", "disapprov",
)
# 10/29 同上: 検索の言葉にも入れない（thumbs up も down と並ぶと逆向きを引くので thumb ごと外す）
_NG_QUERY_WORDS = {"thumb", "thumbs", "dislike", "dislikes", "disapproval", "disapprove", "disapproving"}


def _extract_keywords(prompt: str) -> str:
    """image_prompt から Pexels 検索用に具体名詞を抽出（一般語を除外）。
    題材から離れる事故（深海の話で星空写真等）を防ぐ。"""
    raw = re.sub(r"[^a-zA-Z0-9\s]", " ", prompt).lower()
    kept: list[str] = []
    seen: set = set()
    if _NG_QUERY_WORDS & set(raw.split()):
        seen |= {"down", "gesture", "gesturing"}   # thumb を外しても down が残ると逆向きの手を引く
    for w in raw.split():
        if len(w) < 3 or w in _STOPWORDS or w in _NG_QUERY_WORDS or w in seen:
            continue
        seen.add(w)
        kept.append(w)
        if len(kept) >= 5:
            break
    return " ".join(kept) or "nature"


def _alt_ng(photo: dict) -> bool:
    alt = (photo.get("alt") or "").lower()
    return any(k in alt for k in _NG_KEYWORDS)


def _to_rgb(out_path: str) -> None:
    """画像をRGBのベースラインJPEGとして必ず再保存する（sanitize）。

    PILでは開けるがffmpegデコーダがクラッシュする不正JPEG
    （progressive / CMYK / 拡張子偽装PNG等）を排除し、
    -loop 1 -i <img> で確実に読める形式に正規化する。
    RGBA(透過)による黒帯対策も兼ねる。mode に関わらず常に再エンコードする。"""
    with _PILImage.open(out_path) as im:
        im.load()                       # 遅延デコードを強制し壊れた画像をここで検出
        rgb = im.convert("RGB")         # CMYK/P/RGBA/L すべて RGB に統一
    # progressive=False でベースラインJPEGを強制（ffmpegが落ちる主因を排除）
    rgb.save(out_path, "JPEG", quality=90, progressive=False, optimize=False)


# 検索結果のキャッシュ（クエリ+ページ → 写真リスト）。
# 同じシーンの複数カードは _extract_keywords を通すと同じ語になるので、
# キャッシュしておけば Pexels を叩く回数がそのぶん減る（2026-09-18）。
_PEXELS_CACHE: dict = {}

# 429（Throttle limit exceeded）で待つ秒数。Pexels は1時間あたりの回数で絞る。
# ここで待たずに素通りすると Pollinations に落ち、透かし入りの画像が動画に焼かれる。
_PEXELS_429_WAITS = (60, 120, 240)


def _pexels_search(query: str, orientation: str, page: int = 1) -> list:
    """Pexels 検索。結果はキャッシュし、429 は待ってやり直す（2026-09-18）。"""
    key = (query, orientation, page)
    if key in _PEXELS_CACHE:
        return _PEXELS_CACHE[key]
    headers = {"Authorization": _PEXELS_API_KEY}
    params = {"query": query, "orientation": orientation,
              "per_page": 80, "size": "large", "page": page}
    for i, wait in enumerate((0,) + _PEXELS_429_WAITS):
        if wait:
            print(f"    Pexels 429（時間あたり制限）: {wait}秒待って再試行 [{query}]", flush=True)
            time.sleep(wait)
        r = _requests.get(_PEXELS_SEARCH_URL, headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            continue
        r.raise_for_status()
        photos = r.json().get("photos", [])
        _PEXELS_CACHE[key] = photos
        return photos
    # 待っても開かなかった。キャッシュはせず、呼び出し側に判断させる。
    raise ValueError(f"Pexels: 429 が続く（{query}）")


def _generate_via_pexels(prompt: str, out_path: str, used_ids: set | None = None) -> str:
    """Pexels で写真検索してダウンロード。具体名詞抽出・NG除外・同動画内重複回避付き。"""
    orientation = "portrait" if IMG_H > IMG_W else "landscape"
    keywords = _extract_keywords(prompt)
    headers = {"Authorization": _PEXELS_API_KEY}
    params = {"query": keywords, "orientation": orientation,
              "per_page": 80, "size": "large"}

    photos = _pexels_search(keywords, orientation)

    if not photos:
        short_kw = keywords.split()[0] if keywords.split() else "nature"
        params["query"] = short_kw
        photos = _pexels_search(short_kw, orientation)

    # NG除外
    photos = [p for p in photos if not _alt_ng(p)]
    if not photos:
        raise ValueError(f"Pexels: 写真が見つかりません ({keywords})")

    # 同動画内重複回避
    if used_ids:
        fresh = [p for p in photos if p.get("id") not in used_ids]
        if fresh:
            photos = fresh
        else:
            # page=2 で追加検索
            try:
                fresh = [p for p in _pexels_search(params["query"], orientation, page=2)
                         if p.get("id") not in used_ids and not _alt_ng(p)]
                if fresh:
                    photos = fresh
            except Exception:
                pass  # 全部使い切ったら重複許可で続行

    # 上位15枚からランダム、DL失敗時は次候補
    candidates = photos[:15]
    random.shuffle(candidates)
    last_err = None
    for cand in candidates[:5]:
        src = cand.get("src", {})
        img_url = (src.get("portrait") if IMG_H > IMG_W else src.get("landscape")) \
            or src.get("large2x") or src.get("large") or src.get("original")
        if not img_url:
            continue
        try:
            img_r = _requests.get(img_url, timeout=60)
            img_r.raise_for_status()
            if len(img_r.content) < 5000:
                raise ValueError(f"小さすぎる ({len(img_r.content)}B)")
            with open(out_path, "wb") as f:
                f.write(img_r.content)
            _to_rgb(out_path)
            if used_ids is not None:
                used_ids.add(cand.get("id"))
            return out_path
        except Exception as e:
            last_err = e
            continue
    raise ValueError(f"Pexels: 全候補DL失敗 ({last_err})")



def _generate_via_pollinations(prompt: str, out_path: str, retry: int = 2) -> str:
    """Pollinations.ai で画像生成"""
    base_url = "https://image.pollinations.ai/prompt"
    full_prompt = _NEGATIVE_PREFIX + prompt + STYLE_SUFFIX
    encoded = urllib.parse.quote(full_prompt)

    for (model, w, h) in _POLLINATIONS_CANDIDATES:
        params = f"&model={model}&nologo=true"
        params += f"&width={w or IMG_W}&height={h or IMG_H}"
        url = f"{base_url}/{encoded}?{params.lstrip('&')}"
        for attempt in range(1, retry + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/jpeg,image/*",
                })
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = r.read()
                if len(data) < 1000:
                    raise ValueError(f"レスポンスが小さすぎる ({len(data)} bytes)")
                with open(out_path, "wb") as f:
                    f.write(data)
                label = f"{model} {w or IMG_W}x{h or IMG_H}"
                print(f"      (pollinations {label})")
                return out_path
            except urllib.error.HTTPError as e:
                if e.code in (402, 429, 503):
                    print(f"    model={model} HTTP{e.code} → 次候補へ")
                    break
                print(f"    model={model} (試行{attempt}): {e}")
                if attempt < retry:
                    time.sleep(5)
            except Exception as e:
                print(f"    model={model} (試行{attempt}): {e}")
                if attempt < retry:
                    time.sleep(5)
        else:
            continue

    raise RuntimeError("Pollinations 全候補失敗")


def _generate_via_hf(prompt: str, out_path: str, retry: int = 2) -> str:
    """Hugging Face Inference API で画像生成（HF_TOKEN 必須）"""
    full_prompt = _NEGATIVE_PREFIX + prompt + STYLE_SUFFIX
    payload = json.dumps({"inputs": full_prompt}).encode("utf-8")
    req = urllib.request.Request(
        _HF_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {_HF_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) < 1000:
                raise ValueError(f"HF レスポンスが小さすぎる ({len(data)} bytes)")
            with open(out_path, "wb") as f:
                f.write(data)
            print(f"      (huggingface sdxl)")
            return out_path
        except Exception as e:
            print(f"    HF (試行{attempt}): {e}")
            if attempt < retry:
                time.sleep(10)
    raise RuntimeError("HuggingFace 失敗")


def _generate_fallback_image(out_path: str) -> str:
    """全ソース失敗時のグラデーション背景画像を生成"""
    from PIL import Image, ImageDraw
    import random
    colors = [
        [(20, 20, 40), (60, 40, 80)],
        [(10, 30, 50), (40, 80, 100)],
        [(30, 20, 20), (80, 40, 40)],
        [(20, 30, 20), (40, 70, 50)],
    ]
    pair = random.choice(colors)
    img = Image.new("RGB", (IMG_W, IMG_H))
    draw = ImageDraw.Draw(img)
    for y in range(IMG_H):
        t = y / IMG_H
        r = int(pair[0][0] + (pair[1][0] - pair[0][0]) * t)
        g = int(pair[0][1] + (pair[1][1] - pair[0][1]) * t)
        b = int(pair[0][2] + (pair[1][2] - pair[0][2]) * t)
        draw.line([(0, y), (IMG_W, y)], fill=(r, g, b))
    img.save(out_path)
    return out_path


def generate_image(prompt: str, out_path: str, retry: int = 2,
                   used_ids: set | None = None) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # 1. Pexels（通常プロンプト → 短縮プロンプト → 汎用キーワードの順でリトライ）
    if _PEXELS_API_KEY:
        pexels_prompts = [
            prompt,
            " ".join(prompt.split()[:4]),   # 先頭4単語
            "scenic nature background",      # 汎用フォールバック
        ]
        for p in pexels_prompts:
            try:
                result = _generate_via_pexels(p, out_path, used_ids=used_ids)
                try:
                    from src.image_pool import is_blocked
                    _blk = is_blocked(result)
                except Exception:
                    _blk = False
                if _blk:   # 10/29 全チャンネル共通の使わない写真（image_pool/blocked.json）
                    raise ValueError("使わない写真（blocked.json）に当たった")
                print(f"      (pexels)")
                return result
            except Exception as e:
                print(f"    Pexels リトライ ({p[:30]}): {e}")

    # 2. Pollinations.ai
    # ここに落ちた画像には右下に pollinations.ai の透かしが入り、動画に焼かれると消せない。
    # 2026-09-18、30本一括生成で Pexels が429を返し続け、156枚が黙ってこちらになった。
    # 黙って落ちるのが一番困るので、必ず目立つ形で残す。
    print(f"    ⚠ Pexels を使えず Pollinations にフォールバック（透かしが入ります）: {out_path}",
          flush=True)
    try:
        result = _generate_via_pollinations(prompt, out_path, retry)
        _to_rgb(result)
        return result
    except Exception as e:
        print(f"    Pollinations 全候補失敗 → HuggingFace へ: {e}")

    # 3. HuggingFace
    if _HF_TOKEN:
        try:
            result = _generate_via_hf(prompt, out_path, retry)
            _to_rgb(result)   # ffmpeg互換のベースラインJPEGに正規化
            return result
        except Exception as e:
            print(f"    HuggingFace 失敗 → グラデーション背景へ: {e}")

    # 4. グラデーション背景（最終フォールバック・エラーにしない）
    print(f"    グラデーション背景を使用")
    return _generate_fallback_image(out_path)
