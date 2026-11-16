"""YouTube Data API v3 からチャンネル動画データ・コメントを取得"""
import sys
from pathlib import Path

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CLIENT_SECRET_FILE, TOKEN_FILE

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    # commentThreads 等のコメント系APIに必須（2026-07-02 追加・要再同意）
    "https://www.googleapis.com/auth/youtube.force-ssl",
    # 年齢・性別など視聴者属性(YouTube Analytics API)。2026-07-23 追加・要再同意
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _get_credentials(token_file: str = TOKEN_FILE) -> Credentials:
    creds = None
    token_path = Path(token_file)
    if token_path.exists():
        # scopes引数は渡さない。トークンの実スコープと食い違うとrefresh時に
        # RefreshErrorになるため。_SCOPESは新規認証時のみ使う
        creds = Credentials.from_authorized_user_file(token_file)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return creds


def _parse_duration(iso_duration: str) -> str:
    """PT1H2M3S → 1:02:03 形式に変換"""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not m:
        return iso_duration
    h, mi, s = (int(x or 0) for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _duration_seconds(iso_duration: str) -> int:
    """PT1H2M3S → 総秒数"""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


class ChannelAnalyzer:
    def __init__(self, channel_id: str = "", token_file: str = ""):
        """
        channel_id: 空なら認証アカウント本人(mine=True)、指定すればそのチャンネルの公開データを分析
        token_file: 別アカウントのOAuthトークンを使う場合のパス（省略時は既定のtoken.json）
        """
        creds = _get_credentials(token_file or TOKEN_FILE)
        self.youtube = build("youtube", "v3", credentials=creds)
        self.channel_id = channel_id

    def _get_uploads_playlist_id(self) -> str:
        if self.channel_id:
            resp = self.youtube.channels().list(
                part="contentDetails", id=self.channel_id
            ).execute()
        else:
            resp = self.youtube.channels().list(
                part="contentDetails", mine=True
            ).execute()
        items = resp.get("items", [])
        if not items:
            raise ValueError(
                f"チャンネルが見つかりません (channel_id={self.channel_id or 'mine'})"
            )
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def get_all_video_ids(self) -> list[str]:
        playlist_id = self._get_uploads_playlist_id()
        video_ids = []
        next_page_token = None

        while True:
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if next_page_token:
                params["pageToken"] = next_page_token
            resp = self.youtube.playlistItems().list(**params).execute()
            for item in resp.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return video_ids

    def get_video_details(self, video_ids: list[str]) -> list[dict]:
        all_videos = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = self.youtube.videos().list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(batch),
            ).execute()
            all_videos.extend(resp.get("items", []))
        return all_videos

    def get_top_comments(self, video_id: str, max_results: int = 20,
                         raise_on_error: bool = False) -> list[dict]:
        """動画の上位コメントを取得。
        注意: commentThreads API は youtube.force-ssl スコープが必要。
        raise_on_error=True なら例外をそのまま投げる（呼び出し側でスコープ不足を検知する用）。
        """
        try:
            resp = self.youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                order="relevance",
                maxResults=max_results,
                textFormat="plainText",
            ).execute()
            comments = []
            for item in resp.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                comments.append({
                    "text": snippet.get("textDisplay", ""),
                    "likes": snippet.get("likeCount", 0),
                    "author": snippet.get("authorDisplayName", ""),
                })
            return comments
        except Exception:
            if raise_on_error:
                raise
            return []

    def get_all_videos_with_stats(self) -> list[dict]:
        print("動画ID一覧を取得中...")
        video_ids = self.get_all_video_ids()
        print(f"  {len(video_ids)} 本発見")

        print("動画詳細データを取得中...")
        raw_videos = self.get_video_details(video_ids)

        videos = []
        for v in raw_videos:
            stats = v.get("statistics", {})
            snippet = v.get("snippet", {})
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            comment_count = int(stats.get("commentCount", 0))
            like_rate = round(likes / views * 100, 2) if views > 0 else 0.0

            iso_dur = v["contentDetails"].get("duration", "")
            videos.append({
                "id": v["id"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
                "privacy_status": v.get("status", {}).get("privacyStatus", ""),
                "duration": _parse_duration(iso_dur),
                "duration_seconds": _duration_seconds(iso_dur),
                "tags": snippet.get("tags", []),
                "views": views,
                "likes": likes,
                "comment_count": comment_count,
                "like_rate": like_rate,
                "url": f"https://youtu.be/{v['id']}",
            })

        videos.sort(key=lambda x: x["views"], reverse=True)
        return videos
