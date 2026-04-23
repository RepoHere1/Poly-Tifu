"""
Unit Tests for Signer Module (CLOB V2)

Run with:
    pytest tests/test_signer.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.signer import (
    EXCHANGE_V2_ADDRESS,
    NEG_RISK_EXCHANGE_V2_ADDRESS,
    ZERO_BYTES32_HEX,
    Order,
    OrderSigner,
)


class TestOrderSigner:
    """Tests for OrderSigner class."""

    TEST_PRIVATE_KEY = "0x" + "a" * 64

    def setup_method(self):
        self.signer = OrderSigner(self.TEST_PRIVATE_KEY)
        self.test_address = self.signer.address

    def test_signer_address_from_key(self):
        assert self.test_address.startswith("0x")
        assert len(self.test_address) == 42

    def test_invalid_key_raises(self):
        with pytest.raises(ValueError, match="Invalid private key"):
            OrderSigner("invalid_key")

    def test_from_encrypted_exists(self):
        assert hasattr(OrderSigner, "from_encrypted")

    def test_sign_auth_message(self):
        signature = self.signer.sign_auth_message()
        assert signature.startswith("0x")
        assert len(signature) == 132

    def test_sign_auth_message_with_timestamp(self):
        signature = self.signer.sign_auth_message(timestamp="1234567890")
        assert signature.startswith("0x")

    def test_sign_auth_message_with_nonce(self):
        signature = self.signer.sign_auth_message(nonce=42)
        assert signature is not None

    def test_exchange_domain_default(self):
        domain = self.signer._exchange_domain(neg_risk=False)
        assert domain["name"] == "Polymarket CTF Exchange"
        assert domain["version"] == "2"
        assert domain["chainId"] == 137
        assert domain["verifyingContract"].lower() == EXCHANGE_V2_ADDRESS.lower()

    def test_exchange_domain_neg_risk(self):
        domain = self.signer._exchange_domain(neg_risk=True)
        assert domain["verifyingContract"].lower() == NEG_RISK_EXCHANGE_V2_ADDRESS.lower()

    def test_sign_order_dict_basic(self):
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.test_address,
        )

        assert "order" in result
        assert "signature" in result
        assert "signer" in result

        order = result["order"]
        assert order["tokenId"] == "1234567890123456789"
        assert order["side"] == "BUY"
        assert order["signatureType"] == 2
        assert order["builder"] == ZERO_BYTES32_HEX
        assert order["metadata"] == ZERO_BYTES32_HEX
        assert "timestamp" in order
        assert "salt" in order
        # V2: signature embedded in order body as well
        assert order["signature"] == result["signature"]

    def test_sign_order_dict_sell_side(self):
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.35,
            size=5.0,
            side="SELL",
            maker=self.test_address,
        )
        assert result["order"]["side"] == "SELL"

    def test_sign_order_with_builder_code(self):
        builder_code = "0x" + "ab" * 32
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.test_address,
            builder_code=builder_code,
        )
        assert result["order"]["builder"].lower() == builder_code.lower()

    def test_sign_order_neg_risk(self):
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.test_address,
            neg_risk=True,
        )
        # Signature is deterministic per domain; no exception = pass,
        # plus sanity-check the shape.
        assert result["signature"].startswith("0x")
        assert len(result["signature"]) == 132

    def test_sign_order_generates_valid_signature(self):
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.test_address,
        )
        signature = result["signature"]
        assert signature.startswith("0x")
        assert len(signature) == 132


class TestOrder:
    """Tests for Order dataclass."""

    MAKER = "0x1234567890123456789012345678901234567890"

    def test_order_creation(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        assert order.token_id == "1234567890123456789"
        assert order.price == 0.65
        assert order.size == 10.0
        assert order.side == "BUY"

    def test_order_side_normalized_to_upper(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="buy",
            maker=self.MAKER,
        )
        assert order.side == "BUY"

    def test_order_invalid_side_raises(self):
        with pytest.raises(ValueError, match="Invalid side"):
            Order(
                token_id="1234567890123456789",
                price=0.65,
                size=10.0,
                side="INVALID",
                maker=self.MAKER,
            )

    def test_order_invalid_price_too_low(self):
        with pytest.raises(ValueError, match="Invalid price"):
            Order(
                token_id="1234567890123456789",
                price=0,
                size=10.0,
                side="BUY",
                maker=self.MAKER,
            )

    def test_order_invalid_price_above_one(self):
        with pytest.raises(ValueError, match="Invalid price"):
            Order(
                token_id="1234567890123456789",
                price=1.5,
                size=10.0,
                side="BUY",
                maker=self.MAKER,
            )

    def test_order_invalid_size_raises(self):
        with pytest.raises(ValueError, match="Invalid size"):
            Order(
                token_id="1234567890123456789",
                price=0.65,
                size=0,
                side="BUY",
                maker=self.MAKER,
            )

    def test_order_defaults_timestamp_ms(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        assert order.timestamp_ms is not None
        assert isinstance(order.timestamp_ms, int)
        # Must be milliseconds (>= 10^12 from ~2001)
        assert order.timestamp_ms > 10**12

    def test_order_defaults_salt(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        assert order.salt is not None
        assert isinstance(order.salt, int)

    def test_order_defaults_builder_code_zero(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        assert order.builder_code == ZERO_BYTES32_HEX
        assert order.metadata == ZERO_BYTES32_HEX

    def test_order_invalid_builder_code_length_raises(self):
        with pytest.raises(ValueError):
            Order(
                token_id="1234567890123456789",
                price=0.65,
                size=10.0,
                side="BUY",
                maker=self.MAKER,
                builder_code="0xabcd",  # not 32 bytes
            )

    def test_order_calculates_maker_amount(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        expected = str(int(10.0 * 0.65 * 1_000_000))
        assert order.maker_amount == expected

    def test_order_calculates_taker_amount(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        expected = str(int(10.0 * 1_000_000))
        assert order.taker_amount == expected

    def test_order_side_value_buy(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            maker=self.MAKER,
        )
        assert order.side_value == 0

    def test_order_side_value_sell(self):
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="SELL",
            maker=self.MAKER,
        )
        assert order.side_value == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
