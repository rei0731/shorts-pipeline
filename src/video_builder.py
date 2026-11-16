"""動画編集: MoviePy + ImageMagick で縦動画を生成"""
import glob
import os
import random
import shutil
from pathlib import Path


def _locate_imagemagick() -> str | None:
    """magick.exe の場所を自動検出"""
    # 1. PATH上
    found = shutil.which("magick")
    if found:
        return found
    # 2. 既定インストール先を探索
    for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
        p = Path(base)
        if not p.exists():
            continue
        for d in p.glob("ImageMagick*"):
            exe = d / "magick.exe"
            if exe.exists():
                return str(exe)
    return None


# MoviePy がインポート時に IMAGEMAGICK_BINARY を読むため、import より前に設定
_magick = _locate_imagemagick()
if _magick:
    os.environ["IMAGEMAGICK_BINARY"] = _magick

import numpy as np
from moviepy.editor import (
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    CompositeAudioClip,
    ImageClip,
    TextClip,
    VideoClip,
)

# import後にも明示設定（環境差異対策）
if _magick:
    from moviepy.config import change_settings
    change_settings({"IMAGEMAGICK_BINARY": _magick})

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    BGM_DIR, BGM_FILE, BGM_VOLUME, FONT_PATH,
    SUBTITLE_COLOR, SUBTITLE_FONT_SIZE, SUBTITLE_STROKE_COLOR, SUBTITLE_STROKE_WIDTH,
    SUBTITLE_BOTTOM_MARGIN as _DEF_SUB_MARGIN,
    VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH,
)
from src.voice_generator import VoiceResult

# モジュール変数（ロングモード時に main._apply_long_mode が上書きする）
W = VIDEO_WIDTH
H = VIDEO_HEIGHT
SUB_FONT_SIZE = SUBTITLE_FONT_SIZE
SUB_BOTTOM_MARGIN = _DEF_SUB_MARGIN


def _cover_resize(img_path: str) -> "Image.Image":
    """画像をフレーム(W x H)全体を覆うようにリサイズ＋センタークロップ"""
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    iw, ih = img.size
    target_ratio = W / H
    img_ratio = iw / ih
    if img_ratio > target_ratio:
        # 横長すぎ → 高さ基準で拡大
        new_h = H
        new_w = int(iw * (H / ih))
    else:
        # 縦長すぎ → 幅基準で拡大
        new_w = W
        new_h = int(ih * (W / iw))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # センタークロップ
    left = (new_w - W) // 2
    top = (new_h - H) // 2
    return img.crop((left, top, left + W, top + H))


def _ken_burns(img_path: str, duration: float) -> VideoClip:
    """Ken Burnsエフェクト（フレーム全体にフィットさせてゆっくりズームイン）"""
    from PIL import Image
    base = _cover_resize(img_path)  # W x H にフィット済み
    base_arr = np.array(base)
    w, h = W, H

    def make_frame(t):
        progress = min(t / duration, 1.0)
        scale = 1.0 + 0.05 * progress  # 1.0 → 1.05 ズームイン
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = Image.fromarray(base_arr).resize((new_w, new_h), Image.LANCZOS)
        # センタークロップで元サイズへ
        x = (new_w - w) // 2
        y = (new_h - h) // 2
        cropped = np.array(img)[y:y + h, x:x + w]
        return cropped

    return VideoClip(make_frame, duration=duration).set_fps(VIDEO_FPS)


def _wrap_japanese(text: str, font, max_width: int) -> list[str]:
    """日本語テキストを幅に収まるように改行（空白がないため1文字ずつ測定）"""
    lines = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        test = current + ch
        w = font.getbbox(test)[2]
        if w > max_width and current:
            # 行末禁則（句読点・閉じ括弧は前行に残す）
            if ch in "、。」』）":
                lines.append(test)
                current = ""
            else:
                lines.append(current)
                current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


# === 字幕分割の目安（読みやすさ優先） ===
# 1行13〜20文字程度・最大2行。表示回数が増えてもよいので1枚を短く。
SUB_CHARS_PER_LINE = 13   # 1行あたり最大文字数（読みやすさ優先で短め）
SUB_MAX_LINES = 2         # 最大行数
SUB_MAX_CHARS = SUB_CHARS_PER_LINE * SUB_MAX_LINES  # 1字幕あたり最大文字数(=26)
SUB_MIN_DURATION = 3.5    # 1字幕の最低表示時間（秒）


def _split_subtitle(text: str) -> list[str]:
    """ナレーションを読みやすい短い字幕単位に分割する。
    句点(。!?)→読点(、)→長さ、の順に自然な位置で区切る。"""
    import re
    text = text.replace("\n", "").strip()
    # まず句点で文に分割（区切り文字は前に残す）
    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    chunks: list[str] = []
    for sent in sentences:
        if len(sent) <= SUB_MAX_CHARS:
            chunks.append(sent)
            continue
        # 読点でさらに分割して詰める
        units = [u for u in re.split(r"(?<=、)", sent) if u]
        buf = ""
        for u in units:
            if len(buf) + len(u) <= SUB_MAX_CHARS:
                buf += u
            else:
                if buf:
                    chunks.append(buf)
                if len(u) <= SUB_MAX_CHARS:
                    buf = u
                else:
                    # それでも長い場合は文字数で強制分割
                    for i in range(0, len(u), SUB_MAX_CHARS):
                        chunks.append(u[i:i + SUB_MAX_CHARS])
                    buf = ""
        if buf:
            chunks.append(buf)
    return chunks or [text]


def _wrap_chars(text: str, n: int) -> list[str]:
    """n文字ごとに改行（最大SUB_MAX_LINES行）。表示用に末尾の読点は除去。"""
    text = text.rstrip("、")
    lines = [text[i:i + n] for i in range(0, len(text), n)]
    return lines[:SUB_MAX_LINES]


def _render_subtitle_image(text: str) -> np.ndarray:
    """1枚の字幕画像（濃い白字＋黒縁＋半透明の暗い帯）をRGBAで返す"""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(FONT_PATH, SUB_FONT_SIZE)
    lines = _wrap_chars(text, SUB_CHARS_PER_LINE)

    line_h = int(SUB_FONT_SIZE * 1.45)
    pad_x, pad_y = 44, 26
    text_w = max(font.getbbox(ln)[2] for ln in lines)
    band_w = min(W - 40, text_w + pad_x * 2)
    band_h = line_h * len(lines) + pad_y * 2

    img = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 半透明の暗い帯（角丸）で背景コントラストを確保
    band_x0 = (W - band_w) // 2
    draw.rounded_rectangle(
        [band_x0, 0, band_x0 + band_w, band_h],
        radius=24, fill=(0, 0, 0, 140),
    )
    # 濃い白字＋太い黒縁
    y = pad_y
    for ln in lines:
        w = font.getbbox(ln)[2]
        x = (W - w) // 2
        draw.text(
            (x, y), ln, font=font,
            fill=(255, 255, 255),
            stroke_width=max(SUBTITLE_STROKE_WIDTH, 5),
            stroke_fill=(0, 0, 0),
        )
        y += line_h
    return np.array(img)


def _sanitize_subtitle(text: str) -> str:
    """YouTube ポリシー対応文字置換（句読点はここで除去しない→分割後に除去）"""
    text = text.replace("死", "シ")
    return text


def _remove_punctuation(text: str) -> str:
    """分割後の各チャンクから読点・句点を除去（表示しないが改行ヒントとして使った後）"""
    return text.replace("、", "").replace("。", "")


def _phrase_timings_from_mora(mora_times) -> list[tuple[float, float]]:
    """mora_timesのポーズ（text='　'）を区切りにフレーズ単位の(start, end)リストを返す。
    PDFspec原則: ズレの原因は固定秒管理。mora実測値で区切る。"""
    if not mora_times:
        return []
    phrases: list[tuple[float, float]] = []
    phrase_start = mora_times[0].start
    last_speech_end = mora_times[0].end

    for m in mora_times:
        if m.text == "　":
            # ポーズ前までをひとつのフレーズとして確定
            if last_speech_end > phrase_start:
                phrases.append((phrase_start, last_speech_end))
            phrase_start = m.end  # ポーズ終了後から次フレーズ開始
        else:
            last_speech_end = m.end

    # 末尾フレーズ
    if last_speech_end > phrase_start:
        phrases.append((phrase_start, last_speech_end))

    return [p for p in phrases if p[1] > p[0] + 0.05]


def _subtitle_clips(text: str, duration: float, mora_times=None) -> list[ImageClip]:
    """PDFspec準拠: 1シーン = 1字幕カード。分割しない。"""
    text = _sanitize_subtitle(_remove_punctuation(text.replace("\n", "")))
    speech_start = mora_times[0].start if mora_times else 0.0
    speech_end = mora_times[-1].end if mora_times else max(duration - 0.15, 0.1)
    seg = max(0.1, speech_end - speech_start)
    arr = _render_subtitle_image(text)
    img_h = arr.shape[0]
    y_pos = H - img_h - SUB_BOTTOM_MARGIN
    return [
        ImageClip(arr, transparent=True)
        .set_start(speech_start)
        .set_duration(seg)
        .set_position(("center", y_pos))
    ]

def _subtitle_clips_old(text: str, duration: float, mora_times=None) -> list[ImageClip]:
    """mora_timesのフレーズ実測タイミングで字幕を表示する。
    PDFspec: 音声の実測値を信じてテロップ側がそれに従う。固定秒は使わない。"""
    text = _sanitize_subtitle(text)
    chunks = [_remove_punctuation(c) for c in _split_subtitle(text)]
    n = len(chunks)

    # ---- mora_timesがある場合: フレーズ実測タイミングを使用 ----
    if mora_times and len(mora_times) > 1:
        phrase_timings = _phrase_timings_from_mora(mora_times)

        if phrase_timings:
            # チャンク数とフレーズ数を合わせる（多い方に少ない方を合流）
            if len(phrase_timings) >= n:
                # フレーズが多い → n個に均等に束ねる
                step = len(phrase_timings) / n
                merged_timings = []
                for i in range(n):
                    s_idx = int(i * step)
                    e_idx = int((i + 1) * step) - 1
                    e_idx = min(e_idx, len(phrase_timings) - 1)
                    merged_timings.append((
                        phrase_timings[s_idx][0],
                        phrase_timings[e_idx][1],
                    ))
            else:
                # フレーズが少ない → チャンクをフレーズ数に合流してからフレーズタイミングを使用
                pn = len(phrase_timings)
                step = n / pn
                chunks = [
                    "".join(chunks[int(i * step):int((i + 1) * step)])
                    for i in range(pn)
                ]
                merged_timings = phrase_timings
                n = len(chunks)

            clips = []
            for i, (chunk, (t_start, t_end)) in enumerate(zip(chunks, merged_timings)):
                seg = max(0.15, t_end - t_start)
                # 最後の字幕は音声終了まで延ばす
                if i == n - 1:
                    seg = max(seg, duration - t_start)
                    seg = min(seg, duration - t_start)  # durationを超えない
                arr = _render_subtitle_image(chunk)
                img_h = arr.shape[0]
                y_pos = H - img_h - SUB_BOTTOM_MARGIN
                clip = (
                    ImageClip(arr, transparent=True)
                    .set_start(t_start)
                    .set_duration(seg)
                    .set_position(("center", y_pos))
                )
                clips.append(clip)
            return clips

    # ---- フォールバック: mora_timesなし → 文字数比で配分 ----
    total_chars = sum(len(c) for c in chunks) or 1
    speech_end = duration - 0.3
    clips = []
    start = 0.0
    for i, chunk in enumerate(chunks):
        if i == n - 1:
            seg = max(0.2, duration - start)
        else:
            seg = max(0.5, speech_end * (len(chunk) / total_chars))
        arr = _render_subtitle_image(chunk)
        img_h = arr.shape[0]
        y_pos = H - img_h - SUB_BOTTOM_MARGIN
        clip = (
            ImageClip(arr, transparent=True)
            .set_start(start)
            .set_duration(seg)
            .set_position(("center", y_pos))
        )
        clips.append(clip)
        start += seg
    return clips


def _get_bgm() -> AudioFileClip | None:
    # 固定BGM指定があればそれを使用
    if BGM_FILE:
        fixed = os.path.join(BGM_DIR, BGM_FILE)
        if os.path.exists(fixed):
            return AudioFileClip(fixed).volumex(BGM_VOLUME)
        print(f"  BGM_FILE が見つかりません: {fixed} → ランダム選択にフォールバック")
    bgm_files = glob.glob(os.path.join(BGM_DIR, "*.mp3")) + glob.glob(os.path.join(BGM_DIR, "*.wav"))
    if not bgm_files:
        return None
    path = random.choice(bgm_files)
    return AudioFileClip(path).volumex(BGM_VOLUME)


def build_video(
    scene_images: list[str],
    voice_results: list[VoiceResult],
    scene_narrations: list[str],
    out_path: str,
    thumbnail_path: str | None = None,
) -> str:
    clips = []

    for i, (img_path, vr, narration) in enumerate(zip(scene_images, voice_results, scene_narrations)):
        duration = vr.duration + 0.3  # 末尾に少しマージン

        # 背景（Ken Burns）
        bg = _ken_burns(img_path, duration)

        # 音声: CompositeAudioClipでラップし、末尾超過読み込み時のIOErrorを回避
        # （範囲外は無音を返すためマージン分の余白も安全）
        raw_audio = AudioFileClip(vr.wav_path)
        audio = CompositeAudioClip([raw_audio]).set_duration(duration)

        # 字幕（短く分割して順番に表示・mora_timesで音声と同期）
        subtitle_clips = _subtitle_clips(narration, duration, vr.mora_times)

        scene_clip = CompositeVideoClip(
            [bg, *subtitle_clips],
            size=(W, H),
        ).set_audio(audio).set_duration(duration)

        clips.append(scene_clip)

    # 最終フレーム: サムネイル静止画（1フレーム分）
    if thumbnail_path and Path(thumbnail_path).exists():
        try:
            import numpy as np
            dur = 1 / VIDEO_FPS
            pil_img = _cover_resize(thumbnail_path)
            arr = np.array(pil_img)
            thumb_clip = ImageClip(arr).set_duration(dur)
            clips.append(thumb_clip)
        except Exception as e:
            print(f"  サムネフレーム追加スキップ: {e}")

    final = concatenate_videoclips(clips, method="compose")

    # BGM合成
    bgm = _get_bgm()
    if bgm:
        total = final.duration
        if bgm.duration < total:
            # ループ
            loops = int(total / bgm.duration) + 1
            bgm = concatenate_audioclips([bgm] * loops).subclip(0, total)
        else:
            bgm = bgm.subclip(0, total)
        final = final.set_audio(
            CompositeAudioClip([final.audio, bgm]).set_duration(total)
        )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # temp_audiofile を出力フォルダ内に指定することで複数プロセス同時実行時の競合を防ぐ
    temp_audio = str(Path(out_path).parent / "TEMP_MPY_audio.mp4")
    final.write_videofile(
        out_path,
        fps=VIDEO_FPS,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        logger=None,
        temp_audiofile=temp_audio,
    )
    return out_path
