"""Regression tests for pricing fixes from client screenshots."""
import pytest

from app.services.pricing_calculator import PricingCalculator, format_quote_message


@pytest.fixture
def calc():
    return PricingCalculator()


def test_lanyard_20mm_not_15(calc):
    r = calc.calculate("шнурок для бейджа", 100, size_mm="20", text="шнурки 20 мм 100 шт")
    assert r["success"]
    assert "20" in r["product_name"]
    assert "15" not in r["product_name"]
    assert r["unit_price"] == 30.5


def test_lanyard_two_carabiners_surcharge(calc):
    base = calc.calculate("шнурок", 100, size_mm="15", text="шнурок 15 мм 100 шт")
    two = calc.calculate(
        "шнурок", 100, size_mm="15", text="шнурок 15 мм з двома карабінами 100 шт"
    )
    assert two["success"]
    assert two["unit_price"] == pytest.approx(base["unit_price"] + 6)


def test_retractor_not_lanyard(calc):
    r = calc.calculate("ретрактор для бейджа з друком", 50, text="ретрактор для бейджа з друком")
    assert r["success"]
    assert "ретрактор" in r["product_name"].lower()
    assert "шнурок" not in r["product_name"].lower()
    assert r["unit_price"] == 155.0


def test_reflective_color_logo(calc):
    one = calc.calculate(
        "світловідбиваючий", 100, text="світловідбиваючі 100 шт з 1 колір"
    )
    color = calc.calculate(
        "світловідбиваючий", 100, text="світловідбиваючі 100 шт з кольоровим лого"
    )
    assert one["unit_price"] == 28.0
    assert color["unit_price"] == 38.0
    assert "кольоровий" in color["product_name"].lower()


def test_tyvek_both_variants(calc):
    multi = calc.calculate_multi_variants(
        "контрольні браслети",
        100,
        text="контрольні браслети з кольоровим лого і одноколірним 100 шт",
    )
    assert multi and multi.get("multi_quotes")
    assert len(multi["multi_quotes"]) == 2
    prices = {q["unit_price"] for q in multi["multi_quotes"]}
    assert 2.85 in prices and 3.40 in prices


def test_slap_extra_colors(calc):
    one = calc.calculate("slap", 50, text="слеп 50 шт 1 колір")
    four = calc.calculate("slap", 50, text="слеп 50 шт 4 кольори", print_colors=4)
    assert one["unit_price"] == 65.0
    assert four["unit_price"] == 95.0
    two = calc.calculate("slap", 50, text="слеп 50 шт 2 кольори", print_colors=2)
    three = calc.calculate("slap", 50, text="слеп 50 шт 3 кольори", print_colors=3)
    assert two["unit_price"] == 75.0
    assert three["unit_price"] == 85.0


def test_slap_starting_shows_color_table_and_both_variants(calc):
    both = calc.starting_quote(
        "slap",
        text="силіконові слеп браслети з логотипом 1 колір та 4 кольори",
    )
    assert both
    assert "65" in both and "95" in both
    assert "1 колір" in both and "4 кольор" in both
    assert "silicon.com.ua/silikonovi-slep-braslety" in both

    only4 = calc.starting_quote("slap", text="слеп з друком лого 4 кольори")
    assert only4 and "95" in only4
    assert "4 кольор" in only4
    # лише 4 кольори — без нав'язування таблиці 1 кольору як основної відповіді
    assert only4.lower().count("1 колір") <= 1

    table = calc.starting_quote("slap", text="слеп браслети яка вартість")
    assert "75" in table and "85" in table and "95" in table


def test_slap_multi_1_and_4_with_qty(calc):
    multi = calc.calculate_multi_variants(
        "slap", 100, text="слеп 100 шт 1 колір та 4 кольори"
    )
    assert multi and multi.get("multi_quotes")
    prices = {q["unit_price"] for q in multi["multi_quotes"]}
    assert 60.0 in prices and 90.0 in prices


def test_swim_cap_4plus4(calc):
    r = calc.calculate("шапочки", 10, text="шапочки з друком лого 4 кольори 10 шт")
    assert r["success"]
    assert r["unit_price"] == 1200.0


def test_brelok_black_carabiner(calc):
    r = calc.calculate(
        "брелок з чорним карабіном", 100, size_mm="20", text="брелок з чорним карабіном 20 мм 100"
    )
    assert r["success"]
    assert r["unit_price"] == 42.5


def test_fabric_15mm_price(calc):
    r = calc.calculate("тканинний браслет", 200, text="тканинні 200 шт")
    assert r["unit_price"] == 24.0


def test_urgent_doubles_price(calc):
    normal = calc.calculate("силіконовий", 100, size_mm="12")
    urgent = calc.calculate("силіконовий", 100, size_mm="12", urgent=True)
    assert urgent["unit_price"] == pytest.approx(normal["unit_price"] * 2)


def test_multi_quote_message_lists_both(calc):
    multi = calc.calculate_multi_variants(
        "світловідбиваючі",
        100,
        text="світловідбиваючі 100 шт 1 колір та кольоровим лого",
    )
    assert multi
    msg = format_quote_message(multi, with_confirm=False)
    assert "1 колір" in msg or "28" in msg
    assert "кольоровий" in msg or "38" in msg


def test_swim_cap_only_4_colors_not_1plus1(calc):
    multi = calc.calculate_multi_variants(
        "шапочки", 10, text="шапочки з друком логотипу 4 кольори 10 шт"
    )
    assert multi
    assert multi.get("product_key") == "swim_cap_4plus4"
    assert not multi.get("multi_quotes") or len(multi.get("multi_quotes") or []) == 1
    assert multi["unit_price"] == 1200.0


def test_cups_1_and_4_includes_manager_note(calc):
    multi = calc.calculate_multi_variants(
        "екочашки",
        50,
        text="екочашки 350 мл та 450 мл з логотипом 1 колір та з логотипом 4 кольори 50 штук",
    )
    assert multi and multi.get("needs_manager")
    assert multi.get("multi_quotes") and len(multi["multi_quotes"]) == 2
    msg = format_quote_message(multi, with_confirm=False)
    assert "350" in msg and "450" in msg
    assert "макет" in msg.lower()


def test_lanyard_general_faq_not_15mm_default(calc):
    start = calc.starting_quote("шнурки для бейджа", text="Яка вартість шнурків для бейджа?")
    assert start
    assert "15 мм, 20 мм, 25 мм, 30 мм, 40 мм" in start
    assert "репсова" in start.lower()
    assert "гладенька" in start.lower()


def test_tyvek_bahatokolor_and_odnokolir(calc):
    multi = calc.calculate_multi_variants(
        "tyvek",
        100,
        text="контрольні браслети з багатоколірним та одноколірним лого 100 шт",
    )
    assert multi and multi.get("multi_quotes")
    assert len(multi["multi_quotes"]) == 2
    names = " ".join(q["product_name"].lower() for q in multi["multi_quotes"])
    assert "1 колір" in names
    assert "кольор" in names


def test_silicone_starting_with_term_mentions_days(calc):
    start = calc.starting_quote(
        "браслети",
        text="Яка вартість та термін виготовлення браслетів?",
    )
    assert start
    assert "5–7" in start or "5-7" in start
    assert "робоч" in start.lower()


def test_silicone_multi_layout_surcharge(calc):
    r2 = calc.calculate("силіконовий", 100, size_mm="12", text="202*12 100 шт 2 різні макети")
    assert r2["total_amount"] == 1350.0
    assert r2["base_total"] == 1150.0
    r4 = calc.calculate("силіконовий", 100, size_mm="12", text="202*12 100 шт 4 різні макети")
    assert r4["total_amount"] == 1750.0


def test_caps_extra_layout_is_300(calc):
    r = calc.calculate("шапочки", 10, text="шапочки 1+1 10 шт 2 різні макети")
    assert r["extras_total"] == 300.0


def test_silicone_multi_size_surcharge(calc):
    r = calc.calculate("силіконовий", 100, size_mm="12", text="100 шт 30 шт 202*12, 70 шт 180*12")
    assert r["total_amount"] == 1350.0
    r3 = calc.calculate("силіконовий", 100, size_mm="12", text="100 шт 30 шт 202*12, 30 шт 180*12, 40 шт 160*12")
    assert r3["total_amount"] == 1550.0


def test_segmented_bracelet_pricing(calc):
    no_tie = calc.calculate("силіконовий", 100, size_mm="12", text="двохсегментний браслет з друком лого 100 шт")
    assert no_tie["unit_price"] == 13.5
    tie1 = calc.calculate("силіконовий", 100, size_mm="12", text="сегментний браслет з прив'язкою до одного сегмента 100 шт")
    assert tie1["unit_price"] == 15.5
    tie2 = calc.calculate("силіконовий", 100, size_mm="12", text="сегментний браслет з прив'язкою до двох сегментів 100 шт")
    assert tie2["unit_price"] == 17.5


def test_carabiner_fixator_unit_prices():
    from app.services.conversation_flow import _carabiner_fixator_unit
    assert _carabiner_fixator_unit(20) == 54
    assert _carabiner_fixator_unit(50) == 54
    assert _carabiner_fixator_unit(100) == 48
    assert _carabiner_fixator_unit(300) == 42
    assert _carabiner_fixator_unit(1000) == 36


def test_badge_pocket_65x115_site_price(calc):
    r = calc.calculate(
        "кишенька для бейджа",
        100,
        text="Кишенька для бейджа 65*115 мм - 100 шт",
    )
    assert r["success"]
    assert r["unit_price"] == 12.0
    assert r["total_amount"] == 1200.0
    assert "кишеньк" in r["product_name"].lower()
    assert "шнур" not in r["product_name"].lower()
