import unittest
from decimal import Decimal as D

from dafeiyu.config import ConfigError, parse_config
from dafeiyu.models import Asset, Quote, number, position


def quote(price="11.81"):
    return Quote("stock", "sz000001", "平安银行", D(price), D("0.17"), D("1.46"), D("11.85"), D("11.57"), "CNY", "2026-11-06 16:15:00", "腾讯")


class PositionTests(unittest.TestCase):
    def test_decimal_income_and_percentage(self):
        result = position(Asset("stock", "sz000001", D("10"), D("100")), quote())
        self.assertEqual(result.income, D("181.00"))
        self.assertEqual(result.income_percent, D("18.100"))

    def test_fractional_coin_position(self):
        result = position(Asset("coin", "BTC-USD", D("70000"), D("0.00000001")), quote("76000"))
        self.assertEqual(result.income, D("0.00006000"))

    def test_nonpositive_cost_has_income_but_no_percentage(self):
        for cost, expected in (("0", "110"), ("-2", "130")):
            with self.subTest(cost=cost):
                result = position(Asset("stock", "sz000001", D(cost), D("10")), quote("11"))
                self.assertEqual(result.income, D(expected))
                self.assertIsNone(result.income_percent)

    def test_missing_price_does_not_fabricate_loss(self):
        result = position(Asset("stock", "sz000001", D("10"), D("100")), None)
        self.assertIsNone(result.income)
        self.assertIsNone(result.income_percent)

    def test_missing_quantity_still_allows_percentage(self):
        result = position(Asset("stock", "sz000001", D("10")), quote())
        self.assertIsNone(result.income)
        self.assertEqual(result.income_percent, D("18.100"))

    def test_invalid_numbers_are_missing(self):
        for raw in (None, "--", "NaN", "Infinity", "-Infinity", "bad", True):
            with self.subTest(raw=raw):
                self.assertIsNone(number(raw))
        self.assertEqual(number("1,234.50"), D("1234.50"))
        self.assertEqual(number("-1.20%"), D("-1.20"))


class ConfigTests(unittest.TestCase):
    def test_three_categories_and_legacy_holding_format(self):
        config = parse_config({
            "stocks": ["SH600519,1500,100", "usaapl"],
            "funds": [{"code": "110013", "cost_price": "5.5", "quantity": 1000}],
            "coins": ["btc-usd"],
        })
        self.assertEqual([asset.code for asset in config.assets], ["sh600519", "usAAPL", "110013", "BTC-USD"])
        self.assertEqual(config.assets[0].quantity, D("100"))
        self.assertEqual(config.interval_seconds, 10)

    def test_invalid_refresh_intervals(self):
        for raw in (0, -1, "NaN", "Infinity", None, True, [], "bad"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_config({"stocks": ["sh000001"], "interval_seconds": raw})

    def test_invalid_asset_configuration(self):
        cases = [
            {},
            {"stocks": ["sh000001", "sh000001"]},
            {"stocks": ["600519"]},
            {"stocks": ["sh600519,1500"]},
            {"funds": ["11001"]},
            {"coins": ["BTC"]},
            {"stocks": [{"code": "sh600519", "cost_price": "NaN"}]},
            {"stocks": [{"code": "sh600519", "quantity": -1}]},
            {"stocks": ["sh000001"], "stock_source": []},
            {"stocks": ["hk00700"], "stock_source": "sina"},
            {"stocks": ["sh000001"], "unknown_option": True},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ConfigError):
                parse_config(case)

    def test_proxy_normalization_and_invalid_port(self):
        config = parse_config({"stocks": ["sh000001"], "proxy": "127.0.0.1:7890"})
        self.assertEqual(config.proxy, "http://127.0.0.1:7890")
        for proxy in ("", "http://localhost:0", "localhost", "socks5://localhost:1080"):
            with self.subTest(proxy=proxy), self.assertRaises(ConfigError):
                parse_config({"stocks": ["sh000001"], "proxy": proxy})
