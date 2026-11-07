import json
import unittest
from decimal import Decimal as D

from dafeiyu.providers import FetchError, parse_funds, parse_sina, parse_tencent, parse_yahoo


def tencent_line(code="sz000001", *, name="平安银行", price="11.81"):
    values = [""] * 35
    for index, value in {1: name, 3: price, 30: "20261106161500", 31: "0.17", 32: "1.46", 33: "11.85", 34: "11.57"}.items():
        values[index] = value
    return f'v_{code}="' + "~".join(values) + '";'


def yahoo_payload(**changes):
    meta = {
        "symbol": "BTC-USD", "currency": "USD", "shortName": "Bitcoin USD",
        "regularMarketPrice": 76000, "regularMarketTime": 1794009600,
        "regularMarketDayHigh": 77000, "regularMarketDayLow": 75000,
    }
    meta.update(changes)
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": meta, "indicators": {"quote": [{"close": [74000, 75000, 76000]}]},
    }]}})


class TencentTests(unittest.TestCase):
    def test_bad_symbol_does_not_discard_valid_quote(self):
        text = 'v_sh999999="1~broken";\n' + tencent_line()
        batch = parse_tencent(text, ["sh999999", "sz000001", "hk00700"])
        quote = batch.quotes["stock", "sz000001"]
        self.assertEqual(quote.price, D("11.81"))
        self.assertEqual(quote.market_time, "2026-11-06 16:15:00")
        self.assertIn(("stock", "sh999999"), batch.errors)
        self.assertIn(("stock", "hk00700"), batch.errors)

    def test_currency_is_preserved_per_market(self):
        for code, currency in (("sh000001", "CNY"), ("hk00700", "HKD"), ("usAAPL", "USD")):
            with self.subTest(code=code):
                batch = parse_tencent(tencent_line(code), [code])
                self.assertEqual(batch.quotes["stock", code].currency, currency)

    def test_zero_or_nonfinite_prices_are_not_valid_quotes(self):
        for price in ("0", "NaN", "--"):
            with self.subTest(price=price):
                batch = parse_tencent(tencent_line(price=price), ["sz000001"])
                self.assertEqual(batch.quotes, {})
                self.assertIn(("stock", "sz000001"), batch.errors)


class SinaTests(unittest.TestCase):
    def test_previous_close_zero_does_not_divide_by_zero(self):
        fields = [""] * 32
        for index, value in {0: "测试", 2: "0", 3: "11", 4: "12", 5: "10", 30: "2026-11-06", 31: "15:00:00"}.items():
            fields[index] = value
        batch = parse_sina('var hq_str_sh000001="' + ",".join(fields) + '";', ["sh000001"])
        self.assertIsNone(batch.quotes["stock", "sh000001"].change_percent)


class FundTests(unittest.TestCase):
    def test_published_nav_is_not_mislabelled_as_an_estimate(self):
        text = json.dumps({"Datas": [{
            "FCODE": "110013", "SHORTNAME": "易方达科翔混合", "GSZ": None, "GSZZL": None,
            "NAV": "6.1170", "NAVCHGRT": "-1.20", "PDATE": "2026-11-06",
        }]})
        quote = parse_funds(text, ["110013"]).quotes["fund", "110013"]
        self.assertEqual(quote.price_kind, "净值")
        self.assertEqual(quote.price, D("6.1170"))
        self.assertEqual(quote.market_time, "2026-11-06")
        self.assertEqual(quote.change_percent, D("-1.20"))
        self.assertIsNone(quote.change)

    def test_available_estimate_is_marked(self):
        text = json.dumps({"Datas": [{"FCODE": "110013", "NAV": "1.2", "GSZ": "1.3", "GSZZL": "2", "GZTIME": "2026-11-06 14:00:00"}]})
        quote = parse_funds(text, ["110013"]).quotes["fund", "110013"]
        self.assertEqual(quote.price_kind, "估值")
        self.assertEqual(quote.price, D("1.3"))

    def test_html_or_missing_record_is_reported(self):
        with self.assertRaises(FetchError):
            parse_funds("<html>not found</html>", ["110013"])
        batch = parse_funds('{"Datas":[]}', ["110013"])
        self.assertIn(("fund", "110013"), batch.errors)


class YahooTests(unittest.TestCase):
    def test_published_quote_change_takes_precedence_over_chart_history(self):
        quote = parse_yahoo(yahoo_payload(fulldayChange=2000, regularMarketChangePercent=2.7), "BTC-USD")
        self.assertEqual(quote.change, D("2000"))
        self.assertEqual(quote.change_percent, D("2.7"))
        self.assertTrue(quote.market_time.endswith("Z"))

    def test_daily_history_fallback_uses_previous_day_not_start_of_range(self):
        quote = parse_yahoo(yahoo_payload(chartPreviousClose=70000), "BTC-USD")
        self.assertEqual(quote.change, D("1000"))
        self.assertAlmostEqual(float(quote.change_percent), 1.3333333333)

    def test_zero_percent_is_valid(self):
        quote = parse_yahoo(yahoo_payload(regularMarketChange=0, regularMarketChangePercent=0), "BTC-USD")
        self.assertEqual(quote.change_percent, D("0"))

    def test_wrong_symbol_and_invalid_responses_fail_clearly(self):
        for text in (yahoo_payload(symbol="ETH-USD"), "<html>bad</html>", '{"chart":{"result":null,"error":{"code":"Not Found"}}}'):
            with self.subTest(text=text[:50]), self.assertRaises(FetchError):
                parse_yahoo(text, "BTC-USD")
