"""
build.py — Rebuild data.js and data.json from master CSVs.

Reads:
  data/reviews.csv
  data/ig_posts.csv
  data/ig_comments.csv
  data/insights.csv

Writes:
  data.js     (dashboard JS file)
  data.json   (JSON API endpoint)

Usage:
  python scripts/build.py
"""

import os, sys, json, re
from collections import defaultdict
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SCRIPT_DIR)
DATA_DIR   = os.path.join(REPO_ROOT, "data")
DATA_JS    = os.path.join(REPO_ROOT, "data.js")
DATA_JSON  = os.path.join(REPO_ROOT, "data.json")

# ── Load deps (stdlib only) ───────────────────────────────────────────────────
import csv

def read_csv(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found", file=sys.stderr)
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def fix(t):
    if not isinstance(t, str):
        return ""
    try:
        return t.encode("latin-1").decode("utf-8")
    except Exception:
        return t

def to_int(v, default=0):
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return default

def to_float(v, default=0.0):
    try:
        return float(str(v))
    except (ValueError, TypeError):
        return default

# ── Load data ─────────────────────────────────────────────────────────────────
print("Loading CSVs...")
raw_reviews  = read_csv("reviews.csv")
raw_posts    = read_csv("ig_posts.csv")
raw_cmts     = read_csv("ig_comments.csv")
raw_insights = read_csv("insights.csv")

# Fix encoding
for r in raw_reviews:
    for f in ["ReviewerName","Title","Themes","MenuMentions","ReviewText","Location"]:
        if f in r:
            r[f] = fix(r[f])

for p in raw_posts:
    for f in ["Caption","Hashtags"]:
        if f in p:
            p[f] = fix(p[f])

for c in raw_cmts:
    for f in ["Username","FullName","CommentText"]:
        if f in c:
            c[f] = fix(c[f])

# Numeric fields
for r in raw_reviews:
    r["_stars"] = to_int(r.get("Stars", 0))
    r["_year"]  = str(r.get("Year",  "")).strip()
    r["_month"] = str(r.get("Month", "")).strip().zfill(2) if r.get("Month") else ""

for p in raw_posts:
    p["_likes"]    = to_int(p.get("Likes", 0))
    p["_comments"] = to_int(p.get("Comments", 0))
    p["_year"]     = str(p.get("Date", ""))[:4]
    p["_month"]    = str(p.get("Date", ""))[5:7]
    p["Type"]      = str(p.get("Type", "")).replace("GraphSidecar", "Sidecar")

for c in raw_cmts:
    c["_likes"] = to_int(c.get("Likes", 0))
    c["_year"]  = str(c.get("Date", ""))[:4]
    c["_month"] = str(c.get("Date", ""))[5:7]

print(f"  Reviews: {len(raw_reviews)}, Posts: {len(raw_posts)}, Comments: {len(raw_cmts)}, Insights: {len(raw_insights)}")

# ── Helper: safe mean ─────────────────────────────────────────────────────────
def safe_mean(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 4) if v else 0.0

def safe_pct(num, den):
    return round(num / den, 4) if den else 0.0

# ── 1. ratingDistribution ─────────────────────────────────────────────────────
rd_counts = defaultdict(lambda: defaultdict(int))
for r in raw_reviews:
    loc   = r.get("Location", "")
    stars = str(r["_stars"])
    if loc and stars:
        rd_counts[loc][stars] += 1

rating_distribution = []
for loc in sorted(rd_counts):
    for stars in ["1","2","3","4","5"]:
        rating_distribution.append({
            "Location": loc,
            "Stars":    stars,
            "Count":    str(rd_counts[loc].get(stars, 0)),
        })

# ── 2. sentimentSummary ───────────────────────────────────────────────────────
snt_counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
for r in raw_reviews:
    loc = r.get("Location", "")
    src = r.get("Source", "")
    snt = r.get("Sentiment", "")
    if loc and src and snt:
        snt_counts[loc][src][snt] += 1

sentiment_summary = []
for loc in sorted(snt_counts):
    for src in sorted(snt_counts[loc]):
        for snt in ["Positive","Neutral","Negative"]:
            cnt = snt_counts[loc][src].get(snt, 0)
            if cnt > 0:
                sentiment_summary.append({
                    "Location":  loc,
                    "Source":    src,
                    "Sentiment": snt,
                    "Count":     str(cnt),
                })

# ── 3. themeFrequency ────────────────────────────────────────────────────────
theme_loc_stars  = defaultdict(lambda: defaultdict(list))
theme_loc_counts = defaultdict(lambda: defaultdict(int))

for r in raw_reviews:
    loc    = r.get("Location", "")
    themes = str(r.get("Themes", "")).strip()
    if not themes or themes == "nan":
        continue
    stars = r["_stars"]
    for th in [t.strip() for t in re.split(r"[|;,]", themes) if t.strip()]:
        theme_loc_counts[th][loc] += 1
        theme_loc_stars[th][loc].append(stars)

all_themes = sorted(theme_loc_counts, key=lambda t: sum(theme_loc_counts[t].values()), reverse=True)

theme_frequency = []
for th in all_themes:
    for loc in sorted(theme_loc_counts[th]):
        cnt  = theme_loc_counts[th][loc]
        avg  = safe_mean(theme_loc_stars[th][loc])
        theme_frequency.append({
            "Location": loc,
            "Theme":    th,
            "Count":    str(cnt),
            "AvgStars": str(avg),
        })

# ── 4. yearTrend ──────────────────────────────────────────────────────────────
yr_loc_stars  = defaultdict(lambda: defaultdict(list))
yr_loc_counts = defaultdict(lambda: defaultdict(int))

for r in raw_reviews:
    yr  = r["_year"]
    loc = r.get("Location", "")
    if yr and loc:
        yr_loc_counts[yr][loc] += 1
        yr_loc_stars[yr][loc].append(r["_stars"])

year_trend = []
for yr in sorted(yr_loc_counts):
    for loc in sorted(yr_loc_counts[yr]):
        cnt = yr_loc_counts[yr][loc]
        avg = safe_mean(yr_loc_stars[yr][loc])
        year_trend.append({
            "Year":        yr,
            "Location":    loc,
            "ReviewCount": str(cnt),
            "AvgStars":    str(avg),
        })

# ── 5. menuMentions ───────────────────────────────────────────────────────────
menu_stars  = defaultdict(list)
menu_counts = defaultdict(int)

for r in raw_reviews:
    items = str(r.get("MenuMentions", "")).strip()
    if not items or items == "nan":
        continue
    for item in [i.strip() for i in re.split(r"[|;,]", items) if i.strip()]:
        menu_counts[item] += 1
        menu_stars[item].append(r["_stars"])

menu_mentions = []
for item in sorted(menu_counts, key=lambda x: menu_counts[x], reverse=True):
    menu_mentions.append({
        "MenuItem":      item,
        "TotalMentions": str(menu_counts[item]),
        "AvgStars":      str(safe_mean(menu_stars[item])),
    })

# ── 6. themeStrength ─────────────────────────────────────────────────────────
ts_data = defaultdict(lambda: {"pos":0,"neg":0,"neu":0,"stars":[]})

for r in raw_reviews:
    themes = str(r.get("Themes", "")).strip()
    if not themes or themes == "nan":
        continue
    snt = r.get("Sentiment", "")
    for th in [t.strip() for t in re.split(r"[|;,]", themes) if t.strip()]:
        if snt == "Positive":
            ts_data[th]["pos"] += 1
        elif snt == "Negative":
            ts_data[th]["neg"] += 1
        else:
            ts_data[th]["neu"] += 1
        ts_data[th]["stars"].append(r["_stars"])

theme_strength = []
for th in sorted(ts_data, key=lambda t: sum([ts_data[t]["pos"],ts_data[t]["neg"],ts_data[t]["neu"]]), reverse=True):
    d     = ts_data[th]
    total = d["pos"] + d["neg"] + d["neu"]
    avg   = safe_mean(d["stars"])
    pct   = safe_pct(d["pos"], total)
    # Rating: 1-5 based on avg stars
    rating = round(avg)
    theme_strength.append({
        "Theme":       th,
        "Total":       str(total),
        "Positive":    str(d["pos"]),
        "Negative":    str(d["neg"]),
        "AvgStars":    str(avg),
        "PositivePct": str(pct),
        "Rating":      str(rating),
    })

# ── 7. crossSource ───────────────────────────────────────────────────────────
cs_data = defaultdict(lambda: {"count":0,"stars":[],"pos":0})

for r in raw_reviews:
    loc = r.get("Location", "")
    src = r.get("Source", "")
    if not loc or not src:
        continue
    k = f"{loc}|{src}"
    cs_data[k]["count"] += 1
    cs_data[k]["stars"].append(r["_stars"])
    if r.get("Sentiment") == "Positive":
        cs_data[k]["pos"] += 1

cross_source = []
for k in sorted(cs_data):
    loc, src = k.split("|", 1)
    d   = cs_data[k]
    cnt = d["count"]
    avg = safe_mean(d["stars"])
    pct = safe_pct(d["pos"], cnt)
    cross_source.append({
        "Location":    loc,
        "Source":      src,
        "ReviewCount": str(cnt),
        "AvgStars":    str(avg),
        "PositivePct": str(pct),
    })

# ── 8. instagramEngagement ───────────────────────────────────────────────────
eng_data = defaultdict(lambda: {"count":0,"likes":[],"comments":[]})

for p in raw_posts:
    k = f"{p['_year']}|{p['Type']}"
    eng_data[k]["count"] += 1
    eng_data[k]["likes"].append(p["_likes"])
    eng_data[k]["comments"].append(p["_comments"])

instagram_engagement = []
for k in sorted(eng_data):
    yr, tp = k.split("|", 1)
    d = eng_data[k]
    instagram_engagement.append({
        "Year":        yr,
        "Type":        tp,
        "PostCount":   str(d["count"]),
        "AvgLikes":    str(round(safe_mean(d["likes"]), 1)),
        "AvgComments": str(round(safe_mean(d["comments"]), 1)),
    })

# ── 9. insights ──────────────────────────────────────────────────────────────
insights = []
for r in raw_insights:
    insights.append({
        "Priority": r.get("Priority", ""),
        "Category": r.get("Category", ""),
        "Location": r.get("Location", ""),
        "Insight":  r.get("Insight", ""),
        "Action":   r.get("Action", ""),
    })

# ── 10. topPosts (top 100 overall by likes) ───────────────────────────────────
posts_sorted = sorted(raw_posts, key=lambda p: p["_likes"], reverse=True)
top_posts = [
    {"PostUrl": p["PostUrl"], "Date": p["Date"], "Type": p["Type"],
     "Likes": p["_likes"], "Comments": p["_comments"]}
    for p in posts_sorted[:100]
]

# ── 11. topComments (top 100 overall by likes) ───────────────────────────────
cmts_sorted = sorted(raw_cmts, key=lambda c: c["_likes"], reverse=True)
top_comments = [
    {"Username": c["Username"], "FullName": c["FullName"],
     "CommentText": c["CommentText"], "Likes": c["_likes"], "Date": c["Date"]}
    for c in cmts_sorted[:100]
]

# ── 12. rawReviews (compact) ──────────────────────────────────────────────────
raw_reviews_arr = [
    {
        "Src": r.get("Source", ""),
        "Loc": r.get("Location", ""),
        "St":  r["_stars"],
        "Snt": r.get("Sentiment", ""),
        "Yr":  r["_year"],
        "Mo":  r["_month"],
        "Th":  r.get("Themes", ""),
        "Mn":  r.get("MenuMentions", ""),
    }
    for r in raw_reviews
]

# ── 13. fullReviews (with ReviewText) ────────────────────────────────────────
full_reviews_arr = [
    {
        "Src": r.get("Source", ""),
        "Loc": r.get("Location", ""),
        "St":  r["_stars"],
        "Snt": r.get("Sentiment", ""),
        "Yr":  r["_year"],
        "Mo":  r["_month"],
        "Th":  r.get("Themes", ""),
        "Tx":  r.get("ReviewText", ""),
    }
    for r in raw_reviews
]

# ── 14. topPostsAll (top 20/year + top 30 overall, deduped) ─────────────────
top_per_yr  = []
yr_counts   = defaultdict(int)
seen_urls   = set()
posts_by_yr = defaultdict(list)
for p in raw_posts:
    posts_by_yr[p["_year"]].append(p)

for yr in sorted(posts_by_yr):
    yr_posts = sorted(posts_by_yr[yr], key=lambda p: p["_likes"], reverse=True)
    for p in yr_posts[:20]:
        top_per_yr.append(p)

top_overall = posts_sorted[:30]
combined_posts = top_per_yr + top_overall

top_posts_all = []
for p in combined_posts:
    url = p["PostUrl"]
    if url in seen_urls:
        continue
    seen_urls.add(url)
    top_posts_all.append({
        "PostUrl": url, "Date": p["Date"], "Type": p["Type"],
        "Likes": p["_likes"], "Comments": p["_comments"],
    })
top_posts_all.sort(key=lambda p: p["Likes"], reverse=True)

# ── 15. topCommentsAll (top 20/year) ─────────────────────────────────────────
cmts_by_yr = defaultdict(list)
for c in raw_cmts:
    cmts_by_yr[c["_year"]].append(c)

top_comments_all = []
for yr in sorted(cmts_by_yr):
    yr_cmts = sorted(cmts_by_yr[yr], key=lambda c: c["_likes"], reverse=True)
    for c in yr_cmts[:20]:
        top_comments_all.append({
            "Username": c["Username"], "FullName": c["FullName"],
            "CommentText": c["CommentText"], "Likes": c["_likes"], "Date": c["Date"],
        })

# ── 16. instagramMonthly ─────────────────────────────────────────────────────
monthly_data = defaultdict(lambda: {"count":0,"likes":[],"comments":[],"Image":0,"Video":0,"Sidecar":0})
for p in raw_posts:
    k = f"{p['_year']}|{p['_month']}"
    monthly_data[k]["count"] += 1
    monthly_data[k]["likes"].append(p["_likes"])
    monthly_data[k]["comments"].append(p["_comments"])
    tp = p["Type"]
    if tp in ("Image","Video","Sidecar"):
        monthly_data[k][tp] += 1

instagram_monthly = []
for k in sorted(monthly_data):
    yr, mo = k.split("|", 1)
    d = monthly_data[k]
    instagram_monthly.append({
        "Year":         yr,
        "Month":        mo,
        "PostCount":    d["count"],
        "TotalLikes":   sum(d["likes"]),
        "AvgLikes":     round(safe_mean(d["likes"]), 1),
        "TotalComments": sum(d["comments"]),
        "AvgComments":  round(safe_mean(d["comments"]), 1),
        "Image":        d["Image"],
        "Video":        d["Video"],
        "Sidecar":      d["Sidecar"],
    })

# ── 17. instagramCommentsMonthly ─────────────────────────────────────────────
cmt_monthly_data = defaultdict(lambda: {"count":0,"likes":[]})
for c in raw_cmts:
    k = f"{c['_year']}|{c['_month']}"
    cmt_monthly_data[k]["count"] += 1
    cmt_monthly_data[k]["likes"].append(c["_likes"])

instagram_comments_monthly = []
for k in sorted(cmt_monthly_data):
    yr, mo = k.split("|", 1)
    d = cmt_monthly_data[k]
    instagram_comments_monthly.append({
        "Year":         yr,
        "Month":        mo,
        "CommentCount": d["count"],
        "TotalLikes":   sum(d["likes"]),
        "AvgLikes":     round(safe_mean(d["likes"]), 2),
    })

# ── 18. meta ─────────────────────────────────────────────────────────────────
meta = {
    "totalReviews":          len(raw_reviews),
    "totalInstagramPosts":   len(raw_posts),
    "totalInstagramComments": len(raw_cmts),
    "generatedAt":           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}

# ── Assemble DATA object ──────────────────────────────────────────────────────
DATA = {
    "ratingDistribution":       rating_distribution,
    "sentimentSummary":         sentiment_summary,
    "themeFrequency":           theme_frequency,
    "yearTrend":                year_trend,
    "menuMentions":             menu_mentions,
    "themeStrength":            theme_strength,
    "crossSource":              cross_source,
    "instagramEngagement":      instagram_engagement,
    "insights":                 insights,
    "topPosts":                 top_posts,
    "topComments":              top_comments,
    "rawReviews":               raw_reviews_arr,
    "fullReviews":              full_reviews_arr,
    "topPostsAll":              top_posts_all,
    "topCommentsAll":           top_comments_all,
    "instagramMonthly":         instagram_monthly,
    "instagramCommentsMonthly": instagram_comments_monthly,
    "meta":                     meta,
}

# ── Write data.js ─────────────────────────────────────────────────────────────
print("Writing data.js...")
js_parts = ["// Panel de Inteligencia Pots & Bowls\nconst DATA = {"]

field_order = [
    "ratingDistribution","sentimentSummary","themeFrequency","yearTrend",
    "menuMentions","themeStrength","crossSource","instagramEngagement",
    "insights","topPosts","topComments","rawReviews","fullReviews",
]

for key in field_order:
    val_json = json.dumps(DATA[key], ensure_ascii=False, separators=(",",":"))
    js_parts.append(f"  {key}: {val_json},")

# Close main object with meta
meta_json = json.dumps(DATA["meta"], ensure_ascii=False, separators=(",",":"))
js_parts.append(f"  meta: {meta_json}")
js_parts.append("};")

# Append array sections as DATA.xxx = [...];
for key in ["topPostsAll","topCommentsAll","instagramMonthly","instagramCommentsMonthly"]:
    val_json = json.dumps(DATA[key], ensure_ascii=False, separators=(",",":"))
    js_parts.append(f"\nDATA.{key} = {val_json};")

js_content = "\n".join(js_parts) + "\n"

with open(DATA_JS, "w", encoding="utf-8", newline="\n") as f:
    f.write(js_content)

size_kb = os.path.getsize(DATA_JS) // 1024
print(f"  data.js written: {size_kb} KB")

# ── Write data.json ───────────────────────────────────────────────────────────
print("Writing data.json...")
with open(DATA_JSON, "w", encoding="utf-8", newline="\n") as f:
    json.dump(DATA, f, ensure_ascii=False, separators=(",",":"))

size_kb = os.path.getsize(DATA_JSON) // 1024
print(f"  data.json written: {size_kb} KB")

print("\n=== build.py complete ===")
print(f"  Reviews:     {len(raw_reviews)}")
print(f"  IG Posts:    {len(raw_posts)}")
print(f"  IG Comments: {len(raw_cmts)}")
print(f"  Generated:   {meta['generatedAt']}")
