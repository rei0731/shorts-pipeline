"""エントリーポイント: 都市伝説YouTube Shorts 全自動生成・投稿"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import OUTPUT_DIR, LOGS_DIR, CHANNEL_TYPE, TOPIC_GENRES
from src.script_generator import generate_script, save_script_to_history
from src.voice_generator import generate_voice, ensure_voicevox
from src.image_generator import generate_image
from src.video_builder_ffmpeg import build_video
from src.thumbnail_generator import make_thumbnail
from src.youtube_uploader import upload_video
from src.notify import notify_video


def _log(msg: str, log_file: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _apply_long_mode(test: bool = False):
    """ロングモード（横型）の設定を実行時に適用する。
    test=True のとき LONG_TEST_ITEM_COUNT（約2分）を使う。"""
    import config.settings as st
    import src.video_builder_ffmpeg as vb   # 本番ビルダー（旧 src.video_builder はMoviePy版で未使用）
    import src.image_generator as ig
    import src.youtube_uploader as yu
    import src.script_generator as sg
    vb.W = st.LONG_VIDEO_WIDTH
    vb.H = st.LONG_VIDEO_HEIGHT
    vb.SUB_FONT_SIZE = st.LONG_SUBTITLE_FONT_SIZE
    vb.SUB_BOTTOM_MARGIN = st.LONG_SUBTITLE_BOTTOM_MARGIN
    ig.IMG_W = st.LONG_IMAGE_WIDTH
    ig.IMG_H = st.LONG_IMAGE_HEIGHT
    ig.STYLE_SUFFIX = st.LONG_IMAGE_STYLE_SUFFIX
    yu.YOUTUBE_PLAYLIST_NAME = st.LONG_PLAYLIST_NAME
    if test:
        st.LONG_ITEM_COUNT = st.LONG_TEST_ITEM_COUNT
        sg.LONG_ITEM_COUNT = st.LONG_TEST_ITEM_COUNT
        label = f"テスト約2分"
    else:
        label = f"10分"
    print(f"  [ロングモード/{label}] {vb.W}x{vb.H} / 項目{st.LONG_ITEM_COUNT} / playlist={yu.YOUTUBE_PLAYLIST_NAME}")


def run_one(upload: bool = True, genre: str | None = None, long_mode: bool = False) -> dict:
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = Path(OUTPUT_DIR) / date_str
    work_dir.mkdir(parents=True, exist_ok=True)

    log_path = Path(LOGS_DIR) / f"{date_str}.log"
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

    def log(msg):
        _log(msg, str(log_path))

    log(f"=== 都市伝説動画生成開始{'（ロング10分）' if long_mode else ''} ===")

    # 1. 台本生成
    log("【1/5】台本生成中...")
    script = generate_script(genre=genre, long_mode=long_mode)
    log(f"  タイトル: {script.title}")

    # ジャンル混入ガード: 生成ジャンルが当該チャンネルのTOPIC_GENRESに無ければ中断
    # （CHANNEL_TYPE取り違えで雑学ネタが都市伝説に混入する事故の防止。仕様書6.7/15章）
    if script.genre and script.genre not in TOPIC_GENRES:
        raise RuntimeError(
            f"ジャンル不一致: 生成ジャンル『{script.genre}』は CHANNEL_TYPE={CHANNEL_TYPE} の"
            f" TOPIC_GENRES に含まれません。CHANNEL_TYPE の取り違えの可能性があります。生成を中断します。"
        )

    # 台本をJSONで保存
    script_path = work_dir / "script.json"
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump({
            "title": script.title,
            "description": script.description,
            "tags": script.tags,
            "genre": script.genre,
            "channel_type": CHANNEL_TYPE,
            "scenes": [
                {
                    "index": s.index,
                    "narration": s.narration,
                    "narration_kana": s.narration_kana,
                    "image_prompt": s.image_prompt,
                }
                for s in script.scenes
            ],
        }, f, ensure_ascii=False, indent=2)

    # 2. 音声生成
    log("【2/5】音声合成中...")
    voice_results = []
    for scene in script.scenes:
        wav_path = str(work_dir / f"voice_{scene.index:02d}.wav")
        log(f"  シーン {scene.index + 1}/{len(script.scenes)}")
        vr = generate_voice(scene.narration_kana, wav_path, scene.narration)
        voice_results.append(vr)

    # 3. 画像生成（テロップ実カード数ぴったり・カード単位切替）
    log("【3/5】画像生成中...")
    import re as _re
    from src.video_builder_ffmpeg import _subtitle_timings
    from src.image_generator import _PEXELS_API_KEY as _pex_key
    used_pexels = bool(_pex_key)
    used_pexels_ids: set = set()        # 同動画内のPexels重複回避（全カード共有）
    scene_card_images: list[list[str]] = []
    image_paths: list[str] = []         # サムネ用の代表画像（各シーン先頭カード）
    _recent_src: list[str] = []         # 直近のテロップが使った画像の出どころ（同じ写真の連続を防ぐ）
    _real_uses: dict = {}               # 実物写真ごとの使用テロップ数（1本で3まで）
    _used_credits: list[str] = []       # 実際に使った実物写真の出典
    for scene, vr in zip(script.scenes, voice_results):
        cards = _subtitle_timings(scene.narration, vr.duration, vr.mora_times)
        n_cards = max(1, len(cards))
        log(f"  シーン {scene.index + 1}/{len(script.scenes)}: {n_cards}枚 ({scene.image_prompt[:32]}...)")
        card_imgs: list[str] = []
        # 実物の写真（Wikimedia Commons 等・src/image_sources.py）があればそれを使う（2026-09-29）。
        # 「いい加減な画像ばかり」というコメントを受けて、実物がある題材は本物の写真を優先する。
        # 出典は説明欄に書く（pending.json の description に付く）。
        import os as _os
        import shutil as _sh
        local_img = getattr(scene, "image_file", "") or ""
        if local_img and not _os.path.exists(local_img):
            log(f"    ⚠ 実物写真が見つからない（Pexelsで代替）: {local_img}")
            local_img = ""
        # 2枚目以降のテロップにも実物の写真を使う（2026-10-07 ユーザー指示「実在の場所や物は本物の写真で」）。
        # 台本の image_extra に場面の内容に合う写真を並べておくと、テロップ順に割り当てる。
        # 同じ写真は間に2テロップあけ、1本で3テロップまで（1001の規則）。足りないテロップだけ Pexels。
        _cands = ([(local_img, getattr(scene, "image_credit", "") or "")] if local_img else [])
        for _e in (getattr(scene, "image_extra", None) or []):
            _f = (_e or {}).get("file", "")
            if _f and _os.path.exists(_f):
                _cands.append((_f, (_e or {}).get("credit", "")))
            elif _f:
                log(f"    ⚠ 実物写真が見つからない: {_f}")
        for j in range(n_cards):
            img_path = str(work_dir / f"image_{scene.index:02d}_{j:02d}.jpg")
            # なるべく1テロップ1画像・同じ画像は間に2テロップあける（2026-10-01 ユーザー指示）。
            # 直前2テロップに同じ写真があれば使わない
            _pick = None
            # image_extra の無い台本（従来・ロング）は今までどおり先頭テロップだけ
            for _k in range(len(_cands) if (j == 0 or len(_cands) > 1) else 0):
                _f, _c = _cands[(j + _k) % len(_cands)]
                if _f not in _recent_src[-2:] and (len(_cands) == 1 or _real_uses.get(_f, 0) < 3):
                    _pick = (_f, _c)
                    break
            if _pick:
                _sh.copyfile(_pick[0], img_path)
                card_imgs.append(img_path)
                _recent_src.append(_pick[0])
                _real_uses[_pick[0]] = _real_uses.get(_pick[0], 0) + 1
                if _pick[1]:
                    _used_credits.append(_pick[1])
                continue
            _recent_src.append(img_path)
            # カードの字幕語をプロンプトに軽く混ぜてバリエーションを出す（主軸はimage_prompt）
            ck = cards[j][0] if j < len(cards) else ""
            ck_clean = _re.sub(r"[、。！？「」『』0-9]", "", ck).strip()
            extra = f", subtle variation {j+1}" + (f", focus on {ck_clean[:30]}" if ck_clean else "")
            generate_image(scene.image_prompt + extra, img_path, used_ids=used_pexels_ids)
            card_imgs.append(img_path)
        scene_card_images.append(card_imgs)
        image_paths.append(card_imgs[0])

    # サムネイル生成（先頭シーン先頭カード＋タイトル文字・縦1080x1920）
    thumbnail_path = None
    if image_paths:
        thumbnail_path = str(work_dir / "thumbnail.jpg")
        try:
            make_thumbnail(image_paths[0], script.title, thumbnail_path)
            log(f"  サムネイル生成: {thumbnail_path}")
        except Exception as e:
            log(f"  サムネイル生成失敗（画像をそのまま使用）: {e}")
            thumbnail_path = image_paths[0]

    # 4. 動画編集
    log("【4/5】動画編集中...")
    video_path = str(work_dir / "output.mp4")
    narrations = [s.narration for s in script.scenes]
    # ショートのみ最終フレームにサムネを挿入（ロングは不要）
    thumb_for_video = thumbnail_path if (not long_mode and thumbnail_path) else None
    chapter_starts = {i: getattr(s, 'chapter', '')
                      for i, s in enumerate(script.scenes)
                      if getattr(s, 'chapter', '') and i > 0}
    build_video(image_paths, voice_results, narrations, video_path,
                thumbnail_path=thumb_for_video, scene_card_images=scene_card_images,
                chapter_starts=chapter_starts)
    log(f"  動画完成: {video_path}")

    # メール通知（Gmail下書き／ギガファイル便）は無効化（ユーザー要望：報告不要）
    # try:
    #     kind = "ロング" if long_mode else "ショート"
    #     notify_video(video_path, f"{kind}｜{script.title}")
    # except Exception as e:
    #     log(f"  メール通知スキップ: {e}")

    # 説明欄の「画像出典」（実物の写真を使った場合。2026-09-29）
    # 順序を保って重複除去（dict.fromkeys）。内包表記の中で _credits を見ると追加前のリストのままなので、
    # 場面どうしの重複（1枚を3場面に使う）が残る（ロング担当の指摘・2026-09-29）
    _credits = list(dict.fromkeys(
        [c for c in (getattr(script, "image_credits", []) or []) if c]
        + [c for c in (getattr(s, "image_credit", "") for s in script.scenes if not getattr(s, "image_extra", None)) if c]
        + _used_credits))
    if _credits:
        script.description = (script.description.rstrip() + "\n\n📷 画像出典（Wikimedia Commons）\n"
                              + "\n".join(_credits))
        log(f"  画像出典 {len(_credits)} 件を説明欄に追加")

    result = {
        "title": script.title,
        "video_path": video_path,
        "thumbnail_path": thumbnail_path,
        "work_dir": str(work_dir),
        "uploaded": False,
        "video_id": None,
    }

    # 5. YouTube投稿
    if upload:
        log("【5/5】YouTube投稿中...")
        video_id = upload_video(
            video_path=video_path,
            title=script.title,
            description=script.description,
            tags=script.tags,
            thumbnail_path=thumbnail_path,
            used_pexels=used_pexels,
        )
        result["uploaded"] = True
        result["video_id"] = video_id

        # 投稿済みマーク
        uploaded_file = work_dir / "uploaded.txt"
        uploaded_file.write_text(f"video_id: {video_id}\n{script.title}", encoding="utf-8")

        save_script_to_history(script)
        log(f"  投稿完了: https://youtu.be/{video_id}")
    else:
        log("【5/5】投稿スキップ（--no-upload）")
        # 生成しただけでも履歴に残し、次バッチ以降の被り検出を効かせる（投稿時のみ保存だと在庫被りを見逃す）
        save_script_to_history(script)
        # 未投稿メタデータを保存
        pending_file = work_dir / "pending.json"
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump({
                "title": script.title,
                "description": script.description,
                "tags": script.tags,
                "channel_type": CHANNEL_TYPE,
                "video_path": video_path,
                "thumbnail_path": thumbnail_path,
            }, f, ensure_ascii=False, indent=2)

    log(f"=== 完了 ===")
    return result


def main():
    parser = argparse.ArgumentParser(description="YouTube Shorts 自動生成")
    parser.add_argument("--count", type=int, default=1, help="生成本数")
    parser.add_argument("--no-upload", action="store_true", help="投稿しない")
    parser.add_argument("--genre", type=str, default=None, help="ジャンル指定")
    parser.add_argument("--long", action="store_true", help="横型10分のロング動画を生成")
    parser.add_argument("--long-test", action="store_true", help="横型テスト動画（約2分）を生成")
    args = parser.parse_args()

    # 起動時バナー: 現在のチャンネルを大きく表示し、取り違えを目視で防ぐ（仕様書6.7/15章）
    print(f"\n{'#'*50}")
    print(f"#  CHANNEL_TYPE = {CHANNEL_TYPE}")
    print(f"#  対象ジャンル: {', '.join(TOPIC_GENRES[:3])} ...")
    print(f"{'#'*50}")
    # .env とプロセス環境の不一致を検出
    _env_ct = os.environ.get("CHANNEL_TYPE")
    if _env_ct and _env_ct != CHANNEL_TYPE:
        print(f"⚠️ 警告: プロセス環境 CHANNEL_TYPE={_env_ct} と設定値 {CHANNEL_TYPE} が食い違っています。")
    if CHANNEL_TYPE not in ("tosidensetu", "zatsugaku"):
        print(f"✗ 不明な CHANNEL_TYPE: {CHANNEL_TYPE} — 中断します")
        return

    # 古い投稿済みフォルダの自動削除(削除前にNotionへ台本退避→検証成功時だけ削除)
    # 被り防止履歴(history.json)は別ファイルで保全。失敗フォルダは残る=消失ゼロ。
    try:
        import subprocess as _sp
        _root = Path(__file__).resolve().parent.parent
        _sp.run([sys.executable, str(_root / "cleanup_old_outputs.py")],
                cwd=str(_root), check=False)
    except Exception as _e:  # noqa: BLE001
        print(f"cleanup スキップ: {_e}")

    if args.long_test:
        args.long = True
        _apply_long_mode(test=True)
    elif args.long:
        _apply_long_mode(test=False)

    # 生成前にVOICEVOX起動を確認（未起動なら自動起動）。過去に未起動で全滅した事故対策
    from config.settings import TTS_ENGINE
    if TTS_ENGINE == "voicevox" and not ensure_voicevox():
        print("✗ VOICEVOXを起動できませんでした — 中断します")
        return

    for i in range(args.count):
        print(f"\n{'='*50}")
        mode_label = "（ロングテスト）" if args.long_test else "（ロング）" if args.long else ""
        print(f"動画 {i + 1}/{args.count}{mode_label}")
        print(f"{'='*50}")
        try:
            result = run_one(upload=not args.no_upload, genre=args.genre, long_mode=args.long)
            print(f"✓ 完了: {result['title']}")
        except Exception as e:
            print(f"✗ エラー: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
