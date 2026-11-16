"""YouTube Data API v3 で動画投稿・サムネ設定・プレイリスト追加"""
import os
import json
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    CLIENT_SECRET_FILE, TOKEN_FILE,
    YOUTUBE_CATEGORY_ID, YOUTUBE_PLAYLIST_NAME, YOUTUBE_PRIVACY_STATUS, CHANNEL_TYPE,
    SLEEP_PLAYLIST_NAME,
)

_DEFAULT_TAGS = (
    ["雑学", "豆知識", "面白い話", "ショート", "shorts"]
    if CHANNEL_TYPE == "zatsugaku"
    else ["都市伝説", "怖い話", "ショート", "shorts"]
)

# ショートを名乗るタグ。ロングに付けると内容と合わない
# （2026-08-25 発覚: 3時間の睡眠用ロングにも "shorts" が付いていた）。
_SHORTS_ONLY_TAGS = ("ショート", "shorts")
_LONG_TAGS = [t for t in _DEFAULT_TAGS if t not in _SHORTS_ONLY_TAGS]
_SHORTS_MAX_SEC = 180          # YouTube ショートの上限。これを超えたらロング扱い


def _video_seconds(path: str) -> float:
    """動画の尺(秒)。読めなければ0を返す（0ならショート扱いで従来どおり）。"""
    try:
        import re as _re
        import subprocess as _sp
        import imageio_ffmpeg
        r = _sp.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr or "")
        if not m:
            return 0.0
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        return 0.0

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    # channel_analyzer(コメント取得)と同一スコープにする。片方だけで再認証すると
    # token.jsonが狭いスコープで上書きされ、コメント分析が死ぬ(2026-07-15事故)
    "https://www.googleapis.com/auth/youtube.force-ssl",
    # 視聴者属性(YouTube Analytics API)。2026-07-23 追加
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

_VOICEVOX_CREDIT = "\n\n🎙️ 音声: VOICEVOX / 青山龍星"
_AI_DISCLOSURE = "\n⚠️ この動画はAIが生成したコンテンツを含みます"
_PEXELS_CREDIT = "\n📷 写真提供：ペクセル (pexels.com)"


# ---- 説明欄の長さガード（2026-11-07 ユーザー「説明欄が長くなりすぎないように次回からお願いします。」）----
# YouTube の説明欄は 5000 バイト上限。11/06 ゴビ砂漠の回は Commons 出典42件で 7400 バイトになり、
# invalidDescription で定期投稿が止まった。付け足し（VOICEVOX・Pexels・AI表示）を足した最終形で測り、
# 4800 バイトを超えたら CC0 / パブリックドメインの出典だけを作者ごとにまとめて URL を省く。
# CC BY / BY-SA は題名・作者・ライセンス・URL を1件ずつ残す。本文と必要な表示は削らない。
# それでも超えたら DescriptionTooLong を投げて投稿しない（API を呼ぶ前に止まる）。
DESC_LIMIT_BYTES = 4800
_CREDIT_HEAD = "📷 画像出典（Wikimedia Commons）"
_FREE_HEAD = "― 以下は CC0 / パブリックドメイン ―"


class DescriptionTooLong(Exception):
    pass


def _compact_commons_credits(desc: str) -> str:
    # 作者欄に改行や空行が入った出典がある（バージェス 030416 など）ので、行ではなく1件ずつ正規表現で拾う
    import re
    from collections import OrderedDict
    if _CREDIT_HEAD + "\n" not in desc:
        return desc
    head, rest = desc.split(_CREDIT_HEAD + "\n", 1)
    rx = re.compile(r"・(?P<t>[^（\n]+?)（(?P<a>.+?) / (?P<l>[^/（）\n]+?) / Wikimedia Commons）(?P<u>https://\S+)", re.S)
    keep, free, pos = [], OrderedDict(), 0
    for m in rx.finditer(rest):
        gap = rest[pos:m.start()]
        if gap.strip():
            keep.append(gap.strip("\n"))          # 形の分からない文字は消さずに残す
        lic = m.group("l").strip()
        if re.fullmatch(r"CC0|Public domain", lic, re.I):
            free.setdefault((" ".join(m.group("a").split()), lic), []).append(m.group("t").strip())
        else:
            keep.append(m.group(0))               # CC BY / BY-SA はそのまま（題名・作者・ライセンス・URL）
        pos = m.end()
    if not free:
        return desc
    out = keep + [_FREE_HEAD] + [f"・{a}（{lic}）：" + "、".join(ts) for (a, lic), ts in free.items()]
    return head + _CREDIT_HEAD + "\n" + "\n".join(out) + rest[pos:]


def fit_description(body: str, suffix: str) -> str:
    """付け足し込みの最終形を返す。4800 バイトを超えたら CC0/PD 出典をまとめ、まだ超えたら止める。"""
    full = body + suffix
    before = len(full.encode("utf-8"))
    if before <= DESC_LIMIT_BYTES:
        return full
    full = _compact_commons_credits(body) + suffix
    after = len(full.encode("utf-8"))
    print(f"  説明欄: {before}B > {DESC_LIMIT_BYTES}B → CC0/パブリックドメインの出典をまとめて {after}B")
    if after > DESC_LIMIT_BYTES:
        raise DescriptionTooLong(f"説明欄が {after}B（上限 {DESC_LIMIT_BYTES}B）。本文と CC BY 出典は削れないので投稿しない")
    return full


def _get_credentials() -> Credentials:
    creds = None
    token_path = Path(TOKEN_FILE)
    if token_path.exists():
        # scopes引数は渡さない。トークンの実スコープと食い違うとrefresh時に
        # RefreshErrorになるため。_SCOPESは新規認証時のみ使う
        creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def _is_sleep_title(title: str) -> bool:
    """睡眠用ロングかどうかをタイトルで見分ける。

    2026-09-03まで「【睡眠用】で始まるか」で判定していたが、検索型タイトル
    （眠れない夜に都市伝説聞き流し3時間｜…【睡眠用・朗読】）へ移行したため
    先頭一致では拾えなくなった。旧型・新型のどちらも通るよう含有で見る。
    """
    return ("睡眠用" in title) or ("聞き流し" in title)


def _get_or_create_playlist(youtube, name: str) -> str:
    resp = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    for item in resp.get("items", []):
        if item["snippet"]["title"] == name:
            return item["id"]
    created = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": name, "description": f"{name}チャンネルのプレイリスト"},
            "status": {"privacyStatus": "public"},
        },
    ).execute()
    return created["id"]


# 1日の投稿上限（2026-11-09 分析経由ユーザー「1回に上がる分をショートだったら1日5本ロングだったら1日2本に制限。」）
# 上げ直しも数える。どの経路も upload_video() を通るので、ここで止める。
# 2026-11-16 ユーザー直接「基本的に1日6本まで、ショートは1日5本、ロングは1日1本」→ ロングを1に
DAILY_CAP = {"short": 5, "long": 1}
_LEDGER = Path(__file__).resolve().parent.parent / "logs" / "upload_daily_ledger.jsonl"


class DailyCapReached(RuntimeError):
    pass


def _today_count(kind: str) -> int:
    import datetime as _dt
    today = _dt.date.today().isoformat()
    n_ledger = 0
    if _LEDGER.exists():
        for line in _LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("date") == today and r.get("kind") == kind:
                n_ledger += 1
    n_files = 0
    out = Path(__file__).resolve().parent.parent / "assets" / "output"
    if out.exists():
        for f in out.glob("*/uploaded.txt"):
            try:
                if _dt.date.fromtimestamp(f.stat().st_mtime).isoformat() != today:
                    continue
                # 投稿時と同じ基準（動画の尺 > 180秒＝ロング）で分ける。印や script.json だけだと
                # 印のないロング（例 20261024_132700 コパン）をショートと数えてしまう（2026-11-09 都市伝説ショート）
                lm = (f.parent / "pending.json.long").exists() or (f.parent / "pending.json.long.uploaded_bak").exists()
                sj = f.parent / "script.json"
                if sj.exists():
                    s_ = json.loads(sj.read_text(encoding="utf-8"))
                    lm = lm or bool(s_.get("long_mode") or s_.get("is_long"))
                mp4s = sorted(f.parent.glob("*.mp4"))
                if mp4s:
                    _sec = _video_seconds(str(mp4s[0]))
                    if _sec > _SHORTS_MAX_SEC:
                        lm = True
                if ("long" if lm else "short") == kind:
                    n_files += 1
            except Exception:  # noqa: BLE001
                continue
    return max(n_ledger, n_files)


def _check_cap(kind: str) -> None:
    n = _today_count(kind)
    if n >= DAILY_CAP[kind]:
        raise DailyCapReached("今日の%s投稿はもう%d本（上限%d本）→ 投稿しない" % (kind, n, DAILY_CAP[kind]))


def _record(kind: str, video_id: str, title: str) -> None:
    import datetime as _dt
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": _dt.date.today().isoformat(), "time": _dt.datetime.now().strftime("%H:%M:%S"),
                             "kind": kind, "id": video_id, "title": title}, ensure_ascii=False) + chr(10))


def upload_video(
    video_path: str,
    title: str,
    description: str,
    tags: list[str],
    thumbnail_path: str | None = None,
    used_pexels: bool | None = None,
    on_uploaded=None,
) -> str:
    creds = _get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    # used_pexels 未指定なら「Pexelsキーが設定されている＝画像はPexels由来」とみなす。
    # upload_pending.py など日次投稿の経路は引数を渡しておらず、既定 False のせいで
    # 出典表示が全部落ちていた（2026-08-28 に発覚）。出典表示はレート上限引き上げの条件でもある。
    if used_pexels is None:
        # config.settings の import で config/.env はロード済み。
        # channel A 系は image_generator が os.environ から直接キーを読む方式。
        used_pexels = bool(os.environ.get("PEXELS_API_KEY", ""))
    pexels_credit = _PEXELS_CREDIT if used_pexels else ""
    full_description = fit_description(description, _VOICEVOX_CREDIT + pexels_credit + _AI_DISCLOSURE)

    # 尺で足すタグを変える。ロングに「ショート/shorts」は付けない。
    sec = _video_seconds(video_path)
    extra = _DEFAULT_TAGS if 0 < sec <= _SHORTS_MAX_SEC or sec == 0 else _LONG_TAGS
    print(f"  タグ: {'ショート' if extra is _DEFAULT_TAGS else 'ロング'}扱い（{sec:.0f}秒）")
    _kind = "short" if extra is _DEFAULT_TAGS else "long"
    _check_cap(_kind)

    body = {
        "snippet": {
            "title": title,
            "description": full_description,
            "tags": tags + extra,
            "categoryId": YOUTUBE_CATEGORY_ID,
            # 動画の言語を日本語で明示する（2026-08-28 ユーザー指示）。
            # 未指定だとYouTube側の自動判定に任せることになり、英語と誤判定されると
            # 自動翻訳字幕や「この言語の視聴者へのおすすめ」が狂う。
            # defaultLanguage = タイトル・説明文の言語 / defaultAudioLanguage = 音声の言語。
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,  # AI生成コンテンツ開示
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    print("  YouTube アップロード中...")
    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]
    print(f"  アップロード完了: https://youtu.be/{video_id}")
    try:
        _record(_kind, video_id, title)
    except Exception as _e:  # noqa: BLE001
        print(f"  台帳に書けない: {_e}")
    # 上げた直後に呼び出し元へ知らせる（2026-10-27）。このあとサムネ設定の前に30秒待つので、
    # その間に止まると投稿済みの印が書かれず、次の回に二重投稿になる（10/26 16:30 に強制終了で発生）。
    if on_uploaded:
        try:
            on_uploaded(video_id)
        except Exception as e:
            print(f"  ⚠ 投稿済みの印の書き込みに失敗: {e}")

    # サムネイル設定（レート制限対策: 30秒待機 + 429時に60秒待ってリトライ）
    if thumbnail_path and Path(thumbnail_path).exists():
        time.sleep(30)
        for attempt in range(1, 4):
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumbnail_path),
                ).execute()
                print("  サムネイル設定完了")
                break
            except Exception as e:
                if "429" in str(e) or "rateLimitExceeded" in str(e).lower():
                    if attempt < 3:
                        print(f"  サムネイルレート制限 → {60*attempt}秒待機してリトライ ({attempt}/3)")
                        time.sleep(60 * attempt)
                    else:
                        # 429スキップ時は再試行フラグを必ず書き出す（retry_thumbnails.py が拾う）
                        try:
                            flag = Path(thumbnail_path).parent / "thumbnail_retry.json"
                            with open(flag, "w", encoding="utf-8") as _rf:
                                json.dump(
                                    {"video_id": video_id, "thumbnail_path": str(thumbnail_path)},
                                    _rf, ensure_ascii=False, indent=2,
                                )
                            print(f"  サムネイル設定スキップ → 再試行フラグ生成（retry_thumbnails.py で後日再設定）: {e}")
                        except Exception as _fe:
                            print(f"  サムネイル設定スキップ（フラグ書込失敗 {_fe}）: {e}")
                else:
                    print(f"  サムネイル設定エラー: {e}")
                    break

    # プレイリスト追加
    # 睡眠用ロングだけ別のプレイリストへ入れる（2026-08-28 ユーザー指示）。
    # 呼び出し側（main.py / _upload_one_long.py）が YOUTUBE_PLAYLIST_NAME を
    # 差し替える作りなので、経路によらず効くようタイトルで判定する。
    pl_name = SLEEP_PLAYLIST_NAME if _is_sleep_title(title) else YOUTUBE_PLAYLIST_NAME
    try:
        playlist_id = _get_or_create_playlist(youtube, pl_name)
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
        print(f"  プレイリスト追加: {pl_name}")
    except Exception as e:
        print(f"  プレイリスト追加エラー: {e}")

    return video_id
