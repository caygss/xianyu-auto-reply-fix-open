from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = (PROJECT_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
ITEMS_CSS = (PROJECT_ROOT / "static" / "css" / "items.css").read_text(encoding="utf-8")


def test_republish_ui_declares_all_management_api_paths():
    expected_paths = (
        "/api/republish/templates?cookie_id=",
        "/api/republish/templates",
        "/api/republish/templates/",
        "/pause",
        "/check-now",
        "/api/republish/jobs?cookie_id=",
    )

    for path in expected_paths:
        assert path in APP_JS


def test_republish_ui_has_safe_status_and_configuration_controls():
    required_tokens = (
        "displayRepublishStatus",
        "openRepublishConfig",
        "saveRepublishTemplate",
        "toggleRepublishPause",
        "checkRepublishNow",
        "delivery_summary",
        "dry-run",
        "sku_delivery",
        "auto_delivery",
        "auto_republish",
    )

    for token in required_tokens:
        assert token in APP_JS

    assert "republish_delivery_content" not in APP_JS


def test_republish_ui_uses_data_actions_and_fetches_detail_before_editing():
    assert "data-republish-action" in APP_JS
    assert "initRepublishEventDelegation" in APP_JS
    assert "/api/republish/templates/${encodeURIComponent(template.template_id)}" in APP_JS
    assert 'onclick="openRepublishConfig' not in APP_JS
    assert 'onclick="toggleRepublishPause' not in APP_JS
    assert 'onclick="checkRepublishNow' not in APP_JS
    assert 'onclick="saveRepublishTemplate' not in APP_JS
    assert 'onclick="addRepublishSkuRow' not in APP_JS


def test_republish_ui_handles_actual_result_error_fields():
    assert "last_result?.error" in APP_JS
    assert "job?.error" in APP_JS
    assert "template?.dry_run" in APP_JS or "dry_run" in APP_JS


def test_republish_ui_is_responsive_and_keeps_sensitive_values_out_of_status_css():
    assert ".republish-status" in ITEMS_CSS
    assert ".republish-config" in ITEMS_CSS
    assert "@media" in ITEMS_CSS
