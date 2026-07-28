import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = "https://github.com/caygss/xianyu-auto-reply-fix-open"
UPSTREAM_OWNER = "GuDong2003"
UPSTREAM_REPOSITORY_URL = "https://github.com/GuDong2003/xianyu-auto-reply-fix"


def test_dashboard_contains_no_upstream_payment_qr_or_sponsor_ui():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    for forbidden in (
        "about-donate",
        "支付宝收款码",
        "微信收款码",
        "sponsor-pill",
        "bi-cup-hot",
        "data:image",
    ):
        assert forbidden not in html

    assert not (REPO_ROOT / "static" / "css" / "about.css").exists()
    assert "喝杯咖啡" not in app_js
    assert "赞助我继续开发" not in app_js


def test_runtime_update_defaults_point_to_public_repository():
    runtime_files = (
        REPO_ROOT / "auto_updater.py",
        REPO_ROOT / "reply_server.py",
        REPO_ROOT / "generate_update_manifest.py",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)

    assert "caygss" in combined
    assert "xianyu-auto-reply-fix-open" in combined
    assert "GuDong2003" not in combined
    assert "xianyu-auto-reply-fix'" not in combined
    assert 'xianyu-auto-reply-fix"' not in combined


def test_bundled_announcement_links_to_public_repository():
    announcement = json.loads((REPO_ROOT / "announcement.json").read_text(encoding="utf-8"))
    action_urls = [
        item.get("action_url")
        for item in announcement.get("announcements", [])
        if isinstance(item, dict)
    ]

    assert PUBLIC_REPOSITORY in action_urls
    assert all(UPSTREAM_OWNER not in str(url) for url in action_urls)
    assert UPSTREAM_REPOSITORY_URL not in action_urls
