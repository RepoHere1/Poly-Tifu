"""
Signer Module - EIP-712 Order Signing (CLOB V2)

Provides EIP-712 signature functionality for Polymarket orders
and authentication messages.

CLOB V2 changes:
- L1 auth still uses `ClobAuthDomain` v1 (unchanged).
- Orders sign against the `Polymarket CTF Exchange` v2 domain with
  an explicit `verifyingContract` (different for neg-risk markets).
- Order struct drops `taker`, `expiration`, `nonce`, `feeRateBps`
  and adds `timestamp` (ms), `metadata` (bytes32), `builder` (bytes32).
- Fees are operator-set at match time; no `feeRateBps` on orders.

Example:
    from src.signer import OrderSigner

    signer = OrderSigner(private_key)
    signature = signer.sign_order_dict(
        token_id="123...",
        price=0.65,
        size=10,
        side="BUY",
        maker="0x...",
    )
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address


USDC_DECIMALS = 6

# V2 Exchange contracts (Polygon mainnet)
EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"

ZERO_BYTES32_HEX = "0x" + "00" * 32


def _bytes32_from_hex(value: str) -> bytes:
    """Convert a 0x-prefixed 32-byte hex string to bytes."""
    if value is None:
        return b"\x00" * 32
    if value.startswith("0x") or value.startswith("0X"):
        value = value[2:]
    raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError(f"bytes32 must be 32 bytes, got {len(raw)}")
    return raw


@dataclass
class Order:
    """
    Represents a Polymarket CLOB V2 order.

    Attributes:
        token_id: ERC-1155 token ID for the market outcome
        price: Price per share (0 < p <= 1)
        size: Number of shares
        side: 'BUY' or 'SELL'
        maker: Maker wallet address (Safe/Proxy)
        signature_type: Signature type enum (2 = Gnosis Safe)
        neg_risk: Whether the market uses the Neg Risk exchange
        builder_code: bytes32 hex identifying the builder (zero if none)
        metadata: bytes32 hex, currently reserved (zero)
        salt: Random uint256 for struct uniqueness; auto-generated if None
        timestamp_ms: Order creation time in ms (auto-filled if None)
    """

    token_id: str
    price: float
    size: float
    side: str
    maker: str
    signature_type: int = 2
    neg_risk: bool = False
    builder_code: str = ZERO_BYTES32_HEX
    metadata: str = ZERO_BYTES32_HEX
    salt: Optional[int] = None
    timestamp_ms: Optional[int] = None

    # Computed
    maker_amount: str = field(init=False, default="0")
    taker_amount: str = field(init=False, default="0")
    side_value: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.side = self.side.upper()
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {self.side}")

        if not 0 < self.price <= 1:
            raise ValueError(f"Invalid price: {self.price}")

        if self.size <= 0:
            raise ValueError(f"Invalid size: {self.size}")

        if self.timestamp_ms is None:
            self.timestamp_ms = int(time.time() * 1000)

        if self.salt is None:
            self.salt = secrets.randbelow(2**64)

        # Validate bytes32 fields early
        _bytes32_from_hex(self.builder_code)
        _bytes32_from_hex(self.metadata)

        self.maker_amount = str(int(self.size * self.price * 10**USDC_DECIMALS))
        self.taker_amount = str(int(self.size * 10**USDC_DECIMALS))
        self.side_value = 0 if self.side == "BUY" else 1


class SignerError(Exception):
    """Base exception for signer operations."""


class OrderSigner:
    """
    Signs Polymarket V2 orders and L1 auth messages.

    - `sign_auth_message` uses the unchanged `ClobAuthDomain` v1.
    - `sign_order` uses the `Polymarket CTF Exchange` v2 domain with the
      correct `verifyingContract` for regular vs. neg-risk markets.
    """

    AUTH_DOMAIN = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137,
    }

    EXCHANGE_DOMAIN_NAME = "Polymarket CTF Exchange"
    EXCHANGE_DOMAIN_VERSION = "2"

    ORDER_TYPES = {
        "Order": [
            {"name": "salt", "type": "uint256"},
            {"name": "maker", "type": "address"},
            {"name": "signer", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
            {"name": "makerAmount", "type": "uint256"},
            {"name": "takerAmount", "type": "uint256"},
            {"name": "side", "type": "uint8"},
            {"name": "signatureType", "type": "uint8"},
            {"name": "timestamp", "type": "uint256"},
            {"name": "metadata", "type": "bytes32"},
            {"name": "builder", "type": "bytes32"},
        ]
    }

    def __init__(self, private_key: str, chain_id: int = 137):
        if private_key.startswith("0x"):
            private_key = private_key[2:]

        try:
            self.wallet = Account.from_key(f"0x{private_key}")
        except Exception as e:
            raise ValueError(f"Invalid private key: {e}")

        self.address = self.wallet.address
        self.chain_id = chain_id

    @classmethod
    def from_encrypted(cls, encrypted_data: dict, password: str) -> "OrderSigner":
        from .crypto import KeyManager

        manager = KeyManager()
        private_key = manager.decrypt(encrypted_data, password)
        return cls(private_key)

    def _exchange_domain(self, neg_risk: bool) -> Dict[str, Any]:
        return {
            "name": self.EXCHANGE_DOMAIN_NAME,
            "version": self.EXCHANGE_DOMAIN_VERSION,
            "chainId": self.chain_id,
            "verifyingContract": to_checksum_address(
                NEG_RISK_EXCHANGE_V2_ADDRESS if neg_risk else EXCHANGE_V2_ADDRESS
            ),
        }

    def sign_auth_message(
        self,
        timestamp: Optional[str] = None,
        nonce: int = 0,
    ) -> str:
        """Sign an L1 authentication message for API key derivation."""
        if timestamp is None:
            timestamp = str(int(time.time()))

        auth_types = {
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ]
        }

        message_data = {
            "address": self.address,
            "timestamp": timestamp,
            "nonce": nonce,
            "message": "This message attests that I control the given wallet",
        }

        signable = encode_typed_data(
            domain_data=self.AUTH_DOMAIN,
            message_types=auth_types,
            message_data=message_data,
        )

        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()

    def sign_order(self, order: Order) -> Dict[str, Any]:
        """Sign a V2 order. Returns a dict shaped for the POST /order body."""
        try:
            order_message = {
                "salt": int(order.salt),
                "maker": to_checksum_address(order.maker),
                "signer": self.address,
                "tokenId": int(order.token_id),
                "makerAmount": int(order.maker_amount),
                "takerAmount": int(order.taker_amount),
                "side": order.side_value,
                "signatureType": order.signature_type,
                "timestamp": int(order.timestamp_ms),
                "metadata": _bytes32_from_hex(order.metadata),
                "builder": _bytes32_from_hex(order.builder_code),
            }

            signable = encode_typed_data(
                domain_data=self._exchange_domain(order.neg_risk),
                message_types=self.ORDER_TYPES,
                message_data=order_message,
            )

            signed = self.wallet.sign_message(signable)
            signature_hex = "0x" + signed.signature.hex()

            wire_order = {
                "salt": str(int(order.salt)),
                "maker": to_checksum_address(order.maker),
                "signer": self.address,
                "tokenId": order.token_id,
                "makerAmount": str(int(order.maker_amount)),
                "takerAmount": str(int(order.taker_amount)),
                "side": order.side,
                "signatureType": order.signature_type,
                "timestamp": str(int(order.timestamp_ms)),
                "metadata": order.metadata,
                "builder": order.builder_code,
                "signature": signature_hex,
            }

            return {
                "order": wire_order,
                "signature": signature_hex,
                "signer": self.address,
                "price": order.price,
                "size": order.size,
            }

        except Exception as e:
            raise SignerError(f"Failed to sign order: {e}")

    def sign_order_dict(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        maker: str,
        neg_risk: bool = False,
        builder_code: str = ZERO_BYTES32_HEX,
        signature_type: int = 2,
    ) -> Dict[str, Any]:
        """Convenience wrapper: build an Order and sign it."""
        order = Order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            maker=maker,
            neg_risk=neg_risk,
            builder_code=builder_code,
            signature_type=signature_type,
        )
        return self.sign_order(order)

    def sign_message(self, message: str) -> str:
        """Sign a plain text message (non-EIP-712)."""
        from eth_account.messages import encode_defunct

        signable = encode_defunct(text=message)
        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()


# Backwards compatibility alias
WalletSigner = OrderSigner
