"""
wallet_monitor.py
──────────────────
Standalone treasury balance watchdog for the AgriIntel float wallet.
Runs INDEPENDENTLY of the main FastAPI app — designed to run as a
Render Cron Job (or any scheduler), not inside the request/response cycle.

Checks the float wallet's live USDC balance on Algorand mainnet. If it
drops below the alert threshold, sends an alert via email and/or
WhatsApp — so a human actually notices, instead of it just sitting in
server logs nobody is watching at 2 AM.

Why this is separate from x402_client.py's _check_treasury():
    That one only logs (logger.warning/critical) from inside a live
    request — useful for server-log history, but silent otherwise.
    This script is the "wake a human up" layer, run on a fixed
    schedule regardless of whether any farmer is using the bot
    right now.

Env vars required:
    FLOAT_WALLET_MNEMONIC     — same wallet as x402_client.py
    WALLET_ALERT_THRESHOLD_USDC — default 0.008
    ALERT_EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD  (for email)
    ALERT_WHATSAPP_TO         — YOUR number (not a farmer's), for whatsapp.send_text()
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID  (whatsapp.py needs these too)
"""

import asyncio
import logging
import os
import smtplib
from email.mime.text import MIMEText

import httpx
from algosdk import account, mnemonic

logger = logging.getLogger(__name__)


def _load_dotenv(dotenv_path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs from a .env file into os.environ."""
    try:
        with open(dotenv_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        return


# ─── Config ───────────────────────────────────────────────────────────────────

_load_dotenv()

USDC_ASSET_ID = 31566704  # Mainnet USDC ASA ID — same constant as x402_client.py
ALGONODE_URL = "https://mainnet-api.algonode.cloud/v2/accounts"

ALERT_THRESHOLD_USDC = float(os.getenv("WALLET_ALERT_THRESHOLD_USDC", "0.2"))

FLOAT_WALLET_MNEMONIC = os.getenv("FLOAT_WALLET_MNEMONIC", "")

ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
SMTP_HOST      = os.getenv("SMTP_HOST", "")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")

ALERT_WHATSAPP_TO = os.getenv("ALERT_WHATSAPP_TO", "")  # YOUR number, never a farmer's


# ─── Wallet address (derived from mnemonic, no need for a 2nd env var) ────────

def _get_wallet_address() -> str:
    phrase = FLOAT_WALLET_MNEMONIC.strip()
    if not phrase:
        raise ValueError("FLOAT_WALLET_MNEMONIC env var is not set")
    private_key = mnemonic.to_private_key(phrase)
    return account.address_from_private_key(private_key)


# ─── Balance check (sync — simpler for a cron script, no event loop needed) ──

def get_usdc_balance(wallet_address: str) -> float:
    """Query AlgoNode for live USDC balance. Returns -1.0 on failure."""
    try:
        with httpx.Client(timeout=10.0) as http:
            res = http.get(f"{ALGONODE_URL}/{wallet_address}")
        if res.status_code == 200:
            for asset in res.json().get("assets", []):
                if asset.get("asset-id") == USDC_ASSET_ID:
                    return asset.get("amount", 0) / 1_000_000.0
            return 0.0
        logger.error(f"[WalletMonitor] Balance check HTTP {res.status_code}")
        return -1.0
    except Exception as e:
        logger.error(f"[WalletMonitor] Balance check failed: {e}")
        return -1.0


# ─── Alert channel 1: Email ────────────────────────────────────────────────────

def send_email_alert(balance: float, wallet_address: str) -> bool:
    if not (ALERT_EMAIL_TO and SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        logger.warning("[WalletMonitor] Email alert skipped — SMTP env vars not fully set")
        return False

    subject = f"AgriIntel wallet low: ${balance:.4f} USDC"
    body = (
        f"Float wallet {wallet_address} is at ${balance:.4f} USDC — "
        f"below your alert threshold of ${ALERT_THRESHOLD_USDC:.4f}.\n\n"
        f"Top it up before x402 payments start failing."
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ALERT_EMAIL_TO], msg.as_string())
        logger.info("[WalletMonitor] Email alert sent")
        return True
    except Exception as e:
        logger.error(f"[WalletMonitor] Email alert failed: {e}")
        return False


# ─── Alert channel 2: WhatsApp (reuses whatsapp.py — no duplicate API code) ───

def send_whatsapp_alert(balance: float, wallet_address: str) -> bool:
    if not ALERT_WHATSAPP_TO:
        logger.warning("[WalletMonitor] WhatsApp alert skipped — ALERT_WHATSAPP_TO not set")
        return False

    try:
        from whatsapp import send_text  # same module main.py uses for farmer replies

        text = (
            f"🚨 AgriIntel treasury alert\n"
            f"Wallet: {wallet_address[:8]}...\n"
            f"Balance: ${balance:.4f} USDC (threshold: ${ALERT_THRESHOLD_USDC:.4f})\n"
            f"Top up now — x402 payments will start failing soon."
        )
        result = asyncio.run(send_text(ALERT_WHATSAPP_TO, text))
        if result.get("error"):
            logger.error(f"[WalletMonitor] WhatsApp alert failed: {result}")
            return False
        logger.info("[WalletMonitor] WhatsApp alert sent")
        return True
    except Exception as e:
        logger.error(f"[WalletMonitor] WhatsApp alert failed: {e}")
        return False


# ─── Entry point ────────────────────────────────────────────────────────────────

def run_check() -> None:
    wallet_address = _get_wallet_address()
    balance = get_usdc_balance(wallet_address)

    if balance < 0:
        logger.error("[WalletMonitor] Could not fetch balance this run — skipping")
        return

    logger.info(f"[WalletMonitor] Current balance: ${balance:.4f} USDC")

    if balance < ALERT_THRESHOLD_USDC:
        logger.critical(f"🚨 Balance ${balance:.4f} is below threshold ${ALERT_THRESHOLD_USDC:.4f}")
        email_ok = send_email_alert(balance, wallet_address)
        whatsapp_ok = send_whatsapp_alert(balance, wallet_address)
        if not (email_ok or whatsapp_ok):
            logger.error("[WalletMonitor] ALERT FAILED ON BOTH CHANNELS — nobody was notified!")
    else:
        logger.info("[WalletMonitor] Balance healthy, no alert needed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_check()
