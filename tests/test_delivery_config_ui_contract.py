from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
CSS_PATH = ROOT / "static/css/delivery-config.css"
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""


REQUIRED_IDS = {
    "deliveryConfigPrompt",
    "deliveryConfigPanel",
    "deliveryMethodSelect",
    "defaultDeliveryContent",
    "cardInventorySummary",
    "cardImportInput",
    "cardGeneratorBatch",
    "cardGenerateForm",
    "cardReplenishButton",
    "deliveryConfigStatus",
    "deliveryDefaultSaveButton",
    "deliveryDefaultApplyButton",
    "deliveryConfigSaveButton",
    "deliveryConfigDeleteButton",
    "cardImportButton",
    "cardContinueButton",
    "cardImportFileInput",
    "fixedLinkInput",
    "providerEndpoint",
    "providerToken",
    "providerTimeoutSeconds",
    "providerMaxRetries",
    "providerResponseField",
    "providerHeaders",
    "providerRequestBody",
    "providerFieldMapping",
    "inventoryStockCeiling",
    "inventoryLowStockThreshold",
    "inventoryAutoReplenish",
    "inventoryPreviewList",
}


def test_delivery_config_dom_and_stylesheet_contract_are_present():
    for element_id in REQUIRED_IDS:
        assert re.search(rf'id\s*=\s*["\']{element_id}["\']', INDEX)

    assert "/static/css/delivery-config.css" in INDEX
    assert 'aria-live="polite"' in INDEX
    assert 'aria-controls="deliveryConfigPanel"' in INDEX
    assert 'aria-expanded="true"' in INDEX or 'aria-expanded="false"' in INDEX


def test_panel_can_be_closed_collapsed_and_reopened_with_persistent_state():
    for token in (
        "closeDeliveryConfigPrompt",
        "toggleDeliveryConfigPanel",
        "openDeliveryConfigPanel",
        "打开交付配置",
        "localStorage.getItem",
        "localStorage.setItem",
        "deliveryConfigUiState",
    ):
        assert token in APP or token in INDEX

    assert re.search(r"aria-expanded[^\n]{0,200}(?:setAttribute|toggle)", APP)


def test_four_delivery_methods_are_named_for_nontechnical_users():
    expected_options = {
        "fixed_link": "固定内容/网盘链接",
        "imported_card": "导入卡密",
        "generated_card": "自动生成卡密",
        "provider_api": "第三方接口",
    }
    for value, label in expected_options.items():
        assert re.search(
            rf'<option\s+value=["\']{value}["\'][^>]*>\s*{re.escape(label)}\s*</option>',
            INDEX,
        )

    assert "仅支持以 HTTP:// 或 HTTPS:// 开头的链接" in INDEX


def test_default_content_is_local_and_distinct_from_current_item_config():
    for phrase in (
        "常用默认内容",
        "当前商品专属配置",
        "保存到本机",
        "填入当前商品",
        "实际保存到当前商品",
    ):
        assert phrase in INDEX or phrase in APP
    assert "defaultDeliveryContent" in APP
    assert "deliveryDefaultContent" in APP


def test_existing_delivery_and_inventory_api_paths_are_used():
    expected_paths = (
        "/api/cards/${cardId}/delivery-config?account_id=",
        "/api/cards/${cardId}/inventory/settings?account_id=",
        "/api/cards/${cardId}/inventory?account_id=",
        "/api/cards/${cardId}/inventory/import?account_id=",
        "/api/cards/${cardId}/inventory/generate?account_id=",
        "/api/cards/${cardId}/inventory/preview?account_id=",
    )
    for path in expected_paths:
        assert path in APP

    assert "'Authorization': `Bearer ${authToken}`" in APP
    assert "method: 'DELETE'" in APP


def test_item_delivery_card_is_resolved_before_card_scoped_requests():
    assert "/api/items/${encodeURIComponent(itemId)}/delivery-card?account_id=" in APP
    assert re.search(
        r"async\s+function\s+openDeliveryConfigForItem\s*\([^)]*accountId[^)]*itemId[^)]*itemTitle[^)]*\)"
        r"[\s\S]{0,1800}deliveryConfigFetch\([\s\S]{0,500}method:\s*['\"]POST['\"]"
        r"[\s\S]{0,1000}(?:resolved|result|response)[^.\n]*\.card_id",
        APP,
    )
    resolver = re.search(
        r"async\s+function\s+openDeliveryConfigForItem[\s\S]*?(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert resolver
    assert not re.search(r"cardId\s*=\s*itemId\b", resolver.group(0))
    assert "Promise.all" in resolver.group(0)


def test_item_rows_offer_delivery_configuration_using_real_item_fields():
    assert "设置交付方式" in APP
    assert "openDeliveryConfigForItem" in APP
    assert re.search(
        r"openDeliveryConfigForItem[\s\S]{0,2500}item\.cookie_id[\s\S]{0,500}item\.item_id",
        APP,
    ) or re.search(
        r"openDeliveryConfigForItem\([^)]*item\.cookie_id[^)]*item\.item_id",
        APP,
    )


def test_inventory_summary_and_shortage_copy_are_explicit():
    for phrase in (
        "库存上限",
        "可用",
        "锁定",
        "已发出",
        "低库存预警线",
        "自动补充状态",
        "已分配给待发送订单",
        "库存不足，还需要 X 个",
        "本单会暂停，不会少发一部分",
        "去补充库存",
        "继续处理",
        "补足后系统将重试",
    ):
        assert phrase in INDEX or phrase in APP

    for misleading_phrase in (
        "先发送已有卡密",
        "先发已有库存",
        "库存不足时部分发货",
    ):
        assert misleading_phrase not in INDEX
        assert misleading_phrase not in APP


def test_import_and_generation_follow_backend_contract_without_secret_logging():
    for token in (
        "FileReader",
        ".txt",
        ".csv",
        "secrets",
        "filter(Boolean)",
        "generator_prefix",
        "generator_length",
        "generator_charset",
        "stock_ceiling",
        "low_stock_threshold",
        "auto_replenish",
        "批次仅作为本机备注，不会发送到服务器",
    ):
        assert token in APP or token in INDEX

    assert not re.search(
        r"console\.(?:log|info|debug)\s*\([^)]*(?:secrets|cardImportInput|卡密)",
        APP,
        re.IGNORECASE,
    )


def test_delivery_payloads_match_backend_contract_and_inventory_settings_are_scoped():
    for payload in (
        "{ mode: 'fixed_link', config: { url } }",
        "{ mode: 'imported_card', config: { source: 'local-import' } }",
        "{ mode: 'generated_card', config: { source: 'local-generated' } }",
    ):
        assert payload in APP

    for field in (
        "endpoint",
        "token",
        "timeout_seconds",
        "max_retries",
        "response_field",
        "headers",
        "request_body",
        "field_mapping",
    ):
        assert re.search(rf"\b{field}\b", APP)

    assert "JSON.parse" in APP
    assert "高级设置 JSON 格式错误" in APP
    settings_match = re.search(
        r"function\s+buildInventorySettingsPayload[\s\S]*?(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert settings_match
    settings_source = settings_match.group(0)
    for field in (
        "stock_ceiling",
        "low_stock_threshold",
        "auto_replenish",
        "generator_prefix",
        "generator_length",
        "generator_charset",
    ):
        assert field in settings_source
    assert "batch" not in settings_source
    assert "quantity" not in settings_source


def test_generation_batch_note_is_saved_locally_per_item_and_never_sent():
    assert "deliveryCardBatchNotes" in APP

    batch_handlers = re.search(
        r"function\s+saveDeliveryCardBatchNote[\s\S]*?"
        r"(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert batch_handlers
    batch_source = batch_handlers.group(0)
    assert "cardGeneratorBatch" in batch_source
    assert "localStorage.setItem" in batch_source
    assert "accountId" in APP
    assert "itemId" in APP
    assert "cardId" in APP

    restore = re.search(
        r"function\s+restoreDeliveryCardBatchNote[\s\S]*?"
        r"(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert restore
    assert "cardGeneratorBatch" in restore.group(0)
    assert "localStorage.getItem" in APP
    assert re.search(
        r"cardGeneratorBatch['\"]\)\?\.addEventListener\(['\"]input['\"]"
        r"\s*,\s*saveDeliveryCardBatchNote\)",
        APP,
    )

    for function_name in (
        "buildInventorySettingsPayload",
        "saveInventorySettings",
        "generateCardInventory",
    ):
        function_match = re.search(
            rf"(?:async\s+)?function\s+{function_name}[\s\S]*?"
            r"(?=\n(?:async\s+)?function\s|\Z)",
            APP,
        )
        assert function_match
        assert "batch" not in function_match.group(0).lower()


def test_replenish_button_dispatches_by_delivery_method():
    assert re.search(
        r'<button[^>]*id=["\']cardReplenishButton["\'][^>]*'
        r'onclick=["\']handleCardReplenish\(\)["\']',
        INDEX,
    )
    handler = re.search(
        r"function\s+handleCardReplenish[\s\S]*?"
        r"(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert handler
    source = handler.group(0)
    assert "deliveryMethodSelect" in source
    assert re.search(
        r"mode\s*===\s*['\"]imported_card['\"][\s\S]*?"
        r"cardImportInput[\s\S]*?focus\(\)[\s\S]*?return",
        source,
    )
    assert "粘贴" in source or "导入" in source
    assert re.search(
        r"mode\s*===\s*['\"]generated_card['\"][\s\S]*?generateCardInventory\(",
        source,
    )
    assert source.count("generateCardInventory(") == 1


def test_masked_preview_uses_textcontent_and_controls_have_handlers():
    preview_match = re.search(
        r"function\s+renderInventoryPreview[\s\S]*?(?=\n(?:async\s+)?function\s|\Z)",
        APP,
    )
    assert preview_match
    assert ".textContent" in preview_match.group(0)
    assert ".innerHTML" not in preview_match.group(0)

    for element_id in (
        "deliveryDefaultSaveButton",
        "deliveryDefaultApplyButton",
        "deliveryConfigSaveButton",
        "deliveryConfigDeleteButton",
        "cardImportButton",
        "cardContinueButton",
        "cardImportFileInput",
        "deliveryMethodSelect",
    ):
        assert re.search(
            rf"(?:getElementById\(['\"]{element_id}['\"]\)|#{element_id})[\s\S]{{0,400}}"
            r"(?:addEventListener|onchange|onclick)",
            APP,
        )


def test_preview_is_masked_only_and_plaintext_export_is_not_offered():
    assert "脱敏预览" in INDEX
    assert "避免泄露卡密" in INDEX
    assert "明文导出" not in INDEX
    assert "/inventory/export" not in APP


def test_provider_form_and_advanced_json_validation_are_chinese_readable():
    for field_name in (
        "endpoint",
        "token",
        "timeout_seconds",
        "max_retries",
        "response_field",
        "headers",
        "request_body",
        "field_mapping",
    ):
        assert field_name in INDEX or field_name in APP

    for phrase in (
        "接口地址",
        "访问凭证",
        "超时时间",
        "重试次数",
        "响应内容字段",
        "高级设置 JSON 格式错误",
    ):
        assert phrase in INDEX or phrase in APP


def test_delivery_config_styles_are_accessible_responsive_and_dark_mode_ready():
    assert ":focus-visible" in CSS
    assert "min-height: 44px" in CSS
    assert "@media" in CSS
    assert "grid-template-columns: 1fr" in CSS
    assert "[data-theme=\"dark\"]" in CSS or "[data-theme='dark']" in CSS
    assert "prefers-reduced-motion" in CSS
