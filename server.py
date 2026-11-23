import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AZ Crypto Intelligence API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
RISK_KEYWORDS = [
    "hack", "hacked", "exploit", "exploited", "vulnerability",
    "breach", "compromised", "delist", "delisting", "monitoring tag",
    "halt", "halted", "insolvent", "lawsuit", "sec ", "rug"
]

CACHE = {}
CACHE_TTL = 900


def fetch_google_news(coin_name: str, symbol: str):
    q = urllib.parse.quote(f'"{coin_name}" OR "{symbol}" crypto')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    risk_hits = []

    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for item in root.findall("./channel/item")[:8]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            lower_title = title.lower()
            matched = [kw for kw in RISK_KEYWORDS if kw in lower_title]
            if matched:
                risk_hits.append({"title": title, "keywords": matched, "link": link})
            items.append({
                "title": title,
                "link": link,
                "pubDate": pub_date,
                "isRisk": len(matched) > 0
            })
    except Exception as e:
        return {"ok": False, "error": str(e), "items": [], "riskHits": []}

    return {"ok": True, "items": items[:5], "riskHits": risk_hits}


def fetch_x_via_official_api(handle: str):
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    u_resp = requests.get(
        f"https://api.x.com/2/users/by/username/{handle}",
        headers=headers,
        params={"user.fields": "public_metrics,created_at"},
        timeout=10,
    )
    if u_resp.status_code != 200:
        raise RuntimeError(f"X user lookup HTTP {u_resp.status_code}")
    user_data = u_resp.json().get("data")
    if not user_data:
        raise RuntimeError("X user not found")

    uid = user_data["id"]
    followers = user_data.get("public_metrics", {}).get("followers_count", 0)

    t_resp = requests.get(
        f"https://api.x.com/2/users/{uid}/tweets",
        headers=headers,
        params={
            "max_results": 10,
            "exclude": "retweets,replies",
            "tweet.fields": "created_at,public_metrics",
        },
        timeout=10,
    )
    if t_resp.status_code != 200:
        raise RuntimeError(f"X tweets HTTP {t_resp.status_code}")

    tweets = t_resp.json().get("data") or []
    return normalize_x_posts(handle, tweets, followers, source="x_api_v2")


def fetch_x_via_rss_fallback(handle: str):
    mirrors = [
        f"https://nitter.poast.org/{handle}/rss",
        f"https://nitter.privacydev.net/{handle}/rss",
        f"https://xcancel.com/{handle}/rss",
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    last_err = "No mirror reachable"

    for m_url in mirrors:
        try:
            r = requests.get(m_url, headers=headers, timeout=8)
            if r.status_code != 200 or "<item>" not in r.text:
                continue
            root = ET.fromstring(r.text)
            tweets = []
            for item in root.findall("./channel/item")[:10]:
                title = (item.findtext("title") or "").strip()
                if title.startswith("RT by "):
                    continue
                pub = item.findtext("pubDate") or ""
                dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
                tweets.append({
                    "text": title,
                    "created_at": dt.isoformat(),
                    "public_metrics": {"like_count": 0, "retweet_count": 0, "reply_count": 0},
                })
            return normalize_x_posts(handle, tweets, followers=None, source="public_rss_mirror")
        except Exception as e:
            last_err = str(e)

    return {
        "ok": False,
        "handle": handle,
        "status": "unchecked",
        "error": f"X automated fetch unavailable ({last_err}).",
        "recentPosts": [],
    }


def normalize_x_posts(handle: str, tweets: list, followers, source: str):
    now = datetime.now(timezone.utc)
    if not tweets:
        return {
            "ok": True,
            "source": source,
            "handle": handle,
            "status": "dormant",
            "daysSinceLastPost": 999,
            "avgEngagement": 0,
            "engagementRate": None,
            "followers": followers,
            "recentPosts": [],
        }

    parsed_posts = []
    engagements = []

    for tw in tweets:
        raw_dt = tw.get("created_at", "")
        dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        pm = tw.get("public_metrics") or {}
        eng = pm.get("like_count", 0) + pm.get("retweet_count", 0) + pm.get("reply_count", 0)
        engagements.append(eng)
        parsed_posts.append({
            "text": (tw.get("text") or "")[:180],
            "createdAt": dt.isoformat(),
            "daysAgo": round(age_days, 1),
            "likes": pm.get("like_count", 0),
            "reposts": pm.get("retweet_count", 0),
            "replies": pm.get("reply_count", 0),
            "totalEngagement": eng,
        })

    parsed_posts.sort(key=lambda x: x["daysAgo"])
    days_since_last = parsed_posts[0]["daysAgo"]
    avg_eng = round(sum(engagements) / len(engagements), 1)
    eng_rate = round((avg_eng / followers) * 100, 3) if followers and followers > 0 else None

    if days_since_last > 14:
        status = "dormant"
    elif days_since_last > 5 or (followers and avg_eng < 15):
        status = "low"
    else:
        status = "active"

    return {
        "ok": True,
        "source": source,
        "handle": handle,
        "status": status,
        "daysSinceLastPost": days_since_last,
        "avgEngagement": avg_eng,
        "engagementRate": eng_rate,
        "followers": followers,
        "recentPosts": parsed_posts[:3],
    }


@app.get("/api/analyze")
def analyze_coin(
    symbol: str = Query(...),
    name: str = Query(...),
    handle: str = Query(""),
    force: bool = Query(False),
):
    key = f"{symbol.upper()}:{handle.lower()}"
    now_ts = time.time()
    if not force and key in CACHE and (now_ts - CACHE[key]["ts"] < CACHE_TTL):
        return CACHE[key]["data"]

    clean_handle = re.sub(r"^@+", "", handle.strip())
    if clean_handle:
        if X_BEARER_TOKEN:
            try:
                x_data = fetch_x_via_official_api(clean_handle)
            except Exception as e:
                x_data = {"ok": False, "handle": clean_handle, "status": "unchecked", "error": str(e), "recentPosts": []}
        else:
            x_data = fetch_x_via_rss_fallback(clean_handle)
    else:
        x_data = {"ok": False, "handle": "", "status": "unchecked", "error": "No handle provided", "recentPosts": []}

    news_data = fetch_google_news(name, symbol)

    risk_flags = []
    if x_data.get("status") == "dormant":
        risk_flags.append(f"Team dormant on X ({x_data.get('daysSinceLastPost')}d since last post)")
    elif x_data.get("status") == "low":
        risk_flags.append(f"Slow X activity ({x_data.get('daysSinceLastPost')}d since last post)")

    if news_data.get("riskHits"):
        risk_flags.append(f"{len(news_data['riskHits'])} security/negative news hit(s)")

    result = {
        "symbol": symbol.upper(),
        "name": name,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "x": x_data,
        "news": news_data,
        "riskFlags": risk_flags,
        "preMonitoringRisk": len(risk_flags) > 0,
    }
    CACHE[key] = {"ts": now_ts, "data": result}
    return result
