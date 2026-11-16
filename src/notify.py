"""ギガファイル便に動画をアップロードし、ダウンロードリンクを
Gmailの下書きに保存（IMAP APPEND）し、さらにメール送信（Gmail API/HTTPS）もする。"""
import base64
import imaplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, MAIL_TO, NOTIFY_ENABLED,
    CLIENT_SECRET_FILE,
)

# Gmail API（HTTPS経由送信。SMTPがセキュリティソフトに遮断される環境向け）
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_GMAIL_FILE = str(Path(__file__).parent.parent / "config" / "token_gmail.json")


def get_gmail_credentials(allow_browser: bool = True):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    creds = None
    if Path(TOKEN_GMAIL_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_GMAIL_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif allow_browser:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError("Gmail未認証。authenticate_gmail.py を実行してください")
        with open(TOKEN_GMAIL_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def upload_to_gigafile(video_path: str) -> str:
    """ギガファイル便にアップロードしてダウンロードURLを返す"""
    from gfile import GFile
    g = GFile(video_path, progress=False)
    g.upload()
    return f"https://{g.server}/{g.token}"


def _find_drafts_folder(m: imaplib.IMAP4_SSL) -> str:
    """\\Drafts 属性を持つフォルダ名を探す（日本語環境の『[Gmail]/下書き』にも対応）"""
    typ, data = m.list()
    if typ == "OK":
        for raw in data:
            line = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
            if "\\Drafts" in line:
                # 行末の引用符付きフォルダ名を取り出す
                if '"' in line:
                    return line.rsplit('"')[-2]
                return line.split()[-1]
    return "[Gmail]/Drafts"


def _build_message(subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = MAIL_TO
    msg.set_content(body)
    return msg


def save_draft(msg: EmailMessage) -> None:
    """Gmailの下書きフォルダに保存（IMAP APPEND）"""
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    try:
        drafts = _find_drafts_folder(m)
        m.append(f'"{drafts}"', "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    finally:
        m.logout()


def send_mail(msg: EmailMessage) -> None:
    """Gmail API（HTTPS）でメール送信。SMTP遮断環境でも送れる。"""
    from googleapiclient.discovery import build
    creds = get_gmail_credentials(allow_browser=False)
    service = build("gmail", "v1", credentials=creds)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def notify_video(video_path: str, title: str, extra: str = "") -> str | None:
    """動画をギガファイル便にアップ→リンクをGmail下書きに保存し、さらに送信もする。
    URLを返す（無効時はNone）。"""
    if not NOTIFY_ENABLED:
        print("  メール通知: 無効（GMAIL_ADDRESS/GMAIL_APP_PASSWORD未設定）")
        return None
    try:
        url = upload_to_gigafile(video_path)
        body = f"新しい動画です。\n\nタイトル: {title}\n\nダウンロード(ギガファイル便):\n{url}\n"
        if extra:
            body += f"\n{extra}\n"
        msg = _build_message(f"[都市伝説] {title}", body)
        try:
            save_draft(msg)
            print(f"  Gmail下書き保存完了: {url}")
        except Exception as e:
            print(f"  下書き保存エラー: {e}")
        send_mail(msg)
        print(f"  メール送信完了: {MAIL_TO}")
        return url
    except Exception as e:
        print(f"  メール通知エラー: {e}")
        return None


if __name__ == "__main__":
    # テスト: py -3.12 src/notify.py <video_path> <title>
    notify_video(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "テスト動画")
