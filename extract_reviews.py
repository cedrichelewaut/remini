#!/usr/bin/env python3
"""Fetch Google Play / App Store reviews for an app and bucket feature requests.

Usage:
    python extract_reviews.py --store play
    python extract_reviews.py --store both --count 500
"""
import argparse
import json
import re
from collections import defaultdict

import requests
from google_play_scraper import Sort, reviews

# Phrases that signal a reviewer is asking for something, not just reporting a bug.
FEATURE_REQUEST_SIGNALS = [
    r"\bi wish\b",
    r"\bwish (it|there|you|they)\b",
    r"please add",
    r"please implement",
    r"please support",
    r"could you add",
    r"can you add",
    r"would be (nice|great|awesome|good|cool)",
    r"it would be (nice|great|good) if",
    r"should (have|add|include)",
    r"need(s)? (a|an|to have)",
    r"hope (you|they) add",
    r"feature request",
    r"missing (a|an|the)",
    r"add (a|an|the)? ?option",
    r"add support for",
    r"why (isn'?t|is there no)",
    r"\bsuggestion\b",
]

# Keyword buckets used to group flagged reviews by the feature they're asking about.
FEATURE_KEYWORDS = {
    "dark mode": ["dark mode", "night mode"],
    "undo / redo": ["undo", "redo"],
    "batch / bulk processing": ["batch", "bulk", "multiple photos at once", "multiple images"],
    "video enhancement": ["video enhance", "enhance video", "video quality", "video support"],
    "export / original quality": ["export", "original resolution", "full resolution", "watermark"],
    "pricing / subscription": ["subscription", "price", "expensive", "free trial", "paywall", "cost"],
    "offline mode": ["offline"],
    "face / detail accuracy": ["distort", "blurry face", "plastic look", "over-smooth", "artifact"],
    "speed / performance": ["slow", "faster"],
    "language support": ["language", "translate"],
}


def fetch_play_store_reviews(app_id, country="us", lang="en", count=200):
    all_reviews = []
    token = None
    while len(all_reviews) < count:
        batch, token = reviews(
            app_id,
            lang=lang,
            country=country,
            sort=Sort.NEWEST,
            count=min(200, count - len(all_reviews)),
            continuation_token=token,
        )
        if not batch:
            break
        all_reviews.extend(batch)
        if token is None:
            break
    return [
        {
            "source": "play_store",
            "id": r.get("reviewId"),
            "author": r.get("userName"),
            "rating": r.get("score"),
            "date": str(r.get("at")),
            "text": r.get("content") or "",
        }
        for r in all_reviews
    ]


def fetch_app_store_reviews(app_id, country="us", pages=10):
    all_reviews = []
    seen_ids = set()
    for page in range(1, pages + 1):
        url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/json"
        )
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            break
        entries = resp.json().get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            break
        got_review = False
        for e in entries:
            if "im:rating" not in e:
                continue  # first entry on page 1 is app metadata, not a review
            rid = e.get("id", {}).get("label")
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            all_reviews.append(
                {
                    "source": "app_store",
                    "id": rid,
                    "author": e.get("author", {}).get("name", {}).get("label"),
                    "rating": int(e.get("im:rating", {}).get("label", 0)),
                    "date": e.get("updated", {}).get("label"),
                    "text": e.get("content", {}).get("label", ""),
                }
            )
            got_review = True
        if not got_review:
            break
    return all_reviews


def load_reviews_from_file(path):
    """Load reviews pasted/exported manually, for when the store APIs aren't reachable.

    Accepts a .json file (a list of strings, or a list of objects with a
    "text" field and optional "rating"/"date"), or a .txt file with one
    review per line.
    """
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            data = json.load(f)
            out = []
            for item in data:
                if isinstance(item, str):
                    out.append({"source": "manual", "rating": None, "date": None, "text": item})
                else:
                    out.append(
                        {
                            "source": item.get("source", "manual"),
                            "rating": item.get("rating"),
                            "date": item.get("date"),
                            "text": item.get("text", ""),
                        }
                    )
            return out
        return [
            {"source": "manual", "rating": None, "date": None, "text": line.strip()}
            for line in f
            if line.strip()
        ]


def classify_feature_requests(all_reviews):
    buckets = defaultdict(list)
    for r in all_reviews:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        lower = text.lower()
        if not any(re.search(p, lower) for p in FEATURE_REQUEST_SIGNALS):
            continue
        matched = False
        for feature, keywords in FEATURE_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                buckets[feature].append(r)
                matched = True
        if not matched:
            buckets["other / uncategorized"].append(r)
    return buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play-id", default="com.bigwinepot.nwdn.international", help="Play Store package id")
    parser.add_argument("--appstore-id", default="1470373330", help="App Store numeric app id")
    parser.add_argument("--country", default="us")
    parser.add_argument("--lang", default="en", help="Play Store review language")
    parser.add_argument("--count", type=int, default=200, help="Number of Play Store reviews to fetch")
    parser.add_argument("--appstore-pages", type=int, default=10, help="App Store RSS pages (~50 reviews/page)")
    parser.add_argument("--store", choices=["play", "appstore", "both", "file"], default="both")
    parser.add_argument("--input", help="Path to a .json or .txt file of reviews (required for --store file)")
    parser.add_argument("--out", default="reviews_output.json")
    args = parser.parse_args()

    all_reviews = []
    if args.store == "file":
        print(f"Loading reviews from {args.input}...")
        file_reviews = load_reviews_from_file(args.input)
        print(f"  got {len(file_reviews)} reviews")
        all_reviews.extend(file_reviews)

    if args.store in ("play", "both"):
        print(f"Fetching Play Store reviews for {args.play_id}...")
        play_reviews = fetch_play_store_reviews(args.play_id, country=args.country, lang=args.lang, count=args.count)
        print(f"  got {len(play_reviews)} reviews")
        all_reviews.extend(play_reviews)

    if args.store in ("appstore", "both"):
        print(f"Fetching App Store reviews for {args.appstore_id}...")
        appstore_reviews = fetch_app_store_reviews(args.appstore_id, country=args.country, pages=args.appstore_pages)
        print(f"  got {len(appstore_reviews)} reviews")
        all_reviews.extend(appstore_reviews)

    feature_buckets = classify_feature_requests(all_reviews)

    print("\nFeature request summary:")
    for feature, items in sorted(feature_buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {feature}: {len(items)} mentions")

    output = {
        "app": {"play_id": args.play_id, "appstore_id": args.appstore_id},
        "total_reviews_fetched": len(all_reviews),
        "feature_requests": {
            feature: [
                {"source": r["source"], "rating": r["rating"], "date": r["date"], "text": r["text"]}
                for r in items
            ]
            for feature, items in feature_buckets.items()
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSaved full results to {args.out}")


if __name__ == "__main__":
    main()
