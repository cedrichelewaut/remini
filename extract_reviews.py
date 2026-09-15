#!/usr/bin/env python3
"""Fetch Google Play / App Store reviews for an app and bucket feature requests.

Fetches from multiple store countries in parallel, since each country has
its own (capped) pool of reviews - that's how you get thousands instead of
a few hundred.

Usage:
    python extract_reviews.py --store play
    python extract_reviews.py --store both --countries us,gb,in,id,br
"""
import argparse
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from google_play_scraper import Sort, reviews

# Countries with large Remini user bases, to maximize distinct reviews pulled
# (App Store's RSS feed and Play Store's review pool are both per-country).
DEFAULT_COUNTRIES = [
    "us", "gb", "ca", "au", "in", "id", "br", "mx", "de", "fr",
    "it", "es", "jp", "kr", "nl", "pl", "tr", "ph", "vn", "th",
]

# Phrases that signal a reviewer is asking for or demanding something, not just
# venting. Broader than plain "I wish" phrasing - "please fix X", "needs to
# support Y", "should let me Z" are asks too.
FEATURE_REQUEST_SIGNALS = [
    r"\bi wish\b",
    r"\bwish (it|there|you|they)\b",
    r"please (add|implement|support|fix|improve|bring back|allow|let|make|review|consider)",
    r"could you (add|implement|please)",
    r"can (you|we) (add|get|have)",
    r"would be (nice|great|awesome|good|cool|amazing)",
    r"would love (to see|it if)",
    r"it would be (nice|great|good|amazing) if",
    r"should (have|add|include|be able|let|allow|offer)",
    r"need(s)? (a|an|to have|to be able|to support|to add|to improve|improvement)",
    r"hope (you|they) (add|fix|improve|bring|consider)",
    r"hoping (for|they|you)",
    r"feature request",
    r"missing (a|an|the)",
    r"add (a|an|the)? ?option",
    r"add support for",
    r"why (isn'?t|is there no|can'?t|doesn'?t)",
    r"\bsuggestion\b",
    r"give (us|me) (the|an) option",
    r"(the )?ability to \w+",
    r"an option to\b",
    r"let (us|me) \w+",
    r"allow (us|me) to",
    r"want(ed)? the ability",
    r"if only (it|you|there)",
]

# Keyword buckets used to group flagged reviews by the feature they're asking about.
# Built from an initial keyword pass + a manual read of everything that didn't
# match anything, to catch real phrasing ("group shot", "render queue", "bring
# back the old algorithm"...) that a narrower keyword list would miss.
FEATURE_KEYWORDS = {
    "pricing / subscription": [
        "subscription", "\\bprice\\b", "pricing", "expensive", "free trial", "paywall",
        "\\bcost\\b", "one.time (purchase|price|fee)", "lifetime (purchase|plan|price)",
        "refund", "cancel my (account|subscription)", "charg(e|ed|ing) my card", "afford",
    ],
    "usage limits / free tier": [
        "photos per day", "free photos", "\\bfree version\\b", "\\bquota\\b",
        "\\b5x\\b", "\\b10x\\b", "limit(ing)? the (number|amount)", "free daily",
    ],
    "ads": ["\\bads\\b", "\\bad\\b", "advertisement", "advert"],
    "batch / bulk / queue processing": [
        "batch", "bulk", "multiple photos at once", "multiple images", "render queue",
        "process (several|multiple)", "one at a time", "all at once", "\\bqueue\\b",
    ],
    "improving AI model / output quality": [
        "ai model", "the algorithm", "\\baccuracy\\b", "unrealistic", "distort", "blurry face",
        "plastic look", "over.smooth", "\\bartifact\\b", "doesn'?t look like me",
        "fake looking", "uncanny", "hallucinat", "quality of the (result|output|edit)",
        "too smooth", "cartoonish", "overly edit", "bring back the old (algorithm|version)",
        "old version was (better|amazing)", "too aggressive", "extra (arm|leg|limb|finger)",
        "more variation", "unnatural", "\\bvariety\\b", "skin (color|colour|tone)", "melanin",
        "facial (details|features)", "\\bmoles\\b", "\\bfreckles\\b", "facial scars",
    ],
    "manual editing controls / intensity slider": [
        "manual editing", "customize the functionality", "degree of enhanc",
        "level of (the )?enhanc", "adjust the (strength|intensity)", "control the (strength|intensity)",
        "increase or decrease the",
    ],
    "background / full-photo enhancement": [
        "enhance (more than|the rest of|the whole)", "blur.*background", "blur the bg",
        "background enhancer",
    ],
    # The stylized "AI Photo" generation feature applied to 2+ people - a couple,
    # friends, family - as opposed to enhancing an existing photo (see below).
    # This is the priority segment: evidence of an existing/removed "couples"
    # feature people are actively looking for.
    "AI photo generation: couples / multi-person": [
        "couples?( ai)? (photo )?generat", "couples? edition", "photos? of couples?",
        "couple photos? (from|with|of) individual photos", "ai photos? (from|of|with) (couples?|friends|family)",
        "generat(e|ing) (a |an )?(couple|group|family) (ai )?photo",
        "create (more )?couple photos",
    ],
    # Quality gap in the Enhance/upscale feature specifically when 2+ people
    # are in the source photo, as opposed to generation or single-subject work.
    "multi-person photo enhancement quality": [
        "group (photo|pic|picture|shot)", "multiple faces", "several faces",
        "many faces", "two people", "several people", "everyone in the photo",
        "other people in the (photo|picture)", "blur(ring)? other people", "extra (person|people)",
        "random (ai )?(people|women|men|faces)", "more than one person",
        "edit both of us",
    ],
    "multi-user / family accounts": [
        "multiple accounts", "family plan", "share (my |the )?subscription", "multiple users",
        "separate profiles", "family sharing", "add another user", "second account",
        "kids account", "shared account", "multi.?user",
    ],
    "delete my data / uploaded photos": [
        "delete (option|key|my data|the pictures|my photos|my pictures)", "delete.*data",
    ],
    "cancel in-progress processing": ["cancel the generat", "stop the (process|generat)"],
    "new filters / styles": ["add .* filter", "\\bfilter\\b.*please", "taller", "fighting ai"],
    "dark mode": ["dark mode", "night mode"],
    "undo / redo": [r"\bundo\b", r"\bredo\b"],
    "video enhancement": ["video enhance", "enhance video", "video quality", "video support"],
    "export / original quality": ["export", "original resolution", "full resolution", "watermark"],
    "offline mode": ["offline"],
    "speed / performance": ["\\bslow\\b", "\\bfaster\\b"],
    "language support": ["\\blanguage\\b", "\\btranslate\\b"],
    # Not feature requests, but the broadened "please fix" signal catches these -
    # bucketed separately so they don't masquerade as feature asks in "other".
    "bugs / stability (not a feature request)": [
        "network connection", "no internet", "something went wrong", "won'?t open",
        "wont open", "keeps? crashing", "\\bglitch", "doesn'?t work anymore",
        "stopped working", "error message", "reinstall(ed|ing)?",
    ],
}


def fetch_play_store_reviews(app_id, country="us", lang="en", count=1000, retries=4):
    all_reviews = []
    token = None
    while len(all_reviews) < count:
        batch = None
        for attempt in range(retries + 1):
            batch, token = reviews(
                app_id,
                lang=lang,
                country=country,
                sort=Sort.MOST_RELEVANT,
                count=min(200, count - len(all_reviews)),
                continuation_token=token,
            )
            if batch:
                break
            if attempt < retries:
                time.sleep(4 * (attempt + 1))  # likely rate-limited, back off and retry
        if not batch:
            break
        all_reviews.extend(batch)
        if token is None:
            break
    return [
        {
            "source": "play_store",
            "id": r.get("reviewId"),
            "country": country,
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
                    "country": country,
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


def fetch_multi_country(fetch_one, countries, max_workers, label, stagger_seconds=0):
    """Run fetch_one(country) across countries in parallel, deduping by review id.

    stagger_seconds inserts a delay between submitting successive tasks, to
    avoid firing a burst of requests that looks like abuse to the target API.
    """
    all_reviews = []
    seen_ids = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, country in enumerate(countries):
            if stagger_seconds and i > 0:
                time.sleep(stagger_seconds)
            futures[pool.submit(fetch_one, country)] = country
        for future in as_completed(futures):
            country = futures[future]
            try:
                batch = future.result()
            except Exception as exc:
                print(f"  [{label}/{country}] failed: {exc}")
                continue
            new = 0
            for r in batch:
                rid = r.get("id")
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)
                all_reviews.append(r)
                new += 1
            print(f"  [{label}/{country}] +{new} new reviews (running total {len(all_reviews)})")
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
            # Keyword entries are regex fragments (not escaped) so patterns can use
            # groups/alternation/\b themselves - see the false-positive fix history.
            if any(re.search(kw, lower) for kw in keywords):
                buckets[feature].append(r)
                matched = True
        if not matched:
            buckets["other / uncategorized"].append(r)
    return buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play-id", default="com.bigwinepot.nwdn.international", help="Play Store package id")
    parser.add_argument("--appstore-id", default="1470373330", help="App Store numeric app id")
    parser.add_argument(
        "--countries",
        default=",".join(DEFAULT_COUNTRIES),
        help="Comma-separated store country codes to pull from (each country has its own review pool)",
    )
    parser.add_argument("--lang", default="en", help="Play Store review language")
    parser.add_argument("--count", type=int, default=1000, help="Play Store reviews to fetch PER COUNTRY")
    parser.add_argument(
        "--appstore-pages", type=int, default=10, help="App Store RSS pages PER COUNTRY (~50 reviews/page, ~500 max)"
    )
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel country fetches for App Store")
    parser.add_argument(
        "--play-max-workers",
        type=int,
        default=1,
        help="Parallel country fetches for Play Store (kept low; Play throttles concurrent scraping)",
    )
    parser.add_argument(
        "--play-stagger",
        type=float,
        default=3.0,
        help="Seconds to wait between starting each Play Store country fetch",
    )
    parser.add_argument("--store", choices=["play", "appstore", "both", "file"], default="both")
    parser.add_argument("--input", help="Path to a .json or .txt file of reviews (required for --store file)")
    parser.add_argument(
        "--reclassify",
        help="Path to a previous reviews_output.json - reclassify its all_reviews without re-fetching",
    )
    parser.add_argument("--out", default="reviews_output.json")
    parser.add_argument("--debug", action="store_true", help="Print per-source text stats and samples")
    args = parser.parse_args()

    if args.reclassify:
        print(f"Reclassifying reviews from {args.reclassify} (no network calls)...")
        with open(args.reclassify, encoding="utf-8") as f:
            prior = json.load(f)
        all_reviews = prior["all_reviews"]
        print(f"  loaded {len(all_reviews)} reviews")
        feature_buckets = classify_feature_requests(all_reviews)
        print("\nFeature request summary:")
        for feature, items in sorted(feature_buckets.items(), key=lambda kv: -len(kv[1])):
            print(f"  {feature}: {len(items)} mentions")
        output = {
            "app": prior.get("app"),
            "total_reviews_fetched": len(all_reviews),
            "all_reviews": all_reviews,
            "feature_requests": {
                feature: [
                    {
                        "source": r["source"],
                        "country": r.get("country"),
                        "rating": r["rating"],
                        "date": r["date"],
                        "text": r["text"],
                    }
                    for r in items
                ]
                for feature, items in feature_buckets.items()
            },
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nSaved reclassified results to {args.out}")
        return

    countries = [c.strip() for c in args.countries.split(",") if c.strip()]

    all_reviews = []
    if args.store == "file":
        print(f"Loading reviews from {args.input}...")
        file_reviews = load_reviews_from_file(args.input)
        print(f"  got {len(file_reviews)} reviews")
        all_reviews.extend(file_reviews)

    if args.store in ("play", "both"):
        print(f"Fetching Play Store reviews for {args.play_id} across {len(countries)} countries...")
        play_reviews = fetch_multi_country(
            lambda country: fetch_play_store_reviews(args.play_id, country=country, lang=args.lang, count=args.count),
            countries,
            args.play_max_workers,
            "play",
            stagger_seconds=args.play_stagger,
        )
        print(f"  Play Store total: {len(play_reviews)} reviews")
        all_reviews.extend(play_reviews)

    if args.store in ("appstore", "both"):
        print(f"Fetching App Store reviews for {args.appstore_id} across {len(countries)} countries...")
        appstore_reviews = fetch_multi_country(
            lambda country: fetch_app_store_reviews(args.appstore_id, country=country, pages=args.appstore_pages),
            countries,
            args.max_workers,
            "appstore",
        )
        print(f"  App Store total: {len(appstore_reviews)} reviews")
        all_reviews.extend(appstore_reviews)

    if args.debug:
        by_source = defaultdict(list)
        for r in all_reviews:
            by_source[r["source"]].append(r)
        for source, items in by_source.items():
            non_empty = [r for r in items if (r.get("text") or "").strip()]
            print(f"\n[debug] {source}: {len(items)} total, {len(non_empty)} with non-empty text")
            for r in non_empty[:5]:
                print(f"    sample: {r['text'][:150]!r}")

    feature_buckets = classify_feature_requests(all_reviews)

    print(f"\nTotal reviews fetched: {len(all_reviews)}")
    print("Feature request summary:")
    for feature, items in sorted(feature_buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {feature}: {len(items)} mentions")

    output = {
        "app": {"play_id": args.play_id, "appstore_id": args.appstore_id, "countries": countries},
        "total_reviews_fetched": len(all_reviews),
        "all_reviews": all_reviews,
        "feature_requests": {
            feature: [
                {"source": r["source"], "country": r.get("country"), "rating": r["rating"], "date": r["date"], "text": r["text"]}
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
