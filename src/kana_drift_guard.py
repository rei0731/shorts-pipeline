# -*- coding: utf-8 -*-
"""narration と narration_kana が「別の語」になっていないかを見るガード。

⚠ これは読みの正誤（誤読）ではなく、**語そのものが入れ替わっている**ことの検出。
   2026-08-26 発覚: 台本生成の時点で narration に「話」と書きながら
   narration_kana には「つたえ」「かたり」「いちだん」「ものがたり」と
   別の語が書かれていた。字幕は「話です」、音声は「つたえです」になる。
   ユーザー指摘「山姥 土蜘蛛 浦島太郎の 話しの 読みが3動画おかしい」。

   既存の reading_guard.check_scene は該当3本で **検出0件**（実測）。
   読みの正誤しか見ていないため、この型は全部すり抜けていた。

やりかた: narration を janome で読みに起こし、その語の読みが kana 側に
出てくるかを見る。出てこなければ「その語だけ別物」の候補。
janome が持つ読みは1つだけなので、別読みが正しい語（山道=やまみち、
刃=やいば、行き来=ゆきき 等）は誤検知になる。**辞書 _ALT_OK で逃がす**。

使い方:
    from src.kana_drift_guard import check_drift
    issues = check_drift(narration, kana)   # 空リストなら問題なし
"""
from __future__ import annotations

import re

_KANJI = re.compile(r'[一-鿿]')

# 「話」を「はなし」と読まない熟語。ここを除かないと 神話/民話/実話 が誤検知になる
# （2026-08-26、伝説の怪物26選を「8箇所」と誤って数えた）。
_HANASHI_COMPOUND = ('会話', '電話', '世話', '民話', '神話', '童話', '逸話',
                     '説話', '談話', '対話', '実話', '寓話', '挿話', '手話',
                     '通話', '電話', '訓話', 'afterword')

# janome の読みが1つしかないために誤検知になる語（別読みが正しい）。
# 「表記: 許容する読み」。kana 側にこのどれかがあれば正しいとみなす。
_ALT_OK = {
    # 2026-09-04 janomeが音読みで返す組み合わせ
    '目と': ('めと', 'もくと'),
    # 2026-09-04 開く は ひらく／あく どちらもある
    '開け': ('ひらけ', 'あけ'),
    '開く': ('ひらく', 'あく'),
    '開い': ('ひらい', 'あい'),
    '開き': ('ひらき', 'あき'),
    # 2026-09-04 連濁するかは前の語しだい（くり返し／恩返し）
    '返し': ('かえし', 'がえし'),
    '返す': ('かえす', 'がえす'),
    # 2026-09-04 「五分の待ち時間」等。分数は別途 1/3 形を強制しているので緩めてよい
    '分の': ('ふんの', 'ぷんの', 'ぶんの'),
    # 2026-09-04 janomeが助数詞側で読む
    '人組': ('にんぐみ', 'ぐみ'),
    '二人組': ('ふたりぐみ', 'ににんぐみ'),
    # 2026-09-04 janomeが連濁形で読む語
    '図形': ('ずけい', 'づけい'),
    # 2026-09-04 時間の読みと、文脈で割れる同形異音語（channel Dロング量産で頻出）
    '五分': ('ごふん', 'ごぶ'),
    '一分': ('いっぷん', 'いちぶ'),
    '何分': ('なんぷん', 'なにぶん'),
    '一言': ('ひとこと', 'いちげん', 'いちごん'),
    '大勢': ('おおぜい', 'たいせい'),
    '上手': ('じょうず', 'うわて', 'かみて'),
    '人気': ('にんき', 'ひとけ'),
    '市場': ('しじょう', 'いちば'),
    '最中': ('さいちゅう', 'もなか'),
    '明日': ('あした', 'あす', 'みょうにち'),
    '一日': ('いちにち', 'ついたち'),
    # 2026-09-04 どちらの読みも通用する／janomeが助詞ごと1語にする形
    '世論': ('よろん', 'せろん'),
    '側が': ('がわが', 'そくが'),
    '側に': ('がわに', 'そくに'),
    '側は': ('がわは', 'そくは'),
    # 2026-09-04 janomeが「そく」と読む。文脈では がわ が正しい
    '側': ('がわ', 'そく', 'かわ'),
    # 2026-09-04 channel Dロングで誤検知。どちらの読みも通用する連濁語
    '線引き': ('せんびき', 'せんひき'),
    '悪口': ('わるくち', 'わるぐち'),
    # 2026-08-29 channel Dショートで誤検知。janome は 良い=よい だが
    # 「良い面」は いいめん と読むのが自然で、どちらも正しい
    '良い': ('よい', 'いい'),
    '良く': ('よく', 'いく'),
    # 2026-08-28 channel Dロングで誤検知。断つ(たつ)を 断る(ことわる) と解釈された
    '断っ': ('たっ', 'ことわっ'),
    '断つ': ('たつ', 'ことわる'),
    '両端': ('りょうはし', 'りょうたん'),
    '山道': ('やまみち', 'さんどう'),
    '山姥': ('やまんば', 'やまうば'),
    '刃': ('やいば', 'は'),
    '社': ('やしろ', 'しゃ'),
    '行き来': ('ゆきき', 'いきき'),
    '獣': ('けもの', 'しし', 'じゅう'),
    '後': ('のち', 'あと', 'ご', 'うし'),
    '百': ('ひゃく', 'びゃく', 'ぴゃく'),
    '人': ('ひと', 'にん', 'じん', 'り'),
    '日': ('ひ', 'にち', 'じつ', 'か'),
}


def _has_hanashi_drift(narration: str, kana: str) -> bool:
    """narration の「話」が kana 側で別語になっていないか。

    ⚠ 「話の数だけ"はなし"があるか」と数えてはいけない。それだと
       話す/話そう（はなす・はなそう＝動詞）、哀話（あいわ）、神話（しんわ）が
       すべて誤検知になる（2026-08-26、怪談26選と禁断の実験26選を
       危うく「直して」壊すところだった）。
       **その語の読みを起こして、その読みが kana にあるか**で見る。
    """
    try:
        import jaconv
        from janome.tokenizer import Tokenizer
    except Exception:
        return False                      # 解析器が無ければ判定しない（安全側）
    for tok in _tokenizer(Tokenizer).tokenize(narration):
        if '話' not in tok.surface:
            continue
        yomi = getattr(tok, 'reading', '*')
        if not yomi or yomi == '*':
            continue
        hira = jaconv.kata2hira(yomi)
        # 連濁（昔話=むかしばなし）は janome が はなし で返すことがあるので両方見る
        if hira in kana or hira.replace('はなし', 'ばなし') in kana:
            continue
        return True
    return False


def check_drift(narration: str, kana: str) -> list[str]:
    """**NGにしてよい**確度の高い食い違いだけを返す。問題なければ空リスト。

    ⚠ ここに汎用の読み照合を入れてはいけない。janome の読みは1語1つしか無く、
      「行った(おこなった/いった)」「日本(にっぽん/にほん)」のような複数読みが
      すべて誤検知になる。2026-08-26、それを NG にしたせいで preflight が落ち、
      **勝五郎ロングの自動投稿がその晩まるごと止まった**。
      広く見たいときは下の scan_reading_mismatch() を警告として使うこと。
    """
    if _has_hanashi_drift(narration, kana):
        return [f'narration の「話」が kana 側に見当たらない（別語に化けている疑い）: '
                f'「{narration[:40]}」/「{kana[:40]}」']
    return []


def scan_reading_mismatch(narration: str, kana: str) -> list[str]:
    """参考情報（警告用）。narration の漢字語の読みが kana に無いものを挙げる。
    複数読みの語で誤検知が出るので、**投稿を止める判断には使わない**。"""
    issues: list[str] = []
    try:
        import jaconv
        from janome.tokenizer import Tokenizer
    except Exception:
        return issues                      # 解析器が無ければ「話」検査だけで返す

    for tok in _tokenizer(Tokenizer).tokenize(narration):
        surf = tok.surface
        if not _KANJI.search(surf) or len(surf) < 2:
            continue
        if tok.part_of_speech.split(',')[0] not in ('名詞', '動詞', '形容詞', '副詞'):
            continue
        yomi = getattr(tok, 'reading', '*')
        if not yomi or yomi == '*':
            continue
        cands = _ALT_OK.get(surf) or (jaconv.kata2hira(yomi),)
        if not any(c in kana for c in cands):
            issues.append(f'「{surf}」の読み({"/".join(cands)})が kana に無い: 「{kana[:40]}」')
    return issues


_TOK = None


def _tokenizer(cls):
    global _TOK
    if _TOK is None:
        _TOK = cls()
    return _TOK
