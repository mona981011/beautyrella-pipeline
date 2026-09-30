"""전일 광고 데이터 적재 결과를 확인해 Slack 스레드에 댓글로 보고한다.

claude.ai 루틴 샌드박스는 아웃바운드 네트워크가 차단되어 Slack API를 직접 호출할 수 없어,
봇 계정으로 발송하려면 이 워크플로처럼 GitHub Actions에서 실행해야 한다.
"""

import os
import requests
from google.cloud import bigquery
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0ADC72DHE3")
SLACK_THREAD_TS = os.environ.get("SLACK_THREAD_TS", "1790731794.119719")
# 적재 누락 시에만 붙일 멘션. 비우면 멘션하지 않는다.
MENTION_ON_MISSING = os.environ.get("MENTION_ON_MISSING", "<@U02GCLTMDA5>")

# (테이블, 표시명, 성과 표에 넣을지)
TABLES = [
    ("meta_ads", "메타", True),
    ("google_ads", "구글애즈", True),
    ("tiktok_ads", "틱톡", True),
    ("meta_ads_age", "메타 연령별", False),
    ("tiktok_ads_age", "틱톡 연령별", False),
]


def fetch_stats(target_date):
    client = bigquery.Client()
    union = "\nUNION ALL\n".join(
        f"SELECT '{t}' AS tbl, COUNT(*) AS rows_, SUM(spend) AS spend, SUM(installs) AS installs "
        f"FROM `beautyrella-dashboard.beautyrella_ads.{t}` WHERE date = DATE '{target_date}'"
        for t, _, _ in TABLES
    )
    return {r["tbl"]: dict(r) for r in client.query(union).result()}


def krw(v):
    return f"{round(v or 0):,}원"


def build_message(target_date, stats):
    missing = [name for t, name, _ in TABLES if not stats[t]["rows_"]]
    d = datetime.strptime(target_date, "%Y-%m-%d")
    header_date = f"*{d.month}월 {d.day}일 데이터*"

    if missing:
        mention = f" {MENTION_ON_MISSING}" if MENTION_ON_MISSING else ""
        lines = [f":warning: {header_date} — 적재 일부 누락{mention}", ""]
    else:
        lines = [f":white_check_mark: {header_date} — 적재 완료", ""]

    total_spend = total_installs = 0
    for t, name, in_table in TABLES:
        if not in_table:
            continue
        s = stats[t]
        spend, installs = s["spend"] or 0, s["installs"] or 0
        total_spend += spend
        total_installs += installs
        if not s["rows_"]:
            lines.append(f"• {name} · 데이터 없음")
            continue
        cpi = round(spend / installs) if installs else 0
        lines.append(f"• {name} · {krw(spend)} · {installs:,}건 · CPI {cpi:,}원")

    cpi_all = round(total_spend / total_installs) if total_installs else 0
    lines.append(f"• 합계 · {krw(total_spend)} · {total_installs:,}건 · CPI {cpi_all:,}원")
    lines.append("")

    if missing:
        lines.append(f"적재 누락: {', '.join(missing)}")
    else:
        lines.append("적재 상태: 전 테이블 정상 (버즈빌은 주간 수동 업로드라 점검 대상 제외)")

    # 연령별 합계가 매체 전체와 어긋나면 데이터 정합성 이슈이므로 드러낸다
    for age_tbl, main_tbl, name in [
        ("meta_ads_age", "meta_ads", "메타"),
        ("tiktok_ads_age", "tiktok_ads", "틱톡"),
    ]:
        a, m = stats[age_tbl]["installs"], stats[main_tbl]["installs"]
        if a is not None and m is not None and a != m:
            lines.append(f"※ {name} 연령별 설치수({a:,}건)가 전체({m:,}건)와 {abs(m - a):,}건 차이")

    return "\n".join(lines)


def post_to_slack(text):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "channel": SLACK_CHANNEL,
            "thread_ts": SLACK_THREAD_TS,
            "reply_broadcast": False,
            "text": text,
        },
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack 발송 실패: {data.get('error')}")
    print(f"Slack 발송 완료 (ts={data['ts']})")


if __name__ == "__main__":
    target_date = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"TARGET_DATE: {target_date}")
    stats = fetch_stats(target_date)
    for t, s in stats.items():
        print(f"  {t}: rows={s['rows_']} spend={s['spend']} installs={s['installs']}")
    post_to_slack(build_message(target_date, stats))
