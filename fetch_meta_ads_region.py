import os
import json
import requests
from google.cloud import bigquery
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"]

BACKFILL_MODE = os.environ.get("BACKFILL_MODE", "false").lower() == "true"
BACKFILL_START = os.environ.get("BACKFILL_START", "2026-02-23")
BACKFILL_END = os.environ.get("BACKFILL_END", "2026-05-31")
ROLLING_DAYS = int(os.environ.get("ROLLING_DAYS", "7"))
# true면 BigQuery에 쓰지 않고 가져온 결과만 출력한다(소급 적재 전 확인용)
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

TABLE_ID = "beautyrella-dashboard.beautyrella_ads.meta_ads_region"
SCHEMA = [
    bigquery.SchemaField("date", "DATE"),
    bigquery.SchemaField("campaign_name", "STRING"),
    bigquery.SchemaField("region", "STRING"),
    bigquery.SchemaField("spend", "FLOAT"),
    bigquery.SchemaField("installs", "INTEGER"),
    bigquery.SchemaField("clicks", "INTEGER"),
    bigquery.SchemaField("impressions", "INTEGER"),
]


def fetch_meta_ads_region(since, until):
    """캠페인 x 지역(시/도). OS는 캠페인명의 [AOS]/[iOS]로 구분하므로 캠페인 단위로 받는다.
    Meta는 시/군/구 단위 성과를 제공하지 않고, iOS 설치는 지역을 특정하지 못해 Unknown으로 잡힌다."""
    url = f"https://graph.facebook.com/v19.0/{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": ACCESS_TOKEN,
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": "campaign_name,impressions,clicks,spend,actions",
        "breakdowns": "region",
        "level": "campaign",
        "limit": 500,
    }
    rows = []
    while url:
        data = requests.get(url, params=params).json()
        if "error" in data:
            raise Exception(f"Meta API 오류 - {data['error']['message']}")
        for item in data.get("data", []):
            spend = float(item.get("spend", 0))
            installs = int(next((a["value"] for a in item.get("actions", []) if a["action_type"] == "mobile_app_install"), 0))
            if spend == 0 and installs == 0:
                continue
            rows.append({
                "date": since,
                "campaign_name": item.get("campaign_name", ""),
                "region": item.get("region", "Unknown"),
                "impressions": int(item.get("impressions", 0)),
                "clicks": int(item.get("clicks", 0)),
                "spend": spend,
                "installs": installs,
            })
        # 다음 페이지 URL에는 파라미터가 이미 들어 있다
        url = data.get("paging", {}).get("next")
        params = None
    return rows


def ensure_table():
    client = bigquery.Client()
    client.create_table(bigquery.Table(TABLE_ID, schema=SCHEMA), exists_ok=True)


def delete_existing_date(date_str):
    client = bigquery.Client()
    query = f"DELETE FROM `{TABLE_ID}` WHERE date = @date"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
    )
    client.query(query, job_config=job_config).result()


def upload_to_bigquery(rows):
    client = bigquery.Client()
    job_config = bigquery.LoadJobConfig(schema=SCHEMA, write_disposition="WRITE_APPEND")
    client.load_table_from_json(rows, TABLE_ID, job_config=job_config).result()
    print(f"{len(rows)}행 BigQuery 적재 완료")


def process(date_str):
    try:
        rows = fetch_meta_ads_region(date_str, date_str)
    except Exception as e:
        print(f"{date_str} - 가져오기 실패, 다음 날짜로 진행: {e}")
        return
    spend = sum(r["spend"] for r in rows)
    installs = sum(r["installs"] for r in rows)
    print(f"{date_str} - {len(rows)}행, 광고비 {spend:,.0f}, 설치 {installs:,}")
    if DRY_RUN:
        return
    delete_existing_date(date_str)
    if rows:
        upload_to_bigquery(rows)


if __name__ == "__main__":
    if not DRY_RUN:
        ensure_table()
    if BACKFILL_MODE:
        current = datetime.strptime(BACKFILL_START, "%Y-%m-%d")
        end = datetime.strptime(BACKFILL_END, "%Y-%m-%d")
        while current <= end:
            process(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
    else:
        # 최근 N일치를 매번 삭제 후 재수집 — 메타의 소급 설치 반영(최대 7일)을 따라잡기 위함
        today_kst = datetime.now(KST)
        for i in range(1, ROLLING_DAYS + 1):
            process((today_kst - timedelta(days=i)).strftime("%Y-%m-%d"))
