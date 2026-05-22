"""
merge.py — Incremental merge of new Apify data into master CSVs.

Reads normalized CSVs from /tmp/apify_raw/ and merges them into:
  data/reviews.csv       (dedup key: Source + ReviewerName + Date + Location)
  data/ig_posts.csv      (dedup key: ShortCode)
  data/ig_comments.csv   (dedup key: CommentId + MediaId prefix)

Also auto-tags Sentiment for new reviews (star-based), and marks Year/Month.
Sets Themes/MenuMentions to empty string for new rows (can be tagged in future).

Updates scripts/last_scrape.json on completion.

Usage:
  python scripts/merge.py
"""

import os, sys, csv, json, re
from datetime import datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
LAST_SCRAPE = os.path.join(SCRIPT_DIR, "last_scrape.json")
RAW_DIR     = "/tmp/apify_raw"

# ── Encoding fix (double-UTF-8 from some scrapers) ──────────────────────────
def fix_encoding(t):
    if not isinstance(t, str):
        return ""
    try:
        return t.encode("latin-1").decode("utf-8")
    except Exception:
        return t

# ── Sentiment from star rating ────────────────────────────────────────────────
def star_sentiment(stars_str):
    try:
        s = int(float(str(stars_str)))
    except (ValueError, TypeError):
        return "Neutral"
    if s >= 4:
        return "Positive"
    if s <= 2:
        return "Negative"
    return "Neutral"

# ── Dedup key builders ────────────────────────────────────────────────────────
def review_key(row):
    name = str(row.get("ReviewerName", "")).strip()[:40]
    date = str(row.get("Date", ""))[:10]
    loc  = str(row.get("Location", "")).strip()
    src  = str(row.get("Source", "")).strip()
    return f"{src}|{loc}|{date}|{name}"

def ig_post_key(row):
    return str(row.get("ShortCode", "")).strip()

# CommentId is unique per platform but MediaId carries precision loss.
# Use first 16 chars of MediaId prefix + CommentId for stability.
def ig_comment_key(row):
    cid = str(row.get("CommentId", "")).strip()
    mid = str(row.get("MediaId", "")).strip()[:16]
    return f"{mid}|{cid}"

# ── CSV helpers ───────────────────────────────────────────────────────────────
def read_csv(path, encoding="utf-8-sig"):
    if not os.path.exists(path):
        return []
    with open(path, encoding=encoding) as f:
        return list(csv.DictReader(f))

def write_csv(rows, fieldnames, path, encoding="utf-8-sig"):
    with open(path, "w", encoding=encoding, newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

# ── Merge reviews ─────────────────────────────────────────────────────────────
def merge_reviews():
    master_path = os.path.join(DATA_DIR, "reviews.csv")
    master_cols = [
        "Source","Location","Stars","Sentiment","Date","Year","Month",
        "ReviewerName","Title","LocalGuide","LikesCount","Themes","MenuMentions","ReviewText"
    ]

    master = read_csv(master_path)
    existing_keys = {review_key(r) for r in master}
    print(f"Reviews master: {len(master)} rows, {len(existing_keys)} unique keys")

    new_rows = []

    # Google Maps new reviews
    for fname in [f for f in os.listdir(RAW_DIR) if f.startswith("google_") and not f.endswith("_raw.csv")]:
        path = os.path.join(RAW_DIR, fname)
        rows = read_csv(path)
        print(f"  Google file {fname}: {len(rows)} rows")
        for r in rows:
            k = review_key(r)
            if k in existing_keys:
                continue
            date_str = str(r.get("Date", ""))[:10]
            parts    = date_str.split("-")
            year     = parts[0] if len(parts) >= 1 else ""
            month    = parts[1].zfill(2) if len(parts) >= 2 else ""
            new_rows.append({
                "Source":       "Google",
                "Location":     fix_encoding(str(r.get("Location", ""))),
                "Stars":        str(r.get("Stars", "")),
                "Sentiment":    star_sentiment(r.get("Stars", "")),
                "Date":         date_str,
                "Year":         year,
                "Month":        month,
                "ReviewerName": fix_encoding(str(r.get("ReviewerName", ""))),
                "Title":        "",
                "LocalGuide":   str(r.get("LocalGuide", "False")),
                "LikesCount":   str(r.get("LikesCount", "0")),
                "Themes":       "",
                "MenuMentions": "",
                "ReviewText":   fix_encoding(str(r.get("ReviewText", ""))),
            })
            existing_keys.add(k)

    # TripAdvisor new reviews
    for fname in [f for f in os.listdir(RAW_DIR) if f.startswith("tripadvisor_") and not f.endswith("_raw.csv")]:
        path = os.path.join(RAW_DIR, fname)
        rows = read_csv(path)
        print(f"  TripAdvisor file {fname}: {len(rows)} rows")
        for r in rows:
            k = review_key(r)
            if k in existing_keys:
                continue
            date_str = str(r.get("Date", ""))[:10]
            parts    = date_str.split("-")
            year     = parts[0] if len(parts) >= 1 else ""
            month    = parts[1].zfill(2) if len(parts) >= 2 else ""
            new_rows.append({
                "Source":       "TripAdvisor",
                "Location":     fix_encoding(str(r.get("Location", ""))),
                "Stars":        str(r.get("Stars", "")),
                "Sentiment":    star_sentiment(r.get("Stars", "")),
                "Date":         date_str,
                "Year":         year,
                "Month":        month,
                "ReviewerName": fix_encoding(str(r.get("ReviewerName", ""))),
                "Title":        fix_encoding(str(r.get("Title", ""))),
                "LocalGuide":   "False",
                "LikesCount":   "0",
                "Themes":       "",
                "MenuMentions": "",
                "ReviewText":   fix_encoding(str(r.get("ReviewText", ""))),
            })
            existing_keys.add(k)

    # Sort all rows by Date desc, then append
    new_rows.sort(key=lambda r: r.get("Date", ""), reverse=True)
    combined = new_rows + master
    combined.sort(key=lambda r: r.get("Date", ""), reverse=True)

    write_csv(combined, master_cols, master_path)
    print(f"Reviews: {len(master)} → {len(combined)} (+{len(new_rows)} new)")
    return len(combined), len(new_rows)

# ── Merge Instagram Posts ─────────────────────────────────────────────────────
def merge_ig_posts():
    master_path = os.path.join(DATA_DIR, "ig_posts.csv")
    master_cols = ["PostUrl","ShortCode","Date","Type","Likes","Comments","Hashtags","Caption"]

    master = read_csv(master_path)
    existing_keys = {ig_post_key(r) for r in master}
    print(f"\nIG Posts master: {len(master)} rows")

    raw_path = os.path.join(RAW_DIR, "ig_posts.csv")
    if not os.path.exists(raw_path):
        print("  No ig_posts.csv in raw dir — skipping")
        return len(master), 0

    new_data = read_csv(raw_path)
    new_rows = []
    for r in new_data:
        k = ig_post_key(r)
        if k in existing_keys:
            continue
        new_rows.append({
            "PostUrl":   str(r.get("PostUrl", "")),
            "ShortCode": k,
            "Date":      str(r.get("Date", "")),
            "Type":      str(r.get("Type", "")).replace("GraphSidecar", "Sidecar"),
            "Likes":     str(r.get("Likes", "0")),
            "Comments":  str(r.get("Comments", "0")),
            "Hashtags":  fix_encoding(str(r.get("Hashtags", ""))),
            "Caption":   fix_encoding(str(r.get("Caption", ""))),
        })
        existing_keys.add(k)

    combined = new_rows + master
    combined.sort(key=lambda r: r.get("Date", ""), reverse=True)

    write_csv(combined, master_cols, master_path)
    print(f"IG Posts: {len(master)} → {len(combined)} (+{len(new_rows)} new)")
    return len(combined), len(new_rows)

# ── Merge Instagram Comments ──────────────────────────────────────────────────
def merge_ig_comments():
    master_path = os.path.join(DATA_DIR, "ig_comments.csv")
    master_cols = ["Username","FullName","CommentText","Likes","Date","MediaId","CommentId"]

    master = read_csv(master_path)
    existing_keys = {ig_comment_key(r) for r in master}
    print(f"\nIG Comments master: {len(master)} rows")

    raw_path = os.path.join(RAW_DIR, "ig_comments.csv")
    if not os.path.exists(raw_path):
        print("  No ig_comments.csv in raw dir — skipping")
        return len(master), 0

    new_data = read_csv(raw_path)
    new_rows = []
    for r in new_data:
        k = ig_comment_key(r)
        if k in existing_keys:
            continue
        new_rows.append({
            "Username":    fix_encoding(str(r.get("Username", ""))),
            "FullName":    fix_encoding(str(r.get("FullName", ""))),
            "CommentText": fix_encoding(str(r.get("CommentText", ""))),
            "Likes":       str(r.get("Likes", "0")),
            "Date":        str(r.get("Date", ""))[:10],
            "MediaId":     str(r.get("MediaId", "")),
            "CommentId":   str(r.get("CommentId", "")),
        })
        existing_keys.add(k)

    combined = new_rows + master
    combined.sort(key=lambda r: r.get("Date", ""), reverse=True)

    write_csv(combined, master_cols, master_path)
    print(f"IG Comments: {len(master)} → {len(combined)} (+{len(new_rows)} new)")
    return len(combined), len(new_rows)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not os.path.isdir(RAW_DIR):
        print(f"ERROR: Raw dir {RAW_DIR} does not exist. Run run_apify.py first.", file=sys.stderr)
        sys.exit(1)

    print("=== Merging Apify data into master CSVs ===\n")

    total_reviews, new_reviews = merge_reviews()
    total_posts,   new_posts   = merge_ig_posts()
    total_cmts,    new_cmts    = merge_ig_comments()

    # Update last_scrape.json
    scrape_info = {
        "date":        datetime.utcnow().strftime("%Y-%m-%d"),
        "reviews":     total_reviews,
        "ig_posts":    total_posts,
        "ig_comments": total_cmts,
        "new_reviews":     new_reviews,
        "new_ig_posts":    new_posts,
        "new_ig_comments": new_cmts,
    }
    with open(LAST_SCRAPE, "w") as f:
        json.dump(scrape_info, f, indent=2)

    print(f"\n=== merge.py complete ===")
    print(f"  Reviews:     {total_reviews} total (+{new_reviews} new)")
    print(f"  IG Posts:    {total_posts} total (+{new_posts} new)")
    print(f"  IG Comments: {total_cmts} total (+{new_cmts} new)")
    print(f"  last_scrape.json updated")

if __name__ == "__main__":
    main()
