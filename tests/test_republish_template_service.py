import math

import pytest

from republish_models import RepublishTemplate
from republish_store import RepublishStore
from republish_template_service import (
    build_template_from_item_info,
    normalize_delivery_content,
    resolve_delivery_content,
    rotate_current_item_id,
    safe_delivery_summary,
)


DEFAULT_LINK = "  https://pan.example.test/s/alpha?pwd=AbCd1234  "
SPECIAL_LINK = "https://cloud.example.test/folder/special?code=Z9x8"


@pytest.fixture
def store(tmp_path):
    return RepublishStore(tmp_path / "republish.sqlite3")


def item_info(*, detail=None, title="商品标题", description="商品说明"):
    return {
        "item_title": title,
        "item_description": description,
        "item_detail": detail
        if isinstance(detail, str)
        else None,
        "item_detail_parsed": detail if isinstance(detail, dict) else None,
    }


def publishable_detail():
    return {
        "images": [{"url": "https://img.example.test/item-1.jpg"}],
        "price": 12.5,
        "originalPrice": 19.9,
        "deliveryChoice": "快递",
        "postPrice": 0,
        "canSelfPickup": True,
        "categoryHint": "数字商品",
    }


def test_normalize_delivery_content_trims_and_preserves_text():
    value = "  https://pan.example.test/s/x?pwd=Code42  提取码 Code42  "

    assert normalize_delivery_content(value) == value.strip()


@pytest.mark.parametrize("value", [None, "", "   ", 123, [], {}])
def test_normalize_delivery_content_rejects_empty_or_non_string(value):
    with pytest.raises(ValueError, match="delivery_content"):
        normalize_delivery_content(value)


def test_safe_delivery_summary_does_not_return_link_or_secret_text():
    normalized = normalize_delivery_content(DEFAULT_LINK)

    summary = safe_delivery_summary(normalized)

    assert summary != normalized
    assert "pwd=" not in summary
    assert "AbCd1234" not in summary
    assert "https://pan.example.test" not in summary


def test_safe_delivery_summary_is_stable_and_distinct_for_low_entropy_links():
    first_link = "https://pan.example.test/a?pwd=1"
    second_link = "https://pan.example.test/a?pwd=2"

    first_summary = safe_delivery_summary(first_link)
    second_summary = safe_delivery_summary(second_link)

    assert first_summary == safe_delivery_summary(first_link)
    assert first_summary != second_summary
    for summary, original in ((first_summary, first_link), (second_summary, second_link)):
        assert original not in summary
        assert "?pwd=" not in summary
        assert "pwd=" not in summary
        assert summary not in {first_link, second_link}


def test_resolve_delivery_content_uses_default_and_isolated_templates():
    first = RepublishTemplate(
        template_id="template-1",
        cookie_id="cookie-1",
        current_item_id="item-1",
        delivery_content=DEFAULT_LINK,
    )
    second = RepublishTemplate(
        template_id="template-2",
        cookie_id="cookie-1",
        current_item_id="item-2",
        delivery_content=SPECIAL_LINK,
    )

    assert resolve_delivery_content(first, {}) == DEFAULT_LINK.strip()
    assert resolve_delivery_content(second, {}) == SPECIAL_LINK


def test_resolve_delivery_content_prefers_sku_override_and_falls_back_to_default():
    template = RepublishTemplate(
        template_id="template-1",
        cookie_id="cookie-1",
        current_item_id="item-1",
        delivery_content=DEFAULT_LINK,
        sku_delivery={"sku-42": SPECIAL_LINK},
    )

    assert resolve_delivery_content(template, {"sku_id": "sku-42"}) == SPECIAL_LINK
    assert resolve_delivery_content(template, {"skuId": "unknown"}) == DEFAULT_LINK.strip()


@pytest.mark.parametrize(
    "order",
    [
        {"skuId": "sku-1"},
        {"sku": "sku-1"},
        {"spec_id": "sku-1"},
        {"specId": "sku-1"},
        {"sku_name": "sku-1"},
        {"spec_name": "sku-1"},
    ],
)
def test_resolve_delivery_content_supports_sku_field_variants(order):
    template = RepublishTemplate(
        template_id="template-1",
        cookie_id="cookie-1",
        current_item_id="item-1",
        delivery_content=DEFAULT_LINK,
        sku_delivery={"sku-1": SPECIAL_LINK},
    )

    assert resolve_delivery_content(template, order) == SPECIAL_LINK


def test_resolve_delivery_content_validates_every_sku_key_and_value():
    template = RepublishTemplate(
        template_id="template-1",
        cookie_id="cookie-1",
        current_item_id="item-1",
        delivery_content=DEFAULT_LINK,
        sku_delivery={"sku-1": "   "},
    )

    with pytest.raises(ValueError, match="sku_delivery"):
        resolve_delivery_content(template, {"sku_id": "sku-1"})


def test_resolve_delivery_content_requires_a_default_when_no_override_exists():
    template = RepublishTemplate(
        template_id="template-1",
        cookie_id="cookie-1",
        current_item_id="item-1",
        delivery_content=None,
    )

    with pytest.raises(ValueError, match="delivery content"):
        resolve_delivery_content(template, {})


def test_build_template_reads_detail_dict_and_generates_stable_id():
    first = build_template_from_item_info(
        "cookie-1",
        "item-1",
        item_info(detail=publishable_detail()),
        DEFAULT_LINK,
        sku_delivery={"sku-1": SPECIAL_LINK},
        auto_delivery=True,
        auto_republish=True,
    )
    second = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )

    assert isinstance(first, RepublishTemplate)
    assert first.template_id == second.template_id
    assert first.template_id.replace("-", "").isalnum()
    assert first.title == "商品标题"
    assert first.description == "商品说明"
    assert first.images == ["https://img.example.test/item-1.jpg"]
    assert first.current_price == 12.5
    assert first.original_price == 19.9
    assert first.delivery_choice == "快递"
    assert first.post_price == 0
    assert first.can_self_pickup is True
    assert first.category_hint == "数字商品"
    assert first.sku_delivery == {"sku-1": SPECIAL_LINK}


def test_build_template_reads_json_detail_string():
    detail = '{"pictures":[{"image_url":"https://img.example.test/item-2.png"}],"price":8}'

    template = build_template_from_item_info(
        "cookie-1", "item-2", item_info(detail=detail), DEFAULT_LINK
    )

    assert template.images == ["https://img.example.test/item-2.png"]
    assert template.current_price == 8


def test_template_id_normalizes_equivalent_scope_whitespace():
    first = build_template_from_item_info(
        "  cookie-1  ", "  item-1  ", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    second = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )

    assert first.template_id == second.template_id
    assert first.cookie_id == second.cookie_id == "cookie-1"
    assert first.current_item_id == second.current_item_id == "item-1"


def test_template_id_encoding_does_not_collide_when_component_boundaries_move():
    first = build_template_from_item_info(
        "ab", "c", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    second = build_template_from_item_info(
        "a", "bc", item_info(detail=publishable_detail()), DEFAULT_LINK
    )

    assert first.template_id != second.template_id


@pytest.mark.parametrize(
    "cookie_id,item_id",
    [
        ("cookie\x00id", "item"),
        ("cookie", "item\x00id"),
        ("cookie\x01id", "item"),
        ("cookie", "item\x7fid"),
    ],
)
def test_template_id_rejects_control_characters_in_scope_ids(cookie_id, item_id):
    with pytest.raises(ValueError, match="cookie_id|current_item_id"):
        build_template_from_item_info(
            cookie_id, item_id, item_info(detail=publishable_detail()), DEFAULT_LINK
        )


def test_explicit_template_id_uses_the_same_normalization_and_validation():
    template = build_template_from_item_info(
        "cookie-1",
        "item-explicit",
        item_info(detail=publishable_detail()),
        DEFAULT_LINK,
        template_id="  explicit-template  ",
    )

    assert template.template_id == "explicit-template"
    for invalid_template_id in ("", "   ", "explicit\x00template", "explicit\x01template"):
        with pytest.raises(ValueError, match="template_id"):
            build_template_from_item_info(
                "cookie-1",
                "item-explicit",
                item_info(detail=publishable_detail()),
                DEFAULT_LINK,
                template_id=invalid_template_id,
            )


@pytest.mark.parametrize(
    "image_field,image_value,expected_url",
    [
        (
            "item_images",
            [{"src": "https://img.example.test/item-images.jpg"}],
            "https://img.example.test/item-images.jpg",
        ),
        (
            "picUrls",
            [{"url": "https://img.example.test/pic-urls.jpg"}],
            "https://img.example.test/pic-urls.jpg",
        ),
        (
            "imageList",
            [{"image_url": "https://img.example.test/image-list.jpg"}],
            "https://img.example.test/image-list.jpg",
        ),
        (
            "images",
            [{"imageUrl": "https://cdn.example.test/image-token"}],
            "https://cdn.example.test/image-token",
        ),
    ],
)
def test_build_template_extracts_additional_image_field_variants(
    image_field, image_value, expected_url
):
    detail = publishable_detail()
    detail.pop("images")
    detail[image_field] = image_value

    template = build_template_from_item_info(
        "cookie-1", "item-images", item_info(detail=detail), DEFAULT_LINK
    )

    assert template.images == [expected_url]


def test_build_template_preserves_cdn_url_without_file_extension():
    detail = publishable_detail()
    detail["images"] = [{"url": "https://cdn.example.test/image-token-123"}]

    template = build_template_from_item_info(
        "cookie-1", "item-cdn", item_info(detail=detail), DEFAULT_LINK
    )

    assert template.images == ["https://cdn.example.test/image-token-123"]


def test_build_template_accepts_image_data_url():
    detail = publishable_detail()
    detail["images"] = [{"src": "data:image/png;base64,AAAA"}]

    template = build_template_from_item_info(
        "cookie-1", "item-data", item_info(detail=detail), DEFAULT_LINK
    )

    assert template.images == ["data:image/png;base64,AAAA"]


@pytest.mark.parametrize(
    "invalid_images",
    [
        ["plain text"],
        ["javascript:alert(1)"],
        ["file:///tmp/image.jpg"],
        [""],
        [None],
        [{"alt": "missing image key"}],
        [{"url": "ftp://cdn.example.test/image"}],
        [{"url": "http:///missing-host"}],
        [{"url": "https://"}],
        [{"url": "data:text/plain;base64,AAAA"}],
    ],
)
def test_build_template_rejects_invalid_image_references(invalid_images):
    detail = publishable_detail()
    detail["images"] = invalid_images

    with pytest.raises(ValueError, match="image"):
        build_template_from_item_info(
            "cookie-1", "item-invalid-image", item_info(detail=detail), DEFAULT_LINK
        )


@pytest.mark.parametrize(
    "order",
    [
        {"sku": {"skuId": "nested-sku"}},
        {"item": {"sku": {"sku_id": "nested-sku"}}},
        {"item": {"spec": {"specId": "nested-sku"}}},
    ],
)
def test_resolve_delivery_content_supports_nested_order_sku_variants(order):
    template = RepublishTemplate(
        template_id="template-nested",
        cookie_id="cookie-1",
        current_item_id="item-nested",
        delivery_content=DEFAULT_LINK,
        sku_delivery={"nested-sku": SPECIAL_LINK},
    )

    assert resolve_delivery_content(template, order) == SPECIAL_LINK


def test_resolve_delivery_content_nested_unknown_sku_uses_default():
    template = RepublishTemplate(
        template_id="template-nested-default",
        cookie_id="cookie-1",
        current_item_id="item-nested-default",
        delivery_content=DEFAULT_LINK,
        sku_delivery={"nested-sku": SPECIAL_LINK},
    )

    assert (
        resolve_delivery_content(template, {"item": {"sku": {"skuId": "not-configured"}}})
        == DEFAULT_LINK.strip()
    )


def test_safe_delivery_summary_hides_path_query_password_and_instruction_text():
    raw_delivery = (
        "https://pan.example.test/complete/private/folder/path?pwd=SecretCode42"
        "&password=AnotherSecret 提取码 SecretCode42"
    )

    summary = safe_delivery_summary(raw_delivery)

    assert summary != raw_delivery
    for secret_fragment in (
        "complete/private/folder/path",
        "pwd=SecretCode42",
        "password=AnotherSecret",
        "提取码",
        "SecretCode42",
        "AnotherSecret",
    ):
        assert secret_fragment not in summary


@pytest.mark.parametrize(
    "info,delivery,expected",
    [
        (item_info(detail=publishable_detail(), title=""), DEFAULT_LINK, "title"),
        (item_info(detail=publishable_detail(), description=""), DEFAULT_LINK, "description"),
        ({"item_title": "标题", "item_detail_parsed": publishable_detail()}, DEFAULT_LINK, "description"),
        (item_info(detail={"price": 1}), DEFAULT_LINK, "image"),
        (item_info(detail=publishable_detail()), "   ", "delivery_content"),
    ],
)
def test_build_template_rejects_missing_required_publish_fields(info, delivery, expected):
    with pytest.raises(ValueError, match=expected):
        build_template_from_item_info("cookie-1", "item-1", info, delivery)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_build_template_rejects_non_finite_price(invalid):
    detail = publishable_detail()
    detail["price"] = invalid

    with pytest.raises(ValueError, match="finite"):
        build_template_from_item_info("cookie-1", "item-1", item_info(detail=detail), DEFAULT_LINK)


def test_build_template_does_not_use_title_as_missing_description():
    info = {"item_title": "原标题", "item_detail_parsed": publishable_detail()}

    with pytest.raises(ValueError, match="description"):
        build_template_from_item_info("cookie-1", "item-1", info, DEFAULT_LINK)


def test_rotate_current_item_id_updates_store_transactionally(store):
    template = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    store.upsert_template(template)

    rotated = rotate_current_item_id(store, template.template_id, "item-2")

    assert rotated.current_item_id == "item-2"
    assert store.get_template(template_id=template.template_id).current_item_id == "item-2"
    assert store.get_template(cookie_id="cookie-1", current_item_id="item-1") is None


def test_rotate_current_item_id_rejects_empty_and_same_id(store):
    template = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    store.upsert_template(template)

    for new_item_id in ("", "   ", "item-1", None):
        with pytest.raises(ValueError, match="item_id"):
            rotate_current_item_id(store, template.template_id, new_item_id)


@pytest.mark.parametrize("invalid_item_id", ["", "   ", "item\x00invalid", "item\x01invalid"])
def test_rotate_current_item_id_rejects_invalid_ids_without_changing_store(store, invalid_item_id):
    template = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    store.upsert_template(template)

    with pytest.raises(ValueError, match="item_id"):
        rotate_current_item_id(store, template.template_id, invalid_item_id)

    persisted = store.get_template(template_id=template.template_id)
    assert persisted.current_item_id == "item-1"


def test_rotate_current_item_id_rejects_account_conflict(store):
    first = build_template_from_item_info(
        "cookie-1", "item-1", item_info(detail=publishable_detail()), DEFAULT_LINK
    )
    second = build_template_from_item_info(
        "cookie-1", "item-2", item_info(detail=publishable_detail()), SPECIAL_LINK
    )
    store.upsert_template(first)
    store.upsert_template(second)

    with pytest.raises(ValueError, match="already used"):
        rotate_current_item_id(store, first.template_id, "item-2")


def test_template_and_service_representations_do_not_leak_delivery_content():
    template = build_template_from_item_info(
        "cookie-1",
        "item-1",
        item_info(detail=publishable_detail()),
        DEFAULT_LINK,
        sku_delivery={"sku-1": SPECIAL_LINK},
    )

    assert DEFAULT_LINK.strip() not in repr(template)
    assert SPECIAL_LINK not in repr(template)
    assert DEFAULT_LINK.strip() not in safe_delivery_summary(DEFAULT_LINK)
    assert SPECIAL_LINK not in safe_delivery_summary(SPECIAL_LINK)
