# Facebook Share Tracker

Flask starter for tracking public share posts returned by Meta Graph API. Includes keyword matching, deduplication for API posts, campaign statistics, leaderboard, manual review entries, CSV export, login, and CSRF protection.

## Important limitations
- A Page admin role does not automatically grant access to enumerate all public shares or identify every sharer.
- The app calls Graph API `/{POST_ID}/sharedposts`; exact availability and fields depend on Meta app permissions, feature access, API version and the Page/post.
- Posts with missing captions or identity are marked for review. Manually entered shares are not Meta-verified.
- No scraping, cookies, or privacy bypass is used.
- This starter uses SQLite and synchronous scanning. For a sustained 2,000+ campaign, deploy with a persistent disk and one app instance; before multi-instance or scheduled concurrent scans, migrate to PostgreSQL and a background queue.

## Deploy on Render
1. Create a Render Web Service from this repository.
2. Runtime: Docker (Dockerfile is included).
3. Add a persistent disk mounted at `/var/data`.
4. Set environment variables: `APP_ENV=production`, `DATABASE_PATH=/var/data/tracker.db`, `FLASK_SECRET_KEY` (random long value), `ADMIN_PASSWORD` (strong unique password), `COOKIE_SECURE=1`, `META_PAGE_ACCESS_TOKEN` (only after Meta access is approved), `META_GRAPH_VERSION`.
5. Deploy and open the service URL. Visit `/health` to check app status.
6. Never commit real tokens or passwords.

## Meta setup
Create a Meta developer app and use Graph API Explorer to test the required endpoint and fields for a Page post you administer. Common Page permissions can include `pages_show_list`, `pages_read_engagement`, and `pages_read_user_content` depending on the use case. Page Public Content Access or app review/feature approval may be required for public content access. Confirm current requirements in Meta's dashboard and docs:
- https://developers.facebook.com/docs/graph-api/reference/page-post/sharedposts/
- https://developers.facebook.com/docs/graph-api/overview/access-levels/

The campaign form expects the Graph API object ID (often `PAGE_ID_POST_ID`), not just a permalink. Test API access before relying on automatic scans.

## Local run
```bash
python -m venv .venv
pip install -r requirements.txt
cp .env.example .env
python app.py
```
