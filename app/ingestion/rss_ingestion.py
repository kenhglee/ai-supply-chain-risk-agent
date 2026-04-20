import feedparser

def load_headlines_from_rss() -> list[dict]:
    rss_url = "https://news.google.com/rss/search?q=TSMC+OR+Foxconn+OR+Murata&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(rss_url)

    alerts = []

    for idx, entry in enumerate(feed.entries[:10], start=1):
        alerts.append({
            "alert_id": "",
            "headline": entry.title,
            "source": "google_news_rss",
            "status": "new",
        })

    if not alerts:
        alerts = [
            {
                "alert_id": "",
                "headline": "Murata faces disruption due to earthquake in Japan",
                "source": "sample",
                "status": "new",
            },
            {
                "alert_id": "",
                "headline": "Taiwan earthquake disrupts semiconductor operations",
                "source": "sample",
                "status": "new",
            },
            {
                "alert_id": "",
                "headline": "Flooding affects factories in Europe",
                "source": "sample",
                "status": "new",
            },
        ]
        print("(Using sample headlines - RSS feed returned empty)\n")

    return alerts
