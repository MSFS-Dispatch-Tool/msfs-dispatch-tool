"""
Minimal git-driven blog for VirtualDispatch.

Posts are markdown files in content/blog/, named
YYYY-MM-DD-slug.md - the filename IS the date and URL slug, so there's
no database table and no admin UI: publishing a post is "add a file,
commit, push" (matches how this whole project already ships).

File format: an H1 (# Title) as the first line is the post title and
is stripped from the rendered body; the rest is plain markdown. The
first paragraph doubles as the blog-index summary/excerpt.
"""

import os
import re
from datetime import datetime

import markdown as markdown_lib

POSTS_DIR = os.path.join(os.path.dirname(__file__), "content", "blog")

_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)\.md$")


def _strip_html_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()


def _load_post(filename):
    match = _FILENAME_RE.match(filename)
    if not match:
        return None
    year, month, day, slug = match.groups()
    post_date = datetime(int(year), int(month), int(day)).date()

    with open(os.path.join(POSTS_DIR, filename), "r", encoding="utf-8") as f:
        raw = f.read()

    lines = raw.lstrip().splitlines()
    title = slug.replace("-", " ").title()
    body_lines = lines
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        body_lines = lines[1:]
    body_md = "\n".join(body_lines).strip()

    html = markdown_lib.markdown(body_md, extensions=["fenced_code", "tables"])

    first_p_match = re.search(r"<p>(.*?)</p>", html, re.S)
    summary = _strip_html_tags(first_p_match.group(1)) if first_p_match else ""
    if len(summary) > 200:
        summary = summary[:197].rstrip() + "..."

    return {
        "slug": slug,
        "title": title,
        "date": post_date,
        "date_display": post_date.strftime("%B %-d, %Y") if os.name != "nt" else post_date.strftime("%B %d, %Y"),
        "summary": summary,
        "html": html,
    }


def list_posts():
    """Newest first. Missing/unreadable directory just yields no posts
    rather than crashing the blog index."""
    if not os.path.isdir(POSTS_DIR):
        return []
    posts = []
    for filename in os.listdir(POSTS_DIR):
        if not filename.endswith(".md"):
            continue
        post = _load_post(filename)
        if post:
            posts.append(post)
    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts


def get_post(slug):
    for post in list_posts():
        if post["slug"] == slug:
            return post
    return None
