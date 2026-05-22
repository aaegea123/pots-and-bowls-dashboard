"""
run_apify.py — Trigger Apify actors, poll for completion, download results.

Outputs (written to /tmp/apify_raw/):
  google_LasCatalinas.csv
  google_Rohrmoser.csv
  google_Playa Grande.csv
  tripadvisor_LasCatalinas.csv
  tripadvisor_Rohrmoser.csv
  ig_posts.csv
  ig_comments.csv

Usage:
  python scripts/run_apify.py

Env vars required:
  APIFY_TOKEN
"""

import os, sys, json, time, csv, io, re
import urllib.request, urllib.parse

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "apify_config.json")
LAST_SCRAPE  = os.path.join(SCRIPT_DIR, "last_scrape.json")
DATA_DIR     = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
OUT_DIR      = "/tmp/apify_raw"

TOKEN = os.environ.get("APIFY_TOKEN", "")
if not TOKEN:
    print("ERROR: APIFY_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

os.makedirs(OUT_DIR, exist_ok=True)

with open(CONFIG_FILE) as f:
    cfg = json.load(f)
with open(LAST_SCRAPE) as f:
    last = json.load(f)

LAST_DATE = last.get("date", "2020-01-01")
print(f"Last scrape date: {LAST_DATE}")

# ── Apify helpers ─────────────────────────────────────────────────────────────
APIFY_BASE = "https://api.apify.com/v2"

def apify_post(path, body):
    url  = f"{APIFY_BASE}{path}?token={TOKEN}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def apify_get(path):
    url = f"{APIFY_BASE}{path}?token={TOKEN}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())

def apify_get_csv(path):
    url = f"{APIFY_BASE}{path}?token={TOKEN}&format=csv&clean=true"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")

def trigger_actor(actor_id, input_body):
    resp = apify_post(f"/acts/{actor_id}/runs", input_body)
    run_id = resp["data"]["id"]
    print(f"  Triggered actor {actor_id} → run {run_id}")
    return run_id

def wait_for_run(run_id, timeout_sec=1800, poll_interval=30):
    """Poll until run is SUCCEEDED or FAILED. Returns dataset_id."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        info = apify_get(f"/actor-runs/{run_id}")
        status = info["data"]["status"]
        if status == "SUCCEEDED":
            ds_id = info["data"]["defaultDatasetId"]
            print(f"  Run {run_id} SUCCEEDED → dataset {ds_id}")
            return ds_id
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Run {run_id} ended with status: {status}")
        print(f"  Run {run_id} status: {status} — waiting {poll_interval}s...")
        time.sleep(poll_interval)
    raise TimeoutError(f"Run {run_id} timed out after {timeout_sec}s")

def download_dataset_csv(dataset_id):
    return apify_get_csv(f"/datasets/{dataset_id}/items")

def save_csv(content, filename):
    path = os.path.join(OUT_DIR, filename)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    rows = len(content.splitlines()) - 1
    print(f"  Saved {filename} ({rows} rows)")
    return path

# ── Schema-resilient field extractors ────────────────────────────────────────
# Alias maps handle Apify schema changes (column renames).

GOOGLE_ALIASES = {
    "ReviewerName": ["reviewerName", "reviewer_name", "authorName", "author"],
    "Stars":        ["stars", "rating", "reviewRating"],
    "ReviewText":   ["text", "reviewText", "review_text", "snippet"],
    "Date":         ["publishedAtDate", "publishedAt", "date", "reviewDate"],
    "LocalGuide":   ["isLocalGuide", "localGuide", "is_local_guide"],
    "LikesCount":   ["likesCount", "likes", "like_count"],
    "ReviewId":     ["reviewId", "review_id", "id"],
}

TA_ALIASES = {
    "ReviewerName": ["user.name", "reviewerName", "name", "authorName"],
    "Stars":        ["rating", "stars", "reviewRating"],
    "ReviewText":   ["text", "reviewText", "review"],
    "Date":         ["publishedDate", "publishedAt", "date"],
    "Title":        ["title", "reviewTitle"],
    "ReviewUrl":    ["url", "reviewUrl", "link"],
    "ReviewId":     ["id", "reviewId"],
}

IG_POST_ALIASES = {
    "PostUrl":   ["url", "postUrl", "shortCode"],
    "ShortCode": ["shortCode", "id", "shortcode"],
    "Date":      ["timestamp", "date", "takenAt", "taken_at"],
    "Type":      ["type", "mediaType", "media_type"],
    "Likes":     ["likesCount", "likes", "like_count"],
    "Comments":  ["commentsCount", "comments", "comment_count"],
    "Hashtags":  ["hashtags", "caption_hashtags"],
    "Caption":   ["caption", "text"],
}

IG_CMT_ALIASES = {
    "Username":    ["ownerUsername", "username", "user"],
    "FullName":    ["ownerFullName", "fullName", "name", "owner.fullName"],
    "CommentText": ["text", "comment", "commentText"],
    "Likes":       ["likesCount", "likes"],
    "Date":        ["timestamp", "date", "created_at"],
    "MediaId":     ["id", "mediaId", "postId"],
    "CommentId":   ["id", "commentId"],
}

def get_field(item, aliases):
    """Try each alias in order; return first match."""
    for alias in aliases:
        if "." in alias:
            parts = alias.split(".", 1)
            sub = item.get(parts[0], {})
            if isinstance(sub, dict):
                val = sub.get(parts[1])
                if val is not None:
                    return val
        else:
            val = item.get(alias)
            if val is not None:
                return val
    return ""

# ── Google Maps normalization ─────────────────────────────────────────────────
def normalize_google(raw_csv, location_label):
    """
    Google Maps actor returns one row per PLACE, with a nested `reviews` JSON array.
    The CSV will have a `reviews` column containing JSON. We explode it.
    Falls back to treating each row as a review if `reviews` column absent.
    """
    rows = []
    reader = csv.DictReader(io.StringIO(raw_csv))
    for place_row in reader:
        # Try nested reviews column
        reviews_json = place_row.get("reviews", "")
        if reviews_json and reviews_json.strip().startswith("["):
            try:
                reviews = json.loads(reviews_json)
            except Exception:
                reviews = []
        else:
            # Flat row = single review
            reviews = [place_row]

        for r in reviews:
            name = get_field(r, GOOGLE_ALIASES["ReviewerName"])
            stars = get_field(r, GOOGLE_ALIASES["Stars"])
            text  = get_field(r, GOOGLE_ALIASES["ReviewText"])
            date  = get_field(r, GOOGLE_ALIASES["Date"])
            local = get_field(r, GOOGLE_ALIASES["LocalGuide"])
            likes = get_field(r, GOOGLE_ALIASES["LikesCount"])
            rid   = get_field(r, GOOGLE_ALIASES["ReviewId"])

            # Normalise date to YYYY-MM-DD
            date_str = str(date)[:10] if date else ""

            rows.append({
                "Source":       "Google",
                "Location":     location_label,
                "Stars":        str(stars) if stars != "" else "",
                "Date":         date_str,
                "ReviewerName": str(name),
                "LocalGuide":   str(local),
                "LikesCount":   str(likes) if likes != "" else "0",
                "ReviewText":   str(text),
                "ReviewId":     str(rid),
            })
    return rows

def normalize_tripadvisor(raw_csv, location_label):
    rows = []
    reader = csv.DictReader(io.StringIO(raw_csv))
    for r in reader:
        name   = get_field(r, TA_ALIASES["ReviewerName"])
        stars  = get_field(r, TA_ALIASES["Stars"])
        text   = get_field(r, TA_ALIASES["ReviewText"])
        date   = get_field(r, TA_ALIASES["Date"])
        title  = get_field(r, TA_ALIASES["Title"])
        url    = get_field(r, TA_ALIASES["ReviewUrl"])
        rid    = get_field(r, TA_ALIASES["ReviewId"])

        date_str = str(date)[:10] if date else ""

        rows.append({
            "Source":       "TripAdvisor",
            "Location":     location_label,
            "Stars":        str(stars) if stars != "" else "",
            "Date":         date_str,
            "ReviewerName": str(name),
            "Title":        str(title),
            "ReviewText":   str(text),
            "ReviewUrl":    str(url),
            "ReviewId":     str(rid),
        })
    return rows

def normalize_ig_posts(raw_csv):
    rows = []
    reader = csv.DictReader(io.StringIO(raw_csv))
    for r in reader:
        post_url  = get_field(r, IG_POST_ALIASES["PostUrl"])
        shortcode = get_field(r, IG_POST_ALIASES["ShortCode"])
        date      = get_field(r, IG_POST_ALIASES["Date"])
        mtype     = get_field(r, IG_POST_ALIASES["Type"])
        likes     = get_field(r, IG_POST_ALIASES["Likes"])
        comments  = get_field(r, IG_POST_ALIASES["Comments"])
        hashtags  = get_field(r, IG_POST_ALIASES["Hashtags"])
        caption   = get_field(r, IG_POST_ALIASES["Caption"])

        # Build PostUrl from shortcode if missing
        if not post_url and shortcode:
            post_url = f"https://www.instagram.com/p/{shortcode}/"
        # Extract shortcode from URL if missing
        if not shortcode and post_url:
            m = re.search(r"/p/([^/]+)/", str(post_url))
            if m:
                shortcode = m.group(1)

        # Normalise date
        date_str = str(date)[:10] if date else ""
        mtype_str = str(mtype).replace("GraphSidecar", "Sidecar")

        rows.append({
            "PostUrl":   str(post_url),
            "ShortCode": str(shortcode),
            "Date":      date_str + (str(date)[10:] if len(str(date)) > 10 else ""),
            "Type":      mtype_str,
            "Likes":     str(likes) if likes != "" else "0",
            "Comments":  str(comments) if comments != "" else "0",
            "Hashtags":  str(hashtags),
            "Caption":   str(caption),
        })
    return rows

def normalize_ig_comments(raw_csv):
    rows = []
    reader = csv.DictReader(io.StringIO(raw_csv))
    for r in reader:
        username   = get_field(r, IG_CMT_ALIASES["Username"])
        fullname   = get_field(r, IG_CMT_ALIASES["FullName"])
        text       = get_field(r, IG_CMT_ALIASES["CommentText"])
        likes      = get_field(r, IG_CMT_ALIASES["Likes"])
        date       = get_field(r, IG_CMT_ALIASES["Date"])
        # MediaId and CommentId need special handling since both might map to "id"
        media_id   = r.get("mediaId") or r.get("postId") or r.get("id", "")
        comment_id = r.get("id") or r.get("commentId", "")

        date_str = str(date)[:10] if date else ""
        rows.append({
            "Username":    str(username),
            "FullName":    str(fullname),
            "CommentText": str(text),
            "Likes":       str(likes) if likes != "" else "0",
            "Date":        date_str,
            "MediaId":     str(media_id),
            "CommentId":   str(comment_id),
        })
    return rows

def write_normalized_csv(rows, fieldnames, filename):
    path = os.path.join(OUT_DIR, filename)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Normalized {filename} → {len(rows)} rows")
    return path

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    runs = {}  # label → run_id

    # ── Trigger all actors ───────────────────────────────────────────────────
    print("\n=== Triggering Apify actors ===")

    for loc_cfg in cfg["google_maps"]:
        label = f"google_{loc_cfg['location']}"
        print(f"\nTriggering Google Maps — {loc_cfg['location']}")
        runs[label] = trigger_actor(loc_cfg["actorId"], loc_cfg["input"])

    for loc_cfg in cfg["tripadvisor"]:
        label = f"tripadvisor_{loc_cfg['location']}"
        print(f"\nTriggering TripAdvisor — {loc_cfg['location']}")
        runs[label] = trigger_actor(loc_cfg["actorId"], loc_cfg["input"])

    print("\nTriggering Instagram Posts")
    runs["ig_posts"] = trigger_actor(
        cfg["instagram_posts"]["actorId"],
        cfg["instagram_posts"]["input"]
    )

    # For Instagram Comments, build directUrls from the most recent posts in master
    print("\nBuilding Instagram Comments actor input from recent posts...")
    ig_posts_master = os.path.join(DATA_DIR, "ig_posts.csv")
    recent_urls = []
    if os.path.exists(ig_posts_master):
        with open(ig_posts_master, encoding="utf-8-sig") as f:
            posts = list(csv.DictReader(f))
        posts_sorted = sorted(posts, key=lambda r: r.get("Date", ""), reverse=True)
        n = cfg["instagram_comments"]["recentPostsLimit"]
        recent_urls = [p["PostUrl"] for p in posts_sorted[:n] if p.get("PostUrl")]
    if not recent_urls:
        print("  WARNING: No recent post URLs found — skipping comments actor")
        runs["ig_comments"] = None
    else:
        print(f"  Using {len(recent_urls)} recent post URLs")
        ig_cmt_input = {
            "directUrls": recent_urls,
            "resultsLimit": cfg["instagram_comments"]["commentsPerPost"],
            "includeNestedComments": cfg["instagram_comments"]["includeNestedComments"],
        }
        runs["ig_comments"] = trigger_actor(
            cfg["instagram_comments"]["actorId"], ig_cmt_input
        )

    # ── Wait for all runs ────────────────────────────────────────────────────
    print("\n=== Waiting for runs to complete (may take 10-30 min) ===")
    datasets = {}
    for label, run_id in runs.items():
        if run_id is None:
            continue
        print(f"\nWaiting for {label}...")
        try:
            datasets[label] = wait_for_run(run_id)
        except Exception as e:
            print(f"  ERROR: {label} failed — {e}", file=sys.stderr)

    # ── Download and normalize ───────────────────────────────────────────────
    print("\n=== Downloading and normalizing results ===")

    for loc_cfg in cfg["google_maps"]:
        label = f"google_{loc_cfg['location']}"
        if label not in datasets:
            print(f"  SKIP {label} (no dataset)")
            continue
        print(f"\nDownloading {label}...")
        raw = download_dataset_csv(datasets[label])
        save_csv(raw, f"{label}_raw.csv")
        rows = normalize_google(raw, loc_cfg["location"])
        write_normalized_csv(rows,
            ["Source","Location","Stars","Date","ReviewerName","LocalGuide","LikesCount","ReviewText","ReviewId"],
            f"{label}.csv")

    for loc_cfg in cfg["tripadvisor"]:
        label = f"tripadvisor_{loc_cfg['location']}"
        if label not in datasets:
            print(f"  SKIP {label} (no dataset)")
            continue
        print(f"\nDownloading {label}...")
        raw = download_dataset_csv(datasets[label])
        save_csv(raw, f"{label}_raw.csv")
        rows = normalize_tripadvisor(raw, loc_cfg["location"])
        write_normalized_csv(rows,
            ["Source","Location","Stars","Date","ReviewerName","Title","ReviewText","ReviewUrl","ReviewId"],
            f"{label}.csv")

    if "ig_posts" in datasets:
        print("\nDownloading ig_posts...")
        raw = download_dataset_csv(datasets["ig_posts"])
        save_csv(raw, "ig_posts_raw.csv")
        rows = normalize_ig_posts(raw)
        write_normalized_csv(rows,
            ["PostUrl","ShortCode","Date","Type","Likes","Comments","Hashtags","Caption"],
            "ig_posts.csv")

    if "ig_comments" in datasets:
        print("\nDownloading ig_comments...")
        raw = download_dataset_csv(datasets["ig_comments"])
        save_csv(raw, "ig_comments_raw.csv")
        rows = normalize_ig_comments(raw)
        write_normalized_csv(rows,
            ["Username","FullName","CommentText","Likes","Date","MediaId","CommentId"],
            "ig_comments.csv")

    print("\n=== run_apify.py complete ===")
    print(f"Raw + normalized files written to: {OUT_DIR}")

if __name__ == "__main__":
    main()
