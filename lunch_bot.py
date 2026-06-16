#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공채 2기방 점심봇 — GitHub Actions용 사회자 봇
- mode "request" : 점심 참석/투표 요청 (오전 9:10)
- mode "result"  : 출결/식당 투표 집계 + 사회자 멘트 (오전 11:00)
데이터는 사이트의 Firebase Realtime DB(REST)에서 읽고, Claude API로 사회자 멘트를
생성한 뒤 Teams '공채 2기방' 웹훅으로 게시한다. 외부 패키지 없이 표준 라이브러리만 사용.
"""
import os, sys, json, random, urllib.request
from datetime import datetime, timezone, timedelta

# ---- 환경/설정 ----
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
DB_BASE  = os.environ.get("DB_BASE",  "https://lunch-ef42a-default-rtdb.asia-southeast1.firebasedatabase.app")
SITE_URL = os.environ.get("SITE_URL", "https://sohyeonahn.github.io/gongchae2-lunch/")

# index.html 과 동일한 순서여야 함
MEMBERS = ["안소현","오주영","안시현","조상은","이태림","장채아","윤혜준","최준영","용성민","김진영"]
RESTAURANTS = ["또바기","명품가마","장꼬방","오니기리","수라간","칼국수","엄마손","더진국",
               "진지왕","참진","김치찌개","샐러리아","샐러리오","영심이","배달(엄마손)"]

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)
today = now.strftime("%Y-%m-%d")
WD_KR = ["월","화","수","목","금","토","일"][now.weekday()]
DATE_LABEL = f"{now.month}월 {now.day}일 ({WD_KR})"


# ---- Firebase 읽기 ----
def fetch_today():
    url = f"{DB_BASE}/lunch/{today}.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception as e:
        print("[WARN] DB read failed:", e)
        return {}


def normalize_votes(v):
    if not v:
        return []
    if isinstance(v, str):       # 과거 단일 선택 호환
        return [v]
    return [rid for rid, on in v.items() if on]


def aggregate(data):
    att   = (data or {}).get("attendance", {}) or {}
    votes = (data or {}).get("votes", {}) or {}
    yes, no, pend = [], [], []
    for i, name in enumerate(MEMBERS):
        st = att.get(f"m{i}")
        if   st == "yes": yes.append(name)
        elif st == "no":  no.append(name)
        else:             pend.append(name)
    tally = {r: 0 for r in RESTAURANTS}
    for i in range(len(MEMBERS)):
        if att.get(f"m{i}") != "yes":
            continue
        for rid in normalize_votes(votes.get(f"m{i}")):
            try:
                idx = int(rid[1:])
            except ValueError:
                continue
            if 0 <= idx < len(RESTAURANTS):
                tally[RESTAURANTS[idx]] += 1
    ranked = sorted([(r, c) for r, c in tally.items() if c > 0], key=lambda x: -x[1])
    maxc = ranked[0][1] if ranked else 0
    winners = [r for r, c in ranked if c == maxc] if maxc > 0 else []
    return {"yes": yes, "no": no, "pend": pend, "ranked": ranked,
            "winners": winners, "maxc": maxc}


# ---- Claude 사회자 멘트 생성 ----
SYSTEM_PROMPT = (
    "너는 회사 동기 단체방 '공채 2기방'(10명)의 점심 모임을 진행하는 유쾌한 사회자야. "
    "매일 점심 메시지를 한국어로 쓴다. 톤은 밝고 재치있고 친근하게, 너무 오글거리지 않게. "
    "이모지는 1~3개만. 출력은 Teams에 그대로 게시되니 메시지 본문만 쓰고, 군더더기 설명/따옴표/머리말은 쓰지 마. "
    "분량은 4~7줄 정도로 간결하게. 사람을 깎아내리거나 무안 주는 말은 금지(미투표자는 가볍고 장난스럽게만)."
)


def github_models_message(user_prompt):
    """GitHub Models(무료) — Actions의 GITHUB_TOKEN으로 호출. (워크플로에 permissions: models: read 필요)"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return None
    model = os.environ.get("GH_MODEL", "openai/gpt-4o-mini")
    url   = os.environ.get("GH_MODELS_URL", "https://models.github.ai/inference/chat/completions")
    body = json.dumps({
        "model": model,
        "max_tokens": 600,
        "temperature": 0.9,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return (resp["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:
        print("[WARN] GitHub Models failed:", e)
        return None


def generate(user_prompt):
    """GitHub Models(무료)로 생성. 실패하면 None → 호출부에서 무료 템플릿으로 자동 롤백."""
    return github_models_message(user_prompt)


def request_prompt():
    return (
        f"오늘은 {DATE_LABEL}. 점심 같이 먹을 사람을 모으는 '참석/투표 요청' 메시지를 써줘.\n"
        f"- 멤버는 {', '.join(MEMBERS)} 총 {len(MEMBERS)}명.\n"
        f"- 아래 사이트에 들어가서 참석/불참 체크 + 가고 싶은 식당 투표(여러 곳 가능)를 하라고 안내.\n"
        f"- 사이트 링크를 반드시 본문에 포함: {SITE_URL}\n"
        f"- 11시에 결과를 발표한다고 살짝 언급. 마감 독려는 부드럽게.\n"
        f"- 요일/날씨/계절 같은 소재로 가벼운 한 줄 인사를 곁들여줘."
    )


def result_prompt(agg):
    ranked_txt = ", ".join(f"{r} {c}표" for r, c in agg["ranked"]) or "아직 투표 없음"
    return (
        f"오늘은 {DATE_LABEL}, 지금은 오전 11시 점심 집계 발표 시간이야. 아래 데이터로 사회자 발표 멘트를 써줘.\n"
        f"[참석 {len(agg['yes'])}명] {', '.join(agg['yes']) or '없음'}\n"
        f"[불참 {len(agg['no'])}명] {', '.join(agg['no']) or '없음'}\n"
        f"[미응답 {len(agg['pend'])}명] {', '.join(agg['pend']) or '없음'}\n"
        f"[식당 득표] {ranked_txt}\n"
        f"[1위] {', '.join(agg['winners']) or '없음'} ({agg['maxc']}표)\n\n"
        "요구사항:\n"
        "- 참석 인원과 오늘의 식당 결과(1위)를 신나게 발표.\n"
        "- 동률이면 '결선'처럼 재치있게 정리하고 정하라고 유도.\n"
        "- 미응답자가 있으면 이름을 부르며 '아직 안 누른 분~' 식으로 가볍고 장난스럽게 호명(기분 나쁘지 않게, 한 줄).\n"
        "- 마지막에 '맛점하세요' 류의 인사로 마무리.\n"
        f"- 변경/확인 링크 포함: {SITE_URL}\n"
        "- 데이터에 없는 사실은 지어내지 말 것."
    )


# ---- 무료 사회자 멘트 (API 미사용 시 기본 동작) ----
INTROS = [
    "점심팟 모집 오픈! 🍱", "배꼽시계 울리기 전에 모여요~ ⏰", "오늘 뭐 먹지? 같이 정해요!",
    "점심 메이트 구합니다 🙌", "자, 오늘도 점심 투표 시작합니다!", "꼬르륵… 점심시간이 다가옵니다 😋",
    "오늘의 한 끼, 같이 정해요!", "출출해지기 전에 미리미리~",
]
WD_FLAVOR = {
    "월": "월요일이니 든든하게 출발해요!", "화": "화요일도 힘내서 맛있는 점심 어때요?",
    "수": "벌써 주중 반환점, 수요일 점심 가즈아!", "목": "목요일, 주말이 코앞이에요 🙂",
    "금": "불금 전 점심! 오늘은 뭔가 특별한 거 어때요? 🎉",
}
NUDGES = [
    "참석/불참 체크하고, 끌리는 식당에 투표 고고! (여러 곳 OK)",
    "오늘의 한 끼, 다 같이 정해봐요. 식당은 여러 곳 골라도 돼요!",
    "1분이면 끝나요. 참석 체크 + 식당 투표 부탁해요!",
    "어디 갈지 고민될 땐 일단 투표부터! 여러 곳 선택 가능해요.",
]
CALLOUTS = [
    "👀 아직 안 누른 분: {names} — 점심 안 드실 거예요~?",
    "📢 {names} 님, 어디 계신가요~ 참석 체크 기다리는 중!",
    "🙋 {names} 님은 아직 미응답! 1분이면 되는데… 살짝 눌러주세요 😏",
    "⏳ {names} 님 응답 대기 중… 같이 먹어요!",
]
WIN_ONE = [
    "🏆 오늘의 승자 식당은 «{w}»! ({n}표) 가시죠~",
    "🥇 «{w}» 당첨! ({n}표) 오늘 점심 여기로 정해요!",
    "🎉 영예의 1위는 «{w}»! ({n}표)",
]
WIN_TIE = [
    "🏆 공동 1위: {ws} (각 {n}표) — 결선 투표 고고!",
    "🤝 막상막하! {ws} 동률 {n}표 — 가위바위보 각인가요? 😆",
]
CLOSINGS = [
    "다들 맛점하세요! 🍽️", "오늘도 맛있는 점심 되세요 😋", "맛점하고 오후도 화이팅!",
    "즐거운 점심시간 보내세요~ 🙌", "잘 먹겠습니다! 다들 맛점요 😊",
]


def fallback_request():
    intro = random.choice(INTROS)
    flavor = WD_FLAVOR.get(WD_KR, "")
    line2 = (flavor + " " if flavor else "") + random.choice(NUDGES)
    return (f"🍱 오늘 점심 같이 드실 분? ({DATE_LABEL})\n\n{intro}\n{line2}\n\n"
            f"🗳️ {SITE_URL}\n\n⏰ 11시에 결과 발표할게요. 그 전까지 체크 부탁해요 😊")


def fallback_result(agg):
    lines = [f"🍽️ 오늘 점심 집계 결과 ({DATE_LABEL}) — 11시 기준", ""]
    lines.append(f"✅ 참석 {len(agg['yes'])}명" + (f": {', '.join(agg['yes'])}" if agg['yes'] else ""))
    lines.append(f"🙅 불참 {len(agg['no'])}명" + (f": {', '.join(agg['no'])}" if agg['no'] else ""))
    lines.append(f"❔ 미응답 {len(agg['pend'])}명" + (f": {', '.join(agg['pend'])}" if agg['pend'] else ""))
    if agg["pend"]:
        lines.append(random.choice(CALLOUTS).format(names=", ".join(agg["pend"])))
    lines.append("")
    if agg["ranked"]:
        for i, (r, c) in enumerate(agg["ranked"], 1):
            lines.append(f"   {i}위  {r}  —  {c}표")
        lines.append("")
        if len(agg["winners"]) == 1:
            lines.append(random.choice(WIN_ONE).format(w=agg["winners"][0], n=agg["maxc"]))
        else:
            lines.append(random.choice(WIN_TIE).format(ws=", ".join(agg["winners"]), n=agg["maxc"]))
    else:
        lines.append("🗳️ 아직 식당 투표가 없어요. 지금이라도 한 표!")
    lines.append("")
    lines.append(random.choice(CLOSINGS))
    lines.append(f"🔁 변경/확인 → {SITE_URL}")
    return "\n".join(lines)


# ---- Teams 게시 ----
def post_to_teams(text):
    card_text = text.replace("\r\n", "\n").replace("\n", "\n\n")
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4",
                "body": [{"type": "TextBlock", "text": card_text, "wrap": True}],
            },
        }],
    }
    req = urllib.request.Request(
        WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("[OK] Posted to Teams:", r.status)


def mark_sent(kind):
    """오늘 이 모드를 보냈다고 Firebase에 표식. 다음 회차가 중복 전송하지 않도록."""
    url = f"{DB_BASE}/lunch/{today}/sent/{kind}.json"
    req = urllib.request.Request(url, data=b"true", method="PUT",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            print(f"[OK] marked sent: {kind}")
    except Exception as e:
        print("[WARN] mark_sent failed:", e)


def main():
    mode  = (sys.argv[1] if len(sys.argv) > 1 else "result").strip().lower()
    force = (len(sys.argv) > 2 and sys.argv[2].lower() == "force")  # 수동 실행 시 중복가드 무시
    if now.weekday() >= 5:   # 토(5)/일(6)
        print("[SKIP] Weekend.")
        return
    if not WEBHOOK_URL:
        print("[ERROR] WEBHOOK_URL secret is not set."); sys.exit(1)

    data = fetch_today()
    sent = (data or {}).get("sent", {}) or {}
    if sent.get(mode) and not force:
        print(f"[SKIP] '{mode}' already sent today ({today}).")
        return

    agg = aggregate(data)
    if mode == "request":
        msg = generate(request_prompt()) or fallback_request()
    else:
        msg = generate(result_prompt(agg)) or fallback_result(agg)

    print("----- message -----\n" + msg + "\n-------------------")
    post_to_teams(msg)        # 실패하면 예외 발생 → 표식 안 남기고 다음 회차에 재시도
    mark_sent(mode)


if __name__ == "__main__":
    main()
